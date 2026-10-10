"""Bench page: every store in one interactive table. Self-contained HTML, same look as reports."""
from __future__ import annotations

import json
from pathlib import Path

from radar.reporting.template import _CSS

_JS = r"""
const D = /*__DATA__*/null;
const $ = s => document.querySelector(s);
const h = (t,a={},...k)=>{const e=document.createElement(t);for(const[x,v]of Object.entries(a)){if(x==='class')e.className=v;
  else if(x.startsWith('on'))e.addEventListener(x.slice(2),v);else if(v!=null)e.setAttribute(x,v)}
  for(const c of k.flat())if(c!=null)e.append(c.nodeType?c:document.createTextNode(String(c)));return e};
const SUITES = ['journey','smoke','catalog','product','cart','search','health'];
const CELL = {pass:['✓','s-pass'], flaky:['~','s-flaky'], confirmed_fail:['✕','s-confirmed_fail'], blocked:['–','s-blocked'], skipped:['–','s-skip']};
const DEV = D.devices || ['desktop'];
const mark = (r, dev, s) => { const sd = (r.suites_by_device||{})[dev]; const v = sd ? sd[s] : undefined; const c = CELL[v] || ['·','meta'];
  return h('span',{class:c[1], title: dev + ': ' + (v||'not run')}, c[0]); };
const dvv = (r, dev) => { const d = (r.devices||{})[dev]; return d ? h('span',{class:'v-'+d.verdict}, d.verdict) : h('span',{class:'meta'}, '–'); };
$('#when').textContent = 'bench ' + D.stamp + ' · ' + D.rows.length + ' stores';
const T = D.totals;
[[T.tested||0,'Tested'],[T.healthy||0,'Healthy'],[T.degraded||0,'Degraded'],[T.down||0,'Down'],
 [(T.unsupported||0)+(T.blocked||0),'Not testable'],[T.unreachable||0,'Wrong / dead URL'],[T.no_network||0,'Radar offline'],[T.error||0,'Radar crashed'],[T.stopped||0,'Stopped (time limit)']].forEach(([n,l]) =>
  $('#tiles').append(h('div',{class:'tile'}, h('div',{class:'n'},n), h('div',{class:'l'},l))));
const tb = $('#rows');
function render(filter){
  tb.innerHTML='';
  D.rows.filter(r => filter==='all' || (filter==='fail' && r.failures.length) || r.verdict===filter).forEach(r => {
    const tr = h('tr',{class:'srow'},
      h('td',{}, h('b',{}, r.site_id), h('div',{class:'meta'}, r.cart ? 'cart flow on' : 'read-only')),
      h('td',{class:'v-'+r.verdict}, r.verdict),
      ...['desktop','mobile'].map(dev => h('td',{}, dvv(r, dev))),
      h('td',{}, r.theme || '–'), h('td',{}, r.checkout || '–'),
      ...SUITES.map(s => h('td',{style:'text-align:center;font-weight:700;white-space:nowrap'},
        DEV.length > 1 ? [mark(r,'desktop',s), ' ', mark(r,'mobile',s)] : mark(r, DEV[0], s))),
      h('td',{}, r.failures.length || ''), h('td',{}, r.warnings || ''),
      h('td',{}, r.report_rel ? h('a',{href:r.report_rel}, 'report') : ''));
    const only = Object.entries(r.device_only||{}).filter(([d,l]) => l.length);
    const det = h('tr',{class:'det',style:'display:none'}, h('td',{colspan: 16},
      only.length ? h('div',{class:'sub',style:'margin:4px 0 8px'}, only.map(([d,l]) => h('div',{}, h('b',{}, 'Fails only on ' + d + ': '), l.join(', ')))) : null,
      Object.entries(r.devices||{}).length ? h('div',{class:'sub',style:'margin:4px 0 8px'}, Object.entries(r.devices).map(([d,v]) => h('div',{},
        h('b',{}, d + ': '), v.verdict, ' · ', v.report_rel ? h('a',{href:v.report_rel}, 'report') : '', v.perf && v.perf.pages ?
        ` · ${v.perf.pages} pages, median load ${v.perf.median_load_secs}s` + (v.perf.slowest ? `, slowest ${v.perf.slowest.url} ${v.perf.slowest.load_secs}s` : '')
        + ` · console errors ${v.perf.console_errors}, failed requests ${v.perf.failed_requests}` : ''))) : null,
      r.notes.length ? h('ul',{class:'notes'}, r.notes.map(n => h('li',{}, n))) : null,
      r.failures.length ? h('table',{}, h('thead',{}, h('tr',{}, h('th',{},'Test case'), h('th',{},'Device'), h('th',{},'Verdict'), h('th',{},'Step'), h('th',{},'Why'), h('th',{},'LLM triage'))),
        h('tbody',{}, r.failures.map(f => h('tr',{}, h('td',{class:'mono'}, f.case), h('td',{}, f.device||''), h('td',{class:'s-'+f.verdict}, f.verdict),
          h('td',{class:'mono'}, f.step||''), h('td',{}, f.error), h('td',{}, f.llm||''))))) : h('div',{class:'empty'}, 'No failures.')));
    tr.addEventListener('click', e => { if (e.target.tagName !== 'A') det.style.display = det.style.display === 'none' ? '' : 'none'; });
    tb.append(tr, det);
  });
}
const F=[['all','All'],['fail','With failures'],['healthy','Healthy'],['degraded','Degraded'],['down','Down'],['blocked','Blocked'],['unsupported','Unsupported'],['unreachable','Wrong URL'],['no_network','Radar offline'],['error','Crashed'],['stopped','Stopped']];
F.forEach(([k,l]) => $('#chips').append(h('button',{class:'chip'+(k==='all'?' on':''), onclick:e=>{
  document.querySelectorAll('#chips .chip').forEach(c=>c.classList.remove('on')); e.target.classList.add('on'); render(k);}}, l)));
render('all');
"""

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radar bench</title><style>""" + _CSS + """ .srow{cursor:pointer}.srow:hover{background:var(--panel2)} .det td{background:var(--panel2)}
</style></head><body><div class="wrap">
<header class="top"><div style="flex:1"><div class="brand">BugRadar · Radar bench</div><h1>Shopify coverage</h1><div class="sub" id="when"></div></div></header>
<div id="tiles" class="tiles"></div>
<div class="toolbar"><div id="chips" style="display:flex;gap:6px;flex-wrap:wrap"></div></div>
<div class="card"><table><thead><tr><th>Store</th><th>Verdict</th><th>Desktop</th><th>Mobile</th><th>Theme</th><th>Checkout</th>
<th>Journey</th><th>Smoke</th><th>Catalog</th><th>Product</th><th>Cart</th><th>Search</th><th>Health</th><th>Fails</th><th>Warns</th><th></th></tr></thead>
<tbody id="rows"></tbody></table><div class="sub" style="margin-top:8px">Click a row for failures and notes. Suite cells show desktop then mobile: ✓ pass · ~ flaky · ✕ confirmed failure · – blocked/skipped · · not run. Verdict = the worse of the two.</div></div>
</div><script>""" + _JS + """</script></body></html>"""


def write_bench_html(payload: dict, path: Path) -> Path:
    path.write_text(_PAGE.replace("/*__DATA__*/null", json.dumps(payload, default=str).replace("</", "<\\/")))
    return path
