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
$('#when').textContent = 'bench ' + D.stamp + ' · ' + D.rows.length + ' stores';
const T = D.totals;
[[T.tested||0,'Tested'],[T.healthy||0,'Healthy'],[T.degraded||0,'Degraded'],[T.down||0,'Down'],
 [(T.unsupported||0)+(T.blocked||0),'Not testable'],[T.unreachable||0,'Wrong / dead URL'],[T.error||0,'Radar crashed']].forEach(([n,l]) =>
  $('#tiles').append(h('div',{class:'tile'}, h('div',{class:'n'},n), h('div',{class:'l'},l))));
const tb = $('#rows');
function render(filter){
  tb.innerHTML='';
  D.rows.filter(r => filter==='all' || (filter==='fail' && r.failures.length) || r.verdict===filter).forEach(r => {
    const tr = h('tr',{class:'srow'},
      h('td',{}, h('b',{}, r.site_id), h('div',{class:'meta'}, r.cart ? 'cart flow on' : 'read-only')),
      h('td',{class:'v-'+r.verdict}, r.verdict),
      h('td',{}, r.theme || '–'), h('td',{}, r.checkout || '–'),
      ...SUITES.map(s => { const c = CELL[r.suites[s]] || ['·','meta']; return h('td',{class:c[1],style:'text-align:center;font-weight:700'}, c[0]); }),
      h('td',{}, r.failures.length || ''), h('td',{}, r.warnings || ''),
      h('td',{}, r.report_rel ? h('a',{href:r.report_rel}, 'report') : ''));
    const det = h('tr',{class:'det',style:'display:none'}, h('td',{colspan: 14},
      r.notes.length ? h('ul',{class:'notes'}, r.notes.map(n => h('li',{}, n))) : null,
      r.failures.length ? h('table',{}, h('thead',{}, h('tr',{}, h('th',{},'Test case'), h('th',{},'Verdict'), h('th',{},'Step'), h('th',{},'Why'), h('th',{},'LLM triage'))),
        h('tbody',{}, r.failures.map(f => h('tr',{}, h('td',{class:'mono'}, f.case), h('td',{class:'s-'+f.verdict}, f.verdict),
          h('td',{class:'mono'}, f.step||''), h('td',{}, f.error), h('td',{}, f.llm||''))))) : h('div',{class:'empty'}, 'No failures.')));
    tr.addEventListener('click', e => { if (e.target.tagName !== 'A') det.style.display = det.style.display === 'none' ? '' : 'none'; });
    tb.append(tr, det);
  });
}
const F=[['all','All'],['fail','With failures'],['healthy','Healthy'],['degraded','Degraded'],['down','Down'],['blocked','Blocked'],['unsupported','Unsupported'],['unreachable','Wrong URL'],['error','Crashed']];
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
<div class="card"><table><thead><tr><th>Store</th><th>Verdict</th><th>Theme</th><th>Checkout</th>
<th>Journey</th><th>Smoke</th><th>Catalog</th><th>Product</th><th>Cart</th><th>Search</th><th>Health</th><th>Fails</th><th>Warns</th><th></th></tr></thead>
<tbody id="rows"></tbody></table><div class="sub" style="margin-top:8px">Click a row for failures and notes. ✓ pass · ~ flaky · ✕ confirmed failure · – blocked/skipped · · not run</div></div>
</div><script>""" + _JS + """</script></body></html>"""


def write_bench_html(payload: dict, path: Path) -> Path:
    path.write_text(_PAGE.replace("/*__DATA__*/null", json.dumps(payload, default=str).replace("</", "<\\/")))
    return path
