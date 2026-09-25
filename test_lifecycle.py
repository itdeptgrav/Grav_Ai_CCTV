"""Automated lifecycle tests for the CCTV server's viewer / NVR-slot handling.

These test the SERVER-SIDE invariants that the browser relies on:
  - the per-NVR slot cap is never exceeded,
  - removing the last viewer releases the slot promptly (no leak),
  - a worker with viewers keeps exactly one slot (shared connection),
  - offline cameras do not permanently hold a slot (monitor-gated + released
    on every failed attempt).

Run:  python test_lifecycle.py
Exits non-zero on any failure. Does not need the HTTP server running; it drives
CamStream / the NVR semaphore directly. cv2 is stubbed so no real RTSP is needed.
"""
import os
import sys
import time
import types

# Offline unit tests: no real RTSP pre-flight to the NVRs, no event log noise.https://cctv.grav.in/?key=grav-cctv-4821
os.environ["CCTV_PREFLIGHT"] = "0"
os.environ["CCTV_PERSISTENT"] = "0"          # on-demand mode (the fallback); test_relay.py covers the pool
os.environ["CCTV_LOG_EVENTS"] = "0"

# ── stub cv2 BEFORE importing server, so tests are deterministic/offline ──
_fake = types.ModuleType("cv2")
_fake.CAP_FFMPEG = 0
_fake.IMWRITE_JPEG_QUALITY = 1
_fake.FONT_HERSHEY_SIMPLEX = 0
_OPEN = {"ok": True}   # flip to simulate an offline camera


class _Cap:
    def __init__(self, *a, **k):
        self._n = 0
    def isOpened(self):
        return _OPEN["ok"]
    def read(self):
        self._n += 1
        time.sleep(0.02)
        import numpy as np
        return True, np.zeros((8, 8, 3), dtype="uint8")
    def grab(self):                      # server decodes via grab() + retrieve()
        self._n += 1
        time.sleep(0.02)
        return True
    def retrieve(self):
        import numpy as np
        return True, np.zeros((8, 8, 3), dtype="uint8")
    def release(self):
        pass


_fake.VideoCapture = lambda *a, **k: _Cap()
_fake.resize = lambda frame, size: frame
_fake.imencode = lambda ext, frame, *a: (True, memoryview(b"jpegbytes"))
_fake.cvtColor = lambda frame, code: frame
_fake.putText = lambda *a, **k: None
_fake.rectangle = lambda *a, **k: None
_fake.LINE_AA = 16
_fake.getTextSize = lambda text, font, scale, thick: ((int(len(text) * 20 * scale), int(22 * scale)), 5)
_fake.circle = lambda *a, **k: None
sys.modules["cv2"] = _fake

import server   # noqa: E402

FAILS = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def nvr_active(nvr):
    with server._ACTIVE_LOCK:
        return server.NVR_ACTIVE[nvr]


# Make MONITOR always say "reachable" so slot logic (not the network) is tested.
class _AlwaysUp:
    checked = time.time()
    reachable = True
    label = "NVR REACHABLE"
server.MONITOR.get = lambda nvr: _AlwaysUp()

# nvr2 cameras are indices 0..12
NVR2 = [s for s in server.STREAMS if s.info["nvr"] == "nvr2"]


def test_slot_cap_and_release():
    # Open more nvr2 cameras than the cap; active must never exceed the cap.
    cams = NVR2[: server.NVR_MAX_CONN + 3]   # e.g. 9 on a cap of 6
    for c in cams:
        c.add_viewer()
    peak = 0
    for _ in range(30):
        time.sleep(0.1)
        peak = max(peak, nvr_active("nvr2"))
    check("slot cap never exceeded", peak <= server.NVR_MAX_CONN)
    check("cap actually reached (contention exercised)", peak == server.NVR_MAX_CONN)
    # Remove all viewers -> slots must drain to zero quickly (no leak).
    for c in cams:
        c.remove_viewer()
    ok = False
    for _ in range(40):
        time.sleep(0.1)
        if nvr_active("nvr2") == 0:
            ok = True
            break
    check("all slots released after viewers leave (no leak)", ok)


def test_shared_connection_one_slot():
    c = NVR2[0]
    c.add_viewer(); c.add_viewer(); c.add_viewer()   # 3 viewers, same camera
    time.sleep(1.0)
    check("3 viewers on one camera hold exactly 1 slot", nvr_active("nvr2") == 1)
    check("viewer count is 3", c.viewers == 3)
    c.remove_viewer(); c.remove_viewer()
    time.sleep(0.3)
    check("still 1 slot with 1 viewer left", nvr_active("nvr2") == 1 and c.viewers == 1)
    c.remove_viewer()
    ok = False
    for _ in range(30):
        time.sleep(0.1)
        if nvr_active("nvr2") == 0 and c.viewers == 0:
            ok = True; break
    check("slot + worker released when last viewer leaves", ok)


def test_offline_does_not_hog_slot():
    _OPEN["ok"] = False   # simulate camera that never opens
    try:
        c = NVR2[0]
        c.add_viewer()
        time.sleep(1.0)
        # An offline camera cycles: it may momentarily hold a slot during an
        # attempt, but must spend most time NOT holding one (released on backoff),
        # so a second camera can still get a slot.
        c2 = NVR2[1]
        c2.add_viewer()
        _OPEN["ok"] = True          # c2 is "live"
        got_live = False
        for _ in range(40):
            time.sleep(0.1)
            if c2.status == "LIVE":
                got_live = True; break
        check("a live camera gets a slot even while another is offline", got_live)
        c.remove_viewer(); c2.remove_viewer()
        ok = False
        for _ in range(40):
            time.sleep(0.1)
            if nvr_active("nvr2") == 0:
                ok = True; break
        check("slots drain after mixed offline/live viewers leave", ok)
    finally:
        _OPEN["ok"] = True


def test_grace_reopen_race():
    # remove then immediately re-add: the worker must end up running with 1 viewer
    # and exactly 1 slot (no double worker, no killed-by-old-timer).
    c = NVR2[2]
    c.add_viewer()
    time.sleep(0.6)
    c.remove_viewer()
    c.add_viewer()          # re-add immediately
    time.sleep(1.0)
    check("reopen keeps exactly 1 viewer", c.viewers == 1)
    check("reopen holds exactly 1 slot (no duplicate worker)", nvr_active("nvr2") == 1)
    c.remove_viewer()
    time.sleep(1.0)


if __name__ == "__main__":
    test_slot_cap_and_release()
    test_shared_connection_one_slot()
    test_offline_does_not_hog_slot()
    test_grace_reopen_race()
    print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)
