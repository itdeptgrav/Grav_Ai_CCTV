"""Standalone CCTV web server (live viewing).

Pulls RTSP from the NVRs on demand and serves an MJPEG camera grid over HTTP, so
a browser can watch without any camera credentials. One shared RTSP connection
per camera, kept alive only while someone is watching; per-NVR connection limit.

VIEWER MODEL
------------
A viewer IS an open HTTP /stream connection. When the browser closes it (tab
close, navigation, page change, clearing img.src), the server's write fails and
the viewer is removed at once — there is no separate heartbeat/beacon to get
wrong. However many browsers watch a camera, there is ONE upstream connection.

PERSISTENT RELAY (CCTV_PERSISTENT=1, default; see PoolManager)
-------------------------------------------------------------
The server runs 24/7, so it keeps up to the per-NVR cap (NVR_CAP, 6) of upstream
streams HOT even when nobody watches: viewed cameras first (fullscreen before
grid), then recently viewed ones, then the grid order (page 1 first). A browser
subscribes to frames that are already flowing -- no RTSP handshake. A camera that
is not HOT is shown at once from its RAM-cached frame, darkened and stamped
"CACHED hh:mm:ss · Connecting..." (never passed off as live), while it is promoted.
Nothing is recorded to disk. CCTV_PERSISTENT=0 restores the on-demand model: a
camera's worker starts with its first viewer and stops when the last one leaves.

NVR SLOT LIFECYCLE (the important part)
---------------------------------------
Each NVR allows a limited number of simultaneous live pulls (CCTV_NVR_MAX_CONN).
A worker takes a slot ONLY AFTER it has successfully opened the stream, and holds
it only while it is actually pulling frames. It releases the slot the moment its
last viewer leaves and the instant the stream drops. A failing/dead channel never
holds a slot, and never holds one while it backs off. Who owns every slot is
tracked (NVR_OWNERS) and reported in /api/status and in the log whenever a camera
has to wait for a slot.

STARTUP PATH (measured, see FINAL_CCTV_DIAGNOSTIC_REPORT.txt)
------------------------------------------------------------
OpenCV's FFmpeg backend opens RTSP streams one at a time, process-wide. The NVRs
never answer a DESCRIBE for a channel with no camera, and a healthy NVR2 channel
takes ~3 s to answer when "cold". So each camera start is:
  1. PRE-FLIGHT (rtsp_preflight.py): authenticated DESCRIBE, in parallel, outside
     OpenCV's lock. Dead channels are detected here and never reach step 2; a
     healthy channel's connection is held open, which keeps the NVR channel warm.
  2. OpenCV open through CONNECT_GATE (serialized, fresh-first). Because the channel
     is warm its DESCRIBE is answered in ~20 ms instead of ~3 s.
  3. NVR slot, then frames: first frame is JPEG-encoded and published at once;
     afterwards frames are decoded at full rate but encoded at CCTV_STREAM_FPS,
     once per camera, and shared by every viewer.

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
import collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Load .env (optional dependency) before anything reads the environment.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# RTSP over TCP, enforced BEFORE cv2 is imported -- also when the variable was
# already set (e.g. in .env) without a transport. NOTE (measured): FFmpeg's
# 'stimeout'/'timeout' options are not honored for the open phase by this OpenCV
# build; the real timeouts are VideoCapture constructor params (_cap_open_params).
_ffopts = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "")
if "rtsp_transport" not in _ffopts:
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(
        x for x in ("rtsp_transport;tcp", _ffopts) if x)

import cv2
import numpy as np

from nvr_config import (
    CAMERAS, NVRS, CCTV_SUBNET, NETWORK_CHECK_INTERVAL, NETWORK_TIMEOUT,
    make_url, resolve_nvr_ips, endpoint,
)
from netcheck import NetworkMonitor, sanitize_url
import rtsp_preflight as rp
from camera_settings import CameraSettings, camera_key
from settings_page import SETTINGS_PAGE
from grid_page import PAGE


def _envint(name, default):
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


def _envfloat(name, default):
    try:
        return float(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


def _envflag(name, default):
    v = os.getenv(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


PORT         = _envint("CCTV_PORT", 8000)
# Listen address. 0.0.0.0 = reachable from the LAN (default, unchanged). Behind a
# Cloudflare tunnel on the same machine, 127.0.0.1 keeps it off the LAN entirely.
BIND         = os.getenv("CCTV_BIND", "0.0.0.0").strip() or "0.0.0.0"
TOKEN        = os.getenv("CCTV_TOKEN", "change-me-to-a-strong-secret")  # real value in .env; "" disables the gate
STREAM_W     = _envint("CCTV_STREAM_W", 640)
STREAM_H     = _envint("CCTV_STREAM_H", 360)
STREAM_FPS   = _envint("CCTV_STREAM_FPS", 8)
JPEG_QUALITY = _envint("CCTV_JPEG_QUALITY", 70)
IDLE_TIMEOUT = _envint("CCTV_IDLE_TIMEOUT", 60)
NVR_MAX_CONN = _envint("CCTV_NVR_MAX_CONN", 6)
# OpenCV open/read timeout (ms). MEASURED on this LAN: healthy opens take up to
# ~5.2 s (NVR2, cold) and ~3.1 s (NVR1); over the public path they were up to
# ~7 s. A 3-5 s value would kill healthy cameras. Dead channels no longer reach
# OpenCV (the pre-flight catches them), so this value rarely matters any more.
OPEN_TIMEOUT_MS = _envint("CCTV_OPEN_TIMEOUT_MS", 8000)
READ_TIMEOUT_MS = _envint("CCTV_READ_TIMEOUT_MS", 8000)
# Concurrent OpenCV opens, process-wide. MEASURED: 1, 2 or 6 threads finish in the
# same time (OpenCV serializes opens internally), so 1 is kept: it lets the gate
# decide the ORDER (fresh cameras first) instead of OpenCV's internal lock.
CONNECT_MAX  = _envint("CCTV_CONNECT_MAX", 1)
# Pre-flight (see rtsp_preflight.py). Timeout = how long an authenticated DESCRIBE
# may take before the channel is declared dead. MEASURED: healthy cold answers are
# usually 2.7-3.4 s on NVR2 but one took 6.1 s (Floor 9 - Cabin); dead channels never
# answer. A dead channel's pre-flight blocks nobody (it runs outside the open lock),
# so a generous value only delays that one dead tile's "Retrying..." label.
PREFLIGHT_ENABLED    = _envflag("CCTV_PREFLIGHT", True)
PREFLIGHT_TIMEOUT_MS = _envint("CCTV_PREFLIGHT_TIMEOUT_MS", 8000)
# Simultaneous pre-flight handshakes per NVR (the "setup" limit, separate from the
# live-stream limit NVR_MAX_CONN). MEASURED: 6 parallel cold handshakes on NVR2 take
# the same ~3.1 s each as one alone (all ready in 3.7 s vs 22.6 s one-by-one).
PREFLIGHT_PER_NVR    = _envint("CCTV_PREFLIGHT_PER_NVR", 6)
# A cached frame older than this is not shown (placeholder with the real state is
# shown instead). Covers page/fullscreen hand-offs without freezing a dead feed.
FRAME_MAX_AGE_S = _envfloat("CCTV_FRAME_MAX_AGE_S", 5.0)
# FFmpeg decoder threads per camera. MEASURED (6 x NVR2 1280x720 H.265 @25 fps):
# auto = 497 MB RSS / 125 OS threads, 1 = 109 MB / 26 threads, same CPU and full
# 25 fps on every stream. Many small streams gain nothing from 16-way decoding each.
DECODE_THREADS = _envint("CCTV_DECODE_THREADS", 1)
# After the NVR rejects the credentials, pause that NVR this long (lockout safety).
AUTH_PAUSE_S = _envfloat("CCTV_AUTH_PAUSE_S", 900.0)
LOG_EVENTS   = _envflag("CCTV_LOG_EVENTS", True)
DEBUG_TIMING = _envflag("CCTV_DEBUG_TIMING", False)
# Diagnostics: log every HTTP request (path without ?key=, client port). Off by default.
LOG_REQUESTS = _envflag("CCTV_LOG_REQUESTS", False)

# ── persistent relay (see PoolManager) ─────────────────────────────────────────
# ON: the server keeps up to the per-NVR cap of upstream streams HOT around the
# clock, so a browser subscribes to frames that are already flowing instead of
# triggering an RTSP handshake. OFF: the original on-demand behaviour (connect for
# the first viewer, disconnect after the last). Run the pool on ONE server only (the
# production host): every other server.py pointed at the same NVRs must set
# CCTV_PERSISTENT=0, otherwise the NVRs see two pools.
PERSISTENT = _envflag("CCTV_PERSISTENT", True)
# Upstream streams per NVR. 6 is the verified-safe value. Raise it PER NVR
# (CCTV_NVR1_MAX_CONN / CCTV_NVR2_MAX_CONN) only after the supervised capacity test
# (SUPERVISED_NVR_CAPACITY_TEST.txt) has proven that the NVR supports more.
NVR_CAP = {k: max(1, _envint(f"CCTV_{k.upper()}_MAX_CONN", NVR_MAX_CONN)) for k in NVRS}
VERIFIED_SAFE_CAP = 6
IDLE_FPS        = max(0.2, _envfloat("CCTV_IDLE_FPS", 1.0))      # JPEG rate of a HOT camera nobody watches
LIVE_MAX_AGE_S  = _envfloat("CCTV_LIVE_MAX_AGE_S", 2.5)          # newer frame = live; older = CACHED only
CACHE_MAX_AGE_S = _envfloat("CCTV_CACHE_MAX_AGE_S", 1800.0)      # older cached frames are not shown at all
RECENT_S        = _envfloat("CCTV_RECENT_S", 900.0)              # "recently viewed" window (pool rank)
WARM_CONCURRENCY = max(1, _envint("CCTV_WARM_CONCURRENCY", 2))   # background starts in progress per NVR
WARM_STEP_S     = _envfloat("CCTV_WARM_STEP_S", 1.0)             # min gap between background starts per NVR
MIN_BG_HOT_S    = _envfloat("CCTV_MIN_BG_HOT_S", 120.0)          # background stream is not swapped out sooner
BG_SWAP_S       = _envfloat("CCTV_BG_SWAP_S", 30.0)              # at most one background swap per NVR per ...
DEAD_RETRY_S    = _envfloat("CCTV_DEAD_RETRY_S", 60.0)           # 1st background retry of an offline camera (x2 each time, max 1 h)
REFRESH_EVERY_S = _envfloat("CCTV_REFRESH_EVERY_S", 300.0)       # cached-frame refresh of non-HOT cameras (0=off)
REFRESH_SWEEP_GAP_S = _envfloat("CCTV_REFRESH_SWEEP_GAP_S", 20.0)  # ... for cameras never captured yet
REFRESH_VISIT_MAX_S = _envfloat("CCTV_REFRESH_VISIT_MAX_S", 30.0)
REFRESH_IDLE_S  = _envfloat("CCTV_REFRESH_IDLE_S", 60.0)          # ... only when that NVR had no viewer this long

# Tile / API status vocabulary.
S_IDLE       = "Idle"
S_CONNECTING = "Connecting..."
S_WAIT_SLOT  = "Waiting for NVR slot..."
S_NVR_DOWN   = "NVR unreachable"
S_OFFLINE    = "Camera offline"
S_RETRYING   = "Retrying..."
S_LIVE       = "LIVE"
S_LOGIN      = "NVR login failed"

_LOG_LOCK = threading.Lock()


def ev(msg):
    """Concise event log line (viewer/slot/state/startup events, never per frame)."""
    if LOG_EVENTS:
        with _LOG_LOCK:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _ms(t):
    return round((time.monotonic() - t) * 1000)


# User-editable display names / order (Settings page). Presentation only: streams,
# slots and workers stay keyed by the technical camera index.
SETTINGS_FILE = os.getenv("CCTV_SETTINGS_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "camera-settings.json")
SETTINGS = CameraSettings(SETTINGS_FILE, CAMERAS, log=ev)
SETTINGS_MAX_BODY = 64 * 1024


# ── per-NVR live-stream slots (the NVR_CAP limit) + ownership tracking ──
NVR_SEM     = {k: threading.BoundedSemaphore(NVR_CAP[k]) for k in NVRS}
NVR_ACTIVE  = {k: 0 for k in NVRS}          # == len(NVR_OWNERS[k]); kept for callers/tests
NVR_OWNERS  = {k: {} for k in NVRS}         # nvr -> {camera index: monotonic acquired}
NVR_WAITERS = {k: {} for k in NVRS}         # nvr -> {camera index: monotonic wait start}
_WAIT_PRIO  = {k: {} for k in NVRS}         # nvr -> {camera index: (-priority, wait start)}
_ACTIVE_LOCK = threading.Lock()
_POOL_WAKE   = threading.Event()            # viewers / slots changed: re-evaluate the pool now

# ── setup limits ──
# Global gate around the OpenCV open only (never held while streaming).
CONNECT_GATE = threading.BoundedSemaphore(max(1, CONNECT_MAX))
# Per-NVR limit on simultaneous pre-flight handshakes.
PREFLIGHT_SEM    = {k: threading.BoundedSemaphore(max(1, PREFLIGHT_PER_NVR)) for k in NVRS}
PREFLIGHT_ACTIVE = {k: 0 for k in NVRS}

# ── credential safety ──
# Until an NVR has accepted our credentials once, its pre-flights run ONE at a time,
# so wrong credentials can cause at most one failed login (not a burst that locks
# the NVR account). After a 401 the whole NVR is paused for AUTH_PAUSE_S.
_AUTH_OK         = {k: False for k in NVRS}
_AUTH_LOCK       = {k: threading.Lock() for k in NVRS}
_AUTH_PAUSED_TIL = {k: 0.0 for k in NVRS}

# Fair open ordering. Because opens are serialized, a camera whose PREVIOUS attempt
# failed yields the gate to cameras that have not just failed ("failed camera goes
# to the back of the queue"). _FRESH_WAITING counts fresh cameras queued for it.
_ORDER_LOCK    = threading.Lock()
_FRESH_WAITING = 0
# Channels that failed recently, remembered ACROSS worker restarts (index ->
# monotonic time), so a known-dead channel yields -- and shows "Camera offline" --
# even on the first attempt of a fresh page view.
_LAST_FAIL     = {}
_FAIL_WINDOW   = 60.0


def _fresh_wait(delta):
    global _FRESH_WAITING
    with _ORDER_LOCK:
        _FRESH_WAITING += delta


def _fresh_pending():
    with _ORDER_LOCK:
        return _FRESH_WAITING > 0


# Opens for cameras someone is WATCHING go before background warm-up opens.
_VIEWER_OPENS = 0


def _viewer_open_wait(delta):
    global _VIEWER_OPENS
    with _ORDER_LOCK:
        _VIEWER_OPENS += delta


def _viewer_opens_pending():
    with _ORDER_LOCK:
        return _VIEWER_OPENS > 0


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


def _backoff_s(fail_count):
    """Retry delay after `fail_count` consecutive failures: 1,2,4,8,16,30 s cap.
    A dead channel stops hammering the NVR; a transient failure retries fast."""
    return min(2 ** min(max(fail_count, 1) - 1, 6), 30)


def _nvr_auth_paused(nvr):
    return time.monotonic() < _AUTH_PAUSED_TIL[nvr]


def _pause_nvr_auth(nvr):
    _AUTH_OK[nvr] = False
    _AUTH_PAUSED_TIL[nvr] = time.monotonic() + AUTH_PAUSE_S
    ev(f"[{nvr.upper()}] NVR REJECTED THE CONFIGURED CREDENTIALS -- all {nvr} cameras paused "
       f"{AUTH_PAUSE_S:.0f}s to avoid locking the NVR account. Fix {nvr.upper()}_USERNAME/"
       f"{nvr.upper()}_PASSWORD in .env and restart.")


def _cap_open_params():
    """Open/read timeouts as VideoCapture constructor params -- the ONLY form this
    build honors. Returns [] on builds without the properties (e.g. the test stub)."""
    p = []
    if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
        p += [int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), OPEN_TIMEOUT_MS]
    if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
        p += [int(cv2.CAP_PROP_READ_TIMEOUT_MSEC), READ_TIMEOUT_MS]
    if DECODE_THREADS > 0 and hasattr(cv2, "CAP_PROP_N_THREADS"):
        p += [int(cv2.CAP_PROP_N_THREADS), DECODE_THREADS]
    return p


def _new_preflight(nvr, channel, alive):
    """Factory (tests replace it). Credential-free URL; auth is done by the probe."""
    n = NVRS[nvr]
    host, port = endpoint(nvr)
    url = f"rtsp://{host}:{port}/cam/realmonitor?channel={channel}&subtype=1"
    return rp.Preflight(host, port, n["user"], n["pass"], url,
                        timeout_s=PREFLIGHT_TIMEOUT_MS / 1000.0, alive=alive)


# shared reachability monitor (background thread); workers only read its cache
MONITOR = NetworkMonitor(
    NVRS, CCTV_SUBNET,
    interval=NETWORK_CHECK_INTERVAL, timeout=NETWORK_TIMEOUT, endpoint_fn=endpoint,
)


def _slot_try_acquire(nvr, index, timeout=1.0):
    """Try to take an NVR slot for camera `index` (waits up to `timeout` s)."""
    if NVR_SEM[nvr].acquire(timeout=timeout):
        with _ACTIVE_LOCK:
            NVR_ACTIVE[nvr] += 1
            NVR_OWNERS[nvr][index] = time.monotonic()
            NVR_WAITERS[nvr].pop(index, None)
        return True
    return False


def _slot_release(nvr, index):
    with _ACTIVE_LOCK:
        NVR_ACTIVE[nvr] = max(0, NVR_ACTIVE[nvr] - 1)
        NVR_OWNERS[nvr].pop(index, None)
    NVR_SEM[nvr].release()


def _slot_report(nvr):
    """One log line: who owns every occupied slot on `nvr` (no credentials)."""
    now = time.monotonic()
    with _ACTIVE_LOCK:
        owners = sorted(NVR_OWNERS[nvr].items())
        active = NVR_ACTIVE[nvr]
    parts = []
    for idx, t in owners:
        s = STREAMS[idx]
        age = s.frame_age_ms()
        parts.append(f"cam{idx + 1} '{s.name}' viewers={s.viewers} status={s.status} "
                     f"held={now - t:.1f}s lastFrame={'-' if age is None else f'{age}ms'}")
    return f"[{nvr.upper()}] active={active} max={NVR_CAP[nvr]} owners=[{'; '.join(parts)}]"


class CamStream:
    """One upstream RTSP connection per camera, shared by every viewer.

    Two lifecycles (CCTV_PERSISTENT):
      * persistent (default): the worker may also run with NO viewer because the
        PoolManager keeps the most useful cameras HOT; a viewer then subscribes to
        frames that are already flowing. The NVR slot is taken BEFORE connecting,
        so the upstream streams per NVR never exceed its cap -- not even for a
        moment while a camera is being promoted.
      * on-demand (CCTV_PERSISTENT=0, the original design): the worker starts with
        the first viewer and stops with the last; the slot is taken only after a
        successful open.
    In both, a failing camera releases its slot BEFORE any back-off, and there is
    only ever ONE worker thread per camera (a new one waits for the previous one to
    finish), so a camera can never hold two slots or two upstream connections.
    """
    def __init__(self, info, index):
        self.name     = info["name"]
        self.info     = info
        self.index    = index
        self.label    = f"Cam {index + 1} {info['name']}"
        # Latest frame, cached ALREADY JPEG-encoded (encoded once, in the worker)
        # and shared to every viewer as immutable bytes, plus the resized picture
        # itself (RAM only; never written to disk) so a CACHED view can be drawn.
        self.jpeg_bytes = None
        self.frame_ts = 0.0           # monotonic time of the latest frame
        self.frame_wall = 0.0         # wall-clock time of the latest frame ("CACHED 14:03:22")
        self.last_small = None
        self.status   = S_IDLE
        self.status_since = time.monotonic()
        self.last_error = ""
        self.viewers  = 0
        self.viewers_full = 0         # ... of which single-camera (fullscreen) views
        self.last_use = time.time()
        self.last_view_end = 0.0      # monotonic: last viewer left (0 = never viewed)
        # pool state (persistent mode)
        self._bg = False              # kept running by the pool without a viewer
        self.bg_reason = None         # "recent" | "background" | "refresh"
        self.bg_since = 0.0
        self.live_since = None        # monotonic: first frame of the current connection
        self.fail_streak = 0          # consecutive failed attempts (0 after a frame)
        self._pub_now = False         # a viewer just arrived: publish the next frame at once
        self.startup  = {}            # timing breakdown of the last successful start
        self.first_http_ms = None     # last viewer's connect -> first live frame sent
        self.attempts = 0             # connection attempts (pre-flight or open)
        self.opens    = 0             # successful OpenCV opens (= upstream connections made)
        self.reconnects = 0           # streams that dropped after being live
        self.published = 0            # JPEG frames published (-> delivered fps)
        self._gen     = 0
        self._running = False
        self._worker_done = threading.Event()
        self._worker_done.set()
        self._vlock   = threading.Lock()
        self._flock   = threading.Lock()
        self._ph_key  = None
        self._ph_bytes = None
        self._ov_key  = None
        self._ov_bytes = None

    @property
    def url(self):
        return make_url(self.info["nvr"], self.info["channel"])

    def _dbg(self, gen, msg):
        if DEBUG_TIMING:
            ev(f"[{self.label}] g{gen} {msg}")

    # ── viewers / lifecycle ──────────────────────────────────────────────
    def _spawn(self):
        """New worker generation (caller holds _vlock and has set _running). The
        worker first waits for the previous generation's thread to finish."""
        self._gen += 1
        prev, self._worker_done = self._worker_done, threading.Event()
        return self._gen, prev, self._worker_done

    def add_viewer(self, full=False):
        spawn = None
        with self._vlock:
            self.viewers += 1
            if full:
                self.viewers_full += 1
            n = self.viewers
            self.last_use = time.time()
            self._pub_now = True                 # HOT camera at idle rate: next frame at once
            if not self._running:
                self._running = True
                spawn = self._spawn()
        if spawn:
            threading.Thread(target=self._run, args=spawn, daemon=True).start()
        _POOL_WAKE.set()
        return n

    def remove_viewer(self, full=False):
        with self._vlock:
            if self.viewers > 0:
                self.viewers -= 1
            if full and self.viewers_full > 0:
                self.viewers_full -= 1
            if self.viewers == 0:
                self.last_view_end = time.monotonic()
                if self._running:
                    if PERSISTENT and POOL.running:
                        # stays HOT for now as "recently viewed"; the pool decides
                        # (it is demoted only if a viewed camera needs the slot)
                        self._bg, self.bg_reason = True, "recent"
                        self.bg_since = time.monotonic()
                    else:
                        self._running = False     # on-demand: stop now
            n = self.viewers
        _POOL_WAKE.set()
        return n

    def start_bg(self, reason):
        """Pool: keep this camera HOT without a viewer."""
        spawn = None
        with self._vlock:
            self._bg, self.bg_reason, self.bg_since = True, reason, time.monotonic()
            if not self._running:
                self._running = True
                spawn = self._spawn()
        if spawn:
            threading.Thread(target=self._run, args=spawn, daemon=True).start()

    def stop_bg(self):
        """Pool: no longer needed in the background. A viewed camera keeps running."""
        with self._vlock:
            self._bg, self.bg_reason = False, None
            if self.viewers == 0 and self._running:
                self._running = False

    def slot_priority(self):
        """Who gets a free NVR slot first: fullscreen > viewed > recent > background."""
        if self.viewers_full > 0:
            return 3
        if self.viewers > 0:
            return 2
        return 1 if self.bg_reason == "recent" else 0

    def _current(self, gen):
        with self._vlock:
            return self._running and gen == self._gen

    def force_stop(self):
        with self._vlock:
            self._running = False
            self._bg, self.bg_reason = False, None

    def _state(self, gen, status, err=None):
        """Set status (and the masked last error); log real changes only."""
        with self._vlock:
            if gen != self._gen:
                return
            if err is not None:
                self.last_error = sanitize_url(err)[:160]
            old = self.status
            if status == old:
                return
            self.status = status
            self.status_since = time.monotonic()
        ev(f"[{self.label}] {old} -> {status}" + (f"  ({sanitize_url(err)})" if err else ""))

    @staticmethod
    def _fail_status(fail, known_dead):
        """First failure of a camera that was fine -> "Retrying..." (NVR2 sometimes
        does not answer a healthy channel once); repeated / known -> "Camera offline"."""
        return S_RETRYING if fail == 1 and not known_dead else S_OFFLINE

    def _sleep(self, gen, seconds):
        """Back-off sleep that ends early when the camera is no longer wanted."""
        end = time.monotonic() + seconds
        while self._current(gen) and time.monotonic() < end:
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))

    def _acquire(self, sem, gen):
        """Abortable semaphore acquire: gives up as soon as the camera is not wanted."""
        while self._current(gen):
            if sem.acquire(timeout=0.25):
                return True
        return False

    # ── opening ──────────────────────────────────────────────────────────
    def _open_capture(self):
        """OpenCV open with the honored open/read timeouts. (CAP_PROP_BUFFERSIZE
        is not set: measured, the FFmpeg backend rejects it -- cap.set -> False.)"""
        params = _cap_open_params()
        return (cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, params) if params
                else cv2.VideoCapture(self.url, cv2.CAP_FFMPEG))

    def _open_with_priority(self, gen, is_retry):
        """Open through the serialized connect gate. Order: cameras someone is
        watching before background warm-up, and fresh cameras before ones that just
        failed. -> (cap or None if no longer wanted, gate_wait_ms, open_ms)."""
        t_q = time.monotonic()
        if is_retry:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and self._current(gen) and _fresh_pending():
                time.sleep(0.1)
            counted = False
        else:
            _fresh_wait(1)
            counted = True
        counted_viewer = got = False
        try:
            while self._current(gen):
                viewer = self.viewers > 0            # re-checked: a background start may get a viewer
                if viewer and not counted_viewer:
                    _viewer_open_wait(1)
                    counted_viewer = True
                # background warm-up never takes the gate while a watched camera waits for it
                if PERSISTENT and not viewer and _viewer_opens_pending():
                    time.sleep(0.05)
                    continue
                if CONNECT_GATE.acquire(timeout=0.1):
                    got = True
                    break
        finally:
            if counted:
                _fresh_wait(-1)
            if counted_viewer:
                _viewer_open_wait(-1)
        gate_ms = _ms(t_q)
        if not got:
            return None, gate_ms, 0
        try:
            if not self._current(gen):
                return None, gate_ms, 0
            t_o = time.monotonic()
            cap = self._open_capture()
            return cap, gate_ms, _ms(t_o)
        finally:
            CONNECT_GATE.release()

    def _preflight(self, gen):
        """Run the pre-flight (see rtsp_preflight.py). -> (Preflight, result)."""
        nvr, ch = self.info["nvr"], self.info["channel"]
        auth_lock = None
        if not _AUTH_OK[nvr]:
            if not self._acquire(_AUTH_LOCK[nvr], gen):
                return None, rp.ABORTED
            if _AUTH_OK[nvr]:
                _AUTH_LOCK[nvr].release()      # confirmed while we waited: go parallel
            else:
                auth_lock = _AUTH_LOCK[nvr]    # still unconfirmed: one login at a time
        try:
            if _nvr_auth_paused(nvr):
                return None, rp.AUTH_FAIL
            if not self._acquire(PREFLIGHT_SEM[nvr], gen):
                return None, rp.ABORTED
            with _ACTIVE_LOCK:
                PREFLIGHT_ACTIVE[nvr] += 1
            try:
                pf = _new_preflight(nvr, ch, lambda: self._current(gen))
                res = pf.run()
            finally:
                with _ACTIVE_LOCK:
                    PREFLIGHT_ACTIVE[nvr] -= 1
                PREFLIGHT_SEM[nvr].release()
            if res == rp.OK:
                _AUTH_OK[nvr] = True
            elif res == rp.AUTH_FAIL:
                _pause_nvr_auth(nvr)
            return pf, res
        finally:
            if auth_lock is not None:
                auth_lock.release()

    def _acquire_slot(self, gen, nvr):
        """Take an NVR slot. Waiting cameras are served in priority order
        (fullscreen > viewed > background, then first come). While waiting, the
        camera reports who owns the slots (<= 1 log line per 10 s)."""
        t_w = time.monotonic()
        last_report = 0.0
        waited = False
        with _ACTIVE_LOCK:
            NVR_WAITERS[nvr][self.index] = t_w
        try:
            while self._current(gen):
                with _ACTIVE_LOCK:
                    _WAIT_PRIO[nvr][self.index] = (-self.slot_priority(), t_w)
                    mine = min(_WAIT_PRIO[nvr], key=_WAIT_PRIO[nvr].get) == self.index
                if mine and _slot_try_acquire(nvr, self.index, timeout=0.1):
                    if waited:
                        ev(f"[{self.label}] got {nvr.upper()} slot after waiting {_ms(t_w)} ms")
                    return True
                if not mine:
                    time.sleep(0.05)
                _POOL_WAKE.set()                  # the pool may free a background slot
                # persistent: a demotion frees a slot within a moment -- only say
                # "Waiting for NVR slot" when it really takes a while
                if not PERSISTENT or time.monotonic() - t_w >= 1.0:
                    waited = True
                    self._state(gen, S_WAIT_SLOT, f"all {NVR_CAP[nvr]} {nvr.upper()} slots in use")
                    if time.monotonic() - last_report >= 10.0:
                        last_report = time.monotonic()
                        ev(f"{_slot_report(nvr)}  waiting camera={self.label}")
            return False
        finally:
            with _ACTIVE_LOCK:
                NVR_WAITERS[nvr].pop(self.index, None)
                _WAIT_PRIO[nvr].pop(self.index, None)

    # ── the worker ───────────────────────────────────────────────────────
    def _run(self, gen, prev_done, done):
        nvr = self.info["nvr"]
        try:
            while not prev_done.wait(0.05):          # one worker per camera, always
                if not self._current(gen):
                    return
            fail = 0                       # consecutive failures -> backoff + status
            self.fail_streak = 0           # the pool judges THIS run's failures only
            t_worker = time.monotonic()
            # Failed on a PREVIOUS visit (e.g. same page a minute ago)? Then say
            # "Camera offline" while re-checking, and let fresh cameras open first.
            # Evaluated once: failures in THIS session are counted by `fail`, so a
            # healthy camera with one sporadic NVR hang shows "Retrying...", not offline.
            known_dead = _recently_failed(self.index)
            role = "" if self.viewers else f" [{self.bg_reason or 'background'}]"
            ev(f"[{self.label}] worker start ({nvr} ch{self.info['channel']}){role}")
            while self._current(gen):
                # 1. NVR reachable? (cached by the background monitor; never blocks)
                h = MONITOR.get(nvr)
                if h is not None and h.checked and not h.reachable:
                    self._state(gen, S_NVR_DOWN, h.label)
                    self._sleep(gen, 2.0)
                    continue
                if _nvr_auth_paused(nvr):
                    self._state(gen, S_LOGIN, "NVR rejected the configured credentials; "
                                              "paused to avoid an account lockout")
                    self._sleep(gen, 5.0)
                    continue
                if known_dead or fail >= 2:
                    self._state(gen, S_OFFLINE)
                else:
                    self._state(gen, S_CONNECTING if fail == 0 else S_RETRYING)
                self.attempts += 1
                try:
                    outcome = self._attempt(gen, nvr, fail, known_dead, t_worker)
                except Exception as e:           # never let a camera's worker die silently
                    fail += 1
                    ev(f"[{self.label}] worker error: {e!r}")
                    self._state(gen, self._fail_status(fail, known_dead), f"internal error ({type(e).__name__})")
                    outcome = (fail, known_dead, _backoff_s(fail))
                if outcome is None or not self._current(gen):
                    break                        # no longer wanted
                fail, known_dead, pause = outcome
                self.fail_streak = fail
                if pause:
                    self._sleep(gen, pause)      # the slot is already released
        finally:
            with self._vlock:
                if gen == self._gen and not self._running and self.status != S_IDLE:
                    self.status = S_IDLE
                    self.status_since = time.monotonic()
            done.set()
            _POOL_WAKE.set()
            ev(f"[{self.label}] worker stopped")

    def _attempt(self, gen, nvr, fail, known_dead, t_worker):
        """One connection attempt: [slot] -> pre-flight -> open -> [slot] -> frames.
        -> None when the camera is no longer wanted, else (fail, known_dead, pause_s).
        The capture and the slot are released BEFORE a failure is reported and
        before any back-off: a failing camera never holds a scarce NVR slot."""
        t0 = time.monotonic()
        timing = {}
        held = {"cap": None, "pf": None, "slot": False}

        def release():
            if held["pf"] is not None:
                held["pf"].close()
                held["pf"] = None
            if held["cap"] is not None:
                held["cap"].release()
                held["cap"] = None
            if held["slot"]:
                held["slot"] = False
                _slot_release(nvr, self.index)
                _POOL_WAKE.set()
            self.live_since = None

        def failed(status, detail, pause):
            release()                            # slot first, THEN report / back off
            self._state(gen, status, detail)
            return fail, known_dead, pause

        try:
            if PERSISTENT:                       # strict cap: the slot comes FIRST
                t_slot = time.monotonic()
                if not self._acquire_slot(gen, nvr):
                    return None
                held["slot"] = True
                timing["slot_wait_ms"] = _ms(t_slot)
                if self.status == S_WAIT_SLOT:
                    self._state(gen, S_OFFLINE if (known_dead or fail >= 2) else
                                (S_CONNECTING if fail == 0 else S_RETRYING))
            # 2. PRE-FLIGHT: parallel, outside OpenCV's lock. Dead channels stop here.
            if PREFLIGHT_ENABLED:
                pf, res = self._preflight(gen)
                held["pf"] = pf
                timing["preflight_ms"] = pf.ms if pf else 0
                if res == rp.ABORTED:
                    return None
                if res != rp.OK:
                    detail = pf.detail if pf else "NVR paused after a credential failure"
                    if res == rp.AUTH_FAIL:
                        return failed(S_LOGIN, detail, 0.0)       # loop top shows the pause
                    fail += 1
                    _note_open_fail(self.index)
                    self._dbg(gen, f"pre-flight {res} after {timing['preflight_ms']} ms")
                    return failed(S_NVR_DOWN if res == rp.UNREACHABLE else
                                  self._fail_status(fail, known_dead), detail, _backoff_s(fail))
                self._dbg(gen, f"pre-flight OK in {timing['preflight_ms']} ms (channel warm)")

            # 3. OpenCV open through the serialized gate (channel is warm now).
            try:
                cap, gate_ms, open_ms = self._open_with_priority(gen, fail > 0 or known_dead)
                held["cap"] = cap
            finally:
                if held["pf"] is not None:
                    held["pf"].close()           # warm-up no longer needed
                    held["pf"] = None
            timing["gate_wait_ms"], timing["open_ms"] = gate_ms, open_ms
            if cap is None:
                return None                      # no longer wanted while queued
            if not cap.isOpened():
                fail += 1
                _note_open_fail(self.index)
                return failed(self._fail_status(fail, known_dead),
                              f"RTSP open failed after {open_ms} ms", _backoff_s(fail))
            self.opens += 1
            _AUTH_OK[nvr] = True

            # 4. on-demand mode: a live stream needs a slot only now (never while
            #    opening/failing) -- persistent mode already holds it.
            if not held["slot"]:
                t_slot = time.monotonic()
                if not self._acquire_slot(gen, nvr):
                    return None
                held["slot"] = True
                timing["slot_wait_ms"] = _ms(t_slot)

            published = self._stream_frames(gen, cap, timing, t0, t_worker)
            if not self._current(gen):
                return None
            # 5. Stream dropped (or opened but produced no frame): retry.
            if published:
                self.reconnects += 1
                fail, known_dead = 0, False
                return failed(S_RETRYING, "stream dropped (no frame within read timeout)", 0.4)
            fail += 1
            _note_open_fail(self.index)
            return failed(self._fail_status(fail, known_dead), "opened but no frame arrived",
                          _backoff_s(fail))
        finally:
            release()

    def _stream_frames(self, gen, cap, timing, t0, t_worker):
        """Read until the stream drops or the camera is no longer wanted.
        grab() demuxes + decodes EVERY frame (keeps the RTSP stream current);
        retrieve() -- the BGR conversion -- and the resize + JPEG encode run only
        for frames we publish: STREAM_FPS while someone watches, IDLE_FPS for a HOT
        camera nobody watches. MEASURED: 26% -> 18% of a core per NVR2 camera vs
        read() on every frame. Each published JPEG is encoded ONCE, for all viewers."""
        published = False
        t_read = time.monotonic()
        next_pub = 0.0
        while self._current(gen):
            if not cap.grab():
                break
            now = time.monotonic()
            boost = self._pub_now
            if published and now < next_pub and not boost:
                continue
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                break
            fps = STREAM_FPS if (self.viewers > 0 or not PERSISTENT) else IDLE_FPS
            interval = 1.0 / max(0.1, fps)
            if boost:
                self._pub_now = False
                next_pub = now + interval
            else:
                # steady cadence (a plain "now - last >= 1/fps" check only managed
                # ~6.25 fps from 25 fps input); the FIRST frame at once
                next_pub = next_pub + interval if next_pub + interval > now else now + interval
            small = cv2.resize(frame, (STREAM_W, STREAM_H))
            okj, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if not okj:
                continue
            self._publish(gen, buf.tobytes(), small)
            if not published:
                published = True
                self.fail_streak = 0
                self.live_since = now
                _clear_open_fail(self.index)
                timing["first_read_ms"] = round((now - t_read) * 1000)
                timing["jpeg_ms"] = _ms(now)
                timing["total_ms"] = _ms(t0)
                timing["since_worker_start_ms"] = _ms(t_worker)
                self.startup = dict(timing)
                self._state(gen, S_LIVE)
                pre = f"preflight {timing['preflight_ms']} | " if "preflight_ms" in timing else ""
                ev(f"[{self.label}] FIRST FRAME in {timing['total_ms']} ms  ({pre}gate wait "
                   f"{timing.get('gate_wait_ms')} | open {timing.get('open_ms')} | slot wait "
                   f"{timing.get('slot_wait_ms')} | first read {timing['first_read_ms']} | "
                   f"jpeg {timing['jpeg_ms']} ms)")
        return published

    # ── frames ───────────────────────────────────────────────────────────
    def _publish(self, gen, data, small=None):
        with self._flock:
            if gen == self._gen:
                self.jpeg_bytes = data
                self.last_small = small
                self.frame_ts = time.monotonic()
                self.frame_wall = time.time()
                self.published += 1

    def frame_age_ms(self):
        with self._flock:
            if self.jpeg_bytes is None:
                return None
            return round((time.monotonic() - self.frame_ts) * 1000)

    def jpeg(self):
        """Latest JPEG, but only if it is younger than FRAME_MAX_AGE_S (kept for
        the status API and older callers; viewers use frame_for_viewer())."""
        with self._flock:
            if self.jpeg_bytes is not None and time.monotonic() - self.frame_ts <= FRAME_MAX_AGE_S:
                return self.jpeg_bytes
        return None

    def frame_for_viewer(self):
        """What a viewer is sent right now -> (jpeg bytes, state):
          'live'   fresh frame from a running upstream (<= LIVE_MAX_AGE_S old)
          'cached' an older frame from RAM, darkened and stamped
                   "CACHED hh:mm:ss" + what is happening -- never shown as live
          'status' no usable frame: the status card."""
        now = time.monotonic()
        with self._flock:
            data, ts, small, wall = self.jpeg_bytes, self.frame_ts, self.last_small, self.frame_wall
        if data is not None:
            if self._is_live(ts, now):
                return data, "live"
            age = now - ts
            if small is not None and age <= CACHE_MAX_AGE_S:
                ov = self._cached_view(ts, wall, small)
                if ov:
                    return ov, "cached"
        return self._placeholder(), "status"

    def _is_live(self, ts, now):
        """A frame counts as LIVE only if it came from the CURRENT upstream connection
        and is fresh -- never a leftover from before a restart or a demotion."""
        since = self.live_since
        return self._running and since is not None and ts >= since and now - ts <= LIVE_MAX_AGE_S

    def _cache_label(self):
        st = self.status
        if st == S_LIVE:
            return "Reconnecting..."             # stream stalled: the frame is getting old
        if st == S_IDLE:
            return "Connecting..."
        return st

    def _cached_view(self, ts, wall, small):
        """Darkened, stamped copy of the cached frame (encoded once per frame+label)."""
        label = self._cache_label()
        key = (ts, label)
        with self._flock:
            if self._ov_key == key:
                return self._ov_bytes
        img = _cached_look(small, "CACHED " + time.strftime("%H:%M:%S", time.localtime(wall)), label)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        data = buf.tobytes() if ok else None
        with self._flock:
            self._ov_key, self._ov_bytes = key, data
        return data

    def _placeholder(self):
        """Status card shown instead of video: coloured dot + state, display name below.
        Kept clear of the corners and the bottom edge, where the web page overlays
        the tile number and the camera name."""
        # user's display name; OpenCV's Hershey font is ASCII-only, so a non-ASCII
        # name (e.g. Hindi) falls back to the technical name on this image only
        name = SETTINGS.display_name(self.index)
        if not name.isascii():
            name = self.name
        status = self.status
        key = (status, name)
        with self._flock:
            if self._ph_key == key:
                return self._ph_bytes
        ph = _ph_background().copy()
        k = STREAM_W / 640.0                               # layout designed at 640x360
        font, aa = cv2.FONT_HERSHEY_SIMPLEX, cv2.LINE_AA
        # LIVE without a fresh frame = the feed stalled: say so instead of "LIVE"
        text = "Waiting for video..." if status == S_LIVE else status
        thick = max(1, round(2 * k))
        scale, tw, th = _fit_text(text, font, 0.8 * k, thick, STREAM_W - 90 * k)
        r, gap = max(3, round(6 * k)), round(12 * k)
        x = STREAM_W // 2 - (2 * r + gap + tw) // 2
        base = STREAM_H // 2 + th // 2 - round(10 * k)       # status line just above the centre
        cv2.circle(ph, (x + r, base - th // 2), r, _PH_COLOURS.get(status, _PH_WAIT), -1, aa)
        cv2.putText(ph, text, (x + 2 * r + gap, base), font, scale, _PH_TEXT, thick, aa)
        nscale, nw, nh = _fit_text(name, font, 0.55 * k, 1, STREAM_W - 60 * k)
        cv2.putText(ph, name, (STREAM_W // 2 - nw // 2, base + round(24 * k) + nh), font, nscale, _PH_MUTED, 1, aa)
        ok, buf = cv2.imencode(".jpg", ph)
        data = buf.tobytes() if ok else None
        with self._flock:
            self._ph_key, self._ph_bytes = key, data   # encoded once per status change
        return data

    def frame_or_placeholder(self):
        """-> (jpeg bytes, True if it is live camera video)."""
        data, state = self.frame_for_viewer()
        return data, state == "live"

    def jpeg_or_status(self):
        return self.frame_for_viewer()[0]

    # ── diagnostics ──────────────────────────────────────────────────────
    def tier(self, now=None):
        """HOT / CONNECTING / RECONNECTING / WARM / COLD / OFFLINE."""
        now = time.monotonic() if now is None else now
        st = self.status
        if st in (S_OFFLINE, S_NVR_DOWN, S_LOGIN) or (not self._running and self.fail_streak >= 2):
            return "OFFLINE"
        with self._flock:
            ts = self.frame_ts
            age = (now - ts) if self.jpeg_bytes is not None else None
        if self._running:
            if st == S_LIVE and age is not None and self._is_live(ts, now):
                return "HOT"
            return "RECONNECTING" if st in (S_RETRYING, S_LIVE) else "CONNECTING"
        if age is not None and age <= CACHE_MAX_AGE_S:
            return "WARM"
        return "COLD"

    def role(self):
        if self.viewers_full:
            return "fullscreen"
        if self.viewers:
            return "viewed"
        if self._running and self._bg:
            return self.bg_reason or "background"
        return "idle"

    def diag(self, now, owners, waiters, position=None):
        nvr = self.info["nvr"]
        held = self.index in owners.get(nvr, {})
        w = waiters.get(nvr, {}).get(self.index)
        with self._vlock:
            status, viewers, running = self.status, self.viewers, self._running
            since, err = self.status_since, self.last_error
        age = self.frame_age_ms()
        live_since = self.live_since
        return {
            "index": self.index, "key": camera_key(self.info),
            "name": self.name, "technicalName": self.name,
            "displayName": SETTINGS.display_name(self.index), "displayOrder": position,
            "nvr": nvr, "channel": self.info["channel"], "sourceType": "nvr",
            "status": status, "statusForMs": round((now - since) * 1000),
            "tier": self.tier(now), "role": self.role(),
            "viewers": viewers, "viewersFull": self.viewers_full, "running": running,
            "hasFrame": self.jpeg() is not None, "lastFrameAgeMs": age, "cachedFrameAgeMs": age,
            "liveForMs": round((now - live_since) * 1000) if live_since else None,
            "slotHeld": held,
            "slotHeldMs": round((now - owners[nvr][self.index]) * 1000) if held else None,
            "slotWaitMs": round((now - w) * 1000) if w else None,
            "slotPriority": self.slot_priority(),
            "lastErrorMasked": err or None,
            "startup": self.startup or None, "firstHttpFrameMs": self.first_http_ms,
            "attempts": self.attempts, "opens": self.opens, "reconnects": self.reconnects,
            "failStreak": self.fail_streak, "framesPublished": self.published,
        }


# ── status image ("placeholder") look; colours are BGR ────────────────────────
_PH_TEXT  = (241, 236, 232)
_PH_MUTED = (160, 150, 140)
_PH_WAIT  = (11, 158, 245)                                  # amber: connecting / waiting / retrying
_PH_BAD   = (68, 68, 239)                                   # red: offline / unreachable / login failed
_PH_COLOURS = {S_OFFLINE: _PH_BAD, S_NVR_DOWN: _PH_BAD, S_LOGIN: _PH_BAD, S_IDLE: (150, 140, 125)}
_PH_BG = None


def _ph_background():
    """Dark vertical gradient behind the status text (built once, then copied)."""
    global _PH_BG
    if _PH_BG is None:
        t = np.linspace(0.0, 1.0, STREAM_H, dtype=np.float32)[:, None]
        rows = np.array((23, 17, 13), np.float32) * (1 - t) + np.array((36, 28, 22), np.float32) * t
        _PH_BG = np.ascontiguousarray(np.broadcast_to(rows.astype(np.uint8)[:, None, :], (STREAM_H, STREAM_W, 3)))
    return _PH_BG


def _fit_text(text, font, scale, thick, max_w):
    """Shrink `scale` until `text` fits in max_w pixels -> (scale, width, height)."""
    (w, h), _ = cv2.getTextSize(text, font, scale, thick)
    while w > max_w and scale > 0.3:
        scale -= 0.05
        (w, h), _ = cv2.getTextSize(text, font, scale, thick)
    return scale, w, h


def _cached_look(frame, title, detail):
    """A cached frame as shown to viewers: darkened to ~40 % with a centred label
    ("CACHED 14:03:22" + what is happening), so an old picture can never be
    mistaken for live video. Pure numpy + putText; the frame itself is not changed."""
    img = (frame.astype(np.uint16) * 100 // 256).astype(np.uint8)
    h, w = img.shape[:2]
    k = w / 640.0
    font, aa = cv2.FONT_HERSHEY_SIMPLEX, cv2.LINE_AA
    thick = max(1, round(2 * k))
    s1, w1, h1 = _fit_text(title, font, 0.75 * k, thick, w - 80 * k)
    s2, w2, h2 = _fit_text(detail, font, 0.55 * k, 1, w - 60 * k)
    band = int(h1 + h2 + 44 * k)
    y0 = max(0, h // 2 - band // 2)
    y1 = min(h, y0 + band)
    img[y0:y1] = (img[y0:y1].astype(np.uint16) * 120 // 256).astype(np.uint8)   # darker band
    r, gap = max(3, round(6 * k)), round(10 * k)
    x = w // 2 - (2 * r + gap + w1) // 2
    base1 = y0 + int(16 * k) + h1
    cv2.circle(img, (x + r, base1 - h1 // 2), r, _PH_WAIT, -1, aa)
    cv2.putText(img, title, (x + 2 * r + gap, base1), font, s1, _PH_TEXT, thick, aa)
    cv2.putText(img, detail, (w // 2 - w2 // 2, base1 + int(14 * k) + h2), font, s2, _PH_MUTED, 1, aa)
    return img


STREAMS = [CamStream(c, i) for i, c in enumerate(CAMERAS)]


class PoolManager:
    """Persistent relay pool: decides which cameras are HOT (CCTV_PERSISTENT).

    Per NVR at most NVR_CAP upstream streams run. Cameras with viewers always get
    their worker; when slots are contended, fullscreen goes before grid (slot
    priority). The rest of the cap -- the background budget = cap minus viewed
    cameras -- stays HOT with, in this order: recently viewed cameras (most recent
    first), then the cameras earliest in the grid order (page 1 first), so the page
    people open first is already live.

    Against thrashing the NVR (re-creating the handshake problem in the background):
      * a background stream is demoted IMMEDIATELY only when a viewed camera needs
        its slot -- a viewed stream is never stopped by the pool;
      * otherwise background membership changes at most once per BG_SWAP_S per NVR,
        and never for a stream that has been HOT for less than MIN_BG_HOT_S;
      * background starts run WARM_CONCURRENCY at a time, WARM_STEP_S apart
        (controlled start-up: never all cameras at the same moment);
      * an offline camera leaves the background and is retried there only every
        DEAD_RETRY_S (a viewed offline camera keeps its normal back-off);
      * a camera that does not fit gets its cached frame refreshed by a short
        "refresh visit" -- at most one per NVR per REFRESH_EVERY_S (every
        REFRESH_SWEEP_GAP_S while some camera has never been captured) -- which
        borrows the least useful background slot for a few seconds.
    Everything lives in RAM; nothing is recorded to disk.
    """

    def __init__(self):
        self.running = False
        self.ticks = 0
        self.last_start = {k: 0.0 for k in NVRS}
        self.last_swap = {k: 0.0 for k in NVRS}
        self.last_refresh = {k: 0.0 for k in NVRS}
        self.last_activity = {k: 0.0 for k in NVRS}   # last tick with a viewer on this NVR
        self.block_until = {}                         # camera index -> monotonic
        self.dead_count = {}                          # camera index -> consecutive background failures
        self.promotions = 0
        self.demotions = 0
        self.events = collections.deque(maxlen=40)

    def start(self):
        if not self.running:
            self.running = True
            threading.Thread(target=self._loop, name="pool", daemon=True).start()

    def _loop(self):
        pending = True                                 # first decision right after start-up
        while True:
            # while warm-up has slots left to fill, come back after WARM_STEP_S
            _POOL_WAKE.wait(min(0.5, max(0.05, WARM_STEP_S)) if pending else 0.5)
            _POOL_WAKE.clear()
            try:
                pending = self.tick()
            except Exception as e:                    # the pool must never die silently
                pending = False
                ev(f"[POOL] tick error: {e!r}")

    def _note(self, msg):
        self.events.append(f"{time.strftime('%H:%M:%S')} {msg}")
        ev(f"[POOL] {msg}")

    def _promote(self, s, reason, now):
        s.start_bg(reason)
        self.last_start[s.info["nvr"]] = now
        self.promotions += 1
        self._note(f"{s.info['nvr']} HOT+ {s.label} ({reason})")

    def _demote(self, s, why):
        s.stop_bg()
        self.demotions += 1
        self._note(f"{s.info['nvr']} HOT- {s.label} ({why})")

    def blocked_for(self, index, now=None):
        now = time.monotonic() if now is None else now
        return max(0.0, self.block_until.get(index, 0.0) - now)

    def tick(self):
        """One pool decision round. -> True while background slots are still being
        filled (the loop then comes back sooner)."""
        now = time.monotonic()
        pos = {idx: p for p, idx in enumerate(SETTINGS.ordered_indices())}
        pending = False
        for nvr in NVRS:
            pending = bool(self._tick_nvr(nvr, now, pos)) or pending
        self.ticks += 1
        return pending

    @staticmethod
    def _rank(s, now, pos):
        """Lower = more useful in the background."""
        if s.last_view_end and now - s.last_view_end < RECENT_S:
            return (0, -s.last_view_end)              # recently viewed, newest first
        return (1, pos.get(s.index, 10 ** 6))         # then grid order (page 1 first)

    def _tick_nvr(self, nvr, now, pos):
        cams = [s for s in STREAMS if s.info["nvr"] == nvr]
        viewed = [s for s in cams if s.viewers > 0]
        if viewed:
            self.last_activity[nvr] = now
        budget = max(0, NVR_CAP[nvr] - len(viewed))   # slots left for background streams
        bg = [s for s in cams if s.viewers == 0 and s._bg and s._running]
        rank = lambda s: self._rank(s, now, pos)       # noqa: E731

        # 1. an offline camera gives its background slot back and is retried there
        #    only after DEAD_RETRY_S, doubling per consecutive failure (max 1 h), so a
        #    permanently dead channel costs about one attempt per hour. A whole NVR
        #    being down is NOT this case: those workers stay and reconnect by themselves.
        for s in cams:
            if s.live_since is not None:
                self.dead_count.pop(s.index, None)
        for s in list(bg):
            # failures of THIS run (not the "Camera offline" label that a recently
            # failed camera shows while it is being re-checked). An unreachable NVR
            # (reboot, network drop) is not a dead camera: those keep retrying.
            if (s.fail_streak >= 2 and s.status != S_NVR_DOWN) or s.status == S_LOGIN:
                n = self.dead_count.get(s.index, 0) + 1
                self.dead_count[s.index] = n
                wait_s = min(DEAD_RETRY_S * 2 ** (n - 1), 3600.0)
                self.block_until[s.index] = now + wait_s
                self._demote(s, f"offline; next background retry in {wait_s:.0f} s")
                bg.remove(s)

        # 2. refresh visit over? (a fresh frame was captured, or it took too long)
        for s in list(bg):
            if s.bg_reason == "refresh":
                got = s.live_since is not None and now - s.live_since >= 2.0
                if got or now - s.bg_since > REFRESH_VISIT_MAX_S:
                    self._demote(s, "refresh visit done" if got else "refresh visit gave up")
                    bg.remove(s)

        # 3. which cameras SHOULD be HOT in the background
        visiting = [s for s in bg if s.bg_reason == "refresh"]
        cands = sorted((s for s in cams if s.viewers == 0 and s.bg_reason != "refresh"
                        and self.block_until.get(s.index, 0.0) <= now), key=rank)
        want = cands[:max(0, budget - len(visiting))]

        # 4. demand: viewed cameras need slots -> demote the least useful background
        #    streams NOW (refresh visits first, then the lowest ranked)
        excess = len(bg) - budget
        if excess > 0:
            # streams that are not live yet cost nothing to drop: they go before live ones
            rest = [s for s in bg if s.bg_reason != "refresh"]
            order = (visiting + sorted((s for s in rest if s.live_since is None), key=rank, reverse=True)
                     + sorted((s for s in rest if s.live_since is not None), key=rank, reverse=True))
            for s in order[:excess]:
                self._demote(s, "slot needed by a viewed camera")
            return                                     # rebalance once the slots are free

        # 5. fill free background slots, paced (controlled warm-up)
        starting = sum(1 for s in bg if s.live_since is None)
        free = budget - len(bg)
        missing = [s for s in want if not (s._bg and s._running)]
        if free > 0 and missing:
            if starting < WARM_CONCURRENCY and now - self.last_start[nvr] >= WARM_STEP_S:
                s = next((c for c in missing if not c._running), None)   # (skip one still stopping)
                if s is not None:
                    self._promote(s, "recent" if rank(s)[0] == 0 else "background", now)
            return True                                # more to fill: tick again soon

        # 6. rebalance without demand: at most one swap per BG_SWAP_S, and only of a
        #    stream that has been HOT for at least MIN_BG_HOT_S
        if missing and now - self.last_swap[nvr] >= BG_SWAP_S:
            out = [s for s in sorted(bg, key=rank, reverse=True)
                   if s not in want and s.bg_reason != "refresh" and now - s.bg_since >= MIN_BG_HOT_S]
            if out:
                self.last_swap[nvr] = now
                self._demote(out[0], f"background swap for {missing[0].label}")
                return                                 # step 5 fills the slot next

        # 7. refresh visit: keep the cached frame of a camera outside the pool useful.
        #    Idle time only (no viewer on this NVR for REFRESH_IDLE_S), and it only ever
        #    borrows a plain background slot -- never a recently viewed camera's.
        if (REFRESH_EVERY_S > 0 and not visiting and starting == 0 and budget > 0
                and now - self.last_activity[nvr] >= REFRESH_IDLE_S):
            with _ACTIVE_LOCK:
                busy = bool(NVR_WAITERS[nvr])
            cold = [s for s in cams if s.viewers == 0 and not s._running
                    and self.block_until.get(s.index, 0.0) <= now]
            if busy or not cold:
                return
            s = min(cold, key=lambda c: c.frame_ts if c.jpeg_bytes is not None else -1.0)
            never = s.jpeg_bytes is None
            if not never and now - s.frame_ts < REFRESH_EVERY_S:
                return                                 # every cached frame is recent enough
            if now - self.last_refresh[nvr] < (REFRESH_SWEEP_GAP_S if never else REFRESH_EVERY_S):
                return
            if len(bg) >= budget:                      # borrow the least useful background slot
                victims = [v for v in sorted(bg, key=rank, reverse=True) if rank(v)[0] == 1]
                if not victims:
                    return
                self._demote(victims[0], f"lends its slot for a refresh of {s.label}")
            self.last_refresh[nvr] = now
            self._promote(s, "refresh", now)


POOL = PoolManager()


def reaper():
    """Failsafe only. (1) Stop a worker still running with no viewers (a handler that
    died without cleanup) after IDLE_TIMEOUT -- unless the pool keeps it HOT on
    purpose. (2) Shout if a slot is ever held by a camera nobody watches and the
    pool does not want -- with correct code this never happens."""
    suspect = {}
    while True:
        time.sleep(5)
        now = time.time()
        for s in STREAMS:
            with s._vlock:
                leaked = s._running and s.viewers <= 0 and not s._bg
                stale  = now - s.last_use > IDLE_TIMEOUT
            if leaked and stale:
                s.force_stop()
        with _ACTIVE_LOCK:
            owners = [(k, i) for k, o in NVR_OWNERS.items() for i in o]
        suspect = {key: n for key, n in suspect.items() if key in owners}
        for k, i in owners:
            if STREAMS[i].viewers == 0 and not STREAMS[i]._bg:
                # 3 checks (10-15 s): a stalled read may legitimately hold a slot
                # for up to READ_TIMEOUT after the viewer left before it notices.
                suspect[(k, i)] = suspect.get((k, i), 0) + 1
                if suspect[(k, i)] == 3:
                    ev(f"[{k.upper()}] SLOT LEAK? cam{i + 1} holds a slot with 0 viewers. {_slot_report(k)}")
            else:
                suspect.pop((k, i), None)


def system_status():
    now = time.monotonic()
    with _ACTIVE_LOCK:
        owners = {k: dict(v) for k, v in NVR_OWNERS.items()}
        waiters = {k: dict(v) for k, v in NVR_WAITERS.items()}
        active = dict(NVR_ACTIVE)
        pf_active = dict(PREFLIGHT_ACTIVE)
    tiers = {s.index: s.tier(now) for s in STREAMS}
    nvrs = {}
    for k in NVRS:
        h = MONITOR.get(k)
        mine = [s for s in STREAMS if s.info["nvr"] == k]
        nvrs[k] = {
            "active": active[k], "max": NVR_CAP[k],
            "hot": sum(tiers[s.index] == "HOT" for s in mine),
            "background": sum(1 for s in mine if s.viewers == 0 and s._bg and s._running),
            "viewedCameras": sum(1 for s in mine if s.viewers > 0),
            "waiting": len(waiters[k]),
            "waitingCameras": [{"index": i, "name": STREAMS[i].name,
                                "displayName": SETTINGS.display_name(i), "waitMs": round((now - t) * 1000)}
                               for i, t in sorted(waiters[k].items())],
            "owners": [{"index": i, "name": STREAMS[i].name, "displayName": SETTINGS.display_name(i),
                        "viewers": STREAMS[i].viewers,
                        "status": STREAMS[i].status, "heldMs": round((now - t) * 1000),
                        "lastFrameAgeMs": STREAMS[i].frame_age_ms(),
                        "role": STREAMS[i].role(),
                        "suspectLeak": STREAMS[i].viewers == 0 and not STREAMS[i]._bg}
                       for i, t in sorted(owners[k].items())],
            "preflightActive": pf_active[k], "preflightMax": PREFLIGHT_PER_NVR,
            "reachable": (h.reachable if h is not None and h.checked else None),
            "monitor": (h.label if h is not None else None),
            "credentialsConfirmed": _AUTH_OK[k], "authPaused": _nvr_auth_paused(k),
        }
    order = SETTINGS.ordered_indices()
    pos = {idx: p + 1 for p, idx in enumerate(order)}
    cams = [s.diag(now, owners, waiters, pos[s.index]) for s in STREAMS]
    for c in cams:
        c["backgroundRetryInS"] = round(POOL.blocked_for(c["index"], now)) or None
    config = {"nvrMaxConn": NVR_MAX_CONN, "nvrCaps": dict(NVR_CAP), "connectMax": CONNECT_MAX,
              "preflight": PREFLIGHT_ENABLED, "preflightTimeoutMs": PREFLIGHT_TIMEOUT_MS,
              "preflightPerNvr": PREFLIGHT_PER_NVR, "openTimeoutMs": OPEN_TIMEOUT_MS,
              "readTimeoutMs": READ_TIMEOUT_MS, "frameMaxAgeS": FRAME_MAX_AGE_S,
              "streamFps": STREAM_FPS, "streamSize": f"{STREAM_W}x{STREAM_H}",
              "persistent": PERSISTENT, "idleFps": IDLE_FPS, "liveMaxAgeS": LIVE_MAX_AGE_S,
              "cacheMaxAgeS": CACHE_MAX_AGE_S, "recentS": RECENT_S,
              "warmConcurrency": WARM_CONCURRENCY, "warmStepS": WARM_STEP_S,
              "minBackgroundHotS": MIN_BG_HOT_S, "backgroundSwapS": BG_SWAP_S,
              "deadRetryS": DEAD_RETRY_S, "refreshEveryS": REFRESH_EVERY_S, "refreshIdleS": REFRESH_IDLE_S}
    pool = {"enabled": PERSISTENT, "running": POOL.running, "ticks": POOL.ticks,
            "recentDecisions": list(POOL.events)[-15:]}
    return {"nvrs": nvrs, "cameras": cams, "config": config, "pool": pool}


def cameras_for_ui():
    """Credential-free camera list for the grid, in TECHNICAL order (compatible with
    earlier clients); the page sorts by displayOrder. 'index' is what /stream/<index>
    uses -- it never changes when a camera is renamed or re-ordered."""
    return [{"index": c["index"], "key": c["key"], "name": c["technicalName"],
             "technicalName": c["technicalName"], "displayName": c["displayName"],
             "displayOrder": c["displayOrder"], "nvr": c["nvr"], "channel": c["channel"]}
            for c in SETTINGS.snapshot()["cameras"]]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle(self):
        if LOG_REQUESTS:
            ev(f"TCP open  :{self.client_address[1]}")
        try:
            super().handle()
        finally:
            if LOG_REQUESTS:
                ev(f"TCP close :{self.client_address[1]}")

    def _authorised(self, query):
        return not TOKEN or query.get("key", [""])[0] == TOKEN

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        query  = parse_qs(parsed.query)
        if LOG_REQUESTS:
            ev(f"HTTP {self.command} {path} from :{self.client_address[1]} "
               f"(Connection: {self.headers.get('Connection', '-')})")
        if not self._authorised(query):
            self._send(401, b"unauthorised", "text/plain")
            return
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path in ("/settings", "/camera-settings"):
            self._send(200, SETTINGS_PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/cameras":
            self._send(200, json.dumps(cameras_for_ui()).encode(), "application/json")
        elif path == "/api/camera-settings":
            self._send(200, json.dumps(SETTINGS.snapshot()).encode(), "application/json")
        elif path == "/api/status":
            self._send(200, json.dumps(system_status()).encode(), "application/json")
        elif path.startswith("/snapshot/"):
            self._snapshot(path)
        elif path.startswith("/stream/"):
            self._stream(path, query)
        else:
            self._send(404, b"not found", "text/plain")

    # Camera settings are changed with PUT (or POST) /api/camera-settings, behind the
    # same access check as everything else. A JSON content type is required: a
    # cross-site form cannot send one without a CORS preflight, which this server
    # never approves (CSRF protection once access moves to cookies/SSO).
    def do_PUT(self):
        self._update_settings()

    def do_POST(self):
        self._update_settings()

    def _update_settings(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if LOG_REQUESTS:
            ev(f"HTTP {self.command} {parsed.path} from :{self.client_address[1]}")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if 0 <= length <= SETTINGS_MAX_BODY:
            body = self.rfile.read(length)            # always drain (keep-alive safe)
        else:
            self.close_connection = True
            return self._json(413, {"ok": False, "errors": [{"message": "Request too large."}]})
        if not self._authorised(query):
            return self._json(401, {"ok": False, "errors": [{"message": "unauthorised"}]})
        if parsed.path != "/api/camera-settings":
            return self._json(404, {"ok": False, "errors": [{"message": "not found"}]})
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(415, {"ok": False, "errors": [{"message": "Content-Type must be application/json."}]})
        try:
            doc = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._json(400, {"ok": False, "errors": [{"message": "Body is not valid JSON."}]})
        if not isinstance(doc, dict):
            return self._json(400, {"ok": False, "errors": [{"message": "Body must be a JSON object."}]})
        base = doc.get("baseRevision")
        if base is not None and (isinstance(base, bool) or not isinstance(base, int)):
            return self._json(400, {"ok": False, "errors": [{"message": "baseRevision must be an integer."}]})
        try:
            status, payload = SETTINGS.update(doc.get("cameras"), base)
        except OSError as e:
            ev(f"[SETTINGS] SAVE FAILED: could not write {SETTINGS.path}: {e}")
            return self._json(500, {"ok": False, "errors": [{"message": "Could not save the settings file on the server."}]})
        if status == 200 and payload.get("changed"):
            ev(f"[SETTINGS] saved revision {payload['revision']}: " + "; ".join(payload["changed"]))
        self._json(status, payload)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

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
            for _ in range(100):
                jpg, state = cam.frame_for_viewer()
                if state == "live":
                    self._send(200, jpg, "image/jpeg")
                    return
                time.sleep(0.1)
            self._send(200, cam.jpeg_or_status(), "image/jpeg")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            cam.remove_viewer()

    def _stream(self, path, query=None):
        i = self._index(path, "/stream/")
        if i is None:
            self._send(404, b"bad camera", "text/plain")
            return
        # ?fps=N (1..STREAM_FPS): lower send rate for low-bandwidth previews (Settings
        # page). Same camera worker, same NVR slot, same cached JPEG -- only fewer
        # frames are written to THIS viewer.
        try:
            fps = int((query or {}).get("fps", [STREAM_FPS])[0])
        except (TypeError, ValueError):
            fps = STREAM_FPS
        fps = max(1, min(STREAM_FPS, fps))
        # ?prio=full: the single-camera (fullscreen) view -- first in line for an NVR slot
        full = (query or {}).get("prio", [""])[0] == "full"
        cam = STREAMS[i]
        n = cam.add_viewer(full=full)
        ev(f"[{cam.label}] HTTP viewer connected (viewers {n - 1} -> {n}){' [fullscreen]' if full else ''}")
        t_conn = time.monotonic()
        sent_live = False
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")   # nginx: do not buffer MJPEG
            self.end_headers()
            delay = 1.0 / fps
            while True:
                cam.last_use = time.time()
                # the FIRST part goes out at once: a HOT camera's live frame, else its
                # cached frame (clearly stamped CACHED), else the status card
                jpg, state = cam.frame_for_viewer()
                if jpg:
                    # X-Frame-State (live|cached|status) lets tools measure what a
                    # viewer actually saw; browsers ignore unknown part headers
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: " + str(len(jpg)).encode() +
                                     b"\r\nX-Frame-State: " + state.encode() +
                                     b"\r\n\r\n" + jpg + b"\r\n")
                    if state == "live" and not sent_live:
                        sent_live = True
                        cam.first_http_ms = _ms(t_conn)
                        ev(f"[{cam.label}] first live frame sent to viewer {cam.first_http_ms} ms after it connected")
                time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            n = cam.remove_viewer(full=full)
            ev(f"[{cam.label}] HTTP viewer disconnected (viewers {n + 1} -> {n})")

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


def confirm_credentials():
    """Background, once at startup: prove each NVR accepts our credentials (first
    camera that answers). Until then pre-flights on that NVR run one at a time."""
    def run(nvr):
        for cam in STREAMS:
            if cam.info["nvr"] != nvr or _AUTH_OK[nvr] or _nvr_auth_paused(nvr):
                continue
            with _AUTH_LOCK[nvr]:
                if _AUTH_OK[nvr]:
                    return
                pf = _new_preflight(nvr, cam.info["channel"], lambda: True)
                res = pf.run()
                pf.close()
            if res == rp.OK:
                _AUTH_OK[nvr] = True
                ev(f"[{nvr.upper()}] credentials confirmed (ch{cam.info['channel']} answered in {pf.ms} ms)")
                return
            if res == rp.AUTH_FAIL:
                _pause_nvr_auth(nvr)
                return
    for nvr in NVRS:
        threading.Thread(target=run, args=(nvr,), daemon=True).start()


def main():
    resolve_nvr_ips()
    MONITOR.start()
    threading.Thread(target=reaper, daemon=True).start()
    if PREFLIGHT_ENABLED:
        confirm_credentials()
    print(f"\n{len(CAMERAS)} cameras ready on port {PORT}. Open:")
    for ip in local_ips():
        print(f"   http://{ip}:{PORT}/" + ("?key=<CCTV_TOKEN>" if TOKEN else ""))
    caps = "  ".join(f"{k}={c}" for k, c in NVR_CAP.items())
    print(f"\nstream slots per NVR: {caps}  pre-flight={'on' if PREFLIGHT_ENABLED else 'off'}"
          f" (timeout {PREFLIGHT_TIMEOUT_MS} ms, {PREFLIGHT_PER_NVR}/NVR)  open timeout={OPEN_TIMEOUT_MS} ms"
          f"  RTSP={os.environ.get('OPENCV_FFMPEG_CAPTURE_OPTIONS')}")
    for k, c in NVR_CAP.items():
        if c > VERIFIED_SAFE_CAP:
            print(f"WARNING: {k.upper()} cap {c} is above the verified-safe {VERIFIED_SAFE_CAP}. Only run "
                  f"this after the supervised capacity test proved the NVR supports it.")
    if PERSISTENT:
        POOL.start()
        print(f"persistent relay ON: the most useful cameras are kept HOT (<= cap per NVR), "
              f"warming {WARM_CONCURRENCY} at a time per NVR. Set CCTV_PERSISTENT=0 on any OTHER "
              f"server that uses the same NVRs.")
    else:
        print("persistent relay OFF (CCTV_PERSISTENT=0): cameras connect on demand.")
    print("Press Ctrl+C to stop.\n", flush=True)
    try:
        QuietServer((BIND, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in STREAMS:
            s.force_stop()


if __name__ == "__main__":
    main()
