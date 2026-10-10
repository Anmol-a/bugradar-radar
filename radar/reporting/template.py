"""HTML/CSS/JS templates for reports. Kept separate from logic so the design can evolve freely.
Each template has the placeholder /*__DATA__*/null where the JSON payload is injected."""

_CSS = r"""
:root{--bg:#070b10;--panel:#0d141c;--panel2:#111a24;--line:#1c2835;--text:#e4ecf3;--muted:#8796a5;
--green:#36d68a;--amber:#f2b33d;--red:#ff5c5c;--grey:#5f6c79;--accent:#2ee6c5;--accent2:#1a9e8a;
--mono:ui-monospace,"JetBrains Mono","SF Mono",Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme: light){:root:not([data-theme=dark]){--bg:#f4f7f9;--panel:#fff;--panel2:#f7fafc;
--line:#dfe6ec;--text:#0f1b26;--muted:#5b6b7a;--accent:#0f9f88;--accent2:#0b7a69}}
*{box-sizing:border-box;min-width:0}html,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 var(--sans)}
a{color:var(--accent)}code,.mono{font-family:var(--mono);font-size:12.5px;overflow-wrap:anywhere}
.wrap{max-width:1240px;margin:0 auto;padding:24px 16px 80px}
header.top{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:20px}
.brand{font-family:var(--mono);letter-spacing:.18em;font-size:12px;color:var(--accent);text-transform:uppercase}
h1{font-size:26px;margin:2px 0 0;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;font-weight:700;
font-family:var(--mono);font-size:13px;letter-spacing:.08em;border:1px solid currentColor}
.pill::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor;box-shadow:0 0 12px currentColor}
.v-healthy,.s-pass{color:var(--green)}.v-degraded,.s-flaky,.s-warn{color:var(--amber)}
.v-down,.v-error,.v-unreachable,.s-confirmed_fail,.s-fail{color:var(--red)}.v-unsupported,.v-blocked,.v-no_network,.s-no_network,.s-blocked,.s-skip,.s-skipped,.s-info{color:var(--grey)}
.grid{display:grid;grid-template-columns:400px 1fr;gap:18px}
@media(max-width:920px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}
.card h2{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:0 0 12px;font-weight:600}
.radar-wrap{position:relative}
svg.radar{width:100%;height:auto;display:block}
.ring{fill:none;stroke:var(--line)}.ring-label{fill:var(--muted);font:10px var(--mono);letter-spacing:.08em}
.axis{stroke:var(--line);stroke-dasharray:2 4}
.sweep{transform-origin:0 0;animation:spin 4.5s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion: reduce){.sweep,.blip.fail{animation:none}}
.blip{cursor:pointer;transition:r .15s}.blip:hover{r:8}
.blip.fail{animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{50%{opacity:.35}}
.tip{position:absolute;pointer-events:none;background:var(--panel2);border:1px solid var(--line);border-radius:8px;
padding:6px 9px;font-size:12px;max-width:240px;display:none;z-index:5;box-shadow:0 6px 24px rgba(0,0,0,.35)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px;margin-bottom:14px}
.tile{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:12px}
.tile .n{font:700 26px var(--mono)}.tile .l{color:var(--muted);font-size:11.5px;letter-spacing:.06em;text-transform:uppercase}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px}.kv dt{color:var(--muted)}.kv dd{margin:0}
.notes{margin:10px 0 0;padding:0;list-style:none}.notes li{font-size:12.5px;color:var(--amber);padding:3px 0}
.notes li::before{content:"\25B8  "}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:18px 0 10px}
.chip{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:999px;padding:5px 12px;
font-size:12.5px;cursor:pointer}.chip.on{border-color:var(--accent);color:var(--accent)}
input.search{flex:1;min-width:160px;background:var(--panel);border:1px solid var(--line);color:var(--text);
border-radius:10px;padding:7px 12px;font-size:13px}
.suite{margin-bottom:14px}.suite-h{display:flex;align-items:baseline;gap:10px;margin:0 0 8px}
.suite-h h3{margin:0;font-size:15px}.suite-h span{color:var(--muted);font-size:12.5px}
.case{border:1px solid var(--line);border-radius:12px;background:var(--panel);margin-bottom:8px;overflow:hidden}
.case>summary{list-style:none;display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:11px 14px;cursor:pointer}
.case>summary::-webkit-details-marker{display:none}
.case[open]>summary{border-bottom:1px solid var(--line);background:var(--panel2)}
.dot{width:10px;height:10px;border-radius:50%;background:currentColor;flex:none;box-shadow:0 0 10px currentColor}
.ct{flex:1 1 160px;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tag{font:600 10.5px var(--mono);letter-spacing:.06em;padding:2px 7px;border-radius:6px;border:1px solid var(--line);color:var(--muted)}
.tag.seo{color:var(--muted, #888)}
.tag.critical{color:var(--red);border-color:color-mix(in srgb,var(--red) 40%,transparent)}
.tag.warn{color:var(--amber)}.tag.heal{color:var(--accent)}
.meta{color:var(--muted);font:12px var(--mono)}
.body{padding:12px 14px}
.tabs{display:flex;gap:6px;margin-bottom:10px}
.tab{font:12px var(--mono);padding:4px 10px;border-radius:8px;border:1px solid var(--line);cursor:pointer;background:none;color:var(--text)}
.tab.on{background:var(--panel2);border-color:var(--accent)}
.steps{display:flex;flex-direction:column;gap:2px}
.step{display:grid;grid-template-columns:22px 190px 1fr 90px;gap:10px;align-items:start;padding:6px 4px;border-radius:8px}
.step:hover{background:var(--panel2)}
@media(max-width:700px){.step{grid-template-columns:22px minmax(0,1fr)}.step .sd,.step .sb{grid-column:2}}
.si{font:700 13px var(--mono);text-align:center}.sn{font:12.5px var(--mono)}
.sd{font-size:12.5px;color:var(--muted);word-break:break-word}.sd .err{color:var(--red)}.sd .wrn{color:var(--amber)}
.sb{display:flex;align-items:center;gap:6px}.bar{height:6px;border-radius:3px;background:currentColor;opacity:.7;min-width:2px}
.sb .t{font:11px var(--mono);color:var(--muted)}
.heal{margin-top:4px;font-size:12px;color:var(--accent)}
.strip{display:flex;gap:8px;overflow-x:auto;padding:8px 2px 4px;margin-bottom:8px}
.strip figure{margin:0;flex:0 0 150px;cursor:pointer}.strip img{width:150px;height:94px;object-fit:cover;object-position:top;border-radius:6px;border:2px solid var(--line)}
.strip figure.fail img{border-color:var(--red)}.strip figure.warn img{border-color:var(--amber)}.strip figure.pass img{border-color:var(--green)}
.strip figcaption{font:10.5px var(--mono);color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.triage{margin:0 0 10px;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);font-size:12.5px}
.triage b{font-family:var(--mono)}.triage.real b{color:var(--red)}.triage.radar b,.triage.unsure b{color:var(--amber)}
.checks{margin:6px 0 2px;border:1px solid var(--line);border-radius:8px;overflow:hidden;display:table;width:100%;font-size:12px}
.checks .r{display:table-row}.checks .r>div{display:table-cell;padding:4px 8px;border-top:1px solid var(--line);vertical-align:top}
.checks .r:first-child>div{border-top:0}.checks .h>div{color:var(--muted);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;background:var(--panel2)}
.checks .ok{color:var(--green);font-weight:700;width:18px}.checks .no{color:var(--red);font-weight:700;width:18px}
.checks .exp{color:var(--muted)}.checks .bad{color:var(--red)}
@media(max-width:700px){.checks,.checks .r,.checks .r>div{display:block}.checks .h{display:none}}
.ev{margin-top:12px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}
.ev>summary{cursor:pointer;padding:8px 12px;font:12px var(--mono);color:var(--muted)}
.ev .evb{padding:4px 12px 12px;display:flex;flex-direction:column;gap:10px}
.ev h4{margin:0 0 4px;font:600 11px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.ev table{font:12px var(--mono)}.ev td.slow{color:var(--amber);font-weight:700}
.ev ul{margin:0;padding-left:18px;font:12px var(--mono);word-break:break-word}.ev li.error,.ev li.pageerror{color:var(--red)}.ev li.warning{color:var(--amber)}
.ev .note{font:11.5px var(--mono);color:var(--muted)}
.evidence{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px;align-items:flex-start}
.evidence img{max-width:min(420px,100%);border:1px solid var(--line);border-radius:10px;cursor:zoom-in}
.evidence .how{font-size:12px;color:var(--muted);max-width:420px}
table{display:block;overflow-x:auto;width:100%;border-collapse:collapse;font-size:12.5px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:11px;letter-spacing:.08em;text-transform:uppercase}
.hist{display:flex;gap:3px;align-items:flex-end;height:46px;margin-top:6px}
.hist a{flex:1;max-width:16px;border-radius:3px;background:currentColor;opacity:.8;min-height:8px}
.hist a.cur{outline:2px solid var(--text);outline-offset:2px}
.section{margin-top:22px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.lightbox{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;z-index:20;padding:20px}
.lightbox img{max-width:100%;max-height:100%;border-radius:8px}
footer{margin-top:40px;color:var(--muted);font-size:12px}
"""

_RUN_JS = r"""
const D = /*__DATA__*/null; const R = D.run;
const $ = (s, el=document) => el.querySelector(s);
const h = (tag, attrs={}, ...kids) => { const e = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)) { if (k==='class') e.className=v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v!==null && v!==undefined) e.setAttribute(k, v); }
  for (const k of kids.flat()) if (k!==null && k!==undefined) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e; };
const ICON = {pass:'✓',fail:'✕',warn:'!',info:'i',skip:'–'};
const SUITES = ['journey','smoke','catalog','product','cart','search','health','info'];
const caseStatus = c => c.verdict;
const hasWarn = c => c.attempts.some(a => a.steps.some(s => s.status==='warn'));
const hasHeal = c => c.attempts.some(a => a.steps.some(s => s.healed));
const dur = (a,b) => { const s=(new Date(b)-new Date(a))/1000; return isNaN(s)?'-':(s>90?Math.round(s/60)+'m':Math.round(s)+'s'); };

function header(){
  $('#site').textContent = R.site_id;
  $('#base').href = R.base_url; $('#base').textContent = R.base_url;
  const p = $('#verdict'); p.textContent = R.verdict.toUpperCase(); p.className = 'pill v-'+R.verdict;
  const dv = $('#device'); dv.textContent = R.device.toUpperCase();
  $('#when').textContent = `${new Date(R.started_at).toLocaleString()} · ${R.device} · ${dur(R.started_at,R.finished_at)} · run ${R.run_id}`;
  document.title = `Radar · ${R.site_id} · ${R.verdict}`;
}

function tiles(){
  const c = R.counts; const warn = R.cases.filter(hasWarn).length;
  const T = [['pass',c.pass,'Passed'],['confirmed_fail',c.confirmed_fail,'Failed'],['flaky',c.flaky,'Flaky'],
    ['warn',warn,'With warnings'],['blocked',c.blocked,'Blocked'],['info',R.healing_events.length,'Healed']];
  $('#tiles').append(...T.map(([s,n,l]) => h('div',{class:'tile'}, h('div',{class:'n s-'+s}, n), h('div',{class:'l'}, l))));
}

function radar(){
  const svg = $('#radar'); const NS='http://www.w3.org/2000/svg';
  const el = (t,a) => { const e=document.createElementNS(NS,t); for (const k in a) e.setAttribute(k,a[k]); return e; };
  const present = SUITES.filter(s => R.cases.some(c => c.suite===s));
  const ringR = i => 34 + i * (130 / Math.max(present.length,1));
  const defs = el('defs',{}); const g = el('linearGradient',{id:'sw',x1:'0',y1:'0',x2:'1',y2:'0'});
  g.append(el('stop',{offset:'0','stop-color':'var(--accent)','stop-opacity':'0'}), el('stop',{offset:'1','stop-color':'var(--accent)','stop-opacity':'.35'}));
  defs.append(g); svg.append(defs);
  for (let i=0;i<4;i++){ const a=i*Math.PI/4; svg.append(el('line',{class:'axis',x1:-170*Math.cos(a),y1:-170*Math.sin(a),x2:170*Math.cos(a),y2:170*Math.sin(a)})); }
  present.forEach((s,i) => { const r = ringR(i)+12; svg.append(el('circle',{class:'ring',r}));
    const t = el('text',{class:'ring-label',x:4,y:-r+11}); t.textContent = s.toUpperCase(); svg.append(t); });
  const sweep = el('g',{class:'sweep'}); sweep.append(el('path',{d:'M0 0 L170 0 A170 170 0 0 0 120.2 -120.2 Z',fill:'url(#sw)'}));
  sweep.append(el('line',{x1:0,y1:0,x2:170,y2:0,stroke:'var(--accent)','stroke-width':1.5,'stroke-opacity':.8}));
  svg.append(sweep);
  svg.append(el('circle',{r:3,fill:'var(--accent)'}));
  const tip = $('#tip');
  present.forEach((s,i) => {
    const cs = R.cases.filter(c => c.suite===s); const off = i*0.9;
    cs.forEach((c,j) => {
      const a = off + j*(2*Math.PI/cs.length) - Math.PI/2; const r = ringR(i)+6;
      const col = {pass:'var(--green)',flaky:'var(--amber)',confirmed_fail:'var(--red)'}[c.verdict] || 'var(--grey)';
      const b = el('circle',{class:'blip'+(c.verdict==='confirmed_fail'?' fail':''),cx:r*Math.cos(a),cy:r*Math.sin(a),r:5.5,fill:col,stroke:'var(--panel)','stroke-width':2});
      b.addEventListener('mousemove', ev => { const box=svg.parentNode.getBoundingClientRect();
        tip.style.display='block'; tip.style.left=(ev.clientX-box.left+12)+'px'; tip.style.top=(ev.clientY-box.top+12)+'px';
        tip.innerHTML=''; tip.append(h('b',{class:'s-'+c.verdict}, c.verdict.replace('_',' ')), h('div',{}, c.title)); });
      b.addEventListener('mouseleave', () => tip.style.display='none');
      b.addEventListener('click', () => { const d=document.getElementById('c-'+c.case_id); if(d){ d.open=true; d.scrollIntoView({behavior:'smooth',block:'center'}); } });
      svg.append(b);
    });
  });
}

function discovery(){
  const s = R.sitemap_summary || {};
  const kv = $('#disc');
  const pf = R.perf || {};
  const rows = [['Screen size', R.device === 'mobile' ? 'mobile (Pixel 7, touch)' : 'desktop (1366×850)'],
    ['Platform', s.platform + (s.evidence && s.evidence.length ? ` (${s.evidence.join(', ')})` : '')],
    ['Homepage', s.home_title || '-'], ['Theme', s.theme || 'unknown'], ['Checkout', s.checkout_app || '-'],
    ['Access', s.access || 'open'], ['Nav links', s.nav], ['Collections', s.collections],
    ['Products sampled', s.products!==undefined ? `${s.products} (${s.in_stock} in stock)` : '-'],
    ['Search', s.search_path || '-'], ['robots.txt', s.robots_loaded ? 'read and respected' : 'not readable'],
    ['Page load', pf.pages ? `${pf.pages} pages, median ${pf.median_load_secs}s` + (pf.slowest ? `, slowest ${pf.slowest.url} ${pf.slowest.load_secs}s` : '') : '-'],
    ['Console', pf.pages!==undefined || pf.console_errors!==undefined ? `${pf.console_errors} errors, ${pf.console_warnings} warnings, ${pf.failed_requests} failed requests (evidence only)` : '-'],
    ['LLM healing', R.llm_usage.provider + (R.llm_usage.model ? ` · ${R.llm_usage.model} · ${R.llm_usage.calls} call(s), ${R.llm_usage.input_tokens+R.llm_usage.output_tokens} tokens` : '')]];
  for (const [k,v] of rows) kv.append(h('dt',{},k), h('dd',{},v ?? '-'));
  const ul = $('#notes'); (R.notes||[]).forEach(n => ul.append(h('li',{},n)));
  if ((R.llm_usage.errors||[]).length) R.llm_usage.errors.forEach(e => ul.append(h('li',{},'LLM error: '+e)));
}

function stepRow(s, maxT){
  const st = s.status; const det = h('div',{class:'sd'});
  if (s.error) det.append(h('span',{class: st==='warn'?'wrn':'err'}, s.error));
  else if (s.detail!==null && s.detail!==undefined) det.append(typeof s.detail==='object' ? JSON.stringify(s.detail) : String(s.detail));
  if (s.healed) det.append(h('div',{class:'heal'}, `⟲ healed by ${s.healed.method}: ${s.healed.new}  (${s.healed.why})`));
  if (s.checks && s.checks.length) det.append(h('div',{class:'checks'},
    h('div',{class:'r h'}, h('div',{},''), h('div',{},'Assertion'), h('div',{},'Expected'), h('div',{},'Actual')),
    s.checks.map(k => h('div',{class:'r'}, h('div',{class:k.ok?'ok':'no'}, k.ok?'✓':'✕'), h('div',{}, k.what),
      h('div',{class:'exp'}, k.expected), h('div',{class:k.ok?'':'bad'}, k.actual)))));
  return h('div',{class:'step'}, h('div',{class:'si s-'+st}, ICON[st]||'?'), h('div',{class:'sn'}, s.name), det,
    h('div',{class:'sb s-'+st}, h('div',{class:'bar',style:`width:${Math.max(2,Math.round(60*(s.secs||0)/maxT))}px`}), h('span',{class:'t'}, (s.secs||0)+'s')));
}

const SLOW = 3;   // seconds: a page load above this is highlighted (information only, never a verdict)
function evidencePanel(a){
  const con = a.console||[], bad = a.failed_requests||[], loads = a.loads||[];
  if (!con.length && !bad.length && !loads.length) return null;
  const errs = con.filter(c => c.type==='error' || c.type==='pageerror').length, warns = con.filter(c => c.type==='warning').length;
  const body = h('div',{class:'evb'});
  if (loads.length) body.append(h('div',{}, h('h4',{}, 'Page load times'),
    h('table',{}, h('thead',{}, h('tr',{}, h('th',{},'Page'), h('th',{},'Server reply'), h('th',{},'DOM ready'), h('th',{},'Fully loaded'), h('th',{},'Main content shown'))),
      h('tbody',{}, loads.map(l => h('tr',{}, h('td',{}, l.url), h('td',{}, l.ttfb==null?'–':l.ttfb+'s'), h('td',{}, l.dcl==null?'–':l.dcl+'s'),
        h('td',{class:l.load>SLOW?'slow':''}, l.load==null?'–':l.load+'s'), h('td',{class:l.lcp>SLOW?'slow':''}, l.lcp==null?'–':l.lcp+'s')))))));
  if (con.length) body.append(h('div',{}, h('h4',{}, 'Browser console (errors and warnings)'),
    h('ul',{}, con.map(c => h('li',{class:c.type}, `[${c.type}] ${c.text}`, c.url ? h('span',{class:'note'}, '  · '+c.url) : '')))));
  if (bad.length) body.append(h('div',{}, h('h4',{}, 'Requests that got no answer'), h('ul',{}, bad.map(b => h('li',{}, b)))));
  body.append(h('div',{class:'note'}, 'Evidence only. Most stores log some third-party errors; none of this changes a pass or fail.'));
  const bits = [];
  if (loads.length) bits.push(`${loads.length} page${loads.length>1?'s':''} timed`);
  bits.push(`console: ${errs} error${errs===1?'':'s'}, ${warns} warning${warns===1?'':'s'}`);
  if (bad.length) bits.push(`${bad.length} failed request${bad.length===1?'':'s'}`);
  return h('details',{class:'ev'}, h('summary',{}, 'Page timing & console · ' + bits.join(' · ')), body);
}

function attemptView(a){
  const maxT = Math.max(0.5, ...a.steps.map(s => s.secs||0));
  const wrap = h('div',{});
  if (a.note) wrap.append(h('div',{class:'meta',style:'margin-bottom:6px'}, a.note));
  const shots = a.steps.filter(s => s.shot);
  if (shots.length) wrap.append(h('div',{class:'strip'}, shots.map((s,i) => h('figure',{class:s.status,
      onclick:()=>{ $('#lb img').src=s.shot; $('#lb').style.display='flex'; }},
      h('img',{src:s.shot, alt:s.name, loading:'lazy'}), h('figcaption',{}, `${i+1}. ${s.name}`)))));
  wrap.append(h('div',{class:'steps'}, a.steps.map(s => stepRow(s,maxT))));
  const evp = evidencePanel(a); if (evp) wrap.append(evp);
  if (a.screenshot || a.trace) {
    const ev = h('div',{class:'evidence'});
    if (a.screenshot) ev.append(h('img',{src:a.screenshot, alt:'failure screenshot', onclick:()=>{ $('#lb img').src=a.screenshot; $('#lb').style.display='flex'; }}));
    if (a.trace) ev.append(h('div',{class:'how'}, h('div',{}, h('a',{href:a.trace}, 'Download Playwright trace')),
      h('div',{}, 'Replay every action, DOM snapshot and network call:'), h('code',{}, `playwright show-trace "${a.trace}"`)));
    wrap.append(ev);
  }
  return wrap;
}

function caseCard(c){
  const tags = [h('span',{class:'tag '+c.severity}, c.severity)];
  if (hasWarn(c)) tags.push(h('span',{class:'tag warn'}, 'warnings'));
  if (hasHeal(c)) tags.push(h('span',{class:'tag heal'}, 'healed'));
  if (c.healed_after_triage) tags.push(h('span',{class:'tag heal'}, 'passed on LLM-assisted re-check'));
  if (c.triage && c.verdict === 'confirmed_fail') tags.push(h('span',{class:'tag '+(c.triage.verdict==='real_store_problem'?'critical':'warn')},
      c.triage.verdict==='real_store_problem' ? 'store problem (LLM)' : c.triage.verdict==='radar_problem' ? 'Radar suspect (LLM)' : 'unsure (LLM)'));
  const secs = c.attempts.reduce((t,a) => t + a.secs, 0).toFixed(1);
  const body = h('div',{class:'body'});
  const tabs = h('div',{class:'tabs'}); const pane = h('div',{});
  c.attempts.forEach((a,i) => { const t = h('button',{class:'tab'+(i===c.attempts.length-1?' on':''), onclick:() => {
      tabs.querySelectorAll('.tab').forEach(x=>x.classList.remove('on')); t.classList.add('on'); pane.innerHTML=''; pane.append(attemptView(a)); }},
      `attempt ${a.n} ${a.ok?'✓':'✕'}`); tabs.append(t); });
  if (c.triage) body.append(h('div',{class:'triage '+(c.triage.verdict==='real_store_problem'?'real':c.triage.verdict==='radar_problem'?'radar':'unsure')},
      h('b',{}, 'LLM triage: '+c.triage.verdict.replace(/_/g,' ')), ` · ${c.triage.category.replace(/_/g,' ')} · ${c.triage.reason}`,
      c.triage.evidence ? h('div',{class:'meta'}, 'evidence: '+c.triage.evidence) : ''));
  if (c.attempts.length > 1) body.append(tabs);
  if (c.attempts.length) pane.append(attemptView(c.attempts[c.attempts.length-1]));
  body.append(pane);
  if (c.incident_signature) body.append(h('div',{class:'meta',style:'margin-top:10px'}, 'incident: '+c.incident_signature));
  return h('details',{class:'case', id:'c-'+c.case_id, 'data-v':c.verdict, 'data-w':hasWarn(c)?1:0, 'data-h':hasHeal(c)?1:0, 'data-t':(c.title+' '+c.case_id).toLowerCase()},
    h('summary',{}, h('span',{class:'dot s-'+c.verdict}), h('span',{class:'ct'}, c.title), ...tags,
      h('span',{class:'meta'}, `${c.attempts.length}× · ${secs}s`)), body);
}

function suites(){
  const root = $('#suites');
  if (!R.cases.length) { root.append(h('div',{class:'empty'}, R.verdict==='unsupported' ? 'No tests generated: this site is not a platform Radar v1 supports.' : R.verdict==='no_network' ? 'Radar had no internet connection; nothing is reported against this store. Run again.' : 'No tests ran.')); return; }
  const order = SUITES.filter(s => R.cases.some(c => c.suite===s));
  for (const s of order) {
    const cs = R.cases.filter(c => c.suite===s); const ok = cs.filter(c => c.verdict==='pass').length;
    root.append(h('div',{class:'suite','data-s':s}, h('div',{class:'suite-h'}, h('h3',{}, s[0].toUpperCase()+s.slice(1)), h('span',{}, `${ok}/${cs.length} passing`)), cs.map(caseCard)));
  }
  const failing = R.cases.find(c => c.verdict==='confirmed_fail'); if (failing) document.getElementById('c-'+failing.case_id).open = true;
}

let filter='all';
function applyFilter(){
  const q = $('#q').value.trim().toLowerCase();
  document.querySelectorAll('.case').forEach(d => {
    const v=d.dataset.v; const ok = filter==='all' || (filter==='fail'&&v==='confirmed_fail') || (filter==='flaky'&&v==='flaky')
      || (filter==='warn'&&d.dataset.w==='1') || (filter==='heal'&&d.dataset.h==='1') || (filter==='blocked'&&v==='blocked');
    d.style.display = ok && (!q || d.dataset.t.includes(q)) ? '' : 'none'; });
  document.querySelectorAll('.suite').forEach(s => s.style.display = [...s.querySelectorAll('.case')].some(c => c.style.display!=='none') ? '' : 'none');
}
function toolbar(){
  const F=[['all','All'],['fail','Failed'],['flaky','Flaky'],['warn','Warnings'],['heal','Healed'],['blocked','Blocked']];
  const bar=$('#chips'); F.forEach(([k,l]) => bar.append(h('button',{class:'chip'+(k==='all'?' on':''), onclick:e=>{ filter=k;
    bar.querySelectorAll('.chip').forEach(c=>c.classList.remove('on')); e.target.classList.add('on'); applyFilter(); }}, l)));
  $('#q').addEventListener('input', applyFilter);
}

function healing(){
  const t=$('#heal'); if (!R.healing_events.length) { t.replaceWith(h('div',{class:'empty'}, 'No locators needed healing this run.')); return; }
  R.healing_events.forEach(e => t.append(h('tr',{}, h('td',{}, e.intent), h('td',{}, e.method), h('td',{class:'mono'}, e.new), h('td',{}, e.why))));
}

function history(){
  const H=[...D.history].filter(r => r.device===R.device).reverse(); const box=$('#hist');
  H.forEach(r => box.append(h('a',{class:'s-'+({healthy:'pass',degraded:'flaky',down:'confirmed_fail'}[r.verdict]||'skip')+(r.run_id===R.run_id?' cur':''),
    href:`../${r.run_id}/report.html`, title:`${r.started_at} · ${r.device} · ${r.verdict}`, style:`height:${r.verdict==='healthy'?100:r.verdict==='degraded'?65:35}%`})));
  const t=$('#inc'); if (!D.incidents.length) { t.replaceWith(h('div',{class:'empty'}, 'No incidents recorded for this site.')); return; }
  D.incidents.forEach(i => t.append(h('tr',{}, h('td',{class:'s-'+(i.status==='open'?'fail':'pass')}, i.status), h('td',{}, i.case_id), h('td',{}, i.device||'desktop'),
    h('td',{}, i.occurrences), h('td',{class:'mono'}, new Date(i.first_seen).toLocaleString()), h('td',{}, (i.last_error||'').slice(0,140)))));
}

header(); tiles(); radar(); discovery(); toolbar(); suites(); healing(); history();
$('#lb').addEventListener('click', () => $('#lb').style.display='none');
"""

RUN_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Radar report</title>
<style>""" + _CSS + """</style></head><body><div class="wrap">
<header class="top"><div style="flex:1;min-width:240px"><div class="brand">BugRadar · Radar run</div>
<h1 id="site"></h1><div class="sub"><a id="base" target="_blank" rel="noopener"></a> · <span id="when"></span></div></div>
<span id="device" class="pill" style="background:var(--panel2);color:var(--text)"></span><span id="verdict" class="pill"></span><a class="sub" href="../../index.html">site history →</a></header>
<div class="grid">
 <div class="card radar-wrap"><h2>Radar</h2><svg id="radar" class="radar" viewBox="-180 -180 360 360" role="img" aria-label="Test results by suite"></svg><div id="tip" class="tip"></div>
 <div class="sub" style="margin-top:6px">Each ring is a suite, each blip a test case. Click a blip to open it.</div></div>
 <div><div id="tiles" class="tiles"></div>
 <div class="card"><h2>Discovery</h2><dl id="disc" class="kv"></dl><ul id="notes" class="notes"></ul></div></div>
</div>
<div class="toolbar"><div id="chips" style="display:flex;gap:6px;flex-wrap:wrap"></div><input id="q" class="search" placeholder="Filter test cases…"></div>
<div id="suites"></div>
<div class="section card"><h2>Self-healing log</h2><table><thead><tr><th>Intent</th><th>Method</th><th>New selector</th><th>Why</th></tr></thead><tbody id="heal"></tbody></table></div>
<div class="section card"><h2>History &amp; incidents</h2><div class="sub">Last runs on this screen size (click a bar to open that report)</div><div id="hist" class="hist"></div>
<table style="margin-top:14px"><thead><tr><th>Status</th><th>Test case</th><th>Device</th><th>Seen</th><th>First seen</th><th>Last error</th></tr></thead><tbody id="inc"></tbody></table></div>
<footer>Generated by Radar. Honest identification: requests carry the BugRadar User-Agent and respect robots.txt. Checkout is never clicked.</footer>
</div><div id="lb" class="lightbox"><img alt=""></div>
<script>""" + _RUN_JS + """</script></body></html>"""

_INDEX_JS = r"""
const D = /*__DATA__*/null;
const $ = s => document.querySelector(s);
const h = (t,a={},...k)=>{const e=document.createElement(t);for(const[x,v]of Object.entries(a)){if(x==='class')e.className=v;else e.setAttribute(x,v)}for(const c of k.flat())if(c!=null)e.append(c.nodeType?c:document.createTextNode(String(c)));return e};
$('#site').textContent = D.site_id; document.title = 'Radar · ' + D.site_id;
const H = D.history; const last = H[0];
if (last) { const p=$('#verdict'); p.textContent=last.verdict.toUpperCase(); p.className='pill v-'+last.verdict; }
const healthy = H.filter(r => r.verdict==='healthy').length;
const T = [[H.length,'Runs'],[H.length?Math.round(100*healthy/H.length)+'%':'-','Healthy runs'],
  [D.incidents.filter(i=>i.status==='open').length,'Open incidents'],[D.sitemap?D.sitemap.products.length:'-','Products sampled']];
$('#tiles').append(...T.map(([n,l]) => h('div',{class:'tile'}, h('div',{class:'n'},n), h('div',{class:'l'},l))));
const hist=$('#hist'); [...H].reverse().forEach(r => hist.append(h('a',{class:'s-'+({healthy:'pass',degraded:'flaky',down:'confirmed_fail'}[r.verdict]||'skip'),
  href:r.folder,title:r.started_at+' · '+r.verdict,style:'height:'+(r.verdict==='healthy'?100:r.verdict==='degraded'?65:35)+'%'})));
const tb=$('#runs'); H.forEach(r => tb.append(h('tr',{}, h('td',{}, h('a',{href:r.folder}, new Date(r.started_at).toLocaleString())),
  h('td',{}, r.device), h('td',{class:'v-'+r.verdict}, r.verdict), h('td',{}, r.n_pass), h('td',{}, r.n_flaky), h('td',{}, r.n_fail), h('td',{}, r.n_blocked))));
const it=$('#inc'); if(!D.incidents.length) it.replaceWith(h('div',{class:'empty'},'No incidents.'));
else D.incidents.forEach(i => it.append(h('tr',{}, h('td',{class:'s-'+(i.status==='open'?'fail':'pass')}, i.status), h('td',{}, i.case_id), h('td',{}, i.device||'desktop'), h('td',{}, i.occurrences), h('td',{}, (i.last_error||'').slice(0,160)))));
"""

INDEX_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Radar site</title>
<style>""" + _CSS + """</style></head><body><div class="wrap">
<header class="top"><div style="flex:1"><div class="brand">BugRadar · Site history</div><h1 id="site"></h1></div><span id="verdict" class="pill"></span></header>
<div id="tiles" class="tiles"></div>
<div class="card"><h2>Run timeline</h2><div id="hist" class="hist"></div></div>
<div class="section card"><h2>Runs</h2><table><thead><tr><th>Started</th><th>Device</th><th>Verdict</th><th>Pass</th><th>Flaky</th><th>Fail</th><th>Blocked</th></tr></thead><tbody id="runs"></tbody></table></div>
<div class="section card"><h2>Incidents</h2><table><thead><tr><th>Status</th><th>Test case</th><th>Device</th><th>Seen</th><th>Last error</th></tr></thead><tbody id="inc"></tbody></table></div>
</div><script>""" + _INDEX_JS + """</script></body></html>"""
