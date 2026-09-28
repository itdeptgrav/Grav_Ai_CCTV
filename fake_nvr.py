"""Fake NVR RTSP server for OFFLINE audio tests (test_audio.py) -- never used in production.

Behaves like the real NVRs as far as the audio client is concerned: digest auth,
DESCRIBE -> SDP with a video track and (optionally) a G.711 audio track, SETUP over
TCP interleaved, PLAY -> RTP packets of G.711 (a 1 kHz tone), RTCP sender reports,
GET_PARAMETER keep-alive, TEARDOWN. Per channel it can: have no audio track, drop the
stream after N packets, stall, send digital silence, refuse an audio-only SETUP, or
never answer DESCRIBE. It counts sessions so tests can prove how many NVR connections
were used.

Like the REAL NVR (measured 2026-09-26, NVR2 ch8) it assigns the interleaved channels
itself by default ("dahua": track k -> 2k / 2k+1, whatever the client asked for): an
audio-only SETUP asking "interleaved=0-1" is answered "interleaved=2-3;ssrc=..." and
the audio RTP arrives on channel 2, RTCP on 3. packet_bytes=1024 gives the real
NVR2 packet size (1024 bytes = 128 ms); the default 320 bytes = 40 ms.
"""
import re
import math
import time
import socket
import struct
import hashlib
import datetime
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def tone(codec, n=320, freq=1000.0, amp=6000, phase=0):
    """n G.711 bytes of a sine tone."""
    out = bytearray()
    for k in range(n):
        x = int(amp * math.sin(2 * math.pi * freq * (phase + k) / 8000))
        out.append(_lin2ulaw(x) if codec == "PCMU" else _lin2alaw(x))
    return bytes(out)


def _lin2ulaw(x):
    BIAS, CLIP = 0x84, 32635
    sign = 0x80 if x < 0 else 0
    x = min(abs(x), CLIP) + BIAS
    exp = max(0, min(7, x.bit_length() - 8))
    mant = (x >> (exp + 3)) & 0x0F
    return ~(sign | (exp << 4) | mant) & 0xFF


def _lin2alaw(x):
    x >>= 3
    sign = 0x80 if x >= 0 else 0
    if x < 0:
        x = -x - 1
    x = min(x, 0xFFF)
    exp = 0 if x < 32 else max(0, min(7, x.bit_length() - 5))
    mant = (x >> 1 if exp == 0 else x >> exp) & 0x0F
    return (sign | (exp << 4) | mant) ^ 0x55


class FakeNvr:
    def __init__(self, user, pw, audio=None, realm="Login to FAKE", interleaved="dahua", packet_bytes=320):
        self.user, self.pw, self.realm = user, pw, realm
        self.audio = dict(audio or {})     # channel -> "PCMA" | "PCMU" | None (no audio track)
        self.behave = {}                   # channel -> {"drop_after", "stall_at", "stall_s", "silence",
                                           #             "no_rtp", "reject_audio_only", "no_answer"}
        self.interleaved = interleaved     # "dahua" | "echo" (use the client's) | {track: rtp channel}
        self.packet_bytes = packet_bytes
        self.transports = []               # Transport replies sent to SETUPs (newest last)
        # recorded playback (/cam/playback): channel -> [(start, end)] naive NVR-local datetimes
        self.recordings = {}
        self.ts_jumps = {}                 # channel -> [NVR-local datetime]: the RTP clock jumps +2.04 s
                                           # there although the footage does not (real NVR2 quirk)
        self.end_step_back = True          # both real NVRs: ~2 s before endtime the RTP clock steps
                                           # BACK ~1 s (then BYE + close)
        self.gap_ends_session = False      # real NVR1 (fw 4.001): a playback session only covers the
                                           # recording up to the first gap (DESCRIBE counts that part;
                                           # PLAY past it -> 500 + close; at the gap BYE + close)
        self.play_log = []                 # every playback PLAY: {"ch", "range", "scale"}
        self.pb_active = 0                 # playback sessions streaming now
        self.pb_peak = 0
        self.pb_sessions = 0
        self.lock = threading.Lock()
        self.active = {}                   # channel -> sessions currently streaming (after PLAY)
        self.peak_total = 0                # most sessions streaming at once on this NVR
        self.plays = {}                    # channel -> PLAYs so far
        self.teardowns = 0
        self.audio_only = 0                # sessions that set up ONLY the audio track
        self.connections = 0
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(64)
        self.port = self.srv.getsockname()[1]
        self.nonce = "5ac1d0e2f"
        threading.Thread(target=self._accept, daemon=True).start()

    def streaming(self):
        with self.lock:
            return sum(self.active.values())

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with self.lock:
                self.connections += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    # ── one RTSP connection ─────────────────────────────────────────────────────
    def _serve(self, c):
        st = {"tracks": set(), "chan": {}, "session": None, "ch": None, "play": False,
              "stop": threading.Event(), "wlock": threading.Lock()}
        buf = b""
        try:
            while not st["stop"].is_set():
                c.settimeout(0.5)
                try:
                    chunk = c.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\r\n\r\n" in buf:
                    head, _, buf = buf.partition(b"\r\n\r\n")
                    self._handle(c, st, head.decode("latin-1"))
        except OSError:
            pass
        finally:
            st["stop"].set()
            self._end_play(st)
            try:
                c.close()
            except OSError:
                pass

    def _send(self, c, st, data):
        with st["wlock"]:
            c.sendall(data)

    def _reply(self, c, st, cseq, code, text, headers=(), body=""):
        lines = [f"RTSP/1.0 {code} {text}", f"CSeq: {cseq}"] + list(headers)
        if body:
            lines.append(f"Content-Length: {len(body.encode())}")
        self._send(c, st, ("\r\n".join(lines) + "\r\n\r\n" + body).encode())

    def _authorised(self, method, uri, hdrs):
        a = hdrs.get("authorization", "")
        m = dict(re.findall(r'(\w+)="([^"]*)"', a))
        if not m:
            return False
        ha1 = _md5(f"{self.user}:{self.realm}:{self.pw}")
        ha2 = _md5(f"{method}:{m.get('uri', uri)}")
        return m.get("response") == _md5(f"{ha1}:{self.nonce}:{ha2}")

    def _handle(self, c, st, head):
        lines = head.split("\r\n")
        method, uri, _ = (lines[0].split(" ") + ["", "", ""])[:3]
        hdrs = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        cseq = hdrs.get("cseq", "0")
        m = re.search(r"channel=(\d+)", uri)
        ch = int(m.group(1)) if m else st["ch"]
        if ch is not None:
            st["ch"] = ch
        b = self.behave.get(st["ch"], {})
        if method == "DESCRIBE" and b.get("no_answer"):
            return                                           # like a dead channel: no reply
        if not self._authorised(method, uri, hdrs):
            self._reply(c, st, cseq, 401, "Unauthorized",
                        [f'WWW-Authenticate: Digest realm="{self.realm}", nonce="{self.nonce}", stale="FALSE"'])
            return
        base = uri.split("/trackID")[0]
        if "/cam/playback" in uri and method == "DESCRIBE":
            return self._pb_describe(c, st, cseq, uri)
        if st.get("pb") is not None and method == "PLAY":
            return self._pb_play(c, st, cseq, hdrs)
        if st.get("pb") is not None and method == "PAUSE":
            st["pb"]["paused"] = True
            return self._reply(c, st, cseq, 200, "OK", [f"Session: {st['session']}"])
        if method == "DESCRIBE":
            codec = self.audio.get(st["ch"])
            sdp = ["v=0", "o=- 0 0 IN IP4 127.0.0.1", "s=Media Server", "t=0 0",
                   "m=video 0 RTP/AVP 96", "a=rtpmap:96 H265/90000", "a=control:trackID=0"]
            if codec:
                pt = 0 if codec == "PCMU" else 8
                sdp += [f"m=audio 0 RTP/AVP {pt}", f"a=rtpmap:{pt} {codec}/8000", "a=control:trackID=1"]
            self._reply(c, st, cseq, 200, "OK", ["Content-Type: application/sdp", f"Content-Base: {uri}/"],
                        "\r\n".join(sdp) + "\r\n")
        elif method == "SETUP":
            track = 1 if uri.endswith("trackID=1") else 0
            if track == 1 and b.get("reject_audio_only") and 0 not in st["tracks"]:
                self._reply(c, st, cseq, 455, "Method Not Valid In This State")
                return
            st["tracks"].add(track)
            st["session"] = st["session"] or f"{int(time.time() * 1000) % 100000000:08d}"
            asked = re.search(r"interleaved=(\d+)", hdrs.get("transport", ""))
            if self.interleaved == "echo":
                rtp = int(asked.group(1)) if asked else 2 * track
            elif isinstance(self.interleaved, dict):
                rtp = self.interleaved[track]
            else:                                            # the real NVR: numbered by track
                rtp = 2 * track
            st["chan"][track] = rtp
            tr = f"RTP/AVP/TCP;unicast;interleaved={rtp}-{rtp + 1};ssrc={0x4B21A6CE + track:08X}"
            self.transports.append(tr)
            self._reply(c, st, cseq, 200, "OK", [f"Session: {st['session']};timeout=60", f"Transport: {tr}"])
        elif method == "PLAY":
            self._reply(c, st, cseq, 200, "OK", [f"Session: {st['session']}", "Range: npt=0.000-"])
            if not st["play"]:
                st["play"] = True
                with self.lock:
                    self.active[st["ch"]] = self.active.get(st["ch"], 0) + 1
                    self.plays[st["ch"]] = self.plays.get(st["ch"], 0) + 1
                    self.peak_total = max(self.peak_total, sum(self.active.values()))
                    if st["tracks"] == {1}:
                        self.audio_only += 1
                threading.Thread(target=self._stream, args=(c, st), daemon=True).start()
        elif method == "GET_PARAMETER" or method == "OPTIONS":
            self._reply(c, st, cseq, 200, "OK", [f"Session: {st['session']}"] if st["session"] else [])
        elif method == "TEARDOWN":
            self._reply(c, st, cseq, 200, "OK")
            with self.lock:
                self.teardowns += 1
            st["stop"].set()
        else:
            self._reply(c, st, cseq, 501, "Not Implemented")

    def _end_play(self, st):
        if st.get("play"):
            st["play"] = False
            with self.lock:
                self.active[st["ch"]] = max(0, self.active.get(st["ch"], 0) - 1)

    def _stream(self, c, st):
        codec = self.audio.get(st["ch"]) or "PCMA"
        seq, ts, n = 1000, 0, 0
        achan, vchan = st["chan"].get(1), st["chan"].get(0)
        pt = 0 if codec == "PCMU" else 8
        nb = self.packet_bytes
        payload = tone(codec, nb)
        quiet = bytes([0xFF if codec == "PCMU" else 0xD5]) * nb      # G.711 digital silence
        try:
            while not st["stop"].is_set():
                n += 1
                b = self.behave.get(st["ch"], {})        # looked up live: tests change it mid-stream
                if b.get("drop_after") and n > b["drop_after"]:
                    b.pop("drop_after", None)              # once
                    break                                  # the NVR ends the stream
                if b.get("stall_s") and b.get("stall_at", n) <= n:    # once; no stall_at = next packet
                    b.pop("stall_at", None)
                    time.sleep(b.pop("stall_s"))
                if achan is not None:
                    if not b.get("no_rtp"):                # no_rtp: PLAY accepted, but no audio RTP at all
                        data = quiet if b.get("silence") else payload
                        rtp = struct.pack(">BBHII", 0x80, pt, seq & 0xFFFF, ts & 0xFFFFFFFF, 0x1234) + data
                        self._send(c, st, b"$" + bytes([achan]) + struct.pack(">H", len(rtp)) + rtp)
                    if n % 40 == 5:                        # RTCP sender report on the RTCP channel
                        sr = struct.pack(">BBHIIIIII", 0x80, 200, 6, 0x1234, 0, 0, ts & 0xFFFFFFFF, n, n * nb)
                        self._send(c, st, b"$" + bytes([achan + 1]) + struct.pack(">H", len(sr)) + sr)
                if vchan is not None:                      # tiny fake video packet
                    rtp = struct.pack(">BBHII", 0x80, 96, seq & 0xFFFF, ts, 0x5678) + b"\x00" * 60
                    self._send(c, st, b"$" + bytes([vchan]) + struct.pack(">H", len(rtp)) + rtp)
                seq += 1
                ts += nb
                time.sleep(nb / 8000.0)
        except OSError:
            pass
        finally:
            self._end_play(st)
            st["stop"].set()
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # ── recorded playback (the real NVRs' behaviour, NVR_PLAYBACK_CAPABILITY_REPORT.txt) ──
    # DESCRIBE /cam/playback?channel&starttime&endtime (NVR local) -> 404 if nothing is
    # recorded, else SDP + "a=range:npt=0-<recorded seconds of the window>". PLAY
    # "Range: clock=<UTC>-" starts at the key frame at/before that time (2 s GOP from the
    # RECORDING's start -- so at the From time it is the key frame before the window, like
    # the real NVR) or at the next recording inside a gap; a time outside the window ->
    # 500 + connection closed. Scale 2/4 = key frames only, no audio. Crossing a gap makes
    # the RTP timestamp jump by the gap length. At endtime: RTCP BYE + close. Each video
    # frame carries its TRUE recorded time (b"FAKE" + ms) -- the camera's on-screen clock.
    IST = datetime.timedelta(minutes=330)

    def _pb_describe(self, c, st, cseq, uri):
        qs = dict(urllib.parse.parse_qsl(uri.split("?", 1)[1].split("/")[0])) if "?" in uri else {}
        try:
            ch = int(qs["channel"])
            a = datetime.datetime.strptime(qs["starttime"], "%Y_%m_%d_%H_%M_%S")
            b = datetime.datetime.strptime(qs["endtime"], "%Y_%m_%d_%H_%M_%S")
        except (KeyError, ValueError):
            return self._reply(c, st, cseq, 400, "Bad Request")
        segs, real = [], []
        for s0, e0 in sorted(self.recordings.get(ch, [])):
            s1, e1 = max(s0, a), min(e0, b)
            if e1 > s1:
                segs.append((s1, e1))
                real.append((s0, e1))                              # recording start kept (key frame grid)
        if self.gap_ends_session:
            # NVR1: only a window STARTING inside a recording file is answered; the session
            # runs on through files that follow seamlessly (hourly files) and stops at the
            # first gap or overlap (e.g. the 1 s file at the start of a recording)
            k = next((i for i, r in enumerate(real) if r[0] <= a < r[1]), None)
            if k is None:
                segs, real = [], []
            else:
                j = k + 1
                while j < len(real) and 0 <= (real[j][0] - real[j - 1][1]).total_seconds() <= 1.0:
                    j += 1
                segs, real = segs[k:j], real[k:j]
            rec = sum((e - s).total_seconds() for s, e in segs)
        else:
            # NVR2: "window end - first recorded moment" (gaps inside are not subtracted)
            rec = (segs[-1][1] - segs[0][0]).total_seconds() if segs else 0
        if rec <= 0:
            return self._reply(c, st, cseq, 404, "Not Found")
        st["ch"] = ch
        st["pb"] = {"ch": ch, "a": a, "b": b, "segs": real, "pos": None, "paused": True, "scale": 1.0,
                    "vts": 1000000, "ats": 50000, "vseq": 20000, "aseq": 30000, "thread": None, "range": None,
                    "jumps": sorted(self.ts_jumps.get(ch, [])), "lock": threading.Lock()}
        codec = self.audio.get(ch)
        sdp = ["v=0", "o=- 0 0 IN IP4 127.0.0.1", "s=Media Server", "t=0 0", "a=control:*",
               f"a=range:npt=0-{rec:.6f}", "m=video 0 RTP/AVP 98", "a=rtpmap:98 H265/90000", "a=control:trackID=0"]
        if codec:
            pt = 0 if codec == "PCMU" else 8
            sdp += [f"m=audio 0 RTP/AVP {pt}", f"a=rtpmap:{pt} {codec}/8000", "a=control:trackID=1"]
        self._reply(c, st, cseq, 200, "OK", ["Content-Type: application/sdp", f"Content-Base: {uri}/"],
                    "\r\n".join(sdp) + "\r\n")

    def _pb_locate(self, pb, t):
        for s0, e0 in pb["segs"]:
            if s0 <= t < e0:
                return s0 + datetime.timedelta(seconds=int((t - s0).total_seconds() // 2) * 2)
            if s0 > t:
                return s0
        return None

    def _pb_play(self, c, st, cseq, hdrs):
        pb = st["pb"]
        rng = hdrs.get("range", "")
        try:
            scale = float(hdrs.get("scale", "1") or 1)
        except ValueError:
            scale = 1.0
        with self.lock:
            self.play_log.append({"ch": pb["ch"], "range": rng, "scale": scale})
        m = re.match(r"clock=(\d{8}T\d{6})Z-", rng)
        with pb["lock"]:                          # not while the stream thread is mid-frame
            if m:
                target = datetime.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S") + self.IST
                if not pb["a"] <= target < pb["b"] or (self.gap_ends_session and target >= pb["segs"][-1][1]):
                    self._reply(c, st, cseq, 500, "Internal Server Error")
                    st["stop"].set()                               # like the real NVR: drops the connection
                    return
                pb["pos"] = self._pb_locate(pb, target)
                pb["range"] = f"clock={m.group(1)}Z-{(pb['b'] - self.IST).strftime('%Y%m%dT%H%M%S')}Z"
                pb["jumps"] = [j for j in sorted(self.ts_jumps.get(pb["ch"], [])) if pb["pos"] and j > pb["pos"]]
            elif pb["pos"] is None:
                pb["pos"] = self._pb_locate(pb, pb["a"])
                pb["range"] = "npt=0.000000-"
            pb["scale"] = scale
            info = f"url=trackID=0;seq={pb['vseq']};rtptime={pb['vts']}"
            if self.audio.get(pb["ch"]):
                info += f",url=trackID=1;seq={pb['aseq']};rtptime={pb['ats']}"
            self._reply(c, st, cseq, 200, "OK", [f"Session: {st['session']}", f"Range: {pb['range']}", f"RTP-Info: {info}"])
            pb["paused"] = False
        if pb["thread"] is None:
            with self.lock:
                self.pb_active += 1
                self.pb_sessions += 1
                self.pb_peak = max(self.pb_peak, self.pb_active)
            pb["thread"] = threading.Thread(target=self._pb_stream, args=(c, st), daemon=True)
            pb["thread"].start()

    def _pb_send_video(self, c, st, pb, vch, key, rec_ms):
        nal = bytes([(19 if key else 1) << 1, 1]) + b"FAKE" + struct.pack(">Q", rec_ms)
        rtp = struct.pack(">BBHII", 0x80, 0x80 | 98, pb["vseq"] & 0xFFFF, pb["vts"] & 0xFFFFFFFF, 0x7777) + nal
        pb["vseq"] += 1
        self._send(c, st, b"$" + bytes([vch]) + struct.pack(">H", len(rtp)) + rtp)

    def _pb_stream(self, c, st):
        pb = st["pb"]
        vch, ach = st["chan"].get(0), st["chan"].get(1)
        codec = self.audio.get(pb["ch"])
        apt = 0 if codec == "PCMU" else 8
        tone_bytes = tone(codec or "PCMA", 320)
        epoch = datetime.datetime(1970, 1, 1)
        try:
            while not st["stop"].is_set():
                if pb["paused"]:
                    time.sleep(0.01)
                    continue
                with pb["lock"]:                  # a PLAY (new position) never lands mid-frame
                    if pb["paused"]:
                        continue
                    pos = pb["pos"]
                    seg = next(((s0, e0) for s0, e0 in pb["segs"] if pos is not None and s0 <= pos < e0), None)
                    if seg is None and pos is not None:
                        nxt = next((s0 for s0, _ in pb["segs"] if s0 > pos), None)
                        if nxt is not None:                        # gap: the RTP clock jumps with it
                            gap = (nxt - pos).total_seconds()
                            pb["vts"] += int(gap * 90000)
                            pb["ats"] += int(gap * 8000)
                            pb["pos"] = nxt
                            continue
                    if pos is None or seg is None or pos >= pb["b"]:
                        if vch is not None:                        # end of the window: RTCP BYE
                            bye = bytes([0x81, 203, 0, 1]) + struct.pack(">I", 0x7777)
                            self._send(c, st, b"$" + bytes([vch + 1]) + struct.pack(">H", len(bye)) + bye)
                        time.sleep(0.05)
                        break
                    rec_ms = int((pos - self.IST - epoch).total_seconds() * 1000)
                    idx = int(round((pos - seg[0]).total_seconds() * 25))
                    key = idx % 50 == 0
                    if pb["scale"] == 1:
                        if self.end_step_back and not pb.get("stepped") and pos >= pb["b"] - datetime.timedelta(seconds=2):
                            pb["stepped"] = True
                            pb["vts"] -= 90000                                 # real NVR quirk at the end
                        if pb["jumps"] and pos >= pb["jumps"][0]:
                            while pb["jumps"] and pos >= pb["jumps"][0]:
                                pb["jumps"].pop(0)
                            pb["vts"] += int(2.04 * 90000)             # clock jump, footage continues
                        if vch is not None:
                            self._pb_send_video(c, st, pb, vch, key, rec_ms)
                        if ach is not None and codec:
                            rtp = (struct.pack(">BBHII", 0x80, apt, pb["aseq"] & 0xFFFF, pb["ats"] & 0xFFFFFFFF, 0x8888)
                                   + tone_bytes)
                            pb["aseq"] += 1
                            self._send(c, st, b"$" + bytes([ach]) + struct.pack(">H", len(rtp)) + rtp)
                        pb["pos"] = pos + datetime.timedelta(milliseconds=40)
                        pb["vts"] += 3600
                        pb["ats"] += 320
                        pause_s = 0.04
                    else:                                          # fast: key frames only, no audio
                        if not key:
                            k = seg[0] + datetime.timedelta(seconds=(int((pos - seg[0]).total_seconds() // 2) + 1) * 2)
                            pb["vts"] += int((k - pos).total_seconds() * 90000)
                            pb["pos"] = k
                            continue
                        if vch is not None:
                            self._pb_send_video(c, st, pb, vch, True, rec_ms)
                        pb["pos"] = pos + datetime.timedelta(seconds=2)
                        pb["vts"] += 180000
                        pause_s = 2.0 / pb["scale"]
                time.sleep(pause_s)
        except OSError:
            pass
        finally:
            with self.lock:
                self.pb_active -= 1
            st["stop"].set()
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class FakeVendorApi:
    """The NVR's web API as far as playback uses it (HTTP, no auth): getVendor /
    getDeviceType / getSoftwareVersion, getCurrentTime (NVR clock = now + drift),
    getUserInfo (account group), mediaFileFind over the SAME recordings dict."""

    def __init__(self, recordings, drift_s=-37, group="admin"):
        self.recordings, self.drift_s, self.group = recordings, drift_s, group
        self.calls = []
        self.delay_s = 0.0                 # a slow NVR: each findFile takes this long
        self._find = {}
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path, _, query = self.path.partition("?")
                q = dict(urllib.parse.parse_qsl(query))
                api.calls.append((path, q.get("action")))
                body = api.answer(path, q)
                data = body.encode()
                self.send_response(200 if body != "Error" else 400)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.host = f"127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def answer(self, path, q):
        act = q.get("action")
        if path.endswith("magicBox.cgi"):
            return {"getVendor": "vendor=CPPLUS\r\n", "getDeviceType": "type=FAKE-NVR-4K\r\n",
                    "getSoftwareVersion": "version=9.9.9.R,build:2026-01-01\r\n"}.get(act, "Error")
        if path.endswith("global.cgi"):
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + FakeNvr.IST
            return "result=" + (now + datetime.timedelta(seconds=self.drift_s)).strftime("%Y-%m-%d %H:%M:%S") + "\r\n"
        if path.endswith("userManager.cgi"):
            return f"user.Group={self.group}\r\nuser.Name=someone\r\n"
        if path.endswith("mediaFileFind.cgi"):
            if act == "factory.create":
                return "result=4242\r\n"
            if act == "findFile":
                try:
                    ch = int(q["condition.Channel"])
                    a = datetime.datetime.strptime(q["condition.StartTime"], "%Y-%m-%d %H:%M:%S")
                    b = datetime.datetime.strptime(q["condition.EndTime"], "%Y-%m-%d %H:%M:%S")
                except (KeyError, ValueError):
                    return "Error"
                if ch < 1:
                    return "Error"
                time.sleep(self.delay_s)
                self._find[q.get("object")] = [(s, e) for s, e in sorted(self.recordings.get(ch, [])) if e > a and s < b]
                return "OK\r\n"
            if act == "findNextFile":                        # at most `count` per call, like the NVRs
                rest = self._find.get(q.get("object"), [])
                n = max(1, int(q.get("count", "100") or 100))
                files, self._find[q.get("object")] = rest[:n], rest[n:]
                out = [f"found={len(files)}"]
                for i, (s, e) in enumerate(files):
                    out += [f"items[{i}].StartTime={s:%Y-%m-%d %H:%M:%S}", f"items[{i}].EndTime={e:%Y-%m-%d %H:%M:%S}",
                            f"items[{i}].Type=dav", f"items[{i}].VideoStream=Main"]
                return "\r\n".join(out) + "\r\n"
            return "OK\r\n"
        return "Error"
