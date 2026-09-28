"""Recorded playback tests -- offline: fake NVRs (fake_nvr.py) speak the real NVRs'
playback RTSP (proven behaviour: DESCRIBE 404 / recorded range, clock= in UTC, key
frame start, gaps jump the RTP clock, Scale = key frames only, BYE at the end), a fake
vendor web API answers the recording search, the live video is a stubbed cv2 and the
decoder is a fake that reads each frame's TRUE recorded time (the camera clock).

Covers: time rules (IST <-> UTC), search validation, LAN search (vendor segments +
gaps) and remote search (RTSP only), one-camera playback (UTC clock= sent, displayed
time vs camera time, start/end), seek forward/backward, pause/resume, 2x/4x, audio
(one camera, codec), recording gaps (seek into a gap, next/previous, crossing a gap
alone and with other cameras, remote gap probing), capacity (per NVR, server-wide,
stop/retry), Select All + paging, NVR slot accounting (never above the cap, background
yields, viewed live video never touched), cleanup (page gone, close, shutdown), access
control, no credentials, audit log, NVR info (drift, admin warning), page content.

Run:  python test_playback.py        (exits non-zero on any failure)
"""
import os
import re
import sys
import json
import time
import types
import base64
import socket
import struct
import datetime
import tempfile
import threading
import collections

TMP = tempfile.mkdtemp()
os.environ.update({
    "CCTV_PERSISTENT": "1", "CCTV_PREFLIGHT": "0", "CCTV_NVR_MAX_CONN": "6",
    "CCTV_LOG_EVENTS": os.environ.get("PLAYBACK_TEST_LOG", "0"),
    "CCTV_SETTINGS_FILE": os.path.join(TMP, "camera-settings.json"),
    "CCTV_WARM_STEP_S": "0.05", "CCTV_MIN_BG_HOT_S": "0.3", "CCTV_BG_SWAP_S": "0.2",
    "CCTV_REFRESH_EVERY_S": "0", "CCTV_IDLE_FPS": "1", "CCTV_STREAM_FPS": "8",
    "CCTV_PLAYBACK_MAX_WORKERS": "3", "CCTV_NVR1_PLAYBACK_MAX": "2", "CCTV_NVR2_PLAYBACK_MAX": "2",
    "CCTV_PLAYBACK_GRACE_S": "1", "CCTV_PLAYBACK_PAUSE_HOLD_S": "3", "CCTV_PLAYBACK_SEARCH_CACHE_S": "0",
    "CCTV_PLAYBACK_STALL_S": "4", "CCTV_PLAYBACK_GRID_FPS": "25", "CCTV_PLAYBACK_SINGLE_FPS": "25",
})
for k in ("CCTV_NVR1_MAX_CONN", "CCTV_NVR2_MAX_CONN"):
    os.environ.pop(k, None)

import numpy as np                                     # noqa: E402


class _Cap:
    """Live video: never touches the network."""
    def __init__(self, url, *a, **k):
        time.sleep(0.03)
        self.frame = np.zeros((288, 352, 3), dtype="uint8")

    def isOpened(self):
        return True

    def grab(self):
        time.sleep(0.02)
        return True

    def retrieve(self):
        return True, self.frame

    def release(self):
        pass


cv2 = types.ModuleType("cv2")
for k, v in dict(CAP_FFMPEG=0, IMWRITE_JPEG_QUALITY=1, FONT_HERSHEY_SIMPLEX=0, LINE_AA=16, INTER_AREA=3).items():
    setattr(cv2, k, v)
cv2.VideoCapture = _Cap
cv2.resize = lambda frame, size, *a, **k: np.zeros((size[1], size[0], 3), dtype="uint8")
cv2.imencode = lambda ext, frame, *a: (True, memoryview(b"jpeg"))
cv2.putText = lambda *a, **k: None
cv2.getTextSize = lambda text, font, scale, thick: ((int(len(text) * 20 * scale), int(22 * scale)), 5)
cv2.circle = lambda *a, **k: None
sys.modules["cv2"] = cv2

import server                                         # noqa: E402
import playback as pb                                 # noqa: E402
import playback_time as pt                            # noqa: E402
from fake_nvr import FakeNvr, FakeVendorApi           # noqa: E402

S, A = server.STREAMS, server.AUDIO
NVR1 = [s.index for s in S if s.info["nvr"] == "nvr1"]
NVR2 = [s.index for s in S if s.info["nvr"] == "nvr2"]
CH = {i: S[i].info["channel"] for i in range(len(S))}
IDX = {(s.info["nvr"], s.info["channel"]): s.index for s in S}
HR, GAPCAM, NONECAM, CORR, PARTCAM = IDX[("nvr2", 8)], IDX[("nvr2", 1)], IDX[("nvr2", 2)], IDX[("nvr1", 14)], IDX[("nvr1", 4)]
GAP1CAM = IDX[("nvr1", 5)]                             # NVR1 camera with a recording gap

B = (pt.nvr_now() - datetime.timedelta(hours=2)).replace(second=0, microsecond=0)
M = lambda m, s=0: B + datetime.timedelta(minutes=m, seconds=s)          # noqa: E731
FAKE = {
    "nvr1": FakeNvr(server.NVRS["nvr1"]["user"], server.NVRS["nvr1"]["pass"], {c: "PCMU" for c in range(1, 17)}),
    "nvr2": FakeNvr(server.NVRS["nvr2"]["user"], server.NVRS["nvr2"]["pass"], {c: "PCMA" for c in range(1, 17)}),
}
for nvr in FAKE:                                       # continuous recording on every channel ...
    for c in range(1, 17):
        FAKE[nvr].recordings[c] = [(M(0), M(60))]
FAKE["nvr2"].recordings[1] = [(M(0), M(10)), (M(10, 8), M(60))]        # ... an 8 s gap at +10 min
FAKE["nvr2"].recordings[2] = []                                           # ... nothing recorded
FAKE["nvr1"].recordings[4] = [(M(30), M(60))]                             # ... only the second half
FAKE["nvr1"].recordings[5] = [(M(0), M(20)), (M(20, 30), M(20, 31)),     # ... a 30 s gap at +20 min, then
                              (M(20, 30), M(60))]                        # a 1 s file + the long one (real NVR1)
FAKE["nvr1"].gap_ends_session = True        # like the real NVR1: its sessions end at the first gap
# several days: NVR2 CH13 = 30 min files from day 1 06:00 to day 4 06:00 with a 2 h gap on
# day 2 (13:00-15:00), across two midnights; NVR1 CH13 = the same days in seamless 1 h files
DAY0 = (pt.nvr_now() - datetime.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
D = lambda d, h=0, m=0, s=0: DAY0 + datetime.timedelta(days=d, hours=h, minutes=m, seconds=s)   # noqa: E731
LONG2, LONG1 = IDX[("nvr2", 13)], IDX[("nvr1", 13)]
FAKE["nvr2"].recordings[13] = [(D(1, 6) + datetime.timedelta(minutes=30 * k), D(1, 6) + datetime.timedelta(minutes=30 * k + 30))
                               for k in range(144) if not D(2, 13) <= D(1, 6) + datetime.timedelta(minutes=30 * k) < D(2, 15)]
FAKE["nvr1"].recordings[13] = [(D(1, 6) + datetime.timedelta(hours=k), D(1, 6) + datetime.timedelta(hours=k + 1)) for k in range(72)]
API = {k: FakeVendorApi(FAKE[k].recordings, drift_s=-37 if k == "nvr2" else -35) for k in FAKE}
MODE = {"remote": False}
pb.HOOK.endpoint = lambda nvr: ("127.0.0.1", FAKE[nvr].port)
pb.HOOK.remote = lambda: MODE["remote"]
pb.HOOK.api_host = lambda nvr: API[nvr].host
server.MONITOR.get = lambda nvr: type("U", (), {"checked": time.time(), "reachable": True, "label": "ok"})()
for _k in server.NVRS:
    server._AUTH_OK[_k] = True

TRUTH = collections.defaultdict(list)                 # worker label -> [(shown ms, camera ms, t)]
FRAME = np.zeros((72, 128, 3), dtype="uint8")


class FakeDecoder:
    """Stands in for OpenCV/FFmpeg: 'decodes' each access unit at once and records
    the time the server computed next to the camera's own time (b"FAKE" + ms)."""

    def __init__(self, on_frame, params=b"", label="", want=None):
        self.on_frame, self.label, self.fifo, self.frames, self.closed = on_frame, label, collections.deque(), 0, False
        self.want = want

    def push(self, au, meta):
        if self.closed:
            return
        i = au.find(b"FAKE")
        true_ms = struct.unpack(">Q", au[i + 4:i + 12])[0] if i >= 0 else None
        self.frames += 1
        TRUTH[self.label].append((meta[1] if meta else None, true_ms, time.monotonic()))
        self.on_frame(FRAME if self.want is None or self.want(meta) else None, meta)

    def close(self):
        self.closed = True


pb.new_decoder = FakeDecoder
pb.new_key_decoder = FakeDecoder
FAILS = []
PEAK = {"nvr1": 0, "nvr2": 0}
PB_PEAK = {"all": 0}
_RUN = {"on": True}


def _sampler():
    while _RUN["on"]:
        with server._ACTIVE_LOCK:
            for k in server.NVRS:
                PEAK[k] = max(PEAK[k], server.NVR_ACTIVE[k], len(server.NVR_OWNERS[k]))
        PB_PEAK["all"] = max(PB_PEAK["all"], len(pb.running_workers()))
        time.sleep(0.003)


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


def q(extra=""):
    parts = ([f"key={server.TOKEN}"] if server.TOKEN else []) + ([extra] if extra else [])
    return ("?" + "&".join(parts)) if parts else ""


def http(method, path, body=None, key=True, ctype="application/json"):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=90)
    data = json.dumps(body).encode() if body is not None else b""
    p = path + (q() if key else "")
    head = f"{method} {p} HTTP/1.1\r\nHost: t\r\nConnection: close\r\nContent-Length: {len(data)}\r\n"
    if body is not None:
        head += f"Content-Type: {ctype}\r\n"
    s.sendall((head + "\r\n").encode() + data)
    buf = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    s.close()
    head, _, rest = buf.partition(b"\r\n\r\n")
    code = int(head.split(b" ")[1])
    try:
        return code, json.loads(rest.decode())
    except ValueError:
        return code, rest.decode("utf-8", "replace")


def search(cams, a, b, **kw):
    return http("POST", "/api/playback/search", dict({"from": pt.fmt_local(a), "to": pt.fmt_local(b), "cameras": cams}, **kw))


class PWs(threading.Thread):
    """The playback page's WebSocket: states (JSON), frames 'V', audio 'A'; send()."""

    def __init__(self, sid, key=True):
        super().__init__(daemon=True)
        self.sid, self.key = sid, key
        self.states, self.frames, self.audio, self.avails = [], [], [], []
        self.status_line, self.closed, self.stop, self.sock = None, False, False, None
        self._wl = threading.Lock()
        self.start()
        wait(lambda: self.status_line is not None, 5.0)

    def run(self):
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=30)
            self.sock = s
            path = f"/api/playback/ws?sid={self.sid}" + (("&key=" + server.TOKEN) if self.key and server.TOKEN else "")
            s.sendall((f"GET {path} HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                c = s.recv(4096)
                if not c:
                    self.status_line = "closed"
                    return
                buf += c
            head, _, buf = buf.partition(b"\r\n\r\n")
            self.status_line = head.split(b"\r\n")[0].decode()
            if " 101 " not in self.status_line:
                return
            while not self.stop:
                while len(buf) >= 2:
                    op, n, off = buf[0] & 0x0F, buf[1] & 0x7F, 2
                    if n == 126:
                        if len(buf) < 4:
                            break
                        n, off = struct.unpack(">H", buf[2:4])[0], 4
                    elif n == 127:
                        if len(buf) < 10:
                            break
                        n, off = struct.unpack(">Q", buf[2:10])[0], 10
                    if len(buf) < off + n:
                        break
                    data, buf = buf[off:off + n], buf[off + n:]
                    t = time.monotonic()
                    if op == 1:
                        m = json.loads(data)
                        (self.avails if m.get("t") == "avail" else self.states).append((t, m))
                    elif op == 2 and data[:1] == b"V":
                        self.frames.append((t, data[1], int.from_bytes(data[2:10], "big")))
                    elif op == 2 and data[:1] == b"A":
                        self.audio.append((t, data[1], data[2], int.from_bytes(data[3:11], "big")))
                    elif op == 9:
                        self._send(0xA, data)
                    elif op == 8:
                        return
                c = s.recv(65536)
                if not c:
                    break
                buf += c
        except OSError:
            pass
        finally:
            self.closed = True

    def _send(self, op, data):
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        with self._wl:
            self.sock.sendall(head + mask + bytes(b ^ mask[k % 4] for k, b in enumerate(data)))

    def send(self, obj):
        self._send(0x1, json.dumps(obj).encode())

    def state(self):
        return self.states[-1][1] if self.states else {}

    def tile(self, tid):
        return next((t for t in self.state().get("tiles", []) if t["id"] == tid), {})

    def close(self):
        self.stop = True
        try:
            self._send(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        except (OSError, AttributeError):
            pass


class VideoViewer(threading.Thread):
    def __init__(self, i, extra=""):
        super().__init__(daemon=True)
        self.i, self.extra, self.sock, self.stop = i, extra, None, False
        self.start()

    def run(self):
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
            self.sock = s
            s.sendall(f"GET /stream/{self.i}{q(self.extra)} HTTP/1.1\r\nHost: t\r\n\r\n".encode())
            while not self.stop and s.recv(65536):
                pass
        except OSError:
            pass

    def close(self):
        self.stop = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        except (OSError, AttributeError):
            pass


def pb_owners(nvr):
    with server._ACTIVE_LOCK:
        return [k for k in server.NVR_OWNERS[nvr] if k >= pb.KEY_BASE]


def all_stopped(timeout=6.0):
    return wait(lambda: not pb.running_workers() and FAKE["nvr1"].pb_active == 0 and FAKE["nvr2"].pb_active == 0
                and not pb_owners("nvr1") and not pb_owners("nvr2"), timeout)


def close_session(w, sid):
    w.close()
    http("POST", "/api/playback/close", {"sid": sid})
    all_stopped()


def truth_of(label_part):
    return [x for k, v in TRUTH.items() if label_part in k for x in v]


def ms(dt):
    return pt.to_ms(dt)


# ── tests ────────────────────────────────────────────────────────────────────────
def test_time_rules():
    t = pt.parse_local("2026-09-28T11:05:40")
    check("IST -> playback URL time: 2026_09_28_11_05_40 (NVR local)", pt.url_time(t) == "2026_09_28_11_05_40")
    check("IST -> RTSP clock= is UTC: 11:05:40 IST -> 20260928T053540Z (the value proven on the NVR)",
          pt.rtsp_clock(t) == "20260928T053540Z", pt.rtsp_clock(t))
    check("browser ms is the real instant and round-trips exactly", pt.from_ms(pt.to_ms(t)) == t
          and pt.to_ms(t) == int(datetime.datetime(2026, 9, 28, 5, 35, 40, tzinfo=datetime.timezone.utc).timestamp() * 1000))
    check("'YYYY-MM-DD HH:MM' and 'T' forms parse; nonsense is rejected",
          pt.parse_local("2026-09-28 11:05") == datetime.datetime(2026, 9, 28, 11, 5)
          and _raises(lambda: pt.parse_local("28/09/2026")))
    now = pt.nvr_now()
    check("validation: To before From / too long / future From",
          pt.validate_range(now, now - datetime.timedelta(minutes=1), 24) == "To time must be after From time."
          and "too long" in pt.validate_range(now - datetime.timedelta(hours=30), now, 24)
          and pt.validate_range(now + datetime.timedelta(hours=1), now + datetime.timedelta(hours=2), 24) == "From time is in the future.")
    check("the app's range limit is 31 days (was 24 h: our own rule, not the NVRs'); 72 h and 7 days pass",
          pb.MAX_RANGE_H == 744 and pt.validate_range(now - datetime.timedelta(hours=72), now, pb.MAX_RANGE_H) is None
          and pt.validate_range(now - datetime.timedelta(days=7), now, pb.MAX_RANGE_H) is None
          and pt.validate_range(now - datetime.timedelta(days=32), now, pb.MAX_RANGE_H)
          == "The time range is too long: at most 31 days per search.", pb.MAX_RANGE_H)
    check("... without a length limit (the NVRs' oldest recordings bound the search) 60 days pass the time rules",
          pt.validate_range(now - datetime.timedelta(days=60), now) is None
          and pt.validate_range(now, now - datetime.timedelta(minutes=1)) == "To time must be after From time.")
    a = datetime.datetime(2026, 9, 25, 10, 0)
    plan = pt.plan_ranges(a, a + datetime.timedelta(hours=72), 24)
    check("range planner: 25 Sep 10:00 -> 28 Sep 10:00 = three NVR requests 25->26->27->28 Sep 10:00, no overlap, no hole",
          plan == [(a, a + datetime.timedelta(hours=24)), (a + datetime.timedelta(hours=24), a + datetime.timedelta(hours=48)),
                   (a + datetime.timedelta(hours=48), a + datetime.timedelta(hours=72))], plan)
    plan = pt.plan_ranges(datetime.datetime(2026, 9, 26, 23, 50), datetime.datetime(2026, 9, 27, 0, 10), 24)
    check("... 23:50 -> 00:10 stays ONE request across midnight (NVR local dates 26 -> 27 Sep)",
          plan == [(datetime.datetime(2026, 9, 26, 23, 50), datetime.datetime(2026, 9, 27, 0, 10))]
          and pt.url_time(plan[0][1]) == "2026_09_27_00_10_00"
          and pt.rtsp_clock(datetime.datetime(2026, 9, 27, 0, 10)) == "20260926T184000Z", plan)


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


def test_fast_decoder_units():
    """2x/4x on the real NVRs: key frames only, 1-2 a second. The continuous FFmpeg
    decoder held them back in its frame threads (0 frames shown in 5 s), so each key
    frame is decoded on its own."""
    got, gate = [], threading.Event()
    real = pb.decode_picture
    pb.decode_picture = lambda data: (gate.wait(2.0), FRAME if data.startswith(b"PARAMS") else None)[1]
    try:
        k = pb.KeyDecoder(lambda f, m: got.append(m[1]), b"PARAMS", "unit")
        k.push(b"P-frame", (1, 100, False))
        k.push(b"KEY1", (1, 200, True))                  # taken by the decoder (blocked on the gate)
        time.sleep(0.15)
        k.push(b"KEY2", (1, 300, True))                  # waits ...
        k.push(b"KEY3", (1, 400, True))                  # ... and is replaced by the newer one
        gate.set()
        k.finish(2.0)
        check("key-frame decoder: parameter sets prepended, non-key frames skipped, each key frame decoded "
              "on its own; when behind only the newest waits", got == [200, 400], got)
        k.close()
        check("... its thread ends on close", not k._thread.is_alive())
    finally:
        pb.decode_picture = real
    made = []

    class Rec:
        lag = 0

        def __init__(self, on_frame, params=b"", label="", want=None):
            self.pushed, self.closed = [], False
            made.append(self)

        def push(self, au, meta):
            self.pushed.append(au)

        def backlog(self):
            return Rec.lag

        def close(self):
            self.closed = True

    old = pb.new_decoder, pb.new_key_decoder
    pb.new_decoder = pb.new_key_decoder = Rec
    try:
        d = pb.Decoders(lambda f, m: None, b"", "unit")
        d.push(b"a", (1, 1, True))
        d.push(b"b", (2, 2, True), fast=True)
        d.push(b"c", (3, 3, False))
        d.finish(0.1)
        d.close()
        check("Decoders: 1x -> the continuous decoder, 2x/4x -> the key-frame decoder (made when first needed)",
              len(made) == 2 and made[0].pushed == [b"a", b"c"] and made[1].pushed == [b"b"],
              [m.pushed for m in made])
        made.clear()
        d = pb.Decoders(lambda f, m: None, b"", "unit")
        Rec.lag = pb.MAX_LAG_FRAMES + 1                  # the CPU cannot keep up
        d.push(b"p", (1, 1, False))                       # not a key frame: it cannot restart here
        d.push(b"k", (1, 2, True))                        # key frame: a fresh decoder starts from it
        Rec.lag = 0
        time.sleep(0.2)
        check(f"decoder more than {pb.MAX_LAG_FRAMES // 25} s behind: replaced at the next key frame (picture skips "
              "ahead instead of lagging more and more), the old one closed",
              len(made) == 2 and made[0].pushed == [b"p"] and made[1].pushed == [b"k"] and made[0].closed
              and d.resyncs == 1, ([m.pushed for m in made], d.resyncs))
        d.close()
    finally:
        pb.new_decoder, pb.new_key_decoder = old
        Rec.lag = 0


def test_search_validation():
    pb._RETENTION.clear()                                # the NVRs' oldest recordings not known yet
    code, d = search([HR], M(20), M(10))
    check("search: To before From -> 400 'To time must be after From time.'", code == 400 and "after From" in d["error"], d)
    code, d = search([HR], M(0) - datetime.timedelta(days=32), M(0))
    check("search: range too long (32 days) -> 400 'at most 31 days'", code == 400 and "31 days" in d["error"], d)
    code, d = search([HR], M(0) - datetime.timedelta(hours=72), M(0))
    check("search: 72 hours is accepted (no 24 h limit any more)", code == 200 and d.get("ok"), d)
    http("POST", "/api/playback/close", {"sid": d.get("sid")})
    code, d = search([], M(1), M(2))
    check("search: no camera -> 400 'Select at least one camera.'", code == 400 and "camera" in d["error"], d)
    code, d = search([999], M(1), M(2))
    check("search: unknown camera -> 400", code == 400, d)
    code, d = http("POST", "/api/playback/search", {"from": "yesterday", "to": "now", "cameras": [HR]})
    check("search: unparsable date -> 400", code == 400, d)
    code, d = search([HR, HR, HR], M(1), M(2))
    check("search: duplicate cameras collapse to one tile", code == 200 and len(d["tiles"]) == 1, d)
    http("POST", "/api/playback/close", {"sid": d.get("sid")})
    code, d = http("POST", "/api/playback/search", {"from": pt.fmt_local(M(1)), "to": pt.fmt_local(M(2)), "cameras": [HR]},
                   ctype="text/plain")
    check("search: only application/json is accepted (CSRF rule of the settings API)", code == 415, code)


def test_retention_limit():
    """How far back a search goes follows each NVR's OLDEST recording (read from the NVR
    in the background), not a fixed number of days; 31 days only while it is not known."""
    pb._RETENTION.clear()
    MODE["remote"] = False
    real_api, asked = pb.vendor_api, collections.Counter()

    class Counting:                                      # counts the oldest-recording queries per NVR
        def __init__(self, nvr, api):
            self.nvr, self.api = nvr, api

        def oldest_recording(self, *a):
            asked[self.nvr] += 1
            return self.api.oldest_recording(*a)
    pb.vendor_api = lambda nvr: Counting(nvr, real_api(nvr))
    try:
        r2, r1 = pb.refresh_retention("nvr2"), pb.refresh_retention("nvr1")
    finally:
        pb.vendor_api = real_api
    chans = pb._channels("nvr2")
    check("oldest recording read from the NVR (office LAN, vendor search): NVR2 = its oldest file (CH13, day 1 "
          "06:00); a camera without recordings -> None; ONE metadata request per camera",
          r2["method"] == "vendor" and r2["oldest"] == D(1, 6) and r2["cameras"].get(13) == D(1, 6)
          and r2["cameras"].get(2, "x") is None and r2["cameras"].get(8) == M(0) and r2["error"] is None
          and asked["nvr2"] == len(chans), (r2, asked, len(chans)))
    check("... NVR1 too", r1["oldest"] == D(1, 6) and r1["method"] == "vendor", r1)
    code, cfg = http("GET", "/api/playback/config")
    k2 = (cfg.get("retention") or {}).get("NVR2") or {}
    check("playback config: each NVR's oldest recording + days kept (the page's limit and hint)",
          code == 200 and k2.get("oldestMs") == pt.to_ms(D(1, 6)) and k2.get("method") == "vendor"
          and abs(k2.get("days", 0) - (pt.nvr_now() - D(1, 6)).total_seconds() / 86400) < 0.1, k2)
    code, info = http("GET", "/api/playback/nvr-info")
    check("... and in Settings > Playback (NVR info)", code == 200
          and ((info["nvrs"]["NVR2"].get("kept") or {}).get("oldestMs")) == pt.to_ms(D(1, 6)), info)
    far = M(0) - datetime.timedelta(days=40)
    code, d = search([HR], far, M(0), wait=0)
    check("search 40 days back: accepted (no fixed 31-day limit once the NVR's oldest recording is known), "
          "started at the oldest recording, with a note", code == 200 and d.get("fromMs") == pt.to_ms(D(1, 6))
          and d.get("askedFromMs") == pt.to_ms(far) and "Nothing is recorded before" in (d.get("note") or "")
          and "NVR2 keeps" in d["note"] and d.get("retention", {}).get("NVR2"), d)
    http("POST", "/api/playback/close", {"sid": d.get("sid")})
    code, d = search([HR], D(0), D(1), wait=0)
    check("search entirely before the oldest recording -> 400 'No recordings that old: NVR2 keeps N days of footage'",
          code == 400 and d["error"].startswith("No recordings that old: NVR2 keeps") and d.get("retention"), d)
    code, d = search([HR], D(2), D(3), wait=0)
    check("... a range after it: unchanged, no note", code == 200 and d.get("fromMs") == pt.to_ms(D(2))
          and not d.get("note"), d)
    http("POST", "/api/playback/close", {"sid": d.get("sid")})
    with pb._RET_LOCK:                                   # NVR1 keeps older footage than NVR2
        pb._RETENTION["nvr1"] = dict(pb._RETENTION["nvr1"], oldest=D(0, 3))
    code, d = search([HR, CORR], D(0), M(0), wait=0)
    check("cameras on both NVRs: the search starts at the OLDER of their oldest recordings (NVR1, day 0 03:00)",
          code == 200 and d.get("fromMs") == pt.to_ms(D(0, 3)), d)
    http("POST", "/api/playback/close", {"sid": d.get("sid")})
    code, d = search([HR, CORR], D(0), D(0, 2), wait=0)
    check("... before both -> 400 naming both NVRs", code == 400 and "NVR1 keeps" in d.get("error", "")
          and "NVR2 keeps" in d["error"], d)
    code, d = search([HR], D(0), D(1), wait=0)
    check("... NVR2 cameras alone are still bound by NVR2's own oldest recording", code == 400, d)

    class Broken:
        def oldest_recording(self, *a):
            raise OSError("search API did not answer")
    real_api = pb.vendor_api
    pb.vendor_api = lambda nvr: Broken()
    try:
        r = pb.refresh_retention("nvr2")
        check("the NVR not answering the check: the last good answer is kept (and the error shown)",
              r["oldest"] == D(1, 6) and "did not answer" in (r["error"] or ""), r)
        with pb._RET_LOCK:
            pb._RETENTION.pop("nvr2")
        r = pb.refresh_retention("nvr2")
        code, d = search([HR], M(0) - datetime.timedelta(days=32), M(0))
        check("... never answered: the fixed 31-day limit applies, the message says why",
              r["oldest"] is None and code == 400 and "31 days" in d.get("error", "") and "not known" in d["error"], d)
    finally:
        pb.vendor_api = real_api
    # an NVR keeping more than one 30-day query covers: stepped back 30 days at a time
    old16, now = FAKE["nvr2"].recordings[16], pt.nvr_now()
    start = (now - datetime.timedelta(days=70)).replace(minute=0, second=0)
    FAKE["nvr2"].recordings[16] = [(start + datetime.timedelta(hours=h), start + datetime.timedelta(hours=h + 1))
                                   for h in range(int((now - start).total_seconds() // 3600))]
    api, spans = pb.vendor_api("nvr2"), []

    class Spans:
        def oldest_recording(self, ch, a, b):
            spans.append((b - a).days)
            return api.oldest_recording(ch, a, b)
    try:
        t = pb._oldest_vendor(Spans(), 16, now)
        check("an NVR keeping 70 days: found by stepping back in 30-day queries (3 requests, each a measured-safe size)",
              t == start and spans == [30, 30, 30], (t, start, spans))
    finally:
        FAKE["nvr2"].recordings[16] = old16
    # outside the office (RTSP only): DESCRIBE checks against the fake NVR, CH13 only
    real_ch = pb._channels
    MODE["remote"] = True
    pb._channels = lambda nvr: [13]
    try:
        r = pb.refresh_retention("nvr2")
        check("outside the office (RTSP only): oldest recording found by DESCRIBE checks -- the exact first recorded "
              "second, taken 10 s earlier for the NVR (never later than any camera's)",
              r["method"] == "rtsp" and r["cameras"].get(13) == D(1, 6)
              and r["oldest"] == D(1, 6) - datetime.timedelta(seconds=10), r)
    finally:
        pb._channels = real_ch
        MODE["remote"] = False
    # the RTSP search logic on its own: a power cut, a camera without footage, one with older footage
    real_at, real_desc = pb._recorded_at, pb._describe
    now = pt.nvr_now()
    X1, X3 = now - datetime.timedelta(days=4, minutes=-17), now - datetime.timedelta(days=6, minutes=-5)
    cut, probes = [], []

    def truth(ch, x):                                    # CH1 from X1, CH3 from X3 (older), CH2 nothing
        return not any(c0 <= x < c1 for c0, c1 in cut) and {1: X1, 3: X3}.get(ch, now) <= x < now

    def rec_at(nvr, ch, x, b):
        probes.append((ch, x))
        return truth(ch, x)

    def describe(nvr, ch, x, y):                         # like NVR1: a window starting in a gap reads empty
        probes.append((ch, x))
        return (y - x).total_seconds() if truth(ch, x) else 0.0
    pb._recorded_at, pb._describe = rec_at, describe
    try:
        pb._channels = lambda nvr: [1]
        t1 = pb._oldest_rtsp("nvr2").get(1)
        n1 = len(probes)                                 # a moment the bisection found recorded, far from X1:
        p = next((x for c, x in list(probes) if X1 + datetime.timedelta(days=1) < x < now - datetime.timedelta(days=1)
                  and truth(1, x)), None)
        probes.clear()
        cut.append((p - datetime.timedelta(minutes=50), p + datetime.timedelta(minutes=50)))
        t2 = pb._oldest_rtsp("nvr2").get(1)
        check("RTSP search: the exact first recorded second; also when a power cut sits where the bisection probed "
              "(it first lands after the cut, then steps over it)", t1 == X1 and p is not None and t2 == X1, (X1, t1, p, t2))
        cut.clear()
        probes.clear()
        pb._channels = lambda nvr: [1, 2, 3]
        out = pb._oldest_rtsp("nvr2")
        check(f"... another camera with older footage (CH3) is found too; a camera with none (CH2) is passed; "
              f"{len(probes)} DESCRIBE checks (one camera alone: {n1})",
              out == {1: X1, 3: X3} and len(probes) <= 220, (out, len(probes)))
    finally:
        pb._recorded_at, pb._describe, pb._channels = real_at, real_desc, real_ch
    # real NVR1 at its oldest recording (measured 28 Sep): DESCRIBE [x, y] = y - first recorded
    # moment, exact up to 2 h windows, but a 24 h window comes back 1669 s (27 min 49 s) short
    X = pt.nvr_now().replace(microsecond=0) - datetime.timedelta(days=27, minutes=3)
    n_desc = [0]

    def nvr1_describe(nvr, ch, x, y):
        n_desc[0] += 1
        rec = max(0.0, (y - max(x, X)).total_seconds())
        return max(0.0, rec - 1669) if rec and (y - x).total_seconds() > 2 * 3600 else rec
    pb._describe = nvr1_describe
    try:
        one = datetime.timedelta(seconds=1)
        t = pb._probe_next_start("nvr1", 3, X - datetime.timedelta(minutes=33), X + datetime.timedelta(hours=30))
        check("NVR1's 24 h DESCRIBE answers 27 min 49 s short before its oldest recording (measured): the next recorded "
              "moment is still found to the second (checked, then bisected back; a 2 s window starting 1 s before "
              "counts, so it may be 1 s early, never late) -- playback from the oldest recording outside the office "
              f"starts there, not 28 min later ({n_desc[0]} DESCRIBE)", t is not None and X - one <= t <= X, (X, t))
        pb._channels = lambda nvr: [3]
        n_desc[0] = 0
        out = pb._oldest_rtsp("nvr1")
        check(f"... and the RTSP oldest-recording search too ({n_desc[0]} DESCRIBE)",
              set(out) == {3} and X - one <= out[3] <= X, (X, out))
    finally:
        pb._describe, pb._channels = real_desc, real_ch
    pb._RETENTION.clear()                                # the other tests: not known (fixed fallback)


def test_search_lan_and_remote():
    for mode in (False, True):
        MODE["remote"] = mode
        pb._CAPS.clear()
        code, d = search([HR, GAPCAM, NONECAM, PARTCAM], M(5), M(40))
        st = {t["index"]: t for t in d.get("tiles", [])}
        name = "remote (RTSP only)" if mode else "office LAN (vendor search)"
        if not mode:
            check(f"{name}: continuous camera 'found', gap camera 'partial', empty camera 'none', half camera 'partial'",
                  code == 200 and st[HR]["availability"] == "found" and st[GAPCAM]["availability"] == "partial"
                  and st[NONECAM]["availability"] == "none" and st[PARTCAM]["availability"] == "partial",
                  {k: v["availability"] for k, v in st.items()})
            segs = st[GAPCAM]["segments"]
            check("LAN: the gap camera's segments show the real gap (+10:00 -> +10:08)",
                  segs == [[ms(M(5)), ms(M(10))], [ms(M(10, 8)), ms(M(40))]] and st[HR]["method"] == "vendor", segs)
        else:
            # RTSP only: NVR2's DESCRIBE hides inner gaps (found while playing); NVR1 reads a window
            # starting in a gap as empty -> probed further: 'partial' (recording starts later)
            check(f"{name}: continuous 'found', gap camera 'found' (inner gaps show while playing), empty "
                  "'none', NVR1 camera whose recording starts after From 'partial' (not 'No recording')",
                  code == 200 and st[HR]["availability"] == "found" and st[GAPCAM]["availability"] == "found"
                  and st[NONECAM]["availability"] == "none" and st[PARTCAM]["availability"] == "partial",
                  {k: v["availability"] for k, v in st.items()})
            check("remote: no segments, no recorded-seconds claim, method rtsp",
                  st[GAPCAM]["segments"] is None and st[GAPCAM]["method"] == "rtsp"
                  and st[GAPCAM]["recordedS"] is None, st[GAPCAM])
        check(f"{name}: 'No recording found for this camera and time range.'",
              st[NONECAM]["msg"] == "No recording found for this camera and time range.", st[NONECAM]["msg"])
        http("POST", "/api/playback/close", {"sid": d["sid"]})
    MODE["remote"] = False
    pb._CAPS.clear()


def test_one_camera_start_end():
    a, b = M(5), M(5, 12)
    n0 = len(FAKE["nvr2"].play_log)
    code, d = search([HR], a, b)
    time.sleep(0.8)                                   # a slow page (< the 1 s test grace): the timeline must not run
    w = PWs(d["sid"])
    ok = wait(lambda: w.tile(0).get("state") == "PLAYING" and len(w.frames) > 10, 8.0)
    check("one camera: search -> WebSocket -> PLAYING, frames arrive", ok, w.tile(0))
    check("... the timeline waits at the From time until the camera shows it (first state: posMs == From)",
          w.states and w.states[0][1]["posMs"] == ms(a), w.states[0][1]["posMs"] - ms(a) if w.states else None)
    plays = FAKE["nvr2"].play_log[n0:]
    check("the NVR got PLAY Range: clock=<UTC> of the From time (IST converted, not local digits), even 0.8 s later",
          plays and plays[0]["range"] == f"clock={pt.rtsp_clock(a)}-", plays[:1])
    tr = truth_of("[Playback")
    first = tr[0] if tr else (None, None, 0)
    check("first frame is the recording at the From time (camera clock == From, key frame at/before)",
          first[1] is not None and 0 <= ms(a) - first[1] <= 2000, (first[1] - ms(a)) if first[1] else None)
    errs = [abs(s - t) for s, t, _ in tr if s is not None and t is not None]
    check(f"displayed time vs camera clock: max error {max(errs) / 1000 if errs else '?'} s (<= 1.0 s: half a GOP)",
          errs and max(errs) <= 1000, max(errs) if errs else None)
    ok = wait(lambda: w.tile(0).get("state") == "ENDED", 20.0)
    last = max((t for _, t, _ in truth_of("[Playback")), default=0)
    check("playback stops by itself at the To time: 'Playback ended' (no live footage after the end)",
          ok and ms(b) - 200 <= last + 40 <= ms(b) + 200, (w.tile(0).get("state"), (last - ms(b)) / 1000))
    last_decoded = max((s for s, _, _ in truth_of("[Playback") if s is not None), default=0)
    shown = lambda: max((f[2] for f in w.frames if f[1] == 0), default=0)        # noqa: E731
    check("... the last picture shown is the recording's last one (not skipped by the frame-rate limit)",
          wait(lambda: shown() == last_decoded, 2.0), (last_decoded - shown()) / 1000)
    check("... NVR session closed, NVR slot released, worker stopped", all_stopped(), (pb.running_workers(), pb_owners("nvr2")))
    close_session(w, d["sid"])


def test_multi_camera_to_end():
    """Both real NVRs step their RTP clock BACK ~1 s just before the To time: that is no
    gap. Two cameras playing to the end must both say 'Playback ended' (a false gap made
    the second camera PAUSE a closing NVR session -> 'Playback connection failed')."""
    code, d = search([HR, CORR], M(5, 50), M(6))
    w = PWs(d["sid"])
    ok = wait(lambda: all(w.tile(i).get("state") == "ENDED" for i in (0, 1)), 25.0)
    check("two cameras (NVR1 + NVR2) play to the To time: both 'Playback ended', no gap note",
          ok and all(w.tile(i).get("msg") == "Playback ended" for i in (0, 1)) and not w.state().get("notes"),
          ([(w.tile(i).get("state"), w.tile(i).get("msg")) for i in (0, 1)], w.state().get("notes")))
    close_session(w, d["sid"])


def test_seek_pause_speed():
    a, b = M(20), M(30)
    TRUTH.clear()
    code, d = search([HR], a, b)
    w = PWs(d["sid"])
    wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    n0 = len(FAKE["nvr2"].play_log)
    target = M(25)
    t0 = time.monotonic()
    w.send({"op": "seek", "ms": ms(target)})
    ok = wait(lambda: any(t >= t0 and abs(tm - ms(target)) <= 2000 for s, tm, t in truth_of("[Playback")), 5.0)
    check("seek forward: PLAY clock=<UTC of the target> sent, footage from the target (camera clock +-2 s)",
          ok and FAKE["nvr2"].play_log[n0]["range"] == f"clock={pt.rtsp_clock(target)}-", FAKE["nvr2"].play_log[n0:][:1])
    time.sleep(0.6)
    after = [(s, tm) for s, tm, t in truth_of("[Playback") if t >= t0 and tm >= ms(target) - 2000]
    check("... displayed time follows the camera clock after the seek (<= 1 s)",
          after and max(abs(s - tm) for s, tm in after) <= 1000,
          [((s - tm) / 1000, (tm - ms(target)) / 1000) for s, tm in after[:3]])
    back = M(21)
    t1 = time.monotonic()
    w.send({"op": "seek", "ms": ms(back)})
    ok = wait(lambda: any(t >= t1 and abs(tm - ms(back)) <= 2000 for s, tm, t in truth_of("[Playback")), 5.0)
    check("seek backward: footage from the earlier time", ok)
    time.sleep(0.5)
    w.send({"op": "pause"})
    wait(lambda: w.tile(0).get("state") == "PAUSED", 3.0)
    time.sleep(0.3)
    n = len(truth_of("[Playback"))
    time.sleep(1.0)
    check("pause: state PAUSED, no new frames", w.tile(0).get("state") == "PAUSED" and len(truth_of("[Playback")) == n,
          (w.tile(0).get("state"), len(truth_of("[Playback")) - n))
    last_ms = truth_of("[Playback")[-1][1]
    w.send({"op": "play"})
    ok = wait(lambda: len(truth_of("[Playback")) > n + 5, 4.0)
    resumed = [tm for s, tm, t in truth_of("[Playback")[n:]]
    check("resume: continues where it paused (no jump)", ok and resumed and 0 <= resumed[0] - last_ms <= 1500,
          (resumed[0] - last_ms) if resumed else None)
    n1 = len(FAKE["nvr2"].play_log)
    ta = time.monotonic()
    na = len(w.audio)
    w.send({"op": "audio", "tile": 0})
    w.send({"op": "speed", "x": 2})
    time.sleep(3.0)
    fast = [(tm, t) for s, tm, t in truth_of("[Playback") if t >= ta + 0.5]
    rate = (fast[-1][0] - fast[0][0]) / 1000 / (fast[-1][1] - fast[0][1]) if len(fast) > 1 else 0
    check(f"2x: PLAY with Scale 2.0; key frames only; footage advances {rate:.1f}x real time",
          FAKE["nvr2"].play_log[n1:] and FAKE["nvr2"].play_log[n1]["scale"] == 2.0 and 1.6 <= rate <= 2.6
          and len(fast) <= 5, (FAKE["nvr2"].play_log[n1:][:1], rate, len(fast)))
    check("... no audio at 2x (the NVR sends none)", not [x for x in w.audio[na:] if x[0] >= ta + 1.0])
    w.send({"op": "speed", "x": 4})
    time.sleep(2.2)
    fast = [(tm, t) for s, tm, t in truth_of("[Playback") if t >= time.monotonic() - 1.8]
    rate4 = (fast[-1][0] - fast[0][0]) / 1000 / (fast[-1][1] - fast[0][1]) if len(fast) > 1 else 0
    check(f"4x: footage advances {rate4:.1f}x real time", 3.0 <= rate4 <= 5.5, rate4)
    w.send({"op": "speed", "x": 1})
    w.send({"op": "speed", "x": 0.5})
    time.sleep(0.8)
    check("0.5x is refused (not offered: the NVR stops after one frame)", w.state().get("speed") == 1, w.state().get("speed"))
    close_session(w, d["sid"])


def test_clock_jump_without_gap():
    """Real NVR2: starting in the last GOP before its hourly file change, the RTP clock
    jumped +2.04 s at the change while the camera clock ran on (no recording gap)."""
    a, b = M(14), M(14, 12)
    FAKE["nvr2"].ts_jumps[8] = [M(14, 3)]
    TRUTH.clear()
    try:
        code, d = search([HR], a, b)
        w = PWs(d["sid"])
        ok = wait(lambda: any(tm is not None and tm >= ms(M(14, 7)) for s, tm, t in truth_of("[Playback")), 12.0)
        after = [(s, tm) for s, tm, t in truth_of("[Playback") if tm is not None and tm >= ms(M(14, 3, ))]
        err = max((abs(s - tm) for s, tm in after), default=None)
        check(f"RTP clock jump of 2 s without a recording gap: displayed time still follows the camera clock "
              f"(max error {err and err / 1000} s <= 1 s), no 'gap skipped' note",
              ok and after and err <= 1000 and not [n for n in w.state().get("notes", []) if "gap" in n["text"]],
              (err, w.state().get("notes")))
        close_session(w, d["sid"])
    finally:
        FAKE["nvr2"].ts_jumps.clear()


def test_audio_one_camera():
    code, d = search([HR, CORR], M(40), M(50))
    w = PWs(d["sid"])
    wait(lambda: all(w.tile(k).get("state") == "PLAYING" for k in (0, 1)), 8.0)
    time.sleep(0.5)
    check("audio is off until asked: no audio packets", not w.audio)
    w.send({"op": "audio", "tile": 0})
    ok = wait(lambda: len(w.audio) > 10, 4.0)
    check("audio of camera 1: packets for tile 0 only, PCMA (codec 8, NVR2)",
          ok and {x[1] for x in w.audio} == {0} and {x[2] for x in w.audio} == {8})
    n = len(w.audio)
    w.send({"op": "audio", "tile": 1})
    ok = wait(lambda: any(x[1] == 1 for x in w.audio[n:]), 4.0)
    time.sleep(0.5)
    check("switch to camera 2: only tile 1 now, PCMU (codec 0, NVR1) -- never two at once",
          ok and {x[1] for x in w.audio[n + 5:]} == {1} and {x[2] for x in w.audio[n + 5:]} == {0})
    w.send({"op": "audio", "tile": -1})
    time.sleep(0.4)
    n = len(w.audio)
    time.sleep(0.6)
    check("audio off: no more packets", len(w.audio) == n)
    close_session(w, d["sid"])


def test_gap_lan():
    TRUTH.clear()
    code, d = search([GAPCAM], M(8), M(16))
    w = PWs(d["sid"])
    wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    w.send({"op": "seek", "ms": ms(M(10, 4))})
    ok = wait(lambda: w.tile(0).get("state") == "GAP", 4.0)
    g = w.tile(0).get("gap") or {}
    check("seek INTO a gap: 'No recording at this exact time' with previous / next recording",
          ok and w.tile(0).get("msg") == "No recording at this exact time"
          and g.get("next") == ms(M(10, 8)) and g.get("prev") == ms(M(10)), w.tile(0))
    t0 = time.monotonic()
    w.send({"op": "seek", "ms": g.get("next")})
    ok = wait(lambda: any(t >= t0 and tm >= ms(M(10, 8)) for s, tm, t in truth_of("[Playback"))
              and w.tile(0).get("state") == "PLAYING", 4.0)      # (the state reaches the page just after the frame)
    check("'Next recording': plays from the recording after the gap", ok, w.tile(0))
    w.send({"op": "seek", "ms": ms(M(9, 57))})
    t0b = time.monotonic()
    # frames of the previous position may still arrive for a moment: start from the new one
    wait(lambda: any(t >= t0b and tm < ms(M(10)) for s, tm, t in truth_of("[Playback")), 5.0)
    t1 = next((t for s, tm, t in truth_of("[Playback") if t >= t0b and tm < ms(M(10))), time.monotonic())
    ok = wait(lambda: any(t >= t1 and tm >= ms(M(10, 8)) for s, tm, t in truth_of("[Playback")), 16.0)
    seen = [tm for s, tm, t in truth_of("[Playback") if t >= t1]
    jump = next((b2 - a2 for a2, b2 in zip(seen, seen[1:]) if b2 - a2 > 3000), None)
    check("one camera crossing the gap: continues after it (camera time jumps 8 s), 'Recording gap skipped' note",
          ok and jump and 7000 <= jump <= 9000 and any(n["text"] == "Recording gap skipped" for n in w.state().get("notes", [])),
          (jump, w.state().get("notes"), w.tile(0).get("state"), w.tile(0).get("msg"),
           [(tm - ms(M(10))) / 1000 for tm in seen[:2] + seen[-2:]], FAKE["nvr2"].play_log[-2:]))
    shown = [(s, tm) for s, tm, t in truth_of("[Playback") if t >= t1 and tm >= ms(M(10, 8))]
    check("... displayed time right after the gap (<= 1 s)", shown and max(abs(s - tm) for s, tm in shown) <= 1000)
    close_session(w, d["sid"])


def test_gap_multi_camera_sync():
    TRUTH.clear()
    code, d = search([GAPCAM, HR], M(9, 50), M(11))
    w = PWs(d["sid"])
    ok = wait(lambda: w.tile(0).get("state") == "GAP_WAIT", 20.0)
    hold = w.tile(0).get("gap") or {}
    check("two cameras, one crosses a gap: it WAITS ('No recording until ...') while the other plays",
          ok and w.tile(1).get("state") == "PLAYING" and hold.get("next") and abs(hold["next"] - ms(M(10, 8))) <= 1000,
          (w.tile(0), w.tile(1).get("state")))
    ok = wait(lambda: w.tile(0).get("state") == "PLAYING", 14.0)
    both = w.state()
    diff = abs((w.tile(0).get("recMs") or 0) - (w.tile(1).get("recMs") or 0))
    check(f"... it resumes when the shared timeline reaches its recording; cameras in sync (difference {diff / 1000:.1f} s)",
          ok and diff <= 2500, (w.tile(0), w.tile(1).get("recMs")))
    close_session(w, d["sid"])


def test_gap_remote_probe():
    MODE["remote"] = True
    pb._CAPS.clear()
    code, d = search([GAPCAM], M(8), M(16))
    w = PWs(d["sid"])
    wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    w.send({"op": "seek", "ms": ms(M(10, 3))})
    ok = wait(lambda: w.tile(0).get("state") == "GAP", 8.0)
    g = w.tile(0).get("gap") or {}
    check("remote (no segment list): a seek into a gap is detected over RTSP; next recording found (+-1 s)",
          ok and g.get("next") and abs(g["next"] - ms(M(10, 8))) <= 1000, w.tile(0))
    close_session(w, d["sid"])
    MODE["remote"] = False
    pb._CAPS.clear()


def test_nvr1_gap_ends_session():
    """Real NVR1: its playback session ends (BYE + close) at a recording gap and cannot
    PLAY past it (500 + close): the recording after a gap needs a new session."""
    for remote in (False, True):
        MODE["remote"] = remote
        pb._CAPS.clear()
        where = "remote (RTSP only)" if remote else "LAN"
        TRUTH.clear()
        code, d = search([GAP1CAM], M(19, 50), M(21))
        w = PWs(d["sid"])
        ok = wait(lambda: any(tm is not None and tm >= ms(M(20, 33)) for s, tm, t in truth_of("[Playback")), 20.0)
        after = [(s, tm) for s, tm, t in truth_of("[Playback") if tm is not None and tm >= ms(M(20, 30))]
        first_after = min((tm for s, tm in after), default=None)
        check(f"NVR1 ({where}), one camera reaches a gap (the NVR ends its session): a new session plays the "
              f"recording after it from its start (+-2 s, nothing skipped), 'Recording gap skipped', displayed "
              f"time follows (<= 1 s)",
              ok and after and first_after <= ms(M(20, 32)) and max(abs(s - tm) for s, tm in after) <= 1000
              and w.tile(0).get("state") == "PLAYING"
              and any(n["text"] == "Recording gap skipped" for n in w.state().get("notes", [])),
              (w.tile(0), w.state().get("notes"), first_after and (first_after - ms(M(20, 30))) / 1000))
        close_session(w, d["sid"])
    MODE["remote"] = False
    pb._CAPS.clear()
    TRUTH.clear()
    code, d = search([GAP1CAM], M(19), M(22))
    w = PWs(d["sid"])
    wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    t0 = time.monotonic()
    w.send({"op": "seek", "ms": ms(M(21))})
    ok = wait(lambda: any(t >= t0 and abs(tm - ms(M(21))) <= 2000 for s, tm, t in truth_of("[Playback"))
              and w.tile(0).get("state") == "PLAYING", 8.0)
    check("NVR1: seek past a gap (PLAY refused in the old session) -> new session, plays the target", ok, w.tile(0))
    t1 = time.monotonic()
    w.send({"op": "seek", "ms": ms(M(19, 30))})
    ok = wait(lambda: any(t >= t1 and abs(tm - ms(M(19, 30))) <= 2000 for s, tm, t in truth_of("[Playback"))
              and w.tile(0).get("state") == "PLAYING", 8.0)
    check("NVR1: ... and back before the gap (outside the new session's window) -> plays", ok, w.tile(0))
    w.send({"op": "seek", "ms": ms(M(20, 10))})
    ok = wait(lambda: w.tile(0).get("state") == "GAP", 6.0)
    g = w.tile(0).get("gap") or {}
    t2 = time.monotonic()
    w.send({"op": "seek", "ms": g.get("next") or 0})
    ok = ok and wait(lambda: any(t >= t2 and tm >= ms(M(20, 30)) for s, tm, t in truth_of("[Playback"))
                     and w.tile(0).get("state") == "PLAYING", 8.0)
    check("NVR1: seek into the gap -> 'No recording at this exact time'; 'Next recording' plays after it",
          ok and g.get("next") == ms(M(20, 30)), (g, w.tile(0)))
    close_session(w, d["sid"])
    code, d = search([GAP1CAM, HR], M(19, 50), M(21))
    w = PWs(d["sid"])
    ok = wait(lambda: w.tile(0).get("state") == "GAP_WAIT", 20.0)
    check("NVR1 camera + NVR2 camera: the NVR1 one reaches its gap -> waits ('No recording until ...') while "
          "the other plays", ok and w.tile(1).get("state") == "PLAYING" and "No recording until" in (w.tile(0).get("msg") or ""),
          (w.tile(0), w.tile(1).get("state")))
    ok = wait(lambda: w.tile(0).get("state") == "PLAYING" and (w.tile(0).get("recMs") or 0) >= ms(M(20, 29)), 45.0)
    diff = abs((w.tile(0).get("recMs") or 0) - (w.tile(1).get("recMs") or 0))
    check(f"... new NVR1 session when the timeline reaches the next recording; cameras in sync ({diff / 1000:.1f} s)",
          ok and diff <= 2500, (w.tile(0), w.tile(1).get("recMs")))
    close_session(w, d["sid"])


def test_long_range():
    """Several days as ONE timeline: the recording search is split into NVR-safe requests
    and merged (gaps kept, duplicates removed); playback runs as consecutive NVR
    sessions -- across midnight and across a session boundary by itself."""
    a, b = D(1, 6), D(4, 6)
    want = [[ms(D(1, 6)), ms(D(2, 13))], [ms(D(2, 15)), ms(D(4, 6))]]
    pb._SEARCH_CACHE.clear()
    n0 = len(API["nvr2"].calls)
    code, d = search([LONG2], a, b, wait=30)
    calls = [c[1] for c in API["nvr2"].calls[n0:]]
    t = d["tiles"][0] if code == 200 else {}
    check("72 h search (default 7-day request): one NVR query, results PAGED (140 files, 100 per page), one "
          "timeline with the real 2 h gap on day 2", code == 200 and calls.count("findFile") == 1
          and calls.count("findNextFile") == 2 and t.get("segments") == want and t.get("availability") == "partial",
          (calls, t.get("segments")))
    http("POST", "/api/playback/close", {"sid": d.get("sid")})
    old = pb.SEARCH_CHUNK_H
    try:
        pb.SEARCH_CHUNK_H = 24.25                    # cuts through 30 min files: they come twice
        pb._SEARCH_CACHE.clear()
        n0 = len(API["nvr2"].calls)
        code, d = search([LONG2], a, b, wait=30)
        calls = [c[1] for c in API["nvr2"].calls[n0:]]
        t = d["tiles"][0] if code == 200 else {}
        check("... split into 24.25 h requests: 3 NVR queries, overlapping files deduplicated, the SAME timeline",
              code == 200 and calls.count("findFile") == 3 and t.get("segments") == want, (calls.count("findFile"), t.get("segments")))
        http("POST", "/api/playback/close", {"sid": d.get("sid")})
    finally:
        pb.SEARCH_CHUNK_H = old
        pb._SEARCH_CACHE.clear()
    # playback: day 2 23:59:50 -> day 3 00:00:20 across midnight, NVR sessions of only 8 s
    old = pb.SESSION_SPAN_H
    FAKE["nvr2"].end_step_back = False       # (that end-of-window quirk has its own test; with 8 s
    try:                                     #  sessions it would shift ~1 s of labels every 8 s)
        pb.SESSION_SPAN_H = 8 / 3600
        TRUTH.clear()
        code, d = search([LONG2], D(2, 23, 59, 50), D(3, 0, 0, 20), wait=30)
        w = PWs(d["sid"])
        ok = wait(lambda: any(tm is not None and tm >= ms(D(3, 0, 0, 14)) for s, tm, t in truth_of("[Playback")), 45.0)
        seen = sorted({tm for s, tm, t in truth_of("[Playback") if tm is not None})
        hole = max((y - x for x, y in zip(seen, seen[1:])), default=None)
        errs = [abs(s - tm) for s, tm, t in truth_of("[Playback") if s is not None and tm is not None]
        opens = FAKE["nvr2"].play_log
        check("across midnight AND across NVR sessions (a new one every 8 s): the footage runs on by itself "
              f"(largest step {hole} ms), shown time = camera time (<= 1 s), no gap note, no Search click",
              ok and hole is not None and hole <= 1000 and errs and max(errs) <= 1000
              and not [n for n in w.state().get("notes", []) if "gap" in n["text"]]
              and w.tile(0).get("state") == "PLAYING", (hole, max(errs) if errs else None, w.state().get("notes")))
        close_session(w, d["sid"])
    finally:
        pb.SESSION_SPAN_H = old
        FAKE["nvr2"].end_step_back = True
    # far seeks inside a 72 h range: each lands in its own NVR session (24 h at most)
    TRUTH.clear()
    code, d = search([LONG2], a, b, wait=30)
    w = PWs(d["sid"])
    wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    res = []
    for target in (D(3, 12), D(1, 7), D(2, 14)):
        t0 = time.monotonic()
        w.send({"op": "seek", "ms": ms(target)})
        if target == D(2, 14):                       # inside the 2 h gap -> next recording
            ok = wait(lambda: w.tile(0).get("state") == "GAP", 6.0)
            g = w.tile(0).get("gap") or {}
            res.append(ok and g.get("next") == ms(D(2, 15)) and g.get("prev") == ms(D(2, 13)))
        else:
            res.append(wait(lambda: any(t >= t0 and abs(tm - ms(target)) <= 2000 for s, tm, t in truth_of("[Playback")), 8.0))
    check("72 h: seek to day 3 12:00, back to day 1 07:00 (new NVR sessions), into the day-2 gap -> 'No recording at "
          "this exact time' with the recordings before/after it", all(res), res)
    close_session(w, d["sid"])
    MODE["remote"] = True
    pb._CAPS.clear()
    code, d = search([LONG1, LONG2], a, b, wait=30)
    st = {t["index"]: t["availability"] for t in d.get("tiles", [])}
    check("RTSP only (outside the office), 72 h: NVR1 (seamless hourly files) 'found', NVR2 'found' (gaps show while playing)",
          code == 200 and st.get(LONG1) == "found" and st.get(LONG2) == "found", st)
    http("POST", "/api/playback/close", {"sid": d.get("sid")})
    MODE["remote"] = False
    pb._CAPS.clear()


def test_progressive_search():
    """A slow NVR search (many cameras x days): the reply comes at once with the cameras
    'searching' (metadata only, no video yet); each result follows over the WebSocket
    and a camera starts playing as soon as its recording is known."""
    for api in API.values():
        api.delay_s = 0.6
    pb._SEARCH_CACHE.clear()
    try:
        t0 = time.monotonic()
        code, d = search([LONG2, LONG1, HR, CORR], D(1, 6), D(4, 6), wait=0)
        dt = time.monotonic() - t0
        check(f"search reply after {dt:.2f} s with every camera still 'searching', no video opened",
              code == 200 and dt < 1.0 and d.get("searching") == 4 and all(t["availability"] == "searching" for t in d["tiles"])
              and not pb.running_workers(), (dt, d.get("searching")))
        w = PWs(d["sid"])
        ok = wait(lambda: {t["id"] for _, m in w.avails for t in m["tiles"]} >= {0, 1, 2, 3}, 15.0)
        got = {t["id"]: t for _, m in w.avails for t in m["tiles"]}
        check("... results arrive over the WebSocket ('avail'): day-camera partial with its segments, NVR1 found, "
              "today-only cameras 'No recording'", ok and got[0]["availability"] == "partial" and got[0]["segments"]
              and got[1]["availability"] == "found" and got[2]["availability"] == "none" and got[3]["availability"] == "none",
              {k: v["availability"] for k, v in got.items()})
        ok = wait(lambda: w.tile(0).get("state") == "PLAYING" and w.tile(1).get("state") == "PLAYING", 10.0)
        check("... and the cameras with recordings start playing by themselves", ok, (w.tile(0), w.tile(1)))
        close_session(w, d["sid"])
    finally:
        for api in API.values():
            api.delay_s = 0.0
        pb._SEARCH_CACHE.clear()


def test_audio_speed_cycle():
    """1x audio on -> 4x (the NVR sends no sound) -> back to 1x: sound comes back by
    itself; seek / pause / resume keep it."""
    code, d = search([HR], M(40), M(50))
    w = PWs(d["sid"])
    wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    w.send({"op": "audio", "tile": 0})
    ok1 = wait(lambda: len(w.audio) > 5, 4.0)
    w.send({"op": "speed", "x": 4})
    time.sleep(1.5)
    n = len(w.audio)
    time.sleep(1.5)
    none4 = len(w.audio) == n
    w.send({"op": "speed", "x": 1})
    n = len(w.audio)
    back = wait(lambda: len(w.audio) > n + 5, 5.0)
    t0 = time.monotonic()
    w.send({"op": "seek", "ms": ms(M(45))})
    seek_ok = wait(lambda: any(x[0] > t0 + 0.5 for x in w.audio), 5.0)
    w.send({"op": "pause"})
    time.sleep(1.0)
    n = len(w.audio)
    time.sleep(1.0)
    paused_quiet = len(w.audio) == n
    w.send({"op": "play"})
    resumed = wait(lambda: len(w.audio) > n + 3, 5.0)
    check("audio at 1x -> none at 4x -> back at 1x by itself -> after a seek -> none while paused -> back on resume",
          ok1 and none4 and back and seek_ok and paused_quiet and resumed, (ok1, none4, back, seek_ok, paused_quiet, resumed))
    close_session(w, d["sid"])


def test_capacity():
    two_nvr2 = [IDX[("nvr2", c)] for c in (3, 4, 5)]
    code, d = search(two_nvr2, M(30), M(40))
    w = PWs(d["sid"])
    ok = wait(lambda: sum(w.tile(k).get("state") == "PLAYING" for k in range(3)) == 2, 8.0)
    time.sleep(0.5)
    third = [w.tile(k) for k in range(3) if w.tile(k).get("state") != "PLAYING"]
    check("per-NVR limit (NVR2 = 2): 2 play, the 3rd says 'NVR2 playback limit reached (2)...'",
          ok and len(third) == 1 and third[0]["state"] == "CAPACITY" and "NVR2 playback limit" in third[0]["msg"], third)
    check("... never more than 2 playback sessions on the fake NVR2", FAKE["nvr2"].pb_peak <= 2, FAKE["nvr2"].pb_peak)
    stop_id = next(k for k in range(3) if w.tile(k).get("state") == "PLAYING")
    w.send({"op": "stop", "tile": stop_id})
    wait(lambda: w.tile(stop_id).get("state") == "STOPPED", 4.0)
    wait(lambda: len(pb.running_workers("nvr2")) <= 1, 4.0)
    w.send({"op": "retry", "tile": third[0]["id"]})
    ok = wait(lambda: w.tile(third[0]["id"]).get("state") == "PLAYING", 6.0)
    check("stop one camera -> 'Try again' on the waiting one plays it", ok, w.tile(third[0]["id"]))
    close_session(w, d["sid"])
    mix = [IDX[("nvr2", 3)], IDX[("nvr2", 4)], IDX[("nvr1", 5)], IDX[("nvr1", 6)]]
    code, d = search(mix, M(30), M(40))
    w = PWs(d["sid"])
    ok = wait(lambda: sum(w.tile(k).get("state") == "PLAYING" for k in range(4)) == 3, 8.0)
    time.sleep(0.5)
    cap = [w.tile(k) for k in range(4) if w.tile(k).get("state") == "CAPACITY"]
    check("server limit (3): 3 play, the 4th says 'Playback capacity reached. Stop another playback stream and try again.'",
          ok and len(cap) == 1 and cap[0]["msg"] == "Playback capacity reached. Stop another playback stream and try again.", cap)
    close_session(w, d["sid"])


def test_select_all_paging():
    PB_PEAK["all"] = 0
    code, d = search(list(range(len(S))), M(45), M(55))
    check("Select All: all 25 cameras searched in one request", code == 200 and len(d["tiles"]) == len(S), code)
    w = PWs(d["sid"])
    wait(lambda: len(pb.running_workers()) >= 2, 8.0)          # page 1 = NVR2 cameras: NVR2 limit 2
    time.sleep(1.0)
    st = w.state()
    running = {t["id"] for t in st["tiles"] if t["state"] in ("PLAYING", "OPENING", "SEEKING")}
    check("... only the first page (6) is active and at most 3 cameras play at once (server limit)",
          running <= set(range(6)) and len(pb.running_workers()) <= 3, (running, len(pb.running_workers())))
    w.send({"op": "page", "tiles": list(range(6, 12))})
    ok = wait(lambda: {w2.tile.id for w2 in pb.running_workers()} and all(w2.tile.id >= 6 for w2 in pb.running_workers()), 8.0)
    check("next page: page-1 cameras stop, page-2 cameras play", ok, [w2.tile.id for w2 in pb.running_workers()])
    check(f"... never more than 3 playback workers at any moment (peak {PB_PEAK['all']})", PB_PEAK["all"] <= 3, PB_PEAK)
    close_session(w, d["sid"])


def test_slots_and_live_untouched():
    wait(lambda: len([s for s in S if s.info["nvr"] == "nvr2" and s._bg and s._running]) >= 5, 8.0)
    t0 = time.monotonic()
    code, d = search([HR], M(12), M(20))
    w = PWs(d["sid"])
    ok = wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    dt = time.monotonic() - t0
    keys = pb_owners("nvr2")
    check(f"NVR2 full of background streams: one yields, playback holds an NVR slot (key >= 3000, PLAYBACK) ({dt:.1f} s)",
          ok and len(keys) == 1 and server._worker_by_key(keys[0]).priority_name() == "PLAYBACK", keys)
    close_session(w, d["sid"])
    cams = NVR2[:6]
    vids = [VideoViewer(i) for i in cams]
    wait(lambda: all(S[i].is_live() and S[i].viewers == 1 for i in cams), 8.0)
    time.sleep(0.5)
    t0 = time.monotonic()                                   # the watched cameras are all live now
    code, d = search([IDX[("nvr2", 9)]], M(12), M(20))
    w = PWs(d["sid"])
    ok = wait(lambda: w.tile(0).get("state") == "WAITING_SLOT", 6.0)
    time.sleep(1.0)
    check("NVR2 full of WATCHED live video: playback waits ('Waiting for NVR capacity'), no 7th session",
          ok and w.tile(0).get("msg") == "Waiting for NVR capacity" and FAKE["nvr2"].pb_active == 0
          and all(S[i].is_live() for i in cams), w.tile(0))
    vtr = [(e["to"], e["reason"]) for i in cams for e in S[i].transitions if e["t"] >= t0]
    check("... the watched live cameras are untouched (no transition)", not [x for x in vtr if x[0] != "LIVE"], vtr[:4])
    vids[0].close()
    ok = wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    check("... a live viewer leaves: playback gets the slot and plays", ok, w.tile(0))
    close_session(w, d["sid"])
    for v in vids[1:]:
        v.close()
    check(f"per-NVR cap never exceeded (peak {PEAK})", all(PEAK[k] <= server.NVR_CAP[k] for k in server.NVRS), PEAK)


def test_live_same_camera_independent():
    i = CORR
    vid = VideoViewer(i, "prio=full")
    wait(lambda: S[i].is_live(), 6.0)
    t0 = time.monotonic()
    code, d = search([i], M(33), M(35))
    w = PWs(d["sid"])
    ok = wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    time.sleep(1.0)
    vtr = [(e["to"], e["reason"]) for e in S[i].transitions if e["t"] >= t0]
    check("live view of the same camera keeps running untouched while its recording plays (separate worker)",
          ok and S[i].is_live() and not vtr and S[i].viewers == 1, vtr)
    close_session(w, d["sid"])
    vid.close()


def test_cleanup():
    code, d = search([HR, CORR], M(50), M(59))
    w = PWs(d["sid"])
    wait(lambda: len(pb.running_workers()) == 2, 8.0)
    w.close()                                            # page closed without the close call
    ok = all_stopped(6.0)
    check("page gone (WebSocket closed): after the grace period every worker stops, NVR sessions and slots released",
          ok and pb.MANAGER.get(d["sid"]) is None, (pb.running_workers(), FAKE["nvr2"].pb_active))
    code, d = search([HR], M(50), M(59))
    w = PWs(d["sid"])
    wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    code, d2 = search([CORR], M(50), M(59), replaces=d["sid"])
    ok = wait(lambda: pb.MANAGER.get(d["sid"]) is None and not [x for x in pb.running_workers() if x.session.sid == d["sid"]], 4.0)
    check("a new search replaces the previous session at once (range / cameras changed)", ok)
    w.close()
    http("POST", "/api/playback/close", {"sid": d2["sid"]})
    code, d = search([HR], M(50), M(59))
    w = PWs(d["sid"])
    wait(lambda: w.tile(0).get("state") == "PLAYING", 8.0)
    w.send({"op": "pause"})
    ok = wait(lambda: FAKE["nvr2"].pb_active == 0 and not pb_owners("nvr2") or pb.running_workers() and
              pb.running_workers()[0].state == "PAUSED" and FAKE["nvr2"].pb_active == 0, 6.0)
    check("paused longer than the hold time: the NVR session is closed (no idle RTSP session)", FAKE["nvr2"].pb_active == 0,
          FAKE["nvr2"].pb_active)
    w.send({"op": "play"})
    ok = wait(lambda: w.tile(0).get("state") == "PLAYING" and FAKE["nvr2"].pb_active == 1, 8.0)
    check("... Play reopens it at the paused position", ok, w.tile(0))
    pb.MANAGER.close_all("SHUTDOWN")
    check("server shutdown: every playback session is torn down", all_stopped(6.0))
    w.close()
    w = PWs(d["sid"])
    ok = wait(lambda: any(m.get("t") == "error" for _, m in w.states), 3.0)
    check("reconnecting to a session that no longer exists (server restarted): clean 'search again' message",
          ok and "Search again" in next(m["msg"] for _, m in w.states if m.get("t") == "error"))
    w.close()


def test_access_and_secrets():
    if server.TOKEN:
        for path in ("/playback", "/api/playback/config", "/api/playback/nvr-info", "/api/playback/status"):
            code, _ = http("GET", path, key=False)
            check(f"{path} without the access key -> 401", code == 401, code)
        code, _ = http("POST", "/api/playback/search", {"from": "x", "to": "y", "cameras": [HR]}, key=False)
        check("POST /api/playback/search without the key -> 401", code == 401, code)
        w = PWs("x", key=False)
        check("playback WebSocket without the key -> 401", w.status_line and " 401 " in w.status_line, w.status_line)
        w.close()
    code, d = search([HR, CORR], M(20), M(22))
    w = PWs(d["sid"])
    wait(lambda: len(w.frames) > 5, 8.0)
    blobs = [json.dumps(http("GET", "/api/playback/config")[1]), json.dumps(d), json.dumps(w.state()),
             json.dumps(http("GET", "/api/playback/status")[1]), json.dumps(http("GET", "/api/playback/nvr-info")[1]),
             json.dumps(server.system_status())]
    secrets = [x for n in server.NVRS.values() for x in (n["user"], n["pass"]) if x and len(x) >= 4]
    check("no NVR username / password / rtsp:// URL in config, search, WebSocket state, status, NVR info",
          not any(sx in b for sx in secrets for b in blobs) and not any("rtsp://" in b for b in blobs))
    close_session(w, d["sid"])


def test_audit_and_nvr_info():
    path = pb.HOOK.audit_path
    lines = [json.loads(x) for x in open(path, encoding="utf-8")] if os.path.exists(path) else []
    se = [x for x in lines if x["event"] == "search"]
    en = [x for x in lines if x["event"] == "session_end"]
    check("audit log: every search (time, client, cameras, from, to) and every session end is recorded",
          se and en and all({"at", "client", "cameras", "from", "to"} <= set(x) for x in se), len(lines))
    raw = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    secrets = [x for n in server.NVRS.values() for x in (n["user"], n["pass"]) if x and len(x) >= 4]
    check("... and never a credential", not any(sx in raw for sx in secrets))
    pb._NVR_INFO.clear()
    code, info = http("GET", "/api/playback/nvr-info")
    n2 = info["nvrs"]["NVR2"]
    check("NVR info (LAN): model, clock drift measured (-37 s), admin account -> security warning",
          code == 200 and n2["model"] == "FAKE-NVR-4K" and n2["clockDriftS"] in (-38, -37, -36)
          and n2["adminAccount"] and info["securityWarning"], n2)


def test_pages():
    code, p = http("GET", "/playback")
    check("/playback: From/To date+time, presets, camera search, Select all / Clear, Search recordings",
          code == 200 and all(x in p for x in ("id=fromD", "id=fromT", "id=toD", "id=toT", "Last 15 min", "Today",
                                                  "id=camSearch", "Select all", "Clear", "Search recordings")))
    check("... speeds come from the server (1x/2x/4x; no 0.5x anywhere), fast-play note",
          "0.5×" not in p and "0.5x" not in p and "(CFG.speeds || [1, 2, 4])" in p
          and "Fast playback may be less smooth" in p and pb.SPEEDS == (1, 2, 4))
    check("... user messages: no recording / capacity / NVR unreachable / playback ended / seeking",
          all(x in p for x in ("No recording", "Capacity reached", "NVR unreachable", "Playback ended", "Seeking")))
    check("... names inserted with textContent (no innerHTML with camera names)",
          "innerHTML = t" not in p and ".textContent = t.name" in p)
    check("... audio: speaker/mute button + volume + status (available / muted / playing / no recorded audio / 1x only),"
          " quiet microphones raised", all(x in p for x in ("id=audBtn", "id=vol", "Audio available", "Muted",
                                                           "Playing · ", "No recorded audio", "Audio plays at 1× only",
                                                           "quiet microphone raised", "createDynamicsCompressor")))
    check("... long ranges: dated ticks / midnight lines / 'searching' results; limit text from the server",
          all(x in p for x in ("function midnights", "fStamp(", "applyAvail", "searching", "fSpan(CFG.maxRangeH)")))
    check("... how far back follows each NVR's oldest recording: 'footage kept' hint, date pickers start there, "
          "'No recordings that old', the note when the search starts at the oldest recording",
          all(x in p for x in ("footage kept: ", "function keptFor", ".min = ", "No recordings that old: ", "id=sumNote",
                               "refreshKept")))
    code, live = http("GET", "/")
    check("live page: a Playback link next to Settings (the only change to the live page)", 'id=playbackLink' in live)
    code, st = http("GET", "/settings")
    check("settings page: 'Recorded playback' card (NVR status, footage kept, security recommendation)",
          "Recorded playback" in st and "Security recommendation" in st and "footage kept: " in st)


if __name__ == "__main__":
    t_start = time.time()
    threading.Thread(target=_sampler, daemon=True).start()
    srv = server.QuietServer(("127.0.0.1", 0), server.Handler)
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    server.POOL.start()
    order = [n for n in globals() if n.startswith("test_")]
    only = sys.argv[1:]
    for name in order:
        if only and name not in only:
            continue
        print(f"\n-- {name}", flush=True)
        try:
            globals()[name]() if name in ("test_time_rules",) else globals()[name]()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{name} raised {type(e).__name__}: {e}", False)
            pb.MANAGER.close_all("TEST_ERROR")
            all_stopped()
    _RUN["on"] = False
    srv.shutdown()
    print(f"\npeak NVR slots used: {PEAK} (cap {dict(server.NVR_CAP)}); peak playback workers {PB_PEAK['all']}")
    check(f"per-NVR cap never exceeded (peak {PEAK})", all(PEAK[k] <= server.NVR_CAP[k] for k in server.NVRS), PEAK)
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}  ({time.time() - t_start:.1f}s)")
    sys.exit(1 if FAILS else 0)
