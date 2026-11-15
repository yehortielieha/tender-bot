"""
Settings for lt3_bot.py. Nothing here has to be edited to start: the bot asks for the API key when it runs.

Prices are in dollars per share. A "tick" is one second of the 300-second heat.
"""

# --- Connection -----------------------------------------------------------------
# Leave API_KEY empty. The bot asks for the key when it starts (RIT Client status bar -> "API") and
# keeps it in api_key.txt, which git ignores.
# The RIT Client must be open and logged in (practice server: flserver.rotman.utoronto.ca, 16530).
API_KEY = ''
API_URL = 'http://localhost:9999/v1'

# --- Case facts (from the case brief; the bot prefers what the API reports) ------
TICKERS = ['CRZY', 'TAME']
FEE_PER_SHARE = 0.02                    # per share traded in the market
TENDER_FEE_PER_SHARE = 0.0              # set to 0.02 if accepted tenders turn out to be charged too
MAX_ORDER_SIZE = {'CRZY': 25_000, 'TAME': 10_000}
NET_LIMIT = 100_000
GROSS_LIMIT = 250_000

# --- Tender decisions ------------------------------------------------------------
# A tender is accepted when its expected profit per share, after fees, is at least
#     MIN_EDGE + RISK_K x (price risk on the shares the book can't absorb right away)
MIN_EDGE = 0.015            # $/share required even when the book can absorb the whole block
RISK_K = 0.18               # higher = pickier about tenders that need a long unwind. (0.18 with
                            # UNWIND_TICKS 40 asks for the same edge as 0.25 did with UNWIND_TICKS 20)
IMMEDIATE_WINDOW = 0.10     # book levels within this distance of the mid count as "trade now"
DEPTH_SHARE = 0.8           # share of that visible depth you expect to actually get. Lower it
                            # (0.5-0.6) for the live heat, where 100+ traders hit the same book
PATIENT_DISCOUNT = {'CRZY': 0.10, 'TAME': 0.10}   # $ expected to be given up vs the mid on
                                                  # shares that have to be worked over time
                                                  # (calibrated on 19 practice tenders, 5 Nov)
WATCH_BAND = 1.0            # 1.0 = every tender is watched for its whole window (0.15 before 12 Nov:
                            # nothing is gained by declining early). Tenders this close to passing are re-checked every tick inside
                            # their 30-second window instead of being declined straight away.
                            # (0.03 until 10 Nov: live prices swing 15c a tick, and half the
                            # declined tenders cleared the bar a few ticks later)
FAIR_WEIGHT = {'CRZY': 0.75, 'TAME': 0.75}   # fair price: with real traders a jump in the mid mostly
                            # comes back, so the bot expects this share of the gap between the mid and
                            # its FAIR_TICKS average to close. Used to value tenders, to decide what to
                            # hit, and to place the ladder. 0 = off (use 0 on the practice server:
                            # prices trend there and the plain mid predicts better)
FAIR_TICKS = 10             # ticks in that average
FAIR_CAP = 0.30             # the most ($/share) the fair price may add to or take off a tender's edge
DECIDE_BEFORE_EXPIRY = 2    # ticks before expiry when a watched tender gets its final answer
ACCEPT_WAIT_TICKS = 0       # 0 = accept good tenders at once. N = hold a good tender up to N ticks
                            # hoping the edge grows; accepts early if the edge falls ACCEPT_GIVEBACK
ACCEPT_GIVEBACK = 0.02      # below its best level, and declines if it stops being good at all
TREND_ACCEPT_BETA = {'CRZY': 0.0, 'TAME': 0.0}    # OFF: with real traders (10 Nov) prices came back
                                                  # instead of trending. TAME was 1.0 on the practice server.
                                                  # Trend-aware accepting: this share of the last
                                                  # TREND_TICKS price move is expected to continue, and
                                                  # counts for or against a tender. 0 = off for that stock
TREND_ACCEPT_CAP = 0.15     # the most ($/share) the trend may add to or take off a tender's edge
DEFAULT_VOL_PER_TICK = 0.03 # $ price move per tick assumed until the bot has measured it
VOL_WINDOW = 60             # ticks of price history used to measure volatility

# --- Unwinding -------------------------------------------------------------------
UNWIND_TICKS = 40           # aim to be flat this many ticks after accepting (linear schedule). 20 until
                            # 10 Nov: live, the price is no further from where it started after 30 ticks
                            # than after 5, and shares sold by crossing the spread went 19-36c worse
LADDER_STEPS = [0.0, 0.5, 1.0]   # (the setting of the 10 live rounds at $80k on 14 Nov) resting orders: one at the top of the book, the others no worse than
                            # the fair price plus these multiples of the per-tick volatility. [] = off
PATIENT_MODE = 'on'         # patient unwind: 'off' = always fast, 'on' = always patient,
                            # 'alternate' = patient in every other heat, to compare the two (report.bat)
PATIENT_SHARE = 0.6         # in a patient heat, this share of each block rests in limit orders at the
PATIENT_TICKS = 20          # best price for this many ticks, and is then released bit by bit over the
PATIENT_RELEASE_TICKS = 20  # next PATIENT_RELEASE_TICKS (all at once pushed the price against itself)
PATIENT_TAKE_WINDOW = 0.04  # in a patient heat, only hit prices this close to the mid straight away
PATIENT_PASSIVE_ORDERS = 4  # resting limit orders per stock in a patient heat
PATIENT_STOP = {'CRZY': 0.50, 'TAME': 0.50}   # backstop for a price that runs away and doesn't come back
                                              # (5.0 = off). A patient unwind switches to the fast one if the
                                              # price moves this far against you since the unwind began. At
                                              # 0.12 / 0.20 it fired on 15 of 23 live tenders (10 Nov), and the
                                              # fast unwind then sold 19-36c worse than the starting price
DUMP_K = {'CRZY': 0.0, 'TAME': 0.0}   # OFF. If the price was already moving against you by more than
                                      # DUMP_K x volatility x sqrt(TREND_TICKS) when the unwind starts,
                                      # do one deeper sweep. On 10 Nov this lost $32k on two tenders
DUMP_SLIPPAGE = 0.10        # that one sweep goes no further than this from the starting price
SPIKE_MOVE = {'CRZY': 0.10, 'TAME': 0.10}   # a one-tick price jump this big is a "spike" (someone swept
                                            # the book). 0 = off. Spikes mostly come back (10 Nov books)
SPIKE_K = 2.5               # ...and at least SPIKE_K x the per-tick volatility, so that in a jumpy market
                            # (real traders: about 15c a tick) ordinary moves don't count as spikes
SPIKE_TICKS = 3             # for this many ticks after a spike: if it went against you, don't cross the
                            # spread (wait for the bounce); if it went your way, don't start a trend hold
SPIKE_UNDERCUT = 0.03       # ...and rest your orders this far inside the best price, to be filled first
TAKE_EDGE = 0.01           # hit the book at once when a price beats breakeven by this much...
TAKE_WINDOW = 0.08          # ...and is within this distance of the mid
MAX_TAKE_SLIPPAGE = 0.05    # when behind schedule, cross the spread by at most this much
PASSIVE_ORDERS = 2          # resting limit orders per stock (each up to the max order size)
TREND_HOLD = True           # ON for steady moves only (12 Nov): a crazy one-tick jump your way (see
                            # SPIKE_MOVE / SPIKE_K) is sold into at once and ends a running hold, and no
                            # hold starts while such a jump is inside the trend window. True = hold off unwinding while the price is clearly moving your way, then
                            # unwind once it falls back from its best level (trailing stop)
TREND_TICKS = 5             # a "clear trend" = the mid moved your way over this many ticks by more
TREND_K = {'CRZY': 1.0, 'TAME': 0.6}  # than TREND_K x volatility x sqrt(TREND_TICKS). Lower = holds
                                      # more often. TAME trends on the practice server, CRZY barely
TRAIL_K = 1.5               # trailing stop: unwind once the price falls back from its best level by
TRAIL_MIN = 0.05            # max(TRAIL_MIN, TRAIL_K x per-tick volatility)
MAX_HOLD_TICKS = 40         # never hold longer than this
END_FLAT_TICKS = 10         # be flat this many ticks before the heat ends
FORCE_MARKET_TICKS = 3      # in the final ticks, close anything left with market orders

# --- Plumbing --------------------------------------------------------------------
BOOK_DEPTH = 100            # orders per side requested from the order book
LOOP_SLEEP = 0.02           # seconds between loops
REQUOTE_MIN_INTERVAL = 0.25 # seconds between passive order refreshes per stock
SETTLE_SECONDS = 3.0        # the bot only trades shares that both RIT's reported position and its own
                            # count (tenders + its fills) agree on. If the two stand still and disagree
                            # for this long, it takes RIT's number and logs a warning

# --- Safety stop ---------------------------------------------------------------
MAX_HEAT_LOSS = 0           # pause new tenders for the rest of the heat if it's down more than this
                            # (open positions are still unwound). 0 = off
MAX_ERRORS_IN_ROW = 0       # pause new tenders after this many unexpected errors in a row. 0 = off
HTTP_TIMEOUT = 2.0
STATUS_EVERY = 5.0          # seconds between status lines
LOG_PASSIVE_ORDERS = True   # also log every resting (passive) order with its order number. Noisier,
                            # but it is what shows exactly what happened if a position is ever oversold
RECORD_BOOKS = True         # save the order book each tick for 30 ticks after every tender, and
                            # all fills, to logs\book_snapshots.jsonl and logs\fills.csv
DASHBOARD_PORT = 8050       # web dashboard at http://localhost:8050 (restart the bot to change)
