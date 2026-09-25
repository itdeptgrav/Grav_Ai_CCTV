"""Persistent relay (PoolManager) tests -- offline: cv2 and the RTSP pre-flight are
stubbed per camera, so nothing connects to an NVR.

Covers: pool start-up (controlled warm-up, grid order), one upstream worker per
camera however many viewers, idle publish rate, cached frames never shown as live,
HOT/WARM/COLD/OFFLINE tiers, priority promotion (fullscreen > grid > background),
viewed streams never demoted, strict per-NVR cap during every transition, slot
release before back-off, offline background cameras giving their slot back,
automatic reconnect, page switches keeping the other NVR HOT, no churn while idle,
cache-refresh visits, the status API and the HTTP first frame.

Run:  python test_relay.py        (exits non-zero on any failure)
"""
import os
import re
import sys
import json
import time
import types
import random
import socket
import tempfile
import threading

os.environ["CCTV_PERSISTENT"] = "1"
os.environ["CCTV_PREFLIGHT"] = "1"                 # pre-flight ON (stubbed below)
os.environ["CCTV_LOG_EVENTS"] = os.environ.get("RELAY_TEST_LOG", "0")
os.environ["CCTV_NVR_MAX_CONN"] = "6"
os.environ["CCTV_SETTINGS_FILE"] = os.path.join(tempfile.mkdtemp(), "camera-settings.json")
# fast pool timings for the test (production defaults are seconds/minutes)
os.environ.update({"CCTV_WARM_STEP_S": "0.05", "CCTV_WARM_CONCURRENCY": "2", "CCTV_MIN_BG_HOT_S": "0.3",
                   "CCTV_BG_SWAP_S": "0.2", "CCTV_DEAD_RETRY_S": "2", "CCTV_REFRESH_EVERY_S": "0",
                   "CCTV_IDLE_FPS": "1", "CCTV_STREAM_FPS": "8"})
for k in ("CCTV_NVR1_MAX_CONN", "CCTV_NVR2_MAX_CONN"):
    os.environ.pop(k, None)

import numpy as np                                 # noqa: E402

BEHAVIOR = {}          # camera index -> "ok" | "fail" | "drop"
OPEN_COUNT = {}        # camera index -> OpenCV opens
CONCURRENT = {}        # camera index -> open captures right now
MAX_CONCURRENT = {}    # camera index -> highest simultaneous captures ever
_LOCK = threading.Lock()
URL2IDX = {}


class _Cap:
    def __init__(self, url, *a, **k):
        m = re.search(r"@([^/]+)/cam/realmonitor\?channel=(\d+)", url)
        self.idx = URL2IDX.get((m.group(1), int(m.group(2)))) if m else None
        time.sleep(0.03)
        self.mode = BEHAVIOR.get(self.idx, "ok")
        self.n = 0
        self.released = False
        with _LOCK:
            OPEN_COUNT[self.idx] = OPEN_COUNT.get(self.idx, 0) + 1
            CONCURRENT[self.idx] = CONCURRENT.get(self.idx, 0) + 1
            MAX_CONCURRENT[self.idx] = max(MAX_CONCURRENT.get(self.idx, 0), CONCURRENT[self.idx])

    def isOpened(self):
        return self.mode != "fail"

    def grab(self):
        time.sleep(0.02)                            # 50 fps source
        self.n += 1
        return not (self.mode == "drop" and self.n > 15)

    def retrieve(self):
        return True, np.zeros((8, 8, 3), dtype="uint8")

    def read(self):
        return (True, self.retrieve()[1]) if self.grab() else (False, None)

    def release(self):
        with _LOCK:
            if not self.released:
                self.released = True
                CONCURRENT[self.idx] -= 1


cv2 = types.ModuleType("cv2")
cv2.CAP_FFMPEG = 0
cv2.IMWRITE_JPEG_QUALITY = 1
cv2.FONT_HERSHEY_SIMPLEX = 0
cv2.LINE_AA = 16
cv2.VideoCapture = _Cap
cv2.resize = lambda frame, size: frame
cv2.imencode = lambda ext, frame, *a: (True, memoryview(b"jpegbytes"))
cv2.putText = lambda *a, **k: None
cv2.getTextSize = lambda text, font, scale, thick: ((int(len(text) * 20 * scale), int(22 * scale)), 5)
cv2.circle = lambda *a, **k: None
sys.modules["cv2"] = cv2

import server                                     # noqa: E402
import rtsp_preflight as rp                       # noqa: E402
from nvr_config import CAMERAS, endpoint          # noqa: E402

for _i, _c in enumerate(CAMERAS):
    _h, _p = endpoint(_c["nvr"])
    URL2IDX[(f"{_h}:{_p}", _c["channel"])] = _i

PF_BEHAVIOR = {}       # camera index -> "ok" | "dead"


class _FakePreflight:
    def __init__(self, idx, alive):
        self.idx, self.alive = idx, alive
        self.ms, self.detail = 0, ""

    def run(self):
        t0 = time.monotonic()
        mode = PF_BEHAVIOR.get(self.idx, "ok")
        end = t0 + (0.15 if mode == "dead" else 0.05)
        while time.monotonic() < end:
            if not self.alive():
                return rp.ABORTED
            time.sleep(0.01)
        self.ms = round((time.monotonic() - t0) * 1000)
        if mode == "dead":
            self.detail = "no live video (stub)"
            return rp.DEAD
        if mode == "unreach":
            self.detail = "TCP connect failed (stub)"
            return rp.UNREACHABLE
        return rp.OK

    def close(self):
        pass


server._new_preflight = lambda nvr, ch: None      # replaced below (signature has alive)
server._new_preflight = lambda nvr, channel, alive: _FakePreflight(
    next(i for i, c in enumerate(CAMERAS) if c["nvr"] == nvr and c["channel"] == channel), alive)
server._backoff_s = lambda f: min(0.1 * 2 ** (max(f, 1) - 1), 0.8)


class _Up:
    checked, reachable, label = time.time(), True, "NVR REACHABLE"


server.MONITOR.get = lambda nvr: _Up()
for _k in server.NVRS:
    server._AUTH_OK[_k] = True

S = server.STREAMS
NVR2 = [s.index for s in S if s.info["nvr"] == "nvr2"]      # 0..12
NVR1 = [s.index for s in S if s.info["nvr"] == "nvr1"]      # 13..24
DEAD = {6, 11, 12, 20}                                       # like the real site (NVR2 ch7/12/13, NVR1 ch10)
CAP = server.NVR_CAP

# ── helpers ──────────────────────────────────────────────────────────────
FAILS = []
PEAK = {"nvr1": 0, "nvr2": 0}
_SAMPLING = {"on": True}


def _sampler():
    """Background check of the STRICT cap during every test."""
    while _SAMPLING["on"]:
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


def hot(nvr):
    return {s.index for s in S if s.info["nvr"] == nvr and s.tier() == "HOT"}


def bg(nvr):
    return {s.index for s in S if s.info["nvr"] == nvr and s.viewers == 0 and s._bg and s._running}


def owners(nvr):
    with server._ACTIVE_LOCK:
        return set(server.NVR_OWNERS[nvr])


def pool_settled():
    """Background full (or no candidates left) and nothing starting."""
    for k in server.NVRS:
        b = [S[i] for i in bg(k)]
        if any(s.live_since is None for s in b):
            return False
    return True


GRID_BG = {"nvr2": set(NVR2[:6]), "nvr1": set(NVR1[:6])}


def settle(timeout=10.0):
    """Known start state: nobody watching, no 'recently viewed' ranking, the
    background back to the first 6 cameras of each NVR in grid order."""
    for s in S:
        while s.viewers:
            s.remove_viewer(full=s.viewers_full > 0)
        s.last_view_end = 0.0
    for k in server.NVRS:
        server.POOL.last_swap[k] = 0.0
    ok = wait(lambda: all(bg(k) == GRID_BG[k] for k in server.NVRS) and pool_settled(), timeout)
    if not ok:
        print(f"      (settle: nvr2 bg={sorted(bg('nvr2'))} nvr1 bg={sorted(bg('nvr1'))})")
    return ok


# ── tests ────────────────────────────────────────────────────────────────
def test_pool_warms_up_progressively_in_grid_order():
    for i in DEAD:
        PF_BEHAVIOR[i] = "dead"
    starting_peak = {"nvr1": 0, "nvr2": 0}
    stop = {"on": True}

    def watch():
        while stop["on"]:
            for k in server.NVRS:
                n = sum(1 for i in bg(k) if S[i].live_since is None)
                starting_peak[k] = max(starting_peak[k], n)
            time.sleep(0.002)
    t = threading.Thread(target=watch, daemon=True)
    t.start()
    t0 = time.monotonic()
    server.POOL.start()
    ok = wait(lambda: hot("nvr2") == set(NVR2[:6]) and hot("nvr1") == set(NVR1[:6]), 8.0)
    first = min((S[i].live_since or 1e18) for i in NVR2 + NVR1)
    stop["on"] = False
    check("server start: page-1-first cameras become HOT without any viewer (6 per NVR)", ok,
          (sorted(hot("nvr2")), sorted(hot("nvr1"))))
    check("warm-up is controlled: <= WARM_CONCURRENCY background starts at once per NVR",
          all(v <= server.WARM_CONCURRENCY for v in starting_peak.values()), starting_peak)
    check("warm-up really ran in parallel batches (not one by one)", max(starting_peak.values()) >= 2, starting_peak)
    check("each HOT camera connected exactly once", all(OPEN_COUNT.get(i) == 1 for i in NVR2[:6] + NVR1[:6]),
          {i: OPEN_COUNT.get(i) for i in NVR2[:6] + NVR1[:6]})
    print(f"      (first camera HOT {round((first - t0) * 1000) if first < 1e17 else '-'} ms after the pool started)")


def test_many_viewers_one_upstream():
    settle()
    c = S[0]
    opens = OPEN_COUNT.get(0)
    active = len(owners("nvr2"))
    states = []
    for _ in range(5):
        c.add_viewer()
        states.append(c.frame_for_viewer()[1])
    check("5 viewers on a HOT camera: every one gets a LIVE frame at once", states == ["live"] * 5, states)
    time.sleep(0.5)
    check("5 viewers -> still ONE upstream connection (no new open)", OPEN_COUNT.get(0) == opens, OPEN_COUNT.get(0))
    check("5 viewers -> still one slot for this camera, no extra slot", len(owners("nvr2")) == active)
    check("max simultaneous captures for this camera ever = 1", MAX_CONCURRENT.get(0) == 1, MAX_CONCURRENT.get(0))
    for _ in range(5):
        c.remove_viewer()
    time.sleep(0.6)
    check("all 5 viewers left: camera stays HOT as 'recent' (no disconnect)",
          c.tier() == "HOT" and c.role() == "recent" and OPEN_COUNT.get(0) == opens, (c.tier(), c.role()))


def test_idle_publish_rate():
    settle()
    c = S[1]
    n0 = c.published
    time.sleep(2.0)
    idle = c.published - n0
    c.add_viewer()
    n1 = c.published
    time.sleep(2.0)
    watched = c.published - n1
    c.remove_viewer()
    check(f"HOT camera nobody watches publishes ~{server.IDLE_FPS:g} fps (JPEG work saved)", 1 <= idle <= 4, idle)
    check(f"with a viewer it publishes ~{server.STREAM_FPS} fps at once", watched >= 12, watched)


def test_cached_frame_is_never_live():
    settle()
    c = S[2]
    check("HOT camera frame is live", c.frame_for_viewer()[1] == "live")
    orig = server.LIVE_MAX_AGE_S
    try:
        server.POOL.block_until[2] = time.monotonic() + 60   # keep it out of the pool meanwhile
        c.stop_bg()                                   # demote (as the pool would)
        wait(lambda: not c._running, 2.0)
        check("demoted camera keeps its cached frame in RAM (WARM)", c.tier() == "WARM", c.tier())
        check("a stopped upstream is never served as live, even with a fresh frame",
              c.frame_for_viewer()[1] == "cached")
        server.LIVE_MAX_AGE_S = 0.2
        time.sleep(0.3)
        data, state = c.frame_for_viewer()
        check("old frame -> served as CACHED (darkened + stamped), not live", state == "cached" and data)
        server.CACHE_MAX_AGE_S, keep = 0.25, server.CACHE_MAX_AGE_S
        time.sleep(0.1)
        check("too old -> status card, not the frame", c.frame_for_viewer()[1] == "status")
        server.CACHE_MAX_AGE_S = keep
    finally:
        server.LIVE_MAX_AGE_S = orig
        server.POOL.block_until.pop(2, None)
    check("pool puts it back into the background (swap after MIN_BG_HOT_S)",
          wait(lambda: S[2].tier() == "HOT", 6.0), S[2].tier())


def test_tiers():
    settle()
    check("never-captured camera outside the pool is COLD", S[9].tier() == "COLD", S[9].tier())
    check("a dead channel ends up OFFLINE (tried in the background, then dropped)",
          all(S[i].tier() in ("OFFLINE", "COLD") for i in DEAD), {i: S[i].tier() for i in DEAD})


def test_promotion_demotes_background_never_viewers():
    # NVR2 slots: 0..5 in the background. A viewer opens camera 7 (not HOT).
    settle()
    b0 = {i: OPEN_COUNT.get(i) for i in range(5)}
    t0 = time.monotonic()
    S[7].add_viewer()
    first_state = S[7].frame_for_viewer()[1]
    ok = wait(lambda: S[7].tier() == "HOT", 4.0)
    check("viewer on a non-HOT camera: it is promoted and goes live", ok, S[7].tier())
    print(f"      (COLD -> LIVE promotion {round((time.monotonic() - t0) * 1000)} ms with the stub NVR)")
    check("first thing the viewer got was the status card / cached view, never a fake live frame",
          first_state in ("status", "cached"), first_state)
    check("the LEAST useful background stream made room (camera 5, last in grid order)",
          not S[5]._running and all(S[i]._running for i in range(5)), [S[i]._running for i in range(6)])
    check("the other background streams were not reconnected", all(OPEN_COUNT.get(i) == b0[i] for i in range(5)))
    # view 0..4 too -> 6 viewed cameras hold every NVR2 slot
    for i in range(5):
        S[i].add_viewer()
    time.sleep(0.4)
    check("viewing HOT cameras needs no reconnect", all(OPEN_COUNT.get(i) == b0[i] for i in range(5)))
    S[8].add_viewer()                                  # 7th viewed NVR2 camera: grid
    check("7th viewed camera waits (all slots held by VIEWED streams)",
          wait(lambda: S[8].status == server.S_WAIT_SLOT, 3.0), S[8].status)
    check("no viewed stream was stopped to make room",
          all(S[i]._running and S[i].tier() == "HOT" for i in list(range(5)) + [7]))
    S[9].add_viewer()                                  # another grid waiter
    S[10].add_viewer(full=True)                        # fullscreen waiter (arrived last)
    time.sleep(1.2)
    S[0].remove_viewer()                               # one grid viewer leaves -> a slot frees up
    check("the freed slot goes to the FULLSCREEN camera first (before older grid waiters)",
          wait(lambda: S[10].tier() == "HOT", 4.0) and S[8].status == server.S_WAIT_SLOT,
          (S[10].tier(), S[8].status, S[9].status))
    for i in (1, 2, 3, 4, 7, 8, 9):
        S[i].remove_viewer()
    S[10].remove_viewer(full=True)
    check("after everyone leaves, the pool settles back to 6 background streams on NVR2",
          wait(lambda: len(bg("nvr2")) == 6 and pool_settled(), 6.0), sorted(bg("nvr2")))


def test_strict_cap_and_no_slot_in_backoff():
    settle()
    BEHAVIOR[3] = "drop"                                   # HOT background stream starts dropping
    S[3].stop_bg()                                         # (reconnect now so the new capture drops)
    bad = seen_retry = 0
    end = time.monotonic() + 2.5
    while time.monotonic() < end:
        if S[3].status == server.S_RETRYING:
            seen_retry += 1
            bad += 3 in owners("nvr2")
        time.sleep(0.003)
    BEHAVIOR.pop(3)
    check("a dropping HOT stream reconnects by itself", S[3].reconnects >= 1, S[3].reconnects)
    check("its slot is released before the retry back-off (never held while Retrying...)", bad == 0, bad)
    check("it comes back HOT", wait(lambda: S[3].tier() == "HOT", 4.0), S[3].tier())
    check(f"per-NVR cap never exceeded so far (peak {PEAK})", all(PEAK[k] <= CAP[k] for k in server.NVRS), PEAK)


def test_offline_background_camera_gives_slot_back():
    settle()
    PF_BEHAVIOR[4] = "dead"                                # a HOT background camera dies
    t = time.monotonic()
    S[4].stop_bg()                                         # force a reconnect, like a dropped stream
    ok = wait(lambda: server.POOL.blocked_for(4) > 0 and not S[4]._running, 6.0)
    check("offline background camera is dropped from the pool (retry later)", ok,
          (S[4].tier(), server.POOL.blocked_for(4)))
    check("its slot went to the next useful camera", wait(lambda: len(bg("nvr2")) == 6 and 4 not in bg("nvr2"), 4.0),
          sorted(bg("nvr2")))
    PF_BEHAVIOR.pop(4)
    check("after DEAD_RETRY_S it is retried in the background and recovers",
          wait(lambda: S[4].tier() == "HOT", 8.0), S[4].tier())
    print(f"      (offline -> recovered in {time.monotonic() - t:.1f} s with DEAD_RETRY_S={server.DEAD_RETRY_S:g})")


def test_nvr_outage_recovers_without_blocking():
    settle()
    hot1 = sorted(hot("nvr1"))
    class _Down:
        checked, reachable, label = time.time(), False, "NVR UNREACHABLE"
    server.MONITOR.get = lambda nvr: _Down() if nvr == "nvr1" else _Up()   # the monitor notices too
    for i in NVR1:                                   # NVR1 reboots / network drop
        PF_BEHAVIOR[i] = "unreach"
        BEHAVIOR[i] = "drop"
    for i in hot1:
        S[i].stop_bg()                               # (streams die)
    time.sleep(2.5)
    down = {i: S[i].status for i in hot1 if S[i]._running}
    check("during the outage the NVR1 workers report 'NVR unreachable'",
          down and all(st == server.S_NVR_DOWN for st in down.values()), down)
    check("... hold no NVR slot while it is down", not owners("nvr1"), owners("nvr1"))
    check("... and are NOT dropped from the pool as dead cameras",
          all(server.POOL.blocked_for(i) == 0 for i in NVR1), {i: server.POOL.blocked_for(i) for i in NVR1})
    for i in NVR1:                                   # NVR1 is back
        PF_BEHAVIOR.pop(i, None)
        BEHAVIOR.pop(i, None)
    server.MONITOR.get = lambda nvr: _Up()
    t0 = time.monotonic()
    ok = wait(lambda: hot("nvr1") == set(hot1), 6.0)
    check("NVR back -> the same cameras are HOT again by themselves", ok, sorted(hot("nvr1")))
    print(f"      (NVR1 pool back {round((time.monotonic() - t0) * 1000)} ms after the NVR returned)")


def test_worker_survives_unexpected_error():
    settle()
    c = S[15]
    opens = OPEN_COUNT.get(15, 0)
    orig = cv2.resize
    boom = {"n": 0}

    def bad_resize(frame, size):
        boom["n"] += 1
        raise RuntimeError("corrupt frame (test)")
    cv2.resize = bad_resize
    try:
        c.add_viewer()                              # publish at full rate -> hits the error at once
        wait(lambda: boom["n"] > 0, 3.0)
    finally:
        cv2.resize = orig
    check("an unexpected error in a worker does not kill it: it retries and recovers",
          wait(lambda: c.tier() == "HOT", 5.0) and OPEN_COUNT.get(15, 0) > opens, (c.tier(), c.status))
    check("... and its slot was released in between (no leak)",
          all(len(server.NVR_OWNERS[k]) == server.NVR_ACTIVE[k] <= CAP[k] for k in server.NVRS))
    c.remove_viewer()


def test_page_switch_keeps_other_nvr_hot():
    settle()
    page1 = NVR2[:6]
    page4 = [18, 19, 20, 21, 22, 23]                     # NVR1 ch8..13 (ch10 dead)
    for i in page1:
        S[i].add_viewer()
    wait(lambda: all(S[i].tier() == "HOT" for i in page1), 4.0)
    opens1 = {i: OPEN_COUNT.get(i) for i in page1}
    for i in page1:                                      # clearCells() -> page 4
        S[i].remove_viewer()
    time.sleep(0.5)
    t0 = time.monotonic()
    for i in page4:
        S[i].add_viewer()
    healthy4 = [i for i in page4 if i not in DEAD]
    ok = wait(lambda: all(S[i].tier() == "HOT" for i in healthy4), 5.0)
    check("page 4 (NVR1) goes live", ok, {i: S[i].tier() for i in page4})
    print(f"      (page 4 all live after {round((time.monotonic() - t0) * 1000)} ms; "
          f"{sum(1 for i in healthy4 if OPEN_COUNT.get(i, 0) == 1)} of them had to connect)")
    check("while on page 4, page 1 (other NVR) stays HOT in the background",
          all(S[i].tier() == "HOT" for i in page1), {i: S[i].tier() for i in page1})
    for i in page4:
        S[i].remove_viewer()
    time.sleep(0.5)
    states = []
    for i in page1:                                      # back to page 1
        S[i].add_viewer()
        states.append(S[i].frame_for_viewer()[1])
    check("back on page 1: every tile is LIVE immediately", states == ["live"] * 6, states)
    check("... without a single RTSP reconnect", all(OPEN_COUNT.get(i) == opens1[i] for i in page1),
          {i: (opens1[i], OPEN_COUNT.get(i)) for i in page1})
    for i in page1:
        S[i].remove_viewer()


def test_no_churn_while_idle():
    for i in DEAD:                                   # dead channels: next background retry far away
        server.POOL.block_until[i] = time.monotonic() + 120
    settle()
    time.sleep(0.5)
    p0, d0, o0 = server.POOL.promotions, server.POOL.demotions, sum(OPEN_COUNT.values())
    time.sleep(3.0)
    moves = (server.POOL.promotions - p0) + (server.POOL.demotions - d0)
    opens = sum(OPEN_COUNT.values()) - o0
    check("idle server: pool is stable (0 promotions/demotions in 3 s)", moves == 0, moves)
    check("idle server: 0 new upstream connections in 3 s", opens == 0, opens)


def test_refresh_visit_keeps_cache_fresh_without_breaking_cap():
    settle()
    targets = [i for i in NVR2 + NVR1 if i not in DEAD and S[i].jpeg_bytes is None]
    check("some healthy cameras outside the pool have no cached frame yet", len(targets) >= 2, targets)
    server.REFRESH_EVERY_S, server.REFRESH_SWEEP_GAP_S, server.REFRESH_IDLE_S = 1.0, 0.2, 0.0
    try:
        ok = wait(lambda: all(S[i].jpeg_bytes is not None for i in targets), 15.0)
        check("refresh visits captured a cached frame of EVERY camera outside the pool", ok,
              [i for i in targets if S[i].jpeg_bytes is None])
    finally:
        server.REFRESH_EVERY_S = 0.0
    check("visits ended: those cameras are WARM (cached, not connected)",
          wait(lambda: all(S[i].tier() == "WARM" for i in targets), 6.0), {i: S[i].tier() for i in targets})
    check("the borrowed slots came back (grid-order background restored)", settle(), sorted(bg("nvr2")))
    check(f"per-NVR cap never exceeded (peak {PEAK})", all(PEAK[k] <= CAP[k] for k in server.NVRS), PEAK)


def test_refresh_waits_for_idle_and_spares_recent_cameras():
    settle()
    for i in NVR2[:6]:                              # the whole NVR2 background was just viewed
        S[i].add_viewer()
    time.sleep(0.3)
    for i in NVR2[:6]:
        S[i].remove_viewer()
    # REFRESH_EVERY_S=0.5 makes every cached frame outside the pool "due" for a refresh
    server.REFRESH_EVERY_S, server.REFRESH_SWEEP_GAP_S, server.REFRESH_IDLE_S = 0.5, 0.1, 1.0
    visits = 0
    try:
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            visits += sum(1 for i in NVR2 if S[i].bg_reason == "refresh")
            time.sleep(0.01)
    finally:
        server.REFRESH_EVERY_S, server.REFRESH_IDLE_S = 0.0, 60.0
    check("no refresh visit takes the slot of a recently viewed camera", visits == 0, visits)
    check("the recently viewed cameras are all still HOT", all(S[i].tier() == "HOT" for i in NVR2[:6]),
          {i: S[i].tier() for i in NVR2[:6]})
    active_nvr2 = [i for i in NVR2 if S[i].viewers]
    check("(sanity) nobody is watching NVR2", not active_nvr2)


def test_worker_uniqueness_under_random_churn():
    rnd = random.Random(11)
    cam = S[14]
    for _ in range(120):
        r = rnd.random()
        if r < 0.3:
            cam.add_viewer()
        elif r < 0.6 and cam.viewers:
            cam.remove_viewer()
        elif r < 0.8:
            cam.start_bg("background")
        else:
            cam.stop_bg()
        time.sleep(rnd.uniform(0.0, 0.03))
    while cam.viewers:
        cam.remove_viewer()
    time.sleep(1.0)
    check("random viewer/pool churn: never two captures for one camera at once",
          MAX_CONCURRENT.get(14, 0) <= 1, MAX_CONCURRENT.get(14))
    check("never two slots for one camera (owners == active)",
          all(len(server.NVR_OWNERS[k]) == server.NVR_ACTIVE[k] for k in server.NVRS))
    check(f"per-NVR cap never exceeded (peak {PEAK})", all(PEAK[k] <= CAP[k] for k in server.NVRS), PEAK)


def test_status_api():
    d = server.system_status()
    blob = json.dumps(d)
    secrets = [v for n in server.NVRS.values() for v in (n["user"], n["pass"]) if v and len(v) >= 4]
    check("/api/status contains no NVR usernames/passwords", not any(s in blob for s in secrets))
    need = {"tier", "role", "viewersFull", "reconnects", "failStreak", "cachedFrameAgeMs", "sourceType",
            "slotPriority", "liveForMs", "slotHeld", "opens"}
    check("/api/status camera fields for the relay", all(need <= set(c) for c in d["cameras"]),
          need - set(d["cameras"][0]))
    check("/api/status NVR hot/background/cap counts",
          all({"hot", "background", "viewedCameras", "max"} <= set(v) for v in d["nvrs"].values()))
    check("/api/status pool + config", d["pool"]["enabled"] and d["pool"]["running"] and
          d["config"]["persistent"] and d["config"]["nvrCaps"] == {"nvr1": 6, "nvr2": 6}, d["pool"])


def _first_parts(port, path, n_parts, timeout=6.0):
    """Read the first multipart parts of an MJPEG stream -> [(ms, state)]."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    t0 = time.monotonic()
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: t\r\n\r\n".encode())
    buf, out = b"", []
    try:
        while len(out) < n_parts and time.monotonic() - t0 < timeout:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            for m in re.finditer(rb"X-Frame-State: (\w+)", buf):
                pass
            states = re.findall(rb"X-Frame-State: (\w+)", buf)
            while len(out) < len(states):
                out.append((round((time.monotonic() - t0) * 1000), states[len(out)].decode()))
    finally:
        s.close()
    return out


def test_http_first_frame():
    settle()
    srv = server.QuietServer(("127.0.0.1", 0), server.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    q = f"?key={server.TOKEN}" if server.TOKEN else ""
    try:
        wait(lambda: S[0].tier() == "HOT", 4.0)
        parts = _first_parts(port, f"/stream/0{q}", 1)
        check("HTTP: HOT camera -> first MJPEG part is LIVE", parts and parts[0][1] == "live", parts)
        print(f"      (HOT camera first live frame after {parts[0][0] if parts else '-'} ms over HTTP)")
        c = S[10]                                           # cached earlier, not in the pool now
        wait(lambda: not c._running, 3.0)
        time.sleep(server.LIVE_MAX_AGE_S + 0.2)
        check("camera 10 is WARM (cached, not connected)", c.tier() == "WARM", c.tier())
        parts = _first_parts(port, f"/stream/10{q}{'&' if q else '?'}prio=full", 60, timeout=6.0)
        states = [p[1] for p in parts]
        first_live = next((p[0] for p in parts if p[1] == "live"), None)
        check("HTTP: WARM camera -> CACHED frame immediately, then LIVE once promoted",
              states[:1] == ["cached"] and first_live is not None, states[:12])
        print(f"      (WARM camera: cached frame after {parts[0][0] if parts else '-'} ms, "
              f"live after {first_live} ms with the stub NVR)")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    t0 = time.time()
    threading.Thread(target=_sampler, daemon=True).start()
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n-- {name}")
            fn()
    _SAMPLING["on"] = False
    print(f"\npeak NVR slots used: {PEAK} (cap {dict(CAP)})")
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}  ({time.time() - t0:.1f}s)")
    sys.exit(1 if FAILS else 0)
