"""Audio-only RTSP client for the NVR's G.711 microphone track, plus SDP helpers.

WHY A SEPARATE SESSION (measured, see FINAL_CCTV_AUDIO_REPORT.txt)
  * The NVR streams (both subtype=1 and subtype=0) carry an audio track: G.711
    A-law (PCMA) on NVR2 and mu-law (PCMU) on NVR1, 8000 Hz mono -- 64 kbit/s.
  * The video path is OpenCV's FFmpeg backend. It receives that audio track in the
    same RTSP session but cannot hand it out: opening with the audio parameters is
    rejected ("unsupported parameters in .open()"). So audio needs its own session.

This client therefore opens ONE RTSP session per camera that someone is listening
to, SETs UP ONLY THE AUDIO TRACK (the NVR then sends ~64 kbit/s and no video) and
reads the RTP packets over the same TCP connection (interleaved). G.711 is passed
through untouched -- the browser decodes it (a 256-entry table), so nothing is
transcoded. If an NVR refuses an audio-only SETUP, the video track is set up too
and its packets are ignored. The server counts this session as a normal NVR slot.

REAL NVR BEHAVIOUR (measured 2026-09-26 on NVR2 channel 8): the NVR does NOT use the
interleaved channel the client asks for. SETUP of the audio track with
"interleaved=0-1" is answered "interleaved=2-3" (the NVR numbers by track), and the
audio RTP then arrives on channel 2 (RTCP on 3). The channel is therefore always
taken from the NVR's Transport reply. Packets are 1024 bytes (128 ms) of PCMA.

Nothing here logs credentials; RTSP URLs never contain them (digest auth), and the
handshake log (`log`) masks the NVR's address.
"""
import re
import time
import socket
import struct

import rtsp_preflight as rp

STATIC_PT = {0: ("PCMU", 8000, 1), 8: ("PCMA", 8000, 1)}
SUPPORTED = ("PCMU", "PCMA")           # what the browser decoder understands (G.711)
CRLF2 = b"\r\n\r\n"
_MASK_URL = re.compile(r"rtsp://[^/\s;,]+")   # host[:port] (and any user@) of logged URLs


class RtspError(Exception):
    """Protocol / network failure (`code` = short reason for the logs)."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class NoAudio(RtspError):
    """The stream's SDP has no audio track (or none the browser can play)."""


class AuthFailed(RtspError):
    pass


def parse_sdp(sdp):
    """SDP -> {"video": {...}, "audio": {...}} (first track of each kind).
    Track: codec (upper case), rate, channels, pt, control, fmtp."""
    media, cur = [], None
    for ln in (sdp or "").splitlines():
        ln = ln.strip()
        if ln.startswith("m="):
            parts = ln[2:].split()
            cur = {"kind": parts[0], "pts": parts[3:], "rtpmap": {}, "fmtp": {}, "control": None}
            media.append(cur)
        elif cur is None:
            continue
        elif ln.startswith("a=rtpmap:"):
            pt, _, enc = ln[9:].partition(" ")
            cur["rtpmap"][pt.strip()] = enc.strip()
        elif ln.startswith("a=fmtp:"):
            pt, _, f = ln[7:].partition(" ")
            cur["fmtp"][pt.strip()] = f.strip()
        elif ln.startswith("a=control:"):
            cur["control"] = ln[10:].strip()
    out = {}
    for m in media:
        if m["kind"] in out or not m["pts"]:
            continue
        pt = m["pts"][0]
        enc = m["rtpmap"].get(pt)
        if enc:
            p = enc.split("/")
            codec = p[0].upper()
            rate = int(p[1]) if len(p) > 1 and p[1].isdigit() else None
            ch = int(p[2]) if len(p) > 2 and p[2].isdigit() else 1
        elif pt.isdigit() and int(pt) in STATIC_PT:
            codec, rate, ch = STATIC_PT[int(pt)]
        else:
            codec, rate, ch = f"PT{pt}", None, None
        out[m["kind"]] = {"codec": codec, "rate": rate, "channels": ch, "pt": int(pt) if pt.isdigit() else None,
                          "control": m["control"], "fmtp": m["fmtp"].get(pt)}
    return out


def audio_info(sdp):
    """Credential-free audio description of an SDP: None if there is no audio track."""
    a = parse_sdp(sdp).get("audio")
    if not a:
        return None
    return {"codec": a["codec"], "rate": a["rate"], "channels": a["channels"],
            "playable": a["codec"] in SUPPORTED}


def _join(base, control):
    if not control or control == "*":
        return base
    if control.startswith("rtsp://"):
        return control
    return base.rstrip("/") + "/" + control.lstrip("/")


def _parse_head(head):
    lines = head.decode("latin-1").split("\r\n")
    m = re.match(r"RTSP/1\.\d\s+(\d+)", lines[0])
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            k = k.strip().lower()
            hdrs[k] = (hdrs[k] + "\n" + v.strip()) if k in hdrs else v.strip()
    return (int(m.group(1)) if m else 0), hdrs


class AudioSession:
    """One RTSP session: DESCRIBE -> SETUP (audio track) -> PLAY -> RTP packets.

        s = AudioSession(host, port, user, pw, url)
        s.open()                     # raises NoAudio / AuthFailed / RtspError
        for payload, seq, ts in s.read(0.5): ...
        s.keepalive()                # every ~20 s (RTSP session timeout is usually 60 s)
        s.close()                    # TEARDOWN + socket close
    """

    def __init__(self, host, port, user, pw, url, timeout=6.0, alive=None, with_video=False):
        self.host, self.port, self.user, self.pw, self.url = host, int(port), user, pw, url
        self.timeout = timeout
        self.alive = alive or (lambda: True)
        self.with_video = with_video
        self.sock = None
        self.cseq = 0
        self.chal = None
        self.session = None
        self.session_timeout = 60
        self.base = url
        self.audio = None             # parsed audio track
        self.audio_channel = 0        # interleaved channel of the audio RTP (as ASSIGNED by the NVR)
        self.rtcp_channel = 1
        self.video_setup = False
        self.video_channel = None
        self.log = []                 # handshake steps: host masked, never credentials
        self._t0 = time.monotonic()
        self._buf = b""
        self._pending = []            # RTP that arrived while waiting for an RTSP reply
        self._got_first = False
        self.bytes_in = 0
        self.frames = {}              # interleaved frames received per channel
        self.audio_packets = 0        # RTP packets accepted as audio
        self.other_pt = 0             # frames on the audio channel with another payload type

    # ── public ──────────────────────────────────────────────────────────────
    def open(self):
        deadline = time.monotonic() + self.timeout
        try:
            # 5 s: one lost SYN is retransmitted after 3 s (Windows) -- measured on the
            # office public-IP path, where 3 s failed twice in a row and then worked
            self.sock = socket.create_connection((self.host, self.port), timeout=min(5.0, self.timeout))
        except OSError as e:
            self._log(f"TCP connect failed ({type(e).__name__})")
            raise RtspError("TCP_CONNECT_FAILED", f"TCP connect to NVR failed ({type(e).__name__})")
        self._log("TCP connected")
        code, hdrs, body = self._request("DESCRIBE", self.url, {"Accept": "application/sdp"}, deadline)
        self._log(f"DESCRIBE -> {code}")
        if code != 200:
            raise RtspError("DESCRIBE_FAILED", f"DESCRIBE -> {code}")
        self.base = hdrs.get("content-base") or hdrs.get("content-location") or self.url
        tracks = parse_sdp(body)
        a, v = tracks.get("audio"), tracks.get("video")
        self._log(f"Content-Base {self.base}")
        if v:
            self._log(f"video track {v['codec']} pt={v['pt']} control={v['control']!r}")
        if not a:
            self._log("the SDP has no audio track")
            raise NoAudio("NO_AUDIO_TRACK", "the stream's SDP has no audio track")
        self._log(f"audio track {a['codec']} {a['rate']} Hz pt={a['pt']} control={a['control']!r} "
                  f"-> {_join(self.base, a['control'])}")
        if a["codec"] not in SUPPORTED:
            raise NoAudio("UNSUPPORTED_CODEC", f"audio codec {a['codec']} (only G.711 PCMU/PCMA)")
        self.audio = a
        if self.with_video and v:
            self.video_channel = self._setup(_join(self.base, v["control"]), 0, deadline, "video")[0]
            self.video_setup = True
        try:
            chans = self._setup(_join(self.base, a["control"]), 2 if self.video_setup else 0, deadline, "audio")
        except RtspError as e:
            if self.video_setup or not v or e.code != "SETUP_FAILED":
                raise
            # this NVR wants the video track too: set it up and ignore its packets
            self._log("audio-only SETUP refused: falling back to video + audio in the same session")
            self.video_channel = self._setup(_join(self.base, v["control"]), 0, deadline, "video")[0]
            self.video_setup = True
            chans = self._setup(_join(self.base, a["control"]), 2, deadline, "audio")
        # the channels the NVR ASSIGNED (its Transport reply), not the ones requested
        self.audio_channel, self.rtcp_channel = chans
        code, hdrs, _ = self._request("PLAY", self.base, {"Range": "npt=0.000-"}, deadline)
        info = hdrs.get("rtp-info")
        self._log(f"PLAY {self.base} -> {code}" + (f"; RTP-Info: {info}" if info else ""))
        if code != 200:
            raise RtspError("PLAY_FAILED", f"PLAY -> {code}")
        return self.audio

    def read(self, timeout=0.5):
        """RTP packets of the audio track received within `timeout`:
        [(payload bytes, rtp seq, rtp timestamp), ...]; [] if none. Raises
        RtspError("STREAM_CLOSED") when the NVR closes the connection."""
        end = time.monotonic() + timeout
        out = []
        pend, self._pending = self._pending, []
        for chan, data in pend:
            self._take(out, chan, data)
        while True:
            while True:
                item = self._next_item()
                if item is None:
                    break
                if item[0] == "rtp":
                    self._take(out, item[1], item[2])
                # RTSP replies (keep-alive answers) are skipped
            if out or time.monotonic() >= end or not self.alive():
                return out
            self._recv(min(0.25, max(0.01, end - time.monotonic())))

    def keepalive(self):
        """GET_PARAMETER on the session (the reply is skipped by read())."""
        self._send("GET_PARAMETER", self.base, {})

    def close(self):
        s = self.sock
        if s is None:
            return
        try:
            if self.session:
                self._send("TEARDOWN", self.base, {})
        except OSError:
            pass
        self.sock = None
        try:
            s.close()
        except OSError:
            pass

    # ── RTSP requests ──────────────────────────────────────────────────────────
    def _take(self, out, chan, data):
        self.frames[chan] = self.frames.get(chan, 0) + 1
        if chan != self.audio_channel or len(data) < 2 or 200 <= data[1] <= 204:
            return                                  # video / RTCP (even if multiplexed): not audio
        p = _rtp_payload(data)
        if p is None:
            return
        pt = data[1] & 0x7F
        want = self.audio.get("pt") if self.audio else None
        if want is not None and pt != want:
            self.other_pt += 1                      # counted and logged; it IS the audio track's channel
            if self.other_pt == 1:
                self._log(f"audio RTP payload type {pt} differs from the SDP's {want}")
        self.audio_packets += 1
        if not self._got_first:
            self._got_first = True
            self._log(f"first audio RTP packet: interleaved channel {chan}, payload type {pt}, {len(p[0])} bytes")
        out.append(p)

    def _setup(self, uri, ch, deadline, what):
        """SETUP one track over TCP -> (RTP channel, RTCP channel) as ASSIGNED by the
        NVR: the Transport reply wins over the interleaved pair that was requested."""
        code, hdrs, _ = self._request("SETUP", uri,
                                      {"Transport": f"RTP/AVP/TCP;unicast;interleaved={ch}-{ch + 1}"}, deadline)
        tr = hdrs.get("transport", "")
        self._log(f"{what} SETUP {uri} (asked interleaved={ch}-{ch + 1}) -> {code}; "
                  f"Transport: {tr or '-'}; Session: {hdrs.get('session', '-')}")
        if code != 200:
            raise RtspError("SETUP_FAILED", f"{what} SETUP -> {code}")
        sess = hdrs.get("session", "")
        if sess:
            sid, _, rest = sess.partition(";")
            self.session = sid.strip()
            m = re.search(r"timeout=(\d+)", rest)
            if m:
                self.session_timeout = int(m.group(1))
        m = re.search(r"interleaved=(\d+)(?:-(\d+))?", tr)
        if not m:
            return ch, ch + 1
        rtp = int(m.group(1))
        rtcp = int(m.group(2)) if m.group(2) else rtp + 1
        if rtp != ch:
            self._log(f"{what} RTP will arrive on interleaved channel {rtp} (NVR-assigned), not {ch}")
        return rtp, rtcp

    def _log(self, msg):
        msg = _MASK_URL.sub("rtsp://<nvr>", msg).replace(self.host, "<nvr>")
        self.log.append(f"+{round((time.monotonic() - self._t0) * 1000)} ms {msg}")
        del self.log[:-40]

    def _send(self, method, uri, extra):
        self.cseq += 1
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {self.cseq}", "User-Agent: grav-cctv-audio"]
        if self.session:
            lines.append(f"Session: {self.session}")
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
        if self.chal:
            lines.append("Authorization: " + rp.authorization(self.chal, self.user, self.pw, method, uri))
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

    def _request(self, method, uri, extra, deadline, retry_auth=True):
        self._send(method, uri, extra)
        code, hdrs, body = self._response(deadline)
        if code == 401 and retry_auth:
            chal = rp.parse_challenge(hdrs.get("www-authenticate", ""))
            if not chal:
                raise AuthFailed("AUTH_FAIL", "NVR sent 401 without a usable challenge")
            self.chal = chal
            return self._request(method, uri, extra, deadline, retry_auth=False)
        if code == 401:
            raise AuthFailed("AUTH_FAIL", "NVR rejected the configured credentials (RTSP 401)")
        return code, hdrs, body

    def _response(self, deadline):
        """Next RTSP response; RTP that arrives first is kept for read()."""
        while True:
            item = self._next_item()
            if item is None:
                if not self.alive():
                    raise RtspError("ABORTED", "no longer wanted")
                if time.monotonic() >= deadline:
                    raise RtspError("TIMEOUT", "the NVR did not answer in time")
                self._recv(0.25)
            elif item[0] == "resp":
                return item[1], item[2], item[3]
            else:
                self._pending.append((item[1], item[2]))

    # ── framing ───────────────────────────────────────────────────────────────
    def _recv(self, timeout):
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return
        except OSError as e:
            raise RtspError("STREAM_CLOSED", f"connection error ({type(e).__name__})")
        if not chunk:
            raise RtspError("STREAM_CLOSED", "the NVR closed the connection")
        self.bytes_in += len(chunk)
        self._buf += chunk

    def _next_item(self):
        """Consume the next complete item of the TCP stream, in order:
        ("rtp", channel, data) | ("resp", code, headers, body) | None (incomplete)."""
        while self._buf:
            b = self._buf
            if b[0:1] == b"$":
                if len(b) < 4:
                    return None
                n = struct.unpack(">H", b[2:4])[0]
                if len(b) < 4 + n:
                    return None
                self._buf = b[4 + n:]
                return "rtp", b[1], b[4:4 + n]
            if b.startswith(b"RTSP/1."):
                end = b.find(CRLF2)
                if end < 0:
                    return None
                code, hdrs = _parse_head(b[:end])
                n = int(hdrs.get("content-length", "0") or 0)
                if len(b) < end + 4 + n:
                    return None
                body = b[end + 4:end + 4 + n].decode("latin-1", "replace")
                self._buf = b[end + 4 + n:]
                return "resp", code, hdrs, body
            ks = [k for k in (b.find(b"$", 1), b.find(b"RTSP/1.", 1)) if k > 0]     # resync
            self._buf = b[min(ks):] if ks else b""
        return None


def _rtp_payload(pkt):
    """RTP packet -> (payload, seq, timestamp) or None."""
    if len(pkt) < 12 or pkt[0] >> 6 != 2:
        return None
    cc = pkt[0] & 0x0F
    ext = pkt[0] & 0x10
    pad = pkt[0] & 0x20
    seq, ts = struct.unpack(">HI", pkt[2:8])
    off = 12 + 4 * cc
    if ext and len(pkt) >= off + 4:
        off += 4 + 4 * struct.unpack(">H", pkt[off + 2:off + 4])[0]
    end = len(pkt)
    if pad and end > off:
        end -= pkt[-1]
    if off >= end:
        return None
    return pkt[off:end], seq, ts


# ── G.711 (server side: audio level only; the browser does the real decoding) ──────
def _ulaw_table():
    t = []
    for u in range(256):
        u = ~u & 0xFF
        sign, exp, mant = u & 0x80, (u >> 4) & 7, u & 0x0F
        s = (((mant << 3) + 0x84) << exp) - 0x84
        t.append(-s if sign else s)
    return t


def _alaw_table():
    t = []
    for a in range(256):
        a ^= 0x55
        sign, exp, mant = a & 0x80, (a >> 4) & 7, a & 0x0F
        s = ((mant << 4) + 8) if exp == 0 else (((mant << 4) + 0x108) << (exp - 1))
        t.append(s if sign else -s)
    return t


ULAW = _ulaw_table()
ALAW = _alaw_table()
ULAW_SQ, ALAW_SQ = [v * v for v in ULAW], [v * v for v in ALAW]
ULAW_ABS, ALAW_ABS = [abs(v) for v in ULAW], [abs(v) for v in ALAW]


def level(payload, codec):
    """G.711 payload -> (mean square, peak magnitude) of its samples (full scale 32768)."""
    sq, ab = (ALAW_SQ, ALAW_ABS) if codec == "PCMA" else (ULAW_SQ, ULAW_ABS)
    if not payload:
        return 0.0, 0
    return sum(map(sq.__getitem__, payload)) / len(payload), max(map(ab.__getitem__, payload))
