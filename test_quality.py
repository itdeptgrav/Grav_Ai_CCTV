"""Video quality mode tests (Standard / Original) -- offline: cv2 and the RTSP
pre-flight are stubbed, nothing connects to an NVR.

The fake capture serves a different frame size per stream: sub-stream (subtype=1)
352x288, main stream (subtype=0) 1920x1080. The fake JPEG encoder returns
b"jpeg-<w>x<h>-q<quality>", so every MJPEG part a viewer receives tells exactly which
picture (size) and which JPEG quality it came from.

Covers: Standard default + sub-stream, Original = main stream at source size (no
resize, higher JPEG quality), separate worker identity + slot keys, one shared
Original upstream for several viewers, toggle cleanup (no worker/slot leak), Original
never kept in the background, NVR cap + slot priority, the Standard stand-in while
Original starts, fallback when the main stream fails and recovery, status API, UI.

Run:  python test_quality.py        (exits non-zero on any failure)
"""
import os
import re
import sys
import json
import time
import types
import socket
import tempfile
import threading

os.environ.update({
    "CCTV_PERSISTENT": "1", "CCTV_PREFLIGHT": "1", "CCTV_NVR_MAX_CONN": "6",
    "CCTV_LOG_EVENTS": os.environ.get("QUALITY_TEST_LOG", "0"),
    "CCTV_SETTINGS_FILE": os.path.join(tempfile.mkdtemp(), "camera-settings.json"),
    "CCTV_WARM_STEP_S": "0.05", "CCTV_MIN_BG_HOT_S": "0.3", "CCTV_BG_SWAP_S": "0.2",
    "CCTV_DEAD_RETRY_S": "2", "CCTV_REFRESH_EVERY_S": "0", "CCTV_IDLE_FPS": "1", "CCTV_STREAM_FPS": "8",
    "CCTV_ORIGINAL_FPS": "12", "CCTV_ORIGINAL_JPEG_QUALITY": "90",
    # short Original linger here (its own behaviour is covered by test_live_stability.py)
    "CCTV_ORIGINAL_LINGER_S": "0.3",
})
for k in ("CCTV_NVR1_MAX_CONN", "CCTV_NVR2_MAX_CONN", "CCTV_ORIGINAL_MAX_W", "CCTV_ORIGINAL_GRID_MAX_W",
          "CCTV_STANDARD_SUBTYPE", "CCTV_ORIGINAL_SUBTYPE"):
    os.environ.pop(k, None)

import numpy as np                                     # noqa: E402

SIZES = {1: (352, 288), 0: (1920, 1080)}               # subtype -> (w, h) of the fake source
FAIL = set()                                           # (camera index, subtype) whose OPEN fails
DROP_ONCE = set()                                      # (index, subtype): next connection drops after 10 frames
OPENS = {}                                             # (camera index, subtype) -> opens
CONC, MAXCONC = {}, {}                                 # simultaneous captures per (index, subtype)
URLS = []                                              # every URL opened (credentials never checked)
_LOCK = threading.Lock()
URL2IDX = {}


class _Cap:
    def __init__(self, url, *a, **k):
        m = re.search(r"@([^/]+)/cam/realmonitor\?channel=(\d+)&subtype=(\d+)", url)
        self.idx = URL2IDX.get((m.group(1), int(m.group(2)))) if m else None
        self.sub = int(m.group(3)) if m else None
        self.key = (self.idx, self.sub)
        time.sleep(0.03)
        w, h = SIZES.get(self.sub, (8, 8))
        self.frame = np.zeros((h, w, 3), dtype="uint8")
        self.ok = self.key not in FAIL
        self.drop, self.n = self.key in DROP_ONCE, 0
        DROP_ONCE.discard(self.key)
        self.released = False
        with _LOCK:
            URLS.append(url.split("@", 1)[-1])
            OPENS[self.key] = OPENS.get(self.key, 0) + 1
            CONC[self.key] = CONC.get(self.key, 0) + 1
            MAXCONC[self.key] = max(MAXCONC.get(self.key, 0), CONC[self.key])

    def isOpened(self):
        return self.ok

    def grab(self):
        time.sleep(0.02)
        self.n += 1
        if self.drop and self.n > 10:
            return False
        return self.ok

    def retrieve(self):
        return True, self.frame

    def read(self):
        return (True, self.frame) if self.grab() else (False, None)

    def release(self):
        with _LOCK:
            if not self.released:
                self.released = True
                CONC[self.key] -= 1


RESIZES = []                                           # (source w, h) -> (w, h) of every resize


def _resize(frame, size, *a, **k):
    RESIZES.append(((frame.shape[1], frame.shape[0]), tuple(size)))
    return np.zeros((size[1], size[0], 3), dtype="uint8")


def _imencode(ext, frame, params=None):
    q = params[1] if params else 95
    return True, memoryview(f"jpeg-{frame.shape[1]}x{frame.shape[0]}-q{q}".encode())


cv2 = types.ModuleType("cv2")
cv2.CAP_FFMPEG = 0
cv2.IMWRITE_JPEG_QUALITY = 1
cv2.FONT_HERSHEY_SIMPLEX = 0
cv2.LINE_AA = 16
cv2.INTER_AREA = 3
cv2.VideoCapture = _Cap
cv2.resize = _resize
cv2.imencode = _imencode
cv2.putText = lambda *a, **k: None
cv2.getTextSize = lambda text, font, scale, thick: ((int(len(text) * 20 * scale), int(22 * scale)), 5)
cv2.circle = lambda *a, **k: None
sys.modules["cv2"] = cv2

import server                                         # noqa: E402
import rtsp_preflight as rp                           # noqa: E402
from nvr_config import CAMERAS, endpoint              # noqa: E402

for _i, _c in enumerate(CAMERAS):
    _h, _p = endpoint(_c["nvr"])
    URL2IDX[(f"{_h}:{_p}", _c["channel"])] = _i

PF_SUBTYPES = []


class _FakePreflight:
    def __init__(self, alive):
        self.alive, self.ms, self.detail = alive, 0, ""

    def run(self):
        end = time.monotonic() + 0.05
        while time.monotonic() < end:
            if not self.alive():
                return rp.ABORTED
            time.sleep(0.01)
        return rp.OK

    def close(self):
        pass


def _fake_new_preflight(nvr, channel, alive, subtype=1):
    PF_SUBTYPES.append(subtype)
    return _FakePreflight(alive)


server._new_preflight = _fake_new_preflight
server._backoff_s = lambda f: min(0.1 * 2 ** (max(f, 1) - 1), 0.8)


class _Up:
    checked, reachable, label = time.time(), True, "NVR REACHABLE"


server.MONITOR.get = lambda nvr: _Up()
for _k in server.NVRS:
    server._AUTH_OK[_k] = True

S, O = server.STREAMS, server.ORIG_STREAMS
NVR2 = [s.index for s in S if s.info["nvr"] == "nvr2"]
NVR1 = [s.index for s in S if s.info["nvr"] == "nvr1"]
CAP = server.NVR_CAP
FAILS = []
PEAK = {"nvr1": 0, "nvr2": 0}
_RUN = {"on": True}


def _sampler():
    while _RUN["on"]:
        with server._ACTIVE_LOCK:
            for k in server.NVRS:
                PEAK[k] = max(PEAK[k], server.NVR_ACTIVE[k], len(server.NVR_OWNERS[k]))
        time.sleep(0.002)


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def wait(cond, timeout=6.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def owners(nvr):
    with server._ACTIVE_LOCK:
        return set(server.NVR_OWNERS[nvr])


def consistent():
    with server._ACTIVE_LOCK:
        return all(len(server.NVR_OWNERS[k]) == server.NVR_ACTIVE[k] <= CAP[k] for k in server.NVRS)


class Viewer(threading.Thread):
    """One browser <img>: records every MJPEG part (ms, X-Frame-State, body)."""

    def __init__(self, port, path):
        super().__init__(daemon=True)
        self.port, self.path, self.parts, self.sock, self.stop = port, path, [], None, False
        self.start()

    def run(self):
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            self.sock = s
            t0 = time.monotonic()
            s.sendall(f"GET {self.path} HTTP/1.1\r\nHost: t\r\n\r\n".encode())
            buf = b""
            while not self.stop:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    m = re.search(rb"--frame\r\n(.*?)\r\n\r\n", buf, re.S)
                    if not m:
                        break
                    hdr = m.group(1).decode("latin-1")
                    n = int(re.search(r"Content-Length: (\d+)", hdr).group(1))
                    if len(buf) < m.end() + n:
                        break
                    st = re.search(r"X-Frame-State: (\w+)", hdr)
                    self.parts.append((round((time.monotonic() - t0) * 1000), st.group(1) if st else None,
                                       buf[m.end():m.end() + n]))
                    buf = buf[m.end() + n:]
        except OSError:
            pass

    def states(self):
        return [p[1] for p in self.parts]

    def first(self, state):
        return next((p for p in self.parts if p[1] == state), None)

    def close(self):
        self.stop = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        except (OSError, AttributeError):
            pass


def url(i, quality=None, extra=""):
    q = [f"key={server.TOKEN}"] if server.TOKEN else []
    if quality:
        q.append(f"quality={quality}")
    if extra:
        q.append(extra)
    return f"/stream/{i}" + ("?" + "&".join(q) if q else "")


def settle_pool(timeout=8.0):
    return wait(lambda: all(len([s for s in S if s.info["nvr"] == k and s._bg and s._running and s.live_since])
                            == min(6, len([s for s in S if s.info["nvr"] == k])) for k in server.NVRS), timeout)


# ── tests ────────────────────────────────────────────────────────────────
def test_standard_is_default_and_uses_substream(port):
    settle_pool()
    i = NVR1[0]
    v = Viewer(port, url(i))                       # /stream/<i> with no quality -> Standard
    ok = wait(lambda: any(st == "live" for st in v.states()), 4.0)
    check("/stream/<id> without ?quality -> Standard worker, live", ok and S[i].viewers == 1 and O[i].viewers == 0,
          (v.states()[:3], S[i].viewers, O[i].viewers))
    body = v.first("live")[2] if v.first("live") else b""
    check("Standard: sub-stream (subtype=1) source 352x288 -> output 640x360, JPEG quality 70",
          S[i].subtype == 1 and S[i].source_size == (352, 288) and S[i].output_size == (640, 360)
          and body == b"jpeg-640x360-q70", (S[i].subtype, S[i].source_size, S[i].output_size, body))
    check("Standard never opened the main stream", OPENS.get((i, 0), 0) == 0)
    v.close()


def test_original_is_the_real_main_stream(port):
    i = NVR1[1]
    before = sum(1 for u in URLS if f"subtype=0" in u)
    v = Viewer(port, url(i, "original", "prio=full"))
    ok = wait(lambda: any(st == "live" for st in v.states()), 5.0)
    body = v.first("live")[2] if v.first("live") else b""
    check("?quality=original -> the Original worker, live", ok and O[i].viewers == 1, v.states()[:4])
    check("Original opened the MAIN stream (subtype=0), Standard did not change",
          sum(1 for u in URLS if "subtype=0" in u) == before + 1 and O[i].subtype == 0)
    check("Original frame = the 1920x1080 source, NOT resized (no 640x360 -> big upscale)",
          O[i].source_size == (1920, 1080) and O[i].output_size == (1920, 1080) and body == b"jpeg-1920x1080-q90",
          (O[i].source_size, O[i].output_size, body))
    check("no resize was ever applied to an Original (1920x1080) frame",
          not any(src == (1920, 1080) for src, _ in RESIZES))
    first_orig = v.first("live")
    check("before its first Original frame the viewer saw a picture, not nothing",
          v.parts and v.parts[0][1] in ("standard", "cached", "status", "live"), v.states()[:3])
    print(f"      (first Original frame after {first_orig[0] if first_orig else '-'} ms; states "
          f"{sorted(set(v.states()))})")
    check("Original viewer counted as fullscreen (top slot priority, pinned)",
          O[i].viewers_full == 1 and O[i].slot_priority() == server.PRIO_FULLSCREEN_ORIGINAL and O[i].pinned)
    v.close()
    check("last Original viewer left -> Original worker stops (never kept in the background)",
          wait(lambda: not O[i]._running and O[i].slot_key not in owners("nvr1"), 3.0),
          (O[i]._running, owners("nvr1")))


def test_separate_identity_and_slot_keys(port):
    i = NVR2[0]
    a = Viewer(port, url(i))
    b = Viewer(port, url(i, "original"))
    ok = wait(lambda: S[i].viewers == 1 and O[i].viewers == 1 and O[i].tier() == "HOT", 5.0)
    check("same camera, two qualities: two workers, separate viewer counts", ok, (S[i].viewers, O[i].viewers))
    check("slot keys differ: Standard = index, Original = 1000 + index",
          S[i].slot_key == i and O[i].slot_key == 1000 + i and {i, 1000 + i} <= owners("nvr2"), owners("nvr2"))
    a.close()
    b.close()
    wait(lambda: not O[i]._running, 3.0)


def test_original_shared_by_many_viewers(port):
    i = NVR1[2]
    o0 = OPENS.get((i, 0), 0)
    vs = [Viewer(port, url(i, "original")) for _ in range(3)]
    wait(lambda: all(any(st == "live" for st in v.states()) for v in vs), 5.0)
    time.sleep(0.5)
    check("3 Original viewers of one camera -> ONE main-stream connection",
          OPENS.get((i, 0), 0) - o0 == 1 and O[i].viewers == 3 and MAXCONC.get((i, 0)) == 1,
          (OPENS.get((i, 0), 0) - o0, O[i].viewers, MAXCONC.get((i, 0))))
    for v in vs:
        v.close()
    check("... and it stops when the last one leaves", wait(lambda: not O[i]._running, 3.0))


def test_toggle_ten_times_no_leak(port):
    settle_pool()
    i = NVR2[1]
    for n in range(10):
        v = Viewer(port, url(i, "original" if n % 2 == 0 else None))
        wait(lambda: any(st in ("live", "standard") for st in v.states()), 3.0)
        time.sleep(0.15)
        v.close()
    ok = wait(lambda: not O[i]._running and O[i].viewers == 0 and S[i].viewers == 0, 3.0)
    check("10 quality toggles: no Original worker left, no viewer left", ok, (O[i]._running, O[i].viewers, S[i].viewers))
    check("never two captures of the same stream at once", max(MAXCONC.get((i, 0), 0), MAXCONC.get((i, 1), 0)) <= 1,
          (MAXCONC.get((i, 0)), MAXCONC.get((i, 1))))
    check("slot table consistent, cap respected", consistent())
    check("no Original slot held (released right after the worker stops)",
          wait(lambda: not any(k >= 1000 for k in owners("nvr2")), 2.0), owners("nvr2"))


def test_standard_stand_in_while_original_starts(port):
    i = NVR2[2]                                          # HOT in the background (Standard)
    wait(lambda: S[i].tier() == "HOT", 4.0)
    orig_delay = _Cap.__init__

    def slow_init(self, url_, *a, **k):                 # main stream takes 1.5 s to open
        orig_delay(self, url_, *a, **k)
        if "subtype=0" in url_:
            time.sleep(1.5)
    _Cap.__init__ = slow_init
    try:
        v = Viewer(port, url(i, "original"))
        ok = wait(lambda: any(st == "live" for st in v.states()), 6.0)
    finally:
        _Cap.__init__ = orig_delay
    st = v.states()
    first_std = v.first("standard")
    check("while the main stream opens, the viewer sees the LIVE Standard picture (labelled), not a blank tile",
          first_std is not None and st[0] == "standard", st[:5])
    check("... that stand-in is the 640x360 Standard frame, clearly not passed off as Original",
          first_std and first_std[2] == b"jpeg-640x360-q70", first_std[2] if first_std else None)
    check("... then the Original frames replace it by themselves", ok and st[-1] == "live", st[-3:])
    v.close()
    wait(lambda: not O[i]._running, 3.0)


def test_fallback_when_main_stream_fails_then_recovers(port):
    i = NVR2[3]
    FAIL.add((i, 0))                                     # main stream cannot be opened
    s0 = S[i].viewers
    v = Viewer(port, url(i, "original", "prio=full"))
    ok = wait(lambda: server.stream_info(i)["original"]["fallbackViewers"] == 1, 5.0)
    check("main stream fails -> viewer falls back to Standard (and the server says so)", ok,
          server.stream_info(i)["original"])
    check("... the fallback holds a real Standard viewer (video keeps coming)", S[i].viewers == s0 + 1, S[i].viewers)
    wait(lambda: v.states().count("standard") >= 3, 3.0)
    check("... frames shown are Standard (labelled), never 'live' Original", "standard" in v.states() and
          "live" not in v.states(), sorted(set(v.states())))
    FAIL.discard((i, 0))                                 # main stream works again
    ok = wait(lambda: v.states() and v.states()[-1] == "live", 8.0)
    check("main stream back -> the viewer switches to Original by itself", ok, v.states()[-4:])
    check("... and the Standard fallback viewer is released",
          wait(lambda: S[i].viewers == s0 and server.stream_info(i)["original"]["fallbackViewers"] == 0, 3.0),
          (S[i].viewers, s0))
    v.close()
    wait(lambda: not O[i]._running, 3.0)


def test_dropped_original_reconnects_without_fallback(port):
    i = NVR1[6]
    DROP_ONCE.add((i, 0))                                        # the first main-stream connection drops
    r0 = O[i].reconnects
    seen = {"fallback": 0, "stdViewers": 0}
    run = {"on": True}

    def watch():
        while run["on"]:
            seen["fallback"] = max(seen["fallback"], O[i].fallback_viewers)
            seen["stdViewers"] = max(seen["stdViewers"], S[i].viewers)
            time.sleep(0.003)
    threading.Thread(target=watch, daemon=True).start()
    v = Viewer(port, url(i, "original", "prio=full"))
    ok = wait(lambda: O[i].reconnects > r0 and O[i].tier() == "HOT" and v.states()[-1:] == ["live"], 6.0)
    time.sleep(0.3)
    run["on"] = False
    check("a live Original stream that drops reconnects by itself",
          ok and O[i].reconnects == r0 + 1, (O[i].reconnects - r0, O[i].tier()))
    check("... without a Standard fallback for the short gap (no extra NVR connection)",
          seen["fallback"] == 0 and seen["stdViewers"] == 0, seen)
    v.close()
    wait(lambda: not O[i]._running, 3.0)


def test_page_switch_to_original_goes_tile_by_tile(port):
    wait(lambda: not any(o._running for o in O), 5.0)            # nothing left from earlier tests
    settle_pool()
    cams = NVR2[:6]
    stds = [Viewer(port, url(i)) for i in cams]                  # a grid page in Standard
    wait(lambda: all(S[i].tier() == "HOT" and S[i].viewers == 1 for i in cams), 6.0)
    orig_init = _Cap.__init__

    def slow(self, u, *a, **k):                                  # main stream opens in 0.4 s
        orig_init(self, u, *a, **k)
        if "subtype=0" in u:
            time.sleep(0.4)
    stats = {"maxConnecting": 0, "maxFallback": 0}
    run = {"on": True}

    def watch():
        while run["on"]:
            with server._ACTIVE_LOCK:
                own = set(server.NVR_OWNERS["nvr2"])
            conn = sum(1 for i in cams if O[i].slot_key in own and O[i].live_since is None)
            stats["maxConnecting"] = max(stats["maxConnecting"], conn)
            stats["maxFallback"] = max(stats["maxFallback"], sum(O[i].fallback_viewers for i in cams))
            time.sleep(0.005)
    _Cap.__init__ = slow
    threading.Thread(target=watch, daemon=True).start()
    try:
        origs = [Viewer(port, url(i, "original", "fps=6")) for i in cams]   # the page swaps every src
        for v in stds:
            v.close()
        ok = wait(lambda: all(any(st == "live" for st in v.states()) for v in origs), 15.0)
    finally:
        _Cap.__init__ = orig_init
        run["on"] = False
    check("page of 6 switched to Original: all 6 tiles reach Original", ok, [v.states()[-1:] for v in origs])
    check("... one tile at a time (never 2 main streams connecting at once)", stats["maxConnecting"] <= 1, stats)
    check("... no tile put on a Standard 'fallback' while it only waited for its turn",
          stats["maxFallback"] == 0, stats)
    later = [v for v in origs if v.first("live") and v.first("live")[0] > 1200]
    kept = [v for v in later if "standard" in [p[1] for p in v.parts if p[0] < v.first("live")[0]]]
    check("... tiles waiting for their turn kept the live Standard picture (labelled) meanwhile",
          len(later) >= 2 and len(kept) == len(later), (len(later), len(kept)))
    check("cap respected throughout", PEAK["nvr2"] <= CAP["nvr2"] and consistent(), PEAK)
    for v in origs:
        v.close()
    wait(lambda: not any(O[i]._running for i in cams), 4.0)


def test_waiting_for_a_slot_is_said_and_is_not_a_fallback(port):
    settle_pool()
    cams = NVR2[:6]
    stds = [Viewer(port, url(i)) for i in cams]                  # someone watches 6 NVR2 cameras
    wait(lambda: all(S[i].viewers == 1 and S[i].tier() == "HOT" for i in cams), 6.0)
    x = NVR2[8]
    v = Viewer(port, url(x, "original", "prio=full"))            # Original fullscreen of a 7th camera
    ok = wait(lambda: O[x].status == server.S_WAIT_SLOT, 5.0)
    time.sleep(1.5)
    info = server.stream_info(x)["original"]
    check("NVR full of viewed streams: the Original waits and says so ('Waiting for NVR slot')",
          ok and info["status"] == server.S_WAIT_SLOT and not info["slotHeld"], info)
    check("... no Standard fallback grabbed meanwhile, no viewed stream stopped",
          info["fallbackViewers"] == 0 and S[x].viewers == 0 and all(S[i].tier() == "HOT" for i in cams),
          (info["fallbackViewers"], S[x].viewers))
    check("... the viewer still gets pictures (never an empty tile), none of them 'live'",
          len(v.parts) > 0 and "live" not in v.states(), v.states()[:3])
    stds[0].close()
    check("a viewed stream leaves -> the waiting Original gets that slot",
          wait(lambda: O[x].tier() == "HOT", 5.0), O[x].tier())
    for w in stds[1:] + [v]:
        w.close()
    wait(lambda: not O[x]._running, 3.0)


def test_original_counts_toward_cap_and_priority(port):
    settle_pool()
    cams = NVR2[:6]
    vs = [Viewer(port, url(i, "original")) for i in cams]   # 6 Original grid tiles on NVR2
    ok = wait(lambda: all(O[i].tier() == "HOT" for i in cams), 8.0)
    check("6 Original streams on one NVR go live (the pool frees background slots for them)", ok,
          {i: O[i].tier() for i in cams})
    check("NVR2 cap respected: 6 slots, all Original",
          len(owners("nvr2")) <= 6 and all(k >= 1000 for k in owners("nvr2")), sorted(owners("nvr2")))
    extra_grid = Viewer(port, url(NVR2[7]))                  # Standard grid viewer, NVR2 full
    time.sleep(0.4)
    extra_full = Viewer(port, url(NVR2[8], "original", "prio=full"))   # Original fullscreen, arrives later
    time.sleep(1.2)
    check("with every slot held by viewed streams, both wait (nothing viewed is killed)",
          not O[NVR2[8]].tier() == "HOT" and all(O[i].tier() == "HOT" for i in cams))
    vs[0].close()                                            # one Original tile goes away
    ok = wait(lambda: O[NVR2[8]].tier() == "HOT", 5.0)
    check("the freed slot goes to the Original FULLSCREEN camera first (before the older Standard waiter)",
          ok and S[NVR2[7]].slot_key not in owners("nvr2"), (O[NVR2[8]].tier(), sorted(owners("nvr2"))))
    for v in vs[1:] + [extra_grid, extra_full]:
        v.close()
    check("everything released afterwards (no Original slot left)",
          wait(lambda: not any(k >= 1000 for k in owners("nvr2")) and consistent(), 4.0), sorted(owners("nvr2")))
    check(f"per-NVR cap never exceeded (peak {PEAK})", all(PEAK[k] <= CAP[k] for k in server.NVRS), PEAK)


def test_original_fps_follows_viewers_and_sends_only_new_frames(port):
    i = NVR1[4]
    g = Viewer(port, url(i, "original", "fps=6"))                  # an Original grid tile
    wait(lambda: O[i].tier() == "HOT" and any(st == "live" for st in g.states()), 5.0)
    time.sleep(0.3)
    check("grid tile only (fps=6): the Original worker publishes 6 fps", O[i].pub_fps == 6, O[i].pub_fps)
    p0, n0 = O[i].published, len(g.parts)
    time.sleep(2.0)
    dp, dn = O[i].published - p0, len(g.parts) - n0
    check("... ~6 frames/s encoded, each sent to the tile once (no full-size JPEG sent twice)",
          9 <= dp <= 14 and dn <= dp + 1, (dp, dn))
    f = Viewer(port, url(i, "original", "prio=full"))              # fullscreen, same camera
    wait(lambda: O[i].pub_fps == 12 and any(st == "live" for st in f.states()), 3.0)
    time.sleep(0.3)
    p0, n0, m0 = O[i].published, len(g.parts), len(f.parts)
    time.sleep(2.0)
    dp, dn, dm = O[i].published - p0, len(g.parts) - n0, len(f.parts) - m0
    check("+ fullscreen viewer: worker publishes 12 fps, fullscreen gets ~12/s, the tile still ~6/s",
          O[i].pub_fps == 12 and 19 <= dp <= 27 and 17 <= dm <= dp + 1 and 8 <= dn <= 14, (dp, dm, dn))
    check("... still ONE main-stream connection for both", MAXCONC.get((i, 0)) == 1, MAXCONC.get((i, 0)))
    f.close()
    check("fullscreen closed -> back to 6 fps", wait(lambda: O[i].pub_fps == 6, 3.0), O[i].pub_fps)
    g.close()
    check("... and the worker stops with the last viewer", wait(lambda: not O[i]._running, 3.0))


def test_grid_tiles_get_a_display_size_fullscreen_the_full_source(port):
    i = NVR1[5]
    g = Viewer(port, url(i, "original", "fps=6"))                  # grid tile only
    wait(lambda: any(st == "live" for st in g.states()), 5.0)
    time.sleep(0.4)
    body = g.parts[-1][2] if g.parts else b""
    check("Original, grid tiles only: main stream scaled DOWN to 1280 wide (never up), JPEG 90",
          O[i].source_size == (1920, 1080) and O[i].output_size == (1280, 720) and body == b"jpeg-1280x720-q90",
          (O[i].source_size, O[i].output_size, body))
    f = Viewer(port, url(i, "original", "prio=full"))              # + fullscreen of the same camera
    ok = wait(lambda: O[i].output_size == (1920, 1080) and f.parts and f.parts[-1][2] == b"jpeg-1920x1080-q90", 3.0)
    check("... a fullscreen viewer gets the FULL source picture (1920x1080)", ok,
          (O[i].output_size, f.parts[-1][2] if f.parts else None))
    f.close()
    check("fullscreen closed -> grid tiles back to 1280 wide",
          wait(lambda: O[i].output_size == (1280, 720), 3.0), O[i].output_size)
    g.close()
    wait(lambda: not O[i]._running, 3.0)


def test_status_and_stream_info(port):
    i = NVR1[3]
    v = Viewer(port, url(i, "original", "prio=full"))
    wait(lambda: O[i].tier() == "HOT", 5.0)
    info = server.stream_info(i)
    o = info["original"]
    check("/api/stream-info: Original subtype 0, source/output 1920x1080, JPEG 90, fps 12, slot held",
          o["subtype"] == 0 and o["sourceSize"] == "1920x1080" and o["outputSize"] == "1920x1080"
          and o["jpegQuality"] == 90 and o["fps"] == 12 and o["slotHeld"] and o["live"], o)
    d = server.system_status()
    c = d["cameras"][i]
    check("/api/status: per camera Standard fields + an 'original' block + quality config",
          c["subtype"] == 1 and c["original"]["qualityMode"] == "original" and
          d["config"]["original"]["subtype"] == 0 and d["config"]["standard"]["jpegQuality"] == 70, c.get("original"))
    check("/api/status owners say which quality holds each slot",
          any(ow["quality"] == "original" and ow["index"] == i for ow in d["nvrs"]["nvr1"]["owners"]))
    blob = json.dumps(d) + json.dumps(info)
    secrets = [x for n in server.NVRS.values() for x in (n["user"], n["pass"]) if x and len(x) >= 4]
    check("no NVR usernames/passwords anywhere in the status", not any(x in blob for x in secrets))
    v.close()
    wait(lambda: not O[i]._running, 3.0)


def test_pages():
    page, sp = server.PAGE, server.SETTINGS_PAGE
    check("grid: quality switch [Standard | Original] in the header + fullscreen bar",
          page.count("class=qseg") == 2 and "data-q=standard" in page and "data-q=original" in page)
    check("grid: default Standard, remembered per browser only if chosen",
          "let quality = 'standard'" in page and "localStorage.getItem(QKEY) === 'original'" in page)
    check("grid: Original URL = ?quality=original (Standard URL unchanged)",
          "'quality=original'" in page and "function streamUrl(i){ return '/stream/'+i+q +" in page)
    check("settings page previews stay Standard (no quality parameter)", "quality" not in sp)


if __name__ == "__main__":
    t0 = time.time()
    threading.Thread(target=_sampler, daemon=True).start()
    srv = server.QuietServer(("127.0.0.1", 0), server.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    server.POOL.start()
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n-- {name}")
            fn() if name == "test_pages" else fn(port)
    _RUN["on"] = False
    srv.shutdown()
    print(f"\npeak NVR slots used: {PEAK} (cap {dict(CAP)})")
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}  ({time.time() - t0:.1f}s)")
    sys.exit(1 if FAILS else 0)
