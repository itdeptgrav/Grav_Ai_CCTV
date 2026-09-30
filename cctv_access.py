"""Who is watching, and what may they see -- person-wise CCTV permissions.

Two ways in:

  * THE ADMINISTRATORS' KEY LINK  /?key=<CCTV_TOKEN>  -- full access, no password: every
    camera, sound, recorded playback and the camera Settings. Opened in a browser, the key
    is exchanged at once for a signed session cookie and the address bar is cleaned (the
    key is not left on screen or in later URLs). The CMS never hands the key to anybody.
  * SIGN-IN FROM THE GRAV CMS  /sso?token=<90-second HS256 token>  -- the token names the
    person (identity id, kind, token version, email). This app sets a signed session
    cookie and from then on asks the CMS what that person may do
    (GET <CMS>/api/cctv/internal/access): which cameras, and on each live video, sound
    (live and recorded) and recorded playback. The answer is cached for a few seconds;
    the CMS also tells this app at once when somebody's access changes
    (POST /api/internal/access-changed), and every open picture / sound / playback is
    re-checked every couple of seconds -- so a revoked camera goes dark without a reload.

Precedence is decided by the CMS (grav-cms-backend services/cctv/cctvAccess.service.js):
platform administrator -> everything; shared department logins -> nothing; the person's
department must have "CCTV camera access" on; then exactly the cameras granted, never
all of them. Nothing here trusts the browser: every camera route checks THIS principal
for THAT camera and THAT feature (server.py). Standard library only.
"""
import os
import hmac
import json
import time
import base64
import hashlib
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request

# SHA-256 of values that were published and must never be accepted again: the CCTV SSO
# secret that sat in this repo's .env.example (commit 52d9266) -- whoever holds it could
# sign in as anybody. (The administrators' key link is the owner's choice: kept.)
PUBLISHED_SHA256 = {
    "1bcf4ad1a01f516b148911442399bdd040cfcc8f0de14800afb9e7a201b4d9fa",
}

COOKIE = "cctv_session"
KEY_COOKIE = "cctv_key"       # the key link's session (so the key leaves the address bar)
AUDIENCE, ISSUER = "grav-cctv", "grav-cms"
SKEW_S = 30                   # clock difference tolerated between the two servers
MAX_TOKEN_LIFE_S = 300        # the CMS mints 90 s tokens; anything longer is refused
FEATURES = ("live", "audio", "playback")


def _env(name, default=""):
    return (os.getenv(name, default) or "").strip()


def sha256(v):
    return hashlib.sha256(str(v).encode()).hexdigest()


def published(v):
    return bool(v) and sha256(v) in PUBLISHED_SHA256


class Config:
    """Read from the environment on every use, so tests can switch it."""

    @property
    def sso_secret(self):
        return _env("CCTV_SSO_SECRET")

    @property
    def service_key(self):
        return _env("CCTV_SERVICE_KEY")

    @property
    def cms_api(self):
        return _env("CCTV_CMS_API_URL").rstrip("/")

    @property
    def cms_app(self):
        return _env("CCTV_CMS_APP_URL").rstrip("/")

    @property
    def session_ttl(self):
        try:
            return max(300, int(_env("CCTV_SESSION_TTL", "28800") or 28800))
        except ValueError:
            return 28800

    @property
    def perm_ttl(self):
        try:
            return max(1.0, float(_env("CCTV_PERMISSION_TTL_S", "15") or 15))
        except ValueError:
            return 15.0

    @property
    def perm_grace(self):
        try:
            return max(0.0, float(_env("CCTV_PERMISSION_GRACE_S", "60") or 60))
        except ValueError:
            return 60.0

    def problems(self):
        """What stops CMS sign-in from working here ([] = ready)."""
        out = []
        for name, v in (("CCTV_SSO_SECRET", self.sso_secret), ("CCTV_SERVICE_KEY", self.service_key)):
            if len(v) < 32:
                out.append(f"{name} missing or shorter than 32 characters")
            elif published(v):
                out.append(f"{name} is a value that was published in git -- generate a new one (CMS + here)")
        if not self.cms_api.startswith(("http://", "https://")):
            out.append("CCTV_CMS_API_URL (the CMS backend, for permission checks) is not set")
        return out

    def sso_enabled(self):
        return not self.problems()


CONFIG = Config()


# ── principals ──────────────────────────────────────────────────────────────
class Principal:
    """What one viewer may do. kind: 'key' (administrators' key link), 'open' (no gate
    configured at all -- development only), 'user' (signed in from the CMS)."""

    def __init__(self, kind, admin=False, allowed=False, email="", name="", cameras=None, denial=None,
                 message="", session=None):
        self.kind, self.admin, self.allowed = kind, admin, allowed
        self.email, self.name = email, name
        self.cameras = cameras or {}          # key -> {"live", "audio", "playback"}
        self.denial, self.message = denial, message
        self.session = session                # the cookie's claims (user principals)

    @property
    def ident(self):
        """Stable identity, e.g. to bind a playback session to the person who started it."""
        if self.kind != "user":
            return self.kind
        s = self.session or {}
        return f"user:{s.get('subj')}:{s.get('sub')}"

    def can(self, camera_key, feature):
        if self.admin:
            return True
        if not self.allowed:
            return False
        c = self.cameras.get(str(camera_key).lower())
        if not c:
            return False
        if feature == "audio":                # sound needs a picture to go with it
            return bool(c.get("audio")) and bool(c.get("live") or c.get("playback"))
        return bool(c.get(feature))

    def any(self, feature):
        return self.admin or (self.allowed and any(self.can(k, feature) for k in self.cameras))

    def recheck(self):
        """The same viewer, re-decided (cache; refetched from the CMS when stale or when
        the CMS said their access changed). Key / open principals never change."""
        if self.kind != "user":
            return self
        return principal_for(self.session)

    def view(self):
        return {"kind": self.kind, "admin": self.admin, "allowed": self.allowed, "name": self.name,
                "email": self.email, "denial": self.denial, "message": self.message,
                "live": sum(1 for k in self.cameras if self.can(k, "live")),
                "playback": sum(1 for k in self.cameras if self.can(k, "playback")),
                "audio": sum(1 for k in self.cameras if self.can(k, "audio"))}


KEY_PRINCIPAL = Principal("key", admin=True, allowed=True, name="Administrator (key link)")
OPEN_PRINCIPAL = Principal("open", admin=True, allowed=True, name="Open access (no gate configured)")


# ── base64url / HS256 ──────────────────────────────────────────────────────
def _b64d(s):
    s = str(s)
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _hs256(key, msg):
    return hmac.new(key.encode() if isinstance(key, str) else key, msg.encode(), hashlib.sha256).digest()


class SsoError(Exception):
    pass


_JTI = {}                     # jti -> exp (each sign-in token works once)
_JTI_LOCK = threading.Lock()


def verify_sso(token, now=None):
    """-> the claims of a valid, unused CMS sign-in token; raises SsoError otherwise."""
    if not CONFIG.sso_enabled():
        raise SsoError("CMS sign-in is not set up on this CCTV server: " + "; ".join(CONFIG.problems()))
    now = time.time() if now is None else now
    try:
        h64, p64, s64 = str(token or "").split(".")
        header, claims = json.loads(_b64d(h64)), json.loads(_b64d(p64))
        sig = _b64d(s64)
    except (ValueError, TypeError):
        raise SsoError("malformed sign-in token")
    if header.get("alg") != "HS256":
        raise SsoError("unexpected token algorithm")
    if not hmac.compare_digest(sig, _hs256(CONFIG.sso_secret, f"{h64}.{p64}")):
        raise SsoError("bad token signature")
    aud = claims.get("aud")
    if (aud if isinstance(aud, list) else [aud]).count(AUDIENCE) == 0 or claims.get("iss") != ISSUER:
        raise SsoError("token is not for this site")
    try:
        iat, exp = float(claims["iat"]), float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        raise SsoError("token has no lifetime")
    if exp < now - SKEW_S:
        raise SsoError("sign-in link expired -- open CCTV from the CMS again")
    if iat > now + SKEW_S or exp - iat > MAX_TOKEN_LIFE_S:
        raise SsoError("token lifetime not accepted")
    if not claims.get("sub") or claims.get("subj") not in ("employee", "dept_user") or not claims.get("jti"):
        raise SsoError("token does not name a person")
    with _JTI_LOCK:
        for j, e in list(_JTI.items()):
            if e < now - SKEW_S:
                _JTI.pop(j, None)
        if claims["jti"] in _JTI:
            raise SsoError("sign-in link already used -- open CCTV from the CMS again")
        _JTI[claims["jti"]] = exp
    return claims


# ── the session cookie (stateless, signed) ─────────────────────────────────
def _session_key():
    # derived from the SSO secret: rotating that secret signs everybody out here too
    return hmac.new(CONFIG.sso_secret.encode(), b"cctv-session-v1", hashlib.sha256).digest()


def make_session(claims, now=None):
    now = int(time.time() if now is None else now)
    body = {"v": 1, "sid": secrets.token_urlsafe(9), "sub": str(claims.get("sub")), "subj": claims.get("subj"),
            "tv": int(claims.get("tv") or 0), "em": str(claims.get("email") or "").lower(),
            "nm": str(claims.get("name") or "")[:80], "iat": now, "exp": now + CONFIG.session_ttl}
    p64 = _b64e(json.dumps(body, separators=(",", ":")).encode())
    return f"{p64}.{_b64e(_hs256(_session_key(), p64))}"


def read_session(value, now=None):
    """-> the session's claims, or None (missing, tampered, expired, or SSO not set up)."""
    if not value or not CONFIG.sso_enabled():
        return None
    try:
        p64, s64 = str(value).split(".")
        if not hmac.compare_digest(_b64d(s64), _hs256(_session_key(), p64)):
            return None
        s = json.loads(_b64d(p64))
    except (ValueError, TypeError):
        return None
    if s.get("v") != 1 or float(s.get("exp", 0)) < (time.time() if now is None else now):
        return None
    return s


def cookie_header(value, secure, max_age=None, name=COOKIE):
    parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax",
             f"Max-Age={CONFIG.session_ttl if max_age is None else max_age}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


# ── the key link's session: signed with the key itself ─────────────────────
# Opening /?key=<CCTV_TOKEN> in a browser sets this cookie and redirects to the same page
# WITHOUT the key, so the key is not left in the address bar. Signed with a value derived
# from CCTV_TOKEN: changing the key signs every key-link browser out. It lasts as long as
# the key link itself did (a wall screen left open for days keeps working): 400 days, the
# most a browser keeps a cookie, renewed each time a page is opened with it. "Sign out"
# on the page removes it (for a computer that is not yours).
KEY_SESSION_TTL = 400 * 86400


def _key_session_key(token):
    return hmac.new(str(token).encode(), b"cctv-key-session-v1", hashlib.sha256).digest()


def make_key_session(token, now=None):
    now = int(time.time() if now is None else now)
    p64 = _b64e(json.dumps({"v": 1, "k": 1, "iat": now, "exp": now + KEY_SESSION_TTL},
                           separators=(",", ":")).encode())
    return f"{p64}.{_b64e(_hs256(_key_session_key(token), p64))}"


def read_key_session(value, token, now=None):
    """-> True when `value` is a valid, unexpired key-link session for this key."""
    if not value or not token:
        return False
    try:
        p64, s64 = str(value).split(".")
        if not hmac.compare_digest(_b64d(s64), _hs256(_key_session_key(token), p64)):
            return False
        s = json.loads(_b64d(p64))
    except (ValueError, TypeError):
        return False
    return s.get("v") == 1 and s.get("k") == 1 and float(s.get("exp", 0)) >= (time.time() if now is None else now)


# ── permissions, asked of the CMS and cached for a few seconds ──────────────
class _Permissions:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}                   # ident -> (fetched monotonic, generation, decision)
        self.inflight = {}                # ident -> threading.Event
        self.gen_all = 0
        self.gen_email = {}               # email -> generation
        self.fetches = 0                  # (tests / status)

    def _gen(self, email):
        return (self.gen_all, self.gen_email.get(email, 0))

    def invalidate(self, email=None, everyone=False):
        with self.lock:
            if everyone:
                self.gen_all += 1
                n = len(self.cache)
            else:
                e = str(email or "").lower()
                self.gen_email[e] = self.gen_email.get(e, 0) + 1
                n = sum(1 for k in self.cache if k.endswith("|" + e))
        return n

    def decision(self, sess):
        ident = f"{sess.get('subj')}|{sess.get('sub')}|{sess.get('tv')}|{sess.get('em')}"
        email = sess.get("em") or ""
        while True:
            with self.lock:
                hit = self.cache.get(ident)
                if hit and time.monotonic() - hit[0] < CONFIG.perm_ttl and hit[1] == self._gen(email):
                    return hit[2]
                ev = self.inflight.get(ident)
                if ev is None:
                    ev = self.inflight[ident] = threading.Event()
                    mine = True
                else:
                    mine = False
            if not mine:                      # somebody is already asking: wait for their answer
                ev.wait(6.0)
                continue
            try:
                gen = self._gen(email)
                fresh = self._fetch(sess)
                with self.lock:
                    if fresh is not None:
                        self.cache[ident] = (time.monotonic(), gen, fresh)
                        return fresh
                    # the CMS did not answer: the last answer stands for a short grace
                    if hit and time.monotonic() - hit[0] < CONFIG.perm_ttl + CONFIG.perm_grace and hit[1] == gen:
                        return hit[2]
                    return {"allowed": False, "admin": False, "denialCode": "ACCESS_CHECK_UNAVAILABLE",
                            "message": "Your CCTV access could not be checked just now. Try again in a moment.",
                            "cameras": [], "person": {"email": email, "name": sess.get("nm", "")}}
            finally:
                with self.lock:
                    self.inflight.pop(ident, None)
                ev.set()

    def _fetch(self, sess):
        """-> the CMS's decision dict, or None when it could not be had."""
        self.fetches += 1
        q = urllib.parse.urlencode({"id": sess.get("sub", ""), "subject": sess.get("subj", ""),
                                    "tv": sess.get("tv", 0), "email": sess.get("em", "")})
        req = urllib.request.Request(f"{CONFIG.cms_api}/api/cctv/internal/access?{q}",
                                     headers={"X-CCTV-Service-Key": CONFIG.service_key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=4.0) as r:
                d = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:                 # a wrong service key is not an outage: refuse, loudly
                return {"allowed": False, "admin": False, "denialCode": "CCTV_LINK_MISCONFIGURED",
                        "message": "CCTV is not linked to the CMS correctly (service key). Tell an administrator.",
                        "cameras": [], "person": {"email": sess.get("em", ""), "name": sess.get("nm", "")}}
            return None
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or "allowed" not in d:
            return None
        return d


PERMISSIONS = _Permissions()


def principal_for(sess):
    """The signed-in viewer behind a session cookie, decided now (cached briefly)."""
    d = PERMISSIONS.decision(sess)
    cams = {}
    for c in d.get("cameras") or []:
        k = str(c.get("key", "")).lower()
        if k:
            cams[k] = {f: bool(c.get(f)) for f in FEATURES}
    person = d.get("person") or {}
    return Principal("user", admin=bool(d.get("admin")) and bool(d.get("allowed")), allowed=bool(d.get("allowed")),
                     email=person.get("email") or sess.get("em", ""), name=person.get("name") or sess.get("nm", ""),
                     cameras=cams, denial=d.get("denialCode"), message=d.get("message") or "", session=sess)


def service_ok(header_value):
    """Is this the CMS calling (the shared service key)?"""
    key = CONFIG.service_key
    if len(key) < 32 or published(key) or not header_value:
        return False
    return hmac.compare_digest(sha256(header_value).encode(), sha256(key).encode())
