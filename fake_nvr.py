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
import threading


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
