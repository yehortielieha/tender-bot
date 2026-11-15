"""Reproduce the overselling fault: RIT shows a fill on the ORDER at once but in the POSITION about a second
later, and a tender's position a tick after the accept. A resting order that fills completely used to
be followed by a second order for the same shares.

  python tests/oversell_test.py <folder containing lt3_bot.py> [off|on]   (on = patient unwind)
"""
import os
import sys
import threading
import time

BOT_DIR = sys.argv[1]
sys.path.insert(0, BOT_DIR)
import settings as S  # noqa: E402
import lt3_bot as B   # noqa: E402

B.LOG_DIR = os.path.dirname(os.path.abspath(__file__))
S.TREND_HOLD = False
if len(sys.argv) > 2:
    S.PATIENT_MODE = sys.argv[2]
print(f'PATIENT_MODE = {getattr(S, "PATIENT_MODE", "n/a")}')
S.RECORD_BOOKS = False
S.MAX_HEAT_LOSS = 0
POS_LAG, TENDER_LAG, TICK = 1.0, 1.0, 0.5
results = []


def check(name, ok, detail=''):
    results.append(bool(ok))
    print(f'{"PASS" if ok else "FAIL"}  {name}  {detail}')


class Log:
    def __init__(self):
        self.lines = []

    def _w(self, tag, m):
        self.lines.append((tag, m))

    def info(self, m): self._w('', m)
    def warn(self, m): self._w('WARN', m)
    def tender(self, m): self._w('TENDER', m)
    def trade(self, m): self._w('TRADE', m)


class LagAPI:
    """Thin book near the mid, so most of the block has to rest as limit orders that buyers then lift."""
    def __init__(self, tenders, lift_every=3, get_lag=0.0, list_lag=0.0):
        self.get_lag, self.list_lag = get_lag, list_lag   # per-order GET is this stale; new orders reach the open list this late
        self.hist = {}
        self.lock = threading.RLock()
        self.t0 = time.time()
        self.true_pos = {'CRZY': 0, 'TAME': 0}
        self.events = []                      # (time the position shows it, ticker, change)
        self.orders, self.next_id = {}, 1
        self.pending, self.answers = tenders, {}
        self.crossed, self.early = [], []
        self.accepted_at = {}
        self.lift_every = lift_every
        self.books = {'CRZY': {'bids': [(9.98, 3000), (9.97, 4000)], 'asks': [(10.00, 5000)]},
                      'TAME': {'bids': [(24.98, 3000)], 'asks': [(25.01, 3000)]}}

    def tick(self): return int((time.time() - self.t0) / TICK) + 50

    def reported(self, tk):
        now = time.time()
        return sum(d for t, k, d in self.events if k == tk and t <= now)

    def case(self): return {'name': 'overshoot test', 'period': 1, 'tick': self.tick(), 'ticks_per_period': 300, 'status': 'ACTIVE'}
    def trader(self): return {'trader_id': 'ME', 'nlv': 0}
    def limits(self): return [{'net_limit': 100_000, 'gross_limit': 250_000}]
    def history(self, t): return []

    def security(self, t):
        with self.lock:
            return {'ticker': t, 'position': self.reported(t), 'vwap': 9.80, 'trading_fee': 0.02,
                    'max_trade_size': 25_000 if t == 'CRZY' else 10_000, 'last': 9.99, 'realized': 0.0}

    def securities(self): return {t: self.security(t) for t in self.true_pos}

    def book(self, t):
        with self.lock:
            mk = lambda lv: [{'price': p, 'quantity': q, 'quantity_filled': 0, 'trader_id': 'X'} for p, q in lv]
            mine = lambda side: [dict(price=o['price'], quantity=o['quantity'], quantity_filled=o['quantity_filled'],
                                      trader_id='ME') for o in self.orders.values()
                                 if o['ticker'] == t and o['action'] == side and o['status'] == 'OPEN']
            return {'bids': mk(self.books[t]['bids']) + mine('BUY'), 'asks': mk(self.books[t]['asks']) + mine('SELL')}

    def tenders(self): return [x for x in self.pending if x['tender_id'] not in self.answers]

    def _snap(self, o):
        self.hist.setdefault(o['order_id'], []).append((time.time(), dict(o)))

    def open_orders(self):
        with self.lock:
            now = time.time()
            return [dict(o) for o in self.orders.values()
                    if o['status'] == 'OPEN' and self.hist[o['order_id']][0][0] <= now - self.list_lag]

    def get(self, path, **kw):
        if path.startswith('/orders/'):
            with self.lock:
                oid = int(path.rsplit('/', 1)[1])
                for o in self.orders.values():           # make sure the latest state is in the history
                    if o['order_id'] == oid and self.hist[oid][-1][1] != o:
                        self._snap(o)
                old = [s for t, s in self.hist[oid] if t <= time.time() - self.get_lag]
                return dict(old[-1] if old else self.hist[oid][0][1])
        if path == '/orders':
            return []
        return {}

    def accept(self, tid):
        td = next(x for x in self.pending if x['tender_id'] == tid)
        with self.lock:
            self.answers[tid] = 'accepted'
            d = td['quantity'] if td['action'] == 'BUY' else -td['quantity']
            self.accepted_at[td['ticker']] = time.time()
            threading.Timer(TENDER_LAG, self._book_tender, (td['ticker'], d)).start()
        return True

    def _book_tender(self, tk, d):
        with self.lock:
            self.true_pos[tk] += d                # the tender is really booked a tick later...
            self.events.append((time.time(), tk, d))   # ...and shows in the position then

    def decline(self, tid):
        self.answers[tid] = 'declined'
        return True

    def _fill(self, t, side, q):
        before = self.true_pos[t]
        d = q if side == 'BUY' else -q
        self.true_pos[t] += d
        after = self.true_pos[t]
        if (before >= 0 and after < 0 and side == 'SELL') or (before <= 0 and after > 0 and side == 'BUY'):
            self.crossed.append((t, before, after))
        self.events.append((time.time() + POS_LAG, t, d))

    def order(self, t, side, qty, price=None):
        time.sleep(0.03)
        with self.lock:
            levels = self.books[t]['bids' if side == 'SELL' else 'asks']
            o = {'order_id': self.next_id, 'ticker': t, 'action': side, 'quantity': qty, 'quantity_filled': 0,
                 'price': price, 'type': 'MARKET' if price is None else 'LIMIT', 'status': 'OPEN'}
            self.next_id += 1
            self.orders[o['order_id']] = o
            self._snap(o)
            while o['quantity'] > o['quantity_filled'] and levels:
                p, avail = levels[0]
                if price is not None and ((side == 'SELL' and p < price - 1e-9) or (side == 'BUY' and p > price + 1e-9)):
                    break
                take = min(o['quantity'] - o['quantity_filled'], avail)
                o['quantity_filled'] += take
                self._fill(t, side, take)
                if take == avail:
                    levels.pop(0)
                else:
                    levels[0] = (p, avail - take)
            if o['quantity_filled'] == o['quantity'] or price is None:
                o['status'] = 'TRANSACTED'
            first = dict(self.hist[o['order_id']][0][1])     # what the POST reply shows: accepted, nothing filled yet
            self._snap(o)
            return first if self.get_lag else dict(o)

    def lift_resting(self, min_age=0.0):
        """A buyer (or seller) takes ALL of our resting orders: they vanish from the open list at once."""
        with self.lock:
            now = time.time()
            for o in self.orders.values():
                if o['status'] == 'OPEN' and self.hist[o['order_id']][0][0] <= now - min_age:
                    q = o['quantity'] - o['quantity_filled']
                    o['quantity_filled'] = o['quantity']
                    o['status'] = 'TRANSACTED'
                    self._fill(o['ticker'], o['action'], q)
                    self._snap(o)

    def cancel_ticker(self, t):
        with self.lock:
            for o in self.orders.values():
                if o['ticker'] == t and o['status'] == 'OPEN':
                    o['status'] = 'CANCELLED'
                    self._snap(o)
        return True

    def cancel_all(self):
        for t in self.true_pos:
            self.cancel_ticker(t)
        return True


def tender(price, qty=50_000, action='BUY', tid=1):
    return {'tender_id': tid, 'period': 1, 'tick': 50, 'expires': 80, 'ticker': 'CRZY', 'quantity': qty,
            'price': price, 'action': action, 'is_fixed_bid': True, 'caption': f'Would you like to {action} the shares?'}


def run(api, bot, seconds, on_tick=None, on_loop=None):
    end, last = time.time() + seconds, None
    while time.time() < end:
        bot.step()
        if on_loop:
            on_loop()
        tk = api.tick()
        if tk != last:
            last = tk
            if on_tick:
                on_tick(tk)
        time.sleep(0.02)


# A: long block, buyers keep lifting everything the bot rests
api, log = LagAPI([tender(9.60)]), Log()
bot = B.Bot(api, log)
run(api, bot, 12, lambda tk: api.lift_resting() if tk % 2 == 0 else None)
sold = sum(o['quantity_filled'] for o in api.orders.values() if o['action'] == 'SELL')
bought = sum(o['quantity_filled'] for o in api.orders.values() if o['action'] == 'BUY')
check('A: accepted the tender', api.answers.get(1) == 'accepted')
first = min((o for o in api.orders.values()), key=lambda o: o['order_id'], default=None)
check('A: never sold more than the block (no overshoot)', not api.crossed and sold <= 50_000,
      f'sold {sold:,} of 50,000, bought back {bought:,}, crossings {api.crossed[:2]}')
check('A: flat at the end', api.true_pos['CRZY'] == 0, f'true position {api.true_pos["CRZY"]:+,}')
check('A: no warnings about disagreeing counts', not any('going with RIT' in m for t, m in log.lines if t == 'WARN'),
      str([m for t, m in log.lines if t == 'WARN'][:1]))

# B: short block (client buys from us), sellers keep hitting our resting bids
api, log = LagAPI([tender(10.40, action='SELL', tid=2)]), Log()
api.books['CRZY'] = {'bids': [(9.98, 3000)], 'asks': [(10.00, 3000), (10.01, 4000)]}
bot = B.Bot(api, log)
run(api, bot, 12, lambda tk: api.lift_resting() if tk % 2 == 0 else None)
bought = sum(o['quantity_filled'] for o in api.orders.values() if o['action'] == 'BUY')
check('B: never bought back more than the short block', not api.crossed and bought <= 50_000,
      f'bought {bought:,} of 50,000, crossings {api.crossed[:2]}')
check('B: flat at the end', api.true_pos['CRZY'] == 0, f'true position {api.true_pos["CRZY"]:+,}')

# C: nothing is sold before the tender is really booked (that is what RIT fined)
api, log = LagAPI([tender(9.60, tid=3)]), Log()
orig_order = api.order
def guarded(t, side, qty, price=None):
    if api.true_pos[t] == 0:
        api.early.append((side, qty))
    return orig_order(t, side, qty, price)
api.order = guarded
bot = B.Bot(api, log)
run(api, bot, 3)
check('C: no order before the tender is booked', not api.early, str(api.early[:2]))
check('C: unwinding started once it was booked', any(o['action'] == 'SELL' for o in api.orders.values()))

# D: a position the bot never counted (a manual trade): it warns, takes RIT's number and unwinds it
api, log = LagAPI([]), Log()
bot = B.Bot(api, log)
run(api, bot, 0.3)
with api.lock:
    api.true_pos['CRZY'] += 9_000
    api.events.append((time.time(), 'CRZY', 9_000))
run(api, bot, S.SETTLE_SECONDS + 4, lambda tk: api.lift_resting())
check('D: warned and re-based on RIT\'s number', any('going with RIT' in m for t, m in log.lines if t == 'WARN'))
check('D: unwound the uncounted position without overshooting', api.true_pos['CRZY'] == 0 and not api.crossed,
      f'true position {api.true_pos["CRZY"]:+,}, crossings {api.crossed[:2]}')

# E: today's 600-share case. RIT's answer about a single order runs behind, and a new order takes a
# moment to show in the open list, so an order can be "not in the list" without being finished.
for label, kw in (('stale per-order answers', dict(get_lag=0.6)), ('new orders slow to list', dict(list_lag=0.4)),
                  ('both', dict(get_lag=0.6, list_lag=0.4))):
    api, log = LagAPI([tender(9.60, tid=9)], **kw), Log()
    bot = B.Bot(api, log)
    run(api, bot, 14, on_loop=lambda: api.lift_resting(min_age=0.15))    # eager buyers: gone 0.15 s after resting
    sold = sum(o['quantity_filled'] for o in api.orders.values() if o['action'] == 'SELL')
    check(f'E: no overshoot with {label} and eager buyers', not api.crossed and sold <= 50_000 and api.true_pos['CRZY'] == 0,
          f'sold {sold:,} of 50,000, final {api.true_pos["CRZY"]:+,}, crossings {api.crossed[:2]}')

print(f'\n{sum(results)}/{len(results)} passed')
os._exit(0 if all(results) else 1)
