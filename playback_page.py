"""Playback page (served at /playback): recorded footage from the NVRs.

Filter bar (From / To in NVR time, cameras with search / Select all / Clear, quick
presets) -> POST /api/playback/search -> per-camera result + recording availability
(segments on the office LAN) -> player grid fed by ONE WebSocket
(/api/playback/ws?sid=...): JPEG frames that carry their RECORDED time, the chosen
camera's G.711 audio, and the session state. One shared timeline: seek / pause /
speed act on every camera shown. Times are always NVR time (IST), whatever the
browser's own time zone. No NVR credential or RTSP URL ever reaches this page.
All names are rendered with textContent, never as HTML.
"""
from ui_theme import render

PLAYBACK_PAGE = render(r"""<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=color-scheme content=dark>
<meta name=theme-color content="#0a0c10">
<title>Playback · GRAV CCTV</title>
<link rel=icon href="{{FAVICON}}">
<style>
/*@theme*/
body{min-height:100vh}
.nav{display:flex;gap:4px;padding:3px;border-radius:var(--r);background:var(--surface-2);border:1px solid var(--border)}
.nav a{height:32px;padding:0 12px;border-radius:8px;display:inline-flex;align-items:center;gap:7px;text-decoration:none;
  color:var(--text-2);font-weight:500;font-size:13.5px}
.nav a:hover{background:var(--surface-3);color:var(--text)}
.nav a.on{background:var(--surface-4);color:var(--text);box-shadow:inset 0 0 0 1px var(--border-3)}
.wrap{max-width:1680px;margin:0 auto;padding:14px max(16px,var(--sar)) 24px max(16px,var(--sal))}

/* filter bar */
.filter{display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px;padding:14px;border-radius:var(--r-lg);
  background:var(--surface);border:1px solid var(--border)}
.fg{display:flex;flex-direction:column;gap:5px;min-width:0}
.fg>label{font-size:11.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.dt{display:flex;gap:6px}
input[type=date],input[type=time]{height:38px;padding:0 10px;border-radius:var(--r);border:1px solid var(--border-2);
  background:var(--bg);color:var(--text);font:14px var(--font);color-scheme:dark}
input[type=date]:focus,input[type=time]:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.presets{display:flex;gap:6px;flex-wrap:wrap}
.tzn{font-size:12px;color:var(--muted);align-self:center}
.errmsg{flex-basis:100%;display:flex;gap:8px;align-items:center;color:#fca5a5;font-size:13px}
.errmsg .ic{color:#f87171}

/* camera picker */
.picker{position:relative}
#camBtn{min-width:210px;justify-content:space-between}
#camBtn .cnt{color:var(--text-2);font-weight:500}
.panel{position:absolute;z-index:40;top:calc(100% + 6px);left:0;width:340px;max-height:420px;display:flex;flex-direction:column;
  border-radius:var(--r-lg);background:var(--surface-2);border:1px solid var(--border-3);box-shadow:var(--shadow)}
.panel .ph{display:flex;gap:6px;padding:10px;border-bottom:1px solid var(--border)}
.panel .ph input{flex:1}
.panel .pa{display:flex;gap:6px;padding:8px 10px;border-bottom:1px solid var(--border)}
.plist{overflow:auto;padding:6px}
.opt{display:flex;align-items:center;gap:10px;padding:7px 8px;border-radius:8px;cursor:pointer}
.opt:hover{background:var(--surface-3)}
.opt input{width:16px;height:16px;accent-color:var(--accent)}
.opt .on{display:flex;flex-direction:column;min-width:0;line-height:1.25}
.opt .on b{font-weight:600;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.opt .on small{color:var(--muted);font-size:11.5px}

/* summary + timeline */
.card{margin-top:12px;padding:12px 14px;border-radius:var(--r-lg);background:var(--surface);border:1px solid var(--border)}
.sum{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center}
.sum .iv{font-weight:600}
.snote{margin-top:8px;font-size:12.5px;color:#fde68a}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 10px;border-radius:13px;font-size:12.5px;
  background:var(--surface-3);border:1px solid var(--border-2);color:var(--text-2)}
.chip.ok{background:var(--ok-soft);border-color:rgba(34,197,94,.35);color:#bbf7d0}
.chip.part{background:var(--warn-soft);border-color:rgba(245,158,11,.35);color:#fde68a}
.chip.no{background:var(--bad-soft);border-color:rgba(239,68,68,.35);color:#fecaca}
.tl{margin-top:10px}
.tlrow{display:grid;grid-template-columns:170px 1fr;gap:10px;align-items:center;height:18px;margin-bottom:4px}
.tlrow .tn{font-size:12px;color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{position:relative;height:10px;border-radius:5px;background:repeating-linear-gradient(135deg,#1b212c 0 6px,#171c25 6px 12px);overflow:hidden}
.bar i{position:absolute;top:0;bottom:0;background:linear-gradient(#3b82f6,#2563eb);border-radius:3px}
.bar.unk{background:var(--surface-3)} .bar.unk::after{content:"recorded -- gap details only on the office network";position:absolute;inset:0;
  font-size:10px;line-height:10px;color:var(--muted);padding-left:6px}
.bar.srch::after{content:"searching recordings…"}
.scrub{position:relative;display:grid;grid-template-columns:170px 1fr;gap:10px;align-items:center;margin-top:8px}
.strack{position:relative;height:28px;cursor:pointer;touch-action:none}
.strack .rail{position:absolute;left:0;right:0;top:12px;height:4px;border-radius:2px;background:var(--surface-4)}
.strack .fill{position:absolute;left:0;top:12px;height:4px;border-radius:2px;background:var(--accent)}
.strack .knob{position:absolute;top:6px;width:16px;height:16px;margin-left:-8px;border-radius:50%;background:#fff;
  box-shadow:0 0 0 3px var(--accent-soft),0 2px 6px rgba(0,0,0,.4)}
#summary{position:relative}
.tltip{position:absolute;z-index:30;transform:translate(-50%,-100%);padding:4px 9px;border-radius:7px;text-align:center;
  background:var(--surface-4);border:1px solid var(--border-3);box-shadow:0 4px 14px rgba(0,0,0,.45);white-space:nowrap;
  pointer-events:none;line-height:1.3}
.tltip b{display:block;font-size:12.5px;font-weight:600;font-variant-numeric:tabular-nums}
.tltip small{display:block;font-size:11px;color:var(--muted)}
.tlline{position:absolute;z-index:29;width:1px;background:rgba(255,255,255,.5);pointer-events:none}
.ticks{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin:0 0 0 180px}

/* players */
.stage{margin-top:12px;display:flex;flex-direction:column;gap:10px}
.stage:fullscreen{background:var(--bg);padding:10px;gap:8px}
.grid{display:grid;gap:10px}
.grid.g1{grid-template-columns:1fr}
.grid.g2{grid-template-columns:repeat(2,1fr)}
.grid.g4{grid-template-columns:repeat(2,1fr)}
.grid.g6{grid-template-columns:repeat(3,1fr)}
.stage.focus .grid{grid-template-columns:1fr}
.stage.focus .ptile:not(.fo){display:none}
.ptile{display:flex;flex-direction:column;border-radius:var(--r-lg);overflow:hidden;background:var(--surface);
  border:1px solid var(--border)}
.ptile.aud{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.phead{display:flex;align-items:center;gap:8px;padding:7px 10px;border-bottom:1px solid var(--border);min-width:0}
.phead b{font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.phead .pm{font-size:11.5px;color:var(--muted);white-space:nowrap}
.pstat{margin-left:auto;font-size:11.5px;padding:2px 8px;border-radius:10px;background:var(--surface-3);color:var(--text-2);white-space:nowrap}
.pstat.ok{background:var(--ok-soft);color:#86efac} .pstat.warn{background:var(--warn-soft);color:#fcd34d}
.pstat.bad{background:var(--bad-soft);color:#fca5a5}
.pvid{position:relative;aspect-ratio:16/9;background:#05070a}
.stage.focus .pvid{aspect-ratio:auto;height:calc(100vh - 300px);min-height:300px}
.stage:fullscreen.focus .pvid{height:calc(100vh - 170px)}
.pvid canvas{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
.pover{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;
  padding:14px;text-align:center;color:var(--text-2);font-size:13.5px;background:rgba(5,7,10,.55)}
.pover .acts{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
.pts{position:absolute;left:8px;bottom:8px;padding:3px 8px;border-radius:6px;font:600 12.5px/1.2 var(--font);
  background:rgba(0,0,0,.6);color:#fff;letter-spacing:.02em}
.pfoot{display:flex;align-items:center;gap:6px;padding:6px 8px;border-top:1px solid var(--border)}
.pfoot .note{font-size:11.5px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pfoot button.on{color:var(--accent);border-color:var(--accent)}

/* transport */
.transport{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 12px;border-radius:var(--r-lg);
  background:var(--surface);border:1px solid var(--border)}
.now{display:flex;flex-direction:column;line-height:1.2;min-width:150px}
.now b{font-size:20px;font-variant-numeric:tabular-nums;letter-spacing:.02em}
.now small{color:var(--muted);font-size:12px}
.seg{display:inline-flex;padding:3px;gap:3px;border-radius:var(--r);background:var(--surface-2);border:1px solid var(--border)}
.seg button{height:28px;padding:0 10px;border:0;background:transparent;color:var(--text-2)}
.seg button[aria-pressed=true]{background:var(--surface-4);color:var(--text)}
#vol{width:110px;accent-color:var(--accent)}
.abox{display:flex;align-items:center;gap:8px;min-width:0}
.who{display:inline-flex;align-items:center;gap:8px;margin-left:10px;font-size:12.5px;color:var(--text-2);white-space:nowrap}
.who .wn{max-width:160px;overflow:hidden;text-overflow:ellipsis}
.who a{color:var(--text-2)} .who a:hover{color:var(--text)}
.abox #audBtn.on{color:var(--accent);border-color:var(--accent)}
.atx{font-size:12px;color:var(--text-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:360px}
.atx.warn{color:#fcd34d}
.strack .day{position:absolute;top:6px;bottom:6px;width:1px;background:var(--border-3);pointer-events:none}
.bar .day{position:absolute;top:0;bottom:0;width:1px;background:rgba(255,255,255,.28);z-index:1}
.pgnav{display:flex;align-items:center;gap:6px;margin-left:auto;font-size:12.5px;color:var(--text-2)}
.banner{margin-top:12px;display:flex;gap:10px;align-items:center;padding:10px 14px;border-radius:var(--r);
  background:var(--warn-soft);border:1px solid rgba(245,158,11,.35);color:#fde68a;font-size:13px}
.empty{margin-top:28px;text-align:center;color:var(--muted)}
.empty b{display:block;color:var(--text-2);font-size:15px;margin-bottom:4px}
@media (max-width:980px){.grid.g6{grid-template-columns:repeat(2,1fr)}.tlrow,.scrub{grid-template-columns:110px 1fr}.ticks{margin-left:120px}}
@media (max-width:640px){.grid.g2,.grid.g4,.grid.g6{grid-template-columns:1fr}.hide-sm{display:none}}
</style>
</head>
<body>
<!--@icons-->
<header class=appbar>
  <div class=brand>
    <span class=logo><svg class=ic><use href="#i-cam"/></svg></span>
    <span class=brand-txt><b>GRAV CCTV</b><small>Recorded playback</small></span>
  </div>
  <span class=grow></span>
  <nav class=nav aria-label="CCTV sections">
    <a id=navLive href="/"><svg class=ic><use href="#i-grid"/></svg><span class=hide-sm>Live view</span></a>
    <a class=on href="#" aria-current=page><svg class=ic><use href="#i-history"/></svg><span class=hide-sm>Playback</span></a>
    <a id=navSettings href="/settings"><svg class=ic><use href="#i-sliders"/></svg><span class=hide-sm>Settings</span></a>
  </nav>
  <span class=who id=who hidden><span class="wn hide-sm" id=whoName></span><a href="/logout">Sign out</a></span>
</header>
<main class=wrap>
  <section class=filter aria-label="Search recordings">
    <div class=fg><label for=fromD>From</label><div class=dt><input type=date id=fromD><input type=time id=fromT step=1></div></div>
    <div class=fg><label for=toD>To</label><div class=dt><input type=date id=toD><input type=time id=toT step=1></div></div>
    <div class=fg><label>Quick</label><div class=presets>
      <button class=sm data-min=15>Last 15 min</button><button class=sm data-min=30>Last 30 min</button>
      <button class=sm data-min=60>Last 1 hour</button><button class=sm data-min=today>Today</button></div></div>
    <div class="fg picker"><label>Cameras</label>
      <button id=camBtn aria-haspopup=listbox aria-expanded=false><span class=cnt id=camCnt>Select cameras</span><svg class=ic><use href="#i-down"/></svg></button>
      <div class=panel id=panel hidden>
        <div class=ph><input type=search id=camSearch placeholder="Search cameras (e.g. HR)" aria-label="Search cameras"></div>
        <div class=pa><button class=sm id=selAll><svg class=ic><use href="#i-check"/></svg>Select all</button><button class="sm ghost" id=selNone>Clear</button></div>
        <div class=plist id=plist role=listbox aria-multiselectable=true></div>
      </div>
    </div>
    <button class=primary id=searchBtn><svg class=ic><use href="#i-search"/></svg>Search recordings</button>
    <span class=tzn id=tzn>Times are NVR time</span>
    <div class=errmsg id=err hidden><svg class=ic><use href="#i-alert"/></svg><span id=errTx></span></div>
  </section>

  <div class=banner id=banner hidden><svg class=ic><use href="#i-alert"/></svg><span id=bannerTx></span></div>

  <section class=card id=summary hidden>
    <div class=sum><span class=iv id=sumIv></span><span id=sumCnt class=hint></span><div class=chips id=chips></div></div>
    <div class=snote id=sumNote hidden></div>
    <div class=tl id=tl></div>
    <div class=scrub><span class=hint>Timeline</span>
      <div class=strack id=strack role=slider aria-label="Playback position" tabindex=0>
        <div class=rail></div><div class=fill id=sfill></div><div class=knob id=sknob></div>
      </div>
    </div>
    <div class=ticks id=ticks></div>
    <div class=tlline id=tlLine hidden></div>
    <div class=tltip id=tlTip hidden><b id=tlTipT></b><small id=tlTipS hidden>No recording</small></div>
  </section>

  <section class=stage id=stage hidden>
    <div class=grid id=grid></div>
    <div class=transport>
      <button class=primary id=playBtn aria-label="Pause"><svg class=ic><use href="#i-pause"/></svg><span id=playTx>Pause</span></button>
      <div class=now><b id=nowT>--:--:--</b><small id=nowD></small></div>
      <div class=seg role=radiogroup aria-label="Playback speed" id=speedSeg></div>
      <span class=hint id=speedNote hidden>Fast playback may be less smooth (key frames only, no audio).</span>
      <div class=abox id=abox hidden><button class="icon sm" id=audBtn aria-label="Listen"><svg class=ic><use href="#i-mute"/></svg></button>
        <input id=vol type=range min=0 max=100 step=1 aria-label="Volume" title="Volume (50 % = the camera's own level; quiet microphones are raised automatically)">
        <span class=atx id=audTx></span></div>
      <div class=pgnav id=pgnav hidden><button class="icon sm" id=pgPrev aria-label="Previous cameras"><svg class=ic><use href="#i-left"/></svg></button><span id=pgTx></span><button class="icon sm" id=pgNext aria-label="Next cameras"><svg class=ic><use href="#i-right"/></svg></button></div>
    </div>
  </section>
  <div class=empty id=empty><b>Recorded footage</b>Choose a time range and cameras, then Search recordings.</div>
</main>

<template id=tileTpl><div class=ptile><div class=phead><b class=pn></b><span class=pm></span><span class=pstat></span></div>
<div class=pvid><canvas></canvas><div class=pover></div><span class=pts hidden></span></div>
<div class=pfoot><span class=note></span><span class=grow></span>
<button class="icon sm paud" title="Listen to this camera (one camera at a time)" aria-label="Listen"><svg class=ic><use href="#i-mute"/></svg></button>
<button class="icon sm pfs" title="Full screen" aria-label="Full screen"><svg class=ic><use href="#i-max"/></svg></button>
<button class="icon sm pstop" title="Stop this camera (frees playback capacity)" aria-label="Stop this camera"><svg class=ic><use href="#i-close"/></svg></button>
</div></div></template>

<script>
const KEY = new URLSearchParams(location.search).get('key') || '';
const q = KEY ? '?key=' + encodeURIComponent(KEY) : '';
const $ = (id) => document.getElementById(id);
$('navLive').href = '/' + q;
$('navSettings').href = '/settings' + q;
let CFG = null, cams = [], sel = new Set(), SID = null, ws = null, WS_OK = false, ST = null, RES = null;
let page = 0, loadedAt = 0, dragging = false, seekTarget = null, tileEls = {}, wsRetry = 0;
const p2 = (n) => String(n).padStart(2, '0');
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

// ── NVR time (never the browser's zone) ───────────────────────────────────
function parts(ms){ const d = new Date(ms + CFG.tzOffsetMin * 60000);
  return {y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), s: d.getUTCSeconds()}; }
function fTime(ms){ const p = parts(ms); return p2(p.h) + ':' + p2(p.mi) + ':' + p2(p.s); }
function fDate(ms){ const p = parts(ms); return p.y + '-' + p2(p.mo) + '-' + p2(p.d); }
function fLong(ms){ const p = parts(ms); return p.d + ' ' + MON[p.mo - 1] + ' ' + p.y; }
function fDay(ms){ const p = parts(ms); return p.d + ' ' + MON[p.mo - 1]; }
function multiDay(){ return RES && fDate(RES.fromMs) !== fDate(RES.toMs - 1); }              // range crosses midnight
function fStamp(ms){ return multiDay() ? fDay(ms) + ' ' + fTime(ms) : fTime(ms); }
function fSpan(h){ return h >= 48 && h % 24 === 0 ? (h / 24) + ' days' : h + ' hours'; }
function midnights(a, b){                                                                    // NVR-local 00:00 inside (a, b)
  const out = [], p = parts(a); let m = Date.UTC(p.y, p.mo - 1, p.d + 1) - CFG.tzOffsetMin * 60000;
  for (; m < b && out.length < 62; m += 86400000) out.push(m);
  return out;
}
function toMs(dateStr, timeStr){
  const d = (dateStr || '').split('-').map(Number), t = (timeStr || '').split(':').map(Number);
  if (d.length !== 3 || d.some(isNaN) || t.length < 2 || t.some(isNaN)) return NaN;
  return Date.UTC(d[0], d[1] - 1, d[2], t[0], t[1], t[2] || 0) - CFG.tzOffsetMin * 60000;
}
function nvrNowMs(){ return CFG.nvrNowMs + (Date.now() - loadedAt); }
function setRange(a, b){ $('fromD').value = fDate(a); $('fromT').value = fTime(a); $('toD').value = fDate(b); $('toT').value = fTime(b); }
function localStr(ms){ return fDate(ms) + 'T' + fTime(ms); }

// ── camera picker ─────────────────────────────────────────────────────────
function renderPicker(){
  const f = $('camSearch').value.trim().toLowerCase(), list = $('plist');
  list.textContent = '';
  cams.forEach(c => {
    const tech = c.nvr + ' · CH ' + c.channel;
    if (f && !(c.name.toLowerCase().includes(f) || tech.toLowerCase().includes(f))) return;
    const row = document.createElement('label'); row.className = 'opt';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = sel.has(c.index);
    cb.onchange = () => { cb.checked ? sel.add(c.index) : sel.delete(c.index); renderCount(); };
    const on = document.createElement('span'); on.className = 'on';
    const b = document.createElement('b'); b.textContent = c.name;
    const s = document.createElement('small'); s.textContent = tech;
    on.append(b, s); row.append(cb, on); list.append(row);
  });
}
function renderCount(){
  const n = sel.size;
  $('camCnt').textContent = n === 0 ? 'Select cameras' : n === cams.length ? 'All cameras (' + n + ')'
    : n === 1 ? cams.find(c => sel.has(c.index)).name : n + ' cameras';
  renderKept();
}

// ── how far back: each NVR's oldest recording (the server reads it from the NVRs) ──
function keptOf(n){ const r = (CFG.retention || {})[n]; return r && r.oldestMs ? r : null; }
function fKeptDays(r){ return r.days + ' day' + (r.days === 1 ? '' : 's'); }
function keptFor(){                                         // the selected cameras' NVRs (all of them if none)
  const nvrs = [...new Set((sel.size ? cams.filter(c => sel.has(c.index)) : cams).map(c => c.nvr))].sort();
  const rs = nvrs.map(keptOf);
  if (!nvrs.length || rs.some(r => !r)) return null;         // not known for all: the server decides
  return {oldestMs: Math.min(...rs.map(r => r.oldestMs)), text: nvrs.map((n, i) => n + ' keeps ' + fKeptDays(rs[i])
    + (i ? '' : ' of footage') + ' (from ' + fLong(rs[i].oldestMs) + ' ' + fTime(rs[i].oldestMs).slice(0, 5) + ')').join(', ')};
}
function renderKept(){
  if (!CFG) return;
  const all = Object.keys(CFG.retention || {}).sort().filter(n => keptOf(n));
  $('tzn').textContent = 'Times are NVR time (' + CFG.tz + ')'
    + (all.length ? ' · footage kept: ' + all.map(n => { const r = keptOf(n);
        return n + ' ' + fKeptDays(r) + ' (from ' + fDay(r.oldestMs) + ' ' + fTime(r.oldestMs).slice(0, 5) + ')'; }).join(', ') : '')
    + (CFG.remote ? ' · outside the office: recording gaps are found while playing' : '');
  const k = keptFor();
  $('fromD').min = $('toD').min = k ? fDate(k.oldestMs) : '';
}
async function refreshKept(){                               // the NVRs lose their oldest footage every hour
  try { const r = await fetch('/api/playback/config' + q); if (r.ok){ CFG.retention = (await r.json()).retention; renderKept(); } }
  catch (e) {}
}
$('camBtn').onclick = (e) => { e.stopPropagation(); const p = $('panel'); p.hidden = !p.hidden;
  $('camBtn').setAttribute('aria-expanded', String(!p.hidden)); if (!p.hidden){ renderPicker(); $('camSearch').focus(); } };
$('panel').onclick = (e) => e.stopPropagation();
document.addEventListener('click', () => { $('panel').hidden = true; $('camBtn').setAttribute('aria-expanded', 'false'); });
$('camSearch').oninput = renderPicker;
$('selAll').onclick = () => { cams.forEach(c => sel.add(c.index)); renderPicker(); renderCount(); };
$('selNone').onclick = () => { sel.clear(); renderPicker(); renderCount(); };
document.querySelectorAll('.presets button').forEach(b => b.onclick = () => {
  const now = nvrNowMs(), m = b.dataset.min;
  if (m === 'today'){ const p = parts(now); setRange(Date.UTC(p.y, p.mo - 1, p.d) - CFG.tzOffsetMin * 60000, now); }
  else setRange(now - (+m) * 60000, now);
});

// ── search ────────────────────────────────────────────────────────────────
function showErr(msg){ $('err').hidden = !msg; $('errTx').textContent = msg || ''; }
function validate(){
  const a = toMs($('fromD').value, $('fromT').value), b = toMs($('toD').value, $('toT').value);
  if (isNaN(a) || isNaN(b)) return 'Enter a valid From and To date and time.';
  if (b <= a) return 'To time must be after From time.';
  if (a >= nvrNowMs()) return 'From time is in the future.';
  if (!sel.size) return 'Select at least one camera.';
  const k = keptFor();                                      // From earlier than that: the server starts there
  if (k && b <= k.oldestMs) return 'No recordings that old: ' + k.text + '.';
  if (!k && b - a > CFG.maxRangeH * 3600000)
    return 'The time range is too long: at most ' + fSpan(CFG.maxRangeH) + " per search (the NVRs' oldest recordings are not known yet).";
  return '';
}
async function search(){
  if (!keptFor()) await refreshKept();                      // the server may know them by now
  const bad = validate(); showErr(bad); if (bad) return;
  const a = toMs($('fromD').value, $('fromT').value), b = toMs($('toD').value, $('toT').value);
  const order = cams.filter(c => sel.has(c.index)).map(c => c.index);          // display order
  if (sel.size > (CFG.limits.server || 4) && !confirm(sel.size + ' cameras selected. At most ' + CFG.limits.server
      + ' play at the same time; the rest wait on the next pages. Continue?')) return;
  closeWs();
  $('searchBtn').disabled = true; $('searchBtn').lastChild.textContent = 'Searching recordings…';
  $('empty').hidden = false; $('empty').innerHTML = '<b>Searching recordings…</b>Asking the NVRs which footage exists.';
  try {
    const r = await fetch('/api/playback/search' + q, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({from: localStr(a), to: localStr(b), cameras: order, replaces: SID})});
    const d = await r.json().catch(() => ({}));
    if (d.retention){ CFG.retention = d.retention; renderKept(); }
    if (!r.ok || !d.ok){ showErr(d.error || ('Search failed (HTTP ' + r.status + ')')); $('empty').innerHTML = '<b>Recorded footage</b>Choose a time range and cameras, then Search recordings.'; return; }
    RES = d; SID = d.sid; page = 0; ST = null; seekTarget = null; AUD.tile = -1; AUD.muted = false;
    if (d.note){ $('fromD').value = fDate(d.fromMs); $('fromT').value = fTime(d.fromMs); }   // started at the oldest recording
    renderResults(); openWs();
  } catch (e){ showErr('The server did not answer. Check the connection and try again.'); }
  finally { $('searchBtn').disabled = false; $('searchBtn').lastChild.textContent = 'Search recordings'; }
}
$('searchBtn').onclick = search;

function chipFor(t){
  const c = document.createElement('span'), st = t.availability;
  c.className = 'chip ' + (st === 'found' ? 'ok' : st === 'partial' ? 'part' : st === 'searching' ? '' : 'no');
  c.textContent = (st === 'found' ? '✓ ' : st === 'partial' ? '◐ ' : st === 'searching' ? '… ' : '✕ ') + t.name
    + (st === 'partial' ? ' (gaps)' : st === 'none' ? ' — no recording' : st === 'unreachable' ? ' — NVR unreachable'
       : st === 'searching' ? ' — searching' : (st === 'found') ? '' : ' — unavailable');
  return c;
}
function renderResults(){
  $('summary').hidden = false; $('stage').hidden = false; $('empty').hidden = true; hideTip();
  $('sumNote').hidden = !RES.note; $('sumNote').textContent = RES.note || '';
  renderSummary(); renderSpeed(); buildTiles(); updateScrub(RES.fromMs);
}
function renderSummary(){                                   // again whenever search results arrive
  const d = RES, span = d.toMs - d.fromMs, days = midnights(d.fromMs, d.toMs);
  $('sumIv').textContent = fLong(d.fromMs) + ' · ' + fTime(d.fromMs) + ' – ' + (fDate(d.toMs) !== fDate(d.fromMs) ? fLong(d.toMs) + ' ' : '') + fTime(d.toMs) + ' ' + CFG.tz;
  const found = d.tiles.filter(t => t.availability === 'found' || t.availability === 'partial').length;
  const left = d.tiles.filter(t => t.availability === 'searching').length;
  $('sumCnt').textContent = d.tiles.length + ' camera' + (d.tiles.length === 1 ? '' : 's') + ' selected · recordings found for ' + found
    + (left ? ' · searching ' + left + ' more…' : '') + (span > 86400000 ? ' · ' + Math.round(span / 3600000) + ' h' : '');
  const chips = $('chips'); chips.textContent = ''; d.tiles.forEach(t => chips.append(chipFor(t)));
  const tl = $('tl'); tl.textContent = '';
  const dayLines = (el) => days.forEach(m => { const x = document.createElement('b'); x.className = 'day';
    x.style.left = ((m - d.fromMs) / span * 100) + '%'; el.append(x); });
  d.tiles.forEach(t => {
    if (t.availability === 'none' || t.availability === 'unreachable' || t.availability === 'error') return;
    const row = document.createElement('div'); row.className = 'tlrow';
    const n = document.createElement('span'); n.className = 'tn'; n.textContent = t.name;
    const bar = document.createElement('div'); bar.className = 'bar' + (t.segments ? '' : ' unk');
    if (t.availability === 'searching') bar.className = 'bar unk srch';
    bar.dataset.tile = t.id;                                // (the hover tip tells recorded / not)
    (t.segments || []).forEach(([s, e]) => { const i = document.createElement('i');
      i.style.left = ((s - d.fromMs) / span * 100) + '%'; i.style.width = Math.max(.3, (e - s) / span * 100) + '%';
      bar.append(i); });
    if (t.segments) dayLines(bar);
    row.append(n, bar); tl.append(row);
  });
  $('strack').querySelectorAll('.day').forEach(x => x.remove()); dayLines($('strack'));
  const ticks = $('ticks'); ticks.textContent = '';
  for (let k = 0; k <= 6; k++){ const s = document.createElement('span'); s.textContent = fStamp(d.fromMs + span * k / 6); ticks.append(s); }
}

// ── player grid ───────────────────────────────────────────────────────────
function pageTiles(){ const per = RES.tilesPerPage || 6; return RES.tiles.slice(page * per, page * per + per); }
function buildTiles(){
  const g = $('grid'); g.textContent = ''; tileEls = {};
  const list = pageTiles(), n = list.length;
  g.className = 'grid ' + (n <= 1 ? 'g1' : n === 2 ? 'g2' : n <= 4 ? 'g4' : 'g6');
  list.forEach(t => {
    const el = $('tileTpl').content.firstElementChild.cloneNode(true);
    el.querySelector('.pn').textContent = t.name;
    el.querySelector('.pm').textContent = t.nvr + ' · CH ' + t.channel;
    el.querySelector('.paud').onclick = () => toggleAudio(t.id);
    el.querySelector('.pfs').onclick = () => toggleFocus(t.id);
    el.querySelector('.pstop').onclick = () => send({op: 'stop', tile: t.id});
    el.dataset.id = t.id;
    g.append(el); tileEls[t.id] = {el, cv: el.querySelector('canvas'), over: el.querySelector('.pover'),
      stat: el.querySelector('.pstat'), ts: el.querySelector('.pts'), note: el.querySelector('.note'), pending: null, busy: false};
    renderTile(t);
  });
  const per = RES.tilesPerPage || 6, pages = Math.ceil(RES.tiles.length / per);
  $('pgnav').hidden = pages <= 1;
  $('pgTx').textContent = 'Cameras ' + (page * per + 1) + '–' + Math.min(RES.tiles.length, page * per + per) + ' of ' + RES.tiles.length;
}
$('pgPrev').onclick = () => turnPage(-1);
$('pgNext').onclick = () => turnPage(1);
function turnPage(d){
  const per = RES.tilesPerPage || 6, pages = Math.ceil(RES.tiles.length / per);
  page = (page + d + pages) % pages; buildTiles(); send({op: 'page', tiles: pageTiles().map(t => t.id)});
}
const LABEL = {SEARCHING: ['Searching recordings…', ''], READY: ['Recording found', 'ok'], OPENING: ['Opening playback…', 'warn'],
  SEEKING: ['Seeking…', 'warn'], PLAYING: ['Playing', 'ok'], PAUSED: ['Paused', ''], GAP: ['No recording', 'warn'],
  GAP_WAIT: ['Recording gap', 'warn'], ENDED: ['Playback ended', ''], ERROR: ['Playback connection failed', 'bad'],
  NVR_UNREACHABLE: ['NVR unreachable', 'bad'], NO_RECORDING: ['No recording', 'bad'], CAPACITY: ['Capacity reached', 'warn'],
  WAITING_SLOT: ['Waiting for NVR capacity', 'warn'], STOPPED: ['Stopped', ''], IDLE: ['Ready', '']};
function renderTile(t){
  const e = tileEls[t.id]; if (!e) return;
  const [lab, cls] = LABEL[t.state] || [t.state, ''];
  e.stat.textContent = lab; e.stat.className = 'pstat ' + cls;
  const showOver = t.state !== 'PLAYING' && t.state !== 'PAUSED';
  e.over.hidden = !showOver;
  if (showOver){
    e.over.textContent = '';
    const m = document.createElement('div'); m.textContent = t.msg || lab; e.over.append(m);
    const acts = document.createElement('div'); acts.className = 'acts';
    if (t.state === 'GAP' && t.gap){
      if (t.gap.prev) acts.append(btn('Previous recording (' + fTime(t.gap.prev - 2000) + ')', () => seek(t.gap.prev - 5000)));
      if (t.gap.next) acts.append(btn('Next recording (' + fTime(t.gap.next) + ')', () => seek(t.gap.next)));
    }
    if (['CAPACITY', 'ERROR', 'STOPPED', 'NVR_UNREACHABLE', 'ENDED'].includes(t.state) && ['found', 'partial'].includes(t.availability))
      acts.append(btn(t.state === 'ENDED' ? 'Play again from the start' : 'Try again', () => {
        if (t.state === 'ENDED') seek(RES.fromMs); else send({op: 'retry', tile: t.id}); }));
    if (acts.childNodes.length) e.over.append(acts);
    if (['OPENING', 'SEEKING', 'SEARCHING', 'WAITING_SLOT'].includes(t.state)){ const sp = document.createElement('span'); sp.className = 'spinner'; e.over.prepend(sp); }
  }
  const fast = ST && ST.speed !== 1, mine = AUD.tile === t.id, pa = e.el.querySelector('.paud');
  const soundDenied = t.audioAllowed === false;             // not this viewer's to hear: no speaker at all
  e.note.textContent = soundDenied ? '' : t.audio ? (mine ? (fast ? 'Audio at 1× only' : AUD.muted ? 'Audio muted' : 'Listening')
      : 'Audio available') + ' (' + t.audio + ')'
    : (t.state === 'PLAYING' || t.state === 'PAUSED') ? 'No recorded audio' : '';
  pa.hidden = soundDenied;
  pa.disabled = !t.audio;
  pa.title = !t.audio ? 'No recorded audio for this camera' : fast ? 'Listen to this camera (sound plays at 1× — fast playback has none)'
    : mine && !AUD.muted ? 'Stop listening' : 'Listen to this camera (one camera at a time)';
  e.el.querySelector('.pstop').disabled = !['PLAYING', 'PAUSED', 'OPENING', 'SEEKING', 'GAP', 'GAP_WAIT', 'WAITING_SLOT'].includes(t.state);
  if (t.recMs){ e.ts.hidden = false; e.ts.textContent = fTime(t.recMs) + ' ' + CFG.tz; }
}
function btn(text, fn){ const b = document.createElement('button'); b.className = 'sm'; b.textContent = text; b.onclick = fn; return b; }

// ── WebSocket: frames, audio, state ───────────────────────────────────────
function send(o){ if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
function closeWs(){ if (ws){ const w = ws; ws = null; try { w.close(); } catch (e) {} } audioStopAll(); }
function openWs(){
  const w = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/playback/ws?sid='
    + encodeURIComponent(SID) + (KEY ? '&key=' + encodeURIComponent(KEY) : ''));
  w.binaryType = 'arraybuffer'; ws = w;
  w.onopen = () => { WS_OK = true; wsRetry = 0; $('banner').hidden = true; send({op: 'page', tiles: pageTiles().map(t => t.id)}); };
  w.onmessage = (ev) => {
    if (ws !== w) return;
    if (typeof ev.data === 'string'){ let m = {}; try { m = JSON.parse(ev.data); } catch (e) {}
      if (m.t === 'state') applyState(m);
      else if (m.t === 'avail') applyAvail(m);
      else if (m.t === 'error'){ banner(m.msg || 'Playback session ended.'); SID = null; }
      return; }
    const b = new Uint8Array(ev.data);
    if (b[0] === 86) onFrame(b); else if (b[0] === 65) onAudio(b);
  };
  w.onclose = () => {
    if (ws !== w || !SID) return;
    ws = null;
    banner('Connection to the server lost — reconnecting…');
    setTimeout(() => { if (!ws && SID) openWs(); }, Math.min(8000, 1500 * ++wsRetry));
  };
}
function banner(msg){ $('banner').hidden = !msg; $('bannerTx').textContent = msg || ''; }
function u64(b, o){ const v = new DataView(b.buffer, b.byteOffset); return v.getUint32(o) * 4294967296 + v.getUint32(o + 4); }
function onFrame(b){
  const id = b[1], ms = u64(b, 2), e = tileEls[id]; if (!e) return;
  e.pending = {blob: new Blob([b.subarray(14)], {type: 'image/jpeg'}), ms};
  if (!e.busy) drawNext(e);
}
function drawNext(e){
  const p = e.pending; if (!p){ e.busy = false; return; }
  e.pending = null; e.busy = true;
  createImageBitmap(p.blob).then(bm => {
    if (e.cv.width !== bm.width || e.cv.height !== bm.height){ e.cv.width = bm.width; e.cv.height = bm.height; }
    e.cv.getContext('2d').drawImage(bm, 0, 0); bm.close();
    e.ts.hidden = false; e.ts.textContent = fTime(p.ms) + ' ' + CFG.tz;
  }).catch(() => {}).finally(() => drawNext(e));
}
function applyState(m){
  ST = m;
  (m.tiles || []).forEach(t => { const r = RES.tiles[t.id]; if (r) Object.assign(r, t); renderTile(t); });
  $('playBtn').querySelector('use').setAttribute('href', m.paused ? '#i-play' : '#i-pause');
  $('playTx').textContent = m.paused ? 'Play' : 'Pause'; $('playBtn').setAttribute('aria-label', m.paused ? 'Play' : 'Pause');
  const shown = seekTarget != null ? seekTarget : m.posMs;
  if (seekTarget != null && Math.abs(m.posMs - seekTarget) < 4000) seekTarget = null;
  $('nowT').textContent = fTime(shown); $('nowD').textContent = fLong(shown) + ' · ' + CFG.tz + (m.speed !== 1 ? ' · ' + m.speed + '×' : '');
  if (!dragging) updateScrub(shown);
  renderSpeed(); renderAudio();
  const cap = m.capacity || {}; document.title = (m.paused ? '❚❚ ' : '▶ ') + 'Playback · GRAV CCTV';
  (m.notes || []).forEach(n => { if (!seenNotes.has(n.t + n.text)){ seenNotes.add(n.t + n.text); toast(RES.tiles[n.tile].name + ': ' + n.text); } });
}
const seenNotes = new Set();
function applyAvail(m){                                     // recording searches that finished
  (m.tiles || []).forEach(t => { const r = RES.tiles[t.id]; if (r) Object.assign(r, t); });
  renderSummary(); (m.tiles || []).forEach(t => renderTile(RES.tiles[t.id])); renderAudio();
}
function toast(text){ const t = document.createElement('div'); t.className = 'toast'; t.textContent = text;
  t.onclick = () => t.remove(); document.body.append(t); setTimeout(() => t.remove(), 4000); }

// ── transport: play / pause / speed / seek ────────────────────────────────
$('playBtn').onclick = () => { if (!ST) return; send({op: ST.paused ? 'play' : 'pause'}); };
function renderSpeed(){
  const seg = $('speedSeg'); if (!seg.childNodes.length) (CFG.speeds || [1, 2, 4]).forEach(x => {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = x + '×'; b.dataset.x = x;
    b.title = x === 1 ? 'Normal speed' : 'Fast playback may be less smooth (key frames only, no audio)';
    b.onclick = () => send({op: 'speed', x}); seg.append(b); });
  const cur = ST ? ST.speed : 1;
  seg.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(+b.dataset.x === cur)));
  $('speedNote').hidden = cur === 1;
}
function seek(ms){ if (!RES) return; ms = Math.max(RES.fromMs, Math.min(RES.toMs - 1000, ms)); seekTarget = ms; updateScrub(ms);
  $('nowT').textContent = fTime(ms); send({op: 'seek', ms}); }
function updateScrub(ms){
  if (!RES) return; const f = Math.max(0, Math.min(1, (ms - RES.fromMs) / (RES.toMs - RES.fromMs)));
  $('sfill').style.width = (f * 100) + '%'; $('sknob').style.left = (f * 100) + '%';
  $('strack').setAttribute('aria-valuetext', fStamp(ms));
}
function msAt(ev){ const r = $('strack').getBoundingClientRect(); const f = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
  return RES.fromMs + f * (RES.toMs - RES.fromMs); }
$('strack').addEventListener('pointerdown', (ev) => { if (!RES) return; dragging = true; $('strack').setPointerCapture(ev.pointerId); scrubTo(ev); });
$('strack').addEventListener('pointermove', (ev) => { if (dragging) scrubTo(ev); else showTip($('strack'), ev.clientX); });
$('strack').addEventListener('pointerup', (ev) => { if (!dragging) return; dragging = false; seek(msAt(ev));
  if (ev.pointerType !== 'mouse') hideTip(); });
$('strack').addEventListener('pointercancel', () => { dragging = false; hideTip(); });
$('strack').addEventListener('pointerleave', () => { if (!dragging) hideTip(); });
$('strack').addEventListener('keydown', (ev) => { if (!ST) return;            // Shift: 10 minutes
  const step = ev.shiftKey ? 600000 : 10000;
  if (ev.key === 'ArrowRight'){ seek((seekTarget || ST.posMs) + step); ev.preventDefault(); }
  if (ev.key === 'ArrowLeft'){ seek((seekTarget || ST.posMs) - step); ev.preventDefault(); } });
function scrubTo(ev){ updateScrub(msAt(ev)); showTip($('strack'), ev.clientX); }   // tip shown while dragging; no seek until release

// ── hover: the date and time under the mouse, on the Timeline and on every camera's bar ──
let tipTimer = null;
function showTip(el, clientX){                             // el: the track or a camera bar (the same time axis)
  if (!RES) return;
  const r = el.getBoundingClientRect(), box = $('summary').getBoundingClientRect();
  if (!r.width) return;
  const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width)), ms = RES.fromMs + f * (RES.toMs - RES.fromMs);
  const t = el.dataset.tile ? RES.tiles.find(x => String(x.id) === el.dataset.tile) : null;
  $('tlTipT').textContent = fLong(ms) + ' · ' + fTime(ms);
  $('tlTipS').hidden = !(t && t.segments && !t.segments.some(([s, e]) => s <= ms && ms < e));   // a gap of this camera
  const tip = $('tlTip'); tip.hidden = false;
  const x = r.left + f * r.width - box.left, half = tip.offsetWidth / 2;
  tip.style.left = Math.max(half, Math.min(box.width - half, x)) + 'px';
  tip.style.top = (r.top - box.top - 6) + 'px';
  const first = $('tl').querySelector('.bar'), top = (first || $('strack')).getBoundingClientRect().top;
  const ln = $('tlLine'); ln.hidden = false;                // a guide line through every row at that moment
  ln.style.left = x + 'px'; ln.style.top = (top - box.top) + 'px'; ln.style.height = ($('strack').getBoundingClientRect().bottom - top) + 'px';
}
function hideTip(){ clearTimeout(tipTimer); $('tlTip').hidden = true; $('tlLine').hidden = true; }
$('tl').addEventListener('pointermove', (ev) => { const bar = ev.target.closest('.bar'); if (bar) showTip(bar, ev.clientX); else hideTip(); });
$('tl').addEventListener('pointerleave', (ev) => { if (ev.pointerType === 'mouse') hideTip(); });
$('tl').addEventListener('pointerdown', (ev) => { const bar = ev.target.closest('.bar');      // touch: shown for a moment
  if (bar && ev.pointerType !== 'mouse'){ showTip(bar, ev.clientX); clearTimeout(tipTimer); tipTimer = setTimeout(hideTip, 2500); } });
document.addEventListener('keydown', (ev) => { if (ev.target.tagName === 'INPUT') return;
  if (ev.key === ' ' && ST){ ev.preventDefault(); send({op: ST.paused ? 'play' : 'pause'}); } });

// ── fullscreen of one camera (controls and timeline stay) ──────────────────
function toggleFocus(id){
  const st = $('stage');
  if (st.classList.contains('focus') && st.dataset.fo == id){ unfocus(); if (document.fullscreenElement) document.exitFullscreen(); return; }
  st.classList.add('focus'); st.dataset.fo = id;
  Object.values(tileEls).forEach(e => e.el.classList.toggle('fo', e.el.dataset.id == id));
  send({op: 'focus', tile: id});
  if (st.requestFullscreen) st.requestFullscreen().catch(() => {});
}
function unfocus(){ const st = $('stage'); st.classList.remove('focus'); delete st.dataset.fo;
  Object.values(tileEls).forEach(e => e.el.classList.remove('fo')); send({op: 'focus', tile: -1}); }
document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement && $('stage').classList.contains('focus')) unfocus(); });

// ── audio: one camera at a time, off until the speaker is clicked (browser autoplay
// rules). Recorded sound exists at 1× only: in fast playback the NVRs send NO audio at
// all (measured: 0 audio packets at Scale 2 and 4 on both NVRs) -- the choice is kept
// and the sound comes back by itself at 1×. Many CCTV microphones record very quietly
// (NVR1: typically -50 to -58 dBFS), so quiet sound is raised automatically (up to
// +30 dB towards -24 dBFS) with a limiter in front of the volume. ───────────────────
const AUD = {tile: -1, muted: false, ctx: null, boost: null, comp: null, gain: null, meter: null, mbuf: null,
  next: 0, srcs: [], volume: 50, up: 1, lastPkt: 0, packets: 0, lv: [], inDb: -120, boostDb: 0, outDb: -120};
try { const v = parseInt(localStorage.getItem('cctv-volume'), 10); if (v >= 0 && v <= 100) AUD.volume = v; } catch (e) {}
$('vol').value = String(AUD.volume);
$('vol').oninput = (e) => { AUD.volume = +e.target.value; try { localStorage.setItem('cctv-volume', String(AUD.volume)); } catch (err) {} applyGain(); };
const ULAW = new Float32Array(256), ALAW = new Float32Array(256);
for (let i = 0; i < 256; i++){
  const u = ~i & 0xFF, s = ((((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7)) - 0x84;
  ULAW[i] = ((u & 0x80) ? -s : s) / 32768;
  const a = i ^ 0x55, e = (a >> 4) & 7, m = a & 0x0F, t = e ? (((m << 4) + 0x108) << (e - 1)) : ((m << 4) + 8);
  ALAW[i] = ((a & 0x80) ? t : -t) / 32768;
}
function audioCtx(){
  if (!AUD.ctx){ const C = window.AudioContext || window.webkitAudioContext, ctx = AUD.ctx = new C();
    AUD.boost = ctx.createGain(); AUD.comp = ctx.createDynamicsCompressor(); AUD.gain = ctx.createGain();
    AUD.meter = ctx.createAnalyser(); AUD.meter.fftSize = 2048; AUD.mbuf = new Float32Array(AUD.meter.fftSize);
    const c = AUD.comp;                  // limiter: a loud event right after a quiet (raised) stretch
    c.threshold.value = -6; c.knee.value = 4; c.ratio.value = 20; c.attack.value = 0.003; c.release.value = 0.25;
    AUD.boost.connect(c); c.connect(AUD.gain); AUD.gain.connect(AUD.meter); AUD.meter.connect(ctx.destination);
    try { ctx.createBuffer(1, 8, 8000); AUD.up = 1; } catch (e) { AUD.up = 3; } }
  if (AUD.ctx.state !== 'running') AUD.ctx.resume().catch(() => {});
  return AUD.ctx;
}
function applyGain(){ if (AUD.gain) AUD.gain.gain.setTargetAtTime(AUD.volume / 50, AUD.ctx.currentTime, 0.01); }
function flushAudio(){ AUD.srcs.forEach(x => { try { x.stop(); } catch (e) {} }); AUD.srcs = []; AUD.next = 0; }
function resetLevels(){ AUD.lv = []; AUD.boostDb = 0; AUD.inDb = -120; AUD.outDb = -120; AUD.lastPkt = 0;
  if (AUD.boost) AUD.boost.gain.value = 1; }
function audioStopAll(){ AUD.tile = -1; AUD.muted = false; flushAudio(); resetLevels(); }
function wantedAudio(){ return AUD.tile >= 0 && !AUD.muted ? AUD.tile : -1; }
function audioChanged(){ flushAudio(); resetLevels(); send({op: 'audio', tile: wantedAudio()}); renderAudio();
  if (RES) pageTiles().forEach(t => renderTile(t)); }
function toggleAudio(id){                                   // a tile's speaker: listen to THIS camera
  audioCtx(); applyGain();                                  // inside the click: the browser allows sound
  if (AUD.tile === id && !AUD.muted) AUD.tile = -1; else { AUD.tile = id; AUD.muted = false; }
  audioChanged();
}
function defaultAudioTile(){                                // the transport speaker with no camera chosen
  const vis = pageTiles(), fo = $('stage').dataset.fo, pool = fo != null ? vis.filter(t => String(t.id) === fo) : vis;
  const t = pool.find(t => t.audio && t.state === 'PLAYING') || pool.find(t => t.audio) || vis.find(t => t.audio);
  return t ? t.id : -1;
}
$('audBtn').onclick = () => {                               // the transport speaker: listen / mute / unmute
  audioCtx(); applyGain();
  if (AUD.tile < 0){ const id = defaultAudioTile(); if (id < 0) return; AUD.tile = id; AUD.muted = false; }
  else AUD.muted = !AUD.muted;
  audioChanged();
};
function measureOut(){                                      // what reaches the speakers (after boost + volume)
  if (!AUD.meter) return;
  AUD.meter.getFloatTimeDomainData(AUD.mbuf); let s = 0; for (let i = 0; i < AUD.mbuf.length; i++) s += AUD.mbuf[i] * AUD.mbuf[i];
  const db = 10 * Math.log10(s / AUD.mbuf.length + 1e-12); AUD.outDb = Math.max(db, AUD.outDb - 6);    // fast up, slow down
}
function renderAudio(){
  const box = $('abox'); if (!RES){ box.hidden = true; return; }
  // only cameras whose sound this viewer may hear take part (per-person CCTV permissions)
  const vis = pageTiles().filter(t => t.audioAllowed !== false), sel = AUD.tile >= 0 ? RES.tiles[AUD.tile] : null, tx = $('audTx'), btn = $('audBtn');
  if (!vis.length && !sel){ box.hidden = true; return; }
  let msg, warn = false, on = false;
  if (sel){
    const nm = sel.name;
    if (AUD.muted) msg = 'Muted · ' + nm;
    else if (ST && ST.speed !== 1){ msg = 'Audio plays at 1× only (fast playback has no sound) · ' + nm; warn = on = true; }
    else if (ST && ST.paused){ msg = 'Paused · ' + nm; on = true; }
    else if (!sel.audio){ on = true; msg = sel.state === 'PLAYING' ? 'No recorded audio · ' + nm : 'Audio · ' + nm + ' · waiting for the camera…'; }
    else if (AUD.ctx && AUD.ctx.state !== 'running'){ msg = 'Click the speaker to allow sound'; warn = true; }
    else if (performance.now() - AUD.lastPkt > 1500){ msg = 'Waiting for audio… · ' + nm; on = true; }
    else { on = true; msg = 'Playing · ' + nm + ' · ' + (AUD.outDb > -80 ? Math.round(AUD.outDb) + ' dB' : 'no sound')
      + (AUD.boostDb >= 3 ? ' · quiet microphone raised +' + Math.round(AUD.boostDb) + ' dB' : ''); }
  } else if (vis.some(t => t.audio)) msg = ST && ST.speed !== 1 ? 'Audio available at 1× playback' : 'Audio available — click the speaker to listen';
  else if (vis.some(t => t.state === 'PLAYING' || t.state === 'PAUSED')) msg = 'No recorded audio for these cameras';
  else msg = 'Audio: waiting for the cameras…';
  box.hidden = false; tx.textContent = msg; tx.classList.toggle('warn', warn);
  const live = on && !AUD.muted;
  btn.classList.toggle('on', live); btn.querySelector('use').setAttribute('href', live ? '#i-vol' : '#i-mute');
  btn.title = AUD.tile < 0 ? 'Listen' : AUD.muted ? 'Unmute' : 'Mute'; btn.setAttribute('aria-label', btn.title);
  btn.disabled = AUD.tile < 0 && !vis.some(t => t.audio);
  Object.values(tileEls).forEach(e => { const mine = +e.el.dataset.id === AUD.tile && !AUD.muted;
    e.el.classList.toggle('aud', mine); const b = e.el.querySelector('.paud'); b.classList.toggle('on', mine);
    b.querySelector('use').setAttribute('href', mine ? '#i-vol' : '#i-mute'); });
  if (ST && ST.audioTile !== undefined && ST.audioTile !== wantedAudio()) send({op: 'audio', tile: wantedAudio()});
}
setInterval(() => { if (RES && AUD.tile >= 0){ measureOut(); renderAudio(); } }, 400);
function onAudio(b){
  if (b[1] !== AUD.tile || AUD.muted || !AUD.ctx || (ST && (ST.paused || ST.speed !== 1))) return;
  const ctx = AUD.ctx, tab = b[2] === 8 ? ALAW : ULAW, n = b.length - 15, up = AUD.up;
  if (n <= 0) return;
  const buf = ctx.createBuffer(1, n * up, 8000 * up), ch = buf.getChannelData(0);
  let sq = 0;
  for (let k = 0; k < n; k++){ const v = tab[b[15 + k]]; sq += v * v; for (let r = 0; r < up; r++) ch[k * up + r] = v; }
  const t = performance.now(); AUD.lastPkt = t; AUD.packets++;
  AUD.lv.push([t, 10 * Math.log10(sq / n + 1e-12)]); while (AUD.lv.length && t - AUD.lv[0][0] > 3000) AUD.lv.shift();
  const lv = AUD.lv.map(x => x[1]).sort((x, y) => x - y); AUD.inDb = lv[Math.floor(lv.length * 0.8)];
  const boost = Math.max(0, Math.min(30, -24 - AUD.inDb));
  if (Math.abs(boost - AUD.boostDb) >= 1){ AUD.boostDb = boost; AUD.boost.gain.setTargetAtTime(Math.pow(10, boost / 20), ctx.currentTime, 0.5); }
  const now = ctx.currentTime;
  if (AUD.next < now + 0.02) AUD.next = now + 0.3;
  else if (AUD.next > now + 1.0){ flushAudio(); AUD.next = now + 0.3; }
  const ahead = AUD.next - now, rate = ahead > 0.55 ? 1.02 : ahead < 0.2 ? 0.98 : 1;
  const src = ctx.createBufferSource(); src.buffer = buf; src.playbackRate.value = rate; src.connect(AUD.boost);
  src.start(AUD.next); AUD.srcs.push(src);
  src.onended = () => { const k = AUD.srcs.indexOf(src); if (k >= 0) AUD.srcs.splice(k, 1); };
  AUD.next += n / 8000 / rate;
}
window.cctvAudioStats = () => ({context: AUD.ctx ? AUD.ctx.state : null, tile: AUD.tile, muted: AUD.muted, volume: AUD.volume,
  packets: AUD.packets, lastPacketAgoMs: AUD.lastPkt ? Math.round(performance.now() - AUD.lastPkt) : null,
  inputDb: Math.round(AUD.inDb), boostDb: Math.round(AUD.boostDb), outputDb: Math.round(AUD.outDb), queued: AUD.srcs.length,
  status: $('audTx').textContent});

// ── leaving: stop the playback on the server at once (it would also stop after a grace period) ──
window.addEventListener('pagehide', () => {
  if (SID && navigator.sendBeacon) navigator.sendBeacon('/api/playback/close' + q, new Blob([JSON.stringify({sid: SID})], {type: 'application/json'}));
});

// ── start ─────────────────────────────────────────────────────────────────
fetch('/api/playback/config' + q).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).then(c => {
  CFG = c; loadedAt = Date.now(); cams = c.cameras;
  // Who is looking: the camera list above is already only what they may play back.
  const v = c.viewer || {};
  $('navSettings').hidden = !v.admin;
  $('navLive').hidden = !v.admin && !v.live;
  $('who').hidden = !v.signOut; $('whoName').textContent = v.name || v.email || ''; $('whoName').title = v.email || '';
  if (!cams.length){
    $('empty').innerHTML = '<b>No recorded playback assigned</b>No cameras with recorded playback have been assigned to your account.';
    $('searchBtn').disabled = true;
  }
  const now = nvrNowMs(); setRange(now - 3600000, now);
  renderCount();                                            // (+ the footage-kept hint)
  if (!keptFor()) setTimeout(refreshKept, 20000);           // checked right after a server start
  setInterval(refreshKept, 10 * 60000);
}).catch(e => showErr('Could not load the playback settings (' + e.message + ').'));
</script>
</body>
</html>
""")
