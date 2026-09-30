"""Recorded playback: availability search, sessions, per-camera playback workers.

  browser  --search-->  /api/playback/search   (from/to NVR-local, cameras)
           <--result--  per camera: recorded / not recorded (+ segments on the LAN)
           ==WS======>  /api/playback/ws?sid=  (commands: play pause seek speed audio page
           <==frames==                          focus retry stop; JPEG frames with their
                                                recorded time, G.711 audio, state JSON)

A session = one playback page (one search). Each camera shown is a PlaybackWorker:
its OWN RTSP session to the NVR's recording (never a live worker), one NVR slot
(key PLAYBACK_KEY_BASE + n, counted with live video and audio in the per-NVR cap),
an H.265 decoder (OpenCV/FFmpeg fed with the depacketised stream) and a JPEG output.
All cameras of a session follow ONE timeline (a virtual clock re-anchored on the
cameras that play); seek / pause / speed apply to all of them.

Limits (configurable, default = what was proven): CCTV_PLAYBACK_MAX_WORKERS
server-wide, default 3 (measured on the i7-13620H dev laptop: 3 cameras decode
2560x1440 H.265 at 25/s with ~1 CPU core each; 4 fall behind there) and
CCTV_<NVR>_PLAYBACK_MAX per NVR, default 2 (NVR1: 2 proven; NVR2: 4 proven). Measure
on the production host before raising them.
"""
import io
import os
import json
import time
import secrets
import datetime
import threading
import collections
from types import SimpleNamespace

import cv2

import rtsp_audio as ra
import rtsp_playback as rpb
import playback_time as pt
import nvr_api


def _envint(k, d):
    try:
        return int(os.environ.get(k, "") or d)
    except ValueError:
        return d


def _envfloat(k, d):
    try:
        return float(os.environ.get(k, "") or d)
    except ValueError:
        return d


MAX_WORKERS    = max(1, _envint("CCTV_PLAYBACK_MAX_WORKERS", 3))
PER_NVR_MAX_DEFAULT = 2
# Longest From-To of one search WHILE an NVR's oldest recording is not known (see
# RETENTION_REFRESH_S). NOT an NVR limit (the NVRs' recording search answered 30-day
# queries completely); a month covers what the NVRs keep (~3-4 weeks measured).
MAX_RANGE_H    = _envfloat("CCTV_PLAYBACK_MAX_RANGE_H", 31 * 24.0)
# One recording-search request covers at most this (a longer range is split and merged;
# a 7-day query returned exactly the files of seven 1-day queries, 4-5x faster).
SEARCH_CHUNK_H = _envfloat("CCTV_PLAYBACK_SEARCH_CHUNK_H", 7 * 24.0)
# One NVR playback SESSION covers at most this; playback continues into the next one by
# itself. (Measured: a session covers at most ~58 h (NVR1) / ~59 h (NVR2) -- PLAY further
# -> 500; 24 h and 48 h windows were fully covered.)
SESSION_SPAN_H = _envfloat("CCTV_PLAYBACK_SESSION_SPAN_H", 24.0)
SEARCH_WAIT_S  = _envfloat("CCTV_PLAYBACK_SEARCH_WAIT_S", 3.0)  # search reply waits this long, the rest streams
# How far back a search may start follows each NVR's OLDEST recording (its disk is full
# and overwrites the oldest footage: measured NVR1 ~27 days, NVR2 ~22.5 days). It is
# read in the background every RETENTION_REFRESH_S (metadata only, ~1 request per
# camera); MAX_RANGE_H only applies while an NVR's oldest recording is not known.
RETENTION_REFRESH_S = max(60.0, _envfloat("CCTV_PLAYBACK_RETENTION_REFRESH_S", 1800.0))
RETENTION_LOOKBACK_D = 365                   # looked for back to (no NVR here keeps a month)
GRACE_S        = _envfloat("CCTV_PLAYBACK_GRACE_S", 20.0)       # page gone -> stop after this
PAUSE_HOLD_S   = _envfloat("CCTV_PLAYBACK_PAUSE_HOLD_S", 60.0)  # paused longer -> NVR session closed
KEYFRAME_ADJ_S = _envfloat("CCTV_PLAYBACK_KEYFRAME_ADJ_S", 1.0) # first frame after a seek: ~half a GOP earlier
SEARCH_CACHE_S = _envfloat("CCTV_PLAYBACK_SEARCH_CACHE_S", 45.0)
SESSION_MAX_S  = _envfloat("CCTV_PLAYBACK_SESSION_MAX_S", 4 * 3600.0)
STALL_S        = _envfloat("CCTV_PLAYBACK_STALL_S", 12.0)       # no packet while playing -> reconnect
MAX_LAG_FRAMES = int(25 * _envfloat("CCTV_PLAYBACK_MAX_DECODE_LAG_S", 3.0))  # decoder behind -> skip ahead
# FFmpeg threads per camera decoder. Its default (one per CPU) costs ~70% more CPU per
# picture when several cameras play (4 x 16 threads fighting for the cores) and holds
# ~16 pictures in flight; 4 threads still decode 2560x1440 H.265 at 35+ pictures/s each
# with 4 cameras at once (measured), with a shorter pipeline (faster first picture).
DECODE_THREADS = max(1, _envint("CCTV_PLAYBACK_DECODE_THREADS", 4))
GAP_MIN_S      = 3.0
TILES_PER_PAGE = 6
OUT_GRID       = (_envint("CCTV_PLAYBACK_GRID_WIDTH", 960), _envfloat("CCTV_PLAYBACK_GRID_FPS", 10.0))
OUT_SINGLE     = (_envint("CCTV_PLAYBACK_SINGLE_WIDTH", 1280), _envfloat("CCTV_PLAYBACK_SINGLE_FPS", 12.0))
# (one camera: 1280 px / 12 per s measured ~1.55 cores, 1600 px / 15 per s ~2.4 cores)
JPEG_QUALITY   = _envint("CCTV_PLAYBACK_JPEG_QUALITY", 75)
SPEEDS         = (1, 2, 4)                # 0.5x is NOT offered: the NVR stops after one frame
KEY_BASE       = 3000                     # NVR slot key of a playback worker = 3000 + n
PRIO_PLAYBACK  = 70                       # below every WATCHED live video (80-100), above audio
SEARCH_PER_NVR = 3

# server.py plugs its slot machinery, NVR endpoints and logger in here (configure()).
HOOK = SimpleNamespace(
    ev=lambda msg: None,
    endpoint=None,                        # nvr -> (host, port)   (LAN or public, as live video)
    nvrs=None,                            # NVRS dict (credentials stay server-side)
    remote=lambda: False,                 # True when the NVRs are reached via the public IP
    acquire_slot=None,                    # (worker, gen, nvr) -> bool (priority-ordered wait)
    release_slot=None,                    # (nvr, key, reason)
    pool_wake=lambda: None,
    monitor=lambda nvr: None,             # NetworkMonitor health or None
    cameras=None,                         # CAMERAS list
    display_name=lambda i: str(i),
    per_nvr_max={},
    audit_path=None,
    api_host=None,                        # nvr -> host[:port] of the vendor web API (tests)
)


def configure(**kw):
    for k, v in kw.items():
        setattr(HOOK, k, v)


def per_nvr_max(nvr):
    return max(0, int(HOOK.per_nvr_max.get(nvr, PER_NVR_MAX_DEFAULT)))


# ── worker registry (the server's slot table maps slot keys back to workers) ──────
WORKERS = {}
_REG_LOCK = threading.Lock()
_SEQ = [0]


def worker_by_key(key):
    with _REG_LOCK:
        return WORKERS.get(key)


def _new_key():
    with _REG_LOCK:
        _SEQ[0] = (_SEQ[0] + 1) % 900
        while KEY_BASE + _SEQ[0] in WORKERS:
            _SEQ[0] = (_SEQ[0] + 1) % 900
        return KEY_BASE + _SEQ[0]


def running_workers(nvr=None):
    with _REG_LOCK:
        ws = list(WORKERS.values())
    return [w for w in ws if w._running and (nvr is None or w.nvr == nvr)]


# ── decoder: OpenCV/FFmpeg fed with the depacketised Annex-B stream ─────────────
class Feed(io.BufferedIOBase):
    """Bytes as they arrive from the NVR; read() blocks until data or close().
    Seeks are honoured within the bytes still kept (FFmpeg probes the start); older
    data is dropped once the decoder is past it."""

    KEEP = 4 * 1024 * 1024

    def __init__(self):
        super().__init__()
        self._buf = bytearray()
        self._base = 0                    # absolute offset of _buf[0]
        self._pos = 0
        self._eof = False
        self._cv = threading.Condition()

    def readable(self):
        return True

    def seekable(self):
        return True

    def write_au(self, data):
        with self._cv:
            self._buf += data
            self._cv.notify_all()

    def end(self):
        with self._cv:
            self._eof = True
            self._cv.notify_all()

    def read(self, size=-1):
        with self._cv:
            self._cv.wait_for(lambda: self._pos < self._base + len(self._buf) or self._eof)
            avail = self._base + len(self._buf) - self._pos
            if avail <= 0:
                return b""
            n = avail if size is None or size < 0 else min(size, avail)
            i = self._pos - self._base
            out = bytes(self._buf[i:i + n])
            self._pos += n
            drop = self._pos - self._base - self.KEEP
            if drop > 1024 * 1024:
                del self._buf[:drop]
                self._base += drop
            return out

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        with self._cv:
            p = offset if whence == 0 else self._pos + offset if whence == 1 else -1
            if self._base <= p <= self._base + len(self._buf):
                self._pos = p
                return p
            return -1


class CvDecoder:
    """Decoding thread. push(access unit, meta) in order; on_frame(bgr, meta) is called
    for every decoded picture with the meta of its access unit (no B-frames: decode
    order = output order). want(meta) is asked first: a picture that will not be shown
    (frame-rate limit) is decoded but not converted to BGR -- that conversion is a third
    of the CPU and single-threaded -- and on_frame gets None for it."""

    def __init__(self, on_frame, params=b"", label="", want=None):
        self.on_frame, self.params, self.label, self.want = on_frame, params, label, want
        self.feed = Feed()
        self.fifo = collections.deque()
        self.closed = False
        self.flush = False                   # end of the recording: convert every picture left
        self.error = None
        self.frames = 0
        self._thread = None

    def push(self, au, meta):
        if self._thread is None:
            au = self.params + au
            self._thread = threading.Thread(target=self._run, name=f"pbdec {self.label}", daemon=True)
            self._thread.start()
        self.fifo.append(meta)
        self.feed.write_au(au)

    def backlog(self):
        """Pictures pushed but not decoded yet (FFmpeg's frame threads hold a few)."""
        return len(self.fifo)

    def _run(self):
        cap = None
        try:
            cap = cv2.VideoCapture(self.feed, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, DECODE_THREADS])
            if not cap.isOpened():
                self.error = "decoder could not open the stream"
                return
            while not self.closed:
                if not cap.grab():
                    break
                self.frames += 1
                meta = self.fifo.popleft() if self.fifo else None
                frame = None
                try:
                    if self.flush or self.want is None or self.want(meta):
                        ok, frame = cap.retrieve()
                        frame = frame if ok else None
                    self.on_frame(frame, meta)
                except Exception as e:                   # never let delivery kill decoding
                    self.error = f"frame delivery: {e!r}"
        except Exception as e:
            self.error = f"decoder: {type(e).__name__}"
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

    def finish(self, timeout=3.0):
        """End of the recording: end of input makes FFmpeg output the pictures its
        frame threads still hold (otherwise the last ~0.5 s would never be shown)."""
        self.flush = True
        self.feed.end()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

    def close(self):
        self.closed = True
        self.feed.end()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3.0)


def decode_picture(data):
    """One key frame (parameter sets + IDR slices) -> BGR picture, or None."""
    src = io.BytesIO(data)                # referenced until the capture is released
    cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])
    try:
        ok, frame = cap.read() if cap.isOpened() else (False, None)
        return frame if ok else None
    finally:
        try:
            cap.release()
        except Exception:
            pass


class KeyDecoder:
    """2x / 4x: the NVR sends key frames only (one or two a second). A continuous
    decoder would hold several of them in its frame threads before showing anything,
    so each key frame is decoded on its own (~0.2 s). Other frames are skipped; when
    decoding falls behind, only the newest key frame waits."""

    def __init__(self, on_frame, params=b"", label=""):
        self.on_frame, self.params, self.label = on_frame, params, label
        self.fifo = collections.deque(maxlen=1)
        self.closed = False
        self.error = None
        self.frames = 0
        self._busy = False
        self._cv = threading.Condition()
        self._thread = threading.Thread(target=self._run, name=f"pbkey {label}", daemon=True)
        self._thread.start()

    def push(self, au, meta):
        if not (meta and meta[2]):
            return
        with self._cv:
            self.fifo.append((au, meta))
            self._cv.notify_all()

    def _run(self):
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self.fifo or self.closed)
                if self.closed:
                    return
                au, meta = self.fifo.popleft()
                self._busy = True
            try:
                frame = decode_picture(self.params + au)
                if frame is not None:
                    self.frames += 1
                    self.on_frame(frame, meta)
            except Exception as e:                       # never let one picture kill decoding
                self.error = f"key frame: {type(e).__name__}"
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def finish(self, timeout=3.0):
        with self._cv:
            self._cv.wait_for(lambda: not (self.fifo or self._busy) or self.closed, timeout)

    def close(self):
        with self._cv:
            self.closed = True
            self._cv.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)


def new_decoder(on_frame, params=b"", label="", want=None):
    """Factory (tests replace it with a fake that needs no video codec)."""
    return CvDecoder(on_frame, params, label, want)


def new_key_decoder(on_frame, params=b"", label=""):
    """Factory for 2x / 4x (tests replace it too)."""
    return KeyDecoder(on_frame, params, label)


class Decoders:
    """The decoders of one NVR session: continuous at 1x (P-frames need the frames
    before them), key frame by key frame at 2x / 4x (created when first needed).
    When the CPU cannot keep up and the 1x decoder falls more than MAX_LAG_FRAMES
    behind, it is replaced at the next key frame: the picture skips ahead instead of
    running later and later (and the unread stream piling up in memory)."""

    def __init__(self, on_frame, params=b"", label="", want=None):
        self._args = (on_frame, params, label)
        self._want = want
        self.normal = new_decoder(on_frame, params, label, want)
        self.fast = None
        self.resyncs = 0

    def backlog(self):
        return getattr(self.normal, "backlog", lambda: 0)()

    def push(self, au, meta, fast=False):
        if not fast:
            if meta and meta[2] and self.backlog() > MAX_LAG_FRAMES:
                old, self.normal = self.normal, new_decoder(*self._args, self._want)
                self.resyncs += 1
                if self.resyncs == 1 or self.resyncs % 10 == 0:
                    HOOK.ev(f"[{self._args[2]}] decoding behind by {old.backlog()} pictures: skipped to the "
                            f"next key frame ({self.resyncs} so far; CPU too busy for this many playbacks)")
                threading.Thread(target=old.close, daemon=True).start()
            self.normal.push(au, meta)
            return
        if self.fast is None:
            self.fast = new_key_decoder(*self._args)
        self.fast.push(au, meta)

    def finish(self, timeout=3.0):
        for d in (self.normal, self.fast):
            fin = getattr(d, "finish", None)
            if fin is not None:
                fin(timeout)

    def close(self):
        for d in (self.normal, self.fast):
            if d is not None:
                d.close()


def encode_jpeg(frame, width):
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, max(2, int(h * width / w) // 2 * 2)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    return bytes(buf) if ok else None


# ── availability search ─────────────────────────────────────────────────────────
_SEARCH_CACHE = {}
_SEARCH_LOCK = threading.Lock()
_SEARCH_SEM = collections.defaultdict(lambda: threading.BoundedSemaphore(SEARCH_PER_NVR))
_CAPS = {}                                   # nvr -> (checked monotonic, vendor api available)


def _nvr_host(nvr):
    """The NVR's LAN address (the web API is not forwarded to the internet)."""
    return HOOK.api_host(nvr) if HOOK.api_host else HOOK.nvrs[nvr]["ip"]


def vendor_api(nvr):
    """The vendor web API if it answers (office LAN only), else None. Cached 5 min."""
    if HOOK.remote():
        return None
    now = time.monotonic()
    c = _CAPS.get(nvr)
    n = HOOK.nvrs[nvr]
    api = nvr_api.api_for(nvr, _nvr_host(nvr), n["user"], n["pass"])
    if c is not None and now - c[0] < 300:
        return api if c[1] else None
    ok = api.available()
    _CAPS[nvr] = (now, ok)
    return api if ok else None


def search_camera(nvr, channel, a, b):
    """-> {"status": found|partial|none|unreachable|auth|error, "recordedS", "segments"
    [(start, end)] or None (RTSP only), "method": vendor|rtsp}."""
    key = (nvr, channel, a, b)
    now = time.monotonic()
    with _SEARCH_LOCK:
        hit = _SEARCH_CACHE.get(key)
        if hit and now - hit[0] < SEARCH_CACHE_S:
            return hit[1]
    with _SEARCH_SEM[nvr]:
        res = _search_uncached(nvr, channel, a, b)
    if res["status"] in ("found", "partial", "none"):
        with _SEARCH_LOCK:
            _SEARCH_CACHE[key] = (time.monotonic(), res)
    return res


def _search_uncached(nvr, channel, a, b):
    total = (b - a).total_seconds()
    api = vendor_api(nvr)
    if api is not None:
        try:
            files = set()                                 # a file across two windows comes twice
            for x, y in pt.plan_ranges(a, b, SEARCH_CHUNK_H):
                files.update(api.find_files(channel, x, y))
            segs = nvr_api.merge_segments(sorted(files), a, b)
            rec = sum((e - s).total_seconds() for s, e in segs)
            st = "none" if rec <= 0 else ("found" if rec >= total - GAP_MIN_S else "partial")
            return {"status": st, "recordedS": round(rec), "segments": segs, "method": "vendor"}
        except OSError:
            pass                                          # fall back to RTSP
    # RTSP only (outside the office): DESCRIBE tells THAT something is recorded, not where
    # the gaps are -- NVR2 answers "window end - first recorded moment" (inner gaps are
    # invisible), NVR1 answers only when the window STARTS inside a recording file (then
    # with that file's length; a window starting in a gap reads as empty). Gaps show up
    # while playing; "partial" only means the recording starts after From.
    host, port = HOOK.endpoint(nvr)
    n = HOOK.nvrs[nvr]
    st, rec = rpb.describe_range(host, port, n["user"], n["pass"], channel, a, b)
    res = {"status": st, "recordedS": None, "segments": None, "method": "rtsp"}
    if st not in ("ok", "none"):
        return res
    try:
        if st == "ok" and (rec or 0) > 0:
            res["status"] = "found" if rec >= total - GAP_MIN_S or _recorded_at(nvr, channel, a, b) else "partial"
        else:                                             # NVR1 with From in a gap: look further
            res["status"] = "partial" if _probe_next_start(nvr, channel, a, b) else "none"
    except OSError as e:
        res["status"] = str(e) if str(e) in ("unreachable", "auth") else "error"
    return res


def _describe(nvr, channel, x, y):
    host, port = HOOK.endpoint(nvr)
    n = HOOK.nvrs[nvr]
    st, r = rpb.describe_range(host, port, n["user"], n["pass"], channel, x, y, timeout=6.0)
    if st not in ("ok", "none"):
        raise OSError(st)
    return r or 0.0


def _recorded_at(nvr, channel, x, b):
    """Is moment x (whole second) recorded? (a 2 s window starting there: both NVRs
    answer for a window that starts inside a recording)"""
    x = x.replace(microsecond=0)
    return x < b and _describe(nvr, channel, x, min(b, x + datetime.timedelta(seconds=2))) > 0


def _first_recorded(nvr, channel, lo, hi, b):
    """First recorded whole second in (lo, hi]: lo not recorded, hi recorded (bisection
    with 2-second DESCRIBE checks, ~12 for an hour). Where the NVR counts a window that
    starts just before a recording (NVR2; NVR1 at its oldest recording) it is 1 s early."""
    one = datetime.timedelta(seconds=1)
    while (hi - lo) > one:
        mid = lo + datetime.timedelta(seconds=int((hi - lo).total_seconds() // 2))
        if _recorded_at(nvr, channel, mid, b):
            hi = mid
        else:
            lo = mid
    return hi


def _probe_next_start(nvr, channel, t, b):
    """RTSP only (remote): first recorded whole second >= t before b, or None -- a
    moment a new NVR session can start at. NVR2: DESCRIBE of [t, b] = b - first recorded
    moment (verified); NVR1 answers so too before its OLDEST recording, but a window of
    many hours comes back short (24 h: 27 min 49 s short, measured) -- so the moment
    found is checked (recorded just before it too -> bisect back). NVR1 in a gap between
    recordings (reads such a window as empty): probe t+4 s, +8 s, +16 s ... then bisect."""
    one = datetime.timedelta(seconds=1)
    t = t.replace(microsecond=0) + (one if t.microsecond else datetime.timedelta(0))
    if t >= b:
        return None
    if _recorded_at(nvr, channel, t, b):
        return t
    bb = min(b, t + datetime.timedelta(hours=SESSION_SPAN_H))   # (a multi-day DESCRIBE answers
    r = _describe(nvr, channel, t, bb)                             #  the session's ~58 h span)
    if r > 0:
        s = max(t, (bb - datetime.timedelta(seconds=r)).replace(microsecond=0))
        for c in (s, s + one, s - one):                   # (+-1 s rounding)
            if t <= c < bb and _recorded_at(nvr, channel, c, b):
                if c - t > 2 * one and _recorded_at(nvr, channel, c - 2 * one, b):
                    return _first_recorded(nvr, channel, t, c, b)   # not the first recorded moment
                return c
    prev, step = t, 4
    while prev < b:
        x = min(t + datetime.timedelta(seconds=step), b - 2 * one)
        if x <= prev:
            break
        if _recorded_at(nvr, channel, x, b):
            return _first_recorded(nvr, channel, prev, x, b)       # prev empty, x recorded
        prev, step = x, step * 2
    return None


# ── how far back each NVR keeps footage (its oldest recording) ──────────────────────
# The disks are full: each NVR overwrites its oldest footage all the time, so the start
# of the oldest recording file moves forward by the hour. A search may reach back to it.
_RETENTION = {}                              # nvr -> {"oldest", "cameras", "method", "checked", "error"}
_RET_LOCK = threading.Lock()
_RET_WINDOW = datetime.timedelta(days=30)    # one oldest-recording query (30-day queries measured OK)
# RTSP only: the oldest recorded second is found exactly on ONE camera; the others' files
# start within ~5 s of it (measured on both NVRs) -- the NVR's oldest is taken 10 s earlier
_RTSP_MARGIN = datetime.timedelta(seconds=10)


def _channels(nvr):
    return sorted({c["channel"] for c in (HOOK.cameras or []) if c["nvr"] == nvr})


def _oldest_vendor(api, channel, now):
    """Office LAN: start of `channel`'s oldest recording file (None: nothing recorded in
    the last 30 days). One query per 30 days, stepping further back only while the
    recording reaches a window's start -- one request for an NVR keeping < 30 days."""
    lo = now - datetime.timedelta(days=RETENTION_LOOKBACK_D)
    best, b = None, now
    while b > lo:
        a = max(lo, b - _RET_WINDOW)
        t = api.oldest_recording(channel, a, b)
        if t is None:
            break
        best = t if best is None else min(best, t)
        if t > a + datetime.timedelta(hours=1):
            break                                         # the recording starts inside this window
        b = a
    return best


def _recorded_near(nvr, channel, x, b):
    """RTSP only: anything recorded at x, x+20 min or x+40 min (steps over short gaps,
    e.g. NVR1's ~37 s nightly gap at 02:00)?"""
    for k in (0, 20, 40):
        y = x + datetime.timedelta(minutes=k)
        if y < b and _recorded_at(nvr, channel, y, b):
            return True
    return False


def _oldest_rtsp(nvr):
    """RTSP only (outside the office): where the NVR's recording starts, by DESCRIBE
    checks -- bisection on the first camera that records (ends 20-40 min before its
    oldest footage), checks further back in case a long gap (e.g. a power cut) misled
    it, the exact first recorded second from there (the gap probe playback uses), then
    ONE check per other camera for footage older than that (an NVR's cameras share its
    disk and lose their oldest footage together: their files start within ~5 s).
    ~70-100 short requests. -> {channel: its oldest recorded second}"""
    now = pt.nvr_now()
    lo0 = now - datetime.timedelta(days=RETENTION_LOOKBACK_D)
    hour, step = datetime.timedelta(hours=1), datetime.timedelta(minutes=20)
    recent = [datetime.timedelta(minutes=15), datetime.timedelta(hours=1, minutes=30), datetime.timedelta(hours=6),
              datetime.timedelta(days=1), datetime.timedelta(days=3)]
    back = [datetime.timedelta(hours=6)] + [datetime.timedelta(days=d) for d in (1, 2, 4, 8, 16, 32, 64, 128)]

    def bisect(ch, hi):                                   # hi: a recorded moment
        for _ in range(4):
            lo = lo0
            if _recorded_near(nvr, ch, lo, now):
                return lo                                 # at least this old
            # "recorded near x" = x, x+20 or x+40 min recorded: true from 40 min before the
            # recording starts, so hi ends 20-40 min before it
            while hi - lo > step:
                mid = (lo + (hi - lo) / 2).replace(microsecond=0)
                if _recorded_near(nvr, ch, mid, now):
                    hi = mid
                else:
                    lo = mid
            older = next((hi - d for d in back if hi - d > lo0 and _recorded_near(nvr, ch, hi - d, now)), None)
            if older is None:
                return hi
            hi = older                                    # footage behind a long gap: again
        return hi

    def exact(ch, t):                                     # the first recorded second from t on
        x = _probe_next_start(nvr, ch, t, now)
        return x if x is not None and x - t <= hour else t

    out, first = {}, None
    for ch in _channels(nvr):
        if first is None:
            hi = next((now - d for d in recent if _recorded_near(nvr, ch, now - d, now)), None)
            if hi is not None:                            # (a camera without recent footage: the next one)
                first = out[ch] = exact(ch, bisect(ch, hi))
        elif first - _RTSP_MARGIN > lo0 and _recorded_at(nvr, ch, first - _RTSP_MARGIN, now):
            first = out[ch] = exact(ch, bisect(ch, first - _RTSP_MARGIN))   # this camera keeps older footage
    return out


def refresh_retention(nvr):
    """Read NVR `nvr`'s oldest recording (metadata only, one request at a time).
    On failure the last good answer is kept."""
    now = pt.nvr_now()
    rec = {"oldest": None, "cameras": {}, "method": None, "checked": time.time(), "error": None}
    try:
        api = vendor_api(nvr)
        if api is not None:                               # office LAN: every camera, ~1 s each
            for ch in _channels(nvr):
                rec["cameras"][ch] = _oldest_vendor(api, ch, now)
            rec["method"] = "vendor"
        else:
            rec["cameras"] = _oldest_rtsp(nvr)
            rec["method"] = "rtsp"
        found = [t for t in rec["cameras"].values() if t is not None]
        rec["oldest"] = min(found) if found else None
        if rec["oldest"] is not None and rec["method"] == "rtsp":
            rec["oldest"] -= _RTSP_MARGIN                 # (never later than any camera's oldest)
    except OSError as e:
        rec["error"] = str(e)[:120] or type(e).__name__
        with _RET_LOCK:
            old = _RETENTION.get(nvr)
        if old and old.get("oldest"):                     # keep the last good answer
            rec.update({k: old[k] for k in ("oldest", "cameras", "method")})
    with _RET_LOCK:
        _RETENTION[nvr] = rec
    HOOK.ev(f"[PLAYBACK] {nvr.upper()} oldest recording: "
            + (f"{pt.fmt_local(rec['oldest'])} ({_days_text(now - rec['oldest'])}) via {rec['method']}"
               if rec["oldest"] else "not known") + (f" -- check failed: {rec['error']}" if rec["error"] else ""))
    return rec


def _days_text(td):
    d = round(td.total_seconds() / 86400, 1)
    return f"{d:g} day" + ("" if d == 1 else "s")


def nvr_oldest(nvr):
    with _RET_LOCK:
        r = _RETENTION.get(nvr)
    return r["oldest"] if r else None


def retention_view():
    """For the page / Settings: {"NVR1": {"oldest", "oldestMs", "days", "method", "error",
    "checkedAgoS", "cameras": {channel: ms or None}}}. Nothing secret."""
    now = pt.nvr_now()
    out = {}
    for nvr in HOOK.nvrs or {}:
        with _RET_LOCK:
            r = dict(_RETENTION.get(nvr) or {})
        o = r.get("oldest")
        out[nvr.upper()] = {"oldest": pt.fmt_local(o) if o else None, "oldestMs": pt.to_ms(o) if o else None,
                            "days": round((now - o).total_seconds() / 86400, 1) if o else None,
                            "method": r.get("method"), "error": r.get("error"),
                            "checkedAgoS": round(time.time() - r["checked"]) if r.get("checked") else None,
                            "cameras": {str(ch): (pt.to_ms(t) if t else None) for ch, t in (r.get("cameras") or {}).items()}}
    return out


_RET_THREAD = [None]


def start_retention_watch():
    """Background: every NVR's oldest recording at start, then every RETENTION_REFRESH_S
    (after a failed check again in 2 minutes)."""
    def loop():
        while True:
            ok = True
            for nvr in list(HOOK.nvrs or {}):
                try:
                    ok = refresh_retention(nvr)["error"] is None and ok
                except Exception as e:                    # never let it die
                    ok = False
                    HOOK.ev(f"[PLAYBACK] oldest-recording check {nvr.upper()}: {e!r}")
            time.sleep(RETENTION_REFRESH_S if ok else min(RETENTION_REFRESH_S, 120.0))
    if _RET_THREAD[0] is None:
        _RET_THREAD[0] = threading.Thread(target=loop, name="playback-retention", daemon=True)
        _RET_THREAD[0].start()


def retention_limit(nvrs):
    """-> (the oldest recording over these NVRs -- None unless known for every one of
    them --, text for the user: "NVR1 keeps 27 days of footage (from 01 Sep 2026 17:00)")"""
    olds = {n: nvr_oldest(n) for n in nvrs}
    if not olds or any(o is None for o in olds.values()):
        return None, ""
    now = pt.nvr_now()
    text = ", ".join(f"{n.upper()} keeps {_days_text(now - o)}{' of footage' if i == 0 else ''} (from {o:%d %b %Y %H:%M})"
                     for i, (n, o) in enumerate(sorted(olds.items())))
    return min(olds.values()), text


# ── one camera of a playback session ─────────────────────────────────────────────
class Reopen(Exception):
    """This NVR session cannot play time t: t is outside its window, or the NVR refused
    the PLAY (NVR1: a session only covers the recording up to its first gap; PLAY past
    it -> 500 + connection closed). Open a new session at t."""

    def __init__(self, t, refused=False):
        super().__init__(str(t))
        self.t, self.refused = t, refused


class PlaybackWorker:
    """Plays ONE camera's recording for one session tile. Attribute / method names
    match the live workers where the server's slot table and pool read them."""
    quality = "playback"
    original = False
    slot_first = True
    subtype = 0

    def __init__(self, session, tile):
        self.session, self.tile = session, tile
        self.info, self.index, self.name = tile.info, tile.index, tile.name
        self.nvr, self.channel = tile.nvr, tile.channel
        self.label = f"Cam {tile.index + 1} {tile.name} [Playback {session.sid[:6]}]"
        self.slot_key = _new_key()
        self.viewers, self.viewers_full = 1, 0
        self._vlock = threading.Lock()
        self._tlock = threading.Lock()
        self._running, self._gen = False, 0
        self._worker_done = threading.Event()
        self._worker_done.set()
        self._bg, self.bg_reason, self.linger_until = False, None, 0.0
        self.status, self.status_since, self.last_error = "Idle", time.monotonic(), ""
        self.vstate = "IDLE"
        self.transitions = collections.deque(maxlen=40)
        self._cmds = collections.deque()
        self._cmd_ev = threading.Event()
        # viewer-facing state
        self.state, self.msg = "IDLE", ""
        self.gap = None                      # {"prev": ms|None, "next": ms|None}
        self.hold_until_ms = None
        self.rec_ms = None                   # recorded time of the latest decoded frame
        self.seq = session.seq               # timeline generation this worker follows (seek / speed)
        self.frame, self.frame_ms, self.frame_seq = None, None, 0
        self._out_lock = threading.Lock()
        self._out_epoch = None
        self.audio_pk = collections.deque(maxlen=80)     # (seq, codec, rec_ms, payload)
        self.audio_seq = 0
        self.has_audio, self.audio_codec = None, None
        self.frames_decoded = self.frames_sent = self.opens = self.reconnects = 0
        self.last_packet = 0.0
        self._seg = None
        self._epoch = 0
        self._last_out = 0.0
        self._dec = None                     # the current Decoders (diagnostics)
        self._speed_now = 1
        self._held = []
        self.ended = False
        self.t_started = time.monotonic()
        with _REG_LOCK:
            WORKERS[self.slot_key] = self

    # shared interface with the live workers (slot table, pool, _acquire_slot)
    def twin(self):
        return self

    @property
    def pinned(self):
        return False

    def slot_priority(self):
        return PRIO_PLAYBACK if self._running else 0

    def priority_name(self):
        return "PLAYBACK" if self._running else "IDLE"

    def role(self):
        return "playback"

    def is_live(self, now=None):
        now = time.monotonic() if now is None else now
        return self._running and self.last_packet > 0 and now - self.last_packet <= 3.0

    def frame_age_ms(self):
        return round((time.monotonic() - self.last_packet) * 1000) if self.last_packet else None

    def stop_bg(self, reason="POOL_DEMOTION"):
        pass                                  # a playback worker always has its viewer

    def force_stop(self, reason="FORCED"):
        self.stop(reason)

    def _current(self, gen):
        with self._vlock:
            return self._running and gen == self._gen

    def _state(self, gen, status, err=None):
        with self._vlock:
            if gen != self._gen:
                return
            if err is not None:
                self.last_error = str(err)[:160]
            if status == self.status:
                return
            self.status, self.status_since = status, time.monotonic()
        if "slot" in str(status).lower():                 # the shared slot queue: all slots in use
            self._set("WAITING_SLOT", "Waiting for NVR capacity")

    def _trans(self, to, reason, detail=""):
        with self._tlock:
            frm = self.vstate
            self.vstate = to
            self.transitions.append({"t": time.monotonic(), "at": time.strftime("%H:%M:%S"), "from": frm,
                                     "to": to, "reason": reason, "detail": str(detail)[:200],
                                     "viewers": self.viewers, "fullscreen": 0})
        HOOK.ev(f"[{self.label}] {frm} -> {to} reason={reason}" + (f" ({detail})" if detail else ""))

    def transitions_view(self, now, n=12):
        with self._tlock:
            items = list(self.transitions)[-n:]
        return [{"at": e["at"], "agoS": round(now - e["t"], 1), "from": e["from"], "to": e["to"],
                 "reason": e["reason"], "detail": e["detail"]} for e in items]

    def _set(self, state, msg=""):
        if (state, msg) != (self.state, self.msg):
            self.state, self.msg = state, msg
            self.session.notify(state_changed=True)

    # ── control (called from the session, any thread) ──────────────────────────
    def start(self, t, paused=False, speed=1):
        with self._vlock:
            if self._running:
                return
            self._running = True
            self._gen += 1
            gen = self._gen
            prev, self._worker_done = self._worker_done, threading.Event()
            done = self._worker_done
        self.ended = False
        threading.Thread(target=self._run, args=(gen, prev, done, t, paused, speed),
                         name=f"playback {self.label}", daemon=True).start()

    def stop(self, reason="STOPPED"):
        with self._vlock:
            was = self._running
            self._running = False
        self._cmd_ev.set()
        if was:
            HOOK.ev(f"[{self.label}] stop requested: {reason}")

    def command(self, op, **kw):
        self._cmds.append((op, kw))
        self._cmd_ev.set()

    # ── the worker thread ────────────────────────────────────────────────────
    def _run(self, gen, prev_done, done, t, paused, speed):
        slot = False
        sess = dec = None
        why = "STOPPED"
        try:
            prev_done.wait(10.0)
            self._set("OPENING", "Opening playback...")
            self._trans("CONNECTING", "PLAYBACK_START",
                        f"{pt.fmt_local(t) if t is not None else 'timeline position'} speed {speed}x")
            if not HOOK.acquire_slot(self, gen, self.nvr):
                return
            slot = True
            if not self._current(gen):
                return
            pos, speed_now, pause_now = t, speed, paused
            wa = self.session.a                           # start of the NVR session's window
            reconnects = reopens = ends = 0
            while self._current(gen):
                why = "STOPPED"                           # (not the previous loop's REOPEN / GAP_END)
                try:
                    sess, dec = self._open(gen, wa)
                except rpb.NoRecording:
                    why = "NO_RECORDING"
                    self._set("NO_RECORDING", "No recording available")
                    return
                rec0 = self.rec_ms
                result = self._stream(gen, sess, dec, pos, speed_now, pause_now)
                if result is None:                        # stopped / ended
                    return
                why, pos, speed_now, pause_now = result
                sess.close()
                dec.close()
                sess = dec = None
                # a new NVR session starts AT the position: an NVR1 session only covers one
                # recording file (its first gap ends it; files can even overlap there)
                wa = pos
                if why == "REOPEN":                        # a position in another NVR session
                    continue
                if why == "REOPEN_REFUSED":                # the NVR refused the PLAY (NVR1 gap)
                    reopens += 1
                    if reopens > 3:
                        self._set("ERROR", "Playback connection failed")
                        return
                    continue
                reopens = 0
                if why == "GAP_END":                       # the NVR ended the stream at a gap (NVR1)
                    ends = ends + 1 if self.rec_ms == rec0 else 0
                    if ends > 2:                           # nothing new comes: stop trying
                        self.ended = True
                        self._set("ENDED", "Playback ended")
                        return
                    nxt = self._gap_end_wait(gen, pos, speed_now)
                    if nxt is None:
                        return
                    pos, speed_now = nxt
                    pause_now = False
                    wa = pos
                    continue
                if why == "RELEASED":                      # paused too long: NVR session closed
                    if not self._wait_resume(gen):
                        return
                    pause_now = False
                    continue
                reconnects += 1
                self.reconnects += 1
                if reconnects > 2:
                    self._set("ERROR", "Playback connection failed")
                    return
                self._set("OPENING", "Reconnecting playback...")
                time.sleep(1.0)
        except ra.AuthFailed:
            why = "AUTH_FAIL"
            self._set("ERROR", "Playback connection failed")
        except ra.RtspError as e:
            if e.code == "ABORTED" or not self._current(gen):
                why = "STOPPED"                           # stopped while talking to the NVR
            else:
                why = e.code
                if e.code == "TCP_CONNECT_FAILED":
                    self._set("NVR_UNREACHABLE", "NVR unreachable")
                else:
                    self._set("ERROR", "Playback connection failed")
                HOOK.ev(f"[{self.label}] playback error {e.code}: {e.detail}")
        except Exception as e:                               # never crash the server
            why = f"EXCEPTION {type(e).__name__}"
            self._set("ERROR", "Playback connection failed")
            HOOK.ev(f"[{self.label}] playback exception {e!r}")
        finally:
            if sess is not None:
                sess.close()
            if dec is not None:
                dec.close()
            if slot:
                HOOK.release_slot(self.nvr, self.slot_key, why)
            with self._vlock:
                if gen == self._gen:
                    self._running = False
            self._trans("OFF", why.split(" ")[0], why)
            done.set()
            HOOK.pool_wake()
            self.session.notify(state_changed=True)

    def _open(self, gen, wa):
        """New NVR playback session for the window [wa, wa + SESSION_SPAN_H] (at most To).
        A long search range is played as consecutive sessions: an NVR session covers at
        most ~58 h, and NVR1's only up to its first recording gap -- after a gap, a seek
        past one or the end of a session, the next starts at the new position."""
        host, port = HOOK.endpoint(self.nvr)
        n = HOOK.nvrs[self.nvr]
        s = self.session
        wa = min(max(wa, s.a), s.b - datetime.timedelta(seconds=1)).replace(microsecond=0)
        wb = min(s.b, wa + datetime.timedelta(hours=SESSION_SPAN_H))
        sess = rpb.RtspPlayback(host, port, n["user"], n["pass"], self.channel, wa, wb,
                                timeout=8.0, alive=lambda: self._current(gen))
        sess.window_start, sess.window_end = wa, wb
        try:
            sess.open()
        except Exception:
            sess.close()
            raise
        self.opens += 1
        if wa != s.a:
            HOOK.ev(f"[{self.label}] NVR session reopened: {pt.fmt_local(wa)} -> {pt.fmt_local(wb)}")
        self.has_audio = sess.audio is not None
        self.audio_codec = sess.audio["codec"] if sess.audio else None
        dep = rpb.Depacketizer(sess.video["codec"], sess.video.get("fmtp"))
        sess.depack = dep
        dec = Decoders(self._on_frame, dep.params, self.label, self._wants)
        self._dec = dec
        return sess, dec

    def decode_state(self):
        d = self._dec
        return (d.backlog(), d.resyncs) if d is not None and self._running else (0, d.resyncs if d else 0)

    def _next_after_end(self, sess=None):
        """The NVR ended the stream (BYE / connection closed). -> NVR-local time to go on
        from, or None: the To time is reached or nothing more is recorded. Both NVRs end
        at the session window's end (a long range: the next session goes on exactly
        there); NVR1 also at every recording gap (its session stops there)."""
        s = self.session
        if self.rec_ms is None:
            return None
        last = pt.from_ms(self.rec_ms)
        if last >= s.b - datetime.timedelta(seconds=2):
            return None
        we = getattr(sess, "window_end", None)
        if we is not None and we < s.b and last >= we - datetime.timedelta(seconds=5):
            return we                                     # end of this session's window: the next one
        t = last + datetime.timedelta(seconds=1 + KEYFRAME_ADJ_S)    # (labels can be ~1 s early)
        where = self.tile.locate(t)
        if where[0] == "unknown":                         # remote: ask the NVR over RTSP
            try:
                return _probe_next_start(self.nvr, self.channel, t, s.b)
            except OSError:
                return None
        if where[0] == "in":
            return t                                      # recorded here: go on (a short break)
        return where[2][0] if where[2] else None

    def _gap_end_wait(self, gen, nxt, speed):
        """The NVR session ended at a recording gap; the next recording starts at nxt.
        Alone: go on at once (the timeline jumps there). With other cameras playing: wait
        until the shared timeline gets there ('No recording until ...').
        -> (position, speed) to reopen at, or None when stopped."""
        s = self.session
        self._seg = None
        if self.rec_ms is not None and pt.to_ms(nxt) - self.rec_ms <= GAP_MIN_S * 1000:
            self._set("SEEKING", "Seeking...")            # NVR1 file change, not a gap: go on now
            return nxt, speed
        self.gap = {"prev": self.rec_ms, "next": pt.to_ms(nxt)}
        if not s.others_playing(self):
            s.note(self, "Recording gap skipped")
            alone = not any(t.worker is not None and t.worker is not self and t.worker._running for t in s.tiles)
            with s.lock:                                  # the timeline jumps to the next recording
                s.anchor(pt.to_ms(nxt), hold=alone)       # (and waits for it, if no other camera runs)
                if alone:
                    self.seq = s.seq
            self._set("SEEKING", "Seeking...")
            return nxt, speed
        self.hold_until_ms = pt.to_ms(nxt)
        self._set("GAP_WAIT", "No recording until " + nxt.strftime("%H:%M:%S"))
        while self._current(gen):
            self._cmd_ev.wait(0.5)
            self._cmd_ev.clear()
            while self._cmds:
                op, kw = self._cmds.popleft()
                if op in ("gap_resume", "gap_start"):
                    self.hold_until_ms = None
                    return nxt, speed
                if op == "seek":
                    self.hold_until_ms = None
                    self.seq = kw.get("seq", self.seq)
                    return kw["t"], speed
                if op == "speed":
                    speed = kw["x"]
                    self.seq = kw.get("seq", self.seq)
        return None

    def _wait_resume(self, gen):
        """NVR session released while paused: wait for play / seek / speed / stop."""
        self._set("PAUSED", "Paused")
        while self._current(gen):
            self._cmd_ev.wait(0.5)
            self._cmd_ev.clear()
            while self._cmds:
                op, kw = self._cmds[0]
                if op in ("resume", "seek", "speed"):
                    return True                           # handled by the next _stream()
                self._cmds.popleft()
        return False

    def _position(self, gen, sess, t, speed):
        """PAUSE (if needed) + PLAY at NVR-local time t, gap-aware. -> True when playing."""
        tile = self.tile
        s = self.session
        t = min(max(t, s.a), s.b - datetime.timedelta(seconds=1))
        self._set("SEEKING", "Seeking...")
        anchor = t - datetime.timedelta(seconds=KEYFRAME_ADJ_S)
        where = tile.locate(t)
        if where[0] == "unknown":                         # remote: probe the NVR over RTSP
            try:
                nxt = _probe_next_start(self.nvr, self.channel, t, s.b)
            except OSError:
                nxt = t
            where = ("in", None) if nxt == t else ("gap", None, (nxt, None) if nxt else None)
        if where[0] == "gap":
            prev, nxt = where[1], where[2]
            if sess.playing:                              # stop the previous position's stream
                sess.pause()
                sess.playing = False
            self._seg = None                              # its frames still in flight are dropped
            self._held = []
            self.gap = {"prev": pt.to_ms(prev[1]) if prev else None, "next": pt.to_ms(nxt[0]) if nxt else None}
            if nxt is None:
                self._set("GAP", "No more recording in this time range")
                self.hold_until_ms = None
                return False
            self.hold_until_ms = pt.to_ms(nxt[0])
            self._set("GAP", "No recording at this exact time")
            return False                                  # the session starts it when the timeline gets there
        self.gap = None
        w0, w1 = getattr(sess, "window_start", None), getattr(sess, "window_end", None)
        if w0 is not None and not w0 <= t.replace(microsecond=0) < w1:
            raise Reopen(t)                               # outside this NVR session: open the one there
        if where[1] is not None and where[1][0] > s.a:
            anchor = max(anchor, where[1][0])             # never before a recording's real start
        self._epoch += 1                                  # (the From time is only a cut: the NVR
        self._seg = {"epoch": self._epoch, "t0": anchor,  #  starts at the key frame before it)
                     "ts0": None, "last": None, "ats0": None, "speed": speed, "skew": 0.0, "frame_s": 0.04}
        self._held = []
        if sess.playing:
            sess.pause()
            sess.playing = False
        try:
            sess.play(t, speed)
        except ra.RtspError as e:                         # NVR1: past its session's first gap
            if e.code in ("PLAY_FAILED", "STREAM_CLOSED") and getattr(sess, "window_start", None) != \
                    t.replace(microsecond=0):             # (500 + connection closed): new session
                HOOK.ev(f"[{self.label}] PLAY {pt.fmt_local(t)} refused in this NVR session ({e.code}): reopening")
                raise Reopen(t, refused=True)
            raise
        sess.playing = True
        self._set("SEEKING" if self.frame_seq else "OPENING", "Seeking..." if self.frame_seq else "Opening playback...")
        self._trans("SEEK", "PLAY_CLOCK", f"{pt.fmt_local(t)} (clock={pt.rtsp_clock(t)}) speed {speed}x")
        return True

    def _stream(self, gen, sess, dec, pos, speed, paused):
        """Play until stopped / ended / released. -> None (done) or (why, pos, speed, paused).
        Every packet that arrives is processed (also the in-flight ones after a PAUSE:
        they belong to the paused position); waiting only stops counting stalls."""
        self.last_packet = time.monotonic()
        self._seg = None
        if pos is None:                                   # follow the timeline: where it is NOW
            pos = pt.from_ms(self.session.pos_ms())
        try:
            return self._stream_loop(gen, sess, dec, pos, speed, paused)
        except Reopen as r:
            return "REOPEN_REFUSED" if r.refused else "REOPEN", r.t, self._speed_now, False

    def _stream_loop(self, gen, sess, dec, pos, speed, paused):
        self._speed_now = speed
        st = {"waiting_gap": False, "pos": pos, "paused": paused, "speed": speed}
        try:
            return self._stream_body(gen, sess, dec, st)
        except ra.RtspError as e:
            # the NVR closed the connection during a request (PAUSE / PLAY / keep-alive):
            # at the To time or at an NVR1 gap it does exactly that -- like its BYE
            if e.code != "STREAM_CLOSED" or not self._current(gen):
                raise
            if st["paused"]:
                return "RELEASED", pt.from_ms(self.rec_ms) if self.rec_ms else st["pos"], st["speed"], True
            if st["waiting_gap"] or self._seg is None:
                return "STALLED", st["pos"], st["speed"], False
            return self._stream_end(sess, dec, st["speed"])

    def _stream_end(self, sess, dec, speed):
        """The NVR ended the stream (BYE / connection closed)."""
        self._drain(dec)
        nxt = self._next_after_end(sess)
        if nxt is None:                                   # the To time / the last recording
            self.ended = True
            self._set("ENDED", "Playback ended")
            return None
        HOOK.ev(f"[{self.label}] the NVR ended the stream at {pt.fmt_local(pt.from_ms(self.rec_ms))}: "
                f"recording goes on at {pt.fmt_local(nxt)}")
        return "GAP_END", nxt, speed, False

    def _stream_body(self, gen, sess, dec, st):
        waiting_gap, pos, paused, speed = st["waiting_gap"], st["pos"], st["paused"], st["speed"]
        if paused:
            self._set("PAUSED", "Paused")
        else:
            waiting_gap = not self._position(gen, sess, pos, speed)
        t_ka = t_pause = time.monotonic()
        cur = lambda: pt.from_ms(self.rec_ms) if self.rec_ms else pos     # noqa: E731
        while self._current(gen):
            st.update(waiting_gap=waiting_gap, pos=pos, paused=paused, speed=speed)
            while self._cmds:
                op, kw = self._cmds.popleft()
                if op == "pause":
                    if not paused:
                        paused, t_pause = True, time.monotonic()
                        if sess.playing:
                            sess.pause()
                            sess.playing = False
                        self._set("PAUSED", "Paused")
                elif op == "resume":
                    if paused:
                        paused = False
                        self.last_packet = time.monotonic()
                        if waiting_gap or self.state == "GAP_WAIT":
                            self._set("GAP" if waiting_gap else "GAP_WAIT", self.msg)
                        elif self._seg is None:
                            waiting_gap = not self._position(gen, sess, pos, speed)
                        else:
                            sess.play(None, self._seg["speed"])
                            sess.playing = True
                            self._set("PLAYING", "")
                elif op in ("seek", "speed", "gap_start"):
                    if op == "speed":
                        speed, pos = kw["x"], cur()
                        self._speed_now = speed
                    else:
                        pos = kw["t"]
                    self._held, self.hold_until_ms = [], None
                    if paused:
                        self._seg = None                  # positioned when playback resumes
                        waiting_gap = False
                        self._set("PAUSED", "Paused")
                        self.seq = kw.get("seq", self.seq)
                        continue
                    waiting_gap = not self._position(gen, sess, pos, speed)
                    self.seq = kw.get("seq", self.seq)   # after SEEKING / GAP is set: the old
                    self.last_packet = time.monotonic()  # position never re-anchors the timeline
                elif op == "gap_resume" and self.state == "GAP_WAIT":
                    held, self._held = self._held, []
                    self.hold_until_ms = None
                    for au, meta in held:
                        dec.push(au, meta)
                    sess.play(None, speed)
                    sess.playing = True
                    self.last_packet = time.monotonic()
                    self._set("PLAYING", "")
            st.update(waiting_gap=waiting_gap, pos=pos, paused=paused, speed=speed)
            now = time.monotonic()
            if now - t_ka >= 20.0:
                sess.keepalive()
                t_ka = now
            if paused and now - t_pause >= PAUSE_HOLD_S:
                return "RELEASED", cur(), speed, True
            active = not paused and not waiting_gap and self.state != "GAP_WAIT"
            item = sess.next_packet(0.2 if active else 0.1)   # (connection closed -> _stream_loop)
            if item is None:
                if active and time.monotonic() - self.last_packet > STALL_S:
                    return "STALLED", cur(), speed, False
                continue
            self.last_packet = time.monotonic()
            if item[0] == "v":
                for au, ts, key in sess.depack.push(item[1]):
                    self._video_au(dec, sess, au, ts, key)
            elif item[0] == "a":
                self._audio(item[1])
            elif item[0] == "bye":
                return self._stream_end(sess, dec, speed)
        return None

    def _video_au(self, dec, sess, au, ts, key):
        seg = self._seg
        if seg is None:
            return
        if seg["ts0"] is None:
            seg["ts0"] = seg["last"] = ts
        raw = (ts - seg["last"]) & 0xFFFFFFFF
        step = (raw - (1 << 32) if raw >= 1 << 31 else raw) / 90000.0   # signed: both NVRs step
        seg["last"] = ts                                                 # BACK ~1 s just before the To time
        if seg["speed"] == 1:
            if 0 < step <= 0.2:
                seg["frame_s"] = step                     # the normal frame interval
            elif 0.2 < step <= GAP_MIN_S:
                # the NVR's RTP clock jumped but the footage did not: seen at its hourly file
                # change when playback started in the old file's last GOP (+2.04 s while the
                # camera clock ran on). Shorter than a recording gap -> one frame interval.
                seg["skew"] += step - seg["frame_s"]
        d = (ts - seg["ts0"]) & 0xFFFFFFFF
        d = d - (1 << 32) if d >= 1 << 31 else d
        rec = seg["t0"] + datetime.timedelta(seconds=d / 90000.0 - seg["skew"])
        rec_ms = pt.to_ms(rec)
        jumped = step > (GAP_MIN_S if seg["speed"] == 1 else 5.0)
        if jumped:
            HOOK.ev(f"[{self.label}] RTP clock jumped {step:.1f} s -> {pt.fmt_local(rec)}")
            if rec_ms >= pt.to_ms(self.session.b) - 500:
                return                                    # past the To time: the NVR's end follows
        if jumped and seg["speed"] == 1 and self.session.others_playing(self):
            # crossed a recording gap while other cameras play: wait for the timeline
            if self.state != "GAP_WAIT":
                sess.pause()
                sess.playing = False
                self.hold_until_ms = rec_ms
                self.gap = {"prev": None, "next": rec_ms}
                self._set("GAP_WAIT", "No recording until " + pt.from_ms(rec_ms).strftime("%H:%M:%S"))
            self._held.append((au, (seg["epoch"], rec_ms, key)))
            return
        if self._held:                                    # in-flight frames after a hold
            self._held.append((au, (seg["epoch"], rec_ms, key)))
            return
        if jumped:
            self.session.note(self, "Recording gap skipped")
        dec.push(au, (seg["epoch"], rec_ms, key), fast=seg["speed"] != 1)

    def _audio(self, pkt):
        seg = self._seg
        if seg is None or seg["speed"] != 1 or not self.has_audio:
            return
        r = ra._rtp_payload(pkt)
        if r is None:
            return
        payload, _seq, ts = r
        if seg["ats0"] is None:
            seg["ats0"] = ts
        d = (ts - seg["ats0"]) & 0xFFFFFFFF
        d = d - (1 << 32) if d >= 1 << 31 else d
        rec_ms = pt.to_ms(seg["t0"]) + int(d / 8)
        self.audio_seq += 1
        self.audio_pk.append((self.audio_seq, 0 if self.audio_codec == "PCMU" else 8, rec_ms, payload))
        self.session.notify()

    def _drain(self, dec, timeout=3.0):
        """End of the recording: output what the decoders still hold, the very last
        picture included (every picture left is converted, see CvDecoder.finish)."""
        t0 = time.monotonic()
        dec.finish(timeout)
        HOOK.ev(f"[{self.label}] end of the recording: decoder drained in {time.monotonic() - t0:.2f} s, "
                f"last picture {pt.fmt_local(pt.from_ms(self.frame_ms)) if self.frame_ms else '-'}")

    def _wants(self, meta):
        """Decoder thread, before a picture is converted: will it be shown? (the output
        frame rate: grid 10/s, one camera 15/s; 2x / 4x: every key frame)"""
        seg = self._seg
        if meta is None or seg is None or meta[0] != seg["epoch"]:
            return False
        width, fps = self.session.output_for(self.tile)
        if width is None:
            return False                                  # not shown right now (another tile is fullscreen)
        if seg["speed"] != 1:
            return True
        now = time.monotonic()
        if now - self._last_out < 1.0 / fps - 0.005:
            return False
        self._last_out = now
        return True

    def _on_frame(self, frame, meta):
        """Every decoded picture (frame None: decoded but not converted, not shown)."""
        if meta is None:
            return
        epoch, rec_ms, _key = meta
        if self._seg is None or epoch != self._seg["epoch"]:
            return                                        # decoded after a seek: stale
        self.rec_ms = rec_ms
        self.frames_decoded += 1
        if self.state in ("OPENING", "SEEKING"):
            self._set("PLAYING", "")
            self._trans("PLAYING", "FIRST_FRAME", pt.fmt_local(pt.from_ms(rec_ms)))
        if frame is None:
            return
        width, _fps = self.session.output_for(self.tile)
        if width is not None:
            self._emit(frame, rec_ms, width, epoch)

    def _emit(self, frame, rec_ms, width, epoch):
        """Publish one picture. Called from the decoder threads and (end of recording)
        the worker: within one position a picture never replaces a newer one."""
        jpg = encode_jpeg(frame, width)
        if jpg is None:
            return
        with self._out_lock:
            if self._seg is None or epoch != self._seg["epoch"]:
                return
            if self._out_epoch == epoch and self.frame_ms is not None and rec_ms <= self.frame_ms:
                return
            self._out_epoch = epoch
            self.frame, self.frame_ms = jpg, rec_ms
            self.frame_seq += 1
            self.frames_sent += 1
        self.session.notify()

    def picture(self):
        """(jpeg, recorded ms, sequence) of the latest published picture, consistent."""
        with self._out_lock:
            return self.frame, self.frame_ms, self.frame_seq

    def view(self):
        return {"state": self.state, "msg": self.msg, "recMs": self.rec_ms, "gap": self.gap,
                "audio": self.audio_codec if self.has_audio else None,
                "opens": self.opens, "reconnects": self.reconnects}


# ── sessions ─────────────────────────────────────────────────────────────────────
class Tile:
    def __init__(self, tid, index, info, name):
        self.id, self.index, self.info, self.name = tid, index, info, name
        self.nvr, self.channel = info["nvr"], info["channel"]
        self.key = f"{self.nvr}:{self.channel}"      # the camera's stable key (permissions)
        self.avail = None                    # search result (None: still searching)
        self.avail_ver = 0                   # session.avail_ver when it arrived
        self.worker = None
        self.state, self.msg = "SEARCHING", "Searching recordings..."
        self.denied = False                  # the viewer lost recorded playback for this camera
        self.audio_allowed = True            # ... or its sound (per-person CCTV permissions)

    def locate(self, t):
        segs = (self.avail or {}).get("segments")
        if segs is None:
            return ("unknown",)
        return nvr_api.locate(segs, t)

    def view(self, full=False):
        """full: with the recording segments (search reply / "avail" message); the
        periodic state leaves them out (a month of one camera can be ~100 segments)."""
        a = self.avail or {}
        segs = a.get("segments")
        v = {"id": self.id, "index": self.index, "name": self.name, "nvr": self.nvr.upper(),
             "channel": self.channel, "availability": a.get("status") if self.avail else "searching",
             "recordedS": a.get("recordedS"), "method": a.get("method")}
        if full:
            v["segments"] = [[pt.to_ms(s), pt.to_ms(e)] for s, e in segs] if segs is not None else None
        if self.worker is not None and not self.denied and (self.worker._running or self.worker.state in
                                                            ("ENDED", "ERROR", "NO_RECORDING", "NVR_UNREACHABLE", "GAP")):
            v.update(self.worker.view())
        else:
            v.update({"state": self.state, "msg": self.msg, "recMs": None, "gap": None, "audio": None})
        v["audioAllowed"] = self.audio_allowed and not self.denied
        if not v["audioAllowed"]:
            v["audio"] = None                # no sound offered for this camera
        return v


class Session:
    def __init__(self, sid, a, b, tiles, client, access=None):
        self.sid, self.a, self.b, self.tiles, self.client = sid, a, b, tiles, client
        # Who started it (cctv_access.Principal: .ident, .can(key, feature), .recheck());
        # None = no per-person permissions (tests / direct use). Only the same person may
        # attach to it, and their permissions are re-checked while it runs.
        self.access = access
        self.t_access = time.monotonic()
        self.lock = threading.RLock()
        self.cv = threading.Condition()
        self.paused, self.speed = False, 1
        self.audio_tile = -1
        self.focus = -1
        self.visible = [t.id for t in tiles[:TILES_PER_PAGE]]
        self._pos_ms, self._t_anchor = pt.to_ms(a), time.monotonic()
        self.hold = True            # the clock stands still until a camera SHOWS the position
        self.seq = 0                # timeline generation: +1 on every seek / speed change
        self.ws = None
        self.detached_at = time.monotonic()
        self.created = time.monotonic()
        self.closed = False
        self.version = 0
        self.notes = collections.deque(maxlen=5)
        self.search_left = len(tiles)       # cameras whose recording search is still running
        self.avail_ver = 0                  # +1 per finished camera search ("avail" messages)
        self.t_search = time.monotonic()

    # timeline (virtual clock, re-anchored on the cameras that really play; it holds while
    # the cameras open / seek, so the search time or a slow NVR never moves the start)
    def pos_ms(self):
        with self.lock:
            if self.paused or self.hold:
                return self._pos_ms
            return int(min(self._pos_ms + (time.monotonic() - self._t_anchor) * 1000 * self.speed,
                           pt.to_ms(self.b)))

    def anchor(self, ms, hold=False):
        with self.lock:
            self._pos_ms = int(min(max(ms, pt.to_ms(self.a)), pt.to_ms(self.b)))
            self._t_anchor = time.monotonic()
            if hold:                                     # a new position: wait for its pictures
                self.seq += 1
                self.hold = True

    def notify(self, state_changed=False):
        with self.cv:
            if state_changed:
                self.version += 1
            self.cv.notify_all()

    def note(self, worker, text):
        now = time.time()
        if any(n["tile"] == worker.tile.id and n["text"] == text and now - n["t"] < 10 for n in self.notes):
            return                                        # (NVR1 can end its stream twice at one gap)
        self.notes.append({"tile": worker.tile.id, "text": text, "t": now})
        self.notify(state_changed=True)

    def others_playing(self, worker):
        return any(t.worker is not None and t.worker is not worker and t.worker.state == "PLAYING"
                   for t in self.tiles if t.id in self.visible)

    def output_for(self, tile):
        if self.focus >= 0:
            return OUT_SINGLE if tile.id == self.focus else (None, None)
        n = len([t for t in self.tiles if t.id in self.visible])
        return OUT_SINGLE if n <= 1 else OUT_GRID

    def view(self):
        return {"t": "state", "sid": self.sid, "paused": self.paused, "speed": self.speed,
                "posMs": self.pos_ms(), "fromMs": pt.to_ms(self.a), "toMs": pt.to_ms(self.b),
                "audioTile": self.audio_tile, "focus": self.focus, "visible": self.visible,
                "tiles": [t.view() for t in self.tiles], "notes": list(self.notes),
                "searching": self.search_left,
                "capacity": {"running": len(running_workers()), "max": MAX_WORKERS}}


class PlaybackManager:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.RLock()
        self._hk = None
        self._audit_lock = threading.Lock()

    # ── search ───────────────────────────────────────────────────────────────
    def search(self, body, client=None, user=None, access=None):
        """-> (http status, response dict). access: the viewer (cctv_access.Principal) --
        every camera asked for must allow THEM recorded playback, or nothing is searched."""
        cams = HOOK.cameras
        try:
            a = pt.parse_local(body.get("from"))
            b = pt.parse_local(body.get("to"))
        except (ValueError, AttributeError):
            return 400, {"ok": False, "error": "Enter a valid From and To date and time."}
        err = pt.validate_range(a, b)
        if err:
            return 400, {"ok": False, "error": err}
        now = pt.nvr_now()
        b = min(b, now)
        idx = body.get("cameras")
        if not isinstance(idx, list) or not idx:
            return 400, {"ok": False, "error": "Select at least one camera."}
        seen, sel = set(), []
        for i in idx:
            if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(cams):
                return 400, {"ok": False, "error": "Unknown camera in the selection."}
            if i not in seen:
                seen.add(i)
                sel.append(i)
        if access is not None:
            refused = [i for i in sel if not access.can(f"{cams[i]['nvr']}:{cams[i]['channel']}", "playback")]
            if refused:
                return 403, {"ok": False, "code": "PLAYBACK_NOT_PERMITTED", "cameras": refused,
                             "error": "You do not have recorded playback for: "
                                      + ", ".join(HOOK.display_name(i) for i in refused) + "."}
        # how far back: each selected NVR's oldest recording (read from the NVRs); the
        # fixed MAX_RANGE_H only while an NVR's oldest recording is not known
        first, kept = retention_limit({cams[i]["nvr"] for i in sel})
        note, asked = None, a
        if first is None:
            if (b - a).total_seconds() > MAX_RANGE_H * 3600:
                return 400, {"ok": False, "error": f"The time range is too long: at most {pt.span_text(MAX_RANGE_H)} "
                                                   f"per search (the NVRs' oldest recordings are not known yet)."}
        elif b <= first:
            return 400, {"ok": False, "error": f"No recordings that old: {kept}.", "retention": retention_view()}
        elif a < first:
            note = f"Nothing is recorded before {first:%d %b %Y %H:%M:%S}, so the search starts there. {kept}."
            a = first
        prev = body.get("replaces")
        if isinstance(prev, str):
            self.close(prev, "NEW_SEARCH")
        tiles = [Tile(k, i, cams[i], HOOK.display_name(i)) for k, i in enumerate(sel)]
        if access is not None:
            for t in tiles:
                t.audio_allowed = access.can(t.key, "audio")
        sid = secrets.token_urlsafe(12)
        s = Session(sid, a, b, tiles, client, access=access)
        with self.lock:
            self.sessions[sid] = s
        self._ensure_hk()
        self.audit("search", s, user, {"hours": round((b - a).total_seconds() / 3600, 2),
                                       **({"askedFrom": pt.fmt_local(asked)} if note else {})})
        # recording METADATA only (no video): one queue per NVR, one camera at a time
        # (never many parallel queries on an NVR), the cameras on the page first. The
        # reply waits a moment; slower results reach the page over its WebSocket.
        by_nvr = collections.OrderedDict()
        for t in tiles:
            by_nvr.setdefault(t.nvr, []).append(t)
        for nvr, lst in by_nvr.items():
            threading.Thread(target=self._search_nvr, args=(s, lst, user), name=f"pbsearch {nvr}",
                             daemon=True).start()
        try:
            wait = max(0.0, min(600.0, float(body.get("wait", SEARCH_WAIT_S))))
        except (TypeError, ValueError):
            wait = SEARCH_WAIT_S
        with s.cv:
            s.cv.wait_for(lambda: s.search_left <= 0 or s.closed, timeout=wait)
        with s.lock:
            views = [t.view(full=True) for t in tiles]
            left = s.search_left
        return 200, {"ok": True, "sid": sid, "from": pt.fmt_local(a), "to": pt.fmt_local(b),
                     "fromMs": pt.to_ms(a), "toMs": pt.to_ms(b), "tiles": views,
                     "found": sum(1 for v in views if v["availability"] in ("found", "partial")),
                     "searching": left, "tilesPerPage": TILES_PER_PAGE,
                     "note": note, "askedFromMs": pt.to_ms(asked) if note else None, "retention": retention_view(),
                     "limits": {"server": MAX_WORKERS, "perNvr": {k: per_nvr_max(k) for k in HOOK.nvrs}}}

    def _search_nvr(self, s, tiles, user):
        """The recording searches of one NVR for one session, one camera after another."""
        pending = list(tiles)
        while pending and not s.closed:
            pending.sort(key=lambda t: (t.id not in s.visible, t.id))    # the page looked at first
            tile = pending.pop(0)
            self._search_tile(tile, s.a, s.b)
            with s.lock:
                s.search_left -= 1
                s.avail_ver += 1
                tile.avail_ver = s.avail_ver
                done = s.search_left <= 0
            s.notify(state_changed=True)
            if s.ws is not None:
                self._sync_workers(s)                     # its recording found: it can play
            if done:
                found = sum(1 for t in s.tiles if (t.avail or {}).get("status") in ("found", "partial"))
                secs = time.monotonic() - s.t_search
                self.audit("search_done", s, user, {"found": found, "seconds": round(secs, 1)})
                HOOK.ev(f"[PLAYBACK] session {s.sid[:6]} search {pt.fmt_local(s.a)} -> {pt.fmt_local(s.b)}, "
                        f"{len(s.tiles)} camera(s), recordings for {found} ({secs:.1f} s)")

    def _search_tile(self, tile, a, b):
        try:
            tile.avail = search_camera(tile.nvr, tile.channel, a, b)
        except Exception as e:
            tile.avail = {"status": "error", "recordedS": None, "segments": None, "method": None}
            HOOK.ev(f"[PLAYBACK] search error {tile.name}: {e!r}")
        st = tile.avail["status"]
        tile.state, tile.msg = {
            "found": ("READY", "Recording found"), "partial": ("READY", "Recording found (with gaps)"),
            "none": ("NO_RECORDING", "No recording found for this camera and time range."),
            "unreachable": ("NVR_UNREACHABLE", "NVR unreachable"),
            "auth": ("ERROR", "Playback unavailable"), "error": ("ERROR", "Playback unavailable"),
        }.get(st, ("ERROR", "Playback unavailable"))

    # ── sessions ─────────────────────────────────────────────────────────────
    def get(self, sid):
        with self.lock:
            return self.sessions.get(sid)

    def close(self, sid, reason="CLOSED"):
        with self.lock:
            s = self.sessions.pop(sid, None)
        if s is None:
            return False
        s.closed = True
        for t in s.tiles:
            if t.worker is not None:
                t.worker.stop(reason)
        if s.ws is not None:
            try:
                s.ws.close()
            except OSError:
                pass
        s.notify(state_changed=True)
        self.audit("session_end", s, None, {"reason": reason})
        HOOK.ev(f"[PLAYBACK] session {sid[:6]} closed ({reason})")
        return True

    def close_all(self, reason="SHUTDOWN"):
        for sid in list(self.sessions):
            self.close(sid, reason)

    def _can_start(self, nvr):
        return len(running_workers()) < MAX_WORKERS and len(running_workers(nvr)) < per_nvr_max(nvr)

    def _start_tile(self, s, tile, t=None):
        if tile.denied or (tile.avail or {}).get("status") not in ("found", "partial"):
            return
        w = tile.worker
        if w is not None and w._running:
            return
        if not self._can_start(tile.nvr):
            per = len(running_workers(tile.nvr)) >= per_nvr_max(tile.nvr)
            tile.state = "CAPACITY"
            tile.msg = (f"{tile.nvr.upper()} playback limit reached ({per_nvr_max(tile.nvr)}). Stop another "
                        f"playback and try again." if per else
                        "Playback capacity reached. Stop another playback stream and try again.")
            if w is not None:
                w.state, w.msg = tile.state, tile.msg
            tile.worker = None
            s.notify(state_changed=True)
            return
        w = PlaybackWorker(s, tile)
        tile.worker = w
        w.start(t, paused=s.paused, speed=s.speed)        # t None: the timeline when it is ready

    def _sync_workers(self, s):
        """Visible tiles with recordings run (capacity permitting); others stop."""
        with s.lock:
            if s.closed:
                return
            for tile in s.tiles:
                if tile.id not in s.visible and tile.worker is not None and tile.worker._running:
                    tile.worker.stop("NOT_VISIBLE")
                    tile.state, tile.msg = "READY", "Recording found"
            for tile in s.tiles:
                if tile.id in s.visible and tile.state not in ("STOPPED",):
                    self._start_tile(s, tile)
        s.notify(state_changed=True)

    # ── per-person permissions, re-checked while the session runs ────────────
    ACCESS_RECHECK_S = 2.0

    def owns(self, s, access):
        """May this viewer attach to / close this session? Only the person who started it."""
        return s.access is None or (access is not None and access.ident == s.access.ident)

    def recheck_access(self, s, force=False):
        """The owner's permissions now (cctv_access re-asks the CMS when stale or told
        of a change): a camera whose playback was taken away stops, a camera whose sound
        was taken away goes silent, and a viewer who lost CCTV altogether loses the
        session. -> False when the session was closed."""
        if s.access is None or s.closed:
            return not s.closed
        now = time.monotonic()
        if not force and now - s.t_access < self.ACCESS_RECHECK_S:
            return True
        s.t_access = now
        try:
            acc = s.access.recheck()
        except Exception as e:                            # never let a check crash the session
            HOOK.ev(f"[PLAYBACK] session {s.sid[:6]} permission re-check failed: {e!r}")
            return True
        s.access = acc
        if not acc.allowed:
            if s.ws is not None:
                try:
                    s.ws.send_json({"t": "error", "error": "denied",
                                    "msg": acc.message or "Your CCTV access was removed."})
                except OSError:
                    pass
            self.close(s.sid, "PERMISSION_REVOKED")
            return False
        changed = False
        with s.lock:
            for t in s.tiles:
                play, hear = acc.can(t.key, "playback"), acc.can(t.key, "audio")
                if not play and not t.denied:
                    t.denied, changed = True, True
                    if t.worker is not None:
                        t.worker.stop("PERMISSION_REVOKED")
                    t.worker = None
                    t.state, t.msg = "DENIED", "Your recorded playback permission for this camera was removed."
                    HOOK.ev(f"[PLAYBACK] session {s.sid[:6]} {t.name}: playback permission removed")
                if hear != t.audio_allowed:
                    t.audio_allowed, changed = hear, True
                if s.audio_tile == t.id and (t.denied or not t.audio_allowed):
                    s.audio_tile, changed = -1, True
        if changed:
            s.notify(state_changed=True)
        return True

    # ── WebSocket session ────────────────────────────────────────────────────
    def run_ws(self, s, ws):
        """Blocking: the page's WebSocket (handler thread). Sends frames / audio / state."""
        old = s.ws
        s.ws, s.detached_at = ws, None
        if old is not None and old is not ws:
            old.close()
        ws.on_text = lambda text: self.command(s, text)
        self._sync_workers(s)
        last_v, last_a, sent_ver, t_state, t_ping = {}, {}, -1, 0.0, time.monotonic()
        sent_avail = 0
        try:
            while not ws.closed and s.ws is ws and not s.closed:
                with s.cv:
                    s.cv.wait(timeout=0.2)
                if not self.recheck_access(s):
                    break
                now = time.monotonic()
                if s.avail_ver != sent_avail:             # recording searches finished since
                    with s.lock:
                        news = [t.view(full=True) for t in s.tiles if t.avail_ver > sent_avail]
                        sent_avail, left = s.avail_ver, s.search_left
                    ws.send_json({"t": "avail", "tiles": news, "searching": left,
                                  "found": sum(1 for t in s.tiles if (t.avail or {}).get("status") in ("found", "partial"))})
                if s.version != sent_ver or now - t_state >= 1.0:
                    sent_ver, t_state = s.version, now
                    ws.send_json(s.view())
                for tile in s.tiles:
                    w = tile.worker
                    if tile.id not in s.visible or w is None or not w.frame_seq:
                        continue
                    if s.focus >= 0 and tile.id != s.focus:
                        continue
                    if last_v.get(tile.id) != w.frame_seq:
                        jpg, ms, seq = w.picture()
                        last_v[tile.id] = seq
                        if jpg:
                            ws.send_binary(b"V" + bytes([tile.id]) + int(ms).to_bytes(8, "big")
                                           + (seq & 0xFFFFFFFF).to_bytes(4, "big") + jpg)
                at = s.audio_tile
                if at >= 0 and not s.paused and at < len(s.tiles) and s.tiles[at].audio_allowed and not s.tiles[at].denied:
                    w = s.tiles[at].worker
                    if w is not None:
                        key = (at, id(w))
                        if key not in last_a:              # new listener target: only NEW audio
                            last_a[key] = w.audio_seq
                        for seq, codec, ms, payload in list(w.audio_pk):
                            if seq > last_a[key]:
                                ws.send_binary(b"A" + bytes([at, codec]) + int(ms).to_bytes(8, "big")
                                               + (seq & 0xFFFFFFFF).to_bytes(4, "big") + payload)
                                last_a[key] = seq
                if now - t_ping >= 15.0:
                    ws.ping()
                    t_ping = now
        except OSError:
            pass
        finally:
            if s.ws is ws:
                s.ws, s.detached_at = None, time.monotonic()

    def command(self, s, text):
        try:
            c = json.loads(text)
            op = c.get("op")
        except (ValueError, AttributeError):
            return
        with s.lock:
            if op == "pause" and not s.paused:
                s.anchor(s.pos_ms())
                s.paused = True
                for t in s.tiles:
                    if t.worker is not None:
                        t.worker.command("pause")
            elif op == "play" and s.paused:
                s.anchor(s._pos_ms)
                s.paused = False
                for t in s.tiles:
                    if t.worker is not None and t.worker._running:
                        t.worker.command("resume")
                self._sync_workers(s)
            elif op == "seek" and isinstance(c.get("ms"), (int, float)):
                s.anchor(c["ms"], hold=True)
                t0 = pt.from_ms(s._pos_ms)
                for t in s.tiles:
                    if t.id not in s.visible:
                        continue
                    if t.worker is not None and t.worker._running:
                        t.worker.command("seek", t=t0, seq=s.seq)
                    else:
                        self._start_tile(s, t)
            elif op == "speed" and c.get("x") in SPEEDS and int(c["x"]) != s.speed:
                s.anchor(s.pos_ms(), hold=True)
                s.speed = int(c["x"])
                for t in s.tiles:
                    if t.worker is not None and t.worker._running:
                        t.worker.command("speed", x=s.speed, seq=s.seq)
            elif op == "audio" and isinstance(c.get("tile"), int):
                t = c["tile"]
                # sound only for a camera whose sound THIS viewer may hear
                s.audio_tile = t if 0 <= t < len(s.tiles) and s.tiles[t].audio_allowed and not s.tiles[t].denied else -1
            elif op == "focus" and isinstance(c.get("tile"), int):
                s.focus = c["tile"] if 0 <= c["tile"] < len(s.tiles) else -1
            elif op == "page" and isinstance(c.get("tiles"), list):
                ids = [i for i in c["tiles"] if isinstance(i, int) and 0 <= i < len(s.tiles)][:TILES_PER_PAGE]
                s.visible = ids
                self._sync_workers(s)
            elif op in ("retry", "stop") and isinstance(c.get("tile"), int) and 0 <= c["tile"] < len(s.tiles):
                t = s.tiles[c["tile"]]
                if op == "stop" and t.worker is not None:
                    t.worker.stop("USER_STOP")
                    t.state, t.msg = "STOPPED", "Stopped"
                elif op == "retry":
                    self._start_tile(s, t)
        s.notify(state_changed=True)

    # ── housekeeping: timeline anchor, gap waits, grace / expiry ─────────────
    def _ensure_hk(self):
        with self.lock:
            if self._hk is None:
                self._hk = threading.Thread(target=self._housekeeping, name="playback-hk", daemon=True)
                self._hk.start()

    def _housekeeping(self):
        while True:
            time.sleep(0.25)
            now = time.monotonic()
            for sid, s in list(self.sessions.items()):
                try:
                    if s.ws is None and s.detached_at and now - s.detached_at > GRACE_S:
                        self.close(sid, "PAGE_GONE")
                        continue
                    if now - s.created > SESSION_MAX_S:
                        self.close(sid, "SESSION_EXPIRED")
                        continue
                    self._tick(s)
                except Exception as e:
                    HOOK.ev(f"[PLAYBACK] housekeeping error {e!r}")
            with _REG_LOCK:                          # forget finished workers after a minute
                for k, w in list(WORKERS.items()):
                    if not w._running and w._worker_done.is_set() and now - w.t_started > 60 \
                            and (w.tile.worker is not w or w.session.closed):
                        WORKERS.pop(k, None)

    def _tick(self, s):
        if s.paused:
            return
        cur = [t.worker for t in s.tiles if t.id in s.visible and t.worker is not None and t.worker._running
               and t.worker.seq == s.seq]                # workers already at the current position
        playing = sorted(w.rec_ms for w in cur if w.state == "PLAYING" and w.rec_ms)
        with s.lock:
            if playing:
                s.anchor(playing[len(playing) // 2])
                s.hold = False
            elif s.hold and cur and all(w.state in ("GAP", "GAP_WAIT") for w in cur):
                s.anchor(s._pos_ms)                      # only recording gaps here: time moves on
                s.hold = False                           # (a camera starts when its recording does)
        pos = s.pos_ms()
        for t in s.tiles:
            w = t.worker
            if w is None or not w._running or w.hold_until_ms is None:
                continue
            if pos >= w.hold_until_ms - 300:
                if w.state == "GAP_WAIT":
                    w.command("gap_resume")
                elif w.state == "GAP":
                    w.command("gap_start", t=pt.from_ms(w.hold_until_ms))
                    w.hold_until_ms = None

    # ── audit log (historical footage is sensitive) ──────────────────────────
    def audit(self, event, s, user, extra=None):
        path = HOOK.audit_path
        if not path:
            return
        rec = {"at": pt.fmt_local(pt.nvr_now()), "tz": pt.NVR_TZ_LABEL, "event": event, "session": s.sid[:8],
               "client": s.client, "user": user, "from": pt.fmt_local(s.a), "to": pt.fmt_local(s.b),
               "cameras": [f"{t.name} ({t.nvr.upper()} CH{t.channel})" for t in s.tiles]}
        rec.update(extra or {})
        try:
            with self._audit_lock:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
        except OSError as e:
            HOOK.ev(f"[PLAYBACK] audit log write failed: {e}")

    # ── status / diagnostics ─────────────────────────────────────────────────
    def status(self):
        ws = running_workers()
        return {"sessions": len(self.sessions), "running": len(ws), "maxWorkers": MAX_WORKERS,
                "perNvr": {k: {"running": len(running_workers(k)), "max": per_nvr_max(k)} for k in HOOK.nvrs},
                "workers": [{"camera": w.name, "nvr": w.nvr, "session": w.session.sid[:6], "state": w.state,
                             "recAt": pt.fmt_local(pt.from_ms(w.rec_ms)) if w.rec_ms else None,
                             "framesDecoded": w.frames_decoded, "framesSent": w.frames_sent,
                             "opens": w.opens, "reconnects": w.reconnects,
                             "decodeBacklog": w.decode_state()[0], "decodeSkips": w.decode_state()[1],
                             "slotKey": w.slot_key} for w in ws]}


MANAGER = PlaybackManager()

_NVR_INFO = {}


def nvr_info(force=False):
    """Per NVR: playback method available here, model / firmware, clock drift, the
    account's group (security warning). Vendor API calls are LAN-only; cached 5 min."""
    out = {}
    now = time.monotonic()
    for nvr in HOOK.nvrs:
        c = _NVR_INFO.get(nvr)
        if c and not force and now - c[0] < 300:
            out[nvr] = c[1]
            continue
        api = vendor_api(nvr)
        info = {"remote": bool(HOOK.remote()), "rtspPlayback": True, "vendorSearch": api is not None,
                "model": None, "firmware": None, "clockDriftS": None, "accountGroup": None,
                "playbackMax": per_nvr_max(nvr)}
        if api is not None:
            try:
                info.update({k: v for k, v in api.device().items() if k in ("model", "firmware")})
                info["clockDriftS"] = nvr_api.clock_drift_s(api)
                info["accountGroup"] = api.account_group()
            except OSError:
                pass
        info["adminAccount"] = (info["accountGroup"] or "").lower() == "admin"
        _NVR_INFO[nvr] = (now, info)
        out[nvr] = info
    return out
