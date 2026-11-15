"""Checks for the fair price, the ladder and the wider watch band. Usage: four_test.py <bot dir>"""
import sys
import types

sys.path.insert(0, sys.argv[1])
import lt3_bot as B
import settings as S

ok = total = 0
def check(name, cond, detail=''):
    global ok, total
    total += 1
    ok += bool(cond)
    print(('PASS  ' if cond else 'FAIL  ') + name + (f'   [{detail}]' if detail and not cond else ''))

# --- fair price
v = B.VolTracker()
for t, mid in enumerate([10.00] * 9 + [10.40]):
    v.update('CRZY', t, mid)
check('fair = average of the last 10 mids', abs(v.fair('CRZY', 10) - 10.04) < 1e-9, v.fair('CRZY', 10))
check('fair needs three ticks of history', B.VolTracker().fair('CRZY', 10) is None)

# --- orders_match with a ladder
def o(px, qty, filled=0, side='SELL'):
    return {'action': side, 'type': 'LIMIT', 'price': px, 'quantity': qty, 'quantity_filled': filled}
lad = [10.00, 10.05, 10.10, 10.20]
book = [o(10.00, 10000), o(10.05, 10000), o(10.10, 10000), o(10.20, 10000)]
check('ladder in place: no re-send', B.orders_match(book, 'SELL', lad, 40000, 70000))
check('levels shifted by 2c: still no re-send', B.orders_match([o(10.00, 10000), o(10.07, 10000), o(10.12, 10000), o(10.18, 10000)], 'SELL', lad, 40000, 70000))
check('top-of-book order gone: re-send', not B.orders_match(book[1:], 'SELL', lad, 40000, 70000))
check('top moved a cent: re-send', not B.orders_match(book, 'SELL', [10.01] + lad[1:], 40000, 70000))
check('an order far from every level: re-send', not B.orders_match(book[:3] + [o(10.50, 10000)], 'SELL', lad, 40000, 70000))
check('more resting than we hold: re-send', not B.orders_match(book, 'SELL', lad, 40000, 30000))
check('single price still works', B.orders_match([o(10.00, 20000)], 'SELL', 10.00, 20000, 20000))
check('single price, wrong price: re-send', not B.orders_match([o(10.02, 20000)], 'SELL', 10.00, 20000, 20000))

# --- _replace with a ladder never sends more than the position, one price per slice
sent = []
class Api:
    def order(self, tk, side, qty, px):
        sent.append((qty, px))
        return {'order_id': len(sent), 'quantity_filled': 0}
class Pool:
    def map(self, fn, *cols):
        return [fn(*a) for a in zip(*cols)]
bot = types.SimpleNamespace(api=Api(), pool=Pool(), own={}, freeze={}, log=types.SimpleNamespace(trade=lambda *a: None, warn=lambda *a: None))
m = types.SimpleNamespace(ticker='TAME', position=33000, max_size=10000)
B.Bot._replace(bot, m, [], 'SELL', 40000, [25.00, 25.03, 25.06, 25.12], 'passive')
check('ladder of 4 on 33,000 shares: sends exactly 33,000', sum(q for q, _ in sent) == 33000, sent)
check('...as four slices at the four prices', [p for _, p in sent] == [25.00, 25.03, 25.06, 25.12] and max(q for q, _ in sent) <= 10000, sent)
check('...and counts every order in its own ledger', len(bot.own) == 4 and sum(x['qty'] for x in bot.own.values()) == 33000)
sent.clear()
m = types.SimpleNamespace(ticker='CRZY', position=-2500, max_size=25000)
B.Bot._replace(bot, m, [], 'BUY', 2500, [9.50, 9.47, 9.44, 9.40], 'passive')
check('small remainder: slices of at least 1,000, total 2,500', sum(q for q, _ in sent) == 2500 and len(sent) == 3, sent)
sent.clear()
B.Bot._replace(bot, m, [], 'SELL', 2500, [9.50, 9.47], 'passive')
check('wrong side for the position: sends nothing', not sent, sent)

print(f'{ok}/{total} passed')
