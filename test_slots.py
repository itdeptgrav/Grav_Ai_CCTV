"""Slot / fairness / page-switch tests for the CCTV server (offline, no NVR needed).

Complements test_lifecycle.py. cv2 and the RTSP pre-flight are replaced by
controllable per-camera stubs, so each camera can be made healthy, dead (pre-flight
never answers), failing (pre-flight OK but OpenCV open fails), dropping (stream
dies after a few frames) or rejected (credentials refused).

Run:  python test_slots.py        (exits non-zero on any failure)
"""
import os
import re
import sys
import time
import json
import types
import random
import threading

os.environ["CCTV_PREFLIGHT"] = "1"          # pre-flight ON (stubbed below)
os.environ["CCTV_PERSISTENT"] = "0"         # on-demand mode (the fallback); test_relay.py covers the pool
os.environ["CCTV_LOG_EVENTS"] = "0"
os.environ.setdefault("CCTV_NVR_MAX_CONN", "6")

import numpy as np                          # noqa: E402

# ── controllable cv2 stub ────────────────────────────────────────────────
BEHAVIOR = {}          # camera index -> "ok" | "fail" | "drop" | "noframe"
OPEN_COUNT = {}        # camera index -> OpenCV opens attempted
_OPEN = {"in": 0, "max": 0}
_LOCK = threading.Lock()
URL2IDX = {}           # (host:port, channel) -> camera index (filled after import)


class _Cap:
    def __init__(self, url, *a, **k):
        m = re.search(r"@([^/]+)/cam/realmonitor\?channel=(\d+)", url)
        self.idx = URL2IDX.get((m.group(1), int(m.group(2)))) if m else None
        with _LOCK:
            _OPEN["in"] += 1
            _OPEN["max"] = max(_OPEN["max"], _OPEN["in"])
            OPEN_COUNT[self.idx] = OPEN_COUNT.get(self.idx, 0) + 1
        time.sleep(0.05)                       # an open takes time: overlap would show
        with _LOCK:
            _OPEN["in"] -= 1
        self.mode = BEHAVIOR.get(self.idx, "ok")
        self.n = 0

    def isOpened(self):
        return self.mode != "fail"

    def read(self):
        return (True, self.retrieve()[1]) if self.grab() else (False, None)

    def grab(self):
        time.sleep(0.02)
        self.n += 1
        return not (self.mode == "noframe" or (self.mode == "drop" and self.n > 10))

    def retrieve(self):
        return True, np.zeros((8, 8, 3), dtype="uint8")

    def release(self):
        pass


cv2 = types.ModuleType("cv2")
cv2.CAP_FFMPEG = 0
cv2.IMWRITE_JPEG_QUALITY = 1
cv2.FONT_HERSHEY_SIMPLEX = 0
cv2.VideoCapture = _Cap
cv2.resize = lambda frame, size: frame
cv2.imencode = lambda ext, frame, *a: (True, memoryview(b"jpegbytes"))
cv2.putText = lambda *a, **k: None
cv2.LINE_AA = 16
cv2.getTextSize = lambda text, font, scale, thick: ((int(len(text) * 20 * scale), int(22 * scale)), 5)
cv2.circle = lambda *a, **k: None
sys.modules["cv2"] = cv2

import server                              # noqa: E402
import rtsp_preflight as rp                # noqa: E402
from nvr_config import CAMERAS, endpoint   # noqa: E402

for _i, _c in enumerate(CAMERAS):
    _h, _p = endpoint(_c["nvr"])
    URL2IDX[(f"{_h}:{_p}", _c["channel"])] = _i

# ── controllable pre-flight stub ─────────────────────────────────────────
PF_BEHAVIOR = {}       # camera index -> "ok" | "dead" | "auth"
PF_COUNT = {}
_PF = {"in": {}, "max": {}}
PF_DELAY = [0.1]


class _FakePreflight:
    def __init__(self, idx, nvr, alive):
        self.idx, self.nvr, self.alive = idx, nvr, alive
        self.ms, self.detail, self.result = None, "", None

    def run(self):
        t0 = time.monotonic()
        with _LOCK:
            PF_COUNT[self.idx] = PF_COUNT.get(self.idx, 0) + 1
            _PF["in"][self.nvr] = _PF["in"].get(self.nvr, 0) + 1
            _PF["max"][self.nvr] = max(_PF["max"].get(self.nvr, 0), _PF["in"][self.nvr])
        try:
            mode = PF_BEHAVIOR.get(self.idx, "ok")
            end = time.monotonic() + PF_DELAY[0]
            while time.monotonic() < end:
                if not self.alive():
                    self.result = rp.ABORTED
                    return self.result
                time.sleep(0.01)
            self.result = {"ok": rp.OK, "dead": rp.DEAD, "auth": rp.AUTH_FAIL}[mode]
            self.detail = {"ok": "", "dead": "no live video (stub)", "auth": "RTSP 401 (stub)"}[mode]
            return self.result
        finally:
            self.ms = round((time.monotonic() - t0) * 1000)
            with _LOCK:
                _PF["in"][self.nvr] -= 1

    def close(self):
        pass


def _fake_new_preflight(nvr, channel, alive):
    idx = next(i for i, c in enumerate(CAMERAS) if c["nvr"] == nvr and c["channel"] == channel)
    return _FakePreflight(idx, nvr, alive)


server._new_preflight = _fake_new_preflight
server._backoff_s = lambda f: min(0.1 * 2 ** (max(f, 1) - 1), 0.8)   # fast, still escalating


class _Up:
    checked, reachable, label = time.time(), True, "NVR REACHABLE"


server.MONITOR.get = lambda nvr: _Up()
for _k in server.NVRS:
    server._AUTH_OK[_k] = True                  # credentials already confirmed

PAGES = {p: list(range((p - 1) * 6, min(p * 6, len(CAMERAS)))) for p in range(1, 6)}
S = server.STREAMS

# ── helpers ──────────────────────────────────────────────────────────────
FAILS = []


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


def active(nvr):
    with server._ACTIVE_LOCK:
        return server.NVR_ACTIVE[nvr]


def live(idxs):
    return all(S[i].status == server.S_LIVE and S[i].jpeg() is not None for i in idxs)


def start(idxs):
    for i in idxs:
        S[i].add_viewer()


def stop(idxs):
    for i in idxs:
        S[i].remove_viewer()


def drain():
    for s in S:
        while s.viewers > 0:
            s.remove_viewer()
    ok = wait(lambda: not any(s._running for s in S) and all(active(k) == 0 for k in server.NVRS)
              and all(not owners(k) for k in server.NVRS), timeout=6.0)
    # wait for worker threads to actually finish (they log/clean up after _running=False)
    wait(lambda: threading.active_count() <= 3, timeout=3.0)
    BEHAVIOR.clear()
    PF_BEHAVIOR.clear()
    OPEN_COUNT.clear()                     # per-test counters: a camera opened while
    PF_COUNT.clear()                       # healthy in an earlier test must not count
    server._LAST_FAIL.clear()
    return ok


# ── tests ────────────────────────────────────────────────────────────────
def test_no_slot_leak_after_page_switch():
    start(PAGES[1])
    check("page 1: all 6 cameras live", wait(lambda: live(PAGES[1])))
    check("page 1: NVR2 slot owners are exactly page 1", owners("nvr2") == set(PAGES[1]), owners("nvr2"))
    stop(PAGES[1])                          # clearCells()
    time.sleep(0.5)                         # UI teardown delay
    check("after page-1 cleanup: NVR2 active 0 and no owners",
          wait(lambda: active("nvr2") == 0 and not owners("nvr2"), 2.0), f"active={active('nvr2')}")
    check("after page-1 cleanup: page-1 viewers all 0", all(S[i].viewers == 0 for i in PAGES[1]))
    start(PAGES[2])
    check("page 2: all 6 cameras live", wait(lambda: live(PAGES[2])))
    check("page 2: slot owners are exactly page 2 (no page-1 stragglers)",
          owners("nvr2") == set(PAGES[2]), owners("nvr2"))
    check("page 2: no page-1 worker still running", not any(S[i]._running for i in PAGES[1]))
    drain()


def test_failed_open_never_holds_slot():
    BEHAVIOR[0] = "fail"                    # pre-flight OK, OpenCV open fails
    PF_BEHAVIOR[1] = "dead"                 # pre-flight never answers
    start([0, 1])
    held = 0
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        o = owners("nvr2")
        held += (0 in o) + (1 in o)
        time.sleep(0.01)
    check("failing camera never held a slot (sampled 2 s)", held == 0, f"held samples={held}")
    check("failing camera kept retrying (with back-off)", OPEN_COUNT.get(0, 0) >= 2, OPEN_COUNT.get(0))
    check("dead channel never reached OpenCV (caught by pre-flight)", OPEN_COUNT.get(1, 0) == 0)
    check("failing cameras show a failure state",
          S[0].status in (server.S_RETRYING, server.S_OFFLINE) and
          S[1].status in (server.S_RETRYING, server.S_OFFLINE), f"{S[0].status} / {S[1].status}")
    drain()


def test_dropped_stream_releases_slot_before_backoff():
    BEHAVIOR[2] = "drop"                    # live for 10 frames, then the stream dies
    start([2])
    seen_retry, bad = 0, 0
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        st = S[2].status
        if st == server.S_RETRYING:
            seen_retry += 1
            bad += 2 in owners("nvr2")
        time.sleep(0.005)
    check("dropped stream went to Retrying...", seen_retry > 0)
    check("slot released BEFORE the retry back-off (never held while Retrying...)", bad == 0, bad)
    check("dropped stream reconnected and went live again", OPEN_COUNT.get(2, 0) >= 2)
    drain()


def test_healthy_gets_slots_while_dead_backs_off():
    PF_BEHAVIOR[6] = "dead"                 # like NVR2 ch7 on page 2
    start(PAGES[2])
    healthy = PAGES[2][1:]
    check("page 2: all 5 healthy cameras live despite the dead one", wait(lambda: live(healthy), 5.0))
    check("dead camera holds no slot", 6 not in owners("nvr2"))
    check("dead camera never entered OpenCV / the serialized open gate", OPEN_COUNT.get(6, 0) == 0)
    drain()


def test_connect_concurrency_cap():
    _OPEN["max"] = 0
    start(PAGES[1])
    wait(lambda: live(PAGES[1]))
    check(f"OpenCV opens never overlapped beyond CONNECT_MAX={server.CONNECT_MAX}",
          _OPEN["max"] <= server.CONNECT_MAX, _OPEN["max"])
    drain()


def test_preflight_concurrency_cap():
    orig = server.PREFLIGHT_SEM["nvr2"]
    server.PREFLIGHT_SEM["nvr2"] = threading.BoundedSemaphore(2)
    PF_DELAY[0] = 0.25
    _PF["max"]["nvr2"] = 0
    try:
        start(PAGES[1])
        wait(lambda: live(PAGES[1]), 8.0)
        check("per-NVR pre-flight cap respected (2)", _PF["max"]["nvr2"] <= 2, _PF["max"]["nvr2"])
        check("per-NVR pre-flight cap exercised", _PF["max"]["nvr2"] == 2, _PF["max"]["nvr2"])
    finally:
        drain()
        server.PREFLIGHT_SEM["nvr2"] = orig
        PF_DELAY[0] = 0.1
    _PF["max"]["nvr2"] = 0
    start(PAGES[1])
    wait(lambda: live(PAGES[1]))
    check(f"default pre-flights run in parallel (<= {server.PREFLIGHT_PER_NVR})",
          2 <= _PF["max"]["nvr2"] <= server.PREFLIGHT_PER_NVR, _PF["max"]["nvr2"])
    drain()


def test_viewer_disconnect_and_last_viewer():
    c = S[8]
    c.add_viewer()
    c.add_viewer()
    check("2 viewers counted", c.viewers == 2)
    check("camera live with 2 viewers", wait(lambda: live([8])))
    check("2 viewers share 1 slot", owners("nvr2") == {8} and active("nvr2") == 1)
    c.remove_viewer()
    time.sleep(0.4)
    check("1 viewer left: still streaming, slot kept", c._running and 8 in owners("nvr2"))
    c.remove_viewer()
    check("last viewer left: worker stopped and slot released",
          wait(lambda: not c._running and 8 not in owners("nvr2") and active("nvr2") == 0, 1.5))
    check("status back to Idle", wait(lambda: c.status == server.S_IDLE, 1.0), c.status)
    drain()


def test_repeated_page_switch():
    rnd = random.Random(7)
    PF_BEHAVIOR[6] = "dead"                 # a dead channel in the mix, like reality
    BEHAVIOR[20] = "fail"
    cur = []
    for _ in range(25):
        stop(cur)
        cur = PAGES[rnd.randint(1, 5)]
        start(cur)
        time.sleep(rnd.uniform(0.0, 0.35))  # some switches happen mid-startup
    stop(cur)
    ok = drain()
    check("25 rapid page switches: everything drained", ok)
    check("no slot owners / waiters left",
          all(not server.NVR_OWNERS[k] and not server.NVR_WAITERS[k] for k in server.NVRS))
    check("semaphores fully restored (no leaked slot)",
          all(server.NVR_SEM[k]._value == server.NVR_MAX_CONN for k in server.NVRS),
          {k: server.NVR_SEM[k]._value for k in server.NVRS})
    check("connect gate and pre-flight caps fully restored",
          server.CONNECT_GATE._value == server.CONNECT_MAX and
          all(server.PREFLIGHT_SEM[k]._value == server.PREFLIGHT_PER_NVR for k in server.NVRS))
    check("fresh-queue counter back to 0", server._FRESH_WAITING == 0, server._FRESH_WAITING)


def test_retry_fairness_and_rate_limit():
    PF_BEHAVIOR[12] = "dead"                # NVR2 ch13 on page 3
    PF_COUNT.pop(12, None)
    start(PAGES[3])
    ok = wait(lambda: live(PAGES[3][1:]), 4.0)
    time.sleep(3.0)                         # let the dead one keep retrying ~3 s
    n = PF_COUNT.get(12, 0)
    check("page 3: all 5 healthy NVR1 cameras live", ok)
    check("dead camera retries are rate-limited by back-off (<= 9 in ~3-4 s)", n <= 9, n)
    check("dead camera does not take slots or OpenCV opens", 12 not in owners("nvr2") and OPEN_COUNT.get(12, 0) == 0)
    drain()


def test_dead_status_progression():
    PF_BEHAVIOR[7] = "dead"
    seen = []
    start([7])
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        st = S[7].status
        if st != server.S_IDLE and (not seen or seen[-1] != st):
            seen.append(st)
        time.sleep(0.005)
    check("dead camera: Connecting... -> Retrying... -> Camera offline",
          seen[:3] == [server.S_CONNECTING, server.S_RETRYING, server.S_OFFLINE], seen)
    check("dead camera error is recorded (masked)", bool(S[7].last_error), S[7].last_error)
    stop([7])                                # leave the page (keep the dead-channel memory)
    wait(lambda: not S[7]._running and S[7].status == server.S_IDLE, 2.0)
    start([7])                               # revisit: known dead -> offline at once
    time.sleep(0.05)
    check("revisit of a recently-dead camera shows Camera offline immediately",
          S[7].status == server.S_OFFLINE, S[7].status)
    drain()


def test_auth_failure_single_login_then_pause():
    server._AUTH_OK["nvr1"] = False
    for i in PAGES[4]:
        PF_BEHAVIOR[i] = "auth"
    before = {i: PF_COUNT.get(i, 0) for i in PAGES[4]}
    opens_before = sum(OPEN_COUNT.get(i, 0) for i in PAGES[4])
    try:
        start(PAGES[4])
        time.sleep(1.2)
        attempts = sum(PF_COUNT.get(i, 0) - before[i] for i in PAGES[4])
        check("wrong credentials: exactly ONE login attempt for 6 cameras (no lockout burst)",
              attempts == 1, attempts)
        check("NVR paused and all its tiles say 'NVR login failed'",
              server._nvr_auth_paused("nvr1") and all(S[i].status == server.S_LOGIN for i in PAGES[4]),
              [S[i].status for i in PAGES[4]])
        check("no OpenCV opens while credentials are rejected",
              sum(OPEN_COUNT.get(i, 0) for i in PAGES[4]) == opens_before)
    finally:
        drain()
        server._AUTH_PAUSED_TIL["nvr1"] = 0.0
        server._AUTH_OK["nvr1"] = True


def test_slot_wait_reporting_and_handoff():
    extra = 6                               # 7th NVR2 camera while page 1 is open
    start(PAGES[1])
    wait(lambda: live(PAGES[1]))
    start([extra])
    check("7th NVR2 camera waits for a slot",
          wait(lambda: S[extra].status == server.S_WAIT_SLOT, 3.0), S[extra].status)
    d = server.system_status()["nvrs"]["nvr2"]
    check("/api/status names the waiting camera and all 6 owners",
          d["waiting"] == 1 and d["waitingCameras"][0]["index"] == extra and
          sorted(o["index"] for o in d["owners"]) == PAGES[1], d)
    stop([PAGES[1][0]])                     # one page-1 viewer leaves ...
    check("... and the waiting camera gets that slot and goes live", wait(lambda: live([extra]), 3.0))
    drain()


def test_frame_max_age():
    orig = server.FRAME_MAX_AGE_S
    server.FRAME_MAX_AGE_S = 0.3
    try:
        start([3])
        wait(lambda: live([3]))
        stop([3])
        check("last frame retained briefly after the viewer left (hand-off)", S[3].jpeg() is not None)
        time.sleep(0.45)
        check("stale frame is NOT served after the max age (never frozen forever)", S[3].jpeg() is None)
    finally:
        server.FRAME_MAX_AGE_S = orig
        drain()


def test_status_api_safe_and_complete():
    start([0])
    wait(lambda: live([0]))
    d = server.system_status()
    blob = json.dumps(d)
    secrets = [v for n in server.NVRS.values() for v in (n["user"], n["pass"]) if v and len(v) >= 4]
    check("/api/status contains no NVR usernames/passwords", not any(s in blob for s in secrets))
    need_n = {"active", "max", "waiting", "owners"}
    need_c = {"index", "name", "nvr", "channel", "status", "viewers", "hasFrame", "slotHeld",
              "slotWaitMs", "lastFrameAgeMs", "lastErrorMasked"}
    check("/api/status NVR fields present", all(need_n <= set(v) for v in d["nvrs"].values()))
    check("/api/status camera fields present", all(need_c <= set(c) for c in d["cameras"]))
    c0 = d["cameras"][0]
    check("live camera reports hasFrame + slotHeld + startup timing",
          c0["hasFrame"] and c0["slotHeld"] and c0["startup"] and "total_ms" in c0["startup"], c0)
    drain()


def test_page_js_retry_url_works_without_key():
    page = server.PAGE
    check("JS retry URL no longer appends '&_r=' to a key-less URL", "streamUrl(i) + '&_r='" not in page)
    check("JS retryUrl() picks '?' or '&'", "(q ? '&' : '?')" in page)
    idx = server.Handler._index
    check("server parses '/stream/5' (retry URL path without ?key=)", idx(None, "/stream/5", "/stream/") == 5)
    check("old buggy form '/stream/5&_r=1' is indeed rejected", idx(None, "/stream/5&_r=1", "/stream/") is None)


def test_page_js_releases_streams_on_unload():
    page = server.PAGE
    check("page aborts all MJPEG streams on beforeunload (reload would otherwise hang)",
          "addEventListener('beforeunload', stopAllStreams)" in page)
    check("page aborts all MJPEG streams on pagehide (mobile)", "addEventListener('pagehide', stopAllStreams)" in page)
    body = page.split("function stopAllStreams(){", 1)[1].split("}", 2)
    check("stopAllStreams clears every grid cell and the fullscreen <img>",
          "cells.forEach(c => c.img.removeAttribute('src'))" in page and "live.removeAttribute('src')" in body[0] + body[1])
    check("grid stream <img> requested at high priority (not throttled as deferrable)",
          "<img fetchpriority=high>" in page)
    check("fullscreen stream <img> requested at high priority", "<img id=live fetchpriority=high>" in page)


if __name__ == "__main__":
    t0 = time.time()
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n-- {name}")
            fn()
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}  ({time.time() - t0:.1f}s)")
    sys.exit(1 if FAILS else 0)
