"""Camera audio tests -- offline: two fake NVRs (fake_nvr.py) speak real RTSP over
local sockets, video uses a stubbed cv2, browsers are minimal WebSocket clients.

Covers: the RTSP client reading audio from the interleaved channel the NVR ASSIGNS
(the real NVR answers an audio-only SETUP asking 0-1 with 2-3 -- the production bug),
RTCP never taken as audio, the masked handshake log; a silent microphone is PLAYING
("no sound detected"), never a reconnect; PLAY without audio RTP is diagnosed
(NO_AUDIO_PACKETS, "Audio stream unavailable"); camera without audio; one listener (audio-only session, G.711 pass-through);
two listeners share ONE NVR session; a listener leaving; last-listener cleanup after
the linger; quick re-listen reuses the session; switching camera audio; audio
reconnect (NVR ends the stream / read timeout) without touching video; video
unaffected by audio failing; slot accounting (never above the NVR cap, audio waits
when viewers hold every slot, a background stream yields for it); per-NVR audio
limit; quality switch never touches audio; NVR refusing audio-only SETUP; the
Settings override (on/off); access control; status API fields without credentials;
the page's audio manager (muted by default, one camera, volume memory, grace).

Run:  python test_audio.py        (exits non-zero on any failure)
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
import tempfile
import threading

os.environ.update({
    "CCTV_PERSISTENT": "1", "CCTV_PREFLIGHT": "1", "CCTV_NVR_MAX_CONN": "6",
    "CCTV_LOG_EVENTS": os.environ.get("AUDIO_TEST_LOG", "0"),
    "CCTV_SETTINGS_FILE": os.path.join(tempfile.mkdtemp(), "camera-settings.json"),
    "CCTV_WARM_STEP_S": "0.05", "CCTV_MIN_BG_HOT_S": "0.3", "CCTV_BG_SWAP_S": "0.2",
    "CCTV_DEAD_RETRY_S": "2", "CCTV_REFRESH_EVERY_S": "0", "CCTV_IDLE_FPS": "1", "CCTV_STREAM_FPS": "8",
    "CCTV_AUDIO": "1", "CCTV_AUDIO_LINGER_S": "0.8", "CCTV_AUDIO_READ_TIMEOUT_S": "1.5",
    "CCTV_AUDIO_OPEN_TIMEOUT_S": "1.5", "CCTV_AUDIO_MAX_PER_NVR": "2", "CCTV_ORIGINAL_LINGER_S": "0.3",
    "CCTV_AUDIO_SILENCE_S": "1.0",
})
for k in ("CCTV_NVR1_MAX_CONN", "CCTV_NVR2_MAX_CONN"):
    os.environ.pop(k, None)

import numpy as np                                     # noqa: E402


class _Cap:
    """Video: never touches the network."""
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
cv2.CAP_FFMPEG = 0
cv2.IMWRITE_JPEG_QUALITY = 1
cv2.FONT_HERSHEY_SIMPLEX = 0
cv2.LINE_AA = 16
cv2.INTER_AREA = 3
cv2.VideoCapture = _Cap
cv2.resize = lambda frame, size, *a, **k: np.zeros((size[1], size[0], 3), dtype="uint8")
cv2.imencode = lambda ext, frame, *a: (True, memoryview(b"jpeg"))
cv2.putText = lambda *a, **k: None
cv2.getTextSize = lambda text, font, scale, thick: ((int(len(text) * 20 * scale), int(22 * scale)), 5)
cv2.circle = lambda *a, **k: None
sys.modules["cv2"] = cv2

import server                                         # noqa: E402
import rtsp_preflight as rp                           # noqa: E402
from fake_nvr import FakeNvr                          # noqa: E402


class _FakePreflight:
    def __init__(self, alive):
        self.alive, self.ms, self.detail = alive, 0, ""

    def run(self):
        time.sleep(0.03)
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

S, O, A = server.STREAMS, server.ORIG_STREAMS, server.AUDIO
NVR1 = [s.index for s in S if s.info["nvr"] == "nvr1"]
NVR2 = [s.index for s in S if s.info["nvr"] == "nvr2"]
CAP = server.NVR_CAP
# NVR1 channels send mu-law, NVR2 A-law -- like the real site. One channel has no audio.
# Both fakes assign the interleaved channels THEMSELVES like the real NVR (audio-only
# SETUP asking 0-1 -> "interleaved=2-3"); NVR2 sends the real 1024-byte (128 ms) packets.
NO_AUDIO = NVR2[5]
FAKE = {
    "nvr1": FakeNvr(server.NVRS["nvr1"]["user"], server.NVRS["nvr1"]["pass"],
                    {S[i].info["channel"]: "PCMU" for i in NVR1}),
    "nvr2": FakeNvr(server.NVRS["nvr2"]["user"], server.NVRS["nvr2"]["pass"],
                    {S[i].info["channel"]: (None if i == NO_AUDIO else "PCMA") for i in NVR2}, packet_bytes=1024),
}
server._audio_endpoint = lambda nvr: ("127.0.0.1", FAKE[nvr].port)
CH = {i: S[i].info["channel"] for i in range(len(S))}

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


def q(extra=""):
    parts = ([f"key={server.TOKEN}"] if server.TOKEN else []) + ([extra] if extra else [])
    return ("?" + "&".join(parts)) if parts else ""


class Ws(threading.Thread):
    """A browser's audio WebSocket: records states (text) and packets (binary)."""

    def __init__(self, port, i, full=False, key=True):
        super().__init__(daemon=True)
        self.port, self.i, self.full, self.key = port, i, full, key
        self.states, self.packets, self.status_line = [], [], None
        self.sock, self.stop, self.closed = None, False, False
        self.start()
        wait(lambda: self.status_line is not None, 5.0)

    def run(self):
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            self.sock = s
            path = f"/audio/{self.i}" + (q("prio=full" if self.full else "") if self.key else "")
            s.sendall((f"GET {path} HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\n"
                       f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
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
                    op, n = buf[0] & 0x0F, buf[1] & 0x7F
                    off = 2
                    if n == 126:
                        if len(buf) < 4:
                            break
                        n, off = struct.unpack(">H", buf[2:4])[0], 4
                    if len(buf) < off + n:
                        break
                    data, buf = buf[off:off + n], buf[off + n:]
                    t = time.monotonic()
                    if op == 1:
                        self.states.append((t, json.loads(data)))
                    elif op == 2:
                        self.packets.append((t, data))
                    elif op == 9:
                        self._send(0xA, data)
                    elif op == 8:
                        self.closed = True
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
        body = bytes(b ^ mask[k % 4] for k, b in enumerate(data))
        self.sock.sendall(bytes([0x80 | op, 0x80 | len(data)]) + mask + body)

    def state(self):
        return self.states[-1][1].get("state") if self.states else None

    def seen(self, st):
        return any(m.get("state") == st for _, m in self.states)

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
    """A tile's MJPEG stream (keeps a video worker viewed)."""

    def __init__(self, port, i, extra=""):
        super().__init__(daemon=True)
        self.port, self.i, self.extra, self.sock, self.stop, self.n = port, i, extra, None, False, 0
        self.start()

    def run(self):
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            self.sock = s
            s.sendall(f"GET /stream/{self.i}{q(self.extra)} HTTP/1.1\r\nHost: t\r\n\r\n".encode())
            while not self.stop:
                c = s.recv(65536)
                if not c:
                    break
                self.n += c.count(b"--frame")
        except OSError:
            pass

    def close(self):
        self.stop = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        except (OSError, AttributeError):
            pass


def plays(i):
    return FAKE[S[i].info["nvr"]].plays.get(CH[i], 0)


def streaming(nvr):
    return FAKE[nvr].streaming()


def stopped(i, timeout=4.0):
    return wait(lambda: not A[i]._running and A[i].slot_key not in owners(S[i].info["nvr"])
                and A[i]._worker_done.is_set(), timeout)


# ── tests ──────────────────────────────────────────────────────────────────────────
def test_rtsp_client_follows_nvr_channels(port=None):
    """The RTSP client alone, against NVRs that assign the interleaved channels differently."""
    import rtsp_audio as ra
    user, pw = server.NVRS["nvr2"]["user"], server.NVRS["nvr2"]["pass"]
    cases = [("real NVR (audio-only SETUP asked 0-1, answered 2-3)", "dahua", False, 2),
             ("NVR that keeps the client's 0-1", "echo", False, 0),
             ("NVR that assigns 6-7", {0: 4, 1: 6}, False, 6),
             ("NVR that refuses audio-only and assigns video 4-5, audio 6-7", {0: 4, 1: 6}, True, 6)]
    for label, mode, refuse, want in cases:
        fk = FakeNvr(user, pw, {3: "PCMA"}, interleaved=mode, packet_bytes=1024)
        if refuse:
            fk.behave[3] = {"reject_audio_only": True}
        s = ra.AudioSession("127.0.0.1", fk.port, user, pw,
                            f"rtsp://127.0.0.1:{fk.port}/cam/realmonitor?channel=3&subtype=1", timeout=3.0)
        got = []
        try:
            s.open()
            end = time.monotonic() + 4.0
            while len(got) < 8 and time.monotonic() < end:
                got += s.read(0.5)
        finally:
            s.close()
        check(f"RTSP client, {label}: audio read from the NVR-assigned channel {want}",
              s.audio_channel == want and len(got) >= 8 and all(len(p[0]) == 1024 for p in got)
              and s.video_setup == refuse, (s.audio_channel, len(got), s.frames))
        check("... RTCP sender reports (own channel) are never taken as audio",
              s.frames.get(want + 1, 0) >= 1 and s.audio_packets == s.frames.get(want, 0), s.frames)
        log = "\n".join(s.log)
        check("... handshake log: SETUP reply's Transport, NVR address masked, no credentials",
              "Transport: RTP/AVP/TCP;unicast;interleaved=" in log and "127.0.0.1" not in log
              and user not in log and pw not in log and "first audio RTP packet" in log, log[-300:])
    # the production bug, reproduced: listening on the REQUESTED channel instead of the assigned one
    fk = FakeNvr(user, pw, {3: "PCMA"}, packet_bytes=1024)
    s = ra.AudioSession("127.0.0.1", fk.port, user, pw,
                        f"rtsp://127.0.0.1:{fk.port}/cam/realmonitor?channel=3&subtype=1", timeout=3.0)
    try:
        s.open()
        s.audio_channel = 0                         # what the client did before the fix
        got = []
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            got += s.read(0.5)
    finally:
        s.close()
    check("old behaviour reproduced: listening on the requested channel 0 gets NO audio while channel 2 "
          "carries it (what the real NVR showed)", not got and s.frames.get(2, 0) >= 8, s.frames)


def test_camera_without_audio(port):
    i = NO_AUDIO
    w = Ws(port, i)
    ok = wait(lambda: w.state() == "UNAVAILABLE", 5.0)
    check("camera whose stream has no audio track: listener told UNAVAILABLE", ok, w.states)
    check("... no slot kept, worker stopped (reason NO_AUDIO_TRACK)", stopped(i) and
          any(e["reason"] == "NO_AUDIO_TRACK" for e in A[i].transitions), list(A[i].transitions)[-2:])
    w.close()
    ui = {c["index"]: c for c in server.cameras_for_ui()}
    check("... /api/cameras now says audio 'unavailable' (the page disables its speaker)",
          ui[i]["audio"] == "unavailable", ui[i].get("audio"))
    n0 = FAKE["nvr2"].connections
    w = Ws(port, i)
    wait(lambda: w.state() == "UNAVAILABLE", 3.0)
    check("... asking again is answered at once WITHOUT contacting the NVR",
          w.state() == "UNAVAILABLE" and FAKE["nvr2"].connections == n0, FAKE["nvr2"].connections - n0)
    w.close()


def test_one_listener(port):
    i = NVR2[7]                                       # HR office's position in the real list (NVR2 ch8)
    t0 = time.monotonic()
    w = Ws(port, i)
    ok = wait(lambda: len(w.packets) >= 10, 5.0)
    first = next((t for t, m in w.states if m.get("state") == "PLAYING"), None)
    check("one listener: audio PLAYING, G.711 packets arrive (~8/s of 128 ms, like the real NVR2)",
          ok and w.seen("PLAYING"), w.states[-3:])
    pk = w.packets[-1][1] if w.packets else b""
    check("... packet = [1, codec 8 (A-law, NVR2)] + number + 1024 bytes G.711 (passed through, no transcoding)",
          pk[:2] == bytes([1, 8]) and len(pk) == 6 + 1024, pk[:6])
    check("... ONE audio-only RTSP session on the NVR (no video sent for audio)",
          plays(i) == 1 and FAKE["nvr2"].audio_only >= 1 and streaming("nvr2") == 1, (plays(i), streaming("nvr2")))
    rt = server.stream_info(i)["audio"]["rtsp"]
    check("... the NVR answered the audio-only SETUP with ITS OWN channel (interleaved=2-3) and the audio "
          "is read from channel 2", rt["nvrInterleaved"] == {"rtp": 2, "rtcp": 3}
          and "interleaved=2-3" in FAKE["nvr2"].transports[-1] and rt["framesByChannel"].get("2", 0) >= 5
          and rt["audioRtpPackets"] == rt["framesByChannel"]["2"], rt)
    info = server.stream_info(i)["audio"]
    check("... it holds an NVR slot (key 2000+index, priority AUDIO) and says so in the status",
          info["slotHeld"] and info["priorityName"] == "AUDIO" and info["state"] == "PLAYING"
          and info["codec"] == "PCMA" and info["rate"] == 8000 and info["listeners"] == 1, info)
    gaps = [b[0] - a[0] for a, b in zip(w.packets[3:], w.packets[4:])]
    print(f"      first PLAYING {round((first or t0) - t0, 3)} s after connecting; packet interval "
          f"avg {1000 * sum(gaps) / max(1, len(gaps)):.0f} ms, max {1000 * max(gaps or [0]):.0f} ms")
    w.close()
    check("... last listener gone: session kept briefly (linger), then TEARDOWN and slot released",
          A[i]._running and stopped(i, 4.0) and FAKE["nvr2"].streaming() == 0, (A[i]._running, streaming("nvr2")))
    check("... logged reason LINGER_EXPIRED", any(e["reason"] == "LINGER_EXPIRED" for e in A[i].transitions))


def test_two_listeners_share_one_session(port):
    i = NVR1[3]
    p0 = plays(i)
    a = Ws(port, i)
    wait(lambda: len(a.packets) >= 5, 5.0)
    b = Ws(port, i)
    ok = wait(lambda: len(b.packets) >= 5, 5.0)
    check("browser A + browser B on the same camera: ONE NVR audio session, 2 listeners",
          ok and plays(i) - p0 == 1 and A[i].viewers == 2 and FAKE["nvr1"].streaming() == 1,
          (plays(i) - p0, A[i].viewers))
    check("... both get the same packets (mu-law on NVR1)", b.packets[-1][1][:2] == bytes([1, 0]))
    b.close()
    time.sleep(0.6)
    n = len(a.packets)
    time.sleep(0.6)
    check("B leaves: A keeps listening on the same session (no new PLAY, packets continue)",
          plays(i) - p0 == 1 and A[i].viewers == 1 and len(a.packets) > n, (plays(i) - p0, A[i].viewers))
    a.close()
    time.sleep(0.3)
    c = Ws(port, i)                                   # re-listen within the linger
    wait(lambda: len(c.packets) >= 3, 3.0)
    check("quick re-listen within the linger: same session again (no new NVR session)", plays(i) - p0 == 1,
          plays(i) - p0)
    c.close()
    check("last listener cleanup: session closed, slot released", stopped(i))


def test_switch_camera_audio(port):
    a_i, b_i = NVR1[4], NVR1[5]
    a = Ws(port, a_i)
    wait(lambda: len(a.packets) >= 3, 5.0)
    a.close()                                          # the page stops A ...
    b = Ws(port, b_i)                                  # ... and starts B
    ok = wait(lambda: len(b.packets) >= 3, 5.0)
    check("switch audio A -> B: B plays", ok)
    check("... A's session ends after the linger, B keeps playing (only B audible)",
          stopped(a_i) and A[b_i].vstate == "PLAYING" and FAKE["nvr1"].streaming() == 1,
          (A[a_i]._running, A[b_i].vstate, streaming("nvr1")))
    b.close()
    a = Ws(port, a_i)                                  # and back to A
    ok = wait(lambda: len(a.packets) >= 3, 5.0)
    check("... switch back to A: plays again; B ends", ok and stopped(b_i))
    a.close()
    check("... nothing left running afterwards (no leaked session)",
          stopped(a_i) and wait(lambda: streaming("nvr1") == 0, 2.0), streaming("nvr1"))


def test_audio_reconnect_and_video_unaffected(port):
    i = NVR1[6]
    vid = VideoViewer(port, i, "prio=full")           # the camera's video is watched meanwhile
    wait(lambda: S[i].is_live(), 5.0)
    t0 = time.monotonic()
    FAKE["nvr1"].behave[CH[i]] = {"drop_after": 20}   # the NVR ends the audio stream once
    w = Ws(port, i)
    ok = wait(lambda: A[i].reconnects >= 1 and A[i].vstate == "PLAYING", 8.0)
    tr = [(e["to"], e["reason"]) for e in A[i].transitions if e["t"] >= t0]
    check("NVR ends the audio stream: RECONNECTING reason=STREAM_CLOSED, then PLAYING reason=RECONNECTED",
          ok and ("RECONNECTING", "STREAM_CLOSED") in tr and ("PLAYING", "RECONNECTED") in tr, tr)
    n = len(w.packets)
    time.sleep(0.5)
    check("... the listener's WebSocket stayed open and audio flows again", not w.closed and len(w.packets) > n)
    FAKE["nvr1"].behave[CH[i]] = {"stall_s": 2.5}     # next packet late: longer than the read timeout
    t1 = time.monotonic()
    ok = wait(lambda: any(e["reason"] == "READ_TIMEOUT" for e in A[i].transitions if e["t"] >= t1)
              and A[i].vstate == "PLAYING", 12.0)
    check("audio silent longer than the read timeout: reconnect reason=READ_TIMEOUT, then PLAYING", ok,
          [(e["to"], e["reason"]) for e in A[i].transitions if e["t"] >= t1])
    vtr = [(e["to"], e["reason"]) for e in S[i].transitions if e["t"] >= t0]
    check("... the camera's VIDEO was not touched (still LIVE, no transition, no reconnect)",
          S[i].is_live() and not vtr and S[i].reconnects == 0, vtr)
    w.close()
    vid.close()
    stopped(i)


def test_network_stall_rides_through(port):
    i = NVR1[7]
    old = server.AUDIO_READ_TIMEOUT_S
    server.AUDIO_READ_TIMEOUT_S = 4.0                 # like the default (= video's 8 s): longer than the stall
    w = Ws(port, i)
    try:
        wait(lambda: A[i].vstate == "PLAYING", 5.0)
        o0, r0 = A[i].opens, A[i].reconnects
        t0 = time.monotonic()
        FAKE["nvr1"].behave[CH[i]] = {"stall_s": 3.0}  # the network freezes for 3 s (seen on the real site)
        ok = wait(lambda: any(m.get("cause") == "stalled" for t, m in w.states if t >= t0), 5.0)
        check("network stall of 3 s (< read timeout): the listener is told 'Audio interrupted' (cause=stalled)",
              ok and A[i].ui_cause() in ("stalled", ""), w.states[-2:])
        ok = wait(lambda: A[i].vstate == "PLAYING" and w.states[-1][1].get("cause") == ""
                  and any(e["reason"] == "PACKETS_RESUMED" for e in A[i].transitions if e["t"] >= t0), 6.0)
        check("... packets resume on the SAME session: PLAYING again, cause cleared, no reconnect",
              ok and A[i].opens == o0 and A[i].reconnects == r0, (A[i].vstate, A[i].opens - o0, A[i].reconnects - r0))
    finally:
        server.AUDIO_READ_TIMEOUT_S = old
        w.close()
    stopped(i)


def test_audio_failure_leaves_video_live(port):
    i = NVR2[8]
    vid = VideoViewer(port, i, "prio=full")
    wait(lambda: S[i].is_live(), 5.0)
    t0 = time.monotonic()
    FAKE["nvr2"].behave[CH[i]] = {"no_answer": True}  # the audio session cannot even be opened
    w = Ws(port, i)
    ok = wait(lambda: A[i].fail_streak >= 2, 10.0)
    check("audio cannot connect: audio RECONNECTING / ERROR and retries by itself", ok and
          (w.seen("RECONNECTING") or w.seen("ERROR")), w.states[-3:])
    vtr = [(e["to"], e["reason"]) for e in S[i].transitions if e["t"] >= t0]
    check("... video of the same camera stays LIVE, untouched", S[i].is_live() and not vtr, vtr)
    FAKE["nvr2"].behave.pop(CH[i], None)
    ok = wait(lambda: A[i].vstate == "PLAYING", 10.0)
    check("... audio recovers when the NVR answers again", ok, A[i].vstate)
    w.close()
    vid.close()
    stopped(i)


def test_silence_is_not_a_failure(port):
    i = NVR2[6]
    FAKE["nvr2"].behave[CH[i]] = {"silence": True}    # the microphone sends G.711 digital silence
    w = Ws(port, i)
    ok = wait(lambda: A[i].silent and w.states and w.states[-1][1].get("cause") == "silent", 6.0)
    check("silent microphone: PLAYING + 'Audio connected - no sound detected' (cause=silent), not RECONNECTING",
          ok and A[i].vstate == "PLAYING" and w.state() == "PLAYING", (A[i].vstate, A[i].silent, w.states[-2:]))
    r0, o0, n0 = A[i].reconnects, A[i].opens, len(w.packets)
    time.sleep(2 * server.AUDIO_READ_TIMEOUT_S + 0.5)
    check("... silent for longer than twice the read timeout: NO reconnect, same session, packets keep coming",
          A[i].reconnects == r0 and A[i].opens == o0 and A[i].vstate == "PLAYING" and len(w.packets) >= n0 + 10,
          (A[i].reconnects - r0, A[i].opens - o0, A[i].vstate, len(w.packets) - n0))
    info = server.stream_info(i)["audio"]
    check("... /api/status: silent=true, level about -72 dBFS (G.711 digital silence), uiCause=silent",
          info["silent"] is True and info["levelDb"] is not None and info["levelDb"] < -60
          and info["uiCause"] == "silent", {k: info[k] for k in ("silent", "levelDb", "peakDb", "uiCause")})
    FAKE["nvr2"].behave[CH[i]] = {}                    # someone speaks
    ok = wait(lambda: not A[i].silent and w.states[-1][1].get("cause") == "", 4.0)
    check("... sound again: silent=false and the listener is told at once (same session)",
          ok and A[i].reconnects == r0 and A[i].opens == o0, (A[i].silent, w.states[-1:]))
    w.close()
    FAKE["nvr2"].behave.pop(CH[i], None)
    stopped(i)


def test_no_audio_packets_is_diagnosed(port):
    i = NVR2[4]
    vid = VideoViewer(port, i, "prio=full")
    wait(lambda: S[i].is_live(), 5.0)
    t0 = time.monotonic()
    FAKE["nvr2"].behave[CH[i]] = {"no_rtp": True}     # PLAY accepted, but no audio RTP (the production symptom)
    w = Ws(port, i)
    ok = wait(lambda: any(e["reason"] == "NO_AUDIO_PACKETS" for e in A[i].transitions if e["t"] >= t0), 8.0)
    d = server.stream_info(i)["audio"]["detail"] or ""
    check("PLAY accepted but no audio RTP: reason NO_AUDIO_PACKETS with the channel and the frames per channel",
          ok and "interleaved channel 2" in d and "frames per channel" in d, d)
    check("... the listener is told 'Audio stream unavailable' (cause=nostream); no technical detail sent",
          wait(lambda: any(m.get("cause") == "nostream" for _, m in w.states), 3.0)
          and not any("detail" in m for _, m in w.states), w.states[-2:])
    vtr = [(e["to"], e["reason"]) for e in S[i].transitions if e["t"] >= t0]
    check("... the camera's video is untouched (LIVE, no transition)", S[i].is_live() and not vtr, vtr)
    FAKE["nvr2"].behave.pop(CH[i], None)
    ok = wait(lambda: A[i].vstate == "PLAYING", 8.0)
    check("... audio recovers by itself once RTP flows", ok, A[i].vstate)
    w.close()
    vid.close()
    stopped(i)


def test_slot_accounting(port):
    cams = NVR1[:6]
    vids = [VideoViewer(port, i) for i in cams]       # 6 viewed tiles fill NVR1 (cap 6)
    wait(lambda: all(S[i].is_live() and S[i].viewers == 1 for i in cams), 8.0)
    x = NVR1[8]
    p0 = FAKE["nvr1"].plays.get(CH[x], 0)
    w = Ws(port, x)
    ok = wait(lambda: w.state() == "WAITING", 5.0)
    time.sleep(1.0)
    check("NVR full of viewed video: audio WAITS ('Audio waiting for available NVR capacity')", ok, w.states)
    check("... no 7th session: the NVR never saw the audio session; video untouched",
          FAKE["nvr1"].plays.get(CH[x], 0) == p0 and all(S[i].is_live() for i in cams)
          and len(owners("nvr1")) <= CAP["nvr1"], (FAKE["nvr1"].plays.get(CH[x], 0) - p0, len(owners("nvr1"))))
    vids[0].close()                                    # a viewer leaves -> capacity
    ok = wait(lambda: A[x].vstate == "PLAYING", 8.0)
    check("... a viewer leaves: audio gets the slot and plays", ok, A[x].vstate)
    check(f"... per-NVR cap never exceeded (peak {PEAK})", all(PEAK[k] <= CAP[k] for k in server.NVRS), PEAK)
    w.close()
    for v in vids[1:]:
        v.close()
    stopped(x)


def test_background_yields_for_audio(port):
    wait(lambda: len([s for s in S if s.info["nvr"] == "nvr2" and s._bg and s._running]) >= 5, 8.0)
    x = NVR2[10]
    t0 = time.monotonic()
    w = Ws(port, x)
    ok = wait(lambda: A[x].vstate == "PLAYING", 6.0)
    dt = time.monotonic() - t0
    demoted = [s for s in S if s.info["nvr"] == "nvr2"
               and any(e["t"] >= t0 and e["reason"] == "POOL_DEMOTION" for e in list(s.transitions))]
    check(f"NVR2 full of background streams: one yields at once, audio plays ({dt:.2f} s)",
          ok and dt < 3.0 and demoted, (ok, round(dt, 2), len(demoted)))
    check("... cap respected", len(owners("nvr2")) <= CAP["nvr2"], len(owners("nvr2")))
    w.close()
    stopped(x)


def test_audio_retry_does_not_churn_background(port):
    wait(lambda: len([s for s in S if s.info["nvr"] == "nvr2" and s._bg and s._running]) >= 5, 8.0)
    x = NVR2[3]
    FAKE["nvr2"].behave[CH[x]] = {"no_answer": True}  # every audio attempt fails (the NVR never answers)
    t0 = time.monotonic()
    w = Ws(port, x)
    ok = wait(lambda: A[x].fail_streak >= 3, 20.0)
    dem = [(s.index, e["reason"]) for s in S if s.info["nvr"] == "nvr2"
           for e in list(s.transitions) if e["t"] >= t0 and e["reason"] == "POOL_DEMOTION"]
    check("audio retrying (3 failed attempts) on a full NVR: ONE background stream yields once -- no background "
          "video started and stopped again at every retry", ok and len(dem) <= 1, (A[x].fail_streak, dem))
    check("... cap respected meanwhile", len(owners("nvr2")) <= CAP["nvr2"], len(owners("nvr2")))
    w.close()
    FAKE["nvr2"].behave.pop(CH[x], None)
    stopped(x)


def test_audio_limit_per_nvr(port):
    xs = NVR2[0:3]
    ws = [Ws(port, i) for i in xs]
    wait(lambda: sum(A[i].vstate == "PLAYING" for i in xs) >= 2, 6.0)
    time.sleep(0.8)
    third = [i for i in xs if A[i].vstate != "PLAYING"]
    check(f"3 cameras' audio on one NVR with CCTV_AUDIO_MAX_PER_NVR=2: 2 play, 1 waits (AUDIO_LIMIT)",
          len(third) == 1 and any(e["reason"] == "AUDIO_LIMIT" for e in A[third[0]].transitions),
          {i: A[i].vstate for i in xs})
    ws[[i for i in xs].index([i for i in xs if i not in third][0])].close()
    ok = wait(lambda: A[third[0]].vstate == "PLAYING", 6.0)
    check("... one stops listening: the waiting one plays", ok, A[third[0]].vstate)
    for w in ws:
        w.close()
    for i in xs:
        stopped(i)


def test_quality_switch_never_touches_audio(port):
    i = NVR1[9]
    w = Ws(port, i, full=True)
    wait(lambda: A[i].vstate == "PLAYING", 5.0)
    p0, t0 = plays(i), time.monotonic()
    std = VideoViewer(port, i, "prio=full")
    wait(lambda: S[i].is_live(), 5.0)
    orig = VideoViewer(port, i, "quality=original&prio=full")      # Standard -> Original
    time.sleep(0.05)
    std.close()
    wait(lambda: O[i].is_live(), 5.0)
    back = VideoViewer(port, i, "prio=full")                        # Original -> Standard
    time.sleep(0.05)
    orig.close()
    wait(lambda: S[i].is_live(), 5.0)
    time.sleep(0.5)
    atr = [(e["to"], e["reason"]) for e in A[i].transitions if e["t"] >= t0]
    check("Standard -> Original -> Standard while listening: the audio session is untouched",
          plays(i) == p0 and not atr and A[i].vstate == "PLAYING", (plays(i) - p0, atr))
    check("... and it is one audio session (no duplicate)", FAKE["nvr1"].plays.get(CH[i]) == p0 == 1,
          FAKE["nvr1"].plays.get(CH[i]))
    check("... audio of a fullscreen listener has priority AUDIO_FULLSCREEN (still below every viewed video)",
          A[i].priority_name() == "AUDIO_FULLSCREEN" and A[i].slot_priority() < server.PRIO_GRID_STANDARD)
    back.close()
    w.close()
    stopped(i)


def test_nvr_refusing_audio_only(port):
    i = NVR2[9]
    FAKE["nvr2"].behave[CH[i]] = {"reject_audio_only": True}
    w = Ws(port, i)
    ok = wait(lambda: len(w.packets) >= 3, 6.0)
    check("NVR refuses an audio-only SETUP: falls back to video+audio on the same session, audio plays",
          ok and A[i].video_setup and server.stream_info(i)["audio"]["videoTrackSetUp"], A[i].vstate)
    w.close()
    FAKE["nvr2"].behave.pop(CH[i], None)
    stopped(i)


def test_settings_override_and_access(port):
    i = NVR1[10]
    key = server.camera_key(server.CAMERAS[i])
    st, _ = server.SETTINGS.update({key: {"audio": "off"}})
    w = Ws(port, i)
    wait(lambda: w.state() is not None, 3.0)
    check("Settings: Audio = Off -> the camera offers no audio (UNAVAILABLE), nothing is opened",
          st == 200 and w.state() == "UNAVAILABLE" and not A[i]._running and plays(i) == 0, (st, w.states))
    w.close()
    ui = {c["index"]: c for c in server.cameras_for_ui()}
    check("... /api/cameras says 'disabled'", ui[i]["audio"] == "disabled", ui[i]["audio"])
    st, bad = server.SETTINGS.update({key: {"audio": "loud"}})
    check("... invalid value rejected", st == 400 and "audio" in json.dumps(bad), bad)
    server.SETTINGS.update({key: {"audio": "auto"}})
    ui = {c["index"]: c for c in server.cameras_for_ui()}
    check("... back to Automatic", ui[i]["audio"] in ("available", "unknown"), ui[i]["audio"])
    if server.TOKEN:
        w = Ws(port, i, key=False)
        check("audio endpoint without the access key -> 401 (same protection as video)",
              w.status_line and " 401 " in w.status_line, w.status_line)
        w.close()


def test_status_api(port):
    i = NVR2[7]
    w = Ws(port, i)
    wait(lambda: A[i].vstate == "PLAYING", 5.0)
    time.sleep(1.2)
    d = server.system_status()
    a = d["cameras"][i]["audio"]
    keys = {"available", "codec", "listeners", "state", "active", "lastPacketAgeMs", "reconnects",
            "slotHeld", "levelDb", "priorityName", "transitions"}
    check("/api/status per camera: audio {available, codec, listeners, state, active, lastPacketAge, reconnects...}",
          keys <= set(a) and a["available"] == "available" and a["listeners"] == 1 and a["active"]
          and a["lastPacketAgeMs"] is not None and a["lastPacketAgeMs"] < 1000, a)
    check("... the tone's level is measured (dBFS); it is not 'silent'; peakDb reported; no UI cause",
          a["levelDb"] is not None and -30 < a["levelDb"] < 0 and a["silent"] is False
          and a["peakDb"] is not None and a["uiCause"] is None, {k: a.get(k) for k in ("levelDb", "silent", "uiCause")})
    rt = a.get("rtsp") or {}
    hs = "\n".join(rt.get("handshake") or [])
    check("... RTSP diagnostics: handshake (DESCRIBE/SETUP/PLAY), NVR-assigned channel, TCP bytes, frames, RTP count",
          "DESCRIBE -> 200" in hs and "audio SETUP" in hs and "PLAY" in hs and rt.get("nvrInterleaved") ==
          {"rtp": 2, "rtcp": 3} and rt.get("tcpBytes", 0) > 0 and rt.get("audioRtpPackets", 0) > 0, rt)
    check("... the handshake log masks the NVR address", "127.0.0.1" not in hs and "<nvr>" in hs, hs[:300])
    slot = [x for x in d["nvrs"]["nvr2"]["slots"] if x["quality"] == "AUDIO"]
    check("... the NVR slot table lists the audio session (AUDIO, 1 listener)",
          slot and slot[0]["index"] == i and slot[0]["viewers"] == 1 and slot[0]["priorityName"] == "AUDIO", slot)
    blob = json.dumps(d) + json.dumps(server.stream_info(i))
    secrets = [x for n in server.NVRS.values() for x in (n["user"], n["pass"]) if x and len(x) >= 4]
    check("... no credentials anywhere in the status", not any(x in blob for x in secrets))
    w.close()
    stopped(i)


def test_page_audio_manager():
    p = server.PAGE
    check("page: every tile has a speaker; fullscreen has speaker + volume; header shows the audio owner",
          "class=aud" in p and "id=abtn" in p and "id=avol" in p and "id=apill" in p)
    check("page: audio OFF by default, starts only inside a click (autoplay rules), volume default 50 % remembered",
          "state: 'OFF'" in p and "muted: false" in p and "volume: 50" in p and "localStorage.getItem(VKEY)" in p
          and "function audioStart(cam, origin){          // runs inside the click" in p)
    check("page: ONE camera at a time (starting one stops the previous), instant mute with a 10 s grace",
          "audioStop();                              // only one camera at a time" in p
          and "AUD.graceT = setTimeout(() => { if (AUD.muted) audioStop(); }, 10000);" in p)
    check("page: no stale audio (> 0.8 s behind live: the QUEUED audio is dropped and playback re-anchored at "
          "0.25 s; unmute / stop flush the queue), G.711 decoded in the browser",
          "else if (AUD.next > now + 0.8){" in p and "s.late += audioFlush(); AUD.next = now + 0.25;" in p
          and "audioFlush(); AUD.next = 0;" in p and "ALAW[i]" in p and "ULAW[i]" in p)
    check("page: camera without audio -> disabled speaker, tooltip 'No audio available'",
          "'No audio available'" in p and "btn.disabled = !hasAudio(cam);" in p)
    check("page: WebSocket transport (not counted in the browser's 6 connections per host)",
          "new WebSocket(audioUrl(i))" in p)
    texts = ("'Audio connecting…'", "'Waiting for NVR capacity'", "'Audio RTSP setup failed — retrying'",
             "'Audio stream unavailable — retrying'", "'Audio connected — no sound detected'", "'Audio reconnecting…'",
             "'Audio interrupted — waiting for the NVR…'")
    check("page: precise audio statuses (connecting / NVR capacity / RTSP setup failed / stream unavailable / "
          "connected - no sound / reconnecting)", all(t in p for t in texts), [t for t in texts if t not in p])
    check("page: AudioContext resumed inside the click; a rejection is logged; [AUDIO UI] console diagnostics",
          "AudioContext resume REJECTED" in p and "'[AUDIO UI] '" in p and "first audio packet" in p)


if __name__ == "__main__":
    t_start = time.time()
    threading.Thread(target=_sampler, daemon=True).start()
    srv = server.QuietServer(("127.0.0.1", 0), server.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    server.POOL.start()
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n-- {name}", flush=True)
            fn() if name == "test_page_audio_manager" else fn(port)
    _RUN["on"] = False
    srv.shutdown()
    print(f"\npeak NVR slots used: {PEAK} (cap {dict(CAP)})")
    check(f"per-NVR cap never exceeded (peak {PEAK})", all(PEAK[k] <= CAP[k] for k in server.NVRS), PEAK)
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}  ({time.time() - t_start:.1f}s)")
    sys.exit(1 if FAILS else 0)
