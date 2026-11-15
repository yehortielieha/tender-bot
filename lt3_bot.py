"""
LT3 tender bot for RITCxDublin 2026 (Liability Trading Case).

Each loop (about 20 a second) it:
  1. reads the case, positions, both order books and pending tenders from the RIT Client API;
  2. values each new tender: walks the order book, takes off fees, prices the risk of working
     the rest of the block over time and checks the 100k net / 250k gross limits. It then
     accepts, declines, or keeps re-checking a borderline tender inside its 30-second window;
  3. unwinds positions: takes liquidity that already beats breakeven, follows a linear schedule
     (crossing the spread a little when behind), rests limit orders at the top of the book for
     the rest, and forces everything flat before the heat ends.

Rules it keeps (from the case brief): it only trades to reduce a position, never trades the side
a pending tender needs before accepting it, and never lets its own orders grow or flip a position.

  python lt3_bot.py            trade through the RIT Client (localhost:9999)
  python lt3_bot.py --dry-run  print tender decisions only; never accepts, declines or trades
  python lt3_bot.py --check    show what the API returns, then exit
"""
import argparse
import collections
import concurrent.futures
import threading
import datetime as dt
import json
import math
import os
import re
import signal
import statistics
import sys
import time
import traceback
from dataclasses import dataclass, field

try:
    import requests
except ImportError:
    sys.exit('The "requests" package is missing. Run setup.bat first.')

import settings as S

stop_requested = False
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
PATH_TICKS = 30                                          # ticks of price path logged after each tender
BOOK_LEVELS = 20                                         # price levels per side saved in book snapshots


def _request_stop(signum, frame):
    global stop_requested
    stop_requested = True


class ApiKeyError(Exception):
    pass


# ----------------------------------------------------------------------------- logging

class Log:
    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, f'lt3_{dt.datetime.now():%Y%m%d_%H%M%S}.log')
        self._file = open(self.path, 'a', encoding='utf-8')
        self.listeners = []                              # e.g. the dashboard
        self._lock = threading.Lock()                    # orders are sent from several threads

    def _write(self, tag, msg):
        line = f'{dt.datetime.now():%H:%M:%S.%f}'[:-3] + f' {tag:<6} {msg}'
        with self._lock:
            print(line, flush=True)
            self._file.write(line + '\n')
            self._file.flush()
            for listener in self.listeners:
                listener(tag, line)

    def info(self, msg):
        self._write('', msg)

    def warn(self, msg):
        self._write('WARN', msg)

    def tender(self, msg):
        self._write('TENDER', msg)

    def trade(self, msg):
        self._write('TRADE', msg)


# ----------------------------------------------------------------------------- API

def _json(resp):
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class RIT:
    """Thin wrapper around the RIT Client REST API."""

    def __init__(self, base_url, api_key, log):
        self.base = base_url.rstrip('/')
        self.log = log
        self.api_key = api_key
        self._local = threading.local()                  # one HTTP session per thread

    @property
    def http(self):
        session = getattr(self._local, 'session', None)
        if session is None:
            session = requests.Session()
            session.trust_env = False                    # never route localhost calls through a proxy
            session.headers.update({'X-API-Key': self.api_key})
            self._local.session = session
        return session

    def call(self, method, path, params=None):
        resp = None
        for _ in range(8):
            resp = self.http.request(method, self.base + path, params=params, timeout=S.HTTP_TIMEOUT)
            if resp.status_code == 401:
                raise ApiKeyError('the RIT Client rejected the API key, or it isn\'t logged in yet. If the key '
                                  'changed, restart the bot and paste the new one when it asks')
            if resp.status_code != 429:
                return resp
            wait = float(_json(resp).get('wait') or 0.1)    # rate limited: wait as long as RIT says
            time.sleep(min(max(wait, 0.02), 1.0))
        return resp

    def get(self, path, **params):
        resp = self.call('GET', path, params or None)
        if resp.status_code != 200:
            raise RuntimeError(f'GET {path} returned {resp.status_code}: {resp.text[:200]}')
        return resp.json()

    def case(self):
        return self.get('/case')

    def trader(self):
        return self.get('/trader')

    def limits(self):
        return self.get('/limits')

    def securities(self):
        return {s['ticker']: s for s in self.get('/securities')}

    def security(self, ticker):
        rows = self.get('/securities', ticker=ticker)
        return rows[0] if rows else {}

    def book(self, ticker):
        return self.get('/securities/book', ticker=ticker, limit=S.BOOK_DEPTH)

    def history(self, ticker):
        return self.get('/securities/history', ticker=ticker, limit=S.VOL_WINDOW)

    def tenders(self):
        return self.get('/tenders')

    def open_orders(self):
        return self.get('/orders', status='OPEN')

    def order(self, ticker, action, qty, price=None):
        """MARKET order when price is None, otherwise LIMIT. Returns the order, or None if rejected."""
        params = {'ticker': ticker, 'action': action, 'quantity': int(qty)}
        if price is None:
            params['type'] = 'MARKET'
        else:
            params.update(type='LIMIT', price=f'{price:.2f}')
        resp = self.call('POST', '/orders', params)
        if resp.status_code == 200:
            return resp.json()
        self.log.warn(f'order rejected: {action} {qty} {ticker} @ {price}: {resp.status_code} {resp.text[:160]}')
        return None

    def cancel_ticker(self, ticker):
        return self.call('POST', '/commands/cancel', {'ticker': ticker}).status_code == 200

    def cancel_all(self):
        return self.call('POST', '/commands/cancel', {'all': 1}).status_code == 200

    def accept(self, tender_id):
        resp = self.call('POST', f'/tenders/{tender_id}')
        return resp.status_code == 200 and _json(resp).get('success', True) is not False

    def decline(self, tender_id):
        return self.call('DELETE', f'/tenders/{tender_id}').status_code == 200


# ----------------------------------------------------------------------------- market data

def book_levels(orders, own_id, descending):
    """Aggregate book orders into [(price, shares)], best price first, leaving out our own orders."""
    agg = collections.defaultdict(int)
    for o in orders or []:
        if own_id is not None and o.get('trader_id') == own_id:
            continue
        left = int(o.get('quantity') or 0) - int(o.get('quantity_filled') or 0)
        if left > 0:
            agg[round(float(o['price']), 2)] += left
    return sorted(agg.items(), reverse=descending)


def _fmt(price):
    return '-' if price is None else f'{price:.2f}'


class Market:
    """One stock at one moment: our position plus the order book without our own orders."""

    def __init__(self, ticker, sec, book, own_id):
        self.ticker = ticker
        self.bids = book_levels(book.get('bids'), own_id, descending=True)
        self.asks = book_levels(book.get('asks'), own_id, descending=False)
        self.best_bid = self.bids[0][0] if self.bids else None
        self.best_ask = self.asks[0][0] if self.asks else None
        if self.best_bid is not None and self.best_ask is not None:
            self.mid = (self.best_bid + self.best_ask) / 2
        else:
            self.mid = self.best_bid or self.best_ask or float(sec.get('last') or 0) or None
        self.position = int(sec.get('position') or 0)
        self.api_position = self.position                    # what RIT reports (position may be overridden)
        self.realized = float(sec.get('realized') or 0)      # realized P&L incl. commissions
        self.cost = float(sec.get('vwap') or 0)              # average price of the open position
        self.fee = max(float(sec.get('trading_fee') or 0), S.FEE_PER_SHARE)
        self.max_size = int(sec.get('max_trade_size') or S.MAX_ORDER_SIZE.get(ticker, 10_000))


def sweep(levels, qty, stop_price, selling, share=1.0):
    """Walk the book best-first, taking up to `share` of each level and never going past
    stop_price. Returns (shares, cash)."""
    got, cash = 0, 0.0
    for price, avail in levels:
        if (selling and price < stop_price - 1e-9) or (not selling and price > stop_price + 1e-9):
            break
        take = min(qty - got, int(avail * share))
        if take > 0:
            got += take
            cash += take * price
        if got >= qty:
            break
    return got, cash


def close_out_value(m, position, ticks_left):
    """Expected net cash from closing `position` (positive when selling a long, negative when
    buying back a short), and how many shares would have to be worked over time."""
    if position == 0:
        return 0.0, 0
    selling = position > 0
    qty = abs(position)
    if selling:
        got, cash = sweep(m.bids, qty, m.mid - S.IMMEDIATE_WINDOW, True, S.DEPTH_SHARE)
    else:
        got, cash = sweep(m.asks, qty, m.mid + S.IMMEDIATE_WINDOW, False, S.DEPTH_SHARE)
    later = qty - got
    # shares worked over time give up PATIENT_DISCOUNT vs the mid; more if time is short
    discount = S.PATIENT_DISCOUNT.get(m.ticker, 0.03) * max(1.0, S.UNWIND_TICKS / max(ticks_left, 1))
    cash += later * (m.mid - discount if selling else m.mid + discount)
    fees = qty * m.fee
    return (cash - fees if selling else -(cash + fees)), later


class VolTracker:
    """Per-tick volatility: standard deviation in $ of the mid from one tick to the next."""

    def __init__(self):
        self.mids = collections.defaultdict(lambda: collections.deque(maxlen=S.VOL_WINDOW))
        self.seen_tick = {}

    def seed(self, ticker, closes):
        for close in closes[-S.VOL_WINDOW:]:
            self.mids[ticker].append(close)

    def update(self, ticker, tick, mid):
        if mid is not None and self.seen_tick.get(ticker) != tick:
            self.seen_tick[ticker] = tick
            self.mids[ticker].append(mid)

    def move(self, ticker, ticks):
        """Change in the mid over the last `ticks` ticks (None until there's enough history)."""
        series = self.mids[ticker]
        return series[-1] - series[-1 - ticks] if len(series) > ticks else None

    def fair(self, ticker, ticks):
        """Average mid over the last `ticks` ticks (None until there are three)."""
        series = list(self.mids[ticker])[-max(1, int(ticks)):]
        return sum(series) / len(series) if len(series) >= 3 else None

    def spike(self, ticker, size, within):
        """The most recent one-tick jump in the mid of at least `size` in the last `within` ticks, else 0."""
        series = self.mids[ticker]
        if size <= 0:
            return 0.0
        for age in range(min(within, len(series) - 1)):
            jump = series[-1 - age] - series[-2 - age]
            if abs(jump) >= size:
                return jump
        return 0.0

    def get(self, ticker):
        series = list(self.mids[ticker])
        if len(series) < 10:
            return S.DEFAULT_VOL_PER_TICK
        steps = [b - a for a, b in zip(series, series[1:])]
        return max(statistics.pstdev(steps), 0.005)


# ----------------------------------------------------------------------------- tenders

CAPTION_SIDE = re.compile(r'would you like to (buy|sell)', re.IGNORECASE)


def tender_side(tender):
    """The side we trade if we accept (BUY = we buy the block from the client). RIT reports it from
    the trader's point of view; the caption ("...Would you like to BUY the shares?") is a cross-check."""
    side = str(tender.get('action') or '').upper()
    match = CAPTION_SIDE.search(tender.get('caption') or '')
    if match and match.group(1).upper() != side:
        caption_side = match.group(1).upper()
        return caption_side, (f'API action {side or "?"} disagrees with the caption; '
                              f'going with the caption ({caption_side})')
    return side, None


def tender_ticker(tender):
    if tender.get('ticker'):
        return tender['ticker']
    caption = tender.get('caption') or ''
    return next((tk for tk in S.TICKERS if tk in caption), None)


@dataclass
class Verdict:
    side: str            # what we do if we accept: BUY = we buy the block from the client
    qty: int
    price: float
    edge: float          # expected profit per tender share, after fees
    required: float      # what we demand per share before accepting
    to_work: int         # shares we would have to work over time after accepting
    blockers: list = field(default_factory=list)
    warning: str = None
    trend: float = 0.0   # part of the edge that comes from the recent price trend (can be negative)

    @property
    def ok(self):
        return not self.blockers and self.edge >= self.required


def per_stock(setting, ticker, default=0.0):
    """A setting that may be one number or one number per stock."""
    if isinstance(setting, dict):
        return float(setting.get(ticker, default))
    return float(setting)


def evaluate(tender, m, positions, limits, tick, ticks_per_period, vol, trend_move=None, fair=None):
    """Value a tender against the current book. Works on the change in our whole position, so a
    tender that offsets what we already hold is valued correctly. trend_move is the change in the
    mid over the last TREND_TICKS ticks: where prices trend, the shares that have to be worked over
    time will on average go out at a better (or worse) price than the mid suggests now."""
    side, warning = tender_side(tender)
    qty, price = int(tender['quantity']), float(tender['price'])
    now = m.position
    after = now + qty if side == 'BUY' else now - qty
    ticks_left = ticks_per_period - S.END_FLAT_TICKS - tick
    value_now, work_now = close_out_value(m, now, ticks_left)
    value_after, work_after = close_out_value(m, after, ticks_left)
    cash = (-qty * price if side == 'BUY' else qty * price) - qty * S.TENDER_FEE_PER_SHARE
    edge = (cash + value_after - value_now) / qty
    extra_work = max(0, work_after - work_now)
    risk = vol * math.sqrt(S.UNWIND_TICKS / 3.0) * extra_work / qty   # $/share std dev while unwinding
    trend = 0.0
    beta = per_stock(S.TREND_ACCEPT_BETA, m.ticker)
    if beta and trend_move is not None and qty:
        favourable = trend_move if side == 'BUY' else -trend_move      # we buy the block, then sell it
        trend = max(-S.TREND_ACCEPT_CAP, min(S.TREND_ACCEPT_CAP, beta * favourable)) * extra_work / qty
        edge += trend
    # Fair price: with real traders a jump in the mid mostly comes back (live session, 10 Nov: 77% of the
    # gap to the 10-tick average closed within a few ticks). So the shares that have to be worked over
    # time are valued part-way back towards that average, not at a mid that has just spiked.
    weight = per_stock(S.FAIR_WEIGHT, m.ticker)
    if weight and fair is not None and m.mid is not None and qty:
        favourable = fair - m.mid if side == 'BUY' else m.mid - fair   # we buy the block, then sell it
        back = max(-S.FAIR_CAP, min(S.FAIR_CAP, weight * favourable)) * extra_work / qty
        edge += back
        trend += back
    v = Verdict(side, qty, price, edge, S.MIN_EDGE + S.RISK_K * risk, work_after, warning=warning, trend=trend)

    book = dict(positions)
    book[m.ticker] = after
    net, gross = sum(book.values()), sum(abs(x) for x in book.values())
    net_limit, gross_limit = limits
    if side not in ('BUY', 'SELL'):
        v.blockers.append(f'unknown tender action {side!r}')
    if abs(net) > net_limit:
        v.blockers.append(f'net position would be {net:+,} (limit {net_limit:,})')
    if gross > gross_limit:
        v.blockers.append(f'gross position would be {gross:,} (limit {gross_limit:,})')
    if ticks_left <= 0 and work_after > 0:
        v.blockers.append('too close to the end of the heat to unwind')
    return v


def read_limits(api):
    """(net limit, gross limit) from the API, falling back to the case brief's numbers."""
    try:
        rows = api.limits()
        nets = [int(r['net_limit']) for r in rows if r.get('net_limit')]
        grosses = [int(r['gross_limit']) for r in rows if r.get('gross_limit')]
        return (min(nets) if nets else S.NET_LIMIT), (min(grosses) if grosses else S.GROSS_LIMIT)
    except Exception:
        return S.NET_LIMIT, S.GROSS_LIMIT


# ----------------------------------------------------------------------------- order helpers

def passive_price(m, selling, improve=0.01):
    """Join the best price on our side, or improve it by a cent when the spread allows (by `improve`
    after a price spike, but never past the middle of the spread). Never crosses."""
    bid, ask = m.best_bid, m.best_ask
    if improve > 0.01 and bid is not None and ask is not None:
        improve = max(0.01, min(improve, round((ask - bid) / 2 - 0.01, 2)))
    if selling:
        if ask is None:
            return round(bid + 0.02, 2) if bid is not None else None
        px = ask - improve if bid is not None and ask - bid > 0.015 else ask
        if bid is not None and px <= bid:
            px = bid + 0.01
    else:
        if bid is None:
            return round(ask - 0.02, 2) if ask is not None else None
        px = bid + improve if ask is not None and ask - bid > 0.015 else bid
        if ask is not None and px >= ask:
            px = ask - 0.01
    return round(px, 2)


def orders_match(orders, side, price, want, position):
    """True if our resting orders are already where we want them. `price` may be a ladder (a list):
    then one order must sit exactly at the first price (the top of the book) and the others within
    2c of one of the further prices, so a ladder isn't re-sent every time its levels shift a cent."""
    if not orders:
        return False
    ladder = list(price) if isinstance(price, (list, tuple)) else [price]
    left, at_top = 0, False
    for o in orders:
        if o.get('action') != side or o.get('type') != 'LIMIT':
            return False
        px = float(o.get('price') or 0)
        if abs(px - ladder[0]) <= 1e-6:
            at_top = True
        elif not any(abs(px - p) <= 0.02 + 1e-6 for p in ladder[1:]):
            return False
        left += int(o.get('quantity') or 0) - int(o.get('quantity_filled') or 0)
    return at_top and 0.5 * want <= left <= position


# ----------------------------------------------------------------------------- the bot

class Bot:
    def __init__(self, api, log, dry_run=False, dash=None):
        self.api, self.log, self.dry, self.dash = api, log, dry_run, dash
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=10)
        self.heats_done = 0
        self.heats_started = 0
        self.patient_heat = False                        # is this heat unwound patiently? (PATIENT_MODE)
        self.own_id = None
        self.in_heat = False
        self._new_heat_state()

    def _new_heat_state(self):
        self.tender_state = {}                          # tender_id -> watching/accepted/declined/ignored
        self.plans = {}                                 # ticker -> (start tick, shares to unwind)
        self.entry_price = {}                           # ticker -> last accepted tender price
        self.last_requote = collections.defaultdict(float)
        self.base = {}                                  # ticker -> position before any tender or order we know of
        self.tender_delta = collections.defaultdict(int)   # ticker -> shares from tenders accepted this heat
        self.own = {}                                   # order_id -> our order: ticker, side, shares filled
        self.unconfirmed = set()                        # tickers with an order RIT hasn't confirmed as finished
        self.freeze = {}                                # ticker -> no trading until then (an order's reply was lost)
        self.mismatch = {}                              # ticker -> (our count, RIT's count, since when)
        self.tender_books = {}                          # ticker -> accepted tenders being unwound, for P&L
        self.holds = {}                                 # ticker -> trend hold state for the current unwind
        self.dumping = set()                            # tickers due one deeper sweep (trend against us)
        self.impatient = set()                          # tickers unwound the fast way even in a patient heat
        self.crossed_tick = {}                          # ticker -> tick in which we last crossed the spread
        self.spike_logged = {}                          # ticker -> (tick, in our favour) of the last spike logged
        self.tender_rows = {}                           # tender_id -> row shown on the dashboard
        self.tender_info = {}                           # tender_id -> {'first': tick, 'peak': best edge}
        self.paths = []                                 # price paths after each tender (logged to CSV)
        self.heat_id = f'{dt.datetime.now():%Y%m%d_%H%M%S}'   # labels this heat in the recorded data
        self.fills_written = set()                      # order ids already saved to fills.csv
        self.auto_paused = None                        # reason, when the safety stop has paused tenders
        self.nlv, self._nlv_tick, self._next_fills = None, None, 0.0
        self.vol = VolTracker()
        self.limits = (S.NET_LIMIT, S.GROSS_LIMIT)
        self.last_tick = -1
        self.period = None
        self.next_status = 0.0

    # -- heat lifecycle -------------------------------------------------------------

    def step(self):
        """One pass of the main loop. Returns True while a heat is running."""
        case = self.api.case()
        status = case.get('status')
        tick = int(case.get('tick') or 0)
        ticks_per_period = int(case.get('ticks_per_period') or 300)
        if status != 'ACTIVE':
            if self.in_heat and status == 'STOPPED':
                self._end_heat()
            if self.dash:
                self.dash.update_status(case)
            return False
        if not self.in_heat or tick < self.last_tick or case.get('period') != self.period:
            if self.in_heat:
                self._end_heat()
            self._start_heat(case)
        self.last_tick = tick

        securities = self.api.securities()
        orders = [] if self.dry else self.api.open_orders()     # read after the positions, never before
        self._sync_orders(orders)
        markets = {}
        for tk in S.TICKERS:
            if tk in securities:
                markets[tk] = self._market(tk, securities[tk])
                self.vol.update(tk, tick, markets[tk].mid)
        self._check_safety(tick)

        blocked, changed = self._handle_tenders(self.api.tenders(), markets, tick, ticks_per_period)
        if not self.dry:
            if changed:                                  # a tender was accepted: act on fresh data now
                orders = self.api.open_orders()
                self._sync_orders(orders)
                for tk in changed:
                    markets[tk] = self._market(tk, self.api.security(tk))
            for tk, m in markets.items():
                self._unwind(m, [o for o in orders if o.get('ticker') == tk], blocked, tick, ticks_per_period)
        self._close_tender_books(markets)
        self._track_paths(tick, markets)
        self._feed_dashboard(case, tick, ticks_per_period, markets)
        self._status(tick, markets)
        return True

    def _market(self, tk, sec):
        """Market snapshot. m.position is the part of the position that is safe to trade (see below);
        m.api_position is what RIT reports."""
        m = Market(tk, sec, self.api.book(tk), self.own_id)
        if not self.dry:
            m.position = self._safe_position(tk, m.api_position)
        return m

    # -- our own count of the position ------------------------------------------------
    # RIT's reported position runs up to about a second behind: a tender is booked a tick after it
    # is accepted, and a fill shows on the order before it shows in the position. Sizing orders from
    # the reported number alone made the bot sell shares it no longer had (and RIT fines that). So
    # the bot keeps its own count (start position + accepted tenders + fills of its own orders) and
    # only ever trades the part that both counts agree it holds.

    def _sync_orders(self, open_orders):
        """Bring our record of each order's fills up to date. An order that is not in the open list is
        only treated as finished once RIT itself says TRANSACTED or CANCELLED; until then its stock is
        'unconfirmed' and nothing is traded in it. (Assuming such an order was gone let two resting
        orders for the same shares be live at once.)"""
        open_ids = set()
        self.unconfirmed = set()
        for o in open_orders:
            oid = o.get('order_id')
            if oid is None:
                continue
            open_ids.add(oid)
            rec = self.own.setdefault(oid, {'tk': o.get('ticker'), 'side': o.get('action'), 'filled': 0,
                                            'qty': int(float(o.get('quantity') or 0)), 'final': False, 'misses': 0})
            rec['filled'] = int(float(o.get('quantity_filled') or 0))
        for oid, rec in self.own.items():
            if rec['final'] or oid in open_ids:
                continue                                 # finished earlier, or visibly resting
            try:
                o = self.api.get(f'/orders/{oid}')
                status = o.get('status')
            except Exception:
                o, status = None, None
            if status == 'TRANSACTED':                   # completely filled
                rec['filled'] = max(int(float(o.get('quantity_filled') or 0)), rec['qty'])
                rec['final'] = True
            elif status == 'CANCELLED':
                rec['filled'] = int(float(o.get('quantity_filled') or 0))
                rec['final'] = True
            else:                                        # RIT still calls it open, or didn't answer
                if status == 'OPEN':
                    rec['filled'] = max(rec['filled'], int(float(o.get('quantity_filled') or 0)))
                rec['misses'] += 1
                if status is None and rec['misses'] >= 25:   # RIT never answers for it: assume the worst
                    rec['filled'], rec['final'] = rec['qty'], True
                else:
                    self.unconfirmed.add(rec['tk'])

    def _own_fills(self, tk):
        return sum(r['filled'] if r['side'] == 'BUY' else -r['filled'] for r in self.own.values() if r['tk'] == tk)

    def _counted_position(self, tk):
        return self.base.get(tk, 0) + self.tender_delta[tk] + self._own_fills(tk)

    def _safe_position(self, tk, api_pos):
        """The position that both RIT and our own count agree we hold right now."""
        if tk not in self.base:                          # first look this heat: start from what RIT says
            self.base[tk] = api_pos - self.tender_delta[tk] - self._own_fills(tk)
        if tk in self.unconfirmed or time.time() < self.freeze.get(tk, 0):
            return 0                                     # an order of ours is in limbo: trade nothing yet
        counted = self._counted_position(tk)
        if counted == api_pos:
            self.mismatch.pop(tk, None)
            return api_pos
        seen = self.mismatch.get(tk)
        if seen is None or seen[:2] != (counted, api_pos):
            self.mismatch[tk] = (counted, api_pos, time.time())     # still changing: normal lag
        elif time.time() - seen[2] > S.SETTLE_SECONDS:
            # both numbers have stood still and still disagree (a manual trade, a lost reply): take RIT's
            self.log.warn(f'{tk}: RIT reports {api_pos:+,} but the bot counted {counted:+,}; going with RIT')
            self.base[tk] += api_pos - counted
            self.mismatch.pop(tk, None)
            return api_pos
        if counted == 0 or api_pos == 0 or (counted > 0) != (api_pos > 0):
            return 0
        return min(counted, api_pos, key=abs)

    # -- safety stop ------------------------------------------------------------------

    def _check_safety(self, tick):
        if tick != self._nlv_tick:                       # NLV once per tick
            self._nlv_tick = tick
            try:
                self.nlv = float(self.api.trader().get('nlv') or 0)
            except Exception:
                pass
        if (S.MAX_HEAT_LOSS and self.nlv is not None and self.nlv < -S.MAX_HEAT_LOSS
                and not self.auto_paused and not self.dry):
            self.pause(f'this heat is down {-self.nlv:,.0f} (limit MAX_HEAT_LOSS {S.MAX_HEAT_LOSS:,})')

    def pause(self, reason):
        """Stop taking new tenders (open positions are still unwound) until the next heat."""
        self.auto_paused = reason
        self.log.warn(f'SAFETY STOP: {reason}. No new tenders until the next heat; still unwinding.')
        if self.dash:
            self.dash.paused = True

    # -- P&L per tender -----------------------------------------------------------------

    def _close_tender_books(self, markets, force=False):
        """When a stock is flat again, the realized P&L since its tender(s) were accepted (commissions
        included) is what they made. Overlapping tenders in one stock share it by size."""
        for tk, book in list(self.tender_books.items()):
            m = markets.get(tk)
            if m is None:
                continue
            if m.api_position != 0:
                book['seen'] = True
                if not force:
                    continue
            if not force and (not book['seen'] or self._counted_position(tk) != m.api_position):
                continue
            made = m.realized - book['realized0']
            total = sum(self.tender_rows[t]['qty'] for t in book['tids']) or 1
            for tid in book['tids']:
                row = self.tender_rows[tid]
                row['made'] = made * row['qty'] / total
                self.log.info(f'tender #{tid} made {row["made"]:+,.0f} (expected {row.get("expected", 0):+,.0f})')
                if self.dash:
                    self.dash.note_tender(tid, dict(row))
            del self.tender_books[tk]
            self._record_fills()

    def _record_fills(self):
        """Append our finished orders that got fills to logs/fills.csv (once each). With the book
        snapshots this shows what each unwind order actually got, for tuning the execution."""
        if not S.RECORD_BOOKS:
            return
        try:
            rows = self.api.get('/orders', status='TRANSACTED') + self.api.get('/orders', status='CANCELLED')
            rows = [o for o in rows if float(o.get('quantity_filled') or 0) > 0
                    and o.get('order_id') not in self.fills_written]
            if not rows:
                return
            path = os.path.join(LOG_DIR, 'fills.csv')
            new = not os.path.exists(path)
            with open(path, 'a', encoding='utf-8') as f:
                if new:
                    f.write('heat,order_id,tick,ticker,action,type,limit_price,filled,avg_price\n')
                for o in sorted(rows, key=lambda o: o.get('order_id') or 0):
                    f.write(f'{self.heat_id},{o.get("order_id")},{o.get("tick")},{o.get("ticker")},{o.get("action")},'
                            f'{o.get("type")},{o.get("price")},{int(float(o.get("quantity_filled") or 0))},'
                            f'{o.get("vwap")}\n')
                    self.fills_written.add(o.get('order_id'))
        except Exception:
            pass                                         # recording must never get in the way of trading

    def _feed_dashboard(self, case, tick, ticks_per_period, markets):
        if not self.dash:
            return
        if time.time() >= self._next_fills:              # our fills every 2 seconds
            self._next_fills = time.time() + 2
            try:
                rows = self.api.get('/orders', status='TRANSACTED') + self.api.get('/orders', status='CANCELLED')
                self.dash.set_fills([o for o in rows if float(o.get('quantity_filled') or 0) > 0])
            except Exception:
                pass
        self.dash.update(case, tick, ticks_per_period, self.nlv,
                         {tk: {'position': m.api_position, 'bid': m.best_bid, 'ask': m.best_ask, 'vol': self.vol.get(tk)}
                          for tk, m in markets.items()})

    def _start_heat(self, case):
        was_auto_paused = self.auto_paused if hasattr(self, 'auto_paused') else None
        self._new_heat_state()
        self.in_heat = True
        if self.dash:
            self.dash.new_heat()
            if was_auto_paused:                          # the safety stop only lasts one heat
                self.dash.paused = False
                self.log.info('safety stop lifted for the new heat')
        self.period = case.get('period')
        self.own_id = self.api.trader().get('trader_id')
        self.limits = read_limits(self.api)
        for tk in S.TICKERS:
            try:
                bars = sorted(self.api.history(tk), key=lambda b: b.get('tick', 0))
                self.vol.seed(tk, [float(b['close']) for b in bars if b.get('close')])
            except Exception:
                pass
        self.heats_started += 1
        mode = str(S.PATIENT_MODE).strip().lower()
        self.patient_heat = mode == 'on' or (mode == 'alternate' and self.heats_started % 2 == 1)
        self.log.info(f'heat started: {case.get("name")} | trader {self.own_id} | tick {case.get("tick")} | '
                      f'limits net {self.limits[0]:,} / gross {self.limits[1]:,} | '
                      f'unwind {"patient" if self.patient_heat else "fast"}')

    def _end_heat(self):
        self.in_heat = False
        self.heats_done += 1
        self._track_paths(10**9, {}, flush=True)        # write out paths cut short by the heat ending
        self._record_fills()
        try:
            secs = self.api.securities()
            if self.tender_books:                        # settle P&L of tenders still open at the end
                self._close_tender_books({tk: Market(tk, s, {}, None) for tk, s in secs.items()}, force=True)
            positions = ', '.join(f'{tk} {int(secs[tk].get("position") or 0):+,}' for tk in S.TICKERS if tk in secs)
            nlv = float(self.api.trader().get('nlv') or 0)
            self.log.info(f'heat over | final positions: {positions} | NLV {nlv:,.2f}')
            if self.dash:
                self.dash.add_heat(nlv, positions)
        except Exception as e:
            self.log.warn(f'heat over (could not read the final state: {e})')

    # -- tenders ----------------------------------------------------------------------

    def _handle_tenders(self, tenders, markets, tick, ticks_per_period):
        """Decide on pending tenders. Returns (sides we must not trade, tickers whose position changed)."""
        blocked, changed = set(), set()
        if self.auto_paused or (self.dash and self.dash.paused):   # paused: tenders are yours
            return blocked, changed
        positions = {tk: m.position for tk, m in markets.items()}
        for t in tenders:
            tid = t.get('tender_id')
            state = self.tender_state.get(tid)
            if state in ('accepted', 'declined', 'ignored'):
                continue
            first = state is None
            tk = tender_ticker(t)
            m = markets.get(tk)
            if first:
                self.log.tender(f'#{tid} new: {t.get("caption") or t}')
            if m is None or m.mid is None or not t.get('is_fixed_bid', True):
                self.log.warn(f'tender #{tid}: not a fixed-price {"/".join(S.TICKERS)} tender with market data; '
                              f'leaving it to you')
                self.tender_state[tid] = 'ignored'
                continue
            if first:
                self.limits = read_limits(self.api)
            vol = self.vol.get(tk)
            v = evaluate(t, m, positions, self.limits, tick, ticks_per_period, vol,
                         self.vol.move(tk, S.TREND_TICKS), self.vol.fair(tk, S.FAIR_TICKS))
            if first and v.warning:
                self.log.warn(f'tender #{tid}: {v.warning}')
            unwind_side = 'SELL' if v.side == 'BUY' else 'BUY'
            # already unwinding on the side this tender would block? then decide now, don't watch
            busy = (m.position > 0 and unwind_side == 'SELL') or (m.position < 0 and unwind_side == 'BUY')
            expires = int(t.get('expires') or tick + 30)
            summary = (f'#{tid} {v.side} {v.qty:,} {tk} @ {v.price:.2f} | mid {m.mid:.3f} | '
                       f'edge {v.edge:+.3f}/sh vs needed {v.required:.3f} | '
                       f'to work later {v.to_work:,} | vol {vol:.3f}'
                       + (f' | trend {v.trend:+.3f}' if v.trend else ''))
            if first:
                self.paths.append({'id': tid, 'ticker': tk, 'side': v.side, 'qty': v.qty, 'price': v.price,
                                   'tick': tick, 'mid': m.mid, 'edge': v.edge, 'decision': 'expired', 'mids': {},
                                   'snaps': set()})
            info = self.tender_info.setdefault(tid, {'first': tick, 'peak': v.edge})
            info['peak'] = max(info['peak'], v.edge)
            hold = (v.ok and S.ACCEPT_WAIT_TICKS > 0 and not busy
                    and tick - info['first'] < S.ACCEPT_WAIT_TICKS
                    and tick < expires - S.DECIDE_BEFORE_EXPIRY
                    and v.edge > info['peak'] - S.ACCEPT_GIVEBACK)
            if hold:                                     # good already; give it a few ticks to get better
                if state != 'watching':
                    self.log.tender(f'HOLDING up to {S.ACCEPT_WAIT_TICKS} ticks (good, waiting for more) {summary}')
                    self.tender_state[tid] = 'watching'
                blocked.add((tk, unwind_side))
            elif v.ok:
                if self._accept(tid, tk, v, summary):
                    positions[tk] = m.position + (v.qty if v.side == 'BUY' else -v.qty)
                    changed.add(tk)
                    # Our own count gets the block now. RIT books the tender a tick later, and until both
                    # agree nothing is unwound: trading before that is fined 40c/share (CASE_FINE)
                    self.tender_delta[tk] += v.qty if v.side == 'BUY' else -v.qty
                    book = self.tender_books.setdefault(tk, {'tids': [], 'realized0': m.realized,
                                                             'seen': m.api_position != 0})
                    book['tids'].append(tid)
                    self.tender_rows.setdefault(tid, {})['expected'] = v.edge * v.qty
            elif (v.blockers or v.edge < v.required - S.WATCH_BAND or busy
                  or tick >= expires - S.DECIDE_BEFORE_EXPIRY):
                self._decline(tid, (', '.join(v.blockers) or 'edge too thin') + ' | ' + summary)
            else:
                if state != 'watching':
                    self.log.tender(f'WATCHING until tick {expires - S.DECIDE_BEFORE_EXPIRY} (borderline) {summary}')
                    self.tender_state[tid] = 'watching'
                blocked.add((tk, unwind_side))           # no trading that side until we answer
            if self.dash:
                decision = self.tender_state.get(tid, '')
                if decision == 'declined' and v.blockers:
                    decision = 'declined: ' + ', '.join(v.blockers)
            row = self.tender_rows.setdefault(tid, {})
            if self.tender_state.get(tid) != 'accepted' or 'mid' not in row:   # freeze the accept-time numbers
                row.update({'tick': info['first'], 'ticker': tk, 'side': v.side, 'qty': v.qty, 'price': v.price,
                            'mid': m.mid, 'edge': v.edge, 'required': v.required})
            if self.dash:
                row['decision'] = decision
                self.dash.note_tender(tid, dict(row))
        return blocked, changed

    def _accept(self, tid, tk, v, summary):
        if self.dry:
            self.log.tender(f'WOULD ACCEPT {summary}')
            self.tender_state[tid] = 'accepted'
            return False
        if self.api.accept(tid):
            self.log.tender(f'ACCEPTED {summary}')
            self._mark_path(tid, 'accepted')
            self.tender_state[tid] = 'accepted'
            self.entry_price[tk] = v.price
            self.plans.pop(tk, None)                     # start a fresh unwind schedule
            self.holds.pop(tk, None)
            self.dumping.discard(tk)
            self.impatient.discard(tk)
            return True
        self.log.warn(f'could not accept tender #{tid} (expired?)')
        self.tender_state[tid] = 'declined'
        return False

    def _decline(self, tid, why):
        if self.dry:
            self.log.tender(f'WOULD DECLINE {why}')
        else:
            self.api.decline(tid)
            self.log.tender(f'DECLINED {why}')
        self.tender_state[tid] = 'declined'
        self._mark_path(tid, 'declined')

    # -- price paths after tenders (for deciding how fast to unwind) --------------------

    def _mark_path(self, tid, decision):
        for p in self.paths:
            if p['id'] == tid:
                p['decision'] = decision

    def _track_paths(self, tick, markets, flush=False):
        """Record each tender's ticker mid for PATH_TICKS ticks after it arrived, then append a row
        to logs/tender_paths.csv. Moves are signed in our unwind direction: + = helped us."""
        keep = []
        for p in self.paths:
            m = markets.get(p['ticker'])
            offset = tick - p['tick']
            if m is not None and m.mid is not None and 0 < offset <= PATH_TICKS:
                p['mids'].setdefault(offset, m.mid)
            if S.RECORD_BOOKS and m is not None and 0 <= offset <= PATH_TICKS and offset not in p['snaps']:
                p['snaps'].add(offset)
                self._write_snapshot(p, m, tick, offset)
            if offset >= PATH_TICKS or flush:
                self._write_path(p)
            else:
                keep.append(p)
        self.paths = keep

    def _write_snapshot(self, p, m, tick, offset):
        """One line per tick in logs/book_snapshots.jsonl: the tender's stock's order book (without
        our own orders) and our position, from the tick the tender arrived to PATH_TICKS later."""
        row = {'time': f'{dt.datetime.now():%Y-%m-%d %H:%M:%S.%f}'[:-3], 'heat': self.heat_id, 'tender': p['id'],
               'ticker': p['ticker'], 'side': p['side'], 'qty': p['qty'], 'price': p['price'],
               'decision': p['decision'], 'tick': tick, 'offset': offset, 'position': m.api_position,
               'bids': m.bids[:BOOK_LEVELS], 'asks': m.asks[:BOOK_LEVELS]}
        try:
            with open(os.path.join(LOG_DIR, 'book_snapshots.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps(row) + '\n')
        except OSError:
            pass                                         # recording must never get in the way of trading

    def _write_path(self, p):
        sign = 1 if p['side'] == 'BUY' else -1           # BUY tender -> we sell -> rising price helps
        moves = [f'{sign * (p["mids"][k] - p["mid"]):.3f}' if k in p['mids'] else '' for k in range(1, PATH_TICKS + 1)]
        path = os.path.join(LOG_DIR, 'tender_paths.csv')
        new = not os.path.exists(path)
        with open(path, 'a', encoding='utf-8') as f:
            if new:
                f.write('time,tender,ticker,side,qty,price,tick,mid,edge,decision,'
                        + ','.join(f'move_{k}' for k in range(1, PATH_TICKS + 1)) + '\n')
            f.write(f'{dt.datetime.now():%Y-%m-%d %H:%M:%S},{p["id"]},{p["ticker"]},{p["side"]},{p["qty"]},'
                    f'{p["price"]:.2f},{p["tick"]},{p["mid"]:.3f},{p["edge"]:.3f},{p["decision"]},'
                    + ','.join(moves) + '\n')

    # -- unwinding --------------------------------------------------------------------

    def _unwind(self, m, my_orders, blocked, tick, ticks_per_period):
        tk, pos = m.ticker, m.position
        if pos == 0:
            if m.api_position == 0 and self._counted_position(tk) == 0:    # really flat, not just waiting
                self.plans.pop(tk, None)
                self.holds.pop(tk, None)
                self.dumping.discard(tk)
                self.impatient.discard(tk)
            if my_orders:
                self._cancel(m, my_orders)
            return
        selling = pos > 0
        side = 'SELL' if selling else 'BUY'
        if (tk, side) in blocked:                        # a pending tender needs this side: hands off
            if my_orders:
                self._cancel(m, my_orders)
            return
        if tick >= ticks_per_period - S.FORCE_MARKET_TICKS:
            self._replace(m, my_orders, side, abs(pos), None, 'end of heat')
            return
        if m.mid is None:
            return
        sign = 1 if selling else -1                      # long: a rising price helps us
        # A sudden one-tick jump (someone swept the book). Recorded books, 10 Nov: jumps of 10c+ mostly
        # come back, and the spread is 12c+ wide right after. So:
        #   in our favour -> no new trend hold; rest our orders a few cents inside the spread to be first
        #   against us    -> don't cross the spread for SPIKE_TICKS; let the price come back to our orders
        spike_size = per_stock(S.SPIKE_MOVE, tk, 0.0)
        if spike_size > 0:                               # with real traders 10c is an ordinary tick
            spike_size = max(spike_size, S.SPIKE_K * self.vol.get(tk))
        spike = self.vol.spike(tk, spike_size, S.SPIKE_TICKS) * sign
        if spike and self.spike_logged.get(tk) != (tick, spike > 0):
            self.spike_logged[tk] = (tick, spike > 0)
            self.log.info(f'{tk}: SPIKE {spike:+.3f} in one tick ' + ('our way; selling into it' if spike > 0 and selling
                          else 'our way; buying into it' if spike > 0 else 'against us; not chasing it'))
        # Trend hold is for steady moves; a crazy one-tick jump is sold into instead. So a jump our way
        # ends a running hold (take the profit now), and no hold starts while a jump is inside the
        # TREND_TICKS window that the trend is measured over.
        hold = self.holds.get(tk)
        if spike > 0 and hold and hold['active']:
            hold['active'] = False
            self.plans[tk] = (tick, abs(pos), m.mid)
            self.log.info(f'{tk}: hold over (price jumped {spike:+.3f} our way in one tick); taking profit on {pos:+,}')
        jumped = self.vol.spike(tk, spike_size, max(S.SPIKE_TICKS, S.TREND_TICKS)) * sign > 0
        if (hold or not jumped) and self._holding(m, my_orders, selling, tick, ticks_per_period):
            return

        plan = self.plans.get(tk)
        if plan is None or abs(pos) > plan[1]:
            plan = self.plans[tk] = (tick, abs(pos), m.mid)
            flat_by = min(tick + S.UNWIND_TICKS, ticks_per_period - S.END_FLAT_TICKS)
            self.log.info(f'{tk}: unwinding {pos:+,} shares, aiming to be flat by tick {flat_by}')
            # Was the price already running against us before we sold anything? Then don't work the
            # block: get out now. (Only checked here, before our own orders start moving the price.)
            dump_k = per_stock(S.DUMP_K, tk)
            move = self.vol.move(tk, S.TREND_TICKS)
            threshold = dump_k * self.vol.get(tk) * math.sqrt(S.TREND_TICKS)
            if dump_k > 0 and move is not None and move * sign < -threshold:
                self.dumping.add(tk)
                self.log.info(f'{tk}: DUMP - price moved {move * sign:+.3f} against us in {S.TREND_TICKS} ticks '
                              f'(limit {threshold:.3f}); unwinding at once')
        start, size, start_mid = plan
        patient = self.patient_heat and tk not in self.dumping and tk not in self.impatient
        stop = per_stock(S.PATIENT_STOP, tk, 5.0)
        if patient and (start_mid - m.mid) * sign > stop:
            patient = False                              # back to the normal fast unwind, never a dump
            self.impatient.add(tk)
            self.log.info(f'{tk}: patient unwind stopped - price is {(start_mid - m.mid) * sign:.3f} worse than when '
                          f'it began (limit {stop:.2f}); switching to the fast unwind')
        dumping = tk in self.dumping
        end_tick = ticks_per_period - S.END_FLAT_TICKS
        deadline = tick if dumping else min(start + S.UNWIND_TICKS, end_tick)
        # In a patient heat, PATIENT_SHARE of the block rests in limit orders until PATIENT_TICKS after
        # the unwind began; the rest follows the normal schedule.
        # After PATIENT_TICKS the resting share is released a little each tick over PATIENT_RELEASE_TICKS.
        # (Releasing it all at once made the bot cross the spread again and again in a second or two,
        # pushing the price a long way against itself.)
        share = S.PATIENT_SHARE if patient else 0.0
        rest_until = min(start + S.PATIENT_TICKS, end_tick)
        release_end = min(rest_until + S.PATIENT_RELEASE_TICKS, end_tick)
        if tick < rest_until:
            reserve = size * share
        elif tick < release_end:
            reserve = size * share * (release_end - tick) / (release_end - rest_until)
        else:
            reserve = 0.0
        core = size * (1 - share)
        target = reserve + (0.0 if tick >= deadline else core * (deadline - tick) / (deadline - start))
        behind = abs(pos) - target
        levels, touch = (m.bids, m.best_bid) if selling else (m.asks, m.best_ask)

        # 1) liquidity near the mid that already beats breakeven: take it now
        cost = m.cost or self.entry_price.get(tk) or m.mid
        window = S.PATIENT_TAKE_WINDOW if patient else S.TAKE_WINDOW
        # Fair price (see evaluate): `ref` is where we expect the mid over the next few ticks. When the
        # market is better than that, hit everything down to it (it won't last); when it is worse, hit
        # less and let the resting orders wait for the price to come back.
        fair = self.vol.fair(tk, S.FAIR_TICKS)
        ref = m.mid + per_stock(S.FAIR_WEIGHT, tk) * (fair - m.mid) if fair is not None else m.mid
        # (Hitting everything down to the fair price is only for a crazy jump our way; a steady move is
        # left to the trend hold and the normal window.)
        if selling:
            reach = ref - window if ref >= m.mid else (min(m.mid - window, ref) if spike > 0 else m.mid - window)
            take_px = max(cost + m.fee + S.TAKE_EDGE, reach)
        else:
            reach = ref + window if ref <= m.mid else (max(m.mid + window, ref) if spike > 0 else m.mid + window)
            take_px = min(cost - m.fee - S.TAKE_EDGE, reach)
        take_qty, _ = sweep(levels, max(0, abs(pos) - int(reserve)), take_px, selling)
        # 2) behind schedule: cross the spread, but no further than MAX_TAKE_SLIPPAGE (DUMP_SLIPPAGE in a dump)
        # Only once per tick: each crossing eats the best prices, and doing it again straight away
        # means paying 5c worse every time (the bot bought TAME up from 25.68 to 26.06 in two seconds).
        crossing = False
        chasing = spike < 0 and not dumping and tick < (release_end if patient else deadline)
        if behind > 0 and touch is not None and self.crossed_tick.get(tk) != tick and not chasing:
            if dumping:
                # One sweep, and never further than DUMP_SLIPPAGE from where the price was when the unwind
                # began. (Chasing the best price down round after round sold a block a dollar through the
                # book and lost $22k on one tender.) After this the normal fast unwind takes over.
                slip_px = start_mid - S.DUMP_SLIPPAGE if selling else start_mid + S.DUMP_SLIPPAGE
                self.dumping.discard(tk)
                self.impatient.add(tk)
            else:
                slip_px = touch - S.MAX_TAKE_SLIPPAGE if selling else touch + S.MAX_TAKE_SLIPPAGE
            due_qty, _ = sweep(levels, math.ceil(behind), slip_px, selling)
            if due_qty > take_qty:
                take_qty, take_px, crossing = due_qty, slip_px, True
        if take_qty > 0:
            if crossing and not my_orders:               # (with resting orders, this call only cancels them)
                self.crossed_tick[tk] = tick
            self._replace(m, my_orders, side, take_qty, round(take_px, 2), 'take')
            return

        # 3) otherwise keep resting limit orders at the top of the book
        px = passive_price(m, selling, S.SPIKE_UNDERCUT if spike > 0 else 0.01)
        n_orders = S.PATIENT_PASSIVE_ORDERS if patient else S.PASSIVE_ORDERS
        want = min(abs(pos), m.max_size * n_orders)
        if px is None:
            return
        # Ladder: one order at the top of the book as before, the others no worse than the fair price
        # plus LADDER_STEPS x volatility. When the market is at or above those levels they all sit at
        # the top; when it has dropped away only the first one follows it down, and the rest wait for
        # the bounce (live, 10 Nov: the book came back through arrival mid + 10c in 8 of 10 unwinds).
        ladder = [px]
        if tick < (release_end if patient else deadline):
            step = self.vol.get(tk)
            for k in list(S.LADDER_STEPS)[:max(0, n_orders - 1)]:
                level = round(ref + sign * k * step, 2)
                ladder.append(max(px, level) if selling else min(px, level))
        if orders_match(my_orders, side, ladder, want, abs(pos)):
            return
        if time.time() - self.last_requote[tk] < S.REQUOTE_MIN_INTERVAL:
            return
        self.last_requote[tk] = time.time()
        self._replace(m, my_orders, side, want, ladder if len(ladder) > 1 else px, 'passive')

    def _holding(self, m, my_orders, selling, tick, ticks_per_period):
        """Trend hold: while the price is clearly moving our way, don't unwind; once it falls back
        from its best level by the trailing stop, end the hold and unwind on a fresh schedule.
        One hold per unwind, never past MAX_HOLD_TICKS or into the end-of-heat flattening."""
        tk = m.ticker
        if not S.TREND_HOLD:
            return False
        h = self.holds.get(tk)
        sign = 1 if selling else -1                      # long: a rising price helps us
        vol = self.vol.get(tk)
        last_call = ticks_per_period - S.END_FLAT_TICKS - S.UNWIND_TICKS // 2
        if h is None:
            move = self.vol.move(tk, S.TREND_TICKS)
            threshold = per_stock(S.TREND_K, tk, 1.5) * vol * math.sqrt(S.TREND_TICKS)
            if move is None or move * sign < threshold or tick >= last_call:
                return False
            h = self.holds[tk] = {'active': True, 'start': tick, 'peak': m.mid}
            self.log.info(f'{tk}: TREND HOLD - price moved {move * sign:+.3f} our way in {S.TREND_TICKS} ticks '
                          f'(needed {threshold:.3f}); holding {m.position:+,} with a trailing stop')
        if not h['active']:
            return False
        h['peak'] = max(h['peak'], m.mid) if selling else min(h['peak'], m.mid)
        stop = max(S.TRAIL_MIN, S.TRAIL_K * vol)
        retrace = (h['peak'] - m.mid) * sign
        reason = ('trailing stop hit' if retrace >= stop else
                  'held MAX_HOLD_TICKS' if tick - h['start'] >= S.MAX_HOLD_TICKS else
                  'heat nearly over' if tick >= last_call else None)
        if reason:
            h['active'] = False
            self.plans[tk] = (tick, abs(m.position), m.mid)   # unwind the rest on a fresh schedule from now
            self.log.info(f'{tk}: hold over ({reason}: best {h["peak"]:.3f}, now {m.mid:.3f}, stop {stop:.3f}); '
                          f'unwinding {m.position:+,}')
            return False
        if my_orders:                                    # don't let resting orders sell into the trend
            self._cancel(m, my_orders)
        return True

    def _replace(self, m, my_orders, side, qty, price, why):
        """Cancel our resting orders in this stock, re-read the position (fills may have landed
        meanwhile), then send `qty`, capped at the position so we can never overshoot."""
        tk, pos = m.ticker, m.position
        if my_orders:
            self._cancel(m, my_orders)
            return                                       # send replacements once the cancel has settled
        if pos == 0 or (pos > 0) != (side == 'SELL'):
            return
        qty = min(int(qty), abs(pos))
        if isinstance(price, (list, tuple)):             # a ladder: equal slices, one price each
            per = min(m.max_size, max(1000, math.ceil(qty / len(price) / 100) * 100))
            chunks = [min(per, qty - i) for i in range(0, qty, per)]
            prices = [price[min(i, len(price) - 1)] for i in range(len(chunks))]
        else:
            chunks = [min(m.max_size, qty - i) for i in range(0, qty, m.max_size)]
            prices = [price] * len(chunks)
        def send(chunk, px):
            try:
                return self.api.order(tk, side, chunk, px)
            except Exception as e:                       # no reply: the order may or may not exist
                return e

        if len(chunks) == 1:
            results = [send(chunks[0], prices[0])]
        else:                                            # each order is a ~0.3s round trip: send at once
            results = list(self.pool.map(send, chunks, prices))
        lost = [r for r in results if isinstance(r, Exception)]
        if lost:                                         # don't trade this stock until RIT's numbers settle
            self.freeze[tk] = time.time() + S.SETTLE_SECONDS
            self.log.warn(f'{tk}: no reply to {len(lost)} order(s) ({type(lost[0]).__name__}); pausing this stock '
                          f'for {S.SETTLE_SECONDS:.0f}s')
            results = [None if isinstance(r, Exception) else r for r in results]
        sent = sum(c for c, r in zip(chunks, results) if r is not None)
        ids = []
        for c, r in zip(chunks, results):                # count these orders' fills from now on
            if r and r.get('order_id') is not None:
                ids.append(r['order_id'])
                self.own[r['order_id']] = {'tk': tk, 'side': side, 'qty': c, 'final': False, 'misses': 0,
                                           'filled': int(float(r.get('quantity_filled') or 0))}
        if sent and (why != 'passive' or S.LOG_PASSIVE_ORDERS):
            at = ('market' if price is None else f'{price:.2f}' if not isinstance(price, (list, tuple))
                  else f'{prices[0]:.2f}' if prices[0] == prices[-1] else f'{prices[0]:.2f}..{prices[-1]:.2f}')
            self.log.trade(f'{tk} {side} {sent:,} @ {at} ({why}) | position before {pos:+,} | '
                           f'order{"s" if len(ids) > 1 else ""} {", ".join(f"#{i}" for i in ids)}')

    def _cancel(self, m, my_orders):
        """Cancel our resting orders in this stock. Their final fills are picked up by _sync_orders, and
        nothing new is sent while any of them still shows as open."""
        self.api.cancel_ticker(m.ticker)

    def _status(self, tick, markets):
        if time.time() < self.next_status:
            return
        self.next_status = time.time() + S.STATUS_EVERY
        try:
            nlv = f'{float(self.api.trader().get("nlv") or 0):,.0f}'
        except Exception:
            nlv = '?'
        books = ' | '.join(f'{tk} {m.api_position:+,} ({_fmt(m.best_bid)} x {_fmt(m.best_ask)})'
                           for tk, m in markets.items())
        self.log.info(f'tick {tick:>3} | {books} | NLV {nlv}')


# ----------------------------------------------------------------------------- entry points

def check(api):
    """Print what the bot sees so you can confirm the API key and data look right."""
    problems = []
    case = api.case()
    print(f'case        : {case.get("name")} | status {case.get("status")} | period {case.get("period")} | '
          f'tick {case.get("tick")}/{case.get("ticks_per_period")}')
    trader = api.trader()
    print(f'trader      : {trader.get("trader_id")} | NLV {trader.get("nlv")}')
    print(f'limits      : {api.limits()}')
    secs = api.securities()
    for tk, s in secs.items():
        print(f'security    : {tk:<5} position {s.get("position")} | bid {s.get("bid")} ask {s.get("ask")} | '
              f'fee {s.get("trading_fee")} | max order {s.get("max_trade_size")} | '
              f'API orders/s {s.get("api_orders_per_second")}')
    for tk in S.TICKERS:
        if tk not in secs:
            problems.append(f'{tk} is not in /securities - is the Liability Trading case loaded?')
            continue
        for key in ('position', 'vwap', 'trading_fee', 'max_trade_size'):
            if key not in secs[tk]:
                problems.append(f'/securities has no "{key}" field for {tk}')
        book = api.book(tk)
        if 'bids' not in book or 'asks' not in book:
            problems.append(f'the {tk} order book has keys {list(book)} (expected bids/asks)')
        print(f'book {tk:<6} : bids {book_levels(book.get("bids"), None, True)[:5]}')
        print(f'              asks {book_levels(book.get("asks"), None, False)[:5]}')
    tenders = api.tenders()
    print(f'tenders     : {tenders if tenders else "none pending right now"}')
    for t in tenders:
        side, warning = tender_side(t)
        print(f'              bot reads #{t.get("tender_id")} as: we {side} {t.get("quantity")} '
              f'{tender_ticker(t)} @ {t.get("price")}')
        if warning:
            problems.append(warning)
    print(f'open orders : {api.open_orders()}')
    print()
    if problems:
        print('PROBLEMS:')
        for p in problems:
            print('  - ' + p)
        return 1
    print('OK: the API key works and the bot can read everything it needs.')
    return 0


def shutdown(api, log, dry_run):
    if not dry_run:
        try:
            api.cancel_all()
            log.info('cancelled all open orders')
            secs = api.securities()
            left = {tk: int(secs[tk].get('position') or 0) for tk in S.TICKERS if tk in secs}
            if any(left.values()):
                log.warn(f'positions still open: {left} - close them by hand before the heat ends')
        except Exception:
            pass
    log.info(f'bot stopped; log saved to {log.path}')


SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.py')


KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'api_key.txt')   # ignored by git


def saved_api_key():
    """The key from api_key.txt, or failing that from settings.py."""
    try:
        with open(KEY_PATH, encoding='utf-8') as f:
            key = f.read().strip()
    except OSError:
        key = ''
    if not key:
        key = S.API_KEY or ''
    return '' if key.startswith('PASTE') else key


def save_api_key(key):
    """Remember the key in api_key.txt. That file is in .gitignore, so the key never reaches the repo."""
    with open(KEY_PATH, 'w', encoding='utf-8') as f:
        f.write(key + '\n')
    S.API_KEY = key
    return True


def test_api_key(url, key):
    """'ok', 'rejected', 'not logged in' or 'offline'."""
    try:
        http = requests.Session()
        http.trust_env = False
        resp = http.get(url.rstrip('/') + '/case', headers={'X-API-Key': key}, timeout=S.HTTP_TIMEOUT)
    except requests.RequestException:
        return 'offline'
    if resp.status_code == 200:
        return 'ok'
    if 'logged in' in resp.text.lower():
        return 'not logged in'
    return 'rejected' if resp.status_code in (401, 403) else 'offline'


def ask_api_key(url):
    """Ask for the API key at start-up. Enter keeps the saved one; a new one is checked and saved."""
    saved = saved_api_key()
    print('API key: in the RIT Client, click "API" in the status bar (bottom right) and copy the API Key.')
    while True:
        keep = f' (or press Enter to keep the saved key ending in "{saved[-3:]}")' if saved else ''
        try:
            typed = input(f'Paste the API key{keep}: ').strip().strip('\'"')
        except EOFError:
            return saved
        key = typed or saved
        if not key:
            print('  A key is needed to connect.')
            continue
        if not re.fullmatch(r'[A-Za-z0-9_\-]+', key):
            print('  That doesn\'t look like an API key (letters and numbers only). Try again.')
            continue
        result = test_api_key(url, key)
        if result == 'rejected':
            print('  The RIT Client rejected that key. Copy it again from the API window and retry.')
            continue
        if key != saved:
            print('  Saved to api_key.txt (not tracked by git).' if save_api_key(key) else '  (Could not save it; using it for this run.)')
        print({'ok': '  Key works.',
               'not logged in': '  The RIT Client isn\'t logged in yet; the bot will connect once you log in.',
               'offline': '  The RIT Client isn\'t answering yet; the bot will keep trying.'}[result])
        return key


# Settings used instead of those in settings.py when the bot is started with --practice (practice.bat).
# The practice server has no crowd: prices trend there instead of bouncing back (10 Nov), so the fair
# price is off and the trend rules are on. settings.py itself is not changed. Not for competition day.
PRACTICE = {
    'FAIR_WEIGHT': {'CRZY': 0.0, 'TAME': 0.0},
    'TREND_HOLD': True,
    'TREND_ACCEPT_BETA': {'CRZY': 0.0, 'TAME': 1.0},
}


def main():
    ap = argparse.ArgumentParser(description='LT3 tender bot for RITCxDublin 2026')
    ap.add_argument('--practice', action='store_true', help='use the practice-server settings (see PRACTICE)')
    ap.add_argument('--no-prompt', action='store_true', help="don't ask for the API key; use the saved one")
    ap.add_argument('--dry-run', action='store_true', help='print tender decisions only; never accept, decline or trade')
    ap.add_argument('--check', action='store_true', help='show what the RIT API returns, then exit')
    ap.add_argument('--heats', type=int, default=0, help='exit after this many heats (default: keep running)')
    ap.add_argument('--no-dashboard', action='store_true', help="don't start the web dashboard")
    args = ap.parse_args()

    url = S.API_URL
    if not args.no_prompt and sys.stdin is not None and sys.stdin.isatty():
        key = ask_api_key(url)
    else:
        key = saved_api_key()
    if not key:
        sys.exit('No API key yet. Start the bot from run_bot.bat and paste the key when it asks '
                 '(RIT Client status bar -> API).')
    log = Log(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs'))
    api = RIT(url, key, log)
    if args.practice:
        for name, value in PRACTICE.items():
            setattr(S, name, value)
        log.warn('PRACTICE-SERVER SETTINGS in use: ' + ', '.join(f'{k} = {v}' for k, v in PRACTICE.items())
                 + '. Start with run_bot.bat for a live room.')

    if args.check:
        try:
            sys.exit(check(api))
        except ApiKeyError as e:
            sys.exit(f'ERROR: {e}')
        except requests.ConnectionError:
            sys.exit(f'ERROR: nothing is answering at {url}. Open the RIT Client and log in first.')

    signal.signal(signal.SIGINT, _request_stop)
    dash = None
    if not args.no_dashboard:
        import dashboard
        try:
            dash = dashboard.Dashboard(S.DASHBOARD_PORT, 'DRY RUN' if args.dry_run else 'LIVE')
            dash.start()
            log.listeners.append(dash.add_line)
            log.info(f'dashboard: http://localhost:{S.DASHBOARD_PORT}')
        except OSError as e:
            log.warn(f'dashboard not started (port {S.DASHBOARD_PORT} busy?): {e}')
            dash = None
    bot = Bot(api, log, dry_run=args.dry_run, dash=dash)
    log.info(f'LT3 bot started in {"DRY-RUN" if args.dry_run else "LIVE"} mode against {url}. Ctrl+C stops it.')
    last_error, last_wait_msg, errors_in_row = None, 0.0, 0
    while not stop_requested:
        try:
            running = bot.step()
            errors_in_row = 0
            if args.heats and bot.heats_done >= args.heats:
                break
            time.sleep(S.LOOP_SLEEP if running else 0.25)
        except ApiKeyError as e:                         # also happens briefly while the client logs in
            if time.time() - last_wait_msg > 10:
                log.warn(f'{e}. Retrying...')
                last_wait_msg = time.time()
            time.sleep(2)
        except (requests.ConnectionError, requests.Timeout):
            if time.time() - last_wait_msg > 10:
                log.warn(f'cannot reach {url}: is the RIT Client open and logged in? Retrying...')
                last_wait_msg = time.time()
            time.sleep(1)
        except Exception as e:
            msg = f'{type(e).__name__}: {e}'
            if msg != last_error:
                log.warn('unexpected error, carrying on:\n' + traceback.format_exc())
                last_error = msg
            errors_in_row += 1
            if S.MAX_ERRORS_IN_ROW and errors_in_row == S.MAX_ERRORS_IN_ROW and not bot.auto_paused:
                bot.pause(f'{errors_in_row} errors in a row ({msg})')
            time.sleep(0.2)
    shutdown(api, log, args.dry_run)


if __name__ == '__main__':
    main()
