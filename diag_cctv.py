"""CCTV diagnostics -- prove (not guess) why cameras do / don't stream.

Subcommands (run with the CCTV server STOPPED so nothing competes for the NVRs):

    python diag_cctv.py env      host network position, NVR endpoints, IP-change check
    python diag_cctv.py map      all 25 cameras: page, NVR, channel, masked RTSP URL + checks
    python diag_cctv.py rtsp     raw RTSP probe of every camera (exact NVR response codes,
                                 handshake / first-RTP / first-keyframe / GOP timings)
    python diag_cctv.py extra    raw probe of channels that are NOT mapped (missing cameras?)
    python diag_cctv.py cv       OpenCV open + first-frame for every camera (sequential)
    python diag_cctv.py conc     setup-concurrency tests (NVR handshakes 1/2/6 parallel,
                                 OpenCV opens 1/2/6 threads)
    python diag_cctv.py all      everything above; writes JSON if --out is given

Safety: uses the configured credentials only; an authenticated 401 is reported as
AUTH_FAIL and never retried, so it cannot trigger an NVR account lockout. Never
prints usernames/passwords. Every RTSP session is TEARDOWN-ed and closed.
"""
import os
import re
import sys
import json
import time
import socket
import base64
import hashlib
import threading
import subprocess

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import nvr_config as C
from netcheck import on_subnet, local_ipv4s, sanitize_url

PER_PAGE = 6            # must match `const PER = 6` in server.py's page JS
OPEN_MS = 8000          # same OpenCV open/read timeout the server uses
READ_MS = 8000

PASS, OFFLINE, AUTH_FAIL, TIMEOUT = "PASS", "OFFLINE", "AUTH_FAIL", "TIMEOUT"
BAD_CHANNEL, NVR_UNREACHABLE, OTHER = "BAD_CHANNEL", "NVR_UNREACHABLE", "OTHER_ERROR"


def mask_ip(ip):
    p = str(ip).split(".")
    if len(p) == 4 and not ip.startswith(("192.168.", "10.", "172.")):
        return f"{p[0]}.{p[1]}.***.{p[3]}"      # public IP: partially masked
    return ip                                      # private LAN IP: harmless


def masked_url(nvr, ch, subtype=1):
    host, port = C.endpoint(nvr)
    return f"rtsp://***@{mask_ip(host)}:{port}/cam/realmonitor?channel={ch}&subtype={subtype}"


# ─────────────────────────── minimal RTSP client ────────────────────────────
class RtspError(Exception):
    pass


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def _parse_challenge(value):
    chals = [c.strip() for c in value.split("\n") if c.strip()]
    for scheme in ("digest", "basic"):
        for c in chals:
            if c.lower().startswith(scheme):
                params = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', c)}
                params.update({k.lower(): v for k, v in re.findall(r'(\w+)=([^",\s]+)', c)
                               if k.lower() not in params})
                return scheme, params
    return None


class Rtsp:
    def __init__(self, host, port, user, pw, timeout=5.0):
        self.host, self.port, self.user, self.pw = host, port, user, pw
        self.timeout = timeout
        self.sock = None
        self.buf = b""
        self.cseq = 0
        self.auth = None
        self.session = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def _authz(self, method, uri):
        if not self.auth:
            return None
        scheme, p = self.auth
        if scheme == "basic":
            return "Basic " + base64.b64encode(f"{self.user}:{self.pw}".encode()).decode()
        realm, nonce = p.get("realm", ""), p.get("nonce", "")
        ha1 = _md5(f"{self.user}:{realm}:{self.pw}")
        ha2 = _md5(f"{method}:{uri}")
        h = f'Digest username="{self.user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
        if "auth" in p.get("qop", "").split(","):
            nc, cn = "00000001", hashlib.md5(str(time.time()).encode()).hexdigest()[:16]
            h += f'response="{_md5(f"{ha1}:{nonce}:{nc}:{cn}:auth:{ha2}")}", qop=auth, nc={nc}, cnonce="{cn}"'
        else:
            h += f'response="{_md5(f"{ha1}:{nonce}:{ha2}")}"'
        if "opaque" in p:
            h += f', opaque="{p["opaque"]}"'
        return h

    def _recv(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise RtspError("connection closed by NVR")
        self.buf += chunk

    def _skip_interleaved(self):
        while True:
            while len(self.buf) < 4:
                self._recv()
            if self.buf[:1] != b"$":
                return
            n = int.from_bytes(self.buf[2:4], "big")
            while len(self.buf) < 4 + n:
                self._recv()
            self.buf = self.buf[4 + n:]

    def _response(self):
        self._skip_interleaved()
        while b"\r\n\r\n" not in self.buf:
            self._recv()
        i = self.buf.index(b"\r\n\r\n")
        head = self.buf[:i].decode("latin-1")
        self.buf = self.buf[i + 4:]
        lines = head.split("\r\n")
        m = re.match(r"RTSP/1\.\d\s+(\d+)", lines[0])
        if not m:
            raise RtspError(f"bad status line {lines[0][:40]!r}")
        hdrs = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                k = k.strip().lower()
                hdrs[k] = (hdrs[k] + "\n" + v.strip()) if k in hdrs else v.strip()
        n = int(hdrs.get("content-length", "0") or 0)
        while len(self.buf) < n:
            self._recv()
        body, self.buf = self.buf[:n], self.buf[n:]
        return int(m.group(1)), hdrs, body.decode("latin-1", "replace")

    def request(self, method, uri, extra=None):
        self.cseq += 1
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {self.cseq}", "User-Agent: grav-cctv-diag"]
        a = self._authz(method, uri)
        if a:
            lines.append("Authorization: " + a)
        if self.session:
            lines.append("Session: " + self.session)
        lines += [f"{k}: {v}" for k, v in (extra or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return self._response()

    def read_interleaved(self):
        """Return (channel, payload) of the next interleaved packet; skips RTSP msgs."""
        while True:
            while len(self.buf) < 4:
                self._recv()
            if self.buf[:1] == b"$":
                ch, n = self.buf[1], int.from_bytes(self.buf[2:4], "big")
                while len(self.buf) < 4 + n:
                    self._recv()
                pkt, self.buf = self.buf[4:4 + n], self.buf[4 + n:]
                return ch, pkt
            if self.buf.startswith(b"RTSP/"):
                self._response()          # stray keep-alive reply etc.
                continue
            self.buf = self.buf[1:]         # resync


def _rtp(pkt):
    if len(pkt) < 12 or pkt[0] >> 6 != 2:
        return None
    cc, x, pad = pkt[0] & 0x0F, (pkt[0] >> 4) & 1, (pkt[0] >> 5) & 1
    ts = int.from_bytes(pkt[4:8], "big")
    off = 12 + 4 * cc
    if x:
        if len(pkt) < off + 4:
            return None
        off += 4 + 4 * int.from_bytes(pkt[off + 2:off + 4], "big")
    end = len(pkt) - (pkt[-1] if pad else 0)
    return ts, pkt[off:end]


def _is_key(codec, pl):
    """True if this RTP payload starts a random-access point (IDR/IRAP or its SPS)."""
    if not pl:
        return False
    if codec == "H264":
        t = pl[0] & 0x1F
        if t in (5, 7):
            return True
        if t == 28 and len(pl) > 1:
            return bool(pl[1] & 0x80) and (pl[1] & 0x1F) == 5
        if t == 24:
            i = 1
            while i + 2 < len(pl):
                n = int.from_bytes(pl[i:i + 2], "big")
                if (pl[i + 2] & 0x1F) in (5, 7):
                    return True
                i += 2 + n
        return False
    if codec in ("H265", "HEVC"):
        irap = (16, 17, 18, 19, 20, 21, 32, 33)
        t = (pl[0] >> 1) & 0x3F
        if t in irap:
            return True
        if t == 49 and len(pl) > 2:
            return bool(pl[2] & 0x80) and (pl[2] & 0x3F) in (16, 17, 18, 19, 20, 21)
        if t == 48:
            i = 2
            while i + 2 < len(pl):
                n = int.from_bytes(pl[i:i + 2], "big")
                if ((pl[i + 2] >> 1) & 0x3F) in irap:
                    return True
                i += 2 + n
        return False
    return False


def rtsp_probe(nvr, ch, subtype=1, rtp_wait=5.0, key_wait=8.0, gop=True, window=0.0):
    """Raw RTSP DESCRIBE/SETUP/PLAY + RTP inspection. Returns a dict of facts."""
    n = C.NVRS[nvr]
    host, port = C.endpoint(nvr)
    url = f"rtsp://{host}:{port}/cam/realmonitor?channel={ch}&subtype={subtype}"
    r = {"nvr": nvr, "ch": ch, "result": None, "detail": "", "codes": {}}
    cli = Rtsp(host, port, n["user"], n["pass"])
    t0 = time.perf_counter()
    ms = lambda: round((time.perf_counter() - t0) * 1000)
    try:
        try:
            cli.connect()
        except socket.timeout:
            r.update(result=NVR_UNREACHABLE, detail="TCP connect timeout")
            return r
        except OSError as e:
            r.update(result=NVR_UNREACHABLE, detail=f"TCP connect failed ({e.__class__.__name__})")
            return r
        r["tcp_ms"] = ms()
        code, h, body = cli.request("DESCRIBE", url, {"Accept": "application/sdp"})
        if code == 401:
            chal = _parse_challenge(h.get("www-authenticate", ""))
            r["realm"] = (chal or ("", {}))[1].get("realm")
            cli.auth = chal
            code, h, body = cli.request("DESCRIBE", url, {"Accept": "application/sdp"})
            if code == 401:
                r.update(result=AUTH_FAIL, detail="401 with credentials (not retried)")
                r["codes"]["DESCRIBE"] = 401
                return r
        r["codes"]["DESCRIBE"] = code
        r["describe_ms"] = ms()
        if code != 200:
            r.update(result=BAD_CHANNEL if code in (400, 404, 454, 457) else OTHER,
                     detail=f"DESCRIBE -> {code}")
            return r
        # --- SDP: codec + video control URL
        base = h.get("content-base", url)
        codec, control, fps, in_video = None, None, None, False
        for ln in body.splitlines():
            ln = ln.strip()
            if ln.startswith("m="):
                in_video = ln.startswith("m=video")
            elif in_video and ln.startswith("a=rtpmap:"):
                codec = ln.split()[1].split("/")[0].upper()
            elif in_video and ln.startswith("a=control:"):
                control = ln[len("a=control:"):]
            elif in_video and ln.startswith("a=framerate:"):
                try:
                    fps = float(ln.split(":", 1)[1])
                except ValueError:
                    pass
        r["codec"], r["sdp_fps"] = codec, fps
        if not control:
            r.update(result=OTHER, detail="SDP has no video track")
            return r
        if not control.startswith("rtsp://"):
            control = base.rstrip("/") + "/" + control.lstrip("/")
        code, h, _ = cli.request("SETUP", control, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        r["codes"]["SETUP"] = code
        if code != 200:
            r.update(result=OTHER, detail=f"SETUP -> {code}")
            return r
        cli.session = h.get("session", "").split(";")[0].strip()
        code, h, _ = cli.request("PLAY", base, {"Range": "npt=0.000-"})
        r["codes"]["PLAY"] = code
        if code != 200:
            r.update(result=OFFLINE if code in (453, 503) else OTHER, detail=f"PLAY -> {code}")
            return r
        r["handshake_ms"] = ms()
        t_play = time.perf_counter()
        cli.sock.settimeout(0.5)
        first_rtp = first_key = None
        keys, nbytes, frames = [], 0, set()
        stop = t_play + max(rtp_wait, key_wait, window)
        while time.perf_counter() < stop:
            try:
                chn, pkt = cli.read_interleaved()
            except socket.timeout:
                if first_rtp is None and time.perf_counter() - t_play > rtp_wait:
                    break
                continue
            if chn != 0:
                continue
            p = _rtp(pkt)
            if not p:
                continue
            ts, pl = p
            now = time.perf_counter()
            if first_rtp is None:
                first_rtp = now
            nbytes += len(pl)
            frames.add(ts)
            if _is_key(codec, pl) and (not keys or keys[-1] != ts):
                keys.append(ts)
                if first_key is None:
                    first_key = now
            done_gop = (not gop) or len(keys) >= 2
            if first_key and done_gop and now - t_play >= window:
                break
            if first_key is None and now - t_play > key_wait:
                break
        span = max(1e-3, time.perf_counter() - (first_rtp or t_play))
        if first_rtp is None:
            r.update(result=OFFLINE, detail="PLAY 200 but NO media: channel has no live video")
        else:
            r["first_rtp_ms"] = round((first_rtp - t_play) * 1000)
            r["kbps"] = round(nbytes * 8 / span / 1000)
            r["rtp_fps"] = round(len(frames) / span, 1)
            if first_key is None:
                r.update(result=OTHER, detail=f"media flows but no keyframe in {key_wait:.0f}s")
            else:
                r["first_key_ms"] = round((first_key - t_play) * 1000)
                if len(keys) >= 2:
                    r["gop_s"] = round(((keys[1] - keys[0]) & 0xFFFFFFFF) / 90000.0, 2)
                r["result"] = PASS
        return r
    except socket.timeout:
        r.update(result=TIMEOUT, detail=f"NVR did not answer within {cli.timeout:.0f}s")
        return r
    except (RtspError, OSError) as e:
        r.update(result=OTHER, detail=f"{e.__class__.__name__}: {sanitize_url(str(e))[:60]}")
        return r
    finally:
        try:
            if cli.sock and cli.session:
                cli.sock.settimeout(0.5)
                cli.request("TEARDOWN", url)
        except Exception:
            pass
        cli.close()
        r["total_ms"] = ms()


def unauth_identity(nvr):
    """Unauthenticated DESCRIBE -> the 401 realm (device identity), no login attempt."""
    host, port = C.endpoint(nvr)
    url = f"rtsp://{host}:{port}/cam/realmonitor?channel=1&subtype=1"
    cli = Rtsp(host, port, "", "", timeout=4.0)
    try:
        cli.connect()
        code, h, _ = cli.request("OPTIONS", url)
        server = h.get("server", "")
        code2, h2, _ = cli.request("DESCRIBE", url, {"Accept": "application/sdp"})
        chal = _parse_challenge(h2.get("www-authenticate", ""))
        return {"options": code, "server": server, "describe_noauth": code2,
                "auth": chal[0] if chal else None, "realm": chal[1].get("realm") if chal else None}
    except Exception as e:
        return {"error": e.__class__.__name__}
    finally:
        cli.close()


# ─────────────────────────────── OpenCV test ────────────────────────────────
def cv_test(nvr, ch):
    import cv2
    url = C.make_url(nvr, ch)
    r = {"nvr": nvr, "ch": ch}
    t0 = time.perf_counter()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG,
                           [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_MS,
                            cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_MS])
    t1 = time.perf_counter()
    r["open_ms"] = round((t1 - t0) * 1000)
    try:
        if not cap.isOpened():
            r["result"] = TIMEOUT if r["open_ms"] >= OPEN_MS - 300 else OFFLINE
            return r
        fcc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        r["fourcc"] = "".join(chr((fcc >> 8 * i) & 0xFF) for i in range(4)).strip("\x00 ") or None
        r["bufsize_set"] = bool(cap.set(cv2.CAP_PROP_BUFFERSIZE, 1))
        for _ in range(60):
            ok, fr = cap.read()
            if ok and fr is not None:
                t2 = time.perf_counter()
                r["first_frame_ms"] = round((t2 - t1) * 1000)
                r["total_ms"] = round((t2 - t0) * 1000)
                r["res"] = f"{fr.shape[1]}x{fr.shape[0]}"
                r["result"] = PASS
                return r
        r["result"] = OTHER
        r["detail"] = "opened but no decodable frame"
        return r
    finally:
        cap.release()


# ──────────────────────────────── commands ──────────────────────────────────
def cmd_env(out):
    print("== HOST NETWORK POSITION ==")
    ips = local_ipv4s()
    lan = on_subnet(C.CCTV_SUBNET)
    print(f"  host IPv4s        : {ips}")
    print(f"  on CCTV subnet    : {C.CCTV_SUBNET} -> {lan}")
    print(f"  ACCESS_MODE       : {C.ACCESS_MODE} -> {'REMOTE (public IP + forwarded ports)' if C.remote_mode() else 'LAN (private NVR IPs)'}")
    arp = C._arp_table()
    print("\n== NVR ENDPOINTS ==")
    print(f"  {'NVR':5} {'configured IP:port':22} {'MAC now at':15} {'IP changed':10} "
          f"{'effective endpoint':24} {'TCP':5} {'cams':4} identity (401 realm / Server)")
    rows = []
    for k, n in C.NVRS.items():
        cur = arp.get(n["mac"].lower()) if n["mac"] else None
        changed = "NO" if cur == n["ip"] else ("YES" if cur else "UNKNOWN")
        host, port = C.endpoint(k)
        tcp = C._port_open(host, port, timeout=2.0)
        ident = unauth_identity(k)
        ncams = sum(1 for c in C.CAMERAS if c["nvr"] == k)
        realm = ident.get("realm") or ident.get("error")
        print(f"  {k:5} {n['ip'] + ':' + str(n['port']):22} {cur or '-':15} {changed:10} "
              f"{mask_ip(host) + ':' + str(port):24} {'yes' if tcp else 'NO':5} {ncams:<4} "
              f"{realm} / {ident.get('server') or '-'}")
        rows.append({"nvr": k, "configured_ip": n["ip"], "rtsp_port": n["port"],
                     "public_port": n["public_port"], "mac_now_at": cur, "ip_changed": changed,
                     "effective": f"{mask_ip(host)}:{port}", "tcp": tcp, "cameras": ncams,
                     "identity": ident})
    realms = [r["identity"].get("realm") for r in rows]
    if all(realms) and len(set(realms)) == len(realms):
        print("  -> each endpoint answers as a DIFFERENT device (distinct realms): ports are not swapped/shared")
    print("\n  public path (used by a HOSTED server off this LAN):")
    for k, n in C.NVRS.items():
        ok = C._port_open(C.PUBLIC_IP, n["public_port"], timeout=3.0) if C.PUBLIC_IP else False
        print(f"    {k}: {mask_ip(C.PUBLIC_IP)}:{n['public_port']} TCP {'yes' if ok else 'NO (not testable from inside the LAN without NAT loopback)'}")
    out["env"] = {"host_ips": ips, "on_cctv_lan": lan, "remote_mode": C.remote_mode(), "nvrs": rows}


def pages_of():
    return [(p + 1, C.CAMERAS[p * PER_PAGE:(p + 1) * PER_PAGE], p * PER_PAGE)
            for p in range((len(C.CAMERAS) + PER_PAGE - 1) // PER_PAGE)]


def cmd_map(out):
    print("== CAMERA MAPPING (UI index = /stream/<i> = CAMERAS[i]) ==")
    print(f"  {'#':>3} {'page':>4} {'slot':>4}  {'name':22} {'NVR':5} {'ch':>3}  RTSP (masked)")
    rows = []
    for page, cams, start in pages_of():
        for k, cam in enumerate(cams):
            i = start + k
            print(f"  {i + 1:>3} {page:>4} {k + 1:>4}  {cam['name']:22} {cam['nvr']:5} {cam['channel']:>3}  "
                  f"{masked_url(cam['nvr'], cam['channel'])}")
            rows.append({"index": i, "ui_number": i + 1, "page": page, "slot": k + 1,
                         "name": cam["name"], "nvr": cam["nvr"], "channel": cam["channel"],
                         "url": masked_url(cam["nvr"], cam["channel"])})
    print("\n== MAPPING CHECKS ==")
    pairs = [(c["nvr"], c["channel"]) for c in C.CAMERAS]
    dup = sorted({p for p in pairs if pairs.count(p) > 1})
    print(f"  duplicate (NVR,channel) pairs : {dup or 'none'}")
    for k in C.NVRS:
        chs = sorted(c["channel"] for c in C.CAMERAS if c["nvr"] == k)
        gaps = [x for x in range(chs[0], chs[-1] + 1) if x not in chs] if chs else []
        below = list(range(1, chs[0])) if chs and chs[0] > 1 else []
        print(f"  {k}: channels {chs[0]}..{chs[-1]} ({len(chs)} cams); gaps inside range: {gaps or 'none'}; "
              f"unmapped below: {below or 'none'}")
    bad = [c for c in C.CAMERAS if c["nvr"] not in C.NVRS or not (1 <= int(c["channel"]) <= 64)]
    print(f"  invalid NVR / channel entries : {bad or 'none'}")
    # the UI computes page p -> cams.slice(p*6, p*6+6) and streams /stream/<p*6+k>;
    # the server maps /stream/<i> -> STREAMS[i] = CamStream(CAMERAS[i]). Same list, same order.
    print("  UI index -> server index      : identical by construction (/api/cameras returns CAMERAS in order)")
    out["mapping"] = rows


def _table(results, label):
    print(f"\n== {label} ==")
    print(f"  {'#':>3} {'name':22} {'NVR':5} {'ch':>3} {'result':15} {'hs ms':>6} {'rtp ms':>6} "
          f"{'key ms':>6} {'GOP s':>5} {'codec':6} {'kbps':>5}  detail")
    for r in results:
        print(f"  {r.get('ui', ''):>3} {r.get('name', '')[:22]:22} {r['nvr']:5} {r['ch']:>3} {r['result']:15} "
              f"{r.get('handshake_ms', ''):>6} {r.get('first_rtp_ms', ''):>6} {r.get('first_key_ms', ''):>6} "
              f"{r.get('gop_s', ''):>5} {str(r.get('codec') or ''):6} {r.get('kbps', ''):>5}  {r.get('detail', '')}")


def cmd_rtsp(out):
    res = []
    for i, cam in enumerate(C.CAMERAS):
        r = rtsp_probe(cam["nvr"], cam["channel"])
        r.update(ui=i + 1, name=cam["name"])
        res.append(r)
        print(f"  probed #{i + 1:<2} {cam['nvr']} ch{cam['channel']:<2} -> {r['result']}", flush=True)
    _table(res, "RAW RTSP PROBE (all mapped cameras, sequential)")
    out["rtsp"] = res


def cmd_extra(out):
    res = []
    for k in C.NVRS:
        mapped = {c["channel"] for c in C.CAMERAS if c["nvr"] == k}
        for ch in range(1, 17):
            if ch in mapped:
                continue
            r = rtsp_probe(k, ch, rtp_wait=4.0, key_wait=6.0, gop=False)
            r.update(name="(not in grid)")
            res.append(r)
            print(f"  probed {k} ch{ch:<2} -> {r['result']}", flush=True)
    _table(res, "UNMAPPED CHANNELS 1-16 (is any live camera missing from the grid?)")
    out["extra"] = res


def cmd_cv(out):
    res = []
    print(f"  {'#':>3} {'name':22} {'NVR':5} {'ch':>3} {'result':12} {'open ms':>7} {'1st frame ms':>12} "
          f"{'total ms':>8} {'res':10} fourcc buffersize=1")
    for i, cam in enumerate(C.CAMERAS):
        r = cv_test(cam["nvr"], cam["channel"])
        r.update(ui=i + 1, name=cam["name"])
        res.append(r)
        print(f"  {i + 1:>3} {cam['name'][:22]:22} {cam['nvr']:5} {cam['channel']:>3} {r['result']:12} "
              f"{r['open_ms']:>7} {r.get('first_frame_ms', ''):>12} {r.get('total_ms', ''):>8} "
              f"{r.get('res', ''):10} {r.get('fourcc') or '-':6} {r.get('bufsize_set', '-')}", flush=True)
    out["cv"] = res


def _parallel(fn, items, conc):
    sem = threading.Semaphore(conc)
    res = [None] * len(items)
    t0 = time.perf_counter()

    def run(j, it):
        with sem:
            s = time.perf_counter()
            r = fn(*it)
            r["t_start_ms"] = round((s - t0) * 1000)
            r["t_done_ms"] = round((time.perf_counter() - t0) * 1000)
            res[j] = r
    th = [threading.Thread(target=run, args=(j, it)) for j, it in enumerate(items)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    return res, round((time.perf_counter() - t0) * 1000)


def cmd_conc(out, healthy=None):
    healthy = healthy or {"nvr1": [3, 4, 5, 6, 8, 9], "nvr2": [1, 2, 3, 5, 6, 8]}
    out["conc"] = {}
    for k, chs in healthy.items():
        items = [(k, ch) for ch in chs]
        print(f"\n== {k}: NVR-side RTSP handshake + first keyframe, {len(items)} channels ==")
        for conc in (1, 2, len(items)):
            fn = lambda nvr, ch: rtsp_probe(nvr, ch, rtp_wait=5.0, key_wait=8.0, gop=False)
            res, wall = _parallel(fn, items, conc)
            hs = [r.get("handshake_ms") for r in res if r.get("handshake_ms")]
            ok = sum(1 for r in res if r["result"] == PASS)
            print(f"  concurrency {conc}: {ok}/{len(res)} PASS | handshake avg {round(sum(hs) / len(hs)) if hs else '-'} ms "
                  f"max {max(hs) if hs else '-'} ms | first-keyframe-ready (all) {wall} ms")
            out["conc"][f"{k}_rtsp_c{conc}"] = {"wall_ms": wall, "ok": ok,
                                                 "hs_avg": round(sum(hs) / len(hs)) if hs else None}
        print(f"== {k}: OpenCV opens (threads) ==")
        for conc in (1, 2, len(items)):
            res, wall = _parallel(cv_test, items, conc)
            done = sorted(r.get("t_done_ms", 0) for r in res if r["result"] == PASS)
            print(f"  threads {conc}: {len(done)}/{len(res)} PASS | first frame at {done[0] if done else '-'} ms | "
                  f"all frames at {done[-1] if done else '-'} ms | per-camera done: {done}")
            out["conc"][f"{k}_cv_c{conc}"] = {"wall_ms": wall, "done": done}


def cmd_openspeed(out):
    """Server-faithful startup: opens SERIALIZED (like CONNECT_GATE), each camera's
    first read in its OWN thread (like the per-camera worker). Options come from the
    OPENCV_FFMPEG_CAPTURE_OPTIONS env var of THIS process; --warm pre-warms the NVR
    channels with parallel raw RTSP sessions first."""
    import cv2
    nvr = os.getenv("DIAG_NVR", "nvr2")
    chs = [int(x) for x in os.getenv("DIAG_CHS", "1,2,3,5,6,8").split(",")]
    warm = "--warm" in sys.argv
    t0 = time.perf_counter()
    rel = lambda: round((time.perf_counter() - t0) * 1000)
    holders = []
    if warm:
        holders = [threading.Thread(target=rtsp_probe, args=(nvr, ch),
                   kwargs=dict(rtp_wait=6, key_wait=6, gop=False, window=45.0)) for ch in chs]
        for h in holders:
            h.start()
        time.sleep(3.6)
    visible, opens, readers = {}, {}, []

    def first_read(ch, cap):
        for _ in range(120):
            ok, fr = cap.read()
            if ok and fr is not None:
                visible[ch] = rel()
                break
        cap.release()
    for ch in chs:
        s = time.perf_counter()
        cap = cv2.VideoCapture(C.make_url(nvr, ch), cv2.CAP_FFMPEG,
                               [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_MS, cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_MS])
        opens[ch] = round((time.perf_counter() - s) * 1000)
        if cap.isOpened():
            th = threading.Thread(target=first_read, args=(ch, cap))
            th.start()
            readers.append(th)
        else:
            cap.release()
    for th in readers:
        th.join()
    v = sorted(visible.values())
    print(f"  opts={os.getenv('OPENCV_FFMPEG_CAPTURE_OPTIONS')!r} warm={warm}")
    print(f"    serialized open() ms per camera: {[opens[c] for c in chs]}  (sum {sum(opens.values())})")
    print(f"    visible: {len(v)}/{len(chs)} | first at {v[0] if v else '-'} ms | all at {v[-1] if v else '-'} ms")
    out.setdefault("openspeed", []).append({"opts": os.getenv("OPENCV_FFMPEG_CAPTURE_OPTIONS"),
                                            "warm": warm, "nvr": nvr, "opens": opens, "visible": visible})
    for h in holders:
        h.join()


def cmd_pages(out):
    rt = {(r["nvr"], r["ch"]): r for r in out.get("rtsp", [])}
    cv = {(r["nvr"], r["ch"]): r for r in out.get("cv", [])}
    print("\n== PAGE BY PAGE ==")
    rows = []
    for page, cams, start in pages_of():
        n1 = sum(1 for c in cams if c["nvr"] == "nvr1")
        n2 = sum(1 for c in cams if c["nvr"] == "nvr2")
        ok = [c for c in cams if rt.get((c["nvr"], c["channel"]), {}).get("result") == PASS
              or cv.get((c["nvr"], c["channel"]), {}).get("result") == PASS]
        over = max(n1, n2) > int(os.getenv("CCTV_NVR_MAX_CONN", "6"))
        print(f"  PAGE {page}: nvr1={n1} nvr2={n2}  direct PASS {len(ok)}/{len(cams)}  "
              f"exceeds NVR_MAX_CONN from one NVR: {'YES' if over else 'no'}")
        for k, c in enumerate(cams):
            a = rt.get((c["nvr"], c["channel"]), {})
            b = cv.get((c["nvr"], c["channel"]), {})
            print(f"      {start + k + 1:>2} {c['name']:22} {c['nvr']} ch{c['channel']:<2} rtsp={a.get('result', '-'):15} "
                  f"opencv={b.get('result', '-'):10} {a.get('detail', '')}")
        rows.append({"page": page, "nvr1": n1, "nvr2": n2, "pass": len(ok), "total": len(cams)})
    out["pages"] = rows


def main():
    args = sys.argv[1:]
    outp = None
    if "--out" in args:
        j = args.index("--out")
        outp = args[j + 1]
        args = args[:j] + args[j + 2:]
    cmd = args[0] if args else "all"
    out = {}
    if outp and os.path.exists(outp):
        with open(outp) as f:
            out = json.load(f)
    steps = {"env": [cmd_env], "map": [cmd_map], "rtsp": [cmd_rtsp], "extra": [cmd_extra],
             "cv": [cmd_cv], "conc": [cmd_conc], "pages": [cmd_pages], "openspeed": [cmd_openspeed],
             "all": [cmd_env, cmd_map, cmd_rtsp, cmd_extra, cmd_cv, cmd_pages]}[cmd]
    for step in steps:
        step(out)
    if outp:
        with open(outp, "w") as f:
            json.dump(out, f, indent=1, default=str)


if __name__ == "__main__":
    main()
