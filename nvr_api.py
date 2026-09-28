"""Read-only client of the NVRs' vendor web API (CP PLUS / Dahua-compatible CGI).

Used for recorded playback on the office LAN only: the router forwards just the
RTSP ports, so from outside the office this API is simply "not available" and the
playback code falls back to RTSP checks. Nothing here changes an NVR setting.

  * find_files(channel, a, b)  recording files -> [(start, end)] in NVR local time
                               (mediaFileFind: create / findFile / findNextFile /
                               close / destroy -- the "object" is a search handle)
  * current_time()             the NVR's clock (for the clock-drift diagnostic)
  * account_group()            group of the configured account ("admin" -> warning)
  * device()                   model / firmware
Measured (NVR_PLAYBACK_CAPABILITY_REPORT.txt): NVR1 serves plain HTTP, NVR2 only
HTTPS with a self-signed certificate (accepted here: LAN address, read-only). Search
channels are 1-based, results 0-based. Credentials never leave this module.
"""
import re
import ssl
import time
import datetime
import threading
import urllib.error
import urllib.parse
import urllib.request

import playback_time as pt

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE           # the NVR's own self-signed certificate (LAN)


class VendorApi:
    def __init__(self, host, user, pw, timeout=6.0):
        self.host, self.user, self.pw, self.timeout = host, user, pw, timeout
        self.scheme = None                   # "https" | "http" once detected
        self.lock = threading.Lock()         # one search at a time per NVR
        self._op = None

    def _opener(self):
        if self._op is None:
            pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            for sch in ("http", "https"):
                pm.add_password(None, f"{sch}://{self.host}/", self.user, self.pw)
            self._op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX),
                                                   urllib.request.HTTPDigestAuthHandler(pm))
        return self._op

    def _get(self, path, scheme=None, timeout=None):
        url = f"{scheme or self.scheme}://{self.host}{path}"
        try:
            r = self._opener().open(url, timeout=timeout or self.timeout)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except (OSError, ValueError):
            return None, ""

    def available(self, timeout=2.5):
        """Detect the web API (HTTPS first, then HTTP). -> True / False."""
        if self.scheme:
            return True
        for sch in ("https", "http"):
            code, body = self._get("/cgi-bin/magicBox.cgi?action=getVendor", sch, timeout)
            if code == 200 and "vendor=" in body:
                self.scheme = sch
                return True
        return False

    def current_time(self):
        code, body = self._get("/cgi-bin/global.cgi?action=getCurrentTime")
        m = re.search(r"result=(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", body or "")
        return pt.parse_api(m.group(1)) if code == 200 and m else None

    def device(self):
        out = {}
        for key, action in (("model", "getDeviceType"), ("firmware", "getSoftwareVersion"),
                            ("vendor", "getVendor")):
            code, body = self._get(f"/cgi-bin/magicBox.cgi?action={action}")
            m = re.search(r"=(.+)", body or "")
            out[key] = m.group(1).strip() if code == 200 and m else None
        return out

    def account_group(self):
        code, body = self._get(f"/cgi-bin/userManager.cgi?action=getUserInfo&name={urllib.parse.quote(self.user)}")
        m = re.search(r"(?m)^user\.Group=(\S+)", body or "")
        return m.group(1) if code == 200 and m else None

    def oldest_recording(self, channel, a, b):
        """Start of the OLDEST recording file of `channel` (1-based) overlapping [a, b],
        or None if nothing is recorded there. Both NVRs list files oldest-first (checked
        against full listings of 690 / 473 files), so the first page is enough -- ~1 s
        instead of paging through a month of files. Raises OSError if the NVR does not
        answer (never mistaken for "nothing recorded")."""
        with self.lock:
            code, body = self._get("/cgi-bin/mediaFileFind.cgi?action=factory.create")
            m = re.search(r"result=(\d+)", body or "")
            if code != 200 or not m:
                raise OSError(f"search API unavailable (HTTP {code})")
            obj = m.group(1)
            try:
                q = urllib.parse.quote
                code, body = self._get(f"/cgi-bin/mediaFileFind.cgi?action=findFile&object={obj}"
                                       f"&condition.Channel={int(channel)}&condition.StartTime={q(pt.api_time(a))}"
                                       f"&condition.EndTime={q(pt.api_time(b))}")
                if code is None:
                    raise OSError("search API did not answer")
                if code != 200:
                    return None                           # nothing recorded -> 400 on some firmware
                code, body = self._get(f"/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={obj}&count=5")
                if code is None:
                    raise OSError("search API did not answer")
                starts = []
                for s in re.findall(r"items\[\d+\]\.StartTime=(.*)", body or ""):
                    try:
                        starts.append(pt.parse_api(s))
                    except ValueError:
                        continue
                return min(starts) if code == 200 and starts else None
            finally:
                self._get(f"/cgi-bin/mediaFileFind.cgi?action=close&object={obj}")
                self._get(f"/cgi-bin/mediaFileFind.cgi?action=destroy&object={obj}")

    def find_files(self, channel, a, b, limit=2000):
        """Recording files of `channel` (1-based) overlapping [a, b] (NVR local).
        -> sorted [(start, end)] or raises OSError when the API does not answer."""
        with self.lock:
            code, body = self._get("/cgi-bin/mediaFileFind.cgi?action=factory.create")
            m = re.search(r"result=(\d+)", body or "")
            if code != 200 or not m:
                raise OSError(f"search API unavailable (HTTP {code})")
            obj = m.group(1)
            files = []
            try:
                q = urllib.parse.quote
                code, body = self._get(f"/cgi-bin/mediaFileFind.cgi?action=findFile&object={obj}"
                                       f"&condition.Channel={int(channel)}&condition.StartTime={q(pt.api_time(a))}"
                                       f"&condition.EndTime={q(pt.api_time(b))}")
                if code != 200:
                    return []                                 # nothing recorded -> 400 on some firmware
                while len(files) < limit:
                    code, body = self._get(f"/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={obj}&count=100")
                    items = {}
                    for idx, key, val in re.findall(r"items\[(\d+)\]\.(\w+)(?:\[\d+\])?=(.*)", body or ""):
                        items.setdefault(int(idx), {})[key] = val.strip()
                    for i in sorted(items):
                        it = items[i]
                        try:
                            files.append((pt.parse_api(it["StartTime"]), pt.parse_api(it["EndTime"])))
                        except (KeyError, ValueError):
                            continue
                    if code != 200 or len(items) < 100:
                        break
            finally:
                self._get(f"/cgi-bin/mediaFileFind.cgi?action=close&object={obj}")
                self._get(f"/cgi-bin/mediaFileFind.cgi?action=destroy&object={obj}")
            return sorted(files)


def merge_segments(files, a, b, join_s=3.0):
    """Recording files -> contiguous segments clipped to [a, b]; files closer than
    `join_s` seconds are one segment (the NVR starts a new file every hour)."""
    segs = []
    for s, e in sorted(files):
        s, e = max(s, a), min(e, b)
        if e <= s:
            continue
        if segs and (s - segs[-1][1]).total_seconds() <= join_s:
            segs[-1][1] = max(segs[-1][1], e)
        else:
            segs.append([s, e])
    return [(s, e) for s, e in segs]


def gaps_of(segs, a, b, min_s=3.0):
    """The parts of [a, b] without recording (>= min_s seconds)."""
    out, cur = [], a
    for s, e in segs:
        if (s - cur).total_seconds() >= min_s:
            out.append((cur, s))
        cur = max(cur, e)
    if (b - cur).total_seconds() >= min_s:
        out.append((cur, b))
    return out


def locate(segs, t):
    """-> ("in", seg) if t is recorded, ("gap", prev_seg or None, next_seg or None) else."""
    prev = nxt = None
    for s, e in segs:
        if s <= t < e:
            return ("in", (s, e))
        if e <= t:
            prev = (s, e)
        elif s > t and nxt is None:
            nxt = (s, e)
    return ("gap", prev, nxt)


_API_CACHE = {}
_API_LOCK = threading.Lock()


def api_for(nvr_key, host, user, pw):
    """One VendorApi per NVR (host changes -> new client)."""
    with _API_LOCK:
        api = _API_CACHE.get(nvr_key)
        if api is None or api.host != host:
            api = _API_CACHE[nvr_key] = VendorApi(host, user, pw)
        return api


def clock_drift_s(api):
    """NVR clock minus the real time (seconds, NVR local vs server UTC -> NVR zone);
    None if not measurable. Negative = the NVR is slow."""
    t0 = time.time()
    t = api.current_time()
    if t is None:
        return None
    ref = pt.nvr_now() - datetime.timedelta(seconds=(time.time() - t0) / 2)
    return round((t - ref).total_seconds())
