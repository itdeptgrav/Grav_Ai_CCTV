"""RTSP pre-flight: a lock-free liveness check that also pre-warms the NVR channel.

WHY (measured, see FINAL_CCTV_DIAGNOSTIC_REPORT.txt)
  * OpenCV's FFmpeg backend opens RTSP streams one at a time, process-wide, so a
    slow or dead channel blocks every other camera's open.
  * These NVRs answer a DESCRIBE for a channel with NO camera by never replying at
    all -- only a timeout can detect it. Inside OpenCV that timeout is spent while
    holding the global open lock.
  * A healthy NVR2 channel takes ~3 s to answer DESCRIBE when "cold" (the NVR starts
    pulling that camera's substream on demand) but ~20 ms while another connection
    to the channel is open -- even an idle one that only did DESCRIBE.

So each camera first runs this pre-flight in its own thread (in parallel, outside
OpenCV's lock). It sends an authenticated DESCRIBE with a deadline:
  * no answer / error -> the channel is reported dead WITHOUT ever touching the
    serialized OpenCV open path, so it cannot delay healthy cameras;
  * 200 OK            -> the connection is simply held open (no SETUP/PLAY, so no
    video is transferred). That keeps the NVR channel warm, and the real OpenCV
    open that follows gets its DESCRIBE answered in ~20 ms instead of ~3 s.
The caller closes the pre-flight as soon as the OpenCV open has finished.

Safety: one authenticated attempt per pre-flight; a 401 WITH credentials is
reported as AUTH_FAIL and never retried here (the server then pauses that NVR), so
it cannot trigger an NVR account lockout. Nothing here logs credentials.
"""
import re
import time
import socket
import base64
import hashlib

OK = "OK"
DEAD = "DEAD"                  # authenticated DESCRIBE never answered: no live video
AUTH_FAIL = "AUTH_FAIL"        # NVR rejected the configured username/password
BAD_CHANNEL = "BAD_CHANNEL"    # NVR says this channel does not exist
UNREACHABLE = "UNREACHABLE"    # TCP connect to the NVR failed
ERROR = "ERROR"                # anything else (unexpected status, NVR closed, ...)
ABORTED = "ABORTED"            # the viewer left; nothing to report


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def parse_challenge(value):
    """-> ("digest"|"basic", {params}) from WWW-Authenticate header(s), or None."""
    chals = [c.strip() for c in value.split("\n") if c.strip()]
    for scheme in ("digest", "basic"):
        for c in chals:
            if c.lower().startswith(scheme):
                params = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', c)}
                for k, v in re.findall(r'(\w+)=([^",\s]+)', c):
                    params.setdefault(k.lower(), v)
                return scheme, params
    return None


def authorization(chal, user, pw, method, uri):
    scheme, p = chal
    if scheme == "basic":
        return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    ha1 = _md5(f"{user}:{realm}:{pw}")
    ha2 = _md5(f"{method}:{uri}")
    h = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
    if "auth" in p.get("qop", "").split(","):
        nc, cn = "00000001", _md5(str(time.time()))[:16]
        h += f'response="{_md5(f"{ha1}:{nonce}:{nc}:{cn}:auth:{ha2}")}", qop=auth, nc={nc}, cnonce="{cn}"'
    else:
        h += f'response="{_md5(f"{ha1}:{nonce}:{ha2}")}"'
    if "opaque" in p:
        h += f', opaque="{p["opaque"]}"'
    return h


class Preflight:
    """One pre-flight for one camera. run() blocks at most `timeout_s`; call close()
    when done (after a successful run() the connection is kept open on purpose)."""

    def __init__(self, host, port, user, pw, url, timeout_s=6.0, alive=None):
        self.host, self.port, self.user, self.pw = host, int(port), user, pw
        self.url = url
        self.timeout_s = timeout_s
        self.alive = alive or (lambda: True)
        self.sock = None
        self.cseq = 0
        self.result, self.code, self.ms, self.detail = None, None, None, ""

    # -- public ------------------------------------------------------------
    def run(self):
        t0 = time.monotonic()
        deadline = t0 + self.timeout_s
        try:
            try:
                self.sock = socket.create_connection((self.host, self.port),
                                                     timeout=min(3.0, self.timeout_s))
            except OSError as e:
                return self._done(UNREACHABLE, None, t0, f"TCP connect to NVR failed ({type(e).__name__})")
            self.sock.settimeout(0.25)            # short slices -> abort within 0.25 s
            code, hdrs = self._request(None, deadline)
            if code is None:
                return self._outcome_no_reply(hdrs, t0, "unauthenticated DESCRIBE")
            if code == 401:
                chal = parse_challenge(hdrs.get("www-authenticate", ""))
                if not chal:
                    return self._done(ERROR, 401, t0, "NVR sent 401 without a usable challenge")
                code, hdrs = self._request(chal, deadline)
                if code is None:
                    return self._outcome_no_reply(hdrs, t0, "DESCRIBE")
                if code == 401:
                    return self._done(AUTH_FAIL, 401, t0, "NVR rejected the configured credentials (RTSP 401)")
            if code == 200:
                return self._done(OK, 200, t0, "")
            if code in (400, 404, 454, 457):
                return self._done(BAD_CHANNEL, code, t0, f"channel not available on this NVR (RTSP {code})")
            if code in (453, 503):
                return self._done(ERROR, code, t0, f"NVR busy / out of resources (RTSP {code})")
            return self._done(ERROR, code, t0, f"unexpected RTSP status {code}")
        except Exception as e:                       # never let a probe crash a worker
            return self._done(ERROR, None, t0, f"pre-flight error ({type(e).__name__})")

    def close(self):
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    # -- internals ---------------------------------------------------------
    def _done(self, result, code, t0, detail):
        self.result, self.code, self.detail = result, code, detail
        self.ms = round((time.monotonic() - t0) * 1000)
        if result != OK:
            self.close()
        return result

    def _outcome_no_reply(self, why, t0, what):
        if why == "aborted":
            return self._done(ABORTED, None, t0, "viewer left")
        if why == "closed":
            return self._done(ERROR, None, t0, f"NVR closed the connection during {what}")
        return self._done(DEAD, None, t0,
                          f"no live video: NVR did not answer {what} within {self.timeout_s:.0f}s")

    def _request(self, chal, deadline):
        self.cseq += 1
        lines = [f"DESCRIBE {self.url} RTSP/1.0", f"CSeq: {self.cseq}",
                 "User-Agent: grav-cctv", "Accept: application/sdp"]
        if chal:
            lines.append("Authorization: " + authorization(chal, self.user, self.pw, "DESCRIBE", self.url))
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return self._response(deadline)

    def _response(self, deadline):
        """-> (code, headers) or (None, "timeout"|"aborted"|"closed")."""
        buf, need = b"", None
        code, hdrs = None, {}
        while True:
            if code is None and b"\r\n\r\n" in buf:
                head, _, buf = buf.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                m = re.match(r"RTSP/1\.\d\s+(\d+)", lines[0])
                if not m:
                    return None, "closed"
                code = int(m.group(1))
                for ln in lines[1:]:
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        k = k.strip().lower()
                        hdrs[k] = (hdrs[k] + "\n" + v.strip()) if k in hdrs else v.strip()
                need = int(hdrs.get("content-length", "0") or 0)
            if code is not None and len(buf) >= need:
                return code, hdrs                    # body (SDP) fully read; not needed
            if not self.alive():
                return None, "aborted"
            if time.monotonic() >= deadline:
                return None, "timeout"
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return None, "closed"
            buf += chunk
