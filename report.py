"""
Results report for lt3_bot.py: reads logs\\lt3_*.log and prints how each heat and each tender did.
It only reads the logs, so it's safe to run while the bot is trading.

  python report.py             the most recent day that has logs
  python report.py 20261110    one day (YYYYMMDD)
  python report.py all         every log
"""
import glob
import os
import re
import statistics
import sys

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')

TENDER = re.compile(r'(\d\d:\d\d:\d\d)\.\d+ TENDER (ACCEPTED|DECLINED)(?: [^#]*)?#(\d+) (BUY|SELL) ([\d,]+) (\w+) @ ([\d.]+) '
                    r'\| mid ([\d.]+) \| edge ([+-][\d.]+)/sh vs needed ([\d.]+) \| to work later ([\d,]+)')
MADE = re.compile(r'tender #(\d+) made ([+-][\d,]+) \(expected ([+-][\d,]+)\)')
HEAT_START = re.compile(r'(\d\d:\d\d:\d\d)\.\d+\s+heat started: .*\| tick (\d+) \|')
STYLE = re.compile(r'\| unwind (\w+)')
HEAT_OVER = re.compile(r'(\d\d:\d\d:\d\d)\.\d+\s+heat over \| final positions: (.*) \| NLV ([-\d,.]+)')
HOLD = re.compile(r'(\w+): TREND HOLD')
TRADE = re.compile(r'(\d\d:\d\d:\d\d\.\d+) TRADE  (\w+) ')


def seconds(clock):
    h, m, s = clock.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse(paths):
    """Returns (heats, tenders). A heat is {'day', 'start', 'from_tick', 'nlv', 'positions', 'tenders'}."""
    heats, tenders = [], []
    for path in paths:
        day = os.path.basename(path)[4:12]
        heat, unwinding = None, {}
        for line in open(path, encoding='utf-8', errors='replace'):
            m = HEAT_START.search(line)
            if m:
                style = STYLE.search(line)
                heat = {'day': day, 'start': m.group(1), 'from_tick': int(m.group(2)), 'nlv': None,
                        'positions': '', 'tenders': [], 'style': style.group(1) if style else 'fast'}
                heats.append(heat)
                unwinding = {}
                continue
            m = TENDER.search(line)
            if m:
                t = {'day': day, 'time': m.group(1), 'decision': m.group(2), 'id': m.group(3), 'side': m.group(4),
                     'qty': int(m.group(5).replace(',', '')), 'ticker': m.group(6), 'price': float(m.group(7)),
                     'mid': float(m.group(8)), 'edge': float(m.group(9)), 'needed': float(m.group(10)),
                     'work': int(m.group(11).replace(',', '')), 'made': None, 'expected': None, 'held': False,
                     'last_trade': None, 'style': heat['style'] if heat else 'fast'}
                t['gap'] = abs(t['mid'] - t['price'])
                tenders.append(t)
                if heat is not None:
                    heat['tenders'].append(t)
                if t['decision'] == 'ACCEPTED':
                    unwinding[t['ticker']] = t
                continue
            m = HOLD.search(line)
            if m and m.group(1) in unwinding:
                unwinding[m.group(1)]['held'] = True
            m = TRADE.search(line)
            if m and m.group(2) in unwinding:
                unwinding[m.group(2)]['last_trade'] = m.group(1)
            m = MADE.search(line)
            if m:
                for t in reversed(heat['tenders'] if heat else []):
                    if t['id'] == m.group(1) and t['decision'] == 'ACCEPTED' and t['made'] is None:
                        t['made'] = float(m.group(2).replace(',', ''))
                        t['expected'] = float(m.group(3).replace(',', ''))
                        unwinding.pop(t['ticker'], None)
                        break
            m = HEAT_OVER.search(line)
            if m and heat is not None:
                heat['nlv'] = float(m.group(3).replace(',', ''))
                heat['positions'] = m.group(2)
                heat = None
    return heats, tenders


def money(v):
    return f'{v:+,.0f}'


def bucket_table(title, rows, key):
    groups = {}
    for t in rows:
        groups.setdefault(key(t), []).append(t)
    print(f'\n{title}')
    print(f'  {"":<22}{"tenders":>8}{"expected":>11}{"made":>11}{"kept":>7}{"avg made":>10}{"losers":>8}{"worst":>10}')
    for name in sorted(groups):
        g = groups[name]
        exp, made = sum(t['expected'] for t in g), sum(t['made'] for t in g)
        kept = f'{made / exp * 100:.0f}%' if exp > 0 else '-'
        print(f'  {name:<22}{len(g):>8}{exp:>11,.0f}{made:>11,.0f}{kept:>7}{made / len(g):>10,.0f}'
              f'{sum(t["made"] < 0 for t in g):>8}{min(t["made"] for t in g):>10,.0f}')


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ''
    paths = sorted(glob.glob(os.path.join(LOG_DIR, 'lt3_*.log')))
    if not paths:
        sys.exit(f'No logs yet in {LOG_DIR}. Run the bot first.')
    if arg.lower() != 'all':
        day = arg if re.fullmatch(r'\d{8}', arg) else os.path.basename(paths[-1])[4:12]
        paths = [p for p in paths if os.path.basename(p)[4:12] == day]
        if not paths:
            sys.exit(f'No logs for {day}.')
        print(f'LT3 bot report for {day[:4]}-{day[4:6]}-{day[6:]} ({len(paths)} log files)')
    else:
        print(f'LT3 bot report for all {len(paths)} log files')
    heats, tenders = parse(paths)

    done_heats = [h for h in heats if h['nlv'] is not None]
    print(f'\nHEATS ({len(done_heats)} finished)')
    print(f'  {"day":<10}{"started":>9}{"from tick":>10}{"unwind":>9}{"accepted":>9}{"declined":>9}{"final NLV":>11}  end positions')
    for h in done_heats:
        acc = sum(t['decision'] == 'ACCEPTED' for t in h['tenders'])
        dec = sum(t['decision'] == 'DECLINED' for t in h['tenders'])
        late = '' if h['from_tick'] <= 5 else '  (bot joined late)'
        print(f'  {h["day"]:<10}{h["start"]:>9}{h["from_tick"]:>10}{h["style"]:>9}{acc:>9}{dec:>9}{h["nlv"]:>11,.0f}  '
              f'{h["positions"]}{late}')
    for style in sorted({h['style'] for h in done_heats}):
        nlvs = [h['nlv'] for h in done_heats if h['style'] == style and h['from_tick'] <= 5 and h['tenders']]
        if nlvs and len({h['style'] for h in done_heats}) > 1:
            print(f'  {style} heats (full): {len(nlvs)} | average {statistics.mean(nlvs):,.0f} | lowest {min(nlvs):,.0f} | '
                  f'highest {max(nlvs):,.0f}')
    # a heat with no tenders at all was cut short by the server, so it doesn't count as a full heat
    full = [h['nlv'] for h in done_heats if h['from_tick'] <= 5 and h['tenders']]
    if full:
        spread = f', steadiness (st. dev.) {statistics.pstdev(full):,.0f}' if len(full) > 1 else ''
        print(f'  full heats: {len(full)} | average {statistics.mean(full):,.0f} | lowest {min(full):,.0f} | '
              f'highest {max(full):,.0f}{spread}')
    left_open = [h for h in done_heats if re.search(r'[+-][1-9]', h['positions'])]
    if left_open:
        print(f'  WARNING: {len(left_open)} heat(s) ended with a position still open (can be penalized).')

    done = [t for t in tenders if t['made'] is not None]
    declined = [t for t in tenders if t['decision'] == 'DECLINED']
    print(f'\nTENDERS: {sum(t["decision"] == "ACCEPTED" for t in tenders)} accepted, {len(declined)} declined, '
          f'{len(done)} with a recorded result')
    if declined:
        print(f'  declined tenders were on average {statistics.mean(t["gap"] for t in declined) * 100:.1f}c from the mid '
              f'(widest {max(t["gap"] for t in declined) * 100:.1f}c)')
    if not done:
        print('  No per-tender results in these logs yet.')
        return
    made = [t['made'] for t in done]
    print(f'  made per tender: average {statistics.mean(made):,.0f}, median {statistics.median(made):,.0f}, '
          f'{sum(m > 0 for m in made)} of {len(made)} profitable')
    print(f'  made per share: {statistics.mean(t["made"] / t["qty"] for t in done) * 100:.1f}c, against an average '
          f'gap to the mid of {statistics.mean(t["gap"] for t in done) * 100:.1f}c')
    times = [seconds(t['last_trade']) - seconds(t['time']) for t in done if t['last_trade']]
    if times:
        print(f'  unwind time (accept to last order): median {statistics.median(times):.1f}s, longest {max(times):.1f}s')

    bucket_table('ALL TENDERS WITH A RESULT', done, lambda t: 'all')
    bucket_table('BY STOCK', done, lambda t: t['ticker'])
    bucket_table('BY GAP BETWEEN TENDER PRICE AND MID', done,
                 lambda t: '1) under 12c' if t['gap'] < 0.12 else '2) 12-18c' if t['gap'] < 0.18
                 else '3) 18-25c' if t['gap'] < 0.25 else '4) 25c or more')
    bucket_table('BY SHARE OF THE BLOCK THE BOOK COULD NOT COVER', done,
                 lambda t: '1) under 40%' if t['work'] / t['qty'] < 0.4 else '2) 40-70%' if t['work'] / t['qty'] < 0.7
                 else '3) 70% or more')
    bucket_table('TREND HOLD', done, lambda t: 'held' if t['held'] else 'not held')
    bucket_table('BY UNWIND STYLE OF THE HEAT (PATIENT_MODE)', done, lambda t: t['style'])

    worst = sorted(done, key=lambda t: t['made'])[:3]
    print('\nWORST THREE TENDERS')
    for t in worst:
        print(f'  {t["day"]} {t["time"]}  {t["side"]:<4} {t["qty"]:>6,} {t["ticker"]} @ {t["price"]:.2f} (mid {t["mid"]:.3f})  '
              f'made {money(t["made"])} vs expected {money(t["expected"])}')


if __name__ == '__main__':
    main()
