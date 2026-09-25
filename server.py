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


# ── per-NVR live-stream slots (the NVR_MAX_CONN limit) + ownership tracking ──
NVR_SEM     = {k: threading.BoundedSemaphore(NVR_MAX_CONN) for k in NVRS}
NVR_ACTIVE  = {k: 0 for k in NVRS}          # == len(NVR_OWNERS[k]); kept for callers/tests
NVR_OWNERS  = {k: {} for k in NVRS}         # nvr -> {camera index: monotonic acquired}
NVR_WAITERS = {k: {} for k in NVRS}         # nvr -> {camera index: monotonic wait start}
_ACTIVE_LOCK = threading.Lock()

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


def _slot_try_acquire(nvr, index):
    """Try to take an NVR slot for camera `index` (waits up to 1 s)."""
    if NVR_SEM[nvr].acquire(timeout=1.0):
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
    return f"[{nvr.upper()}] active={active} max={NVR_MAX_CONN} owners=[{'; '.join(parts)}]"


class CamStream:
    """One shared RTSP connection per camera, alive only while >=1 viewer watches."""
    def __init__(self, info, index):
        self.name     = info["name"]
        self.info     = info
        self.index    = index
        self.label    = f"Cam {index + 1} {info['name']}"
        # Latest frame, cached ALREADY JPEG-encoded (encoded once, in the worker)
        # and shared to every viewer as immutable bytes. Served only while younger
        # than FRAME_MAX_AGE_S (see frame()).
        self.jpeg_bytes = None
        self.frame_ts = 0.0
        self.status   = S_IDLE
        self.status_since = time.monotonic()
        self.last_error = ""
        self.viewers  = 0
        self.last_use = time.time()
        self.startup  = {}            # timing breakdown of the last successful start
        self.first_http_ms = None     # last viewer's connect -> first live frame sent
        self.attempts = 0             # connection attempts (pre-flight or open)
        self.opens    = 0             # successful OpenCV opens
        self.published = 0            # JPEG frames published (-> delivered fps)
        self._gen     = 0
        self._running = False
        self._vlock   = threading.Lock()
        self._flock   = threading.Lock()
        self._ph_key  = None
        self._ph_bytes = None

    @property
    def url(self):
        return make_url(self.info["nvr"], self.info["channel"])

    def _dbg(self, gen, msg):
        if DEBUG_TIMING:
            ev(f"[{self.label}] g{gen} {msg}")

    # ── viewers / lifecycle ──────────────────────────────────────────────
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
            return self.viewers

    def _current(self, gen):
        with self._vlock:
            return self._running and gen == self._gen

    def force_stop(self):
        with self._vlock:
            self._running = False

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
        """Back-off sleep that ends early when the last viewer leaves."""
        end = time.monotonic() + seconds
        while self._current(gen) and time.monotonic() < end:
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))

    def _acquire(self, sem, gen):
        """Abortable semaphore acquire: gives up as soon as the viewer leaves."""
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
        """Open through the serialized connect gate; fresh cameras before ones that
        just failed. -> (cap or None if the viewer left, gate_wait_ms, open_ms)."""
        t_q = time.monotonic()
        if is_retry:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and self._current(gen) and _fresh_pending():
                time.sleep(0.1)
            counted = False
        else:
            _fresh_wait(1)
            counted = True
        got = self._acquire(CONNECT_GATE, gen)
        if counted:
            _fresh_wait(-1)
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
        """Take a live-stream slot; while waiting, report who owns them (<=1 per 10 s)."""
        t_w = time.monotonic()
        last_report = 0.0
        waited = False
        try:
            while self._current(gen):
                if _slot_try_acquire(nvr, self.index):
                    if waited:
                        ev(f"[{self.label}] got {nvr.upper()} slot after waiting {_ms(t_w)} ms")
                    return True
                if not waited:
                    waited = True
                    with _ACTIVE_LOCK:
                        NVR_WAITERS[nvr][self.index] = t_w
                self._state(gen, S_WAIT_SLOT, f"all {NVR_MAX_CONN} {nvr.upper()} slots in use")
                if time.monotonic() - last_report >= 10.0:
                    last_report = time.monotonic()
                    ev(f"{_slot_report(nvr)}  waiting camera={self.label}")
            return False
        finally:
            with _ACTIVE_LOCK:
                NVR_WAITERS[nvr].pop(self.index, None)

    # ── the worker ───────────────────────────────────────────────────────
    def _run(self, gen):
        nvr = self.info["nvr"]
        fail = 0                       # consecutive failures -> backoff + status
        t_worker = time.monotonic()
        # Failed on a PREVIOUS visit (e.g. same page a minute ago)? Then say
        # "Camera offline" while re-checking, and let fresh cameras open first.
        # Evaluated once: failures in THIS session are counted by `fail`, so a
        # healthy camera with one sporadic NVR hang shows "Retrying...", not offline.
        known_dead = _recently_failed(self.index)
        ev(f"[{self.label}] worker start ({nvr} ch{self.info['channel']})")
        try:
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
                t0 = time.monotonic()
                timing = {}

                # 2. PRE-FLIGHT: parallel, outside OpenCV's lock. Dead channels stop here.
                pf = None
                if PREFLIGHT_ENABLED:
                    pf, res = self._preflight(gen)
                    timing["preflight_ms"] = pf.ms if pf else 0
                    if res == rp.ABORTED:
                        if pf:
                            pf.close()
                        break
                    if res != rp.OK:
                        detail = pf.detail if pf else "NVR paused after a credential failure"
                        if pf:
                            pf.close()
                        if res == rp.AUTH_FAIL:
                            self._state(gen, S_LOGIN, detail)
                            continue                 # loop top shows the pause
                        fail += 1
                        _note_open_fail(self.index)
                        if res == rp.UNREACHABLE:
                            self._state(gen, S_NVR_DOWN, detail)
                        else:
                            self._state(gen, self._fail_status(fail, known_dead), detail)
                        self._dbg(gen, f"pre-flight {res} after {timing['preflight_ms']} ms")
                        self._sleep(gen, _backoff_s(fail))
                        continue
                    self._dbg(gen, f"pre-flight OK in {timing['preflight_ms']} ms (channel warm)")

                # 3. OpenCV open through the serialized gate (channel is warm now).
                try:
                    cap, gate_ms, open_ms = self._open_with_priority(
                        gen, fail > 0 or known_dead)
                finally:
                    if pf is not None:
                        pf.close()               # warm-up no longer needed
                timing["gate_wait_ms"], timing["open_ms"] = gate_ms, open_ms
                if cap is None:
                    break                        # viewer left while queued
                if not cap.isOpened():
                    cap.release()
                    fail += 1
                    _note_open_fail(self.index)
                    self._state(gen, self._fail_status(fail, known_dead),
                                f"RTSP open failed after {open_ms} ms")
                    self._sleep(gen, _backoff_s(fail))
                    continue
                self.opens += 1
                _AUTH_OK[nvr] = True

                # 4. A live stream needs a slot (only now -- never while opening/failing).
                t_slot = time.monotonic()
                if not self._acquire_slot(gen, nvr):
                    cap.release()
                    break
                timing["slot_wait_ms"] = _ms(t_slot)
                published = False
                try:
                    t_read = time.monotonic()
                    next_pub = 0.0
                    interval = 1.0 / max(1, STREAM_FPS)
                    while self._current(gen):
                        # grab() demuxes + decodes EVERY frame (keeps the RTSP stream
                        # current); retrieve() -- the BGR conversion -- and the resize +
                        # JPEG encode run only for frames we publish. MEASURED: 26% ->
                        # 18% of a core per NVR2 camera vs read() on every frame.
                        if not cap.grab():
                            break
                        now = time.monotonic()
                        if published and now < next_pub:
                            continue
                        ok, frame = cap.retrieve()
                        if not ok or frame is None:
                            break
                        # steady STREAM_FPS cadence (a plain "now - last >= 1/fps" check
                        # only managed ~6.25 fps from 25 fps input); the FIRST frame at once
                        next_pub = next_pub + interval if next_pub + interval > now else now + interval
                        small = cv2.resize(frame, (STREAM_W, STREAM_H))
                        okj, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                        if not okj:
                            continue
                        self._publish(gen, buf.tobytes())
                        if not published:
                            published = True
                            fail = 0
                            known_dead = False
                            _clear_open_fail(self.index)
                            timing["first_read_ms"] = round((now - t_read) * 1000)
                            timing["jpeg_ms"] = _ms(now)
                            timing["total_ms"] = _ms(t0)
                            timing["since_worker_start_ms"] = _ms(t_worker)
                            self.startup = dict(timing)
                            self._state(gen, S_LIVE)
                            pre = f"preflight {timing['preflight_ms']} | " if "preflight_ms" in timing else ""
                            ev(f"[{self.label}] FIRST FRAME in {timing['total_ms']} ms  ({pre}gate wait "
                               f"{gate_ms} | open {open_ms} | slot wait {timing['slot_wait_ms']} | first read "
                               f"{timing['first_read_ms']} | jpeg {timing['jpeg_ms']} ms)")
                finally:
                    cap.release()
                    _slot_release(nvr, self.index)   # released BEFORE any back-off

                if not self._current(gen):
                    break
                # 5. Stream dropped (or opened but produced no frame): retry.
                if published:
                    self._state(gen, S_RETRYING, "stream dropped (no frame within read timeout)")
                    self._sleep(gen, 0.4)
                else:
                    fail += 1
                    _note_open_fail(self.index)
                    self._state(gen, self._fail_status(fail, known_dead), "opened but no frame arrived")
                    self._sleep(gen, _backoff_s(fail))
        finally:
            with self._vlock:
                if gen == self._gen and not self._running and self.status != S_IDLE:
                    self.status = S_IDLE
                    self.status_since = time.monotonic()
            ev(f"[{self.label}] worker stopped")

    # ── frames ───────────────────────────────────────────────────────────
    def _publish(self, gen, data):
        with self._flock:
            if gen == self._gen:
                self.jpeg_bytes = data
                self.frame_ts = time.monotonic()
                self.published += 1

    def frame_age_ms(self):
        with self._flock:
            if self.jpeg_bytes is None:
                return None
            return round((time.monotonic() - self.frame_ts) * 1000)

    def jpeg(self):
        """Latest JPEG, shared by all viewers -- but only if it is younger than
        FRAME_MAX_AGE_S. That keeps a hand-off seamless (page/fullscreen switch
        shows the last frame at once) without ever freezing a dead feed."""
        with self._flock:
            if self.jpeg_bytes is not None and time.monotonic() - self.frame_ts <= FRAME_MAX_AGE_S:
                return self.jpeg_bytes
        return None

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
        """-> (jpeg bytes, True if it is a live camera frame)."""
        jpg = self.jpeg()
        return (jpg, True) if jpg else (self._placeholder(), False)

    def jpeg_or_status(self):
        return self.frame_or_placeholder()[0]

    # ── diagnostics ──────────────────────────────────────────────────────
    def diag(self, now, owners, waiters, position=None):
        nvr = self.info["nvr"]
        held = self.index in owners.get(nvr, {})
        w = waiters.get(nvr, {}).get(self.index)
        with self._vlock:
            status, viewers, running = self.status, self.viewers, self._running
            since, err = self.status_since, self.last_error
        return {
            "index": self.index, "key": camera_key(self.info),
            "name": self.name, "technicalName": self.name,
            "displayName": SETTINGS.display_name(self.index), "displayOrder": position,
            "nvr": nvr, "channel": self.info["channel"],
            "status": status, "statusForMs": round((now - since) * 1000),
            "viewers": viewers, "running": running,
            "hasFrame": self.jpeg() is not None, "lastFrameAgeMs": self.frame_age_ms(),
            "slotHeld": held,
            "slotHeldMs": round((now - owners[nvr][self.index]) * 1000) if held else None,
            "slotWaitMs": round((now - w) * 1000) if w else None,
            "lastErrorMasked": err or None,
            "startup": self.startup or None, "firstHttpFrameMs": self.first_http_ms,
            "attempts": self.attempts, "opens": self.opens, "framesPublished": self.published,
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


STREAMS = [CamStream(c, i) for i, c in enumerate(CAMERAS)]


def reaper():
    """Failsafe only. (1) Stop a worker still running with no viewers (a handler that
    died without cleanup) after IDLE_TIMEOUT. (2) Shout if a slot is ever held by a
    camera nobody watches -- with correct code this never happens."""
    suspect = {}
    while True:
        time.sleep(5)
        now = time.time()
        for s in STREAMS:
            with s._vlock:
                leaked = s._running and s.viewers <= 0
                stale  = now - s.last_use > IDLE_TIMEOUT
            if leaked and stale:
                s.force_stop()
        with _ACTIVE_LOCK:
            owners = [(k, i) for k, o in NVR_OWNERS.items() for i in o]
        suspect = {key: n for key, n in suspect.items() if key in owners}
        for k, i in owners:
            if STREAMS[i].viewers == 0:
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
    nvrs = {}
    for k in NVRS:
        h = MONITOR.get(k)
        nvrs[k] = {
            "active": active[k], "max": NVR_MAX_CONN,
            "waiting": len(waiters[k]),
            "waitingCameras": [{"index": i, "name": STREAMS[i].name,
                                "displayName": SETTINGS.display_name(i), "waitMs": round((now - t) * 1000)}
                               for i, t in sorted(waiters[k].items())],
            "owners": [{"index": i, "name": STREAMS[i].name, "displayName": SETTINGS.display_name(i),
                        "viewers": STREAMS[i].viewers,
                        "status": STREAMS[i].status, "heldMs": round((now - t) * 1000),
                        "lastFrameAgeMs": STREAMS[i].frame_age_ms(),
                        "suspectLeak": STREAMS[i].viewers == 0}
                       for i, t in sorted(owners[k].items())],
            "preflightActive": pf_active[k], "preflightMax": PREFLIGHT_PER_NVR,
            "reachable": (h.reachable if h is not None and h.checked else None),
            "monitor": (h.label if h is not None else None),
            "credentialsConfirmed": _AUTH_OK[k], "authPaused": _nvr_auth_paused(k),
        }
    order = SETTINGS.ordered_indices()
    pos = {idx: p + 1 for p, idx in enumerate(order)}
    cams = [s.diag(now, owners, waiters, pos[s.index]) for s in STREAMS]
    config = {"nvrMaxConn": NVR_MAX_CONN, "connectMax": CONNECT_MAX,
              "preflight": PREFLIGHT_ENABLED, "preflightTimeoutMs": PREFLIGHT_TIMEOUT_MS,
              "preflightPerNvr": PREFLIGHT_PER_NVR, "openTimeoutMs": OPEN_TIMEOUT_MS,
              "readTimeoutMs": READ_TIMEOUT_MS, "frameMaxAgeS": FRAME_MAX_AGE_S,
              "streamFps": STREAM_FPS, "streamSize": f"{STREAM_W}x{STREAM_H}"}
    return {"nvrs": nvrs, "cameras": cams, "config": config}


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
        cam = STREAMS[i]
        n = cam.add_viewer()
        ev(f"[{cam.label}] HTTP viewer connected (viewers {n - 1} -> {n})")
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
                jpg, live = cam.frame_or_placeholder()
                if jpg:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: " + str(len(jpg)).encode() +
                                     b"\r\n\r\n" + jpg + b"\r\n")
                    if live and not sent_live:
                        sent_live = True
                        cam.first_http_ms = _ms(t_conn)
                        ev(f"[{cam.label}] first live frame sent to viewer {cam.first_http_ms} ms after it connected")
                time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            n = cam.remove_viewer()
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
    print(f"\nstream slots/NVR={NVR_MAX_CONN}  pre-flight={'on' if PREFLIGHT_ENABLED else 'off'}"
          f" (timeout {PREFLIGHT_TIMEOUT_MS} ms, {PREFLIGHT_PER_NVR}/NVR)  open timeout={OPEN_TIMEOUT_MS} ms"
          f"  RTSP={os.environ.get('OPENCV_FFMPEG_CAPTURE_OPTIONS')}")
    print("Press Ctrl+C to stop.\n", flush=True)
    try:
        QuietServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in STREAMS:
            s.force_stop()


if __name__ == "__main__":
    main()
