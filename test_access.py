"""Person-wise CCTV permissions -- offline.

A fake GRAV CMS answers GET /api/cctv/internal/access (per person: which cameras, and
live / audio / playback on each; admin; refused) exactly like grav-cms-backend does; the
tests mint real HS256 sign-in tokens with the test secret and sign in through /sso. Live
video is a stubbed cv2 (no network), audio and playback talk to fake NVRs on 127.0.0.1,
and a socket guard makes any other outbound connection fail the run -- the real NVRs are
never touched.

Covers the required cases: CEO/admin (key link and CMS sign-in) sees everything; user A
(HR only: live, no audio, playback) and user B (Reception live+audio, Corridor
live+playback) see exactly their cameras with the right controls; CCTV access with no
camera -> empty; no CCTV access -> refused; direct stream / snapshot / stream-info /
Original / audio / playback URLs of other cameras -> 403; playback "Select all" = only
permitted cameras; a revoked permission ends open video, sound and playback within
seconds (pushed by the CMS, and by the cache TTL without a push); a shared camera worker
never lets an unauthorised viewer attach. Plus sign-in token checks, tampered cookies,
sign-out, the CMS-only endpoints, fail-closed when the CMS cannot answer, and
administrator-only settings.

Run:  python test_access.py        (exits non-zero on any failure)
"""
import os
import sys
import json
import time
import hmac
import types
import base64
import socket
import struct
import hashlib
import secrets
import datetime
import tempfile
import threading
import collections
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TMP = tempfile.mkdtemp()
SSO = "offline-test-sso-secret-" + secrets.token_hex(16)
SVC = "offline-test-service-key-" + secrets.token_hex(16)
ADMIN_KEY = "offline-admin-key-" + secrets.token_hex(8)
SETTINGS_FILE = os.path.join(TMP, "camera-settings.json")
# Reception's and the Corridor's sound forced "on", so their audio sockets really run
json.dump({"version": 1, "revision": 1, "cameras": {"nvr1:9": {"audio": "on"}, "nvr1:14": {"audio": "on"}}},
          open(SETTINGS_FILE, "w"))
os.environ.update({
    "CCTV_PERSISTENT": "1", "CCTV_PREFLIGHT": "0", "CCTV_NVR_MAX_CONN": "6",
    "CCTV_LOG_EVENTS": os.environ.get("ACCESS_TEST_LOG", "0"),
    "CCTV_SETTINGS_FILE": SETTINGS_FILE,
    "CCTV_WARM_STEP_S": "0.05", "CCTV_MIN_BG_HOT_S": "0.3", "CCTV_BG_SWAP_S": "0.2",
    "CCTV_REFRESH_EVERY_S": "0", "CCTV_IDLE_FPS": "1", "CCTV_STREAM_FPS": "8",
    "CCTV_PLAYBACK_MAX_WORKERS": "3", "CCTV_NVR1_PLAYBACK_MAX": "2", "CCTV_NVR2_PLAYBACK_MAX": "2",
    "CCTV_PLAYBACK_GRACE_S": "1", "CCTV_PLAYBACK_SEARCH_CACHE_S": "0",
    "CCTV_TOKEN": ADMIN_KEY, "CCTV_SSO_SECRET": SSO, "CCTV_SERVICE_KEY": SVC,
    "CCTV_CMS_API_URL": "http://127.0.0.1:9",            # replaced once the fake CMS listens
    "CCTV_CMS_APP_URL": "http://cms.test", "CCTV_PERMISSION_TTL_S": "30", "CCTV_PERMISSION_GRACE_S": "0",
    "CCTV_AUDIO": "1",
})
for k in ("CCTV_NVR1_MAX_CONN", "CCTV_NVR2_MAX_CONN"):
    os.environ.pop(k, None)

# ── nothing but 127.0.0.1: the real NVRs must never be reached from a test ──
_real_create = socket.create_connection
BLOCKED = []


def _guard(addr, *a, **k):
    if addr[0] not in ("127.0.0.1", "localhost"):
        BLOCKED.append(addr)
        raise OSError(f"test guard: outbound connection to {addr} refused")
    return _real_create(addr, *a, **k)


socket.create_connection = _guard

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
import cctv_access as ca                              # noqa: E402
from fake_nvr import FakeNvr, FakeVendorApi           # noqa: E402

S = server.STREAMS
IDX = {server.SETTINGS.keys[i]: i for i in range(len(S))}
HR, RECEPTION, CORRIDOR, CEO = IDX["nvr2:8"], IDX["nvr1:9"], IDX["nvr1:14"], IDX["nvr1:4"]

# fake NVRs: recordings for the last two hours on every channel (playback), audio on all
B = (pt.nvr_now() - datetime.timedelta(hours=2)).replace(second=0, microsecond=0)
FAKE = {
    "nvr1": FakeNvr(server.NVRS["nvr1"]["user"], server.NVRS["nvr1"]["pass"], {c: "PCMU" for c in range(1, 17)}),
    "nvr2": FakeNvr(server.NVRS["nvr2"]["user"], server.NVRS["nvr2"]["pass"], {c: "PCMA" for c in range(1, 17)}),
}
for nvr in FAKE:
    for c in range(1, 17):
        FAKE[nvr].recordings[c] = [(B, B + datetime.timedelta(minutes=60))]
API = {k: FakeVendorApi(FAKE[k].recordings) for k in FAKE}
pb.HOOK.endpoint = lambda nvr: ("127.0.0.1", FAKE[nvr].port)
pb.HOOK.remote = lambda: False
pb.HOOK.api_host = lambda nvr: API[nvr].host
server._audio_endpoint = lambda nvr: ("127.0.0.1", FAKE[nvr].port)
server.MONITOR.get = lambda nvr: type("U", (), {"checked": time.time(), "reachable": True, "label": "ok"})()
for _k in server.NVRS:
    server._AUTH_OK[_k] = True
FRAME = np.zeros((72, 128, 3), dtype="uint8")


class FakeDecoder:
    def __init__(self, on_frame, params=b"", label="", want=None):
        self.on_frame, self.closed, self.want = on_frame, False, want

    def push(self, au, meta):
        if not self.closed:
            self.on_frame(FRAME if self.want is None or self.want(meta) else None, meta)

    def close(self):
        self.closed = True


pb.new_decoder = FakeDecoder
pb.new_key_decoder = FakeDecoder

# ── the fake GRAV CMS (GET /api/cctv/internal/access, like grav-cms-backend) ──
PERMS = {}                 # sub -> decision (allowed, admin, cameras, denialCode, message)
CMS_CALLS = []


class CmsHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if self.headers.get("X-CCTV-Service-Key") != SVC or u.path != "/api/cctv/internal/access":
            return self._out(401, {"success": False})
        sub = qs.get("id", [""])[0]
        CMS_CALLS.append(sub)
        if sub.startswith("down"):
            return self._out(503, {"success": False})
        d = PERMS.get(sub) or {"allowed": False, "admin": False, "denialCode": "CCTV_NOT_ENABLED",
                               "message": "CCTV is not enabled for your department.", "cameras": []}
        self._out(200, dict(d, success=True, person={"email": qs.get("email", [""])[0], "name": f"Test {sub}"}))

    def _out(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


CMS = ThreadingHTTPServer(("127.0.0.1", 0), CmsHandler)
threading.Thread(target=CMS.serve_forever, daemon=True).start()
os.environ["CCTV_CMS_API_URL"] = f"http://127.0.0.1:{CMS.server_address[1]}"

FAILS = []
PORT = None


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


def cam(key, live=False, audio=False, playback=False):
    return {"key": key, "live": live, "audio": audio, "playback": playback}


def b64(b):
    return base64.urlsafe_b64encode(b if isinstance(b, bytes) else json.dumps(b).encode()).decode().rstrip("=")


def token(sub, subj="employee", aud="grav-cctv", iss="grav-cms", life=90, iat=None, secret=None, jti=None, alg="HS256"):
    now = int(time.time()) if iat is None else iat
    h = b64({"alg": alg, "typ": "JWT"})
    p = b64({"sub": sub, "subj": subj, "tv": 0, "email": f"{sub}@grav.test", "name": sub, "iat": now,
             "exp": now + life, "aud": aud, "iss": iss, **({"jti": jti} if jti != "" else {}),
             **({} if jti == "" else {"jti": jti or secrets.token_hex(16)})})
    s = b64(hmac.new((secret or SSO).encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{s}"


def http(method, path, body=None, cookie=None, headers=None, raw=False):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=30)
    data = json.dumps(body).encode() if body is not None else b""
    head = f"{method} {path} HTTP/1.1\r\nHost: t\r\nConnection: close\r\nContent-Length: {len(data)}\r\n"
    if body is not None:
        head += "Content-Type: application/json\r\n"
    if cookie:
        head += f"Cookie: {cookie}\r\n"
    for k, v in (headers or {}).items():
        head += f"{k}: {v}\r\n"
    s.sendall((head + "\r\n").encode() + data)
    buf = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    s.close()
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    code = int(lines[0].split(" ")[1])
    hdrs = collections.defaultdict(list)
    for line in lines[1:]:
        k, _, v = line.partition(":")
        hdrs[k.strip().lower()].append(v.strip())
    if raw:
        return code, hdrs, rest
    try:
        return code, hdrs, json.loads(rest.decode())
    except ValueError:
        return code, hdrs, rest.decode("utf-8", "replace")


def sign_in(sub, subj="employee"):
    code, h, _ = http("GET", "/sso?token=" + urllib.parse.quote(token(sub, subj)))
    cookie = next((c.split(";")[0] for c in h.get("set-cookie", []) if c.startswith(ca.COOKIE + "=")), None)
    return code, cookie, h


def first_part(path, cookie=None):
    """Status code of a streaming URL (MJPEG / snapshot), reading only its start."""
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: t\r\n{('Cookie: ' + cookie + chr(13) + chr(10)) if cookie else ''}\r\n".encode())
    buf = b""
    while b"\r\n" not in buf:
        c = s.recv(4096)
        if not c:
            break
        buf += c
    s.close()
    return int(buf.split(b" ")[1]) if buf else 0


class Stream(threading.Thread):
    """A viewer holding /stream/<i> open; records when the server ended it."""
    def __init__(self, path, cookie=None):
        super().__init__(daemon=True)
        self.path, self.cookie, self.status, self.bytes, self.ended_at, self.stop = path, cookie, None, 0, None, False
        self.start()
        wait(lambda: self.status is not None, 5.0)

    def run(self):
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=20)
            self.sock = s
            s.sendall(f"GET {self.path} HTTP/1.1\r\nHost: t\r\n{('Cookie: ' + self.cookie + chr(13) + chr(10)) if self.cookie else ''}\r\n".encode())
            first = True
            while not self.stop:
                c = s.recv(65536)
                if not c:
                    break
                if first:
                    self.status = int(c.split(b" ")[1])
                    first = False
                self.bytes += len(c)
        except OSError:
            pass
        finally:
            self.ended_at = time.monotonic()
            if self.status is None:
                self.status = 0

    def close(self):
        self.stop = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        except (OSError, AttributeError):
            pass


class Ws(threading.Thread):
    """A WebSocket client (audio or playback): status line, JSON messages, closed?"""
    def __init__(self, path, cookie=None):
        super().__init__(daemon=True)
        self.path, self.cookie = path, cookie
        self.status_line, self.msgs, self.closed, self.stop, self.sock = None, [], False, False, None
        self._wl = threading.Lock()
        self.start()
        wait(lambda: self.status_line is not None, 5.0)

    def run(self):
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=30)
            self.sock = s
            s.sendall((f"GET {self.path} HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       + (f"Cookie: {self.cookie}\r\n" if self.cookie else "")
                       + f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
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
                    if op == 1:
                        self.msgs.append(json.loads(data))
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
        return next((m for m in reversed(self.msgs) if m.get("t") == "state"), {})

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


def search(cookie, cams, **kw):
    now = pt.nvr_now()
    return http("POST", "/api/playback/search", dict({"from": pt.fmt_local(B + datetime.timedelta(minutes=5)),
                "to": pt.fmt_local(min(now, B + datetime.timedelta(minutes=50))), "cameras": cams, "wait": 5}, **kw),
                cookie=cookie)


def push(email=None, everyone=False):
    return http("POST", "/api/internal/access-changed", {"all": True} if everyone else {"email": email},
                headers={"X-CCTV-Service-Key": SVC})


def keys_of(listing):
    return sorted(c["key"] for c in listing)


# ═══════════════════════════════════════════════════════════════════════════
def test_unauthenticated_and_key_link():
    for path in ("/", "/playback", "/settings"):
        code, _, body = http("GET", path)
        check(f"{path} signed out -> 401 page pointing to the GRAV CMS", code == 401 and "Open CCTV from the GRAV CMS" in body, code)
    for path in ("/api/cameras", "/api/me", "/api/playback/config", "/api/status", f"/api/stream-info/{HR}"):
        check(f"{path} signed out -> 401", http("GET", path)[0] == 401)
    for path in (f"/stream/{HR}", f"/snapshot/{HR}", f"/stream/{HR}?quality=original"):
        check(f"{path} signed out -> 401 (no picture)", first_part(path) == 401)
    check("/audio/<HR> signed out -> 401 (no socket)", " 401 " in (Ws(f"/audio/{HR}").status_line or ""))
    check("POST /api/playback/search signed out -> 401", search(None, [HR])[0] == 401)
    # a key set to a value that was published (its SHA-256 in PUBLISHED_SHA256) is refused
    old = "retired-test-key-" + secrets.token_hex(4)
    ca.PUBLISHED_SHA256.add(ca.sha256(old))
    code, _, body = http("GET", f"/?key={old}")
    check("the retired shared key -> 401 'This key link was retired'", code == 401 and "retired" in body, code)
    code, _, cams = http("GET", f"/api/cameras?key={ADMIN_KEY}")
    check("administrators' key link: every camera, with live / audio / playback",
          code == 200 and len(cams) == len(S) and all(c["permissions"] == {"live": True, "audio": True, "playback": True} for c in cams),
          (code, len(cams) if isinstance(cams, list) else cams))
    check("... Settings, status and the settings API open with it",
          http("GET", f"/settings?key={ADMIN_KEY}")[0] == 200 and http("GET", f"/api/status?key={ADMIN_KEY}")[0] == 200
          and http("GET", f"/api/camera-settings?key={ADMIN_KEY}")[0] == 200)
    code, _, cfg = http("GET", f"/api/playback/config?key={ADMIN_KEY}")
    check("... playback lists every camera ('Select all' = all of them for an administrator)",
          code == 200 and len(cfg["cameras"]) == len(S) and cfg["viewer"]["admin"])


def test_key_link_session():
    """The administrators' key link: full access with no password, and the key does not
    stay in the address bar -- a browser visit is turned into a signed cookie at once."""
    html = {"Accept": "text/html,application/xhtml+xml"}
    code, h, _ = http("GET", f"/?key={ADMIN_KEY}", headers=html)
    set_c = [c for c in h.get("set-cookie", []) if c.startswith(ca.KEY_COOKIE + "=")]
    cookie = set_c[0].split(";")[0] if set_c else None
    check("key link opened in a browser: 302 to the same page WITHOUT the key, an HttpOnly cookie instead",
          code == 302 and h.get("location") == ["/"] and cookie and "HttpOnly" in set_c[0]
          and f"Max-Age={ca.KEY_SESSION_TTL}" in set_c[0] and ADMIN_KEY not in set_c[0], (code, h))
    code, h, _ = http("GET", f"/playback?key={ADMIN_KEY}&x=1", headers=html)
    check("... other query parameters are kept", code == 302 and h.get("location") == ["/playback?x=1"], h.get("location"))
    check("... the cookie alone opens everything: pages, all cameras, Settings, streams",
          http("GET", "/", cookie=cookie)[0] == 200 and len(http("GET", "/api/cameras", cookie=cookie)[2]) == len(S)
          and http("GET", "/settings", cookie=cookie)[0] == 200 and first_part(f"/stream/{HR}", cookie) == 200)
    code, h, _ = http("GET", "/playback", cookie=cookie)
    check("... a page opened with it renews the cookie (a browser in use never runs out)",
          code == 200 and any(c.startswith(ca.KEY_COOKIE + "=") and f"Max-Age={ca.KEY_SESSION_TTL}" in c
                              for c in h.get("set-cookie", [])), h.get("set-cookie"))
    check("... API calls and pictures do not re-issue it (pages only)",
          not http("GET", "/api/cameras", cookie=cookie)[1].get("set-cookie"))
    day = 86400
    check("... still valid after a working day and a weekend (a wall screen left open keeps working)",
          ca.read_key_session(ca.make_key_session(ADMIN_KEY, now=time.time() - 3 * day), ADMIN_KEY))
    check("... but not after 400 days unused, and not with another key (changing CCTV_TOKEN signs every key browser out)",
          not ca.read_key_session(ca.make_key_session(ADMIN_KEY, now=time.time() - 401 * day), ADMIN_KEY)
          and not ca.read_key_session(ca.make_key_session("another-key"), ADMIN_KEY))
    me = http("GET", "/api/me", cookie=cookie)[2]
    check("... /api/me: administrator via the key link, with sign-out", me.get("kind") == "key" and me.get("admin") and me.get("signOut"), me)
    check("tools calling with ?key= (no browser) are answered directly, no redirect",
          http("GET", f"/api/cameras?key={ADMIN_KEY}")[0] == 200 and http("GET", f"/?key={ADMIN_KEY}")[0] == 200)
    code, h, _ = http("GET", "/logout", cookie=cookie)
    check("sign-out clears the key-link session too",
          any(c.startswith(ca.KEY_COOKIE + "=;") and "Max-Age=0" in c for c in h.get("set-cookie", [])), h.get("set-cookie"))
    name, _, val = cookie.partition("=")
    forged = f"{name}={val[:-4]}AAAA"
    check("a forged key-link cookie opens nothing (401)", http("GET", "/api/me", cookie=forged)[0] == 401)
    code, h, _ = http("GET", "/?key=wrong-key", headers=html)
    check("a wrong key: 401 page, no cookie", code == 401 and not h.get("set-cookie"), code)
    PERMS["keyswap"] = {"allowed": True, "admin": False, "cameras": [cam("nvr2:8", live=True)]}
    _, _, h = sign_in("keyswap")
    check("a person signing in on the same browser replaces a key-link session (no inherited full access)",
          any(c.startswith(ca.KEY_COOKIE + "=;") and "Max-Age=0" in c for c in h.get("set-cookie", [])), h.get("set-cookie"))


def test_sign_in_tokens():
    code, cookie, h = sign_in("admin1")
    check("CMS sign-in: /sso with a valid token -> 302 to / with an HttpOnly SameSite=Lax session cookie",
          code == 302 and h["location"] == ["/"] and cookie
          and any("HttpOnly" in c and "SameSite=Lax" in c for c in h.get("set-cookie", [])), (code, h))
    t = token("admin1")
    http("GET", "/sso?token=" + urllib.parse.quote(t))
    code, _, body = http("GET", "/sso?token=" + urllib.parse.quote(t))
    check("... the same token a second time -> refused (single use)", code == 401 and "already used" in body)
    bad = [("wrong secret", token("x", secret="another-secret-another-secret-another-secret")),
           ("expired", token("x", iat=int(time.time()) - 600, life=90)),
           ("other site", token("x", aud="grav-other")), ("other issuer", token("x", iss="somebody")),
           ("too long-lived", token("x", life=3600)), ("no jti", token("x", jti="")),
           ("alg none", token("x", alg="none")), ("shared login kind", token("x", subj="legacy")),
           ("garbage", "not.a.token")]
    for what, tok in bad:
        code, h, _ = http("GET", "/sso?token=" + urllib.parse.quote(tok))
        check(f"... {what} -> 401, no cookie", code == 401 and not h.get("set-cookie"), code)
    PERMS["admin1"] = {"allowed": True, "admin": True, "cameras": []}
    _, cookie, _ = sign_in("admin1")
    name, _, rest = cookie.partition("=")
    p64, _, s64 = rest.partition(".")
    forged = json.loads(base64.urlsafe_b64decode(p64 + "=" * (-len(p64) % 4)))
    forged["sub"] = "someone-else"
    tampered = f"{name}={b64(forged)}.{s64}"
    check("a tampered session cookie is no session (401)", http("GET", "/api/me", cookie=tampered)[0] == 401)


def test_admin_via_cms():
    PERMS["admin1"] = {"allowed": True, "admin": True, "cameras": []}
    _, cookie, _ = sign_in("admin1")
    code, _, cams = http("GET", "/api/cameras", cookie=cookie)
    check("1 · CEO/admin signed in from the CMS: every camera, live + audio + playback",
          code == 200 and len(cams) == len(S) and all(all(c["permissions"].values()) for c in cams))
    code, _, me = http("GET", "/api/me", cookie=cookie)
    check("... /api/me says administrator, with sign-out", code == 200 and me["admin"] and me["signOut"], me)
    check("... Settings, status, settings API, NVR info open",
          all(http("GET", p, cookie=cookie)[0] == 200 for p in ("/settings", "/api/status", "/api/camera-settings", "/api/playback/nvr-info")))
    w = Ws(f"/audio/{RECEPTION}", cookie)
    check("... sound of any camera opens (101)", " 101 " in (w.status_line or ""))
    w.close()
    code, _, out = search(cookie, [HR, RECEPTION, CEO])
    check("... playback of any camera", code == 200 and out.get("ok"), out)
    http("POST", "/api/playback/close", {"sid": out.get("sid")}, cookie=cookie)


def user_a():
    PERMS["userA"] = {"allowed": True, "admin": False, "cameras": [cam("nvr2:8", live=True, audio=False, playback=True)]}
    return sign_in("userA")[1]


def user_b():
    PERMS["userB"] = {"allowed": True, "admin": False, "cameras": [
        cam("nvr1:9", live=True, audio=True, playback=False), cam("nvr1:14", live=True, audio=False, playback=True)]}
    return sign_in("userB")[1]


def test_user_a_hr_only():
    ck = user_a()
    code, _, cams = http("GET", "/api/cameras", cookie=ck)
    hr = cams[0] if code == 200 and len(cams) == 1 else {}
    check("2 · user A sees ONLY HR Office (the other 24 cameras are never sent)", code == 200 and keys_of(cams) == ["nvr2:8"], cams)
    check("... its permissions: live yes, audio no, playback yes; sound reported 'denied'",
          hr.get("permissions") == {"live": True, "audio": False, "playback": True} and hr.get("audio") == "denied", hr)
    check("... HR Office live video streams (200)", first_part(f"/stream/{HR}", ck) == 200)
    for path in (f"/stream/{RECEPTION}", f"/snapshot/{RECEPTION}", f"/stream/{RECEPTION}?quality=original",
                 f"/stream/{CEO}", f"/stream/{CEO}?prio=full"):
        check(f"6 · direct URL of a camera not assigned: {path} -> 403", first_part(path, ck) == 403)
    check("... /api/stream-info of another camera -> 403", http("GET", f"/api/stream-info/{RECEPTION}", cookie=ck)[0] == 403)
    code, _, info = http("GET", f"/api/stream-info/{HR}", cookie=ck)
    check("... its own stream-info says sound denied", code == 200 and info["audio"]["state"] == "denied", info)
    check("7 · HR Office sound (not granted) -> 403, no socket", " 403 " in (Ws(f"/audio/{HR}", ck).status_line or ""))
    check("7 · another camera's sound -> 403", " 403 " in (Ws(f"/audio/{RECEPTION}", ck).status_line or ""))
    code, _, cfg = http("GET", "/api/playback/config", cookie=ck)
    check("... playback lists only HR Office, sound denied there",
          code == 200 and keys_of([{"key": f"{c['nvr'].lower()}:{c['channel']}"} for c in cfg["cameras"]]) == ["nvr2:8"]
          and cfg["cameras"][0]["audio"] == "denied", cfg.get("cameras"))
    code, _, out = search(ck, [HR])
    check("... HR Office playback search works", code == 200 and out.get("ok"), out)
    tiles = out.get("tiles") or [{}]
    check("... and its tile offers no sound", tiles[0].get("audioAllowed") is False, tiles[0])
    http("POST", "/api/playback/close", {"sid": out.get("sid")}, cookie=ck)
    code, _, out = search(ck, [RECEPTION])
    check("8 · playback search for a camera not assigned -> 403 (nothing searched)",
          code == 403 and out.get("code") == "PLAYBACK_NOT_PERMITTED", out)
    code, _, out = search(ck, [HR, CEO])
    check("8 · ... also when mixed with an allowed one", code == 403, out)
    for path in ("/settings", "/api/status", "/api/camera-settings", "/api/playback/nvr-info", "/api/playback/status"):
        check(f"... administrator-only {path} -> 403", http("GET", path, cookie=ck)[0] == 403)
    code, _, _ = http("PUT", "/api/camera-settings", {"cameras": {}}, cookie=ck)
    check("... changing camera names / order -> 403 (viewing is not configuring)", code == 403, code)


def test_user_b_two_cameras():
    ck = user_b()
    code, _, cams = http("GET", "/api/cameras", cookie=ck)
    by = {c["key"]: c for c in cams} if code == 200 else {}
    check("3 · user B sees Reception and Corridor only", keys_of(cams) == ["nvr1:14", "nvr1:9"], cams)
    check("... Reception: live + sound, no playback",
          by.get("nvr1:9", {}).get("permissions") == {"live": True, "audio": True, "playback": False}
          and by["nvr1:9"]["audio"] != "denied", by.get("nvr1:9"))
    check("... Corridor: live + playback, sound denied",
          by.get("nvr1:14", {}).get("permissions") == {"live": True, "audio": False, "playback": True}
          and by["nvr1:14"]["audio"] == "denied", by.get("nvr1:14"))
    w = Ws(f"/audio/{RECEPTION}", ck)
    check("... Reception sound opens (101)", " 101 " in (w.status_line or ""), w.status_line)
    w.close()
    check("... Corridor sound -> 403", " 403 " in (Ws(f"/audio/{CORRIDOR}", ck).status_line or ""))
    code, _, cfg = http("GET", "/api/playback/config", cookie=ck)
    names = [f"{c['nvr'].lower()}:{c['channel']}" for c in cfg.get("cameras", [])]
    check("9 · playback camera list ('Select all') = only the Corridor", names == ["nvr1:14"], names)
    check("... Reception playback -> 403", search(ck, [RECEPTION])[0] == 403)
    code, _, out = search(ck, [CORRIDOR])
    check("... Corridor playback works", code == 200 and out.get("ok"), out)
    http("POST", "/api/playback/close", {"sid": out.get("sid")}, cookie=ck)


def test_no_cameras_and_no_access():
    PERMS["empty"] = {"allowed": True, "admin": False, "cameras": []}
    _, ck, _ = sign_in("empty")
    code, _, page = http("GET", "/", cookie=ck)
    code2, _, cams = http("GET", "/api/cameras", cookie=ck)
    code3, _, me = http("GET", "/api/me", cookie=ck)
    check("4 · CCTV access but no camera: the page opens, the camera list is EMPTY (never all cameras)",
          code == 200 and "No CCTV cameras have been assigned to your account." in page and code2 == 200 and cams == [], (code, code2, cams))
    check("... /api/me: allowed, 0 live, 0 playback", code3 == 200 and me["allowed"] and me["live"] == 0 and me["playback"] == 0, me)
    code, _, cfg = http("GET", "/api/playback/config", cookie=ck)
    check("... playback lists nothing", code == 200 and cfg["cameras"] == [])
    check("... any camera URL -> 403", first_part(f"/stream/{HR}", ck) == 403)
    code, ck2, _ = sign_in("outsider")                    # the CMS: CCTV not enabled for their department
    code_p, _, body = http("GET", "/", cookie=ck2)
    check("5 · no CCTV access: sign-in lands on a refusal page with the CMS's reason (403)",
          code == 302 and code_p == 403 and "CCTV is not enabled for your department" in body, (code, code_p))
    check("5 · ... camera list, stream, playback all refused (403)",
          http("GET", "/api/cameras", cookie=ck2)[0] == 403 and first_part(f"/stream/{HR}", ck2) == 403
          and search(ck2, [HR])[0] == 403)
    code, ck3, _ = sign_in("down-1")                      # the CMS cannot answer: fail CLOSED
    code_c, _, out = http("GET", "/api/cameras", cookie=ck3)
    check("CMS unreachable (no earlier answer): refused, never opened ('could not be checked')",
          code_c == 403 and "could not be checked" in json.dumps(out), (code_c, out))


def test_playback_session_is_personal():
    cka, ckb = user_a(), user_b()
    code, _, out = search(cka, [HR])
    sid = out.get("sid")
    wb = Ws(f"/api/playback/ws?sid={sid}", ckb)
    check("8 · another person attaching to user A's playback session (knowing its id) -> 403",
          " 403 " in (wb.status_line or ""), wb.status_line)
    code, _, res = http("POST", "/api/playback/close", {"sid": sid}, cookie=ckb)
    check("8 · ... or closing it -> refused", code == 200 and res.get("ok") is False, res)
    wa = Ws(f"/api/playback/ws?sid={sid}", cka)
    check("... user A attaches to their own session (101)", " 101 " in (wa.status_line or ""), wa.status_line)
    wa.send({"op": "audio", "tile": 0})                   # HR sound is not granted: ignored by the server
    time.sleep(0.6)
    check("... asking for HR's sound anyway: the server keeps audio off", wa.state().get("audioTile") == -1, wa.state().get("audioTile"))
    wa.close()
    http("POST", "/api/playback/close", {"sid": sid}, cookie=cka)


def test_revoke_while_watching():
    ck = user_a()
    v = Stream(f"/stream/{HR}", ck)
    check("10 · user A watching HR Office live", v.status == 200 and wait(lambda: v.bytes > 0, 3.0), v.status)
    PERMS["userA"] = {"allowed": True, "admin": False, "cameras": []}
    t0 = time.monotonic()
    code, _, res = push(email="userA@grav.test")
    ended = wait(lambda: v.ended_at is not None, 6.0)
    check(f"10 · CMS removes HR Office and says so: the open live stream ends within seconds "
          f"({(v.ended_at or time.monotonic()) - t0:.1f} s)", code == 200 and res.get("ok") and ended)
    v.close()
    check("10 · ... new requests refused (stream 403, list empty)",
          first_part(f"/stream/{HR}", ck) == 403 and http("GET", "/api/cameras", cookie=ck)[2] == [])
    # without a push: the permission cache expires on its own
    os.environ["CCTV_PERMISSION_TTL_S"] = "1"
    try:
        ckb = user_b()
        v = Stream(f"/stream/{RECEPTION}", ckb)
        wait(lambda: v.bytes > 0, 3.0)
        PERMS["userB"] = {"allowed": True, "admin": False, "cameras": [cam("nvr1:14", live=True, playback=True)]}
        t0 = time.monotonic()
        ended = wait(lambda: v.ended_at is not None, 8.0)
        check(f"10 · ... and WITHOUT the CMS's notice, by the cache expiry ({(v.ended_at or time.monotonic()) - t0:.1f} s)", ended)
        v.close()
    finally:
        os.environ["CCTV_PERMISSION_TTL_S"] = "30"
    # sound taken away while listening
    ckb = user_b()
    w = Ws(f"/audio/{RECEPTION}", ckb)
    wait(lambda: w.msgs, 3.0)
    PERMS["userB"] = {"allowed": True, "admin": False, "cameras": [cam("nvr1:9", live=True, audio=False), cam("nvr1:14", live=True, playback=True)]}
    push(email="userB@grav.test")
    closed = wait(lambda: w.closed, 6.0)
    check("10 · sound taken away while listening: the audio socket is told and closed",
          closed and any(m.get("detail", "").startswith("Sound is not available") for m in w.msgs), w.msgs[-2:])
    # playback taken away while playing
    cka = user_a()
    PERMS["userA"] = {"allowed": True, "admin": False, "cameras": [cam("nvr2:8", live=True, playback=True)]}
    push(email="userA@grav.test")
    code, _, out = search(cka, [HR])
    ws = Ws(f"/api/playback/ws?sid={out.get('sid')}", cka)
    wait(lambda: ws.state().get("tiles"), 5.0)
    PERMS["userA"] = {"allowed": True, "admin": False, "cameras": [cam("nvr2:8", live=True)]}
    push(email="userA@grav.test")
    denied = wait(lambda: any(t.get("state") == "DENIED" for t in ws.state().get("tiles", [])), 6.0)
    s = pb.MANAGER.get(out.get("sid"))
    check("10 · recorded playback taken away while playing: the tile stops ('DENIED'), its worker is gone",
          denied and s is not None and s.tiles[0].denied and s.tiles[0].worker is None, ws.state().get("tiles"))
    PERMS["userA"] = {"allowed": False, "admin": False, "denialCode": "CCTV_NOT_ENABLED", "message": "CCTV is not enabled for your department.", "cameras": []}
    push(everyone=True)
    gone = wait(lambda: pb.MANAGER.get(out.get("sid")) is None, 6.0)
    check("10 · CCTV removed altogether: the playback session is closed with the reason",
          gone and any(m.get("t") == "error" and "not enabled" in m.get("msg", "") for m in ws.msgs), ws.msgs[-1:])
    ws.close()


def test_shared_worker_isolation():
    admin = Stream(f"/stream/{HR}?key={ADMIN_KEY}")
    live = wait(lambda: admin.bytes > 0 and S[HR].viewers >= 1, 4.0)
    before = S[HR].viewers
    PERMS["userC"] = {"allowed": True, "admin": False, "cameras": [cam("nvr1:9", live=True)]}
    _, ck, _ = sign_in("userC")
    code = first_part(f"/stream/{HR}", ck)
    snap = first_part(f"/snapshot/{HR}", ck)
    check("11 · HR Office's worker is live for an authorised viewer; an unauthorised viewer still gets 403 "
          "(stream and snapshot) and is never attached", live and code == 403 and snap == 403 and S[HR].viewers == before,
          (live, code, snap, before, S[HR].viewers))
    ws = Ws(f"/audio/{HR}", ck)
    check("11 · ... nor to its sound", " 403 " in (ws.status_line or ""))
    admin.close()


def test_cms_endpoints_and_sign_out():
    code, _, out = http("GET", "/api/internal/cameras")
    check("CMS-only camera list without the service key -> 401", code == 401)
    code, _, out = http("GET", "/api/internal/cameras", headers={"X-CCTV-Service-Key": SVC})
    check("... with it: every camera by stable key, display name, NVR, channel (no credentials)",
          code == 200 and len(out["cameras"]) == len(S) and {"key", "displayName", "technicalName", "nvr", "channel"} <= set(out["cameras"][0])
          and "rtsp://" not in json.dumps(out)
          and not any(len(x) >= 4 and x in json.dumps(out) for n in server.NVRS.values() for x in (n["user"], n["pass"])), code)
    check("CMS-only access-changed without the key -> 401", http("POST", "/api/internal/access-changed", {"all": True})[0] == 401)
    PERMS["leaver"] = {"allowed": True, "admin": False, "cameras": [cam("nvr2:8", live=True)]}
    _, ck, _ = sign_in("leaver")
    code, h, _ = http("GET", "/logout", cookie=ck)
    check("sign-out clears the session cookie", code == 200 and any("Max-Age=0" in c for c in h.get("set-cookie", [])))
    check("... the browser that drops it is signed out (401)", http("GET", "/api/me")[0] == 401)
    check("no request reached anything but 127.0.0.1 (real NVRs untouched)", not BLOCKED, BLOCKED[:3])


if __name__ == "__main__":
    t_start = time.time()
    srv = server.QuietServer(("127.0.0.1", 0), server.Handler)
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    server.POOL.start()
    order = [n for n in list(globals()) if n.startswith("test_")]
    only = sys.argv[1:]
    for name in order:
        if only and name not in only:
            continue
        print(f"\n-- {name}", flush=True)
        try:
            globals()[name]()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{name} raised {type(e).__name__}: {e}", False)
    pb.MANAGER.close_all("TEST_END")
    srv.shutdown()
    CMS.shutdown()
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}  ({time.time() - t_start:.1f}s)")
    sys.exit(1 if FAILS else 0)
