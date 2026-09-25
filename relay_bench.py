"""Relay benchmark -- what a VIEWER experiences, measured against a RUNNING server.

    python relay_bench.py --port 8010 --key <CCTV_TOKEN> [--pid <server pid>] [--out results.json]

Scenarios (identical for the on-demand and the persistent server):
  page1       open grid page 1: 6 MJPEG streams at once, exactly like the browser
  page1to4    page 1 -> page 4 (close all, 500 ms, open the next page) -> back to page 1
  fullscreen  on page 1: stop the other 5 tiles, open camera 1 with ?prio=full,
              then return to the grid (re-open the 5 tiles)
  page1to2    page 1 -> page 2 (both on NVR2: needs promotions with a 6-stream cap)
  share       the same camera in 1, 3 and 5 "browsers": upstream connections made
  resources   server CPU/RAM + system network receive rate, idle and with page 1 open

Per stream: time to the FIRST image (and what it was: live / cached / status card)
and time to the first LIVE frame. The persistent server marks each MJPEG part with
X-Frame-State; for an older server the first-live time comes from /api/status
(firstHttpFrameMs). Never opens more than one page (6) + 1 stream. With --guard
(URLs of other servers on the same NVRs, e.g. production, given in the environment
variable RELAY_GUARDS, comma-separated), it aborts as soon as one of them has a
viewer or a busy NVR slot, so the test never adds to real use of the NVRs.
Prints no credentials (keys are only sent, never printed).
"""
import os
import re
import sys
import json
import time
import socket
import argparse
import threading
import subprocess
import http.client
import urllib.parse
import urllib.request

KNOWN_DEAD = {("nvr2", 7), ("nvr2", 12), ("nvr2", 13), ("nvr1", 10)}   # proven by diag_cctv.py
PER = 6
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) relay-bench"


class Api:
    def __init__(self, host, port, key):
        self.host, self.port, self.key = host, port, key

    def q(self, extra=""):
        parts = ([f"key={urllib.parse.quote(self.key)}"] if self.key else []) + ([extra] if extra else [])
        return ("?" + "&".join(parts)) if parts else ""

    def get(self, path):
        c = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            c.request("GET", path + self.q())
            r = c.getresponse()
            body = r.read()
            if r.status != 200:
                raise RuntimeError(f"{path} -> HTTP {r.status}")
            return json.loads(body)
        finally:
            c.close()

    def status(self):
        return self.get("/api/status")

    def grid(self):
        cams = self.get("/api/cameras")
        return sorted(cams, key=lambda c: (c.get("displayOrder") or 0, c["index"]))


class Viewer(threading.Thread):
    """One MJPEG <img>: records every part's arrival time and X-Frame-State."""

    def __init__(self, api, index, prio=None):
        super().__init__(daemon=True)
        self.api, self.index = api, index
        self.path = f"/stream/{index}" + api.q("prio=full" if prio == "full" else "")
        self.parts = []                     # (ms since request, state or None)
        self.sock = None
        self.stop = False
        self.error = None
        self.t0 = None

    def run(self):
        try:
            s = socket.create_connection((self.api.host, self.api.port), timeout=30)
            self.sock = s
            self.t0 = time.monotonic()
            s.sendall(f"GET {self.path} HTTP/1.1\r\nHost: bench\r\n\r\n".encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    return
                buf += chunk
            buf = buf.split(b"\r\n\r\n", 1)[1]
            while not self.stop:
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
                    st = re.search(r"X-Frame-State:\s*(\w+)", hdr, re.I)
                    self.parts.append((round((time.monotonic() - self.t0) * 1000), st.group(1) if st else None))
                    buf = buf[j + 4 + n:]
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except OSError as e:
            if not self.stop:
                self.error = type(e).__name__

    def close(self):
        self.stop = True
        try:
            if self.sock:
                self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
        except OSError:
            pass

    def first(self):
        return self.parts[0] if self.parts else (None, None)

    def first_live_ms(self):
        return next((ms for ms, st in self.parts if st == "live"), None)

    def has_states(self):
        return any(st is not None for _, st in self.parts)


BUSY = {"why": None}          # set when a guarded server gets busy DURING a scenario


class SlotWatch(threading.Thread):
    """Polls /api/status: peak NVR slots in use during a scenario. Every 5 s it also
    re-checks the guarded servers; if one gets busy, BUSY is set and the running
    scenario closes its streams at once."""

    def __init__(self, api):
        super().__init__(daemon=True)
        self.api, self.stop = api, False
        self.peak = {}

    def run(self):
        last_guard = time.monotonic()
        while not self.stop:
            try:
                d = self.api.status()
                for k, v in d["nvrs"].items():
                    self.peak[k] = max(self.peak.get(k, 0), v["active"])
            except Exception:
                pass
            if time.monotonic() - last_guard >= 2.0:
                last_guard = time.monotonic()
                why = guard_check()
                if why:
                    BUSY["why"] = why
            time.sleep(0.25)


def healthy(c):
    return (c["nvr"], c["channel"]) not in KNOWN_DEAD


def guard_check():
    """-> None if every guarded server is idle, else a reason (host only)."""
    urls = [u.strip() for u in os.environ.get("RELAY_GUARDS", "").split(",") if u.strip()]
    for u in urls:
        host = urllib.parse.urlsplit(u).netloc
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            d = json.load(urllib.request.urlopen(req, timeout=10))
        except Exception as e:
            return f"guard {host} unreadable ({type(e).__name__})"
        busy = {k: v["active"] for k, v in d["nvrs"].items() if v["active"]}
        watched = sum(1 for c in d["cameras"] if c["viewers"])
        if busy or watched:
            return f"guard {host} is in use (NVR slots {busy or 0}, {watched} cameras watched)"
    return None


class Bench:
    def __init__(self, api, pid=None):
        self.api, self.pid = api, pid
        self.grid = api.grid()
        self.results = {}
        self.aborted = None

    def page(self, n):
        return self.grid[(n - 1) * PER: n * PER]

    def _guard(self):
        why = guard_check()
        if why:
            self.aborted = why
            raise SystemExit(f"ABORTED: {why}")

    def open(self, cams, prio=None):
        vs = [Viewer(self.api, c["index"], prio) for c in cams]
        for v in vs:
            v.start()
        return vs

    @staticmethod
    def close(vs):
        for v in vs:
            v.close()

    def view(self, cams, prio=None, timeout=30.0):
        """Open one MJPEG stream per camera (exactly like the grid) and wait until
        every healthy camera shows LIVE video -> (viewers, rows).
        LIVE = the server marked the part 'live' (X-Frame-State), or -- for a server
        without that header -- the camera's worker PUBLISHED A NEW FRAME after the
        stream was opened (framesPublished went up). A re-served old frame never counts."""
        before = self.api.status()
        pre = {c["index"]: c for c in before["cameras"]}
        vs = self.open(cams, prio)
        t_open = time.monotonic()
        want = [(c, v) for c, v in zip(cams, vs) if healthy(c)]
        legacy = {}
        while time.monotonic() - t_open < timeout:
            if BUSY["why"]:
                self.close(vs)
                self.aborted = BUSY["why"]
                raise SystemExit(f"ABORTED during the scenario: {BUSY['why']}")
            if any(v.has_states() for v in vs):
                if all(v.first_live_ms() is not None for _, v in want):
                    break
            else:
                try:
                    now = {c["index"]: c for c in self.api.status()["cameras"]}
                except Exception:
                    now = {}
                for c, v in want:
                    ix = c["index"]
                    if ix not in legacy and now.get(ix, {}).get("framesPublished", 0) > \
                            pre.get(ix, {}).get("framesPublished", 0):
                        legacy[ix] = round((time.monotonic() - (v.t0 or t_open)) * 1000)
                if all(c["index"] in legacy for c, _ in want):
                    break
            time.sleep(0.15)
        rows = []
        for c, v in zip(cams, vs):
            ms, st = v.first()
            live = v.first_live_ms() if v.has_states() else legacy.get(c["index"])
            rows.append({"index": c["index"], "name": c.get("displayName") or c["name"], "nvr": c["nvr"],
                         "channel": c["channel"], "healthy": healthy(c),
                         "tierBefore": pre.get(c["index"], {}).get("tier"),
                         "firstImageMs": ms, "firstImageState": st, "firstLiveMs": live, "error": v.error})
        return vs, rows

    def run_scenario(self, name, fn):
        self._guard()
        w = SlotWatch(self.api)
        w.start()
        o0 = sum(c["opens"] for c in self.api.status()["cameras"])
        t0 = time.monotonic()
        try:
            out = fn()
        finally:
            w.stop = True
            w.join(1)
        out["peakNvrSlots"] = dict(w.peak)
        out["upstreamOpens"] = sum(c["opens"] for c in self.api.status()["cameras"]) - o0
        out["seconds"] = round(time.monotonic() - t0, 1)
        self.results[name] = out
        self._guard()
        return out

    def switch(self, vs):
        """The grid's page change: close every stream, then 500 ms hand-off."""
        self.close(vs)
        time.sleep(0.5)

    # ── scenarios ────────────────────────────────────────────────────────
    def sc_page1(self):
        vs, rows = self.view(self.page(1))
        self.switch(vs)
        return {"page1": rows}

    def sc_page1to4(self):
        vs, _ = self.view(self.page(1))
        self.switch(vs)
        vs, p4 = self.view(self.page(4))
        self.switch(vs)
        vs, back = self.view(self.page(1))
        self.switch(vs)
        return {"page4": p4, "backToPage1": back}

    def sc_fullscreen(self):
        cams = self.page(1)
        vs, _ = self.view(cams)
        self.close(vs[1:])                               # open_(): the other tiles stop FIRST
        full, fs = self.view([cams[0]], prio="full")
        time.sleep(2.0)
        self.close(full)                                 # close_(): back to the grid
        again, back = self.view(cams[1:])
        self.switch(again + vs[:1])
        return {"fullscreen": fs, "backToGrid": back}

    def sc_page1to2(self):
        vs, _ = self.view(self.page(1))
        self.switch(vs)
        vs, p2 = self.view(self.page(2))
        self.switch(vs)
        return {"page2": p2}

    def sc_share(self):
        cam = self.page(1)[0]
        ix = cam["index"]
        opens = lambda: next(c["opens"] for c in self.api.status()["cameras"] if c["index"] == ix)  # noqa: E731
        o0 = opens()
        vs, _ = self.view([cam])
        steps = {1: opens() - o0}
        for n in (3, 5):
            vs += self.open([cam] * (n - len(vs)))
            time.sleep(3.0)
            steps[n] = opens() - o0
        viewers = next(c["viewers"] for c in self.api.status()["cameras"] if c["index"] == ix)
        self.switch(vs)
        return {"camera": cam.get("displayName") or cam["name"], "viewersSeen": viewers,
                "upstreamOpensAfterViewers": steps}

    def sc_resources(self, seconds=45):
        if not self.pid:
            return {"skipped": "no --pid"}
        idle = sample_window(self.pid, seconds)
        vs, _ = self.view(self.page(1))
        watched = sample_window(self.pid, seconds)
        self.switch(vs)
        return {"idle": idle, "page1Open": watched}


# ── resource sampling (Windows PowerShell; no extra packages) ──────────────────
def _ps(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def proc_sample(pid):
    out = _ps(f"$p = Get-Process -Id {int(pid)}; '{{0}} {{1}} {{2}}' -f "
              f"$p.TotalProcessorTime.TotalSeconds, $p.WorkingSet64, $p.Threads.Count")
    cpu, ws, th = out.split()
    return float(cpu), int(ws), int(th)


def net_rx():
    out = _ps("(Get-NetAdapterStatistics | Measure-Object -Sum ReceivedBytes).Sum")
    return int(float(out or 0))


def sample_window(pid, seconds):
    c0, _, _ = proc_sample(pid)
    n0, t0 = net_rx(), time.monotonic()
    rss = []
    while time.monotonic() - t0 < seconds:
        rss.append(proc_sample(pid)[1])
        time.sleep(max(0.0, min(5.0, seconds - (time.monotonic() - t0))))
    c1, ws, th = proc_sample(pid)
    n1, t1 = net_rx(), time.monotonic()
    wall = t1 - t0
    cores = os.cpu_count() or 1
    return {"seconds": round(wall, 1), "cpuPctOfOneCore": round((c1 - c0) / wall * 100, 1),
            "cpuPctOfMachine": round((c1 - c0) / wall * 100 / cores, 2), "cores": cores,
            "rssMB": round(max(rss + [ws]) / 2 ** 20, 1), "threads": th,
            "systemRxMbps": round((n1 - n0) * 8 / wall / 1e6, 2)}


def fmt(v):
    return "-" if v is None else f"{v}"


def print_rows(title, rows):
    print(f"  {title}")
    for r in rows:
        print(f"    #{r['index'] + 1:<2} {r['name'][:22]:22} {r['nvr']} ch{r['channel']:<2} "
              f"{'' if r['healthy'] else '(dead) '}tier={fmt(r['tierBefore']):12} first image "
              f"{fmt(r['firstImageMs']):>5} ms ({fmt(r['firstImageState'])})  first LIVE {fmt(r['firstLiveMs']):>5} ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.getenv("CCTV_PORT", "8000")))
    ap.add_argument("--key", default=os.getenv("RELAY_KEY", ""))
    ap.add_argument("--pid", type=int, default=None, help="server process id (CPU/RAM sampling)")
    ap.add_argument("--scenarios", default="page1,page1to4,fullscreen,page1to2,share,resources")
    ap.add_argument("--out", default=None)
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    b = Bench(Api(a.host, a.port, a.key), a.pid)
    print(f"relay bench {a.label} -> {a.host}:{a.port} | {len(b.grid)} cameras")
    try:
        for name in [s.strip() for s in a.scenarios.split(",") if s.strip()]:
            out = b.run_scenario(name, getattr(b, "sc_" + name))
            print(f"\n[{name}] {out.get('seconds')} s | upstream opens {out.get('upstreamOpens')} | "
                  f"peak NVR slots {out.get('peakNvrSlots')}")
            for k, v in out.items():
                if isinstance(v, list):
                    print_rows(k, v)
                elif k not in ("seconds", "upstreamOpens", "peakNvrSlots"):
                    print(f"  {k}: {v}")
    except SystemExit as e:
        print(str(e))
    finally:
        if a.out:
            with open(a.out, "w") as f:
                json.dump({"label": a.label, "aborted": b.aborted, "results": b.results}, f, indent=1)


if __name__ == "__main__":
    main()
