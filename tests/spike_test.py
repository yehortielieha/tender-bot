"""Checks for the spike rule's building blocks. Usage: spike_test.py <bot dir>"""
import sys
import types

sys.path.insert(0, sys.argv[1])
import lt3_bot as B

ok = 0
def check(name, cond):
    global ok
    ok += bool(cond)
    print(('PASS  ' if cond else 'FAIL  ') + name)

v = B.VolTracker()
for t, mid in enumerate([10.00, 10.01, 10.02, 10.14, 10.15, 10.16]):
    v.update('TAME', t, mid)
check('up-jump of 12c two ticks ago is seen within 3 ticks', abs(v.spike('TAME', 0.10, 3) - 0.12) < 1e-9)
check('...but not within 2 ticks', v.spike('TAME', 0.10, 2) == 0.0)
check('size 0 switches it off', v.spike('TAME', 0.0, 3) == 0.0)
v.update('TAME', 6, 10.01)
check('the most recent jump wins (a 15c drop)', abs(v.spike('TAME', 0.10, 3) + 0.15) < 1e-9)
check('no history, no spike', B.VolTracker().spike('CRZY', 0.10, 3) == 0.0)

m = types.SimpleNamespace(best_bid=10.00, best_ask=10.20)
check('wide spread: sell rests 3c inside the ask', B.passive_price(m, True, 0.03) == 10.17)
check('wide spread: buy rests 3c inside the bid', B.passive_price(m, False, 0.03) == 10.03)
check('normal: still one cent inside', B.passive_price(m, True) == 10.19)
m = types.SimpleNamespace(best_bid=10.00, best_ask=10.04)
check('4c spread: undercut shrinks to 1c, stays above the middle', B.passive_price(m, True, 0.03) == 10.03)
m = types.SimpleNamespace(best_bid=10.00, best_ask=10.01)
check('1c spread: joins the ask, never crosses', B.passive_price(m, True, 0.03) == 10.01)
print(f'{ok}/10 passed')
