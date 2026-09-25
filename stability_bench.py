"""Stability recorder -- watch cameras for a long time exactly like a browser and record
EVERY state change, on both sides:

  client : each MJPEG part's X-Frame-State (live / standard / cached / status) and the
           gap between live frames (what the viewer really saw)
  server : /api/status polled every --poll s: status, reconnects, opens, frame age, the
           last error and -- on servers that have it -- the transition log with reasons

    python stability_bench.py --base https://cctv.grav.in --key <CCTV_TOKEN> --page 1 --minutes 10
    python stability_bench.py --base http://127.0.0.1:8000 --key <K> --cameras 24 --full --quality original --minutes 15

It only VIEWS (like a browser): it never changes settings and never opens RTSP itself.
A server that holds the per-NVR cap (the persistent relay) serves these viewers inside
its cap. Prints no credentials. Writes JSON with --out.
"""
import os
import re
import sys
import json
import time
import argparse
import threading
import http.client
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) stability-bench"


class Api:
    def __init__(self, base, key):
        self.base, self.key = base.rstrip("/"), key
        u = urllib.parse.urlsplit(self.base)
        self.scheme, self.netloc = u.scheme, u.netloc

    def q(self, extra=()):
        parts = ([f"key={urllib.parse.quote(self.key)}"] if self.key else []) + list(extra)
        return ("?" + "&".join(parts)) if parts else ""

    def get(self, path, timeout=15):
        req = urllib.request.Request(self.base + path + self.q(), headers={"User-Agent": UA})
        return json.load(urllib.request.urlopen(req, timeout=timeout))

    def conn(self, timeout=30):
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        return cls(self.netloc, timeout=timeout)


class Viewer(threading.Thread):
    """One <img> MJPEG stream. Records (t, state, bytes) per part; reconnects like the
    page does (2 s after an error) and counts those client reconnects."""

    def __init__(self, api, index, t0, full=False, quality=None, fps=None):
        super().__init__(daemon=True)
        self.api, self.index, self.t0 = api, index, t0
        extra = (["prio=full"] if full else []) + (["quality=original"] if quality == "original" else []) + \
                ([f"fps={fps}"] if fps else [])
        self.path = f"/stream/{index}" + api.q(extra)
        self.parts = []                  # (t since start, state, bytes)
        self.client_reconnects = 0
        self.errors = []
        self.stop = False
        self._c = None

    def run(self):
        while not self.stop:
            try:
                self._c = self.api.conn()
                self._c.request("GET", self.path, headers={"User-Agent": UA})
                r = self._c.getresponse()
                buf = b""
                while not self.stop:
                    chunk = r.read1(262144)
                    if not chunk:
                        raise ConnectionError("server closed the stream")
                    buf += chunk
                    while True:
                        i = buf.find(b"--frame\r\n")
                        if i < 0:
                            break
                        j = buf.find(b"\r\n\r\n", i)
                        if j < 0:
                            break
                        hdr = buf[i + 9:j].decode("latin-1")
                        m = re.search(r"Content-Length:\s*(\d+)", hdr, re.I)
                        n = int(m.group(1)) if m else 0
                        if len(buf) < j + 4 + n:
                            break
                        s = re.search(r"X-Frame-State:\s*(\w+)", hdr, re.I)
                        self.parts.append((round(time.monotonic() - self.t0, 3), s.group(1) if s else None, n))
                        buf = buf[j + 4 + n:]
            except Exception as e:
                if self.stop:
                    break
                self.errors.append((round(time.monotonic() - self.t0, 1), type(e).__name__, str(e)[:80]))
                self.client_reconnects += 1
                time.sleep(2.0)                      # the page's onerror retry delay
            finally:
                try:
                    self._c.close()
                except Exception:
                    pass

    def close(self):
        self.stop = True
        try:
            self._c.sock.shutdown(2)
        except Exception:
            pass

    def summary(self, settle_s=0.0):
        parts = [p for p in self.parts if p[0] >= settle_s]
        live = [p for p in parts if p[1] == "live"]
        gaps = [round(b[0] - a[0], 3) for a, b in zip(live, live[1:])]
        # client-visible state changes (live -> cached -> ...)
        changes, prev = [], None
        for t, st, _ in parts:
            if st != prev:
                changes.append((t, st))
                prev = st
        span = (parts[-1][0] - parts[0][0]) if len(parts) > 1 else 0
        return {"index": self.index, "parts": len(parts), "liveParts": len(live),
                "states": sorted({str(p[1]) for p in parts}),
                "nonLiveParts": len(parts) - len(live),
                "stateChanges": len(changes) - 1 if changes else 0,
                "firstChanges": changes[:12],
                "liveFps": round(len(live) / span, 2) if span else None,
                "maxLiveGapS": max(gaps) if gaps else None,
                "gapsOver1s": sum(g > 1 for g in gaps), "gapsOver2_5s": sum(g > 2.5 for g in gaps),
                "gapsOver5s": sum(g > 5 for g in gaps),
                "avgKB": round(sum(p[2] for p in live) / len(live) / 1024, 1) if live else None,
                "clientReconnects": self.client_reconnects, "clientErrors": self.errors[:5]}


class ServerWatch(threading.Thread):
    """Polls /api/status; records every change of the watched cameras' state."""

    FIELDS = ("status", "tier", "viewers", "reconnects", "opens", "failStreak", "lastError", "lastErrorMasked")

    def __init__(self, api, indices, t0, poll, quality):
        super().__init__(daemon=True)
        self.api, self.indices, self.t0, self.poll, self.quality = api, set(indices), t0, poll, quality
        self.events, self.samples, self.stop = [], 0, False
        self.max_age = {}
        self.nvr_peak = {}
        self.poll_errors = 0
        self.transitions = {}          # index -> transition log from the server (new servers)
        self.first = {}
        self.last = {}

    def _pick(self, c):
        o = c.get("original") if self.quality == "original" else None
        src = o if o else c
        d = {k: src.get(k) for k in self.FIELDS if k in src}
        d["age"] = src.get("lastFrameAgeMs")
        return d

    def run(self):
        prev = {}
        while not self.stop:
            try:
                d = self.api.get("/api/status")
                self.samples += 1
                t = round(time.monotonic() - self.t0, 1)
                for k, v in d["nvrs"].items():
                    self.nvr_peak[k] = max(self.nvr_peak.get(k, 0), v["active"])
                for c in d["cameras"]:
                    i = c["index"]
                    if i not in self.indices:
                        continue
                    cur = self._pick(c)
                    age = cur.pop("age")
                    if age is not None:
                        self.max_age[i] = max(self.max_age.get(i, 0), age)
                    self.first.setdefault(i, dict(cur))
                    self.last[i] = dict(cur)
                    if prev.get(i) is not None and cur != prev[i]:
                        diff = {k: (prev[i].get(k), cur.get(k)) for k in cur if prev[i].get(k) != cur.get(k)}
                        self.events.append({"t": t, "index": i, "changed": diff})
                    prev[i] = cur
                    src = c.get("original") if self.quality == "original" else c
                    if src and src.get("transitions"):
                        self.transitions[i] = src["transitions"]
            except Exception:
                self.poll_errors += 1
            time.sleep(self.poll)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--key", default=os.getenv("RELAY_KEY", ""))
    ap.add_argument("--page", type=int, default=None, help="grid page (6 cameras, the server's grid order)")
    ap.add_argument("--cameras", default="", help="camera indices, e.g. 24 or 0,1,2")
    ap.add_argument("--full", action="store_true", help="fullscreen view (one camera, prio=full)")
    ap.add_argument("--quality", default="standard", choices=["standard", "original"])
    ap.add_argument("--fps", type=int, default=None)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--settle", type=float, default=20.0, help="ignore the first N s (start-up) in gap stats")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    api = Api(a.base, a.key)
    if a.cameras:
        idx = [int(x) for x in a.cameras.split(",") if x.strip()]
    else:
        cams = sorted(api.get("/api/cameras"), key=lambda c: (c.get("displayOrder") or 0, c["index"]))
        p = a.page or 1
        idx = [c["index"] for c in cams[(p - 1) * 6: p * 6]]
    if a.full:
        idx = idx[:1]
    fps = a.fps or (6 if a.quality == "original" and not a.full else None)
    t0 = time.monotonic()
    print(f"stability bench -> {urllib.parse.urlsplit(a.base).netloc} | cameras {idx} | "
          f"{'fullscreen' if a.full else 'grid'} {a.quality} | {a.minutes} min", flush=True)
    sw = ServerWatch(api, idx, t0, a.poll, a.quality)
    sw.start()
    vs = [Viewer(api, i, t0, full=a.full, quality=a.quality, fps=fps) for i in idx]
    for v in vs:
        v.start()
    end = t0 + a.minutes * 60
    try:
        while time.monotonic() < end:
            time.sleep(min(30.0, max(0.1, end - time.monotonic())))
            el = round((time.monotonic() - t0) / 60, 1)
            print(f"  {el} min: " + ", ".join(f"#{v.index}:{sum(1 for p in v.parts if p[1] == 'live')}live/"
                                               f"{sum(1 for p in v.parts if p[1] != 'live')}other" for v in vs)
                  + f" | server events {len(sw.events)}", flush=True)
    finally:
        for v in vs:
            v.close()
        sw.stop = True
    res = {"base": urllib.parse.urlsplit(a.base).netloc, "cameras": idx, "view": "fullscreen" if a.full else "grid",
           "quality": a.quality, "minutes": a.minutes, "settleS": a.settle,
           "client": [v.summary(a.settle) for v in vs],
           "server": {"samples": sw.samples, "pollErrors": sw.poll_errors, "nvrPeakActive": sw.nvr_peak,
                      "maxFrameAgeMs": sw.max_age, "first": sw.first, "last": sw.last,
                      "events": sw.events, "transitions": sw.transitions}}
    print(json.dumps(res, indent=1))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
