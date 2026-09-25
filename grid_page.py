"""Live CCTV grid (served at /): 6 cameras per page plus a single-camera view.

Stream lifecycle rules -- the NVR slot / browser-connection fixes depend on them
(see server.py, test_slots.py, test_camera_settings.py):
 * PER fixed <img> cells are created ONCE. A stream is started / stopped only by
   setting / removing an <img>'s src; removing it aborts the MJPEG connection at
   once, which frees the browser connection and the camera's NVR slot.
 * Page change: abort every cell first, wait 500 ms, then open the next page.
 * Single-camera view: stop the other cells first, keep the chosen cell (hand-off).
 * Reload / leave: abort every stream on beforeunload / pagehide.
 * <img fetchpriority=high>: an MJPEG image never "finishes" loading, so the
   browser's low-priority image throttling would hold some streams back forever.
 * The grid never polls the API while streams are open: 6 streams use all of the
   browser's ~6 connections to this server. Tile status (Connecting..., Camera
   offline, ...) is drawn by the server into the stream image itself.
Camera names are always rendered with textContent, never parsed as HTML.
"""
from ui_theme import render

PAGE = render(r"""<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=color-scheme content=dark>
<meta name=theme-color content="#0a0c10">
<meta name=mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black>
<meta name=apple-mobile-web-app-title content="GRAV CCTV">
<title>Live view · GRAV CCTV</title>
<link rel=icon href="{{FAVICON}}">
<style>
/*@theme*/
:root{--pad:16px;--gap:12px;--foot:44px;--cap:40px}
body{display:flex;flex-direction:column;min-height:100vh;min-height:100dvh}
body.viewing{overflow:hidden}

/* ── app bar ─────────────────────────────────────────────── */
.appbar .brand{flex:1 1 0}
.tools{flex:1 1 0;display:flex;align-items:center;justify-content:flex-end;gap:8px}
.pager{display:flex;align-items:center;gap:8px;flex:none}
.pages{display:none;gap:2px;padding:3px;border-radius:12px;background:var(--surface-2);border:1px solid var(--border)}
.pages button{height:30px;min-width:34px;padding:0 10px;border:0;border-radius:9px;background:transparent;
  color:var(--text-2);font-weight:600;font-variant-numeric:tabular-nums}
.pages button:hover:not(:disabled){background:var(--surface-4);color:var(--text)}
.pages button.on,.pages button.on:hover:not(:disabled){background:var(--accent);color:#fff;box-shadow:0 2px 10px rgba(79,140,255,.35)}
.pageinfo{min-width:54px;text-align:center;font-weight:650;font-size:13.5px;font-variant-numeric:tabular-nums}
.clock{display:none;flex-direction:column;align-items:flex-end;line-height:1.2;padding:0 6px;white-space:nowrap;font-variant-numeric:tabular-nums}
.clock .t{font-size:14px;font-weight:650}
.clock .d{font-size:11.5px;color:var(--muted)}
#layoutBtn{display:none}

/* ── camera wall ─────────────────────────────────────────── */
#wall{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;
  padding:var(--pad) max(var(--pad),var(--sar)) var(--pad) max(var(--pad),var(--sal))}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--gap);width:100%;
  /* everything that is not video, vertically (defined here so body.fs can change --foot) */
  --chrome:calc(var(--bar) + var(--sat) + var(--foot) + 2 * var(--pad) + var(--gap) + 2 * var(--cap) + 8px)}
/* tile = unobstructed 16:9 video + caption strip (nothing covers the camera's own
   date/time overlay or the picture) */
.cam{position:relative;display:flex;flex-direction:column;border-radius:var(--r-lg);overflow:hidden;
  background:var(--surface);border:1px solid var(--border);cursor:pointer;-webkit-user-select:none;user-select:none;
  transition:border-color .15s,box-shadow .15s}
.cam .vid{position:relative;aspect-ratio:16/9;overflow:hidden;isolation:isolate;background:#06080b}
.cam img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;opacity:0;transition:opacity .25s}
.cam.ready img{opacity:1}
/* loading shimmer until the first image of the stream has arrived */
.cam .vid::before{content:"";position:absolute;inset:0;z-index:1;pointer-events:none;transform:translateX(-100%);
  background:linear-gradient(100deg,transparent 20%,rgba(255,255,255,.05) 50%,transparent 80%);
  animation:shimmer 1.6s ease-in-out infinite}
.cam.ready .vid::before,.cam.empty .vid::before{display:none}
@keyframes shimmer{to{transform:translateX(100%)}}
.cam .cap{height:var(--cap);flex:none;display:flex;align-items:center;gap:9px;padding:0 12px 0 8px;
  border-top:1px solid var(--border);background:var(--surface);transition:background-color .15s}
.cam .num{flex:none;min-width:28px;height:22px;padding:0 6px;display:inline-flex;align-items:center;justify-content:center;
  border-radius:6px;background:var(--surface-3);color:var(--text-2);font-size:11.5px;font-weight:700;
  font-variant-numeric:tabular-nums;transition:background-color .15s,color .15s}
.cam .name{flex:1;min-width:0;font-size:13.5px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cam .meta{flex:none;font-size:11.5px;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.cam .num:empty,.cam .meta:empty{display:none}
.cam .ex{position:absolute;top:8px;right:8px;z-index:2;width:30px;height:30px;padding:6px;border-radius:8px;
  background:rgba(6,8,11,.7);color:#fff;opacity:0;transform:scale(.9);transition:opacity .15s,transform .15s}
.cam .nocam{display:none}
.cam.empty{cursor:default;border-style:dashed;border-color:#1c222c;background:#0b0e13}
.cam.empty .vid,.cam.empty .cap{visibility:hidden}          /* keep the tile's size */
.cam.empty .nocam{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:8px;color:#3a4352;font-size:12.5px;font-weight:500}
.cam.empty .nocam .ic{width:26px;height:26px;stroke-width:1.6}
.cam:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (hover:hover){
  .cam:not(.empty):hover{border-color:rgba(79,140,255,.75);box-shadow:0 0 0 1px rgba(79,140,255,.35),0 14px 30px rgba(0,0,0,.35)}
  .cam:not(.empty):hover .ex{opacity:1;transform:none}
  .cam:not(.empty):hover .cap{background:var(--surface-2)}
  .cam:not(.empty):hover .num{background:var(--accent);color:#fff}
}
.notice{display:flex;align-items:center;gap:12px;width:100%;max-width:620px;margin:0 auto var(--gap);padding:12px 14px;
  border-radius:var(--r);background:var(--bad-soft);border:1px solid rgba(239,68,68,.35);color:#fecaca}
.notice>.ic{color:#f87171}
.notice div{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.notice small{color:#fca5a5}

/* ── footer / status bar ─────────────────────────────────── */
.foot{display:flex;align-items:center;gap:12px;min-height:var(--foot);
  padding:0 max(var(--pad),var(--sar)) 0 max(var(--pad),var(--sal));
  border-top:1px solid var(--border);background:var(--surface);color:var(--text-2);font-size:12.5px}
#range{display:flex;align-items:baseline;gap:8px;min-width:0;white-space:nowrap}
#range .rp{font-weight:650;color:var(--text)}
#range .rc{color:var(--muted)}
.keys{margin-left:auto;display:none;align-items:center;gap:6px;color:var(--muted);white-space:nowrap}
.keys .sep{opacity:.5;margin:0 4px}
.fnav{display:none}

/* ── single-camera view ──────────────────────────────────── */
#view{position:fixed;inset:0;z-index:50;display:none;background:#000;outline:none}
body.viewing #view{display:block}
#stage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  padding:var(--sat) var(--sar) var(--sab) var(--sal)}
#live{width:100%;height:100%;object-fit:contain;display:block;opacity:0;transition:opacity .2s}
#view.ready #live{opacity:1}
#vload{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;
  color:var(--muted);font-size:13px;pointer-events:none}
#view.ready #vload{display:none}
#bar{position:absolute;left:0;right:0;top:0;z-index:2;display:flex;align-items:center;gap:8px;
  padding:calc(10px + var(--sat)) max(14px,var(--sar)) 34px max(14px,var(--sal));
  background:linear-gradient(to bottom,rgba(0,0,0,.8),rgba(0,0,0,.42) 60%,transparent);transition:opacity .3s}
#bar button,.side{background:rgba(20,24,32,.62);border-color:rgba(255,255,255,.14);color:#fff}
#bar button:hover:not(:disabled),.side:hover:not(:disabled){background:rgba(44,52,66,.88);border-color:rgba(255,255,255,.26)}
.vt{display:flex;flex-direction:column;min-width:0;margin-left:4px}
#title{font-size:16px;font-weight:650;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#tech{font-size:12.5px;color:rgba(255,255,255,.62);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vpos{flex:none;height:28px;padding:0 10px;display:inline-flex;align-items:center;border-radius:999px;
  background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.12);color:rgba(255,255,255,.82);
  font-size:12.5px;font-weight:600;font-variant-numeric:tabular-nums}
.vpos:empty{display:none}
.side{position:absolute;top:50%;z-index:2;width:52px;height:52px;margin-top:-26px;padding:0;border-radius:50%;
  transition:opacity .3s,background-color .15s,border-color .15s}
.side .ic{width:24px;height:24px}
.side.l{left:max(16px,var(--sal))}
.side.r{right:max(16px,var(--sar))}
#view.idle #bar,#view.idle .side{opacity:0;pointer-events:none}
#view.idle{cursor:none}
@media (hover:hover){ #bar .tnav{display:none} }
@media (hover:none){ .side{display:none} }

/* ── responsive ──────────────────────────────────────────── */
@media (min-width:760px){
  .pager:not(.many) .pages{display:flex}
  .pager:not(.many) .pageinfo{display:none}
}
@media (min-width:1000px) and (hover:hover){ .keys{display:flex} }
@media (min-width:1180px){ .clock{display:flex} }

/* phones, portrait: 2 per row (or 1 per row), page buttons at the bottom within thumb reach */
@media (max-width:599px) and (orientation:portrait){
  :root{--pad:10px;--gap:8px;--bar:56px}
  .appbar{gap:8px;padding-left:max(12px,var(--sal));padding-right:max(12px,var(--sar))}
  .appbar .pager{display:none}
  #layoutBtn{display:inline-flex}
  #settingsLink{width:36px;padding:0}
  #settingsLink .lbl{display:none}
  body.large .grid{grid-template-columns:minmax(0,1fr)}
  .cam{border-radius:12px}
  .cam.empty{display:none}
  body:not(.large) .cam .cap{height:32px;gap:6px;padding:0 8px 0 6px}
  body:not(.large) .cam .meta{display:none}
  body:not(.large) .cam .name{font-size:12px}
  body:not(.large) .cam .num{height:20px;min-width:22px;padding:0 4px;font-size:10.5px}
  .foot{position:sticky;bottom:0;z-index:20;justify-content:space-between;min-height:calc(64px + var(--sab));
        padding-top:8px;padding-bottom:calc(8px + var(--sab));background:rgba(13,16,22,.97)}
  .fnav{display:inline-flex;width:46px;height:46px;border-radius:14px}
  #range{flex-direction:column;align-items:center;gap:1px}
  #range .x{display:none}
  #range .rp{font-size:14px}
  #title{font-size:15px}
}
@media (max-width:419px){ .vpos{display:none} .brand-txt small{display:none} }

/* whole wall on one screen: 3 x 2 tiles sized to fit the window (no scrolling) */
@media (min-width:1000px),(orientation:landscape) and (min-width:560px){
  .grid{grid-template-columns:repeat(3,minmax(0,1fr));
        width:min(100%,calc((100vh - var(--chrome)) * 8 / 3 + 2 * var(--gap)));
        width:min(100%,calc((100dvh - var(--chrome)) * 8 / 3 + 2 * var(--gap)))}
  #wall{justify-content:center}
}
/* short landscape screens (phones on their side): slimmer bars, no footer */
@media (orientation:landscape) and (max-height:520px){
  :root{--bar:48px;--foot:0px;--pad:8px;--gap:8px}
  .foot{display:none}
  .appbar .pager{display:flex}
  .brand-txt small{display:none}
  .logo{width:30px;height:30px}
  #settingsLink{width:36px;padding:0}
  #settingsLink .lbl{display:none}
  :root{--cap:30px}
  .cam{border-radius:10px}
  .cam .cap{gap:6px;padding:0 8px 0 6px}
  .cam .meta{display:none}
  .cam .name{font-size:12px}
  .cam .num{height:20px;min-width:22px;padding:0 4px;font-size:10.5px}
}
body.fs .foot{display:none}
body.fs{--foot:0px}
</style>
</head>
<body>
<!--@icons-->
<header class=appbar>
  <div class=brand>
    <span class=logo><svg class=ic><use href="#i-cam"/></svg></span>
    <span class=brand-txt><b>GRAV CCTV</b><small>Live view</small></span>
  </div>
  <nav class=pager id=pager aria-label="Camera pages">
    <button class=icon id=prevBtn onclick="page(-1)" title="Previous page (P or &larr;)" aria-label="Previous page"><svg class=ic><use href="#i-left"/></svg></button>
    <div class=pages id=pages></div>
    <b class=pageinfo id=pageinfo>&ndash;</b>
    <button class=icon id=nextBtn onclick="page(1)" title="Next page (N or &rarr;)" aria-label="Next page"><svg class=ic><use href="#i-right"/></svg></button>
  </nav>
  <div class=tools>
    <div class=clock id=clock aria-hidden=true><span class=t></span><span class=d></span></div>
    <button class="icon ghost" id=layoutBtn title="Show one camera per row" aria-label="Show one camera per row"><svg class=ic><use href="#i-rows"/></svg></button>
    <button class="icon ghost fsb" title="Full screen (F)" aria-label="Full screen" hidden><svg class=ic><use href="#i-max"/></svg></button>
    <a class=btn id=settingsLink href="/settings" title="Rename and re-order cameras"><svg class=ic><use href="#i-sliders"/></svg><span class=lbl>Settings</span></a>
  </div>
</header>
<main id=wall>
  <div class=notice id=notice hidden>
    <svg class=ic><use href="#i-alert"/></svg>
    <div><b>Could not load the camera list</b><small id=noticeMsg></small></div>
    <button class=sm id=retryBtn>Try again</button>
  </div>
  <div class=grid id=grid></div>
</main>
<footer class=foot>
  <button class="icon fnav" onclick="page(-1)" aria-label="Previous page"><svg class=ic><use href="#i-left"/></svg></button>
  <span id=range><span class=rp>Loading cameras&hellip;</span><span class=x>&middot;</span><span class=rc></span></span>
  <span class=keys><span class=kbd>&larr;</span><span class=kbd>&rarr;</span> change page<span class=sep>|</span><span class=kbd>1</span>&ndash;<span class=kbd>6</span> open camera<span class=sep>|</span><span class=kbd>F</span> full screen</span>
  <button class="icon fnav" onclick="page(1)" aria-label="Next page"><svg class=ic><use href="#i-right"/></svg></button>
</footer>

<div id=view role=dialog aria-modal=true aria-labelledby=title tabindex=-1>
  <div id=stage><img id=live fetchpriority=high><div id=vload><span class=spinner></span><span>Connecting to camera&hellip;</span></div></div>
  <div id=bar>
    <button class=icon onclick="close_()" title="Back to all cameras (Esc)" aria-label="Back to all cameras"><svg class=ic><use href="#i-back"/></svg></button>
    <div class=vt><span id=title></span><span id=tech></span></div>
    <span class=grow></span>
    <span class=vpos id=vpos></span>
    <button class="icon tnav" onclick="step(-1)" title="Previous camera" aria-label="Previous camera"><svg class=ic><use href="#i-left"/></svg></button>
    <button class="icon tnav" onclick="step(1)" title="Next camera" aria-label="Next camera"><svg class=ic><use href="#i-right"/></svg></button>
    <button class="icon fsb" title="Full screen (F)" aria-label="Full screen" hidden><svg class=ic><use href="#i-max"/></svg></button>
  </div>
  <button class="side l" onclick="step(-1)" title="Previous camera (&larr;)" aria-label="Previous camera"><svg class=ic><use href="#i-left"/></svg></button>
  <button class="side r" onclick="step(1)" title="Next camera (&rarr;)" aria-label="Next camera"><svg class=ic><use href="#i-right"/></svg></button>
</div>

<template id=cellTpl><div class=cam tabindex=0 role=button><div class=vid><img fetchpriority=high><svg class="ic ex"><use href="#i-expand"/></svg></div><div class=cap><b class=num></b><span class=name></span><span class=meta></span></div><div class=nocam><svg class=ic><use href="#i-off"/></svg><span>No camera</span></div></div></template>

<script>
const PER  = 6;
const KEY  = new URLSearchParams(location.search).get('key') || '';
const q    = KEY ? '?key='+encodeURIComponent(KEY) : '';
const $    = (id) => document.getElementById(id);
let cams = [], pg = 0, loaded = false;

// Cameras in DISPLAY order (names/order come from the Settings page). Each camera
// keeps its technical 'index', which is what /stream/<index> uses -- renaming or
// re-ordering never changes which RTSP stream a tile opens. Pages are cut from
// this sorted list, so displayOrder 1-6 = page 1, 7-12 = page 2, ...
function loadCameras(){
  return fetch('/api/cameras'+q).then(r=>{ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).then(list=>{
    cams = list.slice().sort((a, b) => (a.displayOrder - b.displayOrder) || (a.index - b.index));
    loaded = true;
  });
}
document.getElementById('settingsLink').href = '/settings' + q;

function pages(){ return Math.max(1, Math.ceil(cams.length/PER)); }
function streamUrl(i){ return '/stream/'+i+q; }
// Cache-busting retry URL. Must work with AND without ?key= (e.g. cookie/SSO access):
// appending '&_r=' to '/stream/5' would give '/stream/5&_r=..', which the server
// rejects as a bad camera id, so a retried tile would never recover.
function retryUrl(i){ return streamUrl(i) + (q ? '&' : '?') + '_r=' + Date.now(); }

/* FIXED CELLS. We create PER <img> cells ONCE and only change their src. Changing
   (or clearing) an <img>'s src reliably ABORTS its current MJPEG connection, so a
   camera that scrolls off the page / is stopped for the single-camera view releases
   its NVR slot at once. (Rebuilding the grid's markup used to destroy <img> elements
   WITHOUT aborting their streams -- the browser kept streaming to detached images,
   leaking viewers and NVR slots.) */
let cells = [];
let fullscreen = false;   // single-camera view open
let liveCam = null;       // the camera shown in it

/* fetchpriority=high: browsers throttle LOW-priority image loads while a page is
   "still loading" or the network looks slow, and wait for in-flight images to
   finish first. An MJPEG <img> never finishes, so throttled tiles could wait
   forever (measured: only 3 of 6 streams were even requested, the rest held back
   in the browser for 40 s to indefinitely). High priority exempts the streams. */
function buildCells(){
  const grid = $('grid'), tpl = $('cellTpl');
  grid.textContent = '';
  cells = Array.from({length: PER}, () => {
    const cell = tpl.content.firstElementChild.cloneNode(true);
    grid.appendChild(cell);
    const c = { cell, img: cell.querySelector('img'), span: cell.querySelector('.name'),
                meta: cell.querySelector('.meta'), num: cell.querySelector('.num'), idx: null, cam: null };
    c.img.alt = '';
    cell.onclick = () => { if (c.cam) open_(c.cam); };
    cell.onkeydown = (e) => { if ((e.key === 'Enter' || e.key === ' ') && c.cam){ e.preventDefault(); open_(c.cam); } };
    // server-restart / transient recovery: retry this cell's own camera
    c.img.onerror = () => {
      const i = c.idx;
      setTimeout(() => {
        if (!fullscreen && c.idx === i && i != null) c.img.src = retryUrl(i);
      }, 2000);
    };
    c.img.onload = () => { if (c.img.getAttribute('src')) cell.classList.add('ready'); };
    return c;
  });
}

// Abort every grid stream now, so their browser connection slots and NVR slots
// are released. (An MJPEG <img> holds one of the browser's ~6 per-host
// connections for its whole life; you MUST free them before opening a new
// page's streams or the new ones can't connect.)
function clearCells(){
  cells.forEach(c => { c.idx = null; c.cam = null; c.span.textContent = ''; c.meta.textContent = ''; c.num.textContent = '';
                       c.cell.classList.remove('ready'); c.img.removeAttribute('src'); });
}

function showPage(){
  if (pg >= pages()) pg = pages() - 1;
  const start = pg*PER, shown = cams.slice(start, start+PER);
  renderPager(false);
  cells.forEach((c, k) => {
    if (k < shown.length){
      const cam = shown[k];
      c.cam = cam;
      c.idx = cam.index;                   // technical index = the stream to open
      c.span.textContent = cam.displayName;  // text only -- never parsed as HTML
      c.meta.textContent = cam.nvr.toUpperCase() + ' · CH ' + cam.channel;
      c.num.textContent = String(start + k + 1).padStart(2, '0');
      c.cell.title = cam.displayName + ' — ' + cam.technicalName + ' · CH ' + cam.channel;
      c.cell.setAttribute('aria-label', cam.displayName + ' (camera ' + (start + k + 1) + '). Open large view');
      c.cell.classList.remove('empty');
      c.cell.tabIndex = 0;
      c.img.src = streamUrl(c.idx);
    } else {
      c.idx = null;
      c.cam = null;
      c.span.textContent = ''; c.meta.textContent = ''; c.num.textContent = '';
      c.cell.removeAttribute('title');
      c.cell.setAttribute('aria-label', 'Empty tile');
      c.cell.classList.add('empty');
      c.cell.tabIndex = -1;
      c.img.removeAttribute('src');
    }
  });
}

function renderPager(loading){
  const P = pages(), start = pg*PER, n = Math.max(0, Math.min(PER, cams.length - start));
  $('pageinfo').textContent = (pg+1) + ' / ' + P;
  const rp = document.querySelector('#range .rp'), rc = document.querySelector('#range .rc');
  if (!loaded){ rp.textContent = 'Loading cameras…'; rc.textContent = ''; }
  else if (!cams.length){ rp.textContent = 'No cameras configured'; rc.textContent = ''; }
  else {
    rp.textContent = 'Page ' + (pg+1) + ' of ' + P;
    rc.textContent = loading ? 'Loading…' : 'Cameras ' + (start+1) + '–' + (start+n) + ' of ' + cams.length;
  }
  document.querySelector('#range .x').hidden = !rc.textContent;
  const box = $('pages');
  if (box.childElementCount !== P){
    box.textContent = '';
    for (let p = 0; p < P; p++){
      const b = document.createElement('button');
      b.type = 'button'; b.textContent = String(p+1);
      b.title = 'Page ' + (p+1); b.setAttribute('aria-label', 'Page ' + (p+1));
      b.onclick = () => { if (p !== pg) goPage(p); };
      box.appendChild(b);
    }
  }
  Array.from(box.children).forEach((b, p) => {
    b.classList.toggle('on', p === pg);
    if (p === pg) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  $('pager').classList.toggle('many', P > 12);
  document.querySelectorAll('#prevBtn, #nextBtn, .fnav').forEach(b => { b.disabled = P < 2; });
}

// Initial render.
function draw(){ showPage(); }

// Page change: abort the current page's streams FIRST, let the browser/server
// release those connections, THEN open the new page. Without this hand-off the
// old MJPEG connections keep every browser connection slot and the new page's
// streams deadlock (can't open). This ordered teardown is required for MJPEG
// under the per-host connection limit, not a cosmetic delay.
let pageSeq = 0;
function page(d){ goPage((pg + d + pages()) % pages()); }
function goPage(p, delay){
  if (fullscreen) return;
  const my = ++pageSeq;
  clearCells();
  pg = p;
  renderPager(true);
  // The streams were just aborted, so a connection is free: pick up names/order
  // saved on the Settings page (from any browser) before showing the next page.
  const fresh = loadCameras().catch(() => {});
  setTimeout(() => fresh.then(() => { if (my === pageSeq && !fullscreen) showPage(); }), delay == null ? 500 : delay);
}
// Same page, fresh names/order (after a save on the Settings page, or when the page
// comes back from the browser's back/forward cache).
function refresh(delay){
  const my = ++pageSeq;
  clearCells();
  const fresh = loadCameras().catch(() => {});
  setTimeout(() => fresh.then(() => { if (my === pageSeq && !fullscreen) showPage(); }), delay);
}

// Saved on the Settings page in another tab of this browser: reload names/order now.
window.addEventListener('storage', (e) => {
  if (e.key !== 'cctv-camera-settings-rev' || fullscreen) return;
  refresh(300);
});

// ── single-camera view ─────────────────────────────────────────────────────
const baseTitle = document.title;
let stepT = 0;
function setLiveInfo(cam){
  document.getElementById('title').textContent = cam.displayName;
  document.getElementById('tech').textContent = (cam.displayName !== cam.technicalName
      ? cam.technicalName + ' · ' : '') + cam.nvr.toUpperCase() + ' · CH ' + cam.channel;
  const k = cams.findIndex(c => c.index === cam.index);
  $('vpos').textContent = k < 0 ? '' : (k + 1) + ' / ' + cams.length;
  document.title = cam.displayName + ' · GRAV CCTV';
}
function startLive(cam){
  const live = document.getElementById('live'), i = cam.index;
  live.onerror = () => setTimeout(() => { if (fullscreen && liveCam === cam) live.src = retryUrl(i); }, 2000);
  live.src = streamUrl(i);
}
function open_(cam){
  // Handoff: keep this camera's grid cell streaming (same worker, slot and cached
  // frame -- no restart) and stop the OTHER cells FIRST, so the large view's stream
  // gets a browser connection at once instead of queuing behind the per-host limit.
  const i = cam.index;
  fullscreen = true;
  liveCam = cam;
  cells.forEach((c) => { if (c.idx !== i) c.img.removeAttribute('src'); });
  // phone "back" gesture / button closes the view instead of leaving the page
  // (pushed BEFORE the title changes, so the grid's history entry keeps its title)
  try { if (!(history.state && history.state.cctvView)) history.pushState({cctvView: 1}, ''); } catch (e) {}
  setLiveInfo(cam);
  startLive(cam);
  document.body.classList.add('viewing');
  $('view').focus({preventScroll: true});
  wake();
}
// Previous / next camera in the large view (all cameras, in grid order).
function step(d){
  if (!fullscreen || !liveCam || cams.length < 2) return;
  const k = cams.findIndex(c => c.index === liveCam.index);
  const cam = cams[(k + d + cams.length) % cams.length];
  const live = document.getElementById('live');
  // stop the current stream AND the grid tile kept for the hand-off, so only the
  // camera being watched holds a browser connection and an NVR slot
  cells.forEach(c => c.img.removeAttribute('src'));
  live.onerror = null;
  live.removeAttribute('src');
  liveCam = cam;
  setLiveInfo(cam);
  wake();
  clearTimeout(stepT);
  // flicking through several cameras only opens the one you stop on
  stepT = setTimeout(() => { if (fullscreen && liveCam === cam) startLive(cam); }, 250);
}
function close_(fromHistory){
  if (!fullscreen) return;
  fullscreen = false;
  clearTimeout(stepT);
  clearTimeout(idleT);
  const cam = liveCam;
  liveCam = null;
  const live = document.getElementById('live');
  document.body.classList.remove('viewing');
  $('view').classList.remove('idle', 'ready');
  document.title = baseTitle;
  live.onerror = null;
  live.removeAttribute('src');       // abort the large view's stream
  if (!fromHistory) try { if (history.state && history.state.cctvView) history.back(); } catch (e) {}
  // stepped to a camera on another page: show THAT page
  const k = cam ? cams.findIndex(c => c.index === cam.index) : -1;
  const p = k < 0 ? pg : Math.floor(k / PER);
  if (p !== pg){ goPage(p, 300); return; }
  // restore every grid cell's stream (they were stopped for the large view)
  cells.forEach((c) => { if (c.idx != null) c.img.src = streamUrl(c.idx); });
  const tile = cells.find(c => cam && c.idx === cam.index);
  if (tile) tile.cell.focus({preventScroll: true});
}
window.addEventListener('popstate', () => { if (fullscreen) close_(true); });
try { if (history.state && history.state.cctvView) history.replaceState(null, ''); } catch (e) {}

// controls fade out after a few seconds without mouse movement; tap toggles them
let idleT = 0, wasIdle = false, lastPtr = 'mouse';
function wake(){
  const v = $('view');
  v.classList.remove('idle');
  clearTimeout(idleT);
  idleT = setTimeout(() => { if (fullscreen) v.classList.add('idle'); }, 3500);
}
$('view').addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse') wake(); });
$('view').addEventListener('pointerdown', (e) => { lastPtr = e.pointerType; wasIdle = $('view').classList.contains('idle'); wake(); });
$('stage').addEventListener('click', () => { if (lastPtr !== 'mouse' && !wasIdle){ clearTimeout(idleT); $('view').classList.add('idle'); } });
$('stage').addEventListener('dblclick', () => toggleFs());

// Refresh / close / navigate away: abort every MJPEG stream FIRST. Each open <img>
// stream holds one of the browser's ~6 HTTP/1.1 connections to this host, and the
// old page's streams are only torn down after the NEW page has loaded -- so a
// reload needs a 7th connection that never frees up and hangs forever (measured:
// a reload hung ~7 min until the tab was closed). 'beforeunload' runs before the
// reload request is sent (no prompt is shown); 'pagehide' covers mobile Safari.
function stopAllStreams(){
  cells.forEach(c => c.img.removeAttribute('src'));
  const live = document.getElementById('live');
  if (live) { live.onerror = null; live.removeAttribute('src'); }
}
window.addEventListener('beforeunload', stopAllStreams);
window.addEventListener('pagehide', stopAllStreams);
// Back/forward cache: the page can return WITHOUT reloading after pagehide
// stopped its streams (e.g. Back from the Settings page) -- start them again.
window.addEventListener('pageshow', (e) => {
  if (!e.persisted) return;
  if (fullscreen && liveCam) startLive(liveCam); else refresh(100);
});

// ── browser full screen (wall display) ─────────────────────────────────────
const fsOK = !!(document.fullscreenEnabled || document.webkitFullscreenEnabled);
function isFs(){ return !!(document.fullscreenElement || document.webkitFullscreenElement); }
function toggleFs(){
  if (!fsOK) return;
  try {
    const el = document.documentElement;
    const r = isFs() ? (document.exitFullscreen || document.webkitExitFullscreen).call(document)
                     : (el.requestFullscreen || el.webkitRequestFullscreen).call(el);
    if (r && r.catch) r.catch(() => {});
  } catch (e) {}
}
function syncFs(){
  const on = isFs();
  document.body.classList.toggle('fs', on);
  document.querySelectorAll('.fsb').forEach(b => {
    b.querySelector('use').setAttribute('href', on ? '#i-min' : '#i-max');
    b.title = on ? 'Exit full screen (F)' : 'Full screen (F)';
    b.setAttribute('aria-label', on ? 'Exit full screen' : 'Full screen');
  });
}
document.querySelectorAll('.fsb').forEach(b => { b.hidden = !fsOK; b.onclick = toggleFs; });
document.addEventListener('fullscreenchange', syncFs);
document.addEventListener('webkitfullscreenchange', syncFs);

// ── phone layout: 2 cameras per row, or 1 large camera per row ─────────────
const LAYOUT_KEY = 'cctv-grid-layout';
function setLayout(v, save){
  const large = v === 'large', b = $('layoutBtn');
  document.body.classList.toggle('large', large);
  b.querySelector('use').setAttribute('href', large ? '#i-grid' : '#i-rows');
  b.title = large ? 'Show two cameras per row' : 'Show one camera per row';
  b.setAttribute('aria-label', b.title);
  if (save) try { localStorage.setItem(LAYOUT_KEY, v); } catch (e) {}
}
let savedLayout = 'grid';
try { savedLayout = localStorage.getItem(LAYOUT_KEY) || 'grid'; } catch (e) {}
setLayout(savedLayout, false);
$('layoutBtn').onclick = () => setLayout(document.body.classList.contains('large') ? 'grid' : 'large', true);

// ── swipe: grid = change page, large view = change camera (down = close) ───
function onSwipe(el, fn){
  let x0 = 0, y0 = 0, t0 = 0, on = false;
  el.addEventListener('touchstart', (e) => {
    on = e.touches.length === 1 && !(window.visualViewport && visualViewport.scale > 1.05);  // not while pinch-zoomed
    if (on){ x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; t0 = Date.now(); }
  }, {passive: true});
  el.addEventListener('touchend', (e) => {
    if (!on) return;
    on = false;
    const t = e.changedTouches[0], dx = t.clientX - x0, dy = t.clientY - y0;
    if (Date.now() - t0 > 700) return;
    if (Math.abs(dx) > 60 && Math.abs(dx) > 1.6 * Math.abs(dy)) fn(dx < 0 ? 'left' : 'right');
    else if (dy > 90 && dy > 1.6 * Math.abs(dx)) fn('down');
  }, {passive: true});
}
onSwipe($('wall'), (d) => { if (d === 'left') page(1); else if (d === 'right') page(-1); });
onSwipe($('stage'), (d) => { if (d === 'left') step(1); else if (d === 'right') step(-1); else close_(); });

document.addEventListener('keydown', e=>{
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (fullscreen){
    if (k==='escape'||k==='backspace'||k==='v'){ e.preventDefault(); close_(); }
    else if (k==='arrowright'||k==='n') step(1);
    else if (k==='arrowleft'||k==='p') step(-1);
    else if (k==='f') toggleFs();
    else wake();
    return;
  }
  if (k==='n'||k==='arrowright'||k==='pagedown') page(1);
  else if (k==='p'||k==='arrowleft'||k==='pageup') page(-1);
  else if (k==='f') toggleFs();
  else if (k >= '1' && k <= '6' && k.length === 1){ const c = cells[+k - 1]; if (c && c.cam) open_(c.cam); }
});

// "ready" = the stream's first image has arrived (hides the loading shimmer)
setInterval(() => {
  cells.forEach(c => c.cell.classList.toggle('ready', !!c.img.getAttribute('src') && c.img.naturalWidth > 0));
  const live = $('live');
  $('view').classList.toggle('ready', !!live.getAttribute('src') && live.naturalWidth > 0);
}, 250);

// clock (this device's time)
function tick(){
  const d = new Date(), el = $('clock');
  el.querySelector('.t').textContent = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  el.querySelector('.d').textContent = d.toLocaleDateString([], {weekday: 'short', day: 'numeric', month: 'short', year: 'numeric'});
}
tick();
setInterval(tick, 1000);

function start(){
  $('notice').hidden = true;
  loadCameras().then(draw, (e) => {
    $('noticeMsg').textContent = (e && e.message ? e.message + '. ' : '') + 'Check the network connection, then try again.';
    $('notice').hidden = false;
    cells.forEach(c => c.cell.classList.add('empty'));
    renderPager(false);
  });
}
$('retryBtn').onclick = start;
buildCells();
renderPager(true);
start();
</script>
</body>
</html>
""")
