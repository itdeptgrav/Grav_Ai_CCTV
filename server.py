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
import re
import sys
import time
import json
import math
import base64
import socket
import struct
import hashlib
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
import rtsp_audio as ra
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

# ── video quality modes (the viewer chooses; default Standard) ──────────────────
# Standard: the NVR SUB-stream, resized to STREAM_W x STREAM_H, JPEG_QUALITY,
#   STREAM_FPS -- light, shared, kept HOT by the relay pool.
# Original: the NVR MAIN stream at the camera's own resolution (never resized unless
#   CCTV_ORIGINAL_MAX_W is set), ORIGINAL_JPEG_QUALITY, ORIGINAL_FPS. Only for
#   cameras someone is viewing in Original mode -- never kept in the background --
#   and it counts against the same per-NVR slot cap as everything else.
# Dahua / CP Plus URL (nvr_config.make_url): .../cam/realmonitor?channel=N&subtype=S,
#   S=0 main stream, S=1 sub-stream.
STANDARD_SUBTYPE = _envint("CCTV_STANDARD_SUBTYPE", 1)
ORIGINAL_SUBTYPE = _envint("CCTV_ORIGINAL_SUBTYPE", 0)
ORIGINAL_JPEG_QUALITY = max(50, min(95, _envint("CCTV_ORIGINAL_JPEG_QUALITY", 90)))
ORIGINAL_FPS     = max(1, _envint("CCTV_ORIGINAL_FPS", 12))
ORIGINAL_MAX_W   = _envint("CCTV_ORIGINAL_MAX_W", 0)              # 0 = keep the source size (true original)
# Original shown ONLY in grid tiles (no fullscreen viewer): the main-stream picture
# scaled down to this width (never up). MEASURED 2026-09-25: a 2560x1440 q90 frame is
# 400-720 KB = 19-36 Mbps per tile at 6 fps; a tile is never displayed that large.
# 0 = full source size in the grid too. The fullscreen view always gets the source size.
ORIGINAL_GRID_MAX_W = _envint("CCTV_ORIGINAL_GRID_MAX_W", 1280)

# ── live-stream stability (FINAL_CCTV_STABILITY_REPORT.txt) ──────────────────────
# Make-before-break for a quality switch: a worker whose last viewer left while the
# SAME camera is being opened in the other quality keeps running (0 viewers) until
# that stream is live -- then it is released (no duplicate upstream afterwards).
# HANDOFF_MAX_S is only the safety cap. An Original worker also stays up
# ORIGINAL_LINGER_S after its last viewer, so a page retry or a reopen re-attaches
# without a new RTSP handshake. Both hold a slot with 0 viewers: anything a viewer
# needs takes that slot at once.
HANDOFF_MAX_S     = _envfloat("CCTV_HANDOFF_MAX_S", 60.0)
ORIGINAL_LINGER_S = _envfloat("CCTV_ORIGINAL_LINGER_S", 5.0)
# RTSP read timeout of the main stream (default: the same as CCTV_READ_TIMEOUT_MS).
ORIGINAL_READ_TIMEOUT_MS = _envint("CCTV_ORIGINAL_READ_TIMEOUT_MS", READ_TIMEOUT_MS)

# ── camera audio (FINAL_CCTV_AUDIO_REPORT.txt) ─────────────────────────────────────
# The NVR streams carry a G.711 microphone track, but OpenCV (the video path) cannot
# deliver audio, so a camera's audio is its OWN audio-only RTSP session: opened only
# while someone listens (one per camera, shared by every listener), counted as a
# normal NVR slot, never kept in the background. Off for every viewer until clicked.
AUDIO_ENABLED        = _envflag("CCTV_AUDIO", True)
AUDIO_LINGER_S       = _envfloat("CCTV_AUDIO_LINGER_S", 10.0)     # kept after the last listener
# no RTP PACKET this long = reconnect. Default = the video's read timeout: measured on the
# real NVRs, a network stall froze audio AND every video stream together; video resumed on
# its open connection after 4-8 s, so audio waits as long before it reconnects.
AUDIO_READ_TIMEOUT_S = _envfloat("CCTV_AUDIO_READ_TIMEOUT_S", READ_TIMEOUT_MS / 1000.0)
AUDIO_OPEN_TIMEOUT_S = _envfloat("CCTV_AUDIO_OPEN_TIMEOUT_S", 8.0)
# A silent microphone is a working stream (packets keep arriving): it is only REPORTED
# ("Audio connected - no sound detected") when no packet peaked above this level for
# AUDIO_SILENCE_S, and never causes a reconnect.
AUDIO_SILENCE_DBFS   = _envfloat("CCTV_AUDIO_SILENCE_DBFS", -60.0)
AUDIO_SILENCE_S      = _envfloat("CCTV_AUDIO_SILENCE_S", 3.0)
AUDIO_MAX_PER_NVR    = max(1, _envint("CCTV_AUDIO_MAX_PER_NVR", 2))  # audio sessions per NVR at most
AUDIO_KEY_BASE       = 2000        # slot key of camera i's audio session = 2000 + i

# NVR slot priority: a free slot goes to the highest waiter. A worker WITH viewers is
# ACTIVE and is never preempted -- only workers with 0 viewers are (background, recent,
# cache refresh, a finished quality switch, a lingering Original).
PRIO_FULLSCREEN_ORIGINAL = 100          # pinned: fullscreen view
PRIO_FULLSCREEN_STANDARD = 90           # pinned: fullscreen view
PRIO_GRID_ORIGINAL       = 82           # visible grid tile
PRIO_GRID_STANDARD       = 80           # visible grid tile
PRIO_HANDOFF             = 40           # 0 viewers: bridge of a Standard <-> Original switch
PRIO_RECENT              = 30           # 0 viewers: viewed a moment ago
PRIO_LINGER              = 25           # 0 viewers: Original kept briefly after its viewer left
PRIO_BACKGROUND          = 20           # 0 viewers: kept HOT by the pool
PRIO_REFRESH             = 10           # 0 viewers: short cache-refresh visit
PRIO_IDLE                = 0
PRIO_AUDIO_FULLSCREEN    = 65           # someone listens (fullscreen) -- below every viewed video
PRIO_AUDIO               = 60           # someone listens (grid tile)
PRIO_NAMES = {PRIO_FULLSCREEN_ORIGINAL: "FULLSCREEN_ORIGINAL", PRIO_FULLSCREEN_STANDARD: "FULLSCREEN_STANDARD",
              PRIO_GRID_ORIGINAL: "GRID_ORIGINAL", PRIO_GRID_STANDARD: "GRID_STANDARD",
              PRIO_HANDOFF: "HANDOFF", PRIO_RECENT: "RECENT", PRIO_LINGER: "LINGER",
              PRIO_BACKGROUND: "BACKGROUND_WARM", PRIO_REFRESH: "CACHE_REFRESH", PRIO_IDLE: "IDLE",
              PRIO_AUDIO_FULLSCREEN: "AUDIO_FULLSCREEN", PRIO_AUDIO: "AUDIO"}
_BG_PRIO = {"handoff": PRIO_HANDOFF, "recent": PRIO_RECENT, "linger": PRIO_LINGER,
            "background": PRIO_BACKGROUND, "refresh": PRIO_REFRESH}

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

# Which streams have an audio track -- learned for free from the SDP of every video
# pre-flight (and every audio session), remembered across restarts. Per camera key:
# {"1": track-or-None, "0": track-or-None}; a missing subtype = not checked yet.
AUDIO_DETECT_FILE = os.path.join(os.path.dirname(os.path.abspath(SETTINGS_FILE)), "audio-detected.json")
_AUDIO_DETECT = {}
_AUDIO_DETECT_LOCK = threading.Lock()
_AUDIO_DETECT_SAVED = {"t": 0.0, "dirty": False}
try:
    with open(AUDIO_DETECT_FILE, encoding="utf-8") as _f:
        _d = json.load(_f)
    if isinstance(_d, dict):
        _AUDIO_DETECT = {k: v for k, v in _d.items() if isinstance(v, dict)}
except (OSError, ValueError):
    pass


def _note_audio(index, subtype, info):
    """Remember whether camera `index`'s stream `subtype` has an audio track."""
    k, sub = camera_key(CAMERAS[index]), str(subtype)
    info = None if not info else {x: info.get(x) for x in ("codec", "rate", "channels", "playable")}
    with _AUDIO_DETECT_LOCK:
        cur = _AUDIO_DETECT.setdefault(k, {})
        if sub in cur and cur[sub] == info:
            return
        cur[sub] = info
        _AUDIO_DETECT_SAVED["dirty"] = True
        due = time.monotonic() - _AUDIO_DETECT_SAVED["t"] >= 5.0
    ev(f"[AUDIO] Cam {index + 1} {CAMERAS[index]['name']} subtype={subtype}: "
       + (f"audio track {info['codec']} {info['rate']} Hz" if info else "no audio track"))
    if due:
        _save_audio_detect()


def _save_audio_detect():
    with _AUDIO_DETECT_LOCK:
        if not _AUDIO_DETECT_SAVED["dirty"]:
            return
        doc = json.dumps(_AUDIO_DETECT, indent=1, sort_keys=True)
        _AUDIO_DETECT_SAVED["dirty"], _AUDIO_DETECT_SAVED["t"] = False, time.monotonic()
    try:
        os.makedirs(os.path.dirname(AUDIO_DETECT_FILE), exist_ok=True)
        tmp = AUDIO_DETECT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(doc)
        os.replace(tmp, AUDIO_DETECT_FILE)
    except OSError:
        pass


def audio_detected(index):
    """-> ("available" | "unavailable" | "unknown", track or None), from detection only."""
    with _AUDIO_DETECT_LOCK:
        d = dict(_AUDIO_DETECT.get(camera_key(CAMERAS[index]), {}))
    track = d.get("1") or d.get("0")
    if track:
        return "available", track
    if "1" in d or "0" in d:
        return "unavailable", None
    return "unknown", None


def audio_state(index):
    """What the UI offers: "disabled" (feature off / Settings: Off), "available",
    "unavailable" (no audio track found) or "unknown" (not checked yet -- may try)."""
    det, track = audio_detected(index)
    ov = SETTINGS.audio_override(index)
    if not AUDIO_ENABLED or ov == "off":
        return "disabled", track
    if ov == "on":
        return "available", track
    return det, track
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


def _new_preflight(nvr, channel, alive, subtype=1):
    """Factory (tests replace it). Credential-free URL; auth is done by the probe."""
    n = NVRS[nvr]
    host, port = endpoint(nvr)
    url = f"rtsp://{host}:{port}/cam/realmonitor?channel={channel}&subtype={subtype}"
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
            n = NVR_ACTIVE[nvr]
        w = _worker_by_key(index)
        ev(f"[{nvr.upper()}] slot {n}/{NVR_CAP[nvr]} taken by {w.label} {w.quality.upper()} "
           f"viewers={w.viewers} {w.priority_name()}")
        return True
    return False


def _slot_release(nvr, index, reason=None):
    with _ACTIVE_LOCK:
        NVR_ACTIVE[nvr] = max(0, NVR_ACTIVE[nvr] - 1)
        NVR_OWNERS[nvr].pop(index, None)
        n = NVR_ACTIVE[nvr]
    NVR_SEM[nvr].release()
    w = _worker_by_key(index)
    ev(f"[{nvr.upper()}] slot released by {w.label} {w.quality.upper()} reason={reason or 'STOPPED'} "
       f"-> {n}/{NVR_CAP[nvr]}")


_PREEMPT_T = {}


def _preempt_idle_holder(nvr, waiter):
    """On-demand mode (no pool): a waiting viewer takes the slot of a worker that has
    NO viewer (a quality-switch bridge or a lingering Original) -- its own twin first,
    else the lowest priority. Never a worker with viewers."""
    now = time.monotonic()
    if now - _PREEMPT_T.get(nvr, 0.0) < 0.3:
        return
    with _ACTIVE_LOCK:
        keys = list(NVR_OWNERS[nvr])
    mine = waiter.slot_priority()
    idle = [w for w in (_worker_by_key(k) for k in keys)
            if w.viewers == 0 and w._bg and w.slot_priority() < mine]
    if not idle:
        return
    tw = waiter.twin()
    victim = tw if tw in idle else min(idle, key=lambda w: w.slot_priority())
    _PREEMPT_T[nvr] = now
    victim.stop_bg(f"PREEMPTED by {waiter.label} ({waiter.priority_name()})")


def _slot_report(nvr):
    """One log line: who owns every occupied slot on `nvr` (no credentials)."""
    now = time.monotonic()
    with _ACTIVE_LOCK:
        owners = sorted(NVR_OWNERS[nvr].items())
        active = NVR_ACTIVE[nvr]
    parts = []
    for key, t in owners:
        s = _worker_by_key(key)
        age = s.frame_age_ms()
        parts.append(f"cam{s.index + 1} '{s.name}' {s.quality} viewers={s.viewers} {s.priority_name()} "
                     f"state={s.vstate} held={now - t:.1f}s lastFrame={'-' if age is None else f'{age}ms'}")
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

    quality="original": the camera's MAIN stream (ORIG_STREAMS). A separate worker
    with its own viewers, cache and slot identity (slot key = ORIG_KEY_BASE + index);
    on-demand only (never kept in the background), slot taken before connecting,
    frames published at the source resolution.
    """
    def __init__(self, info, index, quality="standard"):
        self.name     = info["name"]
        self.info     = info
        self.index    = index
        self.quality  = quality
        self.original = quality == "original"
        self.slot_key = index + (ORIG_KEY_BASE if self.original else 0)
        self.subtype  = ORIGINAL_SUBTYPE if self.original else STANDARD_SUBTYPE
        self.slot_first = PERSISTENT or self.original   # strict cap: slot before connecting
        self.label    = f"Cam {index + 1} {info['name']}" + (" [Original]" if self.original else "")
        self.fallback_viewers = 0     # Original viewers currently shown the Standard stream
        self.source_size = None       # (w, h) of the incoming stream, from the first frame
        self.output_size = None       # (w, h) published to viewers
        # Original: frames per second asked by the current viewers (grid tiles fewer than
        # the fullscreen view); the worker publishes at the highest of them
        self._want_fps = collections.Counter()
        self.pub_fps  = ORIGINAL_FPS
        # stability diagnostics: viewer-facing state + transition log with exact reasons
        self.vstate   = "IDLE"
        self.transitions = collections.deque(maxlen=40)
        self._tlock   = threading.Lock()
        self._stop_reason = None      # why _running was cleared (logged when the worker ends)
        self._drop    = None          # (reason, detail) of the last stream drop
        self.linger_until = 0.0       # deadline of a handoff / linger (0 viewers)
        self.stalls   = 0             # LIVE -> STALLED events (no frame, connection open)
        self.drop_reasons = collections.Counter()
        self.max_gap_ms = 0           # longest pause between two frames from the NVR
        self.gaps_over_1s = 0
        self._last_grab = 0.0         # monotonic time of the last frame read from the NVR
        self.grab_fps = None          # frames read from the NVR per second (source rate)
        self.lag_ms   = None          # behind the stream clock (grows = decoding too slowly)
        self.enc_ms   = None          # resize + JPEG time per published frame (smoothed)
        self.enc_drops = 0            # Original: frames replaced before the encoder took them
        self.cached_reason = None     # why a viewer would get the CACHED view right now
        self._twin_key = None         # Standard view made from the live Original (switch)
        self._twin_bytes = None
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
        self._lab_key = None
        self._lab_bytes = None

    @property
    def url(self):
        return make_url(self.info["nvr"], self.info["channel"], subtype=self.subtype)

    def twin(self):
        """The same camera's worker for the other quality."""
        return STREAMS[self.index] if self.original else ORIG_STREAMS[self.index]

    def is_live(self, now=None):
        now = time.monotonic() if now is None else now
        with self._flock:
            return self.jpeg_bytes is not None and self._is_live(self.frame_ts, now)

    def _trans(self, to, reason, detail=""):
        """Record a viewer-facing state change with its exact reason (+ one log line)."""
        detail = sanitize_url(detail or "")[:200]
        with self._tlock:
            frm = self.vstate
            self.vstate = to
            self.transitions.append({"t": time.monotonic(), "at": time.strftime("%H:%M:%S"), "from": frm,
                                     "to": to, "reason": reason, "detail": detail,
                                     "viewers": self.viewers, "fullscreen": self.viewers_full})
        ev(f"[{self.label}] {frm} -> {to} reason={reason}" + (f" ({detail})" if detail else "")
           + f" viewers={self.viewers}" + (" FULLSCREEN" if self.viewers_full else ""))

    def transitions_view(self, now, n=12):
        with self._tlock:
            items = list(self.transitions)[-n:]
        return [{"at": e["at"], "agoS": round(now - e["t"], 1), "from": e["from"], "to": e["to"],
                 "reason": e["reason"], "detail": e["detail"], "viewers": e["viewers"],
                 "fullscreen": e["fullscreen"]} for e in items]

    def hold_for_handoff(self):
        """The other quality of this camera just got a viewer: if this stream still runs
        with no viewer, keep it as the bridge until that one is live."""
        with self._vlock:
            if self._running and self.viewers == 0 and HANDOFF_MAX_S > 0:
                self._bg, self.bg_reason, self.bg_since = True, "handoff", time.monotonic()
                self.linger_until = time.monotonic() + HANDOFF_MAX_S
        _ensure_housekeeping()

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

    def add_viewer(self, full=False, fps=None):
        spawn = None
        with self._vlock:
            self.viewers += 1
            if full:
                self.viewers_full += 1
            if self.original:
                self._want_fps[fps or ORIGINAL_FPS] += 1
                self.pub_fps = max(self._want_fps)
            if self.bg_reason in ("handoff", "linger"):      # a viewer again: normal worker
                self._bg, self.bg_reason, self.linger_until = False, None, 0.0
            n = self.viewers
            self.last_use = time.time()
            self._pub_now = True                 # HOT camera at idle rate: next frame at once
            if not self._running:
                self._running = True
                spawn = self._spawn()
        if spawn:
            threading.Thread(target=self._run, args=spawn, daemon=True).start()
        _ensure_housekeeping()
        _POOL_WAKE.set()
        return n

    def remove_viewer(self, full=False, fps=None):
        with self._vlock:
            if self.viewers > 0:
                self.viewers -= 1
            if full and self.viewers_full > 0:
                self.viewers_full -= 1
            if self.original:
                k = fps or ORIGINAL_FPS
                if self._want_fps[k] > 0:
                    self._want_fps[k] -= 1
                    if not self._want_fps[k]:
                        del self._want_fps[k]
                self.pub_fps = max(self._want_fps, default=ORIGINAL_FPS)
            if self.viewers == 0:
                now = time.monotonic()
                self.last_view_end = now
                if self._running:
                    tw = self.twin()
                    if tw.viewers > 0 and HANDOFF_MAX_S > 0 and not tw.is_live(now):
                        # quality switch of this camera in progress: this stream stays as
                        # the bridge until the other one is live (make-before-break)
                        self._bg, self.bg_reason, self.bg_since = True, "handoff", now
                        self.linger_until = now + HANDOFF_MAX_S
                    elif PERSISTENT and POOL.running and not self.original:
                        # stays HOT for now as "recently viewed"; the pool decides
                        # (it is demoted only if a viewed camera needs the slot)
                        self._bg, self.bg_reason = True, "recent"
                        self.bg_since = now
                    elif self.original and ORIGINAL_LINGER_S > 0:
                        # a page retry / reopen re-attaches without a new handshake
                        self._bg, self.bg_reason, self.bg_since = True, "linger", now
                        self.linger_until = now + ORIGINAL_LINGER_S
                    else:
                        self._running = False     # on-demand: stop now
                        self._stop_reason = "VIEWERS_GONE (last viewer left)"
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

    def stop_bg(self, reason="POOL_DEMOTION"):
        """No longer needed without a viewer. A viewed camera keeps running (a worker
        with viewers is never preempted)."""
        with self._vlock:
            self._bg, self.bg_reason, self.linger_until = False, None, 0.0
            if self.viewers == 0 and self._running:
                self._running = False
                self._stop_reason = reason

    def slot_priority(self):
        """Who gets a free NVR slot first (PRIO_*): fullscreen > grid tiles > the 0-viewer
        roles. Only 0-viewer workers can be preempted, whatever their number."""
        if self.viewers_full > 0:
            return PRIO_FULLSCREEN_ORIGINAL if self.original else PRIO_FULLSCREEN_STANDARD
        if self.viewers > 0:
            return PRIO_GRID_ORIGINAL if self.original else PRIO_GRID_STANDARD
        if self._running and self._bg:
            return _BG_PRIO.get(self.bg_reason, PRIO_BACKGROUND)
        return PRIO_IDLE

    def priority_name(self):
        return PRIO_NAMES.get(self.slot_priority(), str(self.slot_priority()))

    @property
    def pinned(self):
        """Fullscreen viewer: keeps its slot while the viewer is connected and the
        stream is healthy (nothing preempts a worker with viewers)."""
        return self.viewers_full > 0

    def _current(self, gen):
        with self._vlock:
            return self._running and gen == self._gen

    def force_stop(self, reason="FORCED"):
        with self._vlock:
            if self._running:
                self._stop_reason = reason
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
                alive = lambda: self._current(gen)       # noqa: E731
                pf = (_new_preflight(nvr, ch, alive) if self.subtype == 1
                      else _new_preflight(nvr, ch, alive, subtype=self.subtype))
                res = pf.run()
            finally:
                with _ACTIVE_LOCK:
                    PREFLIGHT_ACTIVE[nvr] -= 1
                PREFLIGHT_SEM[nvr].release()
            if res == rp.OK:
                _AUTH_OK[nvr] = True
                if getattr(pf, "sdp", None):             # free audio detection
                    _note_audio(self.index, self.subtype, ra.audio_info(pf.sdp))
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
        key = self.slot_key
        with _ACTIVE_LOCK:
            NVR_WAITERS[nvr][key] = t_w
        try:
            while self._current(gen):
                with _ACTIVE_LOCK:
                    _WAIT_PRIO[nvr][key] = (-self.slot_priority(), t_w)
                    mine = min(_WAIT_PRIO[nvr], key=_WAIT_PRIO[nvr].get) == key
                if mine and _slot_try_acquire(nvr, key, timeout=0.1):
                    if waited:
                        ev(f"[{self.label}] got {nvr.upper()} slot after waiting {_ms(t_w)} ms")
                    return True
                if not mine:
                    time.sleep(0.05)
                elif not (PERSISTENT and POOL.running) and self.viewers > 0:
                    _preempt_idle_holder(nvr, self)   # a bridge / lingering worker yields
                _POOL_WAKE.set()                  # the pool may free a background slot
                # slot-first: a demotion frees a slot within a moment -- only say
                # "Waiting for NVR slot" when it really takes a while
                if not self.slot_first or time.monotonic() - t_w >= 1.0:
                    if not waited:
                        self._trans("WAITING_SLOT", "NVR_FULL", _slot_report(nvr))
                    waited = True
                    self._state(gen, S_WAIT_SLOT, f"all {NVR_CAP[nvr]} {nvr.upper()} slots in use")
                    if time.monotonic() - last_report >= 10.0:
                        last_report = time.monotonic()
                        ev(f"{_slot_report(nvr)}  waiting camera={self.label}")
            return False
        finally:
            with _ACTIVE_LOCK:
                NVR_WAITERS[nvr].pop(key, None)
                _WAIT_PRIO[nvr].pop(key, None)

    # ── the worker ───────────────────────────────────────────────────────
    def _run(self, gen, prev_done, done):
        nvr = self.info["nvr"]
        try:
            while not prev_done.wait(0.05):          # one worker per camera, always
                if not self._current(gen):
                    return
            fail = 0                       # consecutive failures -> backoff + status
            self.fail_streak = 0           # the pool judges THIS run's failures only
            self._stop_reason = None
            self._redial = False           # True after a drop in THIS run (-> "RECONNECTED")
            t_worker = time.monotonic()
            # Failed on a PREVIOUS visit (e.g. same page a minute ago)? Then say
            # "Camera offline" while re-checking, and let fresh cameras open first.
            # Evaluated once: failures in THIS session are counted by `fail`, so a
            # healthy camera with one sporadic NVR hang shows "Retrying...", not offline.
            known_dead = _recently_failed(self.slot_key)
            role = "" if self.viewers else f" [{self.bg_reason or 'background'}]"
            ev(f"[{self.label}] worker start ({nvr} ch{self.info['channel']}){role}")
            while self._current(gen):
                # 1. NVR reachable? (cached by the background monitor; never blocks)
                h = MONITOR.get(nvr)
                if h is not None and h.checked and not h.reachable:
                    if self.vstate != "NVR_UNREACHABLE":
                        self._trans("NVR_UNREACHABLE", "NVR_UNREACHABLE",
                                    f"network monitor: {h.label} (3 TCP checks in a row failed)")
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
                    self._trans("RETRYING", "WORKER_ERROR", repr(e))
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
                reason = self._stop_reason or ("SUPERSEDED (new worker generation)" if gen != self._gen
                                               else "STOPPED")
            if self.vstate not in ("IDLE", "STOPPED", "WARM"):
                self._trans("WARM" if reason.startswith("POOL") else "STOPPED", re.split(r"[ :]", reason)[0],
                            reason + ("; last frame kept as the CACHED picture" if self.jpeg_bytes else ""))
            if self.original:
                with self._flock:                # full-size picture not needed any more
                    if self.last_small is not None and self.last_small.shape[1] > STREAM_W:
                        self.last_small = cv2.resize(self.last_small, (STREAM_W, STREAM_H))
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

        why = {"r": None}

        def release():
            if held["pf"] is not None:
                held["pf"].close()
                held["pf"] = None
            if held["cap"] is not None:
                held["cap"].release()
                held["cap"] = None
            if held["slot"]:
                held["slot"] = False
                _slot_release(nvr, self.slot_key, why["r"] or self._stop_reason)
                _POOL_WAKE.set()
            self.live_since = None

        def failed(status, detail, pause, reason, to=None):
            why["r"] = reason
            release()                            # slot first, THEN report / back off
            self._trans(to or {S_OFFLINE: "OFFLINE", S_NVR_DOWN: "NVR_UNREACHABLE",
                               S_LOGIN: "LOGIN_FAILED"}.get(status, "RETRYING"), reason, detail)
            self._state(gen, status, detail)
            return fail, known_dead, pause

        try:
            if self.slot_first:                  # strict cap: the slot comes FIRST
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
                        return failed(S_LOGIN, detail, 0.0, "PREFLIGHT_AUTH_FAIL")  # loop top shows the pause
                    fail += 1
                    _note_open_fail(self.slot_key)
                    self._dbg(gen, f"pre-flight {res} after {timing['preflight_ms']} ms")
                    return failed(S_NVR_DOWN if res == rp.UNREACHABLE else
                                  self._fail_status(fail, known_dead), detail, _backoff_s(fail),
                                  f"PREFLIGHT_{res}")
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
                _note_open_fail(self.slot_key)
                return failed(self._fail_status(fail, known_dead),
                              f"RTSP open failed after {open_ms} ms", _backoff_s(fail), "OPEN_FAILED")
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

            self._drop = None
            published = self._stream_frames(gen, cap, timing, t0, t_worker)
            if not self._current(gen):
                why["r"] = self._stop_reason
                return None
            # 5. Stream dropped (or opened but produced no frame): retry.
            reason, detail = self._drop or ("STREAM_CLOSED", "stream ended")
            if published:
                self.reconnects += 1
                self._redial = True
                self.drop_reasons[reason] += 1
                fail, known_dead = 0, False
                return failed(S_RETRYING, f"stream dropped: {detail}", 0.4, reason, to="RECONNECTING")
            fail += 1
            _note_open_fail(self.slot_key)
            return failed(self._fail_status(fail, known_dead), f"opened but no frame arrived ({detail})",
                          _backoff_s(fail), "NO_FIRST_FRAME")
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
        self._last_grab = t_read
        rt_ms = ORIGINAL_READ_TIMEOUT_MS if self.original else READ_TIMEOUT_MS
        n_grab, t_rate = 0, t_read
        lag0 = None                            # (wall, stream ms) at the first frame
        enc = None
        try:
            while self._current(gen):
                ok_g = cap.grab()
                now = time.monotonic()
                gap = now - self._last_grab
                if not ok_g:
                    if not self._current(gen):
                        break
                    gap_ms = round(gap * 1000)
                    if gap_ms >= rt_ms * 0.9:
                        self._drop = ("READ_TIMEOUT", f"no data from the NVR for {gap_ms} ms "
                                                      f"(read timeout {rt_ms} ms)")
                    else:
                        self._drop = ("STREAM_CLOSED", f"the NVR / network ended the stream "
                                                       f"{gap_ms} ms after the last frame")
                    break
                if published:
                    gms = round(gap * 1000)
                    if gms > self.max_gap_ms:
                        self.max_gap_ms = gms
                    if gms > 1000:
                        self.gaps_over_1s += 1
                self._last_grab = now
                n_grab += 1
                if now - t_rate >= 2.0:
                    self.grab_fps = round(n_grab / (now - t_rate), 1)
                    n_grab, t_rate = 0, now
                    lag0 = self._measure_lag(cap, lag0)
                boost = self._pub_now
                if published and now < next_pub and not boost:
                    continue
                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    self._drop = ("RETRIEVE_FAILED", "decoder returned no picture")
                    break
                if self.original:
                    fps = self.pub_fps            # the fastest viewer's rate (grid 6, fullscreen 12)
                else:
                    fps = STREAM_FPS if (self.viewers > 0 or not PERSISTENT) else IDLE_FPS
                interval = 1.0 / max(0.1, fps)
                if boost:
                    self._pub_now = False
                    next_pub = now + interval
                else:
                    # steady cadence (a plain "now - last >= 1/fps" check only managed
                    # ~6.25 fps from 25 fps input); the FIRST frame at once
                    next_pub = next_pub + interval if next_pub + interval > now else now + interval
                src_h, src_w = frame.shape[:2]
                if self.original and published:
                    # Original: encoding runs on its own thread -- this loop goes straight
                    # back to reading the RTSP stream (a 4 MP JPEG takes ~10 ms)
                    if enc is None:
                        enc = _FrameEncoder(self, gen)
                        enc.start()
                    enc.put(frame)
                    continue
                res = self._encode_publish(gen, frame, published)
                if res is None:
                    continue
                small, quality = res
                if not published:
                    self.source_size = (src_w, src_h)
                    self.output_size = (small.shape[1], small.shape[0])
                    ev(f"[{self.label}] {self.quality}: source {src_w}x{src_h} (subtype={self.subtype}) -> "
                       f"output {self.output_size[0]}x{self.output_size[1]}, JPEG quality {quality}, "
                       f"{self.pub_fps if self.original else STREAM_FPS} fps")
                    published = True
                    self.fail_streak = 0
                    self.live_since = now
                    _clear_open_fail(self.slot_key)
                    timing["first_read_ms"] = round((now - t_read) * 1000)
                    timing["jpeg_ms"] = _ms(now)
                    timing["total_ms"] = _ms(t0)
                    timing["since_worker_start_ms"] = _ms(t_worker)
                    self.startup = dict(timing)
                    self._state(gen, S_LIVE)
                    self._trans("LIVE", "RECONNECTED" if getattr(self, "_redial", False) else "FIRST_FRAME",
                                f"{timing['total_ms']} ms (open {timing.get('open_ms')} ms, slot wait "
                                f"{timing.get('slot_wait_ms')} ms)")
                    pre = f"preflight {timing['preflight_ms']} | " if "preflight_ms" in timing else ""
                    ev(f"[{self.label}] FIRST FRAME in {timing['total_ms']} ms  ({pre}gate wait "
                       f"{timing.get('gate_wait_ms')} | open {timing.get('open_ms')} | slot wait "
                       f"{timing.get('slot_wait_ms')} | first read {timing['first_read_ms']} | "
                       f"jpeg {timing['jpeg_ms']} ms)")
        finally:
            if enc is not None:
                enc.stop = True
        return published

    def _measure_lag(self, cap, lag0):
        """How far decoding runs behind the stream: wall time elapsed minus stream time
        elapsed (CAP_PROP_POS_MSEC). Growing = the reader cannot keep up (buffer grows)."""
        try:
            pos = cap.get(cv2.CAP_PROP_POS_MSEC)
        except Exception:
            return lag0
        if not pos or pos <= 0:
            return lag0
        wall = time.monotonic()
        if lag0 is None:
            return (wall, pos)
        self.lag_ms = round((wall - lag0[0]) * 1000 - (pos - lag0[1]))
        return lag0

    def _encode_publish(self, gen, frame, published=True):
        """Resize + JPEG-encode one frame and publish it -> (small, quality) or None."""
        t = time.perf_counter()
        src_h, src_w = frame.shape[:2]
        if self.original:
            # the REAL main-stream picture, never scaled to STREAM_W x STREAM_H and never
            # up: full source size for a fullscreen viewer, <= ORIGINAL_GRID_MAX_W wide
            # while only grid tiles watch
            max_w = ORIGINAL_MAX_W
            if ORIGINAL_GRID_MAX_W and self.viewers_full == 0:
                max_w = min(max_w, ORIGINAL_GRID_MAX_W) if max_w else ORIGINAL_GRID_MAX_W
            if max_w and src_w > max_w:
                small = cv2.resize(frame, (max_w, round(src_h * max_w / src_w)),
                                   interpolation=cv2.INTER_AREA)
            else:
                small = frame
            quality = ORIGINAL_JPEG_QUALITY
            out_wh = (small.shape[1], small.shape[0])
            if published and out_wh != self.output_size:
                ev(f"[{self.label}] output now {out_wh[0]}x{out_wh[1]} "
                   f"({'fullscreen viewer' if self.viewers_full else 'grid tiles only'})")
                self.output_size = out_wh
        else:
            small = cv2.resize(frame, (STREAM_W, STREAM_H))
            quality = JPEG_QUALITY
        okj, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not okj:
            return None
        self._publish(gen, buf.tobytes(), small)
        ms = (time.perf_counter() - t) * 1000
        self.enc_ms = round(ms if self.enc_ms is None else self.enc_ms * 0.9 + ms * 0.1, 1)
        return small, quality

    # ── frames ───────────────────────────────────────────────────────────
    def _publish(self, gen, data, small=None):
        with self._flock:
            if gen != self._gen:
                return
            prev = self.frame_ts
            self.jpeg_bytes = data
            self.last_small = small
            self.frame_ts = time.monotonic()
            self.frame_wall = time.time()
            self.published += 1
        if self.vstate == "STALLED":
            self._trans("LIVE", "FRAME_RESUMED", f"frames again after {round((self.frame_ts - prev) * 1000)} ms "
                                                 f"(same connection, no reconnect)")

    def _check_stall(self, now):
        """Housekeeping: a LIVE stream that delivered no frame for LIVE_MAX_AGE_S -- the
        moment its viewers get the CACHED view -- is logged as STALLED with the cause:
        still reading frames (CPU / encoder behind) or waiting for data (network / NVR)."""
        if self.vstate != "LIVE" or not self._running or self.live_since is None:
            return
        with self._flock:
            age = now - self.frame_ts
        if age <= LIVE_MAX_AGE_S:
            return
        since_grab = round((now - self._last_grab) * 1000)
        cause = ("waiting for data from the NVR/network -- last packet " if since_grab > 1000 else
                 "frames still arriving but not published (CPU/encoder) -- last packet ")
        self.stalls += 1
        self._trans("STALLED", f"NO_FRAME_AGE_{round(age * 1000)}MS",
                    f"connection open, {cause}{since_grab} ms ago")

    def _check_linger(self, now):
        """Housekeeping: end a quality-switch bridge once the other quality is live, and a
        lingering Original after ORIGINAL_LINGER_S. Only ever a worker with 0 viewers."""
        with self._vlock:
            r, until, viewers, running = self.bg_reason, self.linger_until, self.viewers, self._running
        if viewers or not running or r not in ("handoff", "linger"):
            return
        tw = self.twin()
        if r == "handoff":
            if tw.viewers > 0 and tw.is_live(now):
                self.stop_bg("HANDOFF_DONE (the other quality is live; duplicate upstream released)")
            elif tw.viewers == 0:                       # switched back before it finished
                with self._vlock:
                    if self.viewers == 0 and self.bg_reason == "handoff":
                        if PERSISTENT and POOL.running and not self.original:
                            self.bg_reason, self.bg_since = "recent", now
                        elif self.original and ORIGINAL_LINGER_S > 0:
                            self.bg_reason, self.linger_until = "linger", now + ORIGINAL_LINGER_S
                        else:
                            self._running, self._bg, self.bg_reason = False, False, None
                            self._stop_reason = "VIEWERS_GONE (switch cancelled)"
            elif now > until:
                self.stop_bg("HANDOFF_TIMEOUT")
        elif now > until:
            self.stop_bg("LINGER_EXPIRED (no viewer came back)")

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
        if data is not None and self._is_live(ts, now):
            self.cached_reason = None
            return data, "live"
        tw = self.twin()
        if not self.original and tw.is_live(now):
            # quality switch Original -> Standard: until this stream is live, Standard
            # viewers get the LIVE Original picture at Standard size (make-before-break)
            b = self._from_original(tw)
            if b:
                self.cached_reason = None
                return b, "live"
        self.cached_reason = self._why_not_live(now, ts, data is not None)
        with tw._flock:                          # the freshest picture of either quality
            if tw.last_small is not None and tw.frame_ts > ts:
                ts, small, wall = tw.frame_ts, tw.last_small, tw.frame_wall
        if small is not None and now - ts <= CACHE_MAX_AGE_S:
            ov = self._cached_view(ts, wall, small)
            if ov:
                return ov, "cached"
        return self._placeholder(), "status"

    def _from_original(self, o):
        """Standard-size copy of the Original twin's live frame (encoded once per frame)."""
        with o._flock:
            key, small = o.frame_ts, o.last_small
        if small is None:
            return None
        with self._flock:
            if self._twin_key == key:
                return self._twin_bytes
        img = cv2.resize(small, (STREAM_W, STREAM_H), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        data = buf.tobytes() if ok else None
        with self._flock:
            self._twin_key, self._twin_bytes = key, data
        return data

    def _why_not_live(self, now, ts, has_frame):
        """Why a viewer of this worker would get the CACHED / status view right now."""
        st = self.status
        if not self._running:
            return "NOT_HOT (worker not running: " + (self._stop_reason or "not started") + ")"
        if st == S_WAIT_SLOT:
            return "WAITING_FOR_SLOT"
        if st in (S_OFFLINE, S_LOGIN):
            return "SOURCE_OFFLINE" if st == S_OFFLINE else "NVR_LOGIN_FAILED"
        if st == S_NVR_DOWN:
            return "NVR_UNREACHABLE"
        if self.live_since is not None and has_frame:
            return f"STALLED (no frame for {round((now - ts) * 1000)} ms, connection open)"
        if st == S_RETRYING or self.reconnects:
            return "RECONNECTING (" + (self._drop[0] if self._drop else "retry") + ")"
        return "CONNECTING"

    def labelled_view(self, title, detail):
        """This (Standard) worker's LIVE frame with a small quality label burned in:
        what an Original viewer sees while the main stream starts or is unavailable.
        Encoded once per frame + label and shared. None if not live."""
        now = time.monotonic()
        with self._flock:
            ts, small = self.frame_ts, self.last_small
        if small is None or not self._is_live(ts, now):
            return None
        key = (ts, title, detail)
        with self._flock:
            if self._lab_key == key:
                return self._lab_bytes
        img = _labelled_look(small, title, detail)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        data = buf.tobytes() if ok else None
        with self._flock:
            self._lab_key, self._lab_bytes = key, data
        return data

    def unavailable(self, now=None):
        """Original worker: should its viewers get the Standard stream instead? Only
        when the main stream FAILED: an attempt failed (open / pre-flight / no frame),
        camera offline, NVR down or login refused. Not while it waits for a slot or
        for its turn to connect (a Standard stand-in would only compete for the same
        NVR slots), and not for a live stream that dropped and is reconnecting."""
        now = time.monotonic() if now is None else now
        with self._flock:
            live = self.jpeg_bytes is not None and self._is_live(self.frame_ts, now)
        if live:
            return False
        return self.fail_streak >= 1 or self.status in (S_OFFLINE, S_NVR_DOWN, S_LOGIN)

    def _is_live(self, ts, now):
        """A frame counts as LIVE only if it came from the CURRENT upstream connection
        and is fresh -- never a leftover from before a restart or a demotion."""
        since = self.live_since
        return self._running and since is not None and ts >= since and now - ts <= LIVE_MAX_AGE_S

    def _cache_label(self):
        st = self.status
        if st == S_LIVE:
            return "Stalled - waiting for video"  # connection open, no new frame yet
        if st == S_IDLE:
            return "Connecting..."
        if st == S_RETRYING and self.reconnects:
            return "Reconnecting..."
        return st

    def _cached_view(self, ts, wall, small):
        """Darkened, stamped copy of the cached frame (encoded once per frame+label)."""
        label = self._cache_label()
        key = (ts, label)
        with self._flock:
            if self._ov_key == key:
                return self._ov_bytes
        if small.shape[1] > STREAM_W and not self.original:     # an Original picture
            small = cv2.resize(small, (STREAM_W, STREAM_H))
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
            **self._stability_info(now, held, owners[nvr].get(self.slot_key) if held else None),
            "lastErrorMasked": err or None,
            "startup": self.startup or None, "firstHttpFrameMs": self.first_http_ms,
            "attempts": self.attempts, "opens": self.opens, "reconnects": self.reconnects,
            "failStreak": self.fail_streak, "framesPublished": self.published,
            "qualityMode": self.quality, "subtype": self.subtype,
            "sourceSize": _size(self.source_size), "outputSize": _size(self.output_size),
        }

    def quality_info(self, now, owners):
        """Compact, credential-free state of this worker (used for the Original one)."""
        with self._flock:
            has = self.jpeg_bytes is not None
            live = has and self._is_live(self.frame_ts, now)
        return {"qualityMode": self.quality, "subtype": self.subtype, "status": self.status,
                "tier": self.tier(now), "live": live, "viewers": self.viewers,
                "viewersFull": self.viewers_full, "running": self._running,
                "slotHeld": self.slot_key in owners.get(self.info["nvr"], {}),
                "sourceSize": _size(self.source_size), "outputSize": _size(self.output_size),
                "jpegQuality": ORIGINAL_JPEG_QUALITY if self.original else JPEG_QUALITY,
                "fps": self.pub_fps if self.original else STREAM_FPS,
                "lastFrameAgeMs": self.frame_age_ms(), "opens": self.opens,
                "failStreak": self.fail_streak, "lastErrorMasked": self.last_error or None,
                "fallbackViewers": self.fallback_viewers,
                **self._stability_info(now, self.slot_key in owners.get(self.info["nvr"], {}),
                                       owners.get(self.info["nvr"], {}).get(self.slot_key))}

    def _stability_info(self, now, held, t_slot):
        last = self.transitions_view(now, 1)
        return {"priority": self.slot_priority(), "priorityName": self.priority_name(),
                "pinned": self.pinned, "state": self.vstate,
                "slotAgeMs": round((now - t_slot) * 1000) if held and t_slot else None,
                "reconnectCount": self.reconnects, "dropReasons": dict(self.drop_reasons),
                "stalls": self.stalls, "maxFrameGapMs": self.max_gap_ms, "gapsOver1s": self.gaps_over_1s,
                "sourceFps": self.grab_fps, "lagMs": self.lag_ms, "encodeMs": self.enc_ms,
                "encoderDrops": self.enc_drops if self.original else None,
                "cachedReason": None if self.is_live(now) else (self.cached_reason
                                                                or self._why_not_live(now, self.frame_ts,
                                                                                      self.jpeg_bytes is not None)),
                "lastTransitionReason": last[0]["reason"] if last else None,
                "lastTransition": last[0] if last else None,
                "transitions": self.transitions_view(now, 12)}


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


def _labelled_look(frame, title, detail):
    """Copy of a live frame with a small pill in the top-right corner, e.g.
    "STANDARD / Original unavailable" -- so a viewer who chose Original always knows
    when they are looking at the Standard stream."""
    img = frame.copy()
    h, w = img.shape[:2]
    k = w / 640.0
    font, aa = cv2.FONT_HERSHEY_SIMPLEX, cv2.LINE_AA
    s1, w1, h1 = _fit_text(title, font, 0.5 * k, 1, w * 0.45)
    s2, w2, h2 = _fit_text(detail, font, 0.4 * k, 1, w * 0.45)
    pw = int(max(w1 + 22 * k, w2) + 20 * k)
    ph = int(h1 + h2 + 22 * k)
    x0, y0 = max(0, w - pw - int(10 * k)), int(10 * k)
    x1, y1 = min(w, x0 + pw), min(h, y0 + ph)
    img[y0:y1, x0:x1] = (img[y0:y1, x0:x1].astype(np.uint16) * 70 // 256).astype(np.uint8)
    r = max(2, round(4 * k))
    cv2.circle(img, (x0 + int(10 * k) + r, y0 + int(8 * k) + h1 // 2), r, _PH_WAIT, -1, aa)
    cv2.putText(img, title, (x0 + int(10 * k) + 2 * r + int(6 * k), y0 + int(8 * k) + h1), font, s1, _PH_TEXT, 1, aa)
    cv2.putText(img, detail, (x0 + int(10 * k), y0 + int(14 * k) + h1 + h2), font, s2, _PH_MUTED, 1, aa)
    return img


def _size(wh):
    return f"{wh[0]}x{wh[1]}" if wh else None


class _FrameEncoder(threading.Thread):
    """Original only: JPEG encoding off the capture thread. The capture loop keeps
    reading the RTSP stream at the camera's rate; the encoder always takes the NEWEST
    picture (one still waiting is replaced -- counted in encoderDrops)."""

    def __init__(self, cam, gen):
        super().__init__(daemon=True, name=f"enc-{cam.slot_key}")
        self.cam, self.gen = cam, gen
        self._box, self._ev, self._lock, self.stop = None, threading.Event(), threading.Lock(), False

    def put(self, frame):
        with self._lock:
            if self._box is not None:
                self.cam.enc_drops += 1
            self._box = frame
        self._ev.set()

    def run(self):
        while not self.stop:
            if not self._ev.wait(0.5):
                continue
            self._ev.clear()
            with self._lock:
                frame, self._box = self._box, None
            if frame is not None and not self.stop:
                try:
                    self.cam._encode_publish(self.gen, frame)
                except Exception as e:
                    ev(f"[{self.cam.label}] encoder error: {e!r}")


_HK = {"started": False}
_HK_LOCK = threading.Lock()


def _ensure_housekeeping():
    with _HK_LOCK:
        if _HK["started"]:
            return
        _HK["started"] = True
    threading.Thread(target=_housekeeping, name="housekeeping", daemon=True).start()


def _housekeeping():
    """Every 0.25 s: log LIVE -> STALLED the moment a connected stream's viewers would
    get the CACHED view, end finished quality-switch bridges and expired lingers."""
    while True:
        time.sleep(0.25)
        now = time.monotonic()
        for w in STREAMS + ORIG_STREAMS + AUDIO:
            try:
                w._check_stall(now)
                w._check_linger(now)
            except Exception as e:
                ev(f"[{w.label}] housekeeping error: {e!r}")


ORIG_KEY_BASE = 1000        # slot key of camera i's Original worker = 1000 + i
STREAMS = [CamStream(c, i) for i, c in enumerate(CAMERAS)]                         # Standard
ORIG_STREAMS = [CamStream(c, i, quality="original") for i, c in enumerate(CAMERAS)]  # Original


def _audio_endpoint(nvr):
    """Where the audio session connects (tests point it at a fake NVR)."""
    return endpoint(nvr)


class AudioWorker:
    """Camera audio: ONE audio-only RTSP session per camera, shared by every listener
    (browser WebSocket). Started by the first listener, kept AUDIO_LINGER_S after the
    last one (a quick re-listen is instant), never kept in the background. It holds a
    normal NVR slot (key AUDIO_KEY_BASE + index): a free or background slot is used,
    a viewed video stream is NEVER stopped for it -- if the NVR has no capacity the
    listener is told "Audio waiting for available NVR capacity". Independent of the
    video workers: audio failing never touches video, and Standard <-> Original never
    touches audio (it always reads the audio track of the Standard stream).
    Attribute / method names match CamStream where the shared slot, pool and status
    code reads them (viewers = listeners)."""
    quality = "audio"
    original = False
    slot_first = True
    subtype = STANDARD_SUBTYPE

    def __init__(self, info, index):
        self.info, self.index, self.name = info, index, info["name"]
        self.label = f"Cam {index + 1} {info['name']} [Audio]"
        self.slot_key = AUDIO_KEY_BASE + index
        self.viewers = 0              # listeners
        self.viewers_full = 0         # ... of them in the fullscreen view
        self.last_use = time.time()
        self._vlock = threading.Lock()
        self._tlock = threading.Lock()
        self._cond = threading.Condition()
        self._running = False
        self._gen = 0
        self._worker_done = threading.Event()
        self._worker_done.set()
        self._bg, self.bg_reason, self.bg_since, self.linger_until = False, None, 0.0, 0.0
        self.status, self.status_since, self.last_error = S_IDLE, time.monotonic(), ""
        self.vstate = "OFF"
        self.transitions = collections.deque(maxlen=40)
        self._stop_reason = None
        self._redial = False
        self.fail_streak = self.opens = self.reconnects = 0
        self.codec = self.rate = self.subtype_used = None
        self.video_setup = False
        self.packets = self.bytes = 0
        self.last_packet = 0.0
        self.first_packet_ms = None
        self.level_db = self.peak_db = None                # of the last ~1 s of audio (dBFS)
        self._lv = collections.deque(maxlen=64)            # (t, mean square, peak) per packet
        self.silent = False           # PLAYING but nothing above AUDIO_SILENCE_DBFS for AUDIO_SILENCE_S
        self.playing_since = self.last_sound = 0.0
        self.detail = ""
        self.reason = ""              # reason of the last state change
        self.diag = {}                # current / last RTSP session: handshake + counters (no credentials)
        self._pkts = collections.deque(maxlen=250)        # (n, payload): 10-30 s of audio
        self._n = 0

    # shared machinery (slot queue, status, transition log) -- same code as video
    _current = CamStream._current
    _state = CamStream._state
    _sleep = CamStream._sleep
    _acquire_slot = CamStream._acquire_slot
    transitions_view = CamStream.transitions_view

    def _trans(self, to, reason, detail=""):
        self.detail = sanitize_url(detail or "")[:300]
        self.reason = reason
        if to not in ("PLAYING", "STALLED"):
            self.silent = False
        CamStream._trans(self, to, reason, detail)
        with self._cond:
            self._cond.notify_all()                         # listeners report the new state

    def ui_cause(self):
        """Viewer-facing cause of the current state (no technical detail): '' | silent |
        stalled | capacity | nvr | setup | nostream."""
        st, r = self.vstate, self.reason
        if st == "STALLED":
            return "stalled"                        # session open, no packet for > 2 s
        if st == "PLAYING":
            return "silent" if self.silent else ""
        if st == "WAITING_SLOT":
            return "capacity"
        if st == "NVR_UNREACHABLE" or r == "TCP_CONNECT_FAILED":
            return "nvr"
        if r in ("DESCRIBE_FAILED", "SETUP_FAILED", "PLAY_FAILED", "TIMEOUT", "AUTH_FAIL", "LOGIN_PAUSED"):
            return "setup"
        if r == "NO_AUDIO_PACKETS":
            return "nostream"
        return ""

    def twin(self):
        return self

    @property
    def pinned(self):
        return False

    def slot_priority(self):
        if self.viewers_full > 0:
            return PRIO_AUDIO_FULLSCREEN
        if self.viewers > 0:
            return PRIO_AUDIO
        if self._running and self._bg:
            return PRIO_LINGER
        return PRIO_IDLE

    def priority_name(self):
        return PRIO_NAMES.get(self.slot_priority(), str(self.slot_priority()))

    def role(self):
        if self.viewers:
            return "listening"
        return "linger" if self._running else "idle"

    def is_live(self, now=None):
        now = time.monotonic() if now is None else now
        return self._running and self.last_packet > 0 and now - self.last_packet <= 1.5

    def frame_age_ms(self):
        return round((time.monotonic() - self.last_packet) * 1000) if self.last_packet else None

    def latest(self):
        with self._cond:
            return self._n

    # ── listeners ───────────────────────────────────────────────────────────────
    def add_listener(self, full=False):
        spawn = None
        with self._vlock:
            self.viewers += 1
            if full:
                self.viewers_full += 1
            if self.bg_reason == "linger":
                self._bg, self.bg_reason, self.linger_until = False, None, 0.0
            self.last_use = time.time()
            if not self._running:
                self._running = True
                self._gen += 1
                prev, self._worker_done = self._worker_done, threading.Event()
                spawn = (self._gen, prev, self._worker_done)
            n = self.viewers
        if spawn:
            threading.Thread(target=self._run, args=spawn, daemon=True).start()
        _ensure_housekeeping()
        _POOL_WAKE.set()
        return n

    def remove_listener(self, full=False):
        with self._vlock:
            if self.viewers > 0:
                self.viewers -= 1
            if full and self.viewers_full > 0:
                self.viewers_full -= 1
            if self.viewers == 0 and self._running:
                if AUDIO_LINGER_S > 0:          # quick re-listen / mute grace: no new session
                    self._bg, self.bg_reason = True, "linger"
                    self.bg_since = time.monotonic()
                    self.linger_until = self.bg_since + AUDIO_LINGER_S
                else:
                    self._running = False
                    self._stop_reason = "LISTENERS_GONE"
            n = self.viewers
        _POOL_WAKE.set()
        return n

    def stop_bg(self, reason="POOL_DEMOTION"):
        with self._vlock:
            self._bg, self.bg_reason, self.linger_until = False, None, 0.0
            if self.viewers == 0 and self._running:
                self._running = False
                self._stop_reason = reason

    def force_stop(self, reason="FORCED"):
        with self._vlock:
            if self._running:
                self._stop_reason = reason
            self._running = False
            self._bg, self.bg_reason = False, None
        with self._cond:
            self._cond.notify_all()

    def _check_linger(self, now):
        with self._vlock:
            due = (self.viewers == 0 and self._running and self.bg_reason == "linger"
                   and now > self.linger_until)
        if due:
            self.stop_bg("LINGER_EXPIRED (no listener)")

    def _check_stall(self, now):
        if self.vstate == "PLAYING" and self.last_packet and now - self.last_packet > 2.0:
            self._trans("STALLED", f"NO_PACKET_AGE_{round((now - self.last_packet) * 1000)}MS",
                        "session open, the NVR sent no audio packet")

    # ── the worker ─────────────────────────────────────────────────────────────
    def _quota_ok(self, nvr):
        with _ACTIVE_LOCK:
            keys = set(NVR_OWNERS[nvr])
        held = sum(1 for k in keys if k >= AUDIO_KEY_BASE and k != self.slot_key)
        return held < AUDIO_MAX_PER_NVR

    def _source_subtype(self):
        with _AUDIO_DETECT_LOCK:
            d = dict(_AUDIO_DETECT.get(camera_key(self.info), {}))
        if "1" in d and not d["1"] and ("0" not in d or d["0"]):
            return 0                     # the Standard stream has no audio track, the main one may
        return STANDARD_SUBTYPE

    def _push(self, payload):
        now = time.monotonic()
        with self._cond:
            self._n += 1
            self._pkts.append((self._n, payload))
            self.packets += 1
            self.bytes += len(payload)
            self.last_packet = now
            self._cond.notify_all()
        # level of every packet (8000 table look-ups a second: negligible), over the last second
        ss, pk = ra.level(payload, self.codec)
        self._lv.append((now, ss, pk))
        win = [x for x in self._lv if now - x[0] <= 1.0]
        ms = sum(x[1] for x in win) / len(win)
        peak = max(x[2] for x in win)
        self.level_db = round(10 * math.log10(ms / 32768.0 ** 2), 1) if ms > 0 else -120.0
        self.peak_db = round(20 * math.log10(peak / 32768.0), 1) if peak > 0 else -120.0
        # silence is only REPORTED: packets arriving keep the session alive, a quiet room
        # or a camera without a microphone must never cause a reconnect
        if pk > 0 and 20 * math.log10(pk / 32768.0) > AUDIO_SILENCE_DBFS:
            self.last_sound = now
            if self.silent:
                self.silent = False
                ev(f"[{self.label}] audio: sound detected again (peak {self.peak_db} dBFS)")
                with self._cond:
                    self._cond.notify_all()
        elif not self.silent and now - max(self.last_sound, self.playing_since) >= AUDIO_SILENCE_S:
            self.silent = True
            ev(f"[{self.label}] audio: no sound detected -- every packet below {AUDIO_SILENCE_DBFS:g} dBFS for "
               f"{AUDIO_SILENCE_S:g} s (the stream is fine: {self.packets} packets; NOT reconnecting)")
            with self._cond:
                self._cond.notify_all()
        if self.vstate == "STALLED":
            self._trans("PLAYING", "PACKETS_RESUMED", "audio packets arrive again (same session)")

    def _snap(self, sess):
        """Credential-free facts of the current / last RTSP audio session (status API)."""
        self.diag = {"handshake": list(sess.log),
                     "nvrInterleaved": ({"rtp": sess.audio_channel, "rtcp": sess.rtcp_channel}
                                        if sess.audio else None),
                     "videoTrackInterleaved": sess.video_channel,
                     "tcpBytes": sess.bytes_in,
                     "framesByChannel": {str(k): v for k, v in sorted(sess.frames.items())},
                     "audioRtpPackets": sess.audio_packets,
                     "otherPayloadType": sess.other_pt,
                     "sessionTimeoutS": sess.session_timeout if sess.session else None}

    def _run(self, gen, prev_done, done):
        nvr = self.info["nvr"]
        try:
            while not prev_done.wait(0.05):          # one audio session per camera, always
                if not self._current(gen):
                    return
            fail = 0
            self.fail_streak = 0
            self._stop_reason = None
            self._redial = False
            ev(f"[{self.label}] audio worker start ({nvr} ch{self.info['channel']})")
            self._trans("CONNECTING", "LISTENER", f"{self.viewers} listener(s)")
            while self._current(gen):
                h = MONITOR.get(nvr)
                if h is not None and h.checked and not h.reachable:
                    if self.vstate != "NVR_UNREACHABLE":
                        self._trans("NVR_UNREACHABLE", "NVR_UNREACHABLE", f"network monitor: {h.label}")
                    self._state(gen, S_NVR_DOWN, h.label)
                    self._sleep(gen, 2.0)
                    continue
                if _nvr_auth_paused(nvr):
                    if self.vstate != "ERROR":
                        self._trans("ERROR", "LOGIN_PAUSED", "the NVR rejected the credentials; paused")
                    self._state(gen, S_LOGIN)
                    self._sleep(gen, 5.0)
                    continue
                if not self._quota_ok(nvr):
                    if self.vstate != "WAITING_SLOT":
                        self._trans("WAITING_SLOT", "AUDIO_LIMIT",
                                    f"{AUDIO_MAX_PER_NVR} camera audio session(s) already open on {nvr.upper()}")
                    self._state(gen, S_WAIT_SLOT, "audio session limit")
                    self._sleep(gen, 1.0)
                    continue
                outcome = self._attempt(gen, nvr, fail)
                if outcome is None or not self._current(gen):
                    break
                fail, pause = outcome
                self.fail_streak = fail
                if pause:
                    self._sleep(gen, pause)
        finally:
            with self._vlock:
                if gen == self._gen and not self._running and self.status != S_IDLE:
                    self.status, self.status_since = S_IDLE, time.monotonic()
                reason = self._stop_reason or ("SUPERSEDED" if gen != self._gen else "STOPPED")
            if self.vstate not in ("OFF", "UNAVAILABLE"):
                self._trans("OFF", re.split(r"[ :]", reason)[0], reason)
            done.set()
            _POOL_WAKE.set()
            with self._cond:
                self._cond.notify_all()
            ev(f"[{self.label}] audio worker stopped")

    def _attempt(self, gen, nvr, fail):
        """Slot -> DESCRIBE/SETUP(audio)/PLAY -> packets until it drops or nobody listens.
        -> None (stop) or (fail count, pause s). The slot is always released before
        any back-off."""
        if self.vstate not in ("CONNECTING", "RECONNECTING"):
            self._trans("RECONNECTING" if (self._redial or fail) else "CONNECTING", "RETRY" if fail else "SLOT",
                        "")
        if not self._acquire_slot(gen, nvr):        # never above the NVR cap
            return None
        why, detail, had, sess = "STOPPED", "", False, None
        try:
            if self.vstate == "WAITING_SLOT":
                self._trans("CONNECTING", "SLOT_ACQUIRED", "")
            sub = self._source_subtype()
            host, port = _audio_endpoint(nvr)
            n = NVRS[nvr]
            url = f"rtsp://{host}:{port}/cam/realmonitor?channel={self.info['channel']}&subtype={sub}"
            sess = ra.AudioSession(host, port, n["user"], n["pass"], url, timeout=AUDIO_OPEN_TIMEOUT_S,
                                   alive=lambda: self._current(gen), with_video=self.video_setup)
            t0 = time.monotonic()
            try:
                info = sess.open()
            except ra.NoAudio as e:
                why = e.code
                _note_audio(self.index, sub, None if e.code == "NO_AUDIO_TRACK" else {"codec": "?", "playable": False})
                self._trans("UNAVAILABLE", e.code, e.detail)
                with self._vlock:
                    self._running = False
                    self._stop_reason = e.code
                return None
            except ra.AuthFailed as e:
                why = "AUTH_FAIL"
                _pause_nvr_auth(nvr)
                self._trans("ERROR", "AUTH_FAIL", e.detail)
                return fail + 1, 0.0
            except ra.RtspError as e:
                if e.code == "ABORTED":
                    return None
                why, fail = e.code, fail + 1
                self._trans("ERROR" if fail >= 3 else "RECONNECTING", e.code, e.detail)
                self._state(gen, S_OFFLINE if fail >= 3 else S_RETRYING, e.detail)
                return fail, _backoff_s(fail)
            finally:
                self._snap(sess)                        # the handshake, also when it failed
            self.opens += 1
            self.codec, self.rate, self.subtype_used = info["codec"], info["rate"], sub
            self.video_setup = sess.video_setup
            _AUTH_OK[nvr] = True
            _note_audio(self.index, sub, {"codec": info["codec"], "rate": info["rate"],
                                          "channels": info["channels"], "playable": True})
            ev(f"[{self.label}] audio session open: {info['codec']} {info['rate']} Hz, audio RTP on "
               f"interleaved channel {sess.audio_channel} (NVR-assigned)"
               + (", video track set up too" if sess.video_setup else ", audio-only"))
            t_last = t_ka = t_snap = time.monotonic()
            while self._current(gen):
                pkts = sess.read(0.5)
                now = time.monotonic()
                self.last_use = time.time()
                if pkts:
                    if not had:
                        had = True
                        self.first_packet_ms = round((now - t0) * 1000)
                        self.playing_since, self.last_sound, self.silent = now, 0.0, False
                        self._lv.clear()
                        self._state(gen, S_LIVE)
                        self._trans("PLAYING", "RECONNECTED" if self._redial else "FIRST_PACKET",
                                    f"{self.codec} {self.rate} Hz, first packet {self.first_packet_ms} ms after "
                                    f"connecting, interleaved channel {sess.audio_channel}"
                                    + (" (video track set up too: the NVR refused audio-only)"
                                       if sess.video_setup else ""))
                        fail = 0
                    for payload, _seq, _ts in pkts:
                        self._push(payload)
                    t_last = now
                elif now - t_last > AUDIO_READ_TIMEOUT_S:
                    # only a missing RTP PACKET counts here; silent packets keep the session alive
                    gap = round((now - t_last) * 1000)
                    if had:
                        why, detail = "READ_TIMEOUT", f"no audio packet for {gap} ms"
                    else:
                        why = "NO_AUDIO_PACKETS"
                        detail = (f"PLAY accepted, but no audio RTP on interleaved channel {sess.audio_channel} "
                                  f"within {gap} ms (TCP bytes {sess.bytes_in}, frames per channel "
                                  f"{dict(sorted(sess.frames.items()))})")
                    break
                if now - t_ka >= min(20.0, max(5.0, sess.session_timeout / 3.0)):
                    sess.keepalive()
                    t_ka = now
                if now - t_snap >= 1.0:
                    self._snap(sess)
                    t_snap = now
            else:
                why = self._stop_reason or "STOPPED"
                return None
        except ra.RtspError as e:
            why, detail = e.code, e.detail
        except OSError as e:
            why, detail = "STREAM_CLOSED", f"connection error ({type(e).__name__})"
        finally:
            if sess is not None:
                sess.close()
                self._snap(sess)
            _slot_release(nvr, self.slot_key, why)
            _POOL_WAKE.set()
        if not self._current(gen):
            return None
        if had:
            self.reconnects += 1
            self._redial = True
        else:
            fail += 1
        self._trans("RECONNECTING", why, detail)
        self._state(gen, S_RETRYING, detail)
        return (0, 0.5) if had else (fail, _backoff_s(fail))

    def info_view(self, now, owners):
        """Credential-free audio state of this camera (status API)."""
        st, track = audio_state(self.index)
        last = self.transitions_view(now, 1)
        return {"available": st, "setting": SETTINGS.audio_override(self.index) or "auto",
                "codec": self.codec or (track or {}).get("codec"),
                "rate": self.rate or (track or {}).get("rate"),
                "state": self.vstate, "detail": self.detail or None, "active": self._running,
                "listeners": self.viewers, "slotHeld": self.slot_key in owners.get(self.info["nvr"], {}),
                "priority": self.slot_priority(), "priorityName": self.priority_name(),
                "lastPacketAgeMs": self.frame_age_ms(), "levelDb": self.level_db, "peakDb": self.peak_db,
                "silent": self.silent if self.vstate in ("PLAYING", "STALLED") else None,
                "lastSoundAgeMs": round((now - self.last_sound) * 1000) if self.last_sound else None,
                "uiCause": self.ui_cause() or None,
                "packets": self.packets, "kbytes": round(self.bytes / 1024, 1),
                "reconnects": self.reconnects, "opens": self.opens, "firstPacketMs": self.first_packet_ms,
                "sourceSubtype": self.subtype_used, "videoTrackSetUp": self.video_setup or None,
                "listenersFullscreen": self.viewers_full,
                "lastTransitionReason": last[0]["reason"] if last else None,
                "rtsp": self.diag or None,
                "transitions": self.transitions_view(now, 10)}


AUDIO = [AudioWorker(c, i) for i, c in enumerate(CAMERAS)]


def _worker_by_key(key):
    if key >= AUDIO_KEY_BASE:
        return AUDIO[key - AUDIO_KEY_BASE]
    return ORIG_STREAMS[key - ORIG_KEY_BASE] if key >= ORIG_KEY_BASE else STREAMS[key]


class PoolManager:
    """Persistent relay pool: decides which cameras are HOT (CCTV_PERSISTENT).

    Per NVR at most NVR_CAP upstream streams run. Cameras with viewers always get
    their worker; when slots are contended, fullscreen goes before grid (slot
    priority), Original before Standard. Original (main-stream) viewers take their
    slots one at a time, each from the least useful background stream -- the
    Standard stream of the camera whose Original is next only when nothing else is
    left -- so a page switched to Original changes tile by tile, never to blank.
    The rest of the cap -- the background budget = cap minus viewed
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
        s.stop_bg(f"POOL_DEMOTION: {why}")
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
        viewed_orig = [o for o in ORIG_STREAMS if o.info["nvr"] == nvr and o.viewers > 0]
        if viewed or viewed_orig:
            self.last_activity[nvr] = now
        # Original viewers take over slots ONE AT A TIME (opens are serialized anyway):
        # those holding a slot, plus the next one in the slot queue once no other
        # Original is still connecting. Every other tile keeps the camera's live
        # Standard picture ("switching to Original...") until its own turn.
        with _ACTIVE_LOCK:
            owned = set(NVR_OWNERS[nvr])
            queued = dict(_WAIT_PRIO[nvr])
        holding = [o for o in viewed_orig if o.slot_key in owned]
        connecting = [o for o in holding if o.live_since is None]
        waiting = sorted((o for o in viewed_orig if o.slot_key not in owned and o.slot_key in queued),
                         key=lambda o: queued[o.slot_key])
        nxt = waiting[0] if waiting and not connecting else None
        orig_need = len(holding) + (1 if nxt is not None else 0)
        # camera audio: a session someone listens to is demand like a viewer -- also
        # between two attempts (it releases its slot during the back-off; were it not
        # counted, a background stream would be started in that slot and stopped again
        # at the retry). Not while it waits for its own per-NVR audio limit. A
        # lingering one (0 listeners) yields first.
        aud = [a for a in AUDIO if a.info["nvr"] == nvr and a._running]
        aud_need = [a for a in aud if a.viewers > 0 and (a.slot_key in owned or a.slot_key in queued
                                                          or a.reason != "AUDIO_LIMIT")]
        aud_idle = [a for a in aud if a.viewers == 0 and a.slot_key in owned]
        if aud_need:
            self.last_activity[nvr] = now
        # slots left for background streams
        budget = max(0, NVR_CAP[nvr] - len(viewed) - orig_need - len(aud_need))
        bg = [s for s in cams if s.viewers == 0 and s._bg and s._running]
        # Original workers with 0 viewers (quality-switch bridge / linger) hold slots too:
        # they are the first to go when a viewer needs one
        orig_idle = [o for o in ORIG_STREAMS if o.info["nvr"] == nvr and o.viewers == 0 and o._running]
        rank = lambda s: self._rank(s, now, pos)       # noqa: E731
        # no duplicate upstream: once a camera's Original is live for its viewers, its
        # Standard stream without viewers is released (and not promoted back meanwhile)
        live_orig = {o.index for o in holding if o.live_since is not None and now - o.live_since >= 1.0
                     and o.is_live(now)}
        for s in list(bg):
            if s.index in live_orig and s.bg_reason not in ("refresh", "handoff"):
                self._demote(s, "DUPLICATE: this camera's Original is live")
                bg.remove(s)
        # a quality-switch bridge whose Original is already live is being released by
        # the housekeeping right now: THAT slot goes to the next Original in line, so the
        # pool must not free another one as well (two Originals would connect at once)
        for s in list(bg):
            if s.bg_reason == "handoff" and ORIG_STREAMS[s.index].viewers > 0 and ORIG_STREAMS[s.index].is_live(now):
                bg.remove(s)

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
        orig_viewed = {o.index for o in viewed_orig}
        cands = sorted((s for s in cams if s.viewers == 0 and s.bg_reason != "refresh"
                        and s.index not in orig_viewed
                        and self.block_until.get(s.index, 0.0) <= now), key=rank)
        want = cands[:max(0, budget - len(visiting))]

        # 4. demand: viewed cameras need slots -> demote the least useful background
        #    streams NOW (refresh visits first, then the lowest ranked)
        excess = len(bg) + len(orig_idle) + len(aud_idle) - budget
        if excess > 0 and (orig_idle or aud_idle):
            for o in sorted(orig_idle + aud_idle, key=lambda o: o.slot_priority())[:excess]:
                o.stop_bg("POOL_DEMOTION: slot needed by a viewed camera")
                self._note(f"{nvr} {o.label} released (0 viewers; slot needed by a viewed camera)")
            return
        if excess > 0:
            # streams that are not live yet cost nothing to drop: they go before live ones.
            # The Standard stream of a camera watched in Original goes last (it is that
            # tile's picture until its Original runs) -- the next Original's own first.
            twins = {o.index for o in viewed_orig}
            rest = [s for s in bg if s.bg_reason != "refresh" and s.index not in twins]
            tw = [s for s in bg if s.bg_reason != "refresh" and s.index in twins]
            order = (visiting + sorted((s for s in rest if s.live_since is None), key=rank, reverse=True)
                     + sorted((s for s in rest if s.live_since is not None), key=rank, reverse=True)
                     + [s for s in tw if nxt is not None and s.index == nxt.index]
                     + sorted((s for s in tw if nxt is None or s.index != nxt.index), key=rank, reverse=True))
            for s in order[:excess]:
                self._demote(s, "slot needed by a viewed camera")
            return                                     # rebalance once the slots are free

        # no background promotion or swap while Original viewers are still taking over
        # slots: a slot freed for the background would go to the next Original in line
        # (higher priority) and break the one-at-a-time order
        if waiting or connecting:
            return

        # 5. fill free background slots, paced (controlled warm-up)
        starting = sum(1 for s in bg if s.live_since is None)
        free = budget - len(bg) - len(orig_idle) - len(aud_idle)
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
        for s in STREAMS + ORIG_STREAMS + AUDIO:
            with s._vlock:
                leaked = s._running and s.viewers <= 0 and not s._bg
                stale  = now - s.last_use > IDLE_TIMEOUT
            if leaked and stale:
                s.force_stop("REAPER (no viewer, handler gone)")
        with _ACTIVE_LOCK:
            owners = [(k, i) for k, o in NVR_OWNERS.items() for i in o]
        suspect = {key: n for key, n in suspect.items() if key in owners}
        for k, i in owners:
            w = _worker_by_key(i)
            if w.viewers == 0 and not w._bg:
                # 3 checks (10-15 s): a stalled read may legitimately hold a slot
                # for up to READ_TIMEOUT after the viewer left before it notices.
                suspect[(k, i)] = suspect.get((k, i), 0) + 1
                if suspect[(k, i)] == 3:
                    ev(f"[{k.upper()}] SLOT LEAK? {w.label} holds a slot with 0 viewers. {_slot_report(k)}")
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
            "originalViewed": sum(1 for o in ORIG_STREAMS if o.info["nvr"] == k and o.viewers > 0),
            "slotsText": f"{active[k]}/{NVR_CAP[k]}",
            "slots": [{"slot": n + 1, "index": w.index, "camera": SETTINGS.display_name(w.index),
                       "quality": w.quality.upper(), "viewers": w.viewers, "priority": w.slot_priority(),
                       "priorityName": w.priority_name(), "pinned": w.pinned, "preemptable": w.viewers == 0,
                       "state": w.vstate, "status": w.status, "slotAgeMs": round((now - t) * 1000),
                       "lastFrameAgeMs": w.frame_age_ms()}
                      for n, (w, t) in enumerate(sorted(((_worker_by_key(key), t) for key, t in owners[k].items()),
                                                        key=lambda wt: wt[1]))],
            "waiting": len(waiters[k]),
            "waitingCameras": [{"index": w.index, "quality": w.quality, "name": w.name,
                                "displayName": SETTINGS.display_name(w.index), "waitMs": round((now - t) * 1000)}
                               for w, t in ((_worker_by_key(key), t) for key, t in sorted(waiters[k].items()))],
            "owners": [{"index": w.index, "quality": w.quality, "name": w.name,
                        "displayName": SETTINGS.display_name(w.index),
                        "viewers": w.viewers,
                        "status": w.status, "heldMs": round((now - t) * 1000),
                        "lastFrameAgeMs": w.frame_age_ms(),
                        "role": w.role(),
                        "suspectLeak": w.viewers == 0 and not w._bg}
                       for w, t in ((_worker_by_key(key), t) for key, t in sorted(owners[k].items()))],
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
        c["original"] = ORIG_STREAMS[c["index"]].quality_info(now, owners)
        c["audio"] = AUDIO[c["index"]].info_view(now, owners)
    config = {"nvrMaxConn": NVR_MAX_CONN, "nvrCaps": dict(NVR_CAP), "connectMax": CONNECT_MAX,
              "preflight": PREFLIGHT_ENABLED, "preflightTimeoutMs": PREFLIGHT_TIMEOUT_MS,
              "preflightPerNvr": PREFLIGHT_PER_NVR, "openTimeoutMs": OPEN_TIMEOUT_MS,
              "readTimeoutMs": READ_TIMEOUT_MS, "frameMaxAgeS": FRAME_MAX_AGE_S,
              "streamFps": STREAM_FPS, "streamSize": f"{STREAM_W}x{STREAM_H}",
              "persistent": PERSISTENT, "idleFps": IDLE_FPS, "liveMaxAgeS": LIVE_MAX_AGE_S,
              "cacheMaxAgeS": CACHE_MAX_AGE_S, "recentS": RECENT_S,
              "warmConcurrency": WARM_CONCURRENCY, "warmStepS": WARM_STEP_S,
              "minBackgroundHotS": MIN_BG_HOT_S, "backgroundSwapS": BG_SWAP_S,
              "deadRetryS": DEAD_RETRY_S, "refreshEveryS": REFRESH_EVERY_S, "refreshIdleS": REFRESH_IDLE_S,
              "standard": {"subtype": STANDARD_SUBTYPE, "output": f"{STREAM_W}x{STREAM_H}",
                           "jpegQuality": JPEG_QUALITY, "fps": STREAM_FPS},
              "original": {"subtype": ORIGINAL_SUBTYPE, "output": "source size" if not ORIGINAL_MAX_W
                           else f"max width {ORIGINAL_MAX_W}",
                           "gridOnlyMaxWidth": ORIGINAL_GRID_MAX_W or "source size",
                           "jpegQuality": ORIGINAL_JPEG_QUALITY,
                           "fps": ORIGINAL_FPS, "gridFps": "as asked by the page (6)",
                           "fallback": "Standard, labelled, only while the main stream fails"},
              "audio": {"enabled": AUDIO_ENABLED, "transport": "WebSocket /audio/<index> (G.711 pass-through)",
                        "separateSession": True, "lingerS": AUDIO_LINGER_S, "maxPerNvr": AUDIO_MAX_PER_NVR,
                        "readTimeoutS": AUDIO_READ_TIMEOUT_S, "readTimeoutCounts": "RTP packets, not sound",
                        "silenceDbfs": AUDIO_SILENCE_DBFS, "silenceS": AUDIO_SILENCE_S,
                        "interleavedChannel": "as assigned by the NVR's SETUP reply"}}
    pool = {"enabled": PERSISTENT, "running": POOL.running, "ticks": POOL.ticks,
            "recentDecisions": list(POOL.events)[-15:]}
    return {"nvrs": nvrs, "cameras": cams, "config": config, "pool": pool}


def stream_info(i):
    """Credential-free quality state of ONE camera (the fullscreen indicator polls it)."""
    now = time.monotonic()
    with _ACTIVE_LOCK:
        owners = {k: dict(v) for k, v in NVR_OWNERS.items()}
    std, orig = STREAMS[i], ORIG_STREAMS[i]
    return {"index": i, "displayName": SETTINGS.display_name(i),
            "standard": std.quality_info(now, owners), "original": orig.quality_info(now, owners),
            "audio": AUDIO[i].info_view(now, owners)}


def cameras_for_ui():
    """Credential-free camera list for the grid, in TECHNICAL order (compatible with
    earlier clients); the page sorts by displayOrder. 'index' is what /stream/<index>
    uses -- it never changes when a camera is renamed or re-ordered."""
    return [{"index": c["index"], "key": c["key"], "name": c["technicalName"],
             "technicalName": c["technicalName"], "displayName": c["displayName"],
             "displayOrder": c["displayOrder"], "nvr": c["nvr"], "channel": c["channel"],
             "audio": audio_state(c["index"])[0], "audioCodec": (audio_state(c["index"])[1] or {}).get("codec")}
            for c in SETTINGS.snapshot()["cameras"]]


def settings_view(snap):
    """The Settings API payload plus what was detected about each camera's audio."""
    for c in snap.get("cameras", []):
        det, track = audio_detected(c["index"])
        c["audioDetected"] = {"state": det, "codec": (track or {}).get("codec"), "rate": (track or {}).get("rate")}
        c["audioEffective"] = audio_state(c["index"])[0]
    return snap


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# viewer-facing audio states (the worker's own names are more detailed)
_AUDIO_UI_STATE = {"OFF": "OFF", "CONNECTING": "CONNECTING", "WAITING_SLOT": "WAITING", "PLAYING": "PLAYING",
                   "STALLED": "PLAYING", "RECONNECTING": "RECONNECTING", "NVR_UNREACHABLE": "RECONNECTING",
                   "ERROR": "ERROR", "UNAVAILABLE": "UNAVAILABLE"}


class _WebSocket:
    """Minimal RFC 6455 server side on a handler's connection (server frames are
    unmasked). A reader thread answers pings, notices close / a dead peer."""

    def __init__(self, handler):
        self.h = handler
        self.closed = False
        self._wlock = threading.Lock()
        try:
            handler.connection.settimeout(35.0)       # the browser answers our 15 s pings
        except OSError:
            pass
        threading.Thread(target=self._reader, daemon=True).start()

    def _frame(self, op, data):
        n = len(data)
        if n < 126:
            head = bytes([0x80 | op, n])
        elif n < 65536:
            head = bytes([0x80 | op, 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | op, 127]) + struct.pack(">Q", n)
        with self._wlock:
            self.h.wfile.write(head + data)

    def send_binary(self, data):
        self._frame(0x2, data)

    def send_json(self, obj):
        self._frame(0x1, json.dumps(obj).encode())

    def ping(self):
        self._frame(0x9, b"")

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self._frame(0x8, struct.pack(">H", 1000))
            except OSError:
                pass

    def _read(self, n):
        b = self.h.rfile.read(n)
        if not b or len(b) < n:
            raise OSError("closed")
        return b

    def _reader(self):
        try:
            while not self.closed:
                h = self._read(2)
                op, n = h[0] & 0x0F, h[1] & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                if n > 65536:
                    break
                mask = self._read(4) if h[1] & 0x80 else b"\0\0\0\0"
                data = bytes(b ^ mask[k % 4] for k, b in enumerate(self._read(n))) if n else b""
                if op == 0x8:                          # close
                    break
                if op == 0x9:                          # ping -> pong
                    self._frame(0xA, data)
        except (OSError, ValueError):
            pass
        finally:
            self.closed = True


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
            self._send(200, json.dumps(settings_view(SETTINGS.snapshot())).encode(), "application/json")
        elif path == "/api/status":
            self._send(200, json.dumps(system_status()).encode(), "application/json")
        elif path.startswith("/api/stream-info/"):
            i = self._index(path, "/api/stream-info/")
            if i is None:
                self._send(404, b"bad camera", "text/plain")
            else:
                self._send(200, json.dumps(stream_info(i)).encode(), "application/json")
        elif path.startswith("/snapshot/"):
            self._snapshot(path)
        elif path.startswith("/stream/"):
            self._stream(path, query)
        elif path.startswith("/audio/"):
            i = self._index(path, "/audio/")
            if i is None:
                self._send(404, b"bad camera", "text/plain")
            else:
                self._audio_ws(i, query)
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
        if status == 200:
            payload = settings_view(payload)
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
        # ?prio=full: the single-camera (fullscreen) view -- first in line for an NVR slot
        full = (query or {}).get("prio", [""])[0] == "full"
        # ?quality=original: the camera's MAIN stream (default / anything else: Standard)
        if (query or {}).get("quality", [""])[0] == "original":
            try:
                ofps = int((query or {}).get("fps", [ORIGINAL_FPS])[0])
            except (TypeError, ValueError):
                ofps = ORIGINAL_FPS
            return self._stream_original(i, max(1, min(ORIGINAL_FPS, ofps)), full)
        fps = max(1, min(STREAM_FPS, fps))
        cam = STREAMS[i]
        ORIG_STREAMS[i].hold_for_handoff()       # Original -> Standard: bridge until live
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

    def _part(self, jpg, state):
        # X-Frame-State (live|standard|cached|status) lets tools measure what a viewer
        # actually saw; browsers ignore unknown part headers
        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                         b"Content-Length: " + str(len(jpg)).encode() +
                         b"\r\nX-Frame-State: " + state.encode() +
                         b"\r\n\r\n" + jpg + b"\r\n")

    def _stream_original(self, i, fps, full):
        """MJPEG of camera i's MAIN stream (Original mode). One Original worker per
        camera, shared by every Original viewer. Until its first Original frame
        arrives, the viewer keeps seeing the camera's Standard picture with a small
        "STANDARD / switching to Original" label (never a blank tile). If the main
        stream FAILS, the viewer is given the Standard stream -- labelled "Original
        unavailable" -- and is switched to Original by itself as soon as the main
        stream works. Waiting for an NVR slot is labelled as such (no fallback: the
        Standard stream would need the same slot).
        Only NEW pictures are written (a full-resolution JPEG is large): at most `fps`
        per second, and the last one again after 1 s without a new one -- that write
        is also how a closed connection is noticed."""
        orig, std = ORIG_STREAMS[i], STREAMS[i]
        std.hold_for_handoff()                   # Standard -> Original: bridge until live
        n = orig.add_viewer(full=full, fps=fps)
        ev(f"[{orig.label}] HTTP viewer connected (viewers {n - 1} -> {n}){' [fullscreen]' if full else ''}")
        t_conn = time.monotonic()
        sent_live = False
        fallback = False                     # this viewer also holds a Standard viewer (main stream failed)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            gap = 0.85 / fps                     # min time between two parts (cap at ~fps)
            last, t_last = None, 0.0
            while True:
                orig.last_use = std.last_use = time.time()
                jpg, state = orig.frame_for_viewer()
                if state == "live":
                    if fallback:                 # Original works now: drop the Standard stand-in
                        std.remove_viewer(full=full)
                        fallback = False
                        with orig._vlock:
                            orig.fallback_viewers -= 1
                        ev(f"[{orig.label}] Original is live again -- Standard fallback released")
                    if not sent_live:
                        sent_live = True
                        orig.first_http_ms = _ms(t_conn)
                        ev(f"[{orig.label}] first Original frame sent to viewer {orig.first_http_ms} ms after it connected")
                else:
                    unavailable = orig.unavailable()
                    if unavailable and not fallback:
                        std.add_viewer(full=full)
                        fallback = True
                        with orig._vlock:
                            orig.fallback_viewers += 1
                        ev(f"[{orig.label}] Original unavailable ({orig.status}) -- showing Standard")
                    detail = ("Original unavailable" if unavailable else
                              "Original waiting for NVR slot" if orig.status == S_WAIT_SLOT else
                              "switching to Original...")
                    lab = std.labelled_view("STANDARD", detail)
                    if lab is not None:
                        jpg, state = lab, "standard"
                    # else: orig.frame_for_viewer() above already gave the freshest picture
                    # of either quality, stamped CACHED with the ORIGINAL stream's own state
                now = time.monotonic()
                if jpg and ((jpg is not last and now - t_last >= gap) or now - t_last >= 1.0):
                    self._part(jpg, state)
                    last, t_last = jpg, now
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            n = orig.remove_viewer(full=full, fps=fps)
            if fallback:
                std.remove_viewer(full=full)
                with orig._vlock:
                    orig.fallback_viewers -= 1
            ev(f"[{orig.label}] HTTP viewer disconnected (viewers {n + 1} -> {n})")

    def _audio_ws(self, i, query):
        """WebSocket: camera i's audio for ONE listener. Binary messages =
        [1, codec (0 = PCMU, 8 = PCMA), 4-byte packet number] + G.711 bytes as they
        arrive from the NVR (~40 ms each); text messages = {"state": ...} whenever the
        audio state changes. Only NEW audio is sent (never a stale backlog)."""
        key = self.headers.get("Sec-WebSocket-Key", "")
        if "websocket" not in self.headers.get("Upgrade", "").lower() or not key:
            self._send(400, b"WebSocket upgrade required", "text/plain")
            return
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        ws = _WebSocket(self)
        aw = AUDIO[i]
        state, track = audio_state(i)
        if state in ("disabled", "unavailable"):
            ws.send_json({"state": "UNAVAILABLE", "detail": "Audio is disabled for this camera"
                          if state == "disabled" else "No audio available"})
            ws.close()
            return
        full = (query or {}).get("prio", [""])[0] == "full"
        n = aw.add_listener(full=full)
        ev(f"[{aw.label}] listener connected (listeners {n - 1} -> {n}){' [fullscreen]' if full else ''}")
        try:
            last, sent, t_ping = aw.latest(), None, time.monotonic()
            now_ui = lambda: (aw.vstate, aw.ui_cause())
            while not ws.closed:
                with aw._cond:
                    aw._cond.wait_for(lambda: aw._n > last or now_ui() != sent or ws.closed, timeout=1.0)
                    pk = [p for p in aw._pkts if p[0] > last]
                    cur = now_ui()
                st = cur[0]
                if cur != sent:
                    # viewers get a state + a coarse cause; the precise reason is in /api/status
                    ws.send_json({"state": _AUDIO_UI_STATE.get(st, st), "cause": cur[1],
                                  "silent": cur[1] == "silent", "codec": aw.codec, "rate": aw.rate})
                    sent = cur
                codec = 0 if aw.codec == "PCMU" else 8
                for n_, payload in pk:
                    ws.send_binary(bytes([1, codec]) + struct.pack(">I", n_ & 0xFFFFFFFF) + payload)
                    last = n_
                aw.last_use = time.time()
                if time.monotonic() - t_ping >= 15.0:        # keeps proxies / NAT awake, finds dead peers
                    ws.ping()
                    t_ping = time.monotonic()
                if st == "UNAVAILABLE":
                    break
        except OSError:
            pass
        finally:
            n = aw.remove_listener(full=full)
            ev(f"[{aw.label}] listener disconnected (listeners {n + 1} -> {n})")
            ws.close()

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
    _ensure_housekeeping()                        # stall watchdog + switch bridges
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
        for s in STREAMS + ORIG_STREAMS + AUDIO:
            s.force_stop("SHUTDOWN")


if __name__ == "__main__":
    main()
