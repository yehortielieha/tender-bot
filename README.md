# LT3 tender bot

A Python bot for the Liability Trading case (LT3) of the Rotman Interactive Trader, written for
RITCxDublin 2026 (Trinity College Dublin, 14 November 2026). It decides which block trades to
accept and then sells or buys the block back in a simulated order book.

On competition day it averaged about **$76,000 profit per heat over 20 heats** (simulated money).

**Built with AI help.** I built this with an AI coding assistant (Claude Code). I ran every
session, decided what to test and what to trade with, and several of the rules started as my own
ideas (the trend hold with a trailing stop, selling into price jumps, watching a tender for its
whole window before answering). The assistant wrote most of the code and did the log analysis
with me. I can explain how each part works and why it is there.

## The case in one minute

- Two stocks, CRZY and TAME, trade in a live order book against other traders and the server's
  own liquidity bots. A heat lasts 300 seconds (one "tick" per second).
- About five times a heat an institution offers a **tender**: buy or sell 65,000 to 75,000 shares
  at a fixed price, usually a little better than the market. You have 30 seconds to answer.
- If you accept, you hold a large position and have to get rid of it in the market. Profit is the
  gap between the tender price and what you manage to trade at, minus 2c a share commission.
- Limits: 100,000 shares net, 250,000 gross, orders of at most 25,000 (CRZY) or 10,000 (TAME).
- Trading that is not unwinding a tender is fined. I measured about 20c a share on the practice
  server, and one early mistake cost $25,400.

## Results

All figures are simulated profit. "Live room" means real traders in the market; the practice
server only has the server's bots.

| Session | Heats | Average per heat | Notes |
|---|---|---|---|
| Live practice room, 10 Nov | 7 | about $43,000 | First time against real traders |
| Competition day, 14 Nov | 20 | $76,018 | Lowest $23,740, highest $144,736, flat at the end of every heat |

What changed between the two sessions was mostly execution:

| | 10 Nov live room | 14 Nov competition |
|---|---|---|
| Average exit price against the mid when the tender arrived (CRZY) | 13 to 18c worse | 5.5c better |
| Share of volume filled by resting limit orders | 45 to 58% | 81 to 91% |
| Profit per accepted tender | $13,146 | $18,650 (morning heats) |
| Losing tenders | 4 of 23 | 0 of 43 (morning heats) |

In the ten morning heats the bot declined 7 tenders. Replaying them on the recorded order book,
all 7 would have lost money.

## How it works

**1. Valuing a tender.** The bot walks the order book to see what the block would really sell
for: the shares the book can absorb near the mid are priced at the book's own prices, and the
rest are priced at a discount to a fair price. It accepts when the expected profit per share
beats a bar that rises with the stock's volatility and with how much of the block the book
cannot absorb.

**2. Watching instead of answering at once.** A tender that is not good enough yet is re-checked
every tick for its whole 30 seconds and accepted the moment it clears the bar.

**3. Fair price.** With real traders, a jump in the price mostly came back within a few ticks.
So the bot values and trades against a 10-tick average blended with the current mid, not the
mid alone. A one-second spike cannot make a tender look better than it is.

**4. Unwinding.**
- Most of the block rests in limit orders: one at the top of the book, the others on a ladder
  above the fair price, spaced by the stock's volatility.
- It only hits the book for prices that beat break-even, and crosses the spread at most once a
  tick when it falls behind schedule, so it does not chase its own price impact.
- A sharp one-tick jump in its favour is sold into at once. A jump against it is not chased.
- If the price drifts steadily in its favour it holds, with a trailing stop.
- A backstop switches to a faster unwind if the price runs 50c against the position.
- Everything is closed with market orders in the last ticks of the heat.

**5. Keeping count of the position.** The exchange reports positions about a second late, and
a tender is booked a tick after it is accepted. The bot keeps its own count (tenders accepted
plus fills of its own orders) and only trades the shares that both counts agree on.

**6. Dashboard.** A small local web page shows trades, profit per tender and every setting, and
settings can be changed while the bot runs.

## What the data showed

The practice server and the live room behaved in opposite ways, and settings tuned on one were
wrong for the other.

| | Practice server | Live room, 10 Nov | Competition, 14 Nov |
|---|---|---|---|
| Spread | 3c | 7c | 5 to 9c |
| Typical price move per tick | 4c | 15c | 21 to 27c |
| Do 5-tick moves continue? (autocorrelation) | slightly, +0.04 to +0.14 | no, about -0.4 to -0.5 | no, about -0.5 |

On the practice server I had the bot ride trends. In the live room big moves reversed, so I
rebuilt the unwind around the fair price and the ladder. That is where most of the improvement
came from.

## What went wrong along the way

These taught me more than the good heats did.

- **Selling before the tender was booked.** The bot started unwinding in the same tick it
  accepted a tender. The exchange had not booked the block yet, treated the sale as a trade of
  its own and fined it $25,400. Now it waits until the exchange shows the position.
- **Overselling.** A resting order filled, the reported position had not caught up, and the bot
  sold the same shares twice. This happened three times (1,800, 12,400 and 600 shares) before
  the position count described above fixed it. `tests/oversell_test.py` reproduces the fault
  with a fake exchange that answers late and loses replies.
- **A "get out now" rule that cost $32,000.** When the price was already falling, the bot was
  meant to sell the whole block at once. On paper it looked good. Live, it pushed the price a
  dollar down through a thin book on two tenders, and the price bounced back seconds later. The
  replay had ignored the bot's own price impact. The rule is off.
- **Thresholds in cents.** A stop tuned on the practice server (12c and 20c) fired on 15 of 23
  tenders in the live room, where 15c is one ordinary tick. Thresholds now scale with
  volatility where it matters.
- **Trading the bounces.** Since prices snapped back, buying dips and selling spikes between
  tenders looked attractive. Tested on the recorded order books using prices you could really
  trade at, it lost 15 to 40c a share after the fine. I did not build it.

## What is in the repository

| File | What it is |
|---|---|
| `lt3_bot.py` | The bot: API wrapper, tender valuation, unwind logic, position count |
| `settings.py` | Every setting, with a comment on what it does and why it has that value |
| `dashboard.py` | The local web dashboard |
| `report.py` | Reads the bot's logs and summarises heats and tenders |
| `tests/` | Checks that run against a fake exchange, no RIT needed |
| `*.bat` | Windows launchers (`setup`, `run_bot`, `practice`, `dry_run`, `check`, `report`) |

## Running it

You need Windows, Python 3.11 or newer, and the RIT Client logged in to a server that runs the
LT3 case. Without RIT only the tests run.

1. Run `setup.bat` once. It creates a virtual environment and installs the one dependency.
2. Open the RIT Client and log in.
3. Run `run_bot.bat` and paste the API key when it asks (RIT Client status bar, "API").
4. Open `http://localhost:8050` for the dashboard.

The API key is saved in `api_key.txt`, which is listed in `.gitignore`. Logs go to `logs/`,
also ignored.

`practice.bat` starts the bot with the practice-server settings (fair price off, trend rules
on). `dry_run.bat` prints its decisions without trading.

## Tests

From the repository folder, with the virtual environment's Python:

```
python tests\ladder_fair_price_test.py .
python tests\spike_test.py .
python tests\oversell_test.py . on
```

The first two take a second. The oversell test takes a few minutes: it drives the real bot
against a fake exchange with late position updates, lost replies and buyers that fill resting
orders within a fraction of a second, and checks that it never sells more than it holds.

## Limits

- It is a simulation. There is no real money, and the other side of the market is students and
  server bots.
- The evidence is thin: one live practice session and one competition day. Heats on identical
  settings ranged from $24,000 to $145,000, mostly depending on which tenders turned up.
- Several rules were switched on together, so I cannot say exactly how much each one is worth.
- The settings fit a market where prices bounce. In a trending market they would need changing.
