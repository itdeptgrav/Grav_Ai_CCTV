"""Video quality benchmark -- Standard vs Original, measured against a RUNNING server.

    python quality_bench.py --port 8010 --key <CCTV_TOKEN> --cameras 24,7 [--pid <server pid>]
           [--scenarios probe,single,share,toggle,grid6] [--grid-original] [--save-dir DIR] [--out FILE]

--cameras: camera indices as in /stream/<index> (0-based), default: the first healthy
camera of each NVR in grid order.

Scenarios
  probe    raw RTSP facts of each camera's sub-stream (subtype=1) and main stream
           (subtype=0): codec, bitrate sent by the NVR, frame rate, GOP -- one connection
           at a time, ~8 s each (diag_cctv.rtsp_probe). Run it while the server is idle.
  single   per camera, the fullscreen view: Standard -> switch to Original -> back to
           Standard. Time to the first image and to the first real Original frame,
           delivered fps, bandwidth to the viewer, JPEG size, source/output resolution
           (/api/stream-info); the last frame of each mode is saved (--save-dir) for a
           visual comparison. With --pid: server CPU/RAM for idle / 1 Standard / 1 Original.
  share    two Original viewers of one camera -> main-stream connections made
  toggle   10 Standard <-> Original switches of one camera -> nothing left running
  grid6    6 Standard tiles of one NVR (a grid page): CPU/RAM/bandwidth; with
           --grid-original also 6 Original tiles = 6 MAIN streams from ONE NVR (heavy:
           only when nothing else uses that NVR)
  fallback for a server started with CCTV_ORIGINAL_SUBTYPE set to a stream the NVR does
           not serve: the Original viewer must be shown Standard, labelled, never 'live'

Guards as relay_bench (RELAY_GUARDS; RELAY_GUARD_DOWN_OK=1 treats "origin down" answers
as not in use): stops as soon as another server on these NVRs gets a viewer. Never more
than 6 streams per NVR. Prints no credentials.
"""
import os
import json
import time
import argparse

import relay_bench as rb
from relay_bench import Api, Viewer, SlotWatch, BUSY, guard_check, healthy

STEADY_S = 15          # measuring window once a mode is live
WINDOW_S = 30          # CPU/RAM sampling window


class QBench:
    def __init__(self, api, pid=None, save_dir=None):
        self.api, self.pid, self.save_dir = api, pid, save_dir
        self.grid = api.grid()
        self.by_index = {c["index"]: c for c in self.grid}
        self.results = {}
        self.aborted = None
        self.open_viewers = []

    # ── helpers ──────────────────────────────────────────────────────────
    def _check(self):
        if BUSY["why"]:
            for v in self.open_viewers:
                v.close()
            self.aborted = BUSY["why"]
            raise SystemExit(f"ABORTED during the scenario: {BUSY['why']}")

    def hold(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check()
            time.sleep(0.2)

    def open(self, ix, **kw):
        v = Viewer(self.api, ix, **kw)
        v.start()
        self.open_viewers.append(v)
        return v

    def close(self, *vs):
        for v in vs:
            v.close()
            if v in self.open_viewers:
                self.open_viewers.remove(v)

    def wait_for(self, v, state, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._check()
            if any(st == state for _, st in list(v.parts)):
                return True
            time.sleep(0.05)
        return False

    def info(self, ix):
        return self.api.get(f"/api/stream-info/{ix}")

    def cam_status(self, ix):
        d = self.api.status()
        return d, next(c for c in d["cameras"] if c["index"] == ix)

    def save(self, v, state, name):
        if self.save_dir and v.last.get(state):
            os.makedirs(self.save_dir, exist_ok=True)
            with open(os.path.join(self.save_dir, name), "wb") as f:
                f.write(v.last[state])
            return name
        return None

    def sample(self, seconds):
        """CPU/RAM of the server + system receive rate, checking the guards meanwhile."""
        if not self.pid:
            return None
        c0, _, _ = rb.proc_sample(self.pid)
        n0, t0 = rb.net_rx(), time.monotonic()
        rss = []
        while time.monotonic() - t0 < seconds:
            self._check()
            rss.append(rb.proc_sample(self.pid)[1])
            time.sleep(1.5)
        c1, ws, th = rb.proc_sample(self.pid)
        n1, wall = rb.net_rx(), time.monotonic() - t0
        cores = os.cpu_count() or 1
        return {"seconds": round(wall, 1), "cpuPctOfOneCore": round((c1 - c0) / wall * 100, 1),
                "cpuPctOfMachine": round((c1 - c0) / wall * 100 / cores, 2), "cores": cores,
                "rssMB": round(max(rss + [ws]) / 2 ** 20, 1), "threads": th,
                "systemRxMbps": round((n1 - n0) * 8 / wall / 1e6, 2)}

    @staticmethod
    def _q(inf):
        keys = ("qualityMode", "subtype", "sourceSize", "outputSize", "jpegQuality", "fps", "opens",
                "status", "slotHeld", "fallbackViewers")
        return {k: inf.get(k) for k in keys}

    def run(self, name, fn, *args):
        why = guard_check()
        if why:
            self.aborted = why
            raise SystemExit(f"ABORTED: {why}")
        w = SlotWatch(self.api)
        w.start()
        t0 = time.monotonic()
        try:
            out = fn(*args)
        finally:
            w.stop = True
            w.join(1)
            self.close(*list(self.open_viewers))
        out["peakNvrSlots"] = dict(w.peak)
        out["seconds"] = round(time.monotonic() - t0, 1)
        self.results[name] = out
        return out

    # ── scenarios ────────────────────────────────────────────────────────
    def sc_probe(self, cams):
        import diag_cctv          # raw RTSP (credentials from .env, never printed)
        rows = []
        for c in cams:
            for sub in (1, 0):
                self._check()
                r = diag_cctv.rtsp_probe(c["nvr"], c["channel"], subtype=sub, window=8.0)
                rows.append({"index": c["index"], "nvr": c["nvr"], "channel": c["channel"], "subtype": sub,
                             **{k: r.get(k) for k in ("result", "detail", "codec", "sdp_fps", "kbps",
                                                      "rtp_fps", "gop_s", "first_key_ms", "handshake_ms")}})
                time.sleep(1.0)
        return {"streams": rows}

    def sc_single(self, c):
        ix = c["index"]
        tag = f"cam{ix}"
        out = {"camera": c.get("displayName") or c["name"], "index": ix, "nvr": c["nvr"], "channel": c["channel"]}
        out["idle"] = self.sample(WINDOW_S // 2)
        # 1) Standard, fullscreen
        std = self.open(ix, prio="full", keep=True)
        self.wait_for(std, "live")
        t_a = std.now_ms()
        self.hold(STEADY_S)
        t_b = std.now_ms()
        ms, st = std.first()
        out["standard"] = {"firstImageMs": ms, "firstImageState": st, "firstLiveMs": std.first_live_ms(),
                           **std.window(t_a, t_b), **self._q(self.info(ix)["standard"]),
                           "savedFrame": self.save(std, "live", f"{tag}_standard.jpg")}
        out["standard"]["resources"] = self.sample(WINDOW_S)
        # 2) switch to Original the way the page does it: new src, old request aborted
        o_before = self.info(ix)["original"]["opens"]
        orig = self.open(ix, prio="full", quality="original", keep=True)
        time.sleep(0.05)
        self.close(std)
        ok = self.wait_for(orig, "live", 40.0)
        t_a = orig.now_ms()
        self.hold(STEADY_S)
        t_b = orig.now_ms()
        ms, st = orig.first()
        live_ms = orig.first_live_ms()
        before = sorted({s for m, s in list(orig.parts) if live_ms is None or m < live_ms})
        out["original"] = {"reachedLive": ok, "firstImageMs": ms, "firstImageState": st, "firstLiveMs": live_ms,
                           "statesBeforeFirstOriginalFrame": before,
                           **orig.window(t_a, t_b), **self._q(self.info(ix)["original"]),
                           "mainStreamOpens": self.info(ix)["original"]["opens"] - o_before,
                           "savedFrame": self.save(orig, "live", f"{tag}_original.jpg"),
                           "savedStandIn": [self.save(orig, s, f"{tag}_switching_{s}.jpg")
                                            for s in ("standard", "cached", "status") if orig.last.get(s)]}
        out["original"]["resources"] = self.sample(WINDOW_S)
        # 3) back to Standard
        std2 = self.open(ix, prio="full")
        time.sleep(0.05)
        self.close(orig)
        self.wait_for(std2, "live", 30.0)
        ms, st = std2.first()
        out["backToStandard"] = {"firstImageMs": ms, "firstImageState": st, "firstLiveMs": std2.first_live_ms()}
        self.hold(2.0)
        self.close(std2)
        self.hold(4.0)
        inf = self.info(ix)["original"]
        out["originalAfterAllClosed"] = {"running": inf["running"], "viewers": inf["viewers"],
                                         "slotHeld": inf["slotHeld"]}
        return out

    def sc_share(self, c):
        ix = c["index"]
        o0 = self.info(ix)["original"]["opens"]
        a = self.open(ix, prio="full", quality="original")
        self.wait_for(a, "live", 40.0)
        b = self.open(ix, prio="full", quality="original")
        self.wait_for(b, "live", 20.0)
        self.hold(3.0)
        d, cs = self.cam_status(ix)
        owners = [(o["index"], o["quality"]) for o in d["nvrs"][c["nvr"]]["owners"]]
        out = {"camera": c.get("displayName") or c["name"], "originalViewers": cs["original"]["viewers"],
               "mainStreamOpens": cs["original"]["opens"] - o0, "nvrSlotOwners": owners,
               "secondViewerFirstImageMs": b.first()[0], "secondViewerFirstImageState": b.first()[1]}
        self.close(a, b)
        return out

    def sc_toggle(self, c, n=10):
        ix = c["index"]
        cur, steps = None, []
        o0 = self.info(ix)["original"]["opens"]
        for k in range(n):
            q = "original" if k % 2 == 0 else None
            v = self.open(ix, prio="full", quality=q)
            time.sleep(0.05)
            if cur:
                self.close(cur)
            cur = v
            self.wait_for(v, "live", 40.0)
            ms, st = v.first()
            steps.append({"to": q or "standard", "firstImageMs": ms, "firstImageState": st,
                          "firstLiveMs": v.first_live_ms()})
            self.hold(1.0)
        self.close(cur)
        self.hold(5.0)
        d, cs = self.cam_status(ix)
        nv = d["nvrs"][c["nvr"]]
        return {"camera": c.get("displayName") or c["name"], "switches": steps,
                "mainStreamOpens": cs["original"]["opens"] - o0,
                "after": {"standardViewers": cs["viewers"], "originalViewers": cs["original"]["viewers"],
                          "originalRunning": cs["original"]["running"],
                          "originalSlotHeld": cs["original"]["slotHeld"],
                          "nvrSlotsActive": nv["active"],
                          "nvrSlotOwners": [(o["index"], o["quality"]) for o in nv["owners"]]}}

    def sc_grid6(self, nvr, original):
        cams = [c for c in self.grid if c["nvr"] == nvr and healthy(c)][:6]
        out = {}
        for mode in (["standard", "original"] if original else ["standard"]):
            q = "original" if mode == "original" else None
            vs = [self.open(c["index"], quality=q, fps=6 if q else None) for c in cams]
            for v in vs:
                self.wait_for(v, "live", 45.0)
            t_a = [v.now_ms() for v in vs]
            res = self.sample(WINDOW_S) or (self.hold(STEADY_S) or None)
            wins = [v.window(a, v.now_ms()) for v, a in zip(vs, t_a)]
            d = self.api.status()
            out[mode] = {"cameras": [c["index"] for c in cams],
                         "firstLiveMs": [v.first_live_ms() for v in vs],
                         "viewerKbpsTotal": sum(w["kbps"] for w in wins),
                         "avgFpsPerTile": round(sum(w["fps"] for w in wins) / len(wins), 1),
                         "avgJpegKB": round(sum(w["avgKB"] or 0 for w in wins) / len(wins), 1),
                         "nvrSlotsActive": d["nvrs"][nvr]["active"],
                         "resources": res}
            self.close(*vs)
            self.hold(3.0)
        return out

    def sc_fallback(self, c):
        ix = c["index"]
        v = self.open(ix, prio="full", quality="original", keep=True)
        self.hold(15.0)
        inf = self.info(ix)
        states = [s for _, s in list(v.parts)]
        out = {"camera": c.get("displayName") or c["name"], "states": sorted(set(states)),
               "liveOriginalFrames": states.count("live"), "standardFrames": states.count("standard"),
               "original": self._q(inf["original"]), "standardViewers": inf["standard"]["viewers"],
               "savedFrame": self.save(v, "standard", f"cam{ix}_fallback.jpg")}
        self.close(v)
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.getenv("CCTV_PORT", "8000")))
    ap.add_argument("--key", default=os.getenv("RELAY_KEY", ""))
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--cameras", default="")
    ap.add_argument("--scenarios", default="probe,single,share,toggle,grid6")
    ap.add_argument("--grid-nvr", default=None, help="NVR for grid6 (default: the first camera's)")
    ap.add_argument("--grid-original", action="store_true", help="grid6 also with 6 ORIGINAL tiles")
    ap.add_argument("--save-dir", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    b = QBench(Api(a.host, a.port, a.key), a.pid, a.save_dir)
    if a.cameras:
        cams = [b.by_index[int(x)] for x in a.cameras.split(",") if x.strip()]
    else:
        cams = [next(c for c in b.grid if c["nvr"] == k and healthy(c)) for k in ("nvr1", "nvr2")]
    print(f"quality bench -> {a.host}:{a.port} | cameras {[c['index'] for c in cams]}")
    try:
        for name in [s.strip() for s in a.scenarios.split(",") if s.strip()]:
            if name == "probe":
                runs = [("probe", (cams,))]
            elif name == "grid6":
                runs = [(f"grid6_{a.grid_nvr or cams[0]['nvr']}", (a.grid_nvr or cams[0]["nvr"], a.grid_original))]
            else:
                runs = [(f"{name}_cam{c['index']}", (c,)) for c in (cams if name == "single" else cams[:1])]
            for key, args in runs:
                out = b.run(key, getattr(b, "sc_" + name), *args)
                print(f"\n[{key}] {json.dumps(out, indent=1)}")
                if a.out:
                    with open(a.out, "w") as f:
                        json.dump({"aborted": b.aborted, "results": b.results}, f, indent=1)
    except SystemExit as e:
        print(str(e))
    finally:
        if a.out:
            with open(a.out, "w") as f:
                json.dump({"aborted": b.aborted, "results": b.results}, f, indent=1)


if __name__ == "__main__":
    main()
