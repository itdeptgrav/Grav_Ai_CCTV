"""RTSP client for RECORDED playback on the CP PLUS / Dahua NVRs (proven behaviour,
see NVR_PLAYBACK_CAPABILITY_REPORT.txt):

  URL   rtsp://<nvr>/cam/playback?channel=N&subtype=0&starttime=<local>&endtime=<local>
        (main stream only -- subtype=1 recordings do not exist -> 404)
  DESCRIBE -> 200 + "a=range:npt=0-<recorded seconds>" (only the RECORDED part of the
        window), or 404 when nothing is recorded in it
  PLAY  "Range: clock=<UTC>-" positions at the key frame at/before that time (up to
        ~2 s earlier; the camera GOP is 2 s); inside a recording gap it starts at the
        next recording. Always PAUSE before a positioning PLAY: a PLAY while playing
        mixes old and new packets. PLAY without Range after PAUSE resumes in place.
  Scale 2.0 / 4.0 -> fast forward with KEY FRAMES ONLY and no audio; 0.5 is not
        supported (the NVR sends one frame and stops).
  RTP timestamps (90 kHz video, 8 kHz audio) advance with the recording and JUMP by
        the gap length when playback crosses a recording gap; they continue across
        PAUSE/PLAY. There is no absolute time in the stream (no SEI, no header
        extension, RTCP carries today's clock).
  End of window: RTCP BYE, then the NVR closes the connection.
Credentials: digest auth only; URLs never contain them; logs mask the NVR address.
"""
import re
import time
import base64
import socket
import struct

import rtsp_audio as ra
import rtsp_preflight as rp
import playback_time as pt


def playback_url(host, port, channel, a, b):
    return (f"rtsp://{host}:{port}/cam/playback?channel={int(channel)}&subtype=0"
            f"&starttime={pt.url_time(a)}&endtime={pt.url_time(b)}")


def describe_range(host, port, user, pw, channel, a, b, timeout=8.0, alive=None):
    """Recording availability over RTSP only (works wherever RTSP works).
    -> (status, recorded_seconds): "ok" | "none" | "auth" | "unreachable" | "error"."""
    p = rp.Preflight(host, port, user, pw, playback_url(host, port, channel, a, b),
                     timeout_s=timeout, alive=alive)
    try:
        res = p.run()
    finally:
        p.close()
    if res == rp.OK:
        m = re.search(r"a=range:npt=[\d.]*-([\d.]+)", p.sdp or "")
        return "ok", (float(m.group(1)) if m else None)
    if res == rp.BAD_CHANNEL and p.code == 404:
        return "none", 0.0
    if res == rp.AUTH_FAIL:
        return "auth", None
    if res == rp.UNREACHABLE:
        return "unreachable", None
    return "error", None


class NoRecording(ra.RtspError):
    """DESCRIBE -> 404: nothing is recorded in the requested window."""


def _seq_before(a, b):
    """RTP sequence a comes before b (mod 2^16)."""
    return ((a - b) & 0xFFFF) >= 0x8000


class RtspPlayback(ra.AudioSession):
    """One playback session: DESCRIBE, SETUP video (+ audio), then PLAY / PAUSE / seek.
    next_packet() hands out ("v", rtp) / ("a", rtp) / ("bye",) in arrival order."""

    def __init__(self, host, port, user, pw, channel, a, b, timeout=8.0, alive=None):
        super().__init__(host, port, user, pw, playback_url(host, port, channel, a, b),
                         timeout=timeout, alive=alive)
        self.video = None
        self.vch = self.vrtcp = self.ach = self.artcp = None
        self.recorded_s = None
        self.audio_channel = -1            # the audio framing of the base class is not used
        self._min_seq = {}                 # track -> first RTP seq of the current PLAY
        self.bye = False
        self.playing = False               # a PLAY is in effect (PAUSE before repositioning)
        self.depack = None

    def open(self):
        deadline = time.monotonic() + self.timeout
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=min(5.0, self.timeout))
        except OSError as e:
            raise ra.RtspError("TCP_CONNECT_FAILED", f"TCP connect to NVR failed ({type(e).__name__})")
        self._log("TCP connected")
        code, hdrs, body = self._request("DESCRIBE", self.url, {"Accept": "application/sdp"}, deadline)
        self._log(f"DESCRIBE -> {code}")
        if code == 404:
            raise NoRecording("NO_RECORDING", "nothing recorded in this window (DESCRIBE 404)")
        if code != 200:
            raise ra.RtspError("DESCRIBE_FAILED", f"DESCRIBE -> {code}")
        self.base = hdrs.get("content-base") or hdrs.get("content-location") or self.url
        tracks = ra.parse_sdp(body)
        m = re.search(r"a=range:npt=[\d.]*-([\d.]+)", body)
        self.recorded_s = float(m.group(1)) if m else None
        self.video = tracks.get("video")
        if not self.video:
            raise ra.RtspError("NO_VIDEO", "the recording has no video track")
        self.vch, self.vrtcp = self._setup(ra._join(self.base, self.video["control"]), 0, deadline, "video")
        a = tracks.get("audio")
        if a and a["codec"] in ra.SUPPORTED:
            try:
                self.ach, self.artcp = self._setup(ra._join(self.base, a["control"]), 2, deadline, "audio")
                self.audio = a
            except ra.RtspError:
                self.audio = None                        # video only
        self._log(f"recording {self.video['codec']}"
                  + (f" + {self.audio['codec']} audio" if self.audio else " (no audio)")
                  + f", recorded {self.recorded_s} s of the window")
        return self.video

    def play(self, t=None, scale=1.0):
        """PLAY at NVR-local time `t` (absolute, sent as UTC clock=) or resume (t=None).
        -> RTP-Info {"v": (seq, rtptime), "a": (...)} of the new position."""
        extra = {}
        if t is not None:
            extra["Range"] = f"clock={pt.rtsp_clock(t)}-"
        if scale and float(scale) != 1.0:
            extra["Scale"] = f"{float(scale):.1f}"
        code, hdrs, _ = self._request("PLAY", self.base, extra, time.monotonic() + self.timeout)
        self._log(f"PLAY {extra or '(resume)'} -> {code}; Range {hdrs.get('range')}")
        if code != 200:
            raise ra.RtspError("PLAY_FAILED", f"PLAY -> {code}")
        info = {}
        for part in (hdrs.get("rtp-info") or "").split(","):
            m = re.search(r"trackID=(\d+).*?seq=(\d+).*?rtptime=(\d+)", part)
            if m:
                trk = "v" if m.group(1) == str(self.video.get("control", "trackID=0")).split("=")[-1] else "a"
                info[trk] = (int(m.group(2)), int(m.group(3)))
        self._min_seq = {k: v[0] for k, v in info.items()}
        return info

    def pause(self):
        code, _, _ = self._request("PAUSE", self.base, {}, time.monotonic() + self.timeout)
        if code != 200:
            raise ra.RtspError("PAUSE_FAILED", f"PAUSE -> {code}")

    def next_packet(self, timeout=0.25):
        """Next media item or None within `timeout`. Stale packets of a previous
        position (sequence before this PLAY's RTP-Info) are dropped."""
        end = time.monotonic() + timeout
        while True:
            while self._pending:
                c, d = self._pending.pop(0)
                it = self._classify(c, d)
                if it:
                    return it
            item = self._next_item()
            if item is not None:
                if item[0] == "rtp":
                    it = self._classify(item[1], item[2])
                    if it:
                        return it
                continue                                  # RTSP replies (keep-alive) are skipped
            if time.monotonic() >= end or not self.alive():
                return None
            self._recv(min(0.1, max(0.01, end - time.monotonic())))

    def _classify(self, chan, data):
        if chan == self.vch or chan == self.ach:
            trk = "v" if chan == self.vch else "a"
            if len(data) < 12:
                return None
            seq = struct.unpack(">H", data[2:4])[0]
            ms = self._min_seq.get(trk)
            if ms is not None:
                if _seq_before(seq, ms):
                    return None                           # still from the previous position
                self._min_seq.pop(trk, None)
            return (trk, data)
        if chan in (self.vrtcp, self.artcp):
            i = 0
            while i + 4 <= len(data):                     # compound RTCP: look for BYE (203)
                if data[i + 1] == 203:
                    self.bye = True
                    return ("bye",)
                i += 4 * (struct.unpack(">H", data[i + 2:i + 4])[0] + 1)
        return None


class Depacketizer:
    """H.265 / H.264 RTP -> access units (Annex-B bytes, RTP timestamp, key frame?)."""
    SC = b"\x00\x00\x00\x01"

    def __init__(self, codec, fmtp=None):
        self.hevc = str(codec).upper() in ("H265", "HEVC")
        self.params = b""
        for k in ("sprop-vps", "sprop-sps", "sprop-pps", "sprop-parameter-sets"):
            m = re.search(k + r"=([^;\s]+)", fmtp or "")
            if m:
                for part in m.group(1).split(","):
                    try:
                        self.params += self.SC + base64.b64decode(part + "=" * (-len(part) % 4))
                    except (ValueError, TypeError):
                        pass
        self._au = bytearray()
        self._ts = None
        self._key = False

    def _nal(self, nal):
        if not nal:
            return
        t = (nal[0] >> 1) & 0x3F if self.hevc else nal[0] & 0x1F
        if (self.hevc and 16 <= t <= 21) or (not self.hevc and t == 5):
            self._key = True
        self._au += self.SC + nal

    def push(self, pkt):
        """-> list of finished access units [(bytes, ts, key)]."""
        out = []
        r = ra._rtp_payload(pkt)
        if r is None:
            return out
        p, _seq, ts = r
        if self._ts is not None and ts != self._ts and self._au:
            out.append(self._flush())
        self._ts = ts
        if self.hevc:
            t = (p[0] >> 1) & 0x3F
            if t == 48:                                   # aggregation packet
                i = 2
                while i + 2 <= len(p):
                    n = struct.unpack(">H", p[i:i + 2])[0]
                    self._nal(p[i + 2:i + 2 + n])
                    i += 2 + n
            elif t == 49 and len(p) > 3:                  # fragmentation unit
                fu = p[2]
                if fu & 0x80:
                    t2 = fu & 0x3F
                    if 16 <= t2 <= 21:
                        self._key = True
                    self._au += self.SC + bytes([(p[0] & 0x81) | (t2 << 1), p[1]])
                self._au += p[3:]
            elif t < 48:
                self._nal(p)
        else:
            t = p[0] & 0x1F
            if t == 24:                                   # STAP-A
                i = 1
                while i + 2 <= len(p):
                    n = struct.unpack(">H", p[i:i + 2])[0]
                    self._nal(p[i + 2:i + 2 + n])
                    i += 2 + n
            elif t == 28 and len(p) > 2:                  # FU-A
                if p[1] & 0x80:
                    if p[1] & 0x1F == 5:
                        self._key = True
                    self._au += self.SC + bytes([(p[0] & 0xE0) | (p[1] & 0x1F)])
                self._au += p[2:]
            elif t < 24:
                self._nal(p)
        if pkt[1] & 0x80:                                 # marker: last packet of the frame
            out.append(self._flush())
        return out

    def _flush(self):
        au, ts, key = bytes(self._au), self._ts, self._key
        self._au, self._key = bytearray(), False
        return au, ts, key
