"""Camera Settings page (served at /settings): rename and re-order cameras while
watching their live preview. Data comes from GET /api/camera-settings and is saved
with PUT /api/camera-settings; see camera_settings.py for the storage/order model.

Preview streams reuse the normal /stream/<index> worker at a low send rate
(?fps=2) -- no extra RTSP connection. Only 4 previews are shown per page: a browser
allows ~6 connections per server and each MJPEG preview holds one for its whole
life, so 4 leaves room for the Save and status requests (with 6, Save would hang).
Previews are stopped whenever they are not on screen (the "Grid order" tab on a
phone, page hidden/left), so they never hold NVR slots for nothing.
All user-entered names are rendered with textContent, never as HTML.

Layout: wide screens show the camera cards and the grid-order list side by side;
narrower screens switch between them with tabs.
"""
from ui_theme import render

SETTINGS_PAGE = render(r"""<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=color-scheme content=dark>
<meta name=theme-color content="#0a0c10">
<title>Camera Settings · GRAV CCTV</title>
<link rel=icon href="{{FAVICON}}">
<style>
/*@theme*/
body{min-height:100vh}
.appbar .vdiv{width:1px;height:26px;background:var(--border-2);flex:none}
.titles{display:flex;flex-direction:column;min-width:0;line-height:1.25}
.titles b{font-size:16px;font-weight:700}
.titles small{color:var(--muted);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wrap{max-width:1500px;margin:0 auto;padding:18px max(20px,var(--sar)) 28px max(20px,var(--sal))}
body.has-savebar .wrap{padding-bottom:110px}

.alert{display:flex;gap:12px;align-items:flex-start;margin-bottom:16px;padding:12px 14px;border-radius:var(--r);
  background:var(--warn-soft);border:1px solid rgba(245,158,11,.35);color:#fde68a;font-size:13px}
.alert>.ic{color:#fbbf24;margin-top:1px}
.alert div{display:flex;flex-direction:column;gap:2px;min-width:0;overflow-wrap:anywhere}

.toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.searchbox{position:relative;flex:0 1 380px;min-width:220px}
.searchbox .ic{position:absolute;left:12px;top:50%;margin-top:-9px;color:var(--muted)}
.searchbox input{width:100%;padding-left:38px}
.summary{margin-left:auto}
.tabs{display:none;gap:3px;padding:3px;border-radius:12px;background:var(--surface-2);border:1px solid var(--border)}
.tabs button{height:32px;border:0;border-radius:9px;background:transparent;color:var(--text-2);padding:0 14px;font-weight:600}
.tabs button:hover:not(:disabled){background:var(--surface-3);color:var(--text)}
.tabs button[aria-selected=true],.tabs button[aria-selected=true]:hover:not(:disabled){background:var(--surface-4);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.4)}
.tabs .ic{width:16px;height:16px}

#layout{display:grid;grid-template-columns:minmax(0,1fr) 370px;gap:20px;align-items:start}
#camPane{scroll-margin-top:calc(var(--bar) + var(--sat) + 12px)}
.pager{display:flex;align-items:center;gap:10px;margin-bottom:14px;min-height:36px}
.pinfo-wrap{display:flex;flex-direction:column;line-height:1.25;min-width:0}
.pinfo{font-weight:650;font-variant-numeric:tabular-nums}
.pgnum{font-variant-numeric:tabular-nums}
.pager.bottom{justify-content:space-between;margin:16px 0 0}
.loading,.emptystate{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:56px 16px;
  border:1px dashed var(--border-2);border-radius:var(--r-lg);color:var(--muted);text-align:center}
.emptystate .ic{width:26px;height:26px}
.emptystate b{color:var(--text)}

#cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.card{display:flex;flex-direction:column;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);
  overflow:hidden;scroll-margin-top:calc(var(--bar) + var(--sat) + 12px);transition:border-color .2s,box-shadow .2s}
.card.dirty{border-color:rgba(245,158,11,.55);box-shadow:0 0 0 1px rgba(245,158,11,.22)}
.card.flash{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.pv{position:relative;aspect-ratio:16/9;background:#05070a;overflow:hidden}
.pv img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;opacity:0;transition:opacity .25s}
.pv.ready img{opacity:1}
.pvload{position:absolute;inset:0;display:grid;place-items:center}
.pv.ready .pvload{display:none}
.poschip{flex:none;min-width:34px;height:26px;margin-top:1px;padding:0 7px;display:inline-flex;align-items:center;
  justify-content:center;border-radius:7px;background:var(--surface-3);color:var(--text-2);font-size:12px;font-weight:700;
  font-variant-numeric:tabular-nums}
.pills{flex:none;display:flex;flex-direction:column;align-items:flex-end;gap:6px}
.badge{height:22px;display:inline-flex;align-items:center;gap:6px;padding:0 9px;border-radius:999px;white-space:nowrap;
  background:var(--surface-2);border:1px solid var(--border-2);color:var(--text-2);font-size:11.5px;font-weight:650}
.badge::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.badge.live{color:#4ade80} .badge.wait{color:#fbbf24} .badge.bad{color:#f87171} .badge.idle{color:var(--muted)}
.badge:empty{display:none}
.body{display:flex;flex-direction:column;gap:14px;padding:14px 16px 16px}
.head{display:flex;align-items:flex-start;gap:10px}
.ttl{flex:1;min-width:0}
.dname{font-size:16.5px;font-weight:700;line-height:1.3;overflow-wrap:anywhere}
.tech{font-size:12.5px;color:var(--muted);margin-top:2px}
.unsaved{flex:none;padding:3px 8px;border-radius:999px;background:var(--warn-soft);border:1px solid rgba(245,158,11,.35);
  color:#fbbf24;font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase}
.field{display:flex;flex-direction:column;gap:6px}
.lbl{display:flex;justify-content:space-between;gap:8px;font-size:12px;font-weight:600;color:var(--text-2)}
.count{font-weight:500;color:var(--faint);font-variant-numeric:tabular-nums}
.iname{width:100%}
.orderrow{display:flex;align-items:center;gap:6px}
.iorder{width:92px;text-align:center;font-variant-numeric:tabular-nums}
.iaudio{height:38px;padding:0 12px;border-radius:var(--r);border:1px solid var(--border-2);background:var(--bg);
  color:var(--text);font:inherit;font-size:14px;width:100%;max-width:360px;cursor:pointer}
.iaudio:hover{border-color:var(--border-3)}
.iaudio:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.adet{font-weight:500;color:var(--muted)}
.adet.ok{color:#4ade80}
.where{flex:1;min-width:0;margin-left:4px;font-size:12px;color:var(--muted)}
input.bad,input.bad:focus{border-color:var(--bad);box-shadow:0 0 0 3px var(--bad-soft)}
.err{display:flex;flex-direction:column;align-items:flex-start;gap:6px;padding:9px 11px;border-radius:var(--r-sm);
  background:var(--bad-soft);border:1px solid rgba(239,68,68,.3);color:#fca5a5;font-size:12.5px}
.err:empty{display:none}
.err button{height:auto;min-height:30px;padding:6px 10px;white-space:normal;text-align:left;line-height:1.3;font-size:12px}
.actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding-top:12px;border-top:1px solid var(--border)}
.actions .save{min-width:84px;margin-left:auto}          /* stays right-aligned when it wraps */

#orderPane{position:sticky;top:calc(var(--bar) + var(--sat) + 18px);display:flex;flex-direction:column;
  max-height:calc(100vh - var(--bar) - var(--sat) - 36px);background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r-lg);overflow:hidden}
.ohead{display:flex;align-items:flex-start;gap:10px;padding:14px 14px 12px 16px;border-bottom:1px solid var(--border)}
.ohead b{font-size:14.5px}
.ohead small{display:block;margin-top:2px;color:var(--muted);font-size:12px;line-height:1.4}
#orderList{list-style:none;margin:0;padding:4px 8px 12px;overflow:auto;scrollbar-width:thin}
#orderList li.sep{display:flex;align-items:center;gap:10px;padding:14px 6px 6px;color:var(--faint);font-size:11px;
  font-weight:700;letter-spacing:.07em;text-transform:uppercase;cursor:default}
#orderList li.sep::after{content:"";flex:1;height:1px;background:var(--border)}
#orderList li.row{display:flex;align-items:center;gap:8px;margin:1px 0;padding:5px 6px 5px 4px;border-radius:var(--r-sm);
  border:1px solid transparent;cursor:grab;transition:background-color .12s,border-color .12s}
#orderList li.row:hover{background:var(--surface-2)}
#orderList li.inview{background:var(--surface-2);border-color:var(--border-2)}
#orderList li.match .t{color:var(--accent-2)}
#orderList li.pend .pos{color:#fbbf24}
#orderList li.over-top{box-shadow:inset 0 2px 0 var(--accent)}
#orderList li.over-bot{box-shadow:inset 0 -2px 0 var(--accent)}
#orderList li.dragging{opacity:.4}
#orderList .grip{display:flex;color:var(--faint)}
#orderList .grip .ic{width:16px;height:16px}
#orderList .pos{width:24px;text-align:right;font-size:12px;font-weight:700;color:var(--muted);font-variant-numeric:tabular-nums}
#orderList .nm{flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.3}
#orderList .nm span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#orderList .t{font-size:13.5px;font-weight:550}
#orderList .tn{font-size:11.5px;color:var(--muted)}
#orderList .dot{width:7px;height:7px;border-radius:50%;background:var(--warn);flex:none}

#savebar{position:fixed;left:50%;bottom:calc(18px + var(--sab));transform:translateX(-50%);z-index:40;display:flex;
  align-items:center;gap:10px;width:min(660px,calc(100vw - 24px));padding:10px 10px 10px 16px;border-radius:14px;
  background:var(--surface-3);border:1px solid var(--border-3);box-shadow:var(--shadow);animation:rise .2s ease-out}
#savebar .dot{width:8px;height:8px;border-radius:50%;background:var(--warn);box-shadow:0 0 0 4px var(--warn-soft);flex:none}
#dirty{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@keyframes rise{from{opacity:0;transform:translate(-50%,10px)}}

/* ── responsive ──────────────────────────────────────────── */
@media (max-width:1099px){
  #layout{grid-template-columns:minmax(0,1fr)}
  .tabs{display:inline-flex}
  body[data-tab=cams] #orderPane{display:none}
  body[data-tab=order] #camPane{display:none}
  #orderPane{position:static;max-height:none}
  #orderList{overflow:visible}
  .hide-md{display:none}
}
@media (max-width:699px){
  .wrap{padding:12px max(12px,var(--sar)) 20px max(12px,var(--sal))}
  .appbar{gap:8px;padding-left:max(8px,var(--sal))}
  .appbar .vdiv,.titles small,.hide-sm{display:none}
  #back{width:40px;padding:0}
  .toolbar{flex-direction:column;align-items:stretch;gap:10px}
  .searchbox{flex:none;min-width:0}
  .tabs{display:flex}
  .tabs button{flex:1}
  .summary{margin-left:0;text-align:center}
  #cards{grid-template-columns:minmax(0,1fr);gap:14px}
  .actions .grow{display:none}
  .actions .save{flex:1 1 100%}
  button,.btn{height:40px}
  button.sm,.btn.sm{height:34px}
  button.icon,.btn.icon{width:40px}
  button.icon.sm{width:34px}
  input[type=text],input[type=search],input[type=number],input:not([type]){height:42px;font-size:16px}
  #savebar{left:0;right:0;bottom:0;width:auto;transform:none;border-radius:16px 16px 0 0;border-width:1px 0 0;
    padding:10px max(12px,var(--sar)) calc(10px + var(--sab)) max(14px,var(--sal));animation:none}
  #savebar button{flex:none}
  .toast{top:auto;bottom:calc(92px + var(--sab))}
}
</style>
</head>
<body data-tab=cams>
<!--@icons-->
<header class=appbar>
  <a class="btn ghost" id=back href="/" title="Back to the live view"><svg class=ic><use href="#i-back"/></svg><span class=hide-sm>Live view</span></a>
  <span class=vdiv></span>
  <div class=titles><b>Camera Settings</b><small>Rename cameras and choose their order on the live grid</small></div>
</header>
<div id=msg class=toast role=status aria-live=polite hidden></div>
<main class=wrap>
  <div id=warn class=alert hidden><svg class=ic><use href="#i-alert"/></svg><div><b>Settings file notes</b><span id=warnText></span></div></div>
  <div class=toolbar>
    <label class=searchbox><svg class=ic><use href="#i-search"/></svg><input id=search type=search placeholder="Search name, NVR or channel&hellip;" autocomplete=off aria-label="Search cameras"></label>
    <div class=tabs role=tablist aria-label="Sections">
      <button role=tab data-tab=cams aria-selected=true><svg class=ic><use href="#i-cam"/></svg>Cameras</button>
      <button role=tab data-tab=order aria-selected=false><svg class=ic><use href="#i-list"/></svg>Grid order</button>
    </div>
    <span id=summary class="hint summary"></span>
  </div>
  <div id=layout>
    <section id=camPane aria-label="Cameras">
      <div class=pager>
        <button class="icon pprev" title="Previous cameras" aria-label="Previous cameras"><svg class=ic><use href="#i-left"/></svg></button>
        <div class=pinfo-wrap><b id=pinfo class=pinfo>Loading&hellip;</b><span class="pgnum hint"></span></div>
        <button class="icon pnext" title="Next cameras" aria-label="Next cameras"><svg class=ic><use href="#i-right"/></svg></button>
        <span class=grow></span>
        <span class="hint hide-md">Live preview (2 fps), 4 cameras per page. Leave a name empty to use the technical name.</span>
      </div>
      <div id=loading class=loading><span class=spinner></span>Loading cameras&hellip;</div>
      <div id=empty class=emptystate hidden><svg class=ic><use href="#i-search"/></svg><b>No camera matches your search</b><span class=hint>Try a camera name, &ldquo;NVR1&rdquo; or a channel such as &ldquo;ch 8&rdquo;.</span></div>
      <div id=cards></div>
      <div class="pager bottom">
        <button class=pprev><svg class=ic><use href="#i-left"/></svg>Previous</button>
        <span class="pgnum hint"></span>
        <button class=pnext>Next<svg class=ic><use href="#i-right"/></svg></button>
      </div>
    </section>
    <aside id=orderPane aria-label="Grid order">
      <div class=ohead>
        <div class=grow><b>Grid order</b><small>Drag a row, or use the arrows. The live grid shows 6 cameras per page.</small></div>
        <button id=resetOrder class="sm ghost danger" title="Back to the original order"><svg class=ic><use href="#i-reset"/></svg>Reset all ordering</button>
      </div>
      <ol id=orderList></ol>
    </aside>
  </div>
</main>
<div id=savebar role=region aria-label="Unsaved changes" hidden>
  <span class=dot></span>
  <span id=dirty></span>
  <span class=grow></span>
  <button id=discard class=ghost>Discard</button>
  <button id=saveAll class=primary disabled title="Ctrl+S">Save All Changes</button>
</div>
<template id=cardTpl>
  <article class=card>
    <div class=pv>
      <img fetchpriority=high alt="">
      <div class=pvload><span class=spinner></span></div>
    </div>
    <div class=body>
      <div class=head>
        <span class=poschip title="Position on the live grid"></span>
        <div class=ttl><div class=dname></div><div class=tech></div></div>
        <div class=pills><span class=badge></span><span class=unsaved hidden>Unsaved</span></div>
      </div>
      <div class=field>
        <label class="lbl lname">Display name <span class=count></span></label>
        <input class=iname maxlength=60 autocomplete=off spellcheck=false enterkeyhint=done>
      </div>
      <div class=field>
        <label class="lbl lorder">Position on the live grid</label>
        <div class=orderrow>
          <input class=iorder type=number min=1 step=1 inputmode=numeric enterkeyhint=done>
          <button class="up icon" title="Move one place earlier" aria-label="Move one place earlier"><svg class=ic><use href="#i-up"/></svg></button>
          <button class="down icon" title="Move one place later" aria-label="Move one place later"><svg class=ic><use href="#i-down"/></svg></button>
          <span class=where></span>
        </div>
      </div>
      <div class=field>
        <label class="lbl laudio">Audio <span class=adet></span></label>
        <select class=iaudio>
          <option value=auto>Automatic (use what the NVR stream offers)</option>
          <option value=on>On &ndash; always offer the speaker button</option>
          <option value=off>Off &ndash; never offer audio for this camera</option>
        </select>
      </div>
      <div class=err></div>
      <div class=actions>
        <button class="rname ghost sm" title="Use the technical name again"><svg class=ic><use href="#i-reset"/></svg>Reset name</button>
        <button class="rorder ghost sm" title="Automatic position (original order)"><svg class=ic><use href="#i-reset"/></svg>Reset order</button>
        <span class=grow></span>
        <button class="save primary">Save</button>
      </div>
    </div>
  </article>
</template>
<script>
const PER = 4, PREVIEW_FPS = 2, GRID_PER = 6;
const KEY = new URLSearchParams(location.search).get('key') || '';
const q = KEY ? '?key=' + encodeURIComponent(KEY) : '';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
document.getElementById('back').href = '/' + q;

let S = null, cams = [], byKey = {};   // last server snapshot (cameras in technical order)
let pend = {};                          // key -> {name?: raw text, order?: raw text}; absent = unchanged
let serverErr = {};                     // key -> message from the last rejected save
let status = {};                        // key -> status text from /api/status
let slots = [], pg = 0, seq = 0, busy = false;

function previewUrl(i){ return '/stream/' + i + (q ? q + '&' : '?') + 'fps=' + PREVIEW_FPS; }
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
const SVGNS = 'http://www.w3.org/2000/svg';
function icon(name){
  const s = document.createElementNS(SVGNS, 'svg'), u = document.createElementNS(SVGNS, 'use');
  s.setAttribute('class', 'ic'); s.setAttribute('aria-hidden', 'true');
  u.setAttribute('href', '#i-' + name); s.appendChild(u);
  return s;
}

// ── pending edits (same rules as the server) ────────────────────────────────
const norm = v => String(v || '').split(/\s+/).filter(Boolean).join(' ');
const savedName = k => byKey[k].customName || null;
const savedOrder = k => byKey[k].customOrder == null ? null : byKey[k].customOrder;
const nameRaw = k => (pend[k] && 'name' in pend[k]) ? pend[k].name : (savedName(k) || '');
const orderRaw = k => (pend[k] && 'order' in pend[k]) ? pend[k].order : (savedOrder(k) == null ? '' : String(savedOrder(k)));
function pendName(k){ const v = norm(nameRaw(k)); return (!v || v === byKey[k].technicalName) ? null : v; }
function pendOrder(k){ const v = String(orderRaw(k)).trim(); if (v === '') return null; const n = Number(v); return Number.isInteger(n) ? n : NaN; }
const label = k => pendName(k) || byKey[k].technicalName;
const sameOrder = (a, b) => a === b;                       // NaN !== NaN -> counts as a change
const savedAudio = k => byKey[k].audio || 'auto';
const audioRaw = k => (pend[k] && 'audio' in pend[k]) ? pend[k].audio : savedAudio(k);
const isDirty = k => pendName(k) !== savedName(k) || !sameOrder(pendOrder(k), savedOrder(k)) || audioRaw(k) !== savedAudio(k);
const dirtyKeys = () => cams.map(c => c.key).filter(isDirty);
function setPend(k, field, v){ (pend[k] = pend[k] || {})[field] = v; delete serverErr[k]; }
function duplicates(){
  const m = new Map();
  for (const c of cams){ const o = pendOrder(c.key); if (Number.isInteger(o)){ if (!m.has(o)) m.set(o, []); m.get(o).push(c.key); } }
  for (const [o, ks] of [...m]) if (ks.length < 2) m.delete(o);
  return m;
}
// A custom order is a pinned POSITION; every other camera fills the remaining
// positions in its original order (identical to camera_settings.py).
function orderedKeys(){
  const n = cams.length, pinned = new Map();
  for (const c of cams){ const o = pendOrder(c.key); if (Number.isInteger(o) && o >= 1 && o <= n && !pinned.has(o)) pinned.set(o, c.key); }
  const used = new Set(pinned.values()), autos = cams.filter(c => !used.has(c.key)).map(c => c.key);
  const out = []; let a = 0;
  for (let p = 1; p <= n; p++) out.push(pinned.has(p) ? pinned.get(p) : autos[a++]);
  return out;
}
function nameErr(k){
  const v = norm(nameRaw(k));
  if (v.length > S.limits.nameMax) return 'Name is too long (max ' + S.limits.nameMax + ' characters).';
  if (/[<>\u0000-\u001f\u007f]/.test(v)) return 'Name may not contain < or >.';
  return '';
}
function orderErr(k, dups){
  const o = pendOrder(k);
  if (o === null) return '';
  if (!Number.isInteger(o) || o < 1 || o > cams.length) return 'Position must be a whole number from 1 to ' + cams.length + '.';
  const others = (dups.get(o) || []).filter(x => x !== k);
  return others.length ? 'Position ' + o + ' is also set for ' + others.map(x => '"' + label(x) + '"').join(', ') + '.' : '';
}
function pinAll(keys){ keys.forEach((k, i) => setPend(k, 'order', String(i + 1))); }
function move(k, d, refocus){                          // swap with the neighbour in grid order
  const list = orderedKeys(), i = list.indexOf(k), j = i + d;
  if (j < 0 || j >= list.length) return;
  [list[i], list[j]] = [list[j], list[i]];
  setPend(list[i], 'order', String(i + 1)); setPend(list[j], 'order', String(j + 1));
  renderAll();
  if (refocus){                                        // list rows are rebuilt: keep keyboard focus on the moved row
    const li = $('#orderList li[data-key="' + CSS.escape(k) + '"]'), b = li && $(d < 0 ? '.up' : '.down', li);
    if (li) li.scrollIntoView({block: 'nearest'});
    if (b && !b.disabled) b.focus(); else if (li) li.focus();
  }
}
function moveTo(k, pos){                               // insert at pos, shift the others
  const list = orderedKeys().filter(x => x !== k);
  list.splice(Math.max(0, Math.min(list.length, pos - 1)), 0, k);
  pinAll(list); renderAll();
}

// ── layout: cards + order list side by side, or tabs on narrow screens ──────
const narrowMQ = window.matchMedia('(max-width: 1099px)');
const camsVisible = () => !(narrowMQ.matches && document.body.dataset.tab === 'order');
function setTab(t){
  document.body.dataset.tab = t;
  $$('.tabs [role=tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === t)));
  layoutChanged();
}
// hidden previews would keep browser connections and NVR slots busy for nothing
function layoutChanged(){ if (camsVisible()) showCards(); else { stopPreviews(); markInView(); } }
if (narrowMQ.addEventListener) narrowMQ.addEventListener('change', layoutChanged); else narrowMQ.addListener(layoutChanged);

// ── card pager: 4 fixed slots; a slot's stream only changes when its camera changes ──
function matches(c, t){
  return [label(c.key), c.displayName, c.technicalName, c.nvrLabel, 'ch' + c.channel, 'ch ' + c.channel,
          'channel ' + c.channel, String(c.channel), c.key].some(s => String(s).toLowerCase().includes(t));
}
function cardList(){                                   // SAVED order: cards don't jump while you type
  const t = $('#search').value.trim().toLowerCase();
  const list = cams.slice().sort((a, b) => a.displayOrder - b.displayOrder);
  return t ? list.filter(c => matches(c, t)) : list;
}
function buildSlots(){
  const host = $('#cards'), tpl = $('#cardTpl');
  for (let n = 0; n < PER; n++){
    const e = tpl.content.firstElementChild.cloneNode(true);
    const s = {el: e, img: $('img', e), pv: $('.pv', e), badge: $('.badge', e), chip: $('.poschip', e),
               dname: $('.dname', e), tech: $('.tech', e), unsaved: $('.unsaved', e), count: $('.count', e),
               iname: $('.iname', e), iorder: $('.iorder', e), where: $('.where', e), err: $('.err', e),
               iaudio: $('.iaudio', e), adet: $('.adet', e),
               key: null, idx: null, live: null};
    s.iname.id = 'name' + n; $('.lname', e).htmlFor = s.iname.id;
    s.iorder.id = 'order' + n; $('.lorder', e).htmlFor = s.iorder.id;
    s.iaudio.id = 'audio' + n; $('.laudio', e).htmlFor = s.iaudio.id;
    s.iaudio.addEventListener('change', () => { if (s.key){ setPend(s.key, 'audio', s.iaudio.value); renderAll(); } });
    s.img.onerror = () => { const i = s.idx; setTimeout(() => {
      if (s.idx === i && i != null && s.live === i){ s.img.src = previewUrl(i) + '&_r=' + Date.now(); } }, 2000); };
    s.img.onload = () => { if (s.img.getAttribute('src')) s.pv.classList.add('ready'); };
    s.iname.addEventListener('input', () => { if (s.key){ setPend(s.key, 'name', s.iname.value); renderAll(); } });
    s.iorder.addEventListener('input', () => { if (s.key){ setPend(s.key, 'order', s.iorder.value); renderAll(); } });
    const enterSaves = (ev) => { if (ev.key === 'Enter' && s.key){ ev.preventDefault(); save([s.key]); } };
    s.iname.addEventListener('keydown', enterSaves);
    s.iorder.addEventListener('keydown', enterSaves);
    $('.up', e).onclick = () => s.key && move(s.key, -1);
    $('.down', e).onclick = () => s.key && move(s.key, +1);
    $('.rname', e).onclick = () => { if (s.key){ setPend(s.key, 'name', ''); renderAll(); } };
    $('.rorder', e).onclick = () => { if (s.key){ setPend(s.key, 'order', ''); renderAll(); } };
    $('.save', e).onclick = () => s.key && save([s.key]);
    e.hidden = true;
    host.appendChild(e);
    slots.push(s);
  }
}
function showCards(){
  if (!S) return;
  const my = ++seq, list = cardList(), pages = Math.max(1, Math.ceil(list.length / PER));
  pg = Math.max(0, Math.min(pg, pages - 1));
  const shown = list.slice(pg * PER, pg * PER + PER);
  $('#pinfo').textContent = list.length
    ? 'Cameras ' + (pg * PER + 1) + '–' + (pg * PER + shown.length) + ' of ' + list.length + (list.length < cams.length ? ' (filtered)' : '')
    : 'No matching cameras';
  $$('.pgnum').forEach(x => { x.textContent = list.length ? 'Page ' + (pg + 1) + ' of ' + pages : ''; });
  $$('.pprev').forEach(b => { b.disabled = pg === 0; });
  $$('.pnext').forEach(b => { b.disabled = pg >= pages - 1; });
  $('.pager.bottom').hidden = pages < 2;
  $('#empty').hidden = list.length > 0;
  const start = [], visible = camsVisible();
  slots.forEach((s, n) => {
    const c = shown[n];
    if (!c){ s.key = s.idx = s.live = null; s.img.removeAttribute('src'); s.pv.classList.remove('ready'); s.el.hidden = true; return; }
    s.el.hidden = false;
    if (s.key !== c.key){ s.img.removeAttribute('src'); s.pv.classList.remove('ready'); s.key = c.key; s.idx = c.index; s.live = null; }
    if (visible && s.live !== s.idx) start.push(s);
  });
  renderCards();
  markInView();
  // old previews were aborted above; open the new ones a moment later so the
  // browser has released their connections (same hand-off as the CCTV grid)
  if (start.length) setTimeout(() => { if (my === seq && camsVisible()) start.forEach(s => {
    if (s.idx != null && s.live !== s.idx){ s.img.src = previewUrl(s.idx); s.live = s.idx; } }); }, 300);
}
function stopPreviews(){ slots.forEach(s => { s.img.removeAttribute('src'); s.pv.classList.remove('ready'); s.live = null; }); }
function jumpTo(k){
  if (document.body.dataset.tab !== 'cams') setTab('cams');
  let list = cardList(), i = list.findIndex(c => c.key === k);
  if (i < 0){ $('#search').value = ''; list = cardList(); i = list.findIndex(c => c.key === k); renderOrder(); }
  pg = Math.floor(i / PER); showCards();
  const s = slots.find(x => x.key === k);
  if (s){ s.el.classList.add('flash'); s.el.scrollIntoView({block: 'nearest', behavior: 'smooth'});
          setTimeout(() => s.el.classList.remove('flash'), 1400); }
}
function topOfCards(btn){
  if (btn.closest('.bottom')) $('#camPane').scrollIntoView({block: 'start', behavior: 'smooth'});
}

// ── rendering ────────────────────────────────────────────────────────────────
const prettyStatus = st => st === 'LIVE' ? 'Live' : st.replace(/\.\.\.$/, '…');
const statusKind = st => st === 'LIVE' ? 'live' : /offline|unreachable|failed/i.test(st) ? 'bad' : st && st !== 'Idle' ? 'wait' : st ? 'idle' : '';
function renderCards(){
  const dups = duplicates(), order = orderedKeys();
  for (const s of slots){
    if (!s.key) continue;
    const k = s.key, c = byKey[k], pos = order.indexOf(k) + 1;
    s.dname.textContent = label(k);
    s.tech.textContent = c.technicalName + '  ·  ' + c.nvrLabel + '  ·  CH ' + c.channel;
    if (document.activeElement !== s.iname) s.iname.value = nameRaw(k);
    s.iname.placeholder = c.technicalName;
    if (document.activeElement !== s.iorder) s.iorder.value = orderRaw(k);
    s.iorder.placeholder = 'auto (' + pos + ')';
    s.iorder.max = cams.length;
    s.chip.textContent = String(pos).padStart(2, '0');
    s.where.textContent = 'Live grid page ' + Math.ceil(pos / GRID_PER) + ', tile ' + ((pos - 1) % GRID_PER + 1) +
                          (pendOrder(k) === null ? ' (automatic)' : '');
    s.count.textContent = norm(nameRaw(k)).length + ' / ' + S.limits.nameMax;
    if (document.activeElement !== s.iaudio) s.iaudio.value = audioRaw(k);
    const det = c.audioDetected || {};
    s.adet.textContent = det.state === 'available' ? 'detected: ' + (det.codec || 'audio') + (det.rate ? ' ' + det.rate / 1000 + ' kHz' : '')
                       : det.state === 'unavailable' ? 'detected: no audio track' : 'not checked yet';
    s.adet.className = 'adet' + (det.state === 'available' ? ' ok' : '');
    const ne = nameErr(k), oe = orderErr(k, dups), se = serverErr[k] || '';
    s.iname.classList.toggle('bad', !!ne); s.iorder.classList.toggle('bad', !!oe);
    s.iname.setAttribute('aria-invalid', String(!!ne)); s.iorder.setAttribute('aria-invalid', String(!!oe));
    s.err.textContent = '';
    [ne, oe, se].filter(Boolean).forEach(m => s.err.appendChild(el('div', '', m)));
    const o = pendOrder(k);
    if (oe && dups.has(o)){
      const b = el('button', 'sm', 'Move "' + label(k) + '" to position ' + o + ' and shift the others');
      b.onclick = () => moveTo(k, o); s.err.appendChild(b);
    }
    const dirty = isDirty(k);
    s.el.classList.toggle('dirty', dirty);
    s.unsaved.hidden = !dirty;
    $('.save', s.el).disabled = busy || !dirty || !!ne || !!oe;
    $('.rname', s.el).disabled = pendName(k) === null && nameRaw(k) === '';
    $('.rorder', s.el).disabled = pendOrder(k) === null;
    $('.up', s.el).disabled = pos <= 1;
    $('.down', s.el).disabled = pos >= cams.length;
    const st = status[k] || '';
    s.badge.textContent = st ? prettyStatus(st) : '';
    s.badge.className = 'badge ' + statusKind(st);
  }
}
function markInView(){
  const shown = new Set(camsVisible() ? slots.filter(s => s.key).map(s => s.key) : []);
  $$('#orderList li.row').forEach(li => li.classList.toggle('inview', shown.has(li.dataset.key)));
}
let dragKey = null;
function renderOrder(){
  const ol = $('#orderList'), t = $('#search').value.trim().toLowerCase(), keys = orderedKeys();
  ol.textContent = '';
  keys.forEach((k, i) => {
    if (i % GRID_PER === 0) ol.appendChild(el('li', 'sep', 'Live grid · page ' + (i / GRID_PER + 1)));
    const c = byKey[k], li = el('li', 'row');
    li.draggable = true; li.dataset.key = k; li.tabIndex = -1;
    li.classList.toggle('pend', isDirty(k));
    li.classList.toggle('match', !!t && matches(c, t));
    li.title = 'Show ' + label(k);
    const nm = el('span', 'nm');
    nm.append(el('span', 't', label(k)),
              el('span', 'tn', label(k) !== c.technicalName ? c.technicalName + ' · CH ' + c.channel : c.nvrLabel + ' · CH ' + c.channel));
    const grip = el('span', 'grip'); grip.appendChild(icon('grip'));
    const up = el('button', 'up icon sm ghost'), dn = el('button', 'down icon sm ghost');
    up.appendChild(icon('up')); dn.appendChild(icon('down'));
    up.title = 'Earlier'; dn.title = 'Later';
    up.setAttribute('aria-label', 'Move ' + label(k) + ' earlier'); dn.setAttribute('aria-label', 'Move ' + label(k) + ' later');
    up.disabled = i === 0; dn.disabled = i === keys.length - 1;
    up.onclick = (e) => { e.stopPropagation(); move(k, -1, true); };
    dn.onclick = (e) => { e.stopPropagation(); move(k, +1, true); };
    li.append(grip, el('span', 'pos', String(i + 1)), nm);
    if (isDirty(k)){ const d = el('span', 'dot'); d.title = 'Unsaved change'; li.append(d); }
    li.append(up, dn);
    li.onclick = () => jumpTo(k);
    li.addEventListener('dragstart', (e) => { dragKey = k; li.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', k); });
    li.addEventListener('dragend', () => { dragKey = null; renderOrder(); });
    li.addEventListener('dragover', (e) => {
      if (!dragKey || dragKey === k) return;
      e.preventDefault();
      const r = li.getBoundingClientRect(), top = e.clientY < r.top + r.height / 2;
      li.classList.toggle('over-top', top); li.classList.toggle('over-bot', !top);
    });
    li.addEventListener('dragleave', () => li.classList.remove('over-top', 'over-bot'));
    li.addEventListener('drop', (e) => {
      e.preventDefault();
      if (!dragKey || dragKey === k) return;
      const r = li.getBoundingClientRect(), top = e.clientY < r.top + r.height / 2;
      const list = orderedKeys().filter(x => x !== dragKey);
      list.splice(list.indexOf(k) + (top ? 0 : 1), 0, dragKey);
      pinAll(list); dragKey = null; renderAll();
    });
    ol.appendChild(li);
  });
  markInView();
}
function renderAll(){
  if (!S) return;
  renderCards(); renderOrder();
  const n = dirtyKeys().length;
  const dirtyEl = $('#dirty');                          // "3 unsaved changes" ("3 unsaved" on phones)
  dirtyEl.textContent = n ? n + ' unsaved' : 'Saving…';
  if (n) dirtyEl.appendChild(el('span', 'hide-sm', ' change' + (n > 1 ? 's' : '')));
  $('#saveAll').disabled = busy || n === 0;
  $('#saveAll').textContent = busy ? 'Saving…' : 'Save All Changes';
  $('#discard').disabled = busy || n === 0;
  $('#savebar').hidden = n === 0 && !busy;
  document.body.classList.toggle('has-savebar', !$('#savebar').hidden);
  const renamed = cams.filter(c => c.customName).length, pinned = cams.filter(c => c.customOrder != null).length;
  $('#summary').textContent = cams.length + ' cameras · ' + renamed + ' renamed · ' + pinned + ' with a fixed position';
}
function say(text, kind, reload){
  const m = $('#msg');
  m.hidden = false; m.className = 'toast ' + (kind || ''); m.textContent = '';
  m.append(icon(kind === 'ok' ? 'check' : kind === 'bad' ? 'alert' : 'info'), el('span', 'tx', text));
  if (reload){ const b = el('button', 'sm', 'Reload'); b.onclick = () => location.reload(); m.appendChild(b); }
  clearTimeout(say.t); if (kind === 'ok') say.t = setTimeout(() => { m.hidden = true; }, 4000);
}
function applySnapshot(d){
  S = d; cams = d.cameras; byKey = {}; cams.forEach(c => { byKey[c.key] = c; });
  const w = $('#warn');
  w.hidden = !(d.warnings && d.warnings.length);
  $('#warnText').textContent = w.hidden ? '' : d.warnings.join(' | ');
}

// ── saving ───────────────────────────────────────────────────────────────────
async function put(changes){
  const r = await fetch('/api/camera-settings' + q, {method: 'PUT', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({baseRevision: S.revision, cameras: changes})});
  let d = {}; try { d = await r.json(); } catch (e) {}
  return {r, d};
}
function afterSave(d){
  applySnapshot(d);
  try { localStorage.setItem('cctv-camera-settings-rev', d.revision + ':' + Date.now()); } catch (e) {}  // tells open CCTV tabs
  showCards(); renderAll();
}
async function save(keys){
  keys = keys.filter(isDirty);
  if (!keys.length || busy) return;
  const dups = duplicates(), bad = keys.filter(k => nameErr(k) || orderErr(k, dups));
  if (bad.length){ say('Please fix the highlighted fields first (' + bad.length + ' camera' + (bad.length > 1 ? 's' : '') + ').', 'bad'); jumpTo(bad[0]); return; }
  const changes = {};
  for (const k of keys){
    const ch = {};
    if (pendName(k) !== savedName(k)) ch.displayName = pendName(k);
    if (!sameOrder(pendOrder(k), savedOrder(k))) ch.displayOrder = pendOrder(k);
    if (audioRaw(k) !== savedAudio(k)) ch.audio = audioRaw(k);
    changes[k] = ch;
  }
  busy = true; renderAll();
  try {
    const {r, d} = await put(changes);
    if (r.ok && d.ok){
      keys.forEach(k => { delete pend[k]; delete serverErr[k]; });
      afterSave(d);
      say('Saved ' + keys.length + ' camera' + (keys.length > 1 ? 's' : '') + '. The live view uses the new settings.', 'ok');
    } else if (r.status === 409){
      say((d.errors && d.errors[0] && d.errors[0].message) || 'Settings were changed elsewhere.', 'bad', true);
    } else {
      (d.errors || []).forEach(e => { if (e.key) serverErr[e.key] = e.message; });
      const first = (d.errors || [])[0];
      say('Not saved: ' + (first ? first.message : 'the server rejected the change (HTTP ' + r.status + ').'), 'bad');
      if (first && first.key && byKey[first.key]) jumpTo(first.key);
    }
  } catch (e){ say('Could not reach the CCTV server: ' + e.message, 'bad'); }
  finally { busy = false; renderAll(); }
}
async function resetAllOrdering(){
  if (busy || !confirm('Reset the order of ALL cameras to the original order?\n\nThis is saved immediately. Camera names are not changed.')) return;
  const changes = {}; cams.forEach(c => { changes[c.key] = {displayOrder: null}; });
  busy = true; renderAll();
  try {
    const {r, d} = await put(changes);
    if (r.ok && d.ok){
      cams.forEach(c => { if (pend[c.key]) delete pend[c.key].order; });
      afterSave(d); say('Ordering reset to the original order.', 'ok');
    } else say('Not reset: ' + (((d.errors || [])[0] || {}).message || 'HTTP ' + r.status), 'bad', r.status === 409);
  } catch (e){ say('Could not reach the CCTV server: ' + e.message, 'bad'); }
  finally { busy = false; renderAll(); }
}
function discardAll(){
  const n = dirtyKeys().length;
  if (!n || busy || !confirm('Discard ' + n + ' unsaved change' + (n > 1 ? 's' : '') + '?')) return;
  pend = {}; serverErr = {};
  renderAll();
  say('Unsaved changes discarded.', 'ok');
}

// ── status of the visible cameras (reuses /api/status; a connection is free for it) ──
async function pollStatus(){
  if (document.hidden || !camsVisible() || !slots.some(s => s.key)) return;
  try {
    const d = await (await fetch('/api/status' + q)).json();
    status = {}; d.cameras.forEach(c => { status[c.key] = c.status; });
    renderCards();
  } catch (e) {}
}

// ── wiring ───────────────────────────────────────────────────────────────────
$('#msg').addEventListener('click', (e) => { if (!e.target.closest('button')) $('#msg').hidden = true; });
$('#saveAll').onclick = () => save(dirtyKeys());
$('#discard').onclick = discardAll;
$('#resetOrder').onclick = resetAllOrdering;
$$('.pprev').forEach(b => { b.onclick = () => { pg--; showCards(); topOfCards(b); }; });
$$('.pnext').forEach(b => { b.onclick = () => { pg++; showCards(); topOfCards(b); }; });
$$('.tabs [role=tab]').forEach(b => { b.onclick = () => setTab(b.dataset.tab); });
let searchT = 0;
$('#search').addEventListener('input', () => { clearTimeout(searchT); searchT = setTimeout(() => { pg = 0; showCards(); renderOrder(); }, 250); });
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's'){ e.preventDefault(); save(dirtyKeys()); }
  else if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)){ e.preventDefault(); $('#search').focus(); }
});
// Unsaved edits: ask before leaving. Previews are closed when the page really goes
// away (pagehide); with only 4 previews open the browser always has a free
// connection for the next page, so a reload can't get stuck behind them.
window.addEventListener('beforeunload', (e) => { if (dirtyKeys().length){ e.preventDefault(); e.returnValue = ''; } else stopPreviews(); });
window.addEventListener('pagehide', stopPreviews);
// back/forward cache: the page can come back without a reload after pagehide stopped the previews
window.addEventListener('pageshow', (e) => { if (e.persisted) showCards(); });
// "ready" = the preview's first image has arrived (hides the spinner)
setInterval(() => slots.forEach(s => s.pv.classList.toggle('ready', !!s.img.getAttribute('src') && s.img.naturalWidth > 0)), 300);

buildSlots();
fetch('/api/camera-settings' + q).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
  .then(d => { applySnapshot(d); $('#loading').hidden = true; showCards(); renderAll(); setTimeout(pollStatus, 1500); setInterval(pollStatus, 3000); })
  .catch(e => { $('#loading').hidden = true; say('Could not load camera settings: ' + e.message, 'bad', true); });
</script>
</body>
</html>
""")
