"""Camera Settings page (served at /settings): rename and re-order cameras while
watching their live preview. Data comes from GET /api/camera-settings and is saved
with PUT /api/camera-settings; see camera_settings.py for the storage/order model.

Preview streams reuse the normal /stream/<index> worker at a low send rate
(?fps=2) -- no extra RTSP connection. Only 4 previews are shown per page: a browser
allows ~6 connections per server and each MJPEG preview holds one for its whole
life, so 4 leaves room for the Save and status requests (with 6, Save would hang).
All user-entered names are rendered with textContent, never as HTML.
"""

SETTINGS_PAGE = r"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>CCTV &middot; Camera Settings</title>
<style>
 :root{--bg:#14141c;--panel:#1a1a23;--card:#1e1e28;--line:#333;--line2:#4a4a58;--txt:#ddd;
       --mute:#8a8a9a;--acc:#0c8;--warn:#e0a030;--bad:#e05a5a}
 body{background:var(--bg);color:var(--txt);font-family:system-ui,sans-serif;margin:0;padding:10px}
 #top{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
 #top b{font-size:16px}
 .grow{flex:1}
 .hint{color:var(--mute);font-size:12px}
 button,a.btn{background:#333;color:var(--txt);border:1px solid #555;border-radius:4px;padding:6px 12px;
              cursor:pointer;font:inherit;font-size:13px;text-decoration:none;white-space:nowrap}
 button:hover:not(:disabled),a.btn:hover{border-color:var(--acc)}
 button:disabled{opacity:.4;cursor:default}
 button.primary{background:#0a6b4d;border-color:var(--acc);color:#fff}
 input{background:#111118;color:var(--txt);border:1px solid var(--line2);border-radius:4px;padding:7px 8px;
       font:inherit;font-size:14px;min-width:0}
 input:focus{outline:none;border-color:var(--acc)}
 input.bad{border-color:var(--bad)}
 #search{width:240px;max-width:100%}
 /* floating toast: never shifts the page (a banner that pushes content down made
    a click land on the wrong input during testing); click it to dismiss */
 #msg{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:20;max-width:min(720px,92vw);
      padding:10px 14px;border-radius:6px;border:1px solid var(--line);background:#23232e;font-size:13px;
      box-shadow:0 6px 24px rgba(0,0,0,.55);cursor:pointer}
 #msg.ok{border-color:var(--acc)} #msg.bad{border-color:var(--bad);color:#f3b0b0}
 #warn{margin:0 0 10px;padding:8px 12px;border-radius:5px;border:1px solid var(--warn);color:#f0d49a;font-size:12px}
 #layout{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:12px;align-items:start}
 @media(max-width:980px){#layout{grid-template-columns:1fr}}
 .pager{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
 #cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
 @media(max-width:640px){#cards{grid-template-columns:1fr}}
 .card{background:var(--card);border:1px solid var(--line);border-radius:6px;overflow:hidden}
 .card.dirty{border-color:var(--warn)}
 .card.flash{box-shadow:0 0 0 2px var(--acc)}
 .pv{position:relative;background:#000;aspect-ratio:16/9}
 .pv img{width:100%;height:100%;object-fit:cover;display:block}
 .badge{position:absolute;top:6px;left:6px;font-size:11px;padding:2px 8px;border-radius:10px;background:rgba(0,0,0,.72)}
 .badge:empty{display:none}
 .badge.live{color:#4ddb87} .badge.wait{color:var(--warn)} .badge.bad{color:var(--bad)}
 .body{padding:10px 12px 12px;display:flex;flex-direction:column;gap:8px}
 .dname{font-size:17px;font-weight:600;overflow-wrap:anywhere}
 .tech{font-size:12px;color:var(--mute);margin-top:-5px}
 label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--mute)}
 .row{display:flex;gap:6px;align-items:flex-end}
 .row label{flex:1}
 .err{color:#f3a0a0;font-size:12px;display:flex;flex-direction:column;gap:4px;align-items:flex-start}
 .err:empty{display:none}
 .err button{font-size:12px;padding:4px 8px}
 .actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
 #orderPane{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px;
            position:sticky;top:10px;max-height:calc(100vh - 22px);overflow:auto}
 .ohead{display:flex;align-items:center;gap:8px;margin-bottom:4px}
 #orderList{list-style:none;margin:8px 0 0;padding:0}
 #orderList li{display:flex;align-items:center;gap:6px;padding:4px 6px;margin-bottom:3px;border-radius:4px;
               background:#20202b;border:1px solid transparent;font-size:13px;cursor:grab}
 #orderList li.sep{background:none;cursor:default;color:var(--mute);font-size:11px;padding:6px 2px 2px;
                   text-transform:uppercase;letter-spacing:.05em}
 #orderList li.match{border-color:#556}
 #orderList li.pend{box-shadow:inset 3px 0 0 var(--warn)}
 #orderList li.over-top{border-top:2px solid var(--acc)} #orderList li.over-bot{border-bottom:2px solid var(--acc)}
 #orderList li.dragging{opacity:.4}
 #orderList .h{color:#666;cursor:grab}
 #orderList .pos{width:20px;text-align:right;color:var(--mute);font-variant-numeric:tabular-nums}
 #orderList .nm{flex:1;min-width:0;display:flex;flex-direction:column}
 #orderList .nm span{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
 #orderList .nm .tn{color:var(--mute);font-size:11px}
 #orderList button{padding:1px 7px;font-size:11px}
</style>
<div id=top>
  <a class=btn id=back href="/">&larr; Back to CCTV</a>
  <b>Camera Settings</b>
  <input id=search type=search placeholder="Search name, NVR, channel&hellip;" autocomplete=off>
  <span class=grow></span>
  <span id=dirty class=hint></span>
  <button id=saveAll class=primary disabled title="Ctrl+S">Save All Changes</button>
</div>
<div id=msg hidden></div>
<div id=warn hidden></div>
<div id=layout>
  <section>
    <div class=pager>
      <button id=prev>&larr; Prev</button><b id=pinfo>Loading&hellip;</b><button id=next>Next &rarr;</button>
      <span class=hint>Live preview, 4 cameras per page (low frame rate). Leave a name empty to use the technical name.</span>
    </div>
    <div id=cards></div>
  </section>
  <aside id=orderPane>
    <div class=ohead><b>Grid order</b><span class=grow></span><button id=resetOrder title="Back to the original order">Reset all ordering</button></div>
    <div class=hint>Drag a row, or use &#9650; &#9660;, to move a camera. The CCTV grid shows cameras in this order, 6 per page.</div>
    <ol id=orderList></ol>
  </aside>
</div>
<template id=cardTpl>
  <article class=card>
    <div class=pv><img fetchpriority=high alt=""><span class=badge></span></div>
    <div class=body>
      <div class=dname></div>
      <div class=tech></div>
      <label>Display name <input class=iname maxlength=60 autocomplete=off spellcheck=false></label>
      <div class=row>
        <label>Display order <input class=iorder type=number min=1 step=1 inputmode=numeric></label>
        <button class=up title="Move one place earlier">&#9650;</button>
        <button class=down title="Move one place later">&#9660;</button>
      </div>
      <div class=err></div>
      <div class=actions>
        <button class=rname title="Use the technical name again">Reset name</button>
        <button class=rorder title="Automatic position (original order)">Reset order</button>
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
document.getElementById('back').href = '/' + q;

let S = null, cams = [], byKey = {};   // last server snapshot (cameras in technical order)
let pend = {};                          // key -> {name?: raw text, order?: raw text}; absent = unchanged
let serverErr = {};                     // key -> message from the last rejected save
let status = {};                        // key -> status text from /api/status
let slots = [], pg = 0, seq = 0, busy = false;

function previewUrl(i){ return '/stream/' + i + (q ? q + '&' : '?') + 'fps=' + PREVIEW_FPS; }
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

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
const isDirty = k => pendName(k) !== savedName(k) || !sameOrder(pendOrder(k), savedOrder(k));
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
  if (!Number.isInteger(o) || o < 1 || o > cams.length) return 'Order must be a whole number from 1 to ' + cams.length + '.';
  const others = (dups.get(o) || []).filter(x => x !== k);
  return others.length ? 'Order ' + o + ' is also set for ' + others.map(x => '"' + label(x) + '"').join(', ') + '.' : '';
}
function pinAll(keys){ keys.forEach((k, i) => setPend(k, 'order', String(i + 1))); }
function move(k, d){                                   // swap with the neighbour in grid order
  const list = orderedKeys(), i = list.indexOf(k), j = i + d;
  if (j < 0 || j >= list.length) return;
  [list[i], list[j]] = [list[j], list[i]];
  setPend(list[i], 'order', String(i + 1)); setPend(list[j], 'order', String(j + 1));
  renderAll();
}
function moveTo(k, pos){                               // insert at pos, shift the others
  const list = orderedKeys().filter(x => x !== k);
  list.splice(Math.max(0, Math.min(list.length, pos - 1)), 0, k);
  pinAll(list); renderAll();
}

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
    const s = {el: e, img: $('img', e), badge: $('.badge', e), dname: $('.dname', e), tech: $('.tech', e),
               iname: $('.iname', e), iorder: $('.iorder', e), err: $('.err', e), key: null, idx: null, live: null};
    s.img.onerror = () => { const i = s.idx; setTimeout(() => {
      if (s.idx === i && i != null){ s.img.src = previewUrl(i) + '&_r=' + Date.now(); } }, 2000); };
    s.iname.addEventListener('input', () => { if (s.key){ setPend(s.key, 'name', s.iname.value); renderAll(); } });
    s.iorder.addEventListener('input', () => { if (s.key){ setPend(s.key, 'order', s.iorder.value); renderAll(); } });
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
  const my = ++seq, list = cardList(), pages = Math.max(1, Math.ceil(list.length / PER));
  pg = Math.max(0, Math.min(pg, pages - 1));
  const shown = list.slice(pg * PER, pg * PER + PER);
  $('#pinfo').textContent = list.length
    ? 'Cameras ' + (pg * PER + 1) + '–' + (pg * PER + shown.length) + ' of ' + list.length +
      (list.length < cams.length ? ' (filtered)' : '') + '  ·  page ' + (pg + 1) + '/' + pages
    : 'No camera matches the search';
  $('#prev').disabled = pg === 0; $('#next').disabled = pg >= pages - 1;
  const start = [];
  slots.forEach((s, n) => {
    const c = shown[n];
    if (!c){ s.key = s.idx = s.live = null; s.img.removeAttribute('src'); s.el.hidden = true; return; }
    s.el.hidden = false;
    if (s.key !== c.key){ s.img.removeAttribute('src'); s.key = c.key; s.idx = c.index; s.live = null; }
    if (s.live !== s.idx) start.push(s);
  });
  renderCards();
  // old previews were aborted above; open the new ones a moment later so the
  // browser has released their connections (same hand-off as the CCTV grid)
  if (start.length) setTimeout(() => { if (my === seq) start.forEach(s => {
    if (s.idx != null && s.live !== s.idx){ s.img.src = previewUrl(s.idx); s.live = s.idx; } }); }, 300);
}
function stopPreviews(){ slots.forEach(s => { s.img.removeAttribute('src'); s.live = null; }); }
function jumpTo(k){
  let list = cardList(), i = list.findIndex(c => c.key === k);
  if (i < 0){ $('#search').value = ''; list = cardList(); i = list.findIndex(c => c.key === k); }
  pg = Math.floor(i / PER); showCards();
  const s = slots.find(x => x.key === k);
  if (s){ s.el.classList.add('flash'); s.el.scrollIntoView({block: 'nearest', behavior: 'smooth'});
          setTimeout(() => s.el.classList.remove('flash'), 1200); }
}

// ── rendering ────────────────────────────────────────────────────────────────
function renderCards(){
  const dups = duplicates(), order = orderedKeys();
  for (const s of slots){
    if (!s.key) continue;
    const k = s.key, c = byKey[k];
    s.dname.textContent = label(k);
    s.tech.textContent = c.technicalName + '  ·  ' + c.nvrLabel + '  ·  CH ' + c.channel;
    if (document.activeElement !== s.iname) s.iname.value = nameRaw(k);
    s.iname.placeholder = c.technicalName;
    if (document.activeElement !== s.iorder) s.iorder.value = orderRaw(k);
    s.iorder.placeholder = 'auto (' + (order.indexOf(k) + 1) + ')';
    s.iorder.max = cams.length;
    const ne = nameErr(k), oe = orderErr(k, dups), se = serverErr[k] || '';
    s.iname.classList.toggle('bad', !!ne); s.iorder.classList.toggle('bad', !!oe);
    s.err.textContent = '';
    [ne, oe, se].filter(Boolean).forEach(m => s.err.appendChild(el('div', '', m)));
    const o = pendOrder(k);
    if (oe && dups.has(o)){
      const b = el('button', '', 'Move "' + label(k) + '" to position ' + o + ' and shift the others');
      b.onclick = () => moveTo(k, o); s.err.appendChild(b);
    }
    s.el.classList.toggle('dirty', isDirty(k));
    $('.save', s.el).disabled = busy || !isDirty(k) || !!ne || !!oe;
    $('.rname', s.el).disabled = pendName(k) === null && nameRaw(k) === '';
    $('.rorder', s.el).disabled = pendOrder(k) === null;
    const st = status[k] || '';
    s.badge.textContent = st;
    s.badge.className = 'badge ' + (st === 'LIVE' ? 'live' : /offline|unreachable|failed/i.test(st) ? 'bad' : st && st !== 'Idle' ? 'wait' : '');
  }
}
let dragKey = null;
function renderOrder(){
  const ol = $('#orderList'), t = $('#search').value.trim().toLowerCase();
  ol.textContent = '';
  orderedKeys().forEach((k, i) => {
    if (i % GRID_PER === 0) ol.appendChild(el('li', 'sep', 'CCTV page ' + (i / GRID_PER + 1)));
    const c = byKey[k], li = el('li');
    li.draggable = true; li.dataset.key = k;
    li.className = (isDirty(k) ? 'pend ' : '') + (t && matches(c, t) ? 'match' : '');
    const nm = el('span', 'nm');
    nm.append(el('span', '', label(k)),
              el('span', 'tn', label(k) !== c.technicalName ? c.technicalName + ' · CH ' + c.channel : c.nvrLabel + ' · CH ' + c.channel));
    const up = el('button', '', '▲'), dn = el('button', '', '▼');
    up.title = 'Earlier'; dn.title = 'Later';
    up.onclick = (e) => { e.stopPropagation(); move(k, -1); };
    dn.onclick = (e) => { e.stopPropagation(); move(k, +1); };
    li.append(el('span', 'h', '☰'), el('span', 'pos', String(i + 1)), nm, up, dn);
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
}
function renderAll(){
  if (!S) return;
  renderCards(); renderOrder();
  const n = dirtyKeys().length;
  $('#dirty').textContent = n ? '● ' + n + ' unsaved change' + (n > 1 ? 's' : '') : '';
  $('#saveAll').disabled = busy || n === 0;
}
function say(text, kind, reload){
  const m = $('#msg'); m.hidden = false; m.className = kind || ''; m.textContent = text;
  if (reload){ const b = el('button', '', 'Reload'); b.style.marginLeft = '10px'; b.onclick = () => location.reload(); m.appendChild(b); }
  clearTimeout(say.t); if (kind === 'ok') say.t = setTimeout(() => { m.hidden = true; }, 4000);
}
function applySnapshot(d){
  S = d; cams = d.cameras; byKey = {}; cams.forEach(c => { byKey[c.key] = c; });
  const w = $('#warn');
  w.hidden = !(d.warnings && d.warnings.length);
  w.textContent = w.hidden ? '' : 'Settings file notes: ' + d.warnings.join(' | ');
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
    changes[k] = ch;
  }
  busy = true; renderAll();
  try {
    const {r, d} = await put(changes);
    if (r.ok && d.ok){
      keys.forEach(k => { delete pend[k]; delete serverErr[k]; });
      afterSave(d);
      say('Saved ' + keys.length + ' camera' + (keys.length > 1 ? 's' : '') + '. The CCTV page shows the new names and order.', 'ok');
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

// ── status of the visible cameras (reuses /api/status; a connection is free for it) ──
async function pollStatus(){
  if (document.hidden || !slots.some(s => s.key)) return;
  try {
    const d = await (await fetch('/api/status' + q)).json();
    status = {}; d.cameras.forEach(c => { status[c.key] = c.status; });
    renderCards();
  } catch (e) {}
}

// ── wiring ───────────────────────────────────────────────────────────────────
$('#msg').addEventListener('click', (e) => { if (e.target.tagName !== 'BUTTON') $('#msg').hidden = true; });
$('#saveAll').onclick = () => save(dirtyKeys());
$('#resetOrder').onclick = resetAllOrdering;
$('#prev').onclick = () => { pg--; showCards(); };
$('#next').onclick = () => { pg++; showCards(); };
let searchT = 0;
$('#search').addEventListener('input', () => { clearTimeout(searchT); searchT = setTimeout(() => { pg = 0; showCards(); renderOrder(); }, 250); });
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's'){ e.preventDefault(); save(dirtyKeys()); }
});
// Unsaved edits: ask before leaving. Previews are closed when the page really goes
// away (pagehide); with only 4 previews open the browser always has a free
// connection for the next page, so a reload can't get stuck behind them.
window.addEventListener('beforeunload', (e) => { if (dirtyKeys().length){ e.preventDefault(); e.returnValue = ''; } else stopPreviews(); });
window.addEventListener('pagehide', stopPreviews);

buildSlots();
fetch('/api/camera-settings' + q).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
  .then(d => { applySnapshot(d); showCards(); renderAll(); setTimeout(pollStatus, 1500); setInterval(pollStatus, 3000); })
  .catch(e => say('Could not load camera settings: ' + e.message, 'bad', true));
</script>
"""
