"""Live-stream stability tests -- offline (cv2 + RTSP pre-flight stubbed per camera).

The fake capture can: jitter (pause between frames), stall once (connection open, no
data), time out (block for the read timeout, then fail) or end the stream (EOF), and
open slowly. Every worker transition must carry its exact REASON.

Covers: an active (viewed) worker is never preempted; a background worker yields at
once; a fullscreen (pinned) stream survives background churn; a Standard -> Original
switch releases the duplicate Standard upstream; an Original -> Standard switch is
make-before-break (the Standard viewer never sees CACHED) and leaves no duplicate;
jitter below the stale threshold never shows CACHED; a real stall is logged as
STALLED (+ why) and recovers without reconnect; a genuine stream loss falls back to the
CACHED view, is classified (READ_TIMEOUT / STREAM_CLOSED) and reconnects to LIVE; a slow
browser never slows the shared worker; two Original viewers of one camera share one
upstream and the second leaving changes nothing; a lingering Original is re-used; the
Original encoder runs off the capture thread; /api/status shows slots, priorities,
pinning and reasons without credentials; and (subprocess, on-demand mode) a 0-viewer
worker yields its slot to a waiting viewer at once.

Run:  python test_live_stability.py        (exits non-zero on any failure)
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
import subprocess

ONDEMAND = "--ondemand" in sys.argv
os.environ.update({
    "CCTV_PERSISTENT": "0" if ONDEMAND else "1", "CCTV_PREFLIGHT": "1", "CCTV_NVR_MAX_CONN": "6",
    "CCTV_LOG_EVENTS": os.environ.get("STAB_TEST_LOG", "0"),
    "CCTV_SETTINGS_FILE": os.path.join(tempfile.mkdtemp(), "camera-settings.json"),
    "CCTV_WARM_STEP_S": "0.05", "CCTV_MIN_BG_HOT_S": "0.3", "CCTV_BG_SWAP_S": "0.2",
    "CCTV_DEAD_RETRY_S": "2", "CCTV_REFRESH_EVERY_S": "0", "CCTV_IDLE_FPS": "1", "CCTV_STREAM_FPS": "8",
    "CCTV_ORIGINAL_FPS": "12", "CCTV_READ_TIMEOUT_MS": "4000", "CCTV_LIVE_MAX_AGE_S": "2.5",
    "CCTV_ORIGINAL_LINGER_S": "2.0", "CCTV_HANDOFF_MAX_S": "60",
})
for k in ("CCTV_NVR1_MAX_CONN", "CCTV_NVR2_MAX_CONN", "CCTV_ORIGINAL_MAX_W", "CCTV_ORIGINAL_GRID_MAX_W",
          "CCTV_ORIGINAL_READ_TIMEOUT_MS"):
    os.environ.pop(k, None)

import numpy as np                                     # noqa: E402

READ_TIMEOUT_S = 4.0
BEHAVE = {}             # (index, subtype) -> dict: pause_every/pause_s (jitter), stall_s (once),
                        #   eof_after (once), open_delay
OPENS, CONC, MAXCONC = {}, {}, {}
_LOCK = threading.Lock()
URL2IDX = {}
SLOW_ENCODE = {"ms": 0}


class _Cap:
    def __init__(self, url, *a, **k):
        m = re.search(r"@([^/]+)/cam/realmonitor\?channel=(\d+)&subtype=(\d+)", url)
        self.idx = URL2IDX.get((m.group(1), int(m.group(2)))) if m else None
        self.sub = int(m.group(3)) if m else None
        self.key = (self.idx, self.sub)
        b = BEHAVE.get(self.key, {})
        time.sleep(0.03 + b.get("open_delay", 0.0))
        w, h = (1920, 1080) if self.sub == 0 else (352, 288)
        self.frame = np.zeros((h, w, 3), dtype="uint8")
        self.n, self.released = 0, False
        self.stall_s = b.pop("stall_s", 0.0)             # once per connection that sees it
        self.stall_at = b.pop("stall_at", 20)
        self.eof_after = b.pop("eof_after", None)
        self.pause_every, self.pause_s = b.get("pause_every"), b.get("pause_s", 0.0)
        with _LOCK:
            OPENS[self.key] = OPENS.get(self.key, 0) + 1
            CONC[self.key] = CONC.get(self.key, 0) + 1
            MAXCONC[self.key] = max(MAXCONC.get(self.key, 0), CONC[self.key])

    def isOpened(self):
        return True

    def grab(self):
        self.n += 1
        if self.eof_after is not None and self.n > self.eof_after:
            return False                                  # the NVR ended the stream
        if self.stall_s and self.n == self.stall_at:
            if self.stall_s >= READ_TIMEOUT_S:            # read timeout: blocks, then fails
                time.sleep(READ_TIMEOUT_S)
                return False
            time.sleep(self.stall_s)                      # connection open, no data for a while
        if self.pause_every and self.n % self.pause_every == 0:
            time.sleep(self.pause_s)
        time.sleep(0.02)                                  # 50 fps source
        return True

    def retrieve(self):
        return True, self.frame

    def read(self):
        return (True, self.frame) if self.grab() else (False, None)

    def release(self):
        with _LOCK:
            if not self.released:
                self.released = True
                CONC[self.key] -= 1


def _imencode(ext, frame, params=None):
    if SLOW_ENCODE["ms"] and frame.shape[1] >= 1920:
        time.sleep(SLOW_ENCODE["ms"] / 1000.0)
    q = params[1] if params else 95
    return True, memoryview(f"jpeg-{frame.shape[1]}x{frame.shape[0]}-q{q}".encode())


cv2 = types.ModuleType("cv2")
cv2.CAP_FFMPEG = 0
cv2.IMWRITE_JPEG_QUALITY = 1
cv2.FONT_HERSHEY_SIMPLEX = 0
cv2.LINE_AA = 16
cv2.INTER_AREA = 3
cv2.VideoCapture = _Cap
cv2.resize = lambda frame, size, *a, **k: np.zeros((size[1], size[0], 3), dtype="uint8")
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


server._new_preflight = lambda nvr, channel, alive, subtype=1: _FakePreflight(alive)
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
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""), flush=True)
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


def reasons(w, since=0.0):
    return [(e["to"], e["reason"]) for e in list(w.transitions) if e["t"] >= since]


class Viewer(threading.Thread):
    """One browser <img>: records every MJPEG part (seconds, X-Frame-State, body)."""

    def __init__(self, port, path, read=True):
        super().__init__(daemon=True)
        self.port, self.path, self.parts, self.sock, self.stop, self.read = port, path, [], None, False, read
        self.start()

    def run(self):
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=30)
            if not self.read:                             # a stuck browser: tiny window, never reads
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            self.sock = s
            t0 = time.monotonic()
            s.sendall(f"GET {self.path} HTTP/1.1\r\nHost: t\r\n\r\n".encode())
            if not self.read:
                while not self.stop:
                    time.sleep(0.1)
                return
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
                    self.parts.append((time.monotonic() - t0, st.group(1) if st else None,
                                       buf[m.end():m.end() + n]))
                    buf = buf[m.end() + n:]
        except OSError:
            pass

    def states(self, after=0.0):
        return [p[1] for p in self.parts if p[0] >= after]

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


def settle(timeout=8.0):
    wait(lambda: not any(o._running for o in O), 5.0)
    return wait(lambda: all(len([s for s in S if s.info["nvr"] == k and s._bg and s._running and s.live_since])
                            >= min(6, len([s for s in S if s.info["nvr"] == k])) - 1 for k in server.NVRS), timeout)


# ── persistent-mode tests ──────────────────────────────────────────────────────────
def test_viewed_worker_is_never_preempted(port):
    settle()
    cams = NVR2[:6]
    vs = [Viewer(port, url(i)) for i in cams]                      # 6 active grid viewers, cap 6
    wait(lambda: all(S[i].tier() == "HOT" and S[i].viewers == 1 for i in cams), 6.0)
    t0 = time.monotonic()
    o0 = {i: OPENS.get((i, 1), 0) for i in cams}
    extra = [Viewer(port, url(NVR2[7])), Viewer(port, url(NVR2[8], "original", "prio=full"))]
    for _ in range(6):                                            # background churn on the same NVR
        for s in S:
            if s.info["nvr"] == "nvr2" and s.viewers == 0:
                s.start_bg("background")
        time.sleep(0.25)
    time.sleep(1.0)
    check("6 viewed tiles on a full NVR: every one stays LIVE through background churn and 2 waiting viewers",
          all(S[i].tier() == "HOT" and S[i].vstate == "LIVE" for i in cams)
          and all(OPENS.get((i, 1), 0) == o0[i] for i in cams),
          {i: (S[i].vstate, OPENS.get((i, 1), 0) - o0[i]) for i in cams})
    check("... none of them logged a transition (no preemption, no reconnect)",
          all(not reasons(S[i], t0) for i in cams), {i: reasons(S[i], t0) for i in cams})
    check("... the waiting fullscreen Original says so: WAITING_SLOT reason=NVR_FULL",
          ("WAITING_SLOT", "NVR_FULL") in reasons(O[NVR2[8]], t0), reasons(O[NVR2[8]], t0))
    check("... the extra background starts never took a slot (cap respected)", PEAK["nvr2"] <= CAP["nvr2"], PEAK)
    for v in vs + extra:
        v.close()
    wait(lambda: not O[NVR2[8]]._running, 5.0)


def test_background_worker_yields_at_once(port):
    settle()
    bg = [s for s in S if s.info["nvr"] == "nvr1" and s._bg and s._running and s.live_since]
    victim_pool = {s.index for s in bg}
    target = next(i for i in NVR1 if i not in victim_pool and not S[i]._running)
    t0 = time.monotonic()
    v = Viewer(port, url(target, extra="prio=full"))
    ok = wait(lambda: S[target].tier() == "HOT", 5.0)
    dt = time.monotonic() - t0
    wait(lambda: any(e["t"] >= t0 and e["reason"] == "POOL_DEMOTION" for s in S if s.index in victim_pool
                     for e in list(s.transitions)), 2.0)
    demoted = [s for s in S if s.index in victim_pool and any(e["t"] >= t0 and e["reason"] == "POOL_DEMOTION"
                                                               for e in list(s.transitions))]
    check(f"a viewer needing a slot: a background worker (0 viewers) yields at once -> viewer LIVE in {dt:.2f}s",
          ok and dt < 1.5 and len(demoted) >= 1, (ok, round(dt, 2), len(demoted)))
    check("... the yielded worker is logged LIVE -> WARM reason=POOL_DEMOTION (not an error / reconnect)",
          demoted and demoted[0].transitions[-1]["to"] == "WARM" and demoted[0].reconnects == 0,
          [reasons(d, t0) for d in demoted[:1]])
    v.close()


def test_fullscreen_pinned_survives_background_churn(port):
    settle()
    i = NVR1[1]
    t0 = time.monotonic()
    full = Viewer(port, url(i, "original", "prio=full"))
    wait(lambda: O[i].tier() == "HOT", 5.0)
    o0 = OPENS.get((i, 0), 0)
    check("fullscreen Original is PINNED with the top priority",
          O[i].pinned and O[i].priority_name() == "FULLSCREEN_ORIGINAL", (O[i].pinned, O[i].priority_name()))
    server.REFRESH_EVERY_S, server.REFRESH_IDLE_S, server.REFRESH_SWEEP_GAP_S = 0.5, 0.2, 0.2
    try:
        others = []
        for k in range(8):                              # grid viewers on the same NVR come and go
            others.append(Viewer(port, url(NVR1[2 + k % 6])))
            time.sleep(0.3)
            if len(others) > 3:
                others.pop(0).close()
        for s in S:
            if s.info["nvr"] == "nvr1" and s.viewers == 0:
                s.start_bg("background")
        time.sleep(2.0)
    finally:
        server.REFRESH_EVERY_S = 0
        for o in others:
            o.close()
    tr = reasons(O[i], t0)
    check("... it stays LIVE through background promotions and grid churn: 0 reconnects, no new open",
          O[i].vstate == "LIVE" and O[i].reconnects == 0 and OPENS.get((i, 0), 0) == o0, (O[i].vstate, tr))
    check("... its only transition is the first frame", tr == [("LIVE", "FIRST_FRAME")], tr)
    check("... and the viewer only ever got LIVE frames", set(full.states(0.5)) == {"live"}, set(full.states(0.5)))
    full.close()


def test_standard_to_original_releases_the_duplicate(port):
    settle()
    i = NVR2[9]                                          # not in the background pool
    std = Viewer(port, url(i, extra="prio=full"))
    wait(lambda: S[i].tier() == "HOT" and S[i].viewers == 1, 5.0)
    t0 = time.monotonic()
    BEHAVE[(i, 0)] = {"open_delay": 0.6}                  # the main stream takes a moment
    orig = Viewer(port, url(i, "original", "prio=full"))  # the page swaps the src ...
    time.sleep(0.05)
    std.close()                                          # ... and the old request ends
    ok = wait(lambda: O[i].is_live(), 5.0)
    released = wait(lambda: not S[i]._running and S[i].slot_key not in owners("nvr2"), 3.0)
    check("Standard -> Original: while the main stream starts the viewer sees the LIVE Standard picture",
          "standard" in orig.states() and "cached" not in orig.states(), sorted(set(orig.states())))
    check("... then Original; the Standard upstream of this camera is released (no duplicate)",
          ok and released, (ok, S[i]._running, sorted(owners("nvr2"))))
    tr = [r for t, r in reasons(S[i], t0)]
    check("... logged as the end of the switch (HANDOFF_DONE / DUPLICATE), not as an error",
          any(r.startswith("HANDOFF_DONE") or r.startswith("POOL_DEMOTION") for r in tr), reasons(S[i], t0))
    check("... exactly one slot for this camera afterwards",
          sum(1 for k in owners("nvr2") if k % 1000 == i) == 1, sorted(owners("nvr2")))
    orig.close()
    BEHAVE.pop((i, 0), None)
    wait(lambda: not O[i]._running, 5.0)


def test_original_to_standard_is_make_before_break(port):
    settle()
    i = NVR2[10]
    wait(lambda: not S[i]._running, 5.0)
    orig = Viewer(port, url(i, "original", "prio=full"))
    wait(lambda: O[i].is_live(), 5.0)
    wait(lambda: not S[i]._running, 5.0)                 # its Standard stream is not running
    BEHAVE[(i, 1)] = {"open_delay": 1.0}                  # the Standard stream takes 1 s to start
    t0 = time.monotonic()
    std = Viewer(port, url(i, extra="prio=full"))
    time.sleep(0.05)
    orig.close()
    ok = wait(lambda: S[i].is_live(), 5.0)
    gone = wait(lambda: not O[i]._running and O[i].slot_key not in owners("nvr2"), 3.0)
    st = std.states()
    check("Original -> Standard: the viewer never sees CACHED -- the Original keeps running as the bridge",
          ok and st and set(st) == {"live"}, sorted(set(st)))
    check("... and is released once Standard is live: no duplicate main + sub slot",
          gone and sum(1 for k in owners("nvr2") if k % 1000 == i) == 1, sorted(owners("nvr2")))
    check("... logged reason HANDOFF_DONE", any(r.startswith("HANDOFF_DONE") for t, r in reasons(O[i], t0)),
          reasons(O[i], t0))
    std.close()
    BEHAVE.pop((i, 1), None)


def test_jitter_is_not_stale_but_a_real_stall_is_logged(port):
    i = NVR1[3]
    BEHAVE[(i, 0)] = {"pause_every": 15, "pause_s": 1.5}  # 1.5 s gaps now and then (jitter)
    v = Viewer(port, url(i, "original", "prio=full"))
    wait(lambda: O[i].is_live(), 5.0)
    t0 = time.monotonic()
    time.sleep(4.0)
    check("1.5 s frame gaps (below the 2.5 s stale threshold): viewer only gets LIVE, no STALLED logged",
          set(v.states(1.0)) == {"live"} and not any(r[0] == "STALLED" for r in reasons(O[i], t0)),
          (sorted(set(v.states(1.0))), reasons(O[i], t0)))
    check("... and the gaps are measured (maxFrameGapMs)", O[i].max_gap_ms >= 1400, O[i].max_gap_ms)
    v.close()
    wait(lambda: not O[i]._running, 5.0)
    BEHAVE[(i, 0)] = {"stall_s": 3.2, "stall_at": 30}     # one 3.2 s stall, connection stays open
    t0 = time.monotonic()
    r0 = O[i].reconnects
    v = Viewer(port, url(i, "original", "prio=full"))
    wait(lambda: any(r[1] == "FRAME_RESUMED" for r in reasons(O[i], t0)), 8.0)
    tr = [e for e in list(O[i].transitions) if e["t"] >= t0]
    stall = next((e for e in tr if e["to"] == "STALLED"), None)
    check("a 3.2 s stall: LIVE -> STALLED reason=NO_FRAME_AGE_<ms> with the cause (waiting for data)",
          stall is not None and stall["reason"].startswith("NO_FRAME_AGE_") and "waiting for data" in stall["detail"],
          stall)
    check("... then STALLED -> LIVE reason=FRAME_RESUMED on the same connection (no reconnect)",
          any(e["reason"] == "FRAME_RESUMED" for e in tr) and O[i].reconnects == r0 and O[i].stalls >= 1,
          [(e["to"], e["reason"]) for e in tr])
    wait(lambda: v.states()[-1:] == ["live"], 2.0)
    check("... the viewer saw the CACHED view only during the stall, LIVE again after",
          "cached" in v.states() and v.states()[-1] == "live", sorted(set(v.states())))
    v.close()
    BEHAVE.pop((i, 0), None)
    wait(lambda: not O[i]._running, 5.0)


def fresh_camera(nvr_cams):
    """A camera the pool does not run (and will not start meanwhile): its next
    connection is a new one, so the fake behaviour applies."""
    i = next(c for c in nvr_cams if not S[c]._running and not S[c].viewers and not S[c]._bg)
    server.POOL.block_until[i] = time.monotonic() + 120
    wait(lambda: not S[i]._running and S[i]._worker_done.is_set(), 5.0)
    return i


def test_genuine_loss_falls_back_to_cache_and_reconnects(port):
    i = fresh_camera(NVR1)
    BEHAVE[(i, 1)] = {"eof_after": 40}                    # the NVR ends the stream once
    t0 = time.monotonic()
    r0 = S[i].reconnects
    v = Viewer(port, url(i, extra="prio=full"))
    ok = wait(lambda: S[i].reconnects >= r0 + 1 and S[i].is_live(), 8.0)
    BEHAVE[(i, 1)] = {"open_delay": 0.0}
    tr = reasons(S[i], t0)
    check("stream ended by the NVR: LIVE -> RECONNECTING reason=STREAM_CLOSED", ("RECONNECTING", "STREAM_CLOSED") in tr, tr)
    check("... reconnect restores LIVE (reason=RECONNECTED)", ok and ("LIVE", "RECONNECTED") in tr, tr)
    check("... dropReasons counts it", S[i].drop_reasons.get("STREAM_CLOSED", 0) >= 1, dict(S[i].drop_reasons))
    v.close()
    wait(lambda: not S[i]._running, 5.0)
    i = fresh_camera(NVR1)
    BEHAVE[(i, 1)] = {"stall_s": 5.0, "stall_at": 60}      # blocks past the 4 s read timeout
    t0 = time.monotonic()
    r0 = S[i].reconnects
    v = Viewer(port, url(i, extra="prio=full"))
    ok = wait(lambda: S[i].reconnects >= r0 + 1 and S[i].is_live(), 12.0)
    tr = reasons(S[i], t0)
    check("no data for the read timeout: STALLED first, then RECONNECTING reason=READ_TIMEOUT, then LIVE",
          ok and [t for t, r in tr][:4] == ["LIVE", "STALLED", "RECONNECTING", "LIVE"]
          and ("RECONNECTING", "READ_TIMEOUT") in tr, tr)
    wait(lambda: v.states()[-1:] == ["live"], 3.0)
    check("... meanwhile the viewer got the CACHED view (fallback), never a blank",
          "cached" in v.states() and v.states()[-1] == "live", sorted(set(v.states())))
    st = server.stream_info(i)["standard"]
    check("... /api/stream-info names the last transition reason", st["lastTransitionReason"] in ("RECONNECTED",)
          and any(e["reason"] == "READ_TIMEOUT" for e in st["transitions"]), st["lastTransitionReason"])
    v.close()
    BEHAVE.pop((i, 1), None)


def test_slow_browser_never_slows_the_worker(port):
    i = NVR1[6]
    stuck = Viewer(port, url(i, "original", "prio=full"), read=False)   # never reads a byte
    good = Viewer(port, url(i, "original", "prio=full"))
    wait(lambda: O[i].is_live(), 5.0)
    time.sleep(0.5)
    p0, n0 = O[i].published, len(good.parts)
    time.sleep(3.0)
    dp, dn = O[i].published - p0, len(good.parts) - n0
    check("a browser that stops reading: the shared worker keeps publishing ~12 fps, the other viewer ~12 fps",
          dp >= 30 and dn >= 28 and O[i].vstate == "LIVE", (dp, dn, O[i].vstate))
    stuck.close()
    good.close()
    wait(lambda: not O[i]._running, 5.0)


def test_two_original_viewers_one_upstream(port):
    i = NVR1[7]
    a = Viewer(port, url(i, "original", "prio=full"))
    wait(lambda: O[i].is_live(), 5.0)
    b = Viewer(port, url(i, "original"))
    time.sleep(1.0)
    o0, t0 = OPENS.get((i, 0), 0), time.monotonic()
    check("browser A fullscreen + browser B: ONE Original upstream, 2 viewers",
          O[i].viewers == 2 and MAXCONC.get((i, 0)) == 1, (O[i].viewers, MAXCONC.get((i, 0))))
    b.close()
    time.sleep(1.5)
    n = len(a.parts)
    time.sleep(1.0)
    check("B leaves: A keeps the same worker -- no reconnect, no CACHED, frames keep coming",
          OPENS.get((i, 0), 0) == o0 and not reasons(O[i], t0) and len(a.parts) - n >= 8
          and "cached" not in a.states(), (OPENS.get((i, 0), 0) - o0, reasons(O[i], t0)))
    a.close()


def test_lingering_original_is_reused(port):
    i = NVR1[8]
    v = Viewer(port, url(i, "original", "prio=full"))
    wait(lambda: O[i].is_live(), 5.0)
    o0 = OPENS.get((i, 0), 0)
    v.close()
    time.sleep(0.6)
    check("last Original viewer left: worker lingers (0 viewers, preemptable) instead of stopping",
          O[i]._running and O[i].bg_reason == "linger" and O[i].slot_priority() == server.PRIO_LINGER,
          (O[i]._running, O[i].bg_reason))
    v = Viewer(port, url(i, "original", "prio=full"))
    ok = wait(lambda: v.states()[:1] == ["live"], 2.0)
    check("... reopened within the linger: first frame LIVE at once, no new RTSP open",
          ok and OPENS.get((i, 0), 0) == o0, (v.states()[:2], OPENS.get((i, 0), 0) - o0))
    v.close()
    gone = wait(lambda: O[i].transitions[-1]["to"] == "STOPPED", 5.0)
    check("... and without a viewer it stops after the linger (reason LINGER_EXPIRED)",
          gone and O[i].transitions[-1]["reason"].startswith("LINGER_EXPIRED"), O[i].transitions[-1])


def test_encoder_runs_off_the_capture_thread(port):
    i = NVR1[9]
    SLOW_ENCODE["ms"] = 120                               # a slow 1080p encode (~8 fps max)
    try:
        v = Viewer(port, url(i, "original", "prio=full"))
        wait(lambda: O[i].is_live(), 5.0)
        time.sleep(3.0)
        check("slow JPEG encoder: the capture loop still reads the stream at the source rate (~50 fps)",
              (O[i].grab_fps or 0) >= 40, O[i].grab_fps)
        check("... the encoder simply publishes fewer frames (newest wins, counted), no stall, no reconnect",
              O[i].enc_drops > 0 and O[i].vstate == "LIVE" and O[i].reconnects == 0 and O[i].stalls == 0,
              (O[i].enc_drops, O[i].vstate, O[i].reconnects, O[i].stalls))
        v.close()
    finally:
        SLOW_ENCODE["ms"] = 0
    wait(lambda: not O[i]._running, 5.0)


def test_status_slots_priorities_reasons(port):
    i = NVR1[10]
    v = Viewer(port, url(i, "original", "prio=full"))
    wait(lambda: O[i].is_live(), 5.0)
    d = server.system_status()
    n1 = d["nvrs"]["nvr1"]
    slot = next((x for x in n1["slots"] if x["index"] == i and x["quality"] == "ORIGINAL"), None)
    check("/api/status: per NVR 'active/max' + a slot table (camera, quality, viewers, priority, pinned, state, age)",
          n1["slotsText"].endswith(f"/{CAP['nvr1']}") and slot and slot["priorityName"] == "FULLSCREEN_ORIGINAL"
          and slot["pinned"] and slot["state"] == "LIVE" and slot["viewers"] == 1 and not slot["preemptable"]
          and slot["slotAgeMs"] >= 0, slot)
    bgs = [x for x in n1["slots"] if x["viewers"] == 0]
    check("... background slots are marked preemptable with a background priority",
          all(x["preemptable"] and x["priority"] < server.PRIO_GRID_STANDARD for x in bgs), bgs[:2])
    c = d["cameras"][i]["original"]
    keys = {"priority", "priorityName", "pinned", "state", "slotHeld", "slotAgeMs", "lastFrameAgeMs",
            "reconnectCount", "cachedReason", "lastTransitionReason", "transitions", "maxFrameGapMs", "stalls"}
    check("... per camera + quality: every stability field", keys <= set(c), sorted(keys - set(c)))
    blob = json.dumps(d)
    secrets = [x for n in server.NVRS.values() for x in (n["user"], n["pass"]) if x and len(x) >= 4]
    check("... no credentials anywhere", not any(x in blob for x in secrets))
    v.close()


# ── on-demand mode (subprocess) ────────────────────────────────────────────────────
def ondemand_tests(port):
    i, j = NVR1[0], NVR1[1]
    v = Viewer(port, url(i, "original", "prio=full"))
    wait(lambda: O[i].is_live(), 5.0)
    v.close()
    time.sleep(0.3)
    held = O[i].slot_key in owners("nvr1")
    others = [Viewer(port, url(k)) for k in NVR1[2:7]]           # 5 more viewers: NVR1 is full
    wait(lambda: all(S[k].is_live() for k in NVR1[2:7]), 8.0)
    t0 = time.monotonic()
    w = Viewer(port, url(j, extra="prio=full"))                  # a 7th viewer needs a slot
    ok = wait(lambda: S[j].is_live(), 5.0)
    dt = time.monotonic() - t0
    check("[on-demand] a lingering Original (0 viewers) holds a slot ...", held)
    check(f"[on-demand] ... and yields it at once to a waiting viewer ({dt:.2f}s), reason=PREEMPTED",
          ok and dt < 2.0 and O[i].transitions[-1]["reason"].startswith("PREEMPTED"),
          (ok, round(dt, 2), O[i].transitions[-1]["reason"] if O[i].transitions else None))
    check("[on-demand] viewed streams were never preempted", all(S[k].reconnects == 0 and S[k].is_live()
                                                                  for k in NVR1[2:7]))
    for x in others + [w]:
        x.close()


if __name__ == "__main__":
    t_start = time.time()
    threading.Thread(target=_sampler, daemon=True).start()
    srv = server.QuietServer(("127.0.0.1", 0), server.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if ONDEMAND:
        ondemand_tests(port)
        _RUN["on"] = False
        print("ONDEMAND " + ("OK" if not FAILS else "FAILED"), flush=True)
        sys.exit(1 if FAILS else 0)
    server.POOL.start()
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n-- {name}", flush=True)
            fn(port)
    print("\n-- on-demand mode (subprocess)", flush=True)
    r = subprocess.run([sys.executable, __file__, "--ondemand"], capture_output=True, text=True, timeout=120)
    for line in r.stdout.splitlines():
        if line.startswith(("PASS", "FAIL")):
            print(line)
            if line.startswith("FAIL"):
                FAILS.append(line[5:])
    if r.returncode != 0 and not any(l.startswith("FAIL") for l in r.stdout.splitlines()):
        FAILS.append("on-demand subprocess crashed: " + (r.stderr or "")[-300:])
    _RUN["on"] = False
    srv.shutdown()
    print(f"\npeak NVR slots used: {PEAK} (cap {dict(CAP)})")
    check(f"per-NVR cap never exceeded (peak {PEAK})", all(PEAK[k] <= CAP[k] for k in server.NVRS), PEAK)
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}  ({time.time() - t_start:.1f}s)")
    sys.exit(1 if FAILS else 0)
