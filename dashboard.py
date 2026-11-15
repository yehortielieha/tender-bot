"""
Local dashboard for lt3_bot.py: live P&L, positions, fills, tender decisions, the log, and every
setting in settings.py (changes apply immediately and are written back to settings.py).

Started by lt3_bot.py; open http://localhost:8050 (port = DASHBOARD_PORT in settings.py).
Only listens on this computer (127.0.0.1).
"""
import collections
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import settings as S

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.py')
HIDDEN = {'API_KEY'}                       # never sent to the page
READ_ONLY = {'API_URL', 'TICKERS', 'DASHBOARD_PORT'}
_write_lock = threading.Lock()


# ----------------------------------------------------------------------------- settings file

def settings_meta():
    """[{group, name, comment}] in file order, comments joined across continuation lines."""
    group, out, last = 'General', [], None
    with open(SETTINGS_PATH, encoding='utf-8') as f:
        for line in f:
            header = re.match(r'#\s*---\s*(.+?)\s*-*\s*$', line)
            if header:
                group, last = header.group(1), None
                continue
            m = re.match(r'([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$', line)
            if m:
                rest = m.group(2)
                last = {'group': group, 'name': m.group(1),
                        'comment': rest.split('#', 1)[1].strip() if '#' in rest else ''}
                out.append(last)
            elif last is not None and re.match(r'\s+#', line):
                last['comment'] = (last['comment'] + ' ' + line.strip().lstrip('#').strip()).strip()
            else:
                last = None
    return out


def _literal(value):
    if isinstance(value, bool):
        return 'True' if value else 'False'
    if isinstance(value, int):
        return f'{value:_}' if abs(value) >= 10_000 else str(value)
    if isinstance(value, float):
        return repr(round(value, 6))
    if isinstance(value, dict):
        return '{' + ', '.join(f'{k!r}: {_literal(v)}' for k, v in value.items()) + '}'
    return repr(value)


def _coerce(current, value):
    """Convert a value from the page to the type the setting already has."""
    if isinstance(current, bool):
        return bool(value) if not isinstance(value, str) else value.lower() in ('1', 'true', 'yes', 'on')
    if isinstance(current, int):
        return int(round(float(value)))
    if isinstance(current, float):
        return float(value)
    if isinstance(current, dict):
        if not isinstance(value, dict):
            raise ValueError('expected one value per ticker')
        return {k: _coerce(v, value.get(k, v)) for k, v in current.items()}
    return str(value)


def save_setting(name, raw):
    """Apply a setting live and write it back to settings.py, keeping the comment beside it."""
    if name in HIDDEN or name in READ_ONLY or not hasattr(S, name) or not name.isupper():
        raise ValueError(f'{name} cannot be changed here')
    value = _coerce(getattr(S, name), raw)
    with _write_lock:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            text = f.read()
        pattern = re.compile(rf'^({name}\s*=\s*)(.*?)(\s+#.*)?$', re.M)
        m = pattern.search(text)
        if not m:
            raise ValueError(f'{name} not found in settings.py')
        old = m.group(2)
        new = _literal(value)
        if m.group(3):
            new = new.ljust(len(old))
        text = text[:m.start(2)] + new + text[m.end(2):]
        with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
            f.write(text)
        setattr(S, name, value)
    return value


# ----------------------------------------------------------------------------- shared state

class Dashboard:
    def __init__(self, port, mode):
        self.port = port
        self.mode = mode
        self.paused = False
        self.lock = threading.Lock()
        self.lines = collections.deque(maxlen=300)
        self.heats = []
        self._reset_heat()
        self.status = {}

    def _reset_heat(self):
        self.nlv_series = []
        self.fills = []
        self.tenders = collections.OrderedDict()
        self.markets = {}

    # called by the bot -----------------------------------------------------------
    def add_line(self, tag, line):
        with self.lock:
            self.lines.append({'tag': tag.strip(), 'text': line})

    def new_heat(self):
        with self.lock:
            self._reset_heat()

    def update(self, case, tick, ticks_per_period, nlv, markets):
        with self.lock:
            self.status = {'name': case.get('name'), 'status': case.get('status'), 'tick': tick,
                           'ticks': ticks_per_period, 'nlv': nlv}
            self.markets = markets
            if nlv is not None and (not self.nlv_series or self.nlv_series[-1][0] != tick):
                self.nlv_series.append((tick, nlv))

    def update_status(self, case):
        with self.lock:
            self.status = dict(self.status, status=case.get('status'), tick=case.get('tick'))

    def set_fills(self, orders):
        rows = sorted(orders, key=lambda o: o.get('order_id') or 0, reverse=True)[:60]
        with self.lock:
            self.fills = [{'id': o.get('order_id'), 'tick': o.get('tick'), 'ticker': o.get('ticker'),
                           'side': o.get('action'), 'type': o.get('type'),
                           'qty': o.get('quantity_filled'), 'price': o.get('vwap') or o.get('price')}
                          for o in rows]

    def note_tender(self, tid, info):
        with self.lock:
            self.tenders[tid] = info
            while len(self.tenders) > 30:
                self.tenders.popitem(last=False)

    def add_heat(self, nlv, positions):
        with self.lock:
            self.heats.append({'n': len(self.heats) + 1, 'time': time.strftime('%H:%M'), 'nlv': nlv,
                               'positions': positions})

    # served to the page ----------------------------------------------------------
    def snapshot(self):
        with self.lock:
            return {'mode': self.mode, 'paused': self.paused, 'status': self.status, 'markets': self.markets,
                    'nlv_series': self.nlv_series, 'fills': self.fills,
                    'tenders': list(reversed(self.tenders.values())), 'heats': self.heats,
                    'lines': list(self.lines)[-120:]}

    def settings(self):
        out = []
        for meta in settings_meta():
            name = meta['name']
            if name in HIDDEN or not hasattr(S, name):
                continue
            out.append(dict(meta, value=getattr(S, name), read_only=name in READ_ONLY))
        return out

    def start(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body, ctype='application/json'):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path in ('/', '/index.html'):
                    self._send(200, PAGE.encode(), 'text/html; charset=utf-8')
                elif self.path == '/api/state':
                    self._send(200, dash.snapshot())
                elif self.path == '/api/settings':
                    self._send(200, dash.settings())
                else:
                    self._send(404, {'error': 'not found'})

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                    if self.path == '/api/settings':
                        value = save_setting(body['name'], body['value'])
                        dash.add_line('', f'{time.strftime("%H:%M:%S")}        setting changed: '
                                          f'{body["name"]} = {_literal(value)}')
                        self._send(200, {'ok': True, 'value': value})
                    elif self.path == '/api/pause':
                        dash.paused = bool(body.get('paused'))
                        dash.add_line('WARN', f'{time.strftime("%H:%M:%S")} WARN   new tenders '
                                              f'{"PAUSED (still unwinding open positions)" if dash.paused else "resumed"}')
                        self._send(200, {'ok': True, 'paused': dash.paused})
                    else:
                        self._send(404, {'error': 'not found'})
                except Exception as e:
                    self._send(400, {'ok': False, 'error': str(e)})

        server = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()


# ----------------------------------------------------------------------------- the page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LT3 Bot</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;
--pos:#15803d;--neg:#b91c1c;--warn:#b45309;--chip:#eef2ff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e6e8ee;--muted:#9aa3b2;
--line:#272c36;--accent:#60a5fa;--pos:#4ade80;--neg:#f87171;--warn:#fbbf24;--chip:#1e2533}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center;padding:14px 20px;
border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
h1{font-size:16px;margin:0}h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;
color:var(--muted);margin:0 0 10px}
.big{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}.pos{color:var(--pos)}.neg{color:var(--neg)}
.chip{display:inline-block;padding:2px 8px;border-radius:999px;background:var(--chip);font-size:12px}
.chip.live{background:var(--pos);color:#fff}.chip.paused{background:var(--warn);color:#fff}
.bar{width:160px;height:6px;border-radius:3px;background:var(--line);overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent)}
button{font:inherit;padding:6px 12px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--ink);cursor:pointer}button:hover{border-color:var(--accent)}
main{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:16px;padding:16px 20px}
@media (max-width:980px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.stack{display:grid;gap:16px;align-content:start;min-width:0}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-weight:550;color:var(--muted);font-size:12px}td.r,th.r{text-align:right}
.scroll{max-height:320px;overflow:auto}
#log{font:12px/1.5 ui-monospace,Consolas,monospace;max-height:300px;overflow:auto;white-space:pre-wrap;
word-break:break-word}
#log .TENDER{color:var(--accent)}#log .TRADE{color:var(--pos)}#log .WARN{color:var(--warn)}
svg{width:100%;height:170px;display:block}
.set-group{margin-bottom:14px}.set{display:grid;grid-template-columns:1fr 130px;gap:4px 10px;
padding:7px 0;border-bottom:1px solid var(--line);align-items:center}
.set label{font:12px ui-monospace,Consolas,monospace}.set small{grid-column:1/-1;color:var(--muted);font-size:12px}
.set input{width:100%;font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--ink);text-align:right}.set input:disabled{opacity:.6}
.set .dict{display:grid;gap:4px}.set .dict span{font-size:11px;color:var(--muted)}
.flash{outline:2px solid var(--pos)}.bad{outline:2px solid var(--neg)}
</style></head><body>
<header>
  <h1>LT3 Bot</h1>
  <span id="mode" class="chip">-</span>
  <span id="case" class="muted">connecting...</span>
  <div><div class="muted" style="font-size:12px">tick <span id="tick">-</span></div><div class="bar"><i id="prog" style="width:0"></i></div></div>
  <div><div class="muted" style="font-size:12px">P&amp;L this heat (NLV)</div><div id="nlv" class="big">-</div></div>
  <div style="margin-left:auto"><button id="pause">Pause new tenders</button></div>
</header>
<main>
  <div class="stack">
    <div class="row" id="markets"></div>
    <div class="card"><h2>P&amp;L this heat</h2><svg id="chart" viewBox="0 0 600 170" preserveAspectRatio="none"></svg></div>
    <div class="card"><h2>Tenders</h2><div class="scroll"><table id="tenders"></table></div></div>
    <div class="card"><h2>Recent fills</h2><div class="scroll"><table id="fills"></table></div></div>
    <div class="card"><h2>Log</h2><div id="log"></div></div>
  </div>
  <div class="stack">
    <div class="card"><h2>Heats</h2><table id="heats"></table></div>
    <div class="card"><h2>Settings</h2><p class="muted" style="margin:0 0 10px;font-size:12px">
      Changes apply immediately and are saved to settings.py.</p><div id="settings"></div></div>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
const money = v => v == null ? '-' : (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString(undefined, {maximumFractionDigits: 0});
const num = v => v == null ? '-' : Number(v).toLocaleString();
const px = v => v == null ? '-' : Number(v).toFixed(2);
const cls = v => v > 0 ? 'pos' : v < 0 ? 'neg' : '';
const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
let paused = false, logPinned = true;

$('log').addEventListener('scroll', e => { const el = e.target; logPinned = el.scrollTop + el.clientHeight >= el.scrollHeight - 8; });
$('pause').onclick = async () => {
  const r = await fetch('/api/pause', {method: 'POST', body: JSON.stringify({paused: !paused})});
  paused = (await r.json()).paused; render(last);
};

function chart(series) {
  const svg = $('chart');
  if (series.length < 2) { svg.innerHTML = '<text x="300" y="90" text-anchor="middle" fill="currentColor" opacity=".5" font-size="13">waiting for data</text>'; return; }
  const xs = series.map(p => p[0]), ys = series.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs, x0 + 1);
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys), span = (hi - lo) || 1;
  const X = x => (x - x0) / (x1 - x0) * 596 + 2, Y = y => 160 - (y - lo) / span * 150;
  const pts = series.map(p => X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)).join(' ');
  const end = ys[ys.length - 1];
  svg.innerHTML = `<line x1="0" x2="600" y1="${Y(0)}" y2="${Y(0)}" stroke="currentColor" opacity=".2"/>
    <polyline points="${pts}" fill="none" stroke="var(--${end >= 0 ? 'pos' : 'neg'})" stroke-width="2" vector-effect="non-scaling-stroke"/>
    <text x="4" y="14" font-size="12" fill="currentColor" opacity=".6">${money(hi)}</text>
    <text x="4" y="166" font-size="12" fill="currentColor" opacity=".6">${money(lo)}</text>`;
}

let last = null;
function render(s) {
  if (!s) return; last = s;
  const st = s.status || {};
  $('mode').textContent = s.paused ? 'PAUSED' : s.mode; $('mode').className = 'chip ' + (s.paused ? 'paused' : s.mode === 'LIVE' ? 'live' : '');
  $('pause').textContent = s.paused ? 'Resume tenders' : 'Pause new tenders'; paused = s.paused;
  $('case').textContent = (st.name || '') + (st.status ? ' | ' + st.status : '');
  $('tick').textContent = (st.tick ?? '-') + ' / ' + (st.ticks ?? 300);
  $('prog').style.width = ((st.tick || 0) / (st.ticks || 300) * 100) + '%';
  $('nlv').textContent = money(st.nlv); $('nlv').className = 'big ' + cls(st.nlv);
  $('markets').innerHTML = Object.entries(s.markets || {}).map(([tk, m]) => `<div class="card">
    <h2>${tk}</h2><div class="big ${cls(m.position)}">${m.position > 0 ? '+' : ''}${num(m.position)}</div>
    <div class="muted">bid ${px(m.bid)} &middot; ask ${px(m.ask)} &middot; vol/tick ${m.vol ? m.vol.toFixed(3) : '-'}</div></div>`).join('');
  chart(s.nlv_series || []);
  $('tenders').innerHTML = '<tr><th>tick</th><th>tender</th><th class="r">price</th><th class="r">mid</th><th class="r">edge</th><th class="r">needed</th><th>decision</th><th class="r">expected</th><th class="r">made</th></tr>' +
    (s.tenders || []).map(t => `<tr><td>${t.tick}</td><td>${t.side} ${num(t.qty)} ${t.ticker}</td><td class="r">${px(t.price)}</td>
    <td class="r">${t.mid.toFixed(3)}</td><td class="r ${cls(t.edge - t.required)}">${t.edge.toFixed(3)}</td><td class="r">${t.required.toFixed(3)}</td>
    <td>${esc(t.decision)}</td><td class="r">${t.expected == null ? '' : money(t.expected)}</td>
    <td class="r ${cls(t.made)}">${t.made == null ? (t.expected == null ? '' : '<span class="muted">unwinding</span>') : money(t.made)}</td></tr>`).join('');
  const done = (s.tenders || []).filter(t => t.made != null);
  if (done.length) {
    const made = done.reduce((a, t) => a + t.made, 0), exp = done.reduce((a, t) => a + t.expected, 0);
    $('tenders').innerHTML += `<tr><td colspan="7" class="muted">closed tenders this heat: kept ${exp ? Math.round(made / exp * 100) : 0}% of expected</td>
      <td class="r">${money(exp)}</td><td class="r ${cls(made)}">${money(made)}</td></tr>`;
  }
  $('fills').innerHTML = '<tr><th>tick</th><th>stock</th><th>side</th><th class="r">filled</th><th class="r">avg price</th><th>type</th></tr>' +
    (s.fills || []).map(f => `<tr><td>${f.tick}</td><td>${f.ticker}</td><td class="${f.side === 'BUY' ? 'pos' : 'neg'}">${f.side}</td>
    <td class="r">${num(f.qty)}</td><td class="r">${px(f.price)}</td><td class="muted">${f.type}</td></tr>`).join('');
  $('heats').innerHTML = '<tr><th>#</th><th>time</th><th class="r">final NLV</th><th>end positions</th></tr>' +
    (s.heats || []).slice().reverse().map(h => `<tr><td>${h.n}</td><td>${h.time}</td><td class="r ${cls(h.nlv)}">${money(h.nlv)}</td>
    <td class="muted">${esc(h.positions)}</td></tr>`).join('') + ((s.heats || []).length ? '' : '<tr><td colspan="4" class="muted">none finished yet</td></tr>');
  const log = $('log');
  log.innerHTML = (s.lines || []).map(l => `<div class="${l.tag}">${esc(l.text)}</div>`).join('');
  if (logPinned) log.scrollTop = log.scrollHeight;
}

async function poll() {
  try { render(await (await fetch('/api/state')).json()); }
  catch (e) { $('case').textContent = 'bot not running (start run_bot.bat)'; }
}

async function save(name, value, el) {
  const r = await fetch('/api/settings', {method: 'POST', body: JSON.stringify({name, value})});
  const res = await r.json();
  el.classList.remove('flash', 'bad'); void el.offsetWidth;
  el.classList.add(res.ok ? 'flash' : 'bad'); if (!res.ok) alert(res.error);
}

async function loadSettings() {
  const rows = await (await fetch('/api/settings')).json();
  const groups = {};
  rows.forEach(r => (groups[r.group] = groups[r.group] || []).push(r));
  $('settings').innerHTML = Object.entries(groups).map(([g, list]) => `<div class="set-group"><h2>${esc(g)}</h2>` +
    list.map(r => {
      let input;
      if (typeof r.value === 'boolean') input = `<input type="checkbox" data-name="${r.name}" ${r.value ? 'checked' : ''} ${r.read_only ? 'disabled' : ''}>`;
      else if (r.value && typeof r.value === 'object' && !Array.isArray(r.value))
        input = '<div class="dict">' + Object.entries(r.value).map(([k, v]) =>
          `<span>${esc(k)}</span><input data-name="${r.name}" data-key="${esc(k)}" value="${v}" inputmode="decimal">`).join('') + '</div>';
      else input = `<input data-name="${r.name}" value="${esc(Array.isArray(r.value) ? r.value.join(', ') : r.value)}" ${r.read_only ? 'disabled' : ''} ${typeof r.value === 'number' ? 'inputmode="decimal"' : ''}>`;
      return `<div class="set"><label>${r.name}</label>${input}${r.comment ? `<small>${esc(r.comment)}</small>` : ''}</div>`;
    }).join('') + '</div>').join('');
  document.querySelectorAll('#settings input').forEach(el => el.addEventListener('change', () => {
    const name = el.dataset.name;
    if (el.dataset.key !== undefined) {
      const value = {};
      document.querySelectorAll(`#settings input[data-name="${name}"]`).forEach(i => value[i.dataset.key] = Number(i.value));
      save(name, value, el);
    } else save(name, el.type === 'checkbox' ? el.checked : (el.inputMode === 'decimal' ? Number(el.value) : el.value), el);
  }));
}

loadSettings(); poll(); setInterval(poll, 1000);
</script></body></html>
"""
