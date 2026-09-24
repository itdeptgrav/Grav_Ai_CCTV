"""Standalone CCTV web server (live viewing).

Pulls RTSP from the NVRs on demand and serves an MJPEG camera grid over HTTP, so
a browser can watch without any camera credentials. One shared RTSP connection
per camera, kept alive only while someone is watching; per-NVR connection limit.

VIEWER MODEL
------------
A viewer IS an open HTTP /stream connection. When the browser closes it (tab
close, navigation, page change, clearing img.src), the server's write fails and
the viewer is removed at once — there is no separate heartbeat/beacon to get
wrong. A camera's worker stops as soon as its last viewer leaves.

NVR SLOT LIFECYCLE (the important part)
---------------------------------------
Each NVR allows a limited number of simultaneous RTSP pulls (CCTV_NVR_MAX_CONN).
A worker holds a slot ONLY while it actually has a stream open, and releases it:
  - the moment its last viewer leaves (worker exits, releases immediately), and
  - after every failed/lost connection, BEFORE backing off — so an offline or
    slow camera never hogs a slot and starve others.
A worker also does not even try (and never takes a slot) while its NVR is
unreachable, per the shared NetworkMonitor.

TIME-TO-FIRST-FRAME (measured, not guessed)
-------------------------------------------
OpenCV's FFmpeg backend serializes RTSP opens process-wide and, by default, lets
a dead channel block ~30 s on its internal interrupt timeout. Two things fix the
"all tiles stuck on Connecting..." delay:
  - a per-capture OPEN/READ timeout passed as VideoCapture CONSTRUCTOR params
    (CAP_PROP_OPEN_TIMEOUT_MSEC/READ) -- the only form this build honors -- so a
    dead camera fails in ~6 s, not ~30 s, and stops blocking the ones behind it;
  - a global CONNECT_GATE around the open only, plus progressive backoff on open
    failure, so a dead channel drifts to the back of the serialized open queue and
    healthy cameras appear progressively (first one in ~2.5-4.5 s) instead of all
    waiting on the slowest/dead one. See FINAL_CCTV_PERFORMANCE_REPORT.txt.

Host-ready: all settings come from the environment (see .env.example). Run:

    python server.py

then open  http://<server>:<CCTV_PORT>/?key=<CCTV_TOKEN>
"""
import os
import sys
import time
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Load .env (optional dependency) before anything reads the environment.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    # Force RTSP-over-TCP. NOTE (measured): the FFmpeg 'stimeout'/'timeout'/
    # 'rw_timeout' options are NOT honored by this OpenCV build for the OPEN phase
    # -- a dead channel blocks on OpenCV's hardcoded 30 000 ms interrupt timeout
    # regardless. The real open/read timeout is therefore set per-capture through
    # CAP_PROP_OPEN_TIMEOUT_MSEC / CAP_PROP_READ_TIMEOUT_MSEC (see _open_capture),
    # which IS honored, but only when passed as VideoCapture constructor params.
    "rtsp_transport;tcp",
)

import cv2
import numpy as np

from nvr_config import (
    CAMERAS, NVRS, CCTV_SUBNET, NETWORK_CHECK_INTERVAL, NETWORK_TIMEOUT,
    make_url, resolve_nvr_ips, endpoint,
)
from netcheck import NetworkMonitor


def _envint(name, default):
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


PORT         = _envint("CCTV_PORT", 8000)
TOKEN        = os.getenv("CCTV_TOKEN", "change-me-to-a-strong-secret")  # real value in .env; "" disables the gate
STREAM_W     = _envint("CCTV_STREAM_W", 640)
STREAM_H     = _envint("CCTV_STREAM_H", 360)
STREAM_FPS   = _envint("CCTV_STREAM_FPS", 8)
JPEG_QUALITY = _envint("CCTV_JPEG_QUALITY", 70)
IDLE_TIMEOUT = _envint("CCTV_IDLE_TIMEOUT", 60)
NVR_MAX_CONN = _envint("CCTV_NVR_MAX_CONN", 6)
# Per-capture open/read timeout (ms). MEASURED: a healthy nvr1 substream opens in
# ~2.5 s and a warm nvr2 substream in ~4.0-4.4 s, BUT some nvr2 channels need
# 6-7 s on their very first (cold) open. The timeout MUST stay above the slowest
# healthy open or real cameras get killed mid-handshake and waste a whole retry;
# 8000 covers the cold outliers while still failing a truly dead channel in ~8 s
# instead of OpenCV's hardcoded ~30 s default. Healthy first-frame time is set by
# the NVR handshake (~2.5-4.5 s), not by this value -- raising it does not slow a
# camera that is actually there; it only bounds how long a dead one may block.
OPEN_TIMEOUT_MS = _envint("CCTV_OPEN_TIMEOUT_MS", 8000)
READ_TIMEOUT_MS = _envint("CCTV_READ_TIMEOUT_MS", 8000)
# How many RTSP opens may be in-flight at once, process-wide. MEASURED: OpenCV's
# FFmpeg backend serializes opens globally, so >1 gives no speed-up; the gate just
# makes that serialization explicit and keeps one slow open from stampeding.
CONNECT_MAX  = _envint("CCTV_CONNECT_MAX", 1)
# Optional per-camera startup timing to stdout (open/first-frame ms). Off by default.
DEBUG_TIMING = os.getenv("CCTV_DEBUG_TIMING", "").lower() in ("1", "true", "yes", "on")

# one connection-limiter per NVR, shared by all camera threads
NVR_SEM = {k: threading.BoundedSemaphore(NVR_MAX_CONN) for k in NVRS}
# live count of held slots per NVR, for /api/status (BoundedSemaphore hides it)
NVR_ACTIVE = {k: 0 for k in NVRS}
_ACTIVE_LOCK = threading.Lock()

# Global gate serializing the RTSP OPEN handshake (see CONNECT_MAX). Held ONLY
# across cv2.VideoCapture(...) open, never during streaming, so a live camera
# never blocks another camera's frames -- only their initial connect.
CONNECT_GATE = threading.BoundedSemaphore(max(1, CONNECT_MAX))

# Fair open ordering. Because opens are serialized, a dead channel whose worker
# reaches the gate first would make every healthy camera behind it wait out its
# full open timeout. So a camera whose PREVIOUS open failed yields the gate (up to
# ~2 s) to any camera that has NOT just failed -- the "failed camera goes to the
# back of the queue" rule -- while a never-failed / previously-live camera keeps
# priority. _FRESH_WAITING counts fresh cameras currently queued for the gate.
_ORDER_LOCK    = threading.Lock()
_FRESH_WAITING = 0
# Channels that failed to open recently, remembered ACROSS worker restarts (index
# -> monotonic time) so a known-dead channel yields the gate even on the first
# attempt of a fresh page view, not only on its own in-session retries.
_LAST_FAIL     = {}
_FAIL_WINDOW   = 60.0


def _fresh_wait(delta):
    global _FRESH_WAITING
    with _ORDER_LOCK:
        _FRESH_WAITING += delta


def _fresh_pending():
    with _ORDER_LOCK:
        return _FRESH_WAITING > 0


def _note_open_fail(index):
    with _ORDER_LOCK:
        _LAST_FAIL[index] = time.monotonic()


def _clear_open_fail(index):
    with _ORDER_LOCK:
        _LAST_FAIL.pop(index, None)


def _recently_failed(index):
    with _ORDER_LOCK:
        t = _LAST_FAIL.get(index)
    return t is not None and (time.monotonic() - t) < _FAIL_WINDOW


def _cap_open_params():
    """Open/read timeouts as VideoCapture constructor params -- the ONLY form this
    build honors (a post-open cap.set() or an FFmpeg 'stimeout' is ignored for the
    open phase). Returns [] on builds without the properties (e.g. the test stub)."""
    p = []
    if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
        p += [int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), OPEN_TIMEOUT_MS]
    if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
        p += [int(cv2.CAP_PROP_READ_TIMEOUT_MSEC), READ_TIMEOUT_MS]
    return p

# shared reachability monitor; workers consult it before taking a slot
MONITOR = NetworkMonitor(
    NVRS, CCTV_SUBNET,
    interval=NETWORK_CHECK_INTERVAL, timeout=NETWORK_TIMEOUT, endpoint_fn=endpoint,
)


def _slot_try_acquire(nvr):
    """Try to take an NVR slot (waits up to 1s). Returns True if taken."""
    if NVR_SEM[nvr].acquire(timeout=1.0):
        with _ACTIVE_LOCK:
            NVR_ACTIVE[nvr] += 1
        return True
    return False


def _slot_release(nvr):
    NVR_SEM[nvr].release()
    with _ACTIVE_LOCK:
        NVR_ACTIVE[nvr] = max(0, NVR_ACTIVE[nvr] - 1)


class CamStream:
    """One shared RTSP connection per camera, alive only while >=1 viewer watches."""
    def __init__(self, info, index):
        self.name     = info["name"]
        self.info     = info
        self.index    = index
        # The latest frame is cached ALREADY JPEG-encoded (encode once per decoded
        # frame, in the worker) and shared to every viewer as immutable bytes, so
        # N viewers cost one encode, not N. None means "no frame yet".
        self.jpeg_bytes = None
        self.status   = "Idle"
        self.viewers  = 0
        self.last_use = time.time()
        self._gen     = 0
        self._running = False
        self._vlock   = threading.Lock()
        self._flock   = threading.Lock()

    @property
    def url(self):
        return make_url(self.info["nvr"], self.info["channel"])

    def _log(self, gen, msg):
        if DEBUG_TIMING:
            print(f"[{time.strftime('%H:%M:%S')}] cam{self.index:2d} "
                  f"{self.name[:20]:20} g{gen} {msg}", flush=True)

    def _open_capture(self):
        """Open the RTSP stream with the honored open/read timeout, buffer of 1."""
        params = _cap_open_params()
        cap = (cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, params) if params
               else cv2.VideoCapture(self.url, cv2.CAP_FFMPEG))
        try:
            if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # keep only the newest frame
        except Exception:
            pass
        return cap

    def _open_with_priority(self, gen, is_retry):
        """Open the RTSP stream through the serialized connect gate, giving fresh
        cameras priority over ones that just failed. Returns the (opened-or-not)
        VideoCapture, or None if the viewer left before we could open."""
        if is_retry:
            # A just-failed camera steps aside until EVERY fresh camera waiting to
            # open has gone first -- otherwise a dead channel that grabs the single
            # open slot holds it for the full ~8 s timeout and stalls the healthy
            # cameras behind it. The cap is only a safety backstop (fresh cameras on
            # a page drain within ~30 s); it re-checks liveness so it exits at once
            # if the viewer leaves.
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and self._current(gen) and _fresh_pending():
                time.sleep(0.1)
            counted = False
        else:
            _fresh_wait(1)          # announce ourselves as a fresh camera waiting
            counted = True
        CONNECT_GATE.acquire()
        if counted:
            _fresh_wait(-1)         # holding the gate now; no longer "waiting"
        try:
            if not self._current(gen):
                return None
            return self._open_capture()
        finally:
            CONNECT_GATE.release()

    def add_viewer(self):
        with self._vlock:
            self.viewers += 1
            n = self.viewers
            self.last_use = time.time()
            start = not self._running
            if start:
                self._running = True
                self._gen += 1
                gen = self._gen
        if start:
            threading.Thread(target=self._run, args=(gen,), daemon=True).start()
        return n

    def remove_viewer(self):
        with self._vlock:
            if self.viewers > 0:
                self.viewers -= 1
            if self.viewers == 0 and self._running:
                self._running = False   # signal the worker to exit now

    def _current(self, gen):
        with self._vlock:
            return self._running and gen == self._gen

    def _set_status(self, gen, s):
        with self._vlock:
            if gen == self._gen:
                self.status = s

    def _run(self, gen):
        nvr = self.info["nvr"]
        fail_count = 0            # consecutive OPEN failures -> escalating backoff
        self._log(gen, "worker start")
        try:
            while self._current(gen):
                # 1. Skip entirely (take NO slot) if the NVR is unreachable.
                h = MONITOR.get(nvr)
                if h is not None and h.checked and not h.reachable:
                    self._set_status(gen, h.label)
                    time.sleep(2)
                    continue

                # 2. Take an NVR active-stream slot for THIS attempt only.
                self._set_status(gen, "Connecting...")
                got = False
                while self._current(gen) and not got:
                    if _slot_try_acquire(nvr):
                        got = True
                    else:
                        self._set_status(gen, "Waiting for NVR slot...")
                if not got:
                    break   # viewer left while waiting

                cap = None
                live = False
                try:
                    # 3. Open the stream through the serialized, fresh-first connect
                    #    gate (OpenCV serializes opens process-wide anyway); the gate
                    #    is held ONLY across the open, never during streaming, and a
                    #    just-failed camera yields it to healthy ones.
                    t0 = time.time()
                    is_retry = fail_count > 0 or _recently_failed(self.index)
                    cap = self._open_with_priority(gen, is_retry)
                    if cap is None:
                        break                   # viewer left while we waited
                    opened = cap.isOpened()
                    t_open = time.time()

                    if not opened:
                        _note_open_fail(self.index)
                        self._set_status(gen, "OFFLINE")
                        self._log(gen, f"OPEN-FAIL after {t_open - t0:.2f}s")
                    else:
                        _clear_open_fail(self.index)
                        live = True
                        self._set_status(gen, "LIVE")
                        self._log(gen, f"opened in {t_open - t0:.2f}s")
                        first = True
                        while self._current(gen):
                            ok, frame = cap.read()
                            if not ok or frame is None:
                                self._set_status(gen, "Reconnecting...")
                                break
                            small = cv2.resize(frame, (STREAM_W, STREAM_H))
                            okj, buf = cv2.imencode(
                                ".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                            if okj:
                                data = buf.tobytes()
                                with self._flock:
                                    if gen == self._gen:
                                        self.jpeg_bytes = data
                                if first:
                                    first = False
                                    now = time.time()
                                    self._log(gen, f"FIRST FRAME +{now - t_open:.2f}s "
                                                   f"after open ({now - t0:.2f}s total)")
                finally:
                    if cap is not None:
                        cap.release()
                    _slot_release(nvr)   # ALWAYS free the slot after each attempt

                # 4. Back off between attempts WITHOUT holding a slot or the gate.
                #    A stream that was LIVE and merely dropped retries fast; one that
                #    fails to OPEN backs off progressively (1,2,3,4,5 s cap) so a dead
                #    channel drifts to the back of the queue and stops delaying the
                #    healthy cameras behind it on the serialized open path.
                if not self._current(gen):
                    break
                if live:
                    fail_count = 0
                    time.sleep(0.4)
                else:
                    fail_count += 1
                    time.sleep(min(1.0 * fail_count, 5.0))
        finally:
            with self._flock:
                if gen == self._gen:
                    self.jpeg_bytes = None
            with self._vlock:
                if gen == self._gen and not self._running:
                    self.status = "Idle"

    def jpeg(self):
        # Already-encoded bytes, shared to every viewer (encode happens once, in
        # the worker). bytes are immutable so no copy/lock-during-send is needed.
        with self._flock:
            return self.jpeg_bytes

    def jpeg_or_status(self):
        jpg = self.jpeg()
        if jpg:
            return jpg
        ph = np.zeros((STREAM_H, STREAM_W, 3), dtype=np.uint8)
        ph[:] = (30, 30, 40)
        cv2.putText(ph, self.status, (20, STREAM_H // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (150, 150, 170), 2)
        cv2.putText(ph, self.name, (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 220), 1)
        ok, buf = cv2.imencode(".jpg", ph)
        return buf.tobytes() if ok else None

    def force_stop(self):
        with self._vlock:
            self._running = False


STREAMS = [CamStream(c, i) for i, c in enumerate(CAMERAS)]


def reaper():
    """Failsafe only: stop a worker still running with no viewers (a handler that
    died without cleanup) after IDLE_TIMEOUT. Normal cleanup is immediate."""
    while True:
        time.sleep(5)
        now = time.time()
        for s in STREAMS:
            with s._vlock:
                leaked = s._running and s.viewers <= 0
                stale  = now - s.last_use > IDLE_TIMEOUT
            if leaked and stale:
                s.force_stop()


def system_status():
    with _ACTIVE_LOCK:
        nvrs = {k: {"active": NVR_ACTIVE[k], "max": NVR_MAX_CONN} for k in NVRS}
    cams = []
    for s in STREAMS:
        cams.append({"index": s.index, "name": s.name, "nvr": s.info["nvr"],
                     "viewers": s.viewers, "status": s.status})
    return {"nvrs": nvrs, "cameras": cams}


PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>CCTV</title>
<style>
 body{background:#14141c;color:#ddd;font-family:system-ui,sans-serif;margin:0;padding:10px}
 #top{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
 #top b{font-size:15px}
 .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
 @media(max-width:700px){.grid{grid-template-columns:repeat(2,1fr)}}
 .cam{position:relative;background:#000;border:1px solid #333;border-radius:5px;overflow:hidden;cursor:pointer;aspect-ratio:16/9}
 .cam:hover{border-color:#0c8}
 .cam img{width:100%;height:100%;object-fit:cover;display:block}
 .cam span{position:absolute;top:0;left:0;right:0;background:rgba(0,0,0,.65);
           font-size:12px;padding:3px 6px;white-space:nowrap;overflow:hidden}
 #view{position:fixed;inset:0;background:#000;display:none;flex-direction:column;z-index:9}
 #view img{flex:1;object-fit:contain;min-height:0}
 #bar{background:#1e1e28;padding:10px;display:flex;gap:12px;align-items:center}
 button{background:#333;color:#ddd;border:1px solid #555;border-radius:4px;padding:6px 14px;cursor:pointer}
 button:hover{border-color:#0c8}
 .hint{color:#888;font-size:12px}
</style>
<div id=top>
  <button onclick="page(-1)">&larr; Prev (P)</button>
  <b id=pageinfo>-</b>
  <button onclick="page(1)">Next (N) &rarr;</button>
  <span class=hint>tap a camera for live video</span>
</div>
<div class=grid id=grid></div>
<div id=view><div id=bar><button onclick="close_()">&larr; Back</button><span id=title></span></div><img id=live></div>
<script>
const PER  = 6;
const KEY  = new URLSearchParams(location.search).get('key') || '';
const q    = KEY ? '?key='+encodeURIComponent(KEY) : '';
let cams = [], pg = 0;

fetch('/api/cameras'+q).then(r=>r.json()).then(list=>{ cams = list; draw(); });

function pages(){ return Math.max(1, Math.ceil(cams.length/PER)); }
function streamUrl(i){ return '/stream/'+i+q; }

/* FIXED CELLS. We create PER <img> cells ONCE and only change their src. Changing
   (or clearing) an <img>'s src reliably ABORTS its current MJPEG connection, so a
   camera that scrolls off the page / is stopped for fullscreen releases its NVR
   slot at once. (Rebuilding the grid via innerHTML used to destroy <img> elements
   WITHOUT aborting their streams — the browser kept streaming to detached images,
   leaking viewers and NVR slots. This is the fix.) */
let cells = [];
let fullscreen = false;

function buildCells(){
  const grid = document.getElementById('grid');
  grid.innerHTML = Array.from({length: PER}, () =>
    `<div class=cam><img><span></span></div>`).join('');
  cells = [...grid.querySelectorAll('.cam')].map((cell) => {
    const c = { cell, img: cell.querySelector('img'), span: cell.querySelector('span'), idx: null };
    cell.onclick = () => { if (c.idx != null) open_(c.idx); };
    // server-restart / transient recovery: retry this cell's own camera
    c.img.onerror = () => {
      const i = c.idx;
      setTimeout(() => {
        if (!fullscreen && c.idx === i && i != null) c.img.src = streamUrl(i) + '&_r=' + Date.now();
      }, 2000);
    };
    return c;
  });
}

// Abort every grid stream now, so their browser connection slots and NVR slots
// are released. Returns nothing. (An MJPEG <img> holds one of the browser's ~6
// per-host connections for its whole life; you MUST free them before opening a
// new page's streams or the new ones can't connect.)
function clearCells(){
  cells.forEach(c => { c.idx = null; c.span.textContent = ''; c.img.removeAttribute('src'); });
}

function showPage(){
  const start = pg*PER, shown = cams.slice(start, start+PER);
  document.getElementById('pageinfo').textContent =
     `Page ${pg+1}/${pages()}  (cameras ${start+1}-${start+shown.length} of ${cams.length})`;
  cells.forEach((c, k) => {
    if (k < shown.length){
      c.idx = start + k;
      c.span.textContent = shown[k].name;
      c.cell.style.display = '';
      c.img.src = streamUrl(c.idx);
    } else {
      c.idx = null;
      c.span.textContent = '';
      c.cell.style.display = 'none';
      c.img.removeAttribute('src');
    }
  });
}

// Initial render.
function draw(){ showPage(); }

// Page change: abort the current page's streams FIRST, let the browser/server
// release those connections, THEN open the new page. Without this hand-off the
// old MJPEG connections keep every browser connection slot and the new page's
// streams deadlock (can't open). This ordered teardown is required for MJPEG
// under the per-host connection limit, not a cosmetic delay.
let pageSeq = 0;
function page(d){
  const my = ++pageSeq;
  clearCells();
  pg = (pg + d + pages()) % pages();
  document.getElementById('pageinfo').textContent = 'Loading page ' + (pg+1) + '/' + pages() + '…';
  setTimeout(() => { if (my === pageSeq && !fullscreen) showPage(); }, 500);
}

function open_(i){
  // Handoff: keep camera i's grid cell streaming (slot reused, no restart) and
  // stop the OTHER cells, freeing their slots for the fullscreen main view.
  fullscreen = true;
  const live = document.getElementById('live');
  live.onerror = () => setTimeout(() => { if (fullscreen) live.src = streamUrl(i) + '&_r=' + Date.now(); }, 2000);
  live.src = streamUrl(i);
  document.getElementById('title').textContent = cams[i].name;
  document.getElementById('view').style.display = 'flex';
  cells.forEach((c) => { if (c.idx !== i) c.img.removeAttribute('src'); });
}
function close_(){
  fullscreen = false;
  const live = document.getElementById('live');
  document.getElementById('view').style.display = 'none';
  live.onerror = null;
  live.removeAttribute('src');       // abort fullscreen stream
  // restore every grid cell's stream (they were stopped for fullscreen)
  cells.forEach((c) => { if (c.idx != null) c.img.src = streamUrl(c.idx); });
}

document.addEventListener('keydown', e=>{
  const k = e.key.toLowerCase();
  if (fullscreen){ if(k==='escape'||k==='backspace'||k==='v') close_(); return; }
  if(k==='n') page(1);
  if(k==='p') page(-1);
});

buildCells();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _authorised(self, query):
        return not TOKEN or query.get("key", [""])[0] == TOKEN

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        query  = parse_qs(parsed.query)
        if not self._authorised(query):
            self._send(401, b"unauthorised", "text/plain")
            return
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/cameras":
            self._send(200, json.dumps(CAMERAS).encode(), "application/json")
        elif path == "/api/status":
            self._send(200, json.dumps(system_status()).encode(), "application/json")
        elif path.startswith("/snapshot/"):
            self._snapshot(path)
        elif path.startswith("/stream/"):
            self._stream(path)
        else:
            self._send(404, b"not found", "text/plain")

    def _index(self, path, prefix):
        try:
            i = int(path[len(prefix):].split(".")[0])
            return i if 0 <= i < len(STREAMS) else None
        except ValueError:
            return None

    def _snapshot(self, path):
        i = self._index(path, "/snapshot/")
        if i is None:
            self._send(404, b"bad camera", "text/plain")
            return
        cam = STREAMS[i]
        cam.add_viewer()
        try:
            for _ in range(50):
                jpg = cam.jpeg()
                if jpg:
                    self._send(200, jpg, "image/jpeg")
                    return
                time.sleep(0.1)
            self._send(200, cam.jpeg_or_status(), "image/jpeg")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            cam.remove_viewer()

    def _stream(self, path):
        i = self._index(path, "/stream/")
        if i is None:
            self._send(404, b"bad camera", "text/plain")
            return
        cam = STREAMS[i]
        cam.add_viewer()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            delay = 1.0 / max(1, STREAM_FPS)
            while True:
                cam.last_use = time.time()
                jpg = cam.jpeg_or_status()
                if jpg:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: " + str(len(jpg)).encode() +
                                     b"\r\n\r\n" + jpg + b"\r\n")
                time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            cam.remove_viewer()

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def local_ips():
    ips = []
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


def main():
    resolve_nvr_ips()
    MONITOR.start()
    threading.Thread(target=reaper, daemon=True).start()
    print(f"\n{len(CAMERAS)} cameras ready on port {PORT}. Open:")
    for ip in local_ips():
        print(f"   http://{ip}:{PORT}/" + (f"?key={TOKEN}" if TOKEN else ""))
    print("\nPress Ctrl+C to stop.\n")
    try:
        QuietServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in STREAMS:
            s.force_stop()


if __name__ == "__main__":
    main()
