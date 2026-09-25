"""Headless reproduction of the grid UI against a RUNNING server, for before/after
performance and slot-leak measurement. Mirrors the page JS exactly: 6 concurrent
MJPEG /stream/<i> connections per page; on page change close them all (clearCells),
wait 500 ms, then open the next page's 6.

    python walk_pages.py [pages...]        default: 1 2 3 4 5 1

For each page: time to first visible camera, time until all HEALTHY cameras are
visible, and every camera's final state. At each transition: NVR1/NVR2 active/max
BEFORE the switch, AFTER old-page cleanup, and AFTER the new page started -- plus
any camera not on the current page that still has viewers or holds a slot (= leak).
"""
import os
import sys
import json
import time
import socket
import threading
import http.client

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

HOST, PORT = "127.0.0.1", int(os.getenv("CCTV_PORT", "8000"))
KEY = os.getenv("CCTV_TOKEN", "")
Q = f"?key={KEY}" if KEY else ""
PER = 6
WAIT_S = float(os.getenv("WALK_WAIT_S", "45"))
# channels proven dead by diag_cctv.py (direct RTSP + OpenCV): excluded from "all healthy"
KNOWN_DEAD = {("nvr2", 7), ("nvr2", 12), ("nvr2", 13), ("nvr1", 10)}


def status():
    c = http.client.HTTPConnection(HOST, PORT, timeout=5)
    c.request("GET", "/api/status" + Q)
    d = json.loads(c.getresponse().read())
    c.close()
    return d


def cameras():
    """Cameras in the order the grid shows them (displayOrder, set on the Settings
    page). Each entry keeps its technical 'index' = the /stream/<index> to open."""
    c = http.client.HTTPConnection(HOST, PORT, timeout=5)
    c.request("GET", "/api/cameras" + Q)
    d = json.loads(c.getresponse().read())
    c.close()
    for i, cam in enumerate(d):
        cam.setdefault("index", i)
        cam.setdefault("displayOrder", i + 1)
        cam.setdefault("displayName", cam["name"])
    return sorted(d, key=lambda cam: (cam["displayOrder"], cam["index"]))


class Stream(threading.Thread):
    """One MJPEG <img>: keeps the connection open and drains it until closed."""
    def __init__(self, i):
        super().__init__(daemon=True)
        self.i, self.stop, self.bytes, self.sock = i, False, 0, None

    def run(self):
        try:
            conn = http.client.HTTPConnection(HOST, PORT, timeout=60)
            conn.request("GET", f"/stream/{self.i}" + Q)
            self.sock = conn.sock
            r = conn.getresponse()
            while not self.stop:
                b = r.read1(65536) if hasattr(r, "read1") else r.read(4096)
                if not b:
                    break
                self.bytes += len(b)
        except Exception:
            pass

    def close(self):                      # == img.removeAttribute('src')
        self.stop = True
        try:
            if self.sock:
                self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
        except OSError:
            pass


def live(cam):
    if "hasFrame" in cam:                 # new server: exact "frame is being served"
        return bool(cam["hasFrame"]) and cam["status"] == "LIVE"
    return cam["status"] == "LIVE"        # old server: LIVE ~= first frame (<0.1 s apart)


def slots(d):
    return " ".join(f"{k.upper()} {v['active']}/{v['max']}" for k, v in sorted(d["nvrs"].items()))


def strays(d, page_idx):
    """Cameras NOT on the current page that still have viewers or hold a slot."""
    out = []
    held = set()
    for v in d["nvrs"].values():
        for o in v.get("owners", []):
            held.add(o["index"])
    for c in d["cameras"]:
        if c["index"] in page_idx:
            continue
        if c["viewers"] > 0 or c.get("slotHeld") or c["index"] in held or \
                (("slotHeld" not in c) and c["status"] == "LIVE"):
            out.append(f"#{c['index'] + 1}({c['status']},v={c['viewers']})")
    return out


def main():
    pages = [int(x) for x in sys.argv[1:]] or [1, 2, 3, 4, 5, 1]
    cams = cameras()                      # display order, like the grid
    tech = {c["index"]: c for c in cams}  # technical index -> camera
    n_pages = (len(cams) + PER - 1) // PER
    open_streams, prev_idx = [], set()
    results = []
    print(f"server {HOST}:{PORT} | {len(cams)} cameras, {n_pages} pages | walk {pages}\n")
    print("TRANSITIONS (active/max per NVR):")
    for p in pages:
        # technical indices of the cameras on grid page p (after display-order sort)
        idx = [cams[pos]["index"] for pos in range((p - 1) * PER, min(p * PER, len(cams)))]
        before = status()
        for s in open_streams:            # clearCells()
            s.close()
        time.sleep(0.5)                   # page(): 500 ms teardown before showPage()
        after_cleanup = status()
        t0 = time.perf_counter()
        open_streams = [Stream(i) for i in idx]
        for s in open_streams:
            s.start()
        first_live = {}
        healthy = [i for i in idx if (tech[i]["nvr"], tech[i]["channel"]) not in KNOWN_DEAD]
        while time.perf_counter() - t0 < WAIT_S:
            d = status()
            now = round((time.perf_counter() - t0) * 1000)
            for c in d["cameras"]:
                if c["index"] in idx and c["index"] not in first_live and live(c):
                    first_live[c["index"]] = now
            if all(i in first_live for i in healthy):
                break
            time.sleep(0.2)
        after_start = status()
        final = {c["index"]: c for c in after_start["cameras"]}
        v = sorted(first_live.values())
        all_h = max((first_live.get(i, 10 ** 9) for i in healthy), default=0)
        print(f"  -> page {p}: before [{slots(before)}] | after old-page cleanup [{slots(after_cleanup)}] "
              f"strays={strays(after_cleanup, set()) or 'none'} | after new page started [{slots(after_start)}] "
              f"strays={strays(after_start, set(idx)) or 'none'}")
        results.append({"page": p, "first": v[0] if v else None,
                        "all_healthy": all_h if all_h < 10 ** 9 else None,
                        "live": len(first_live), "healthy": len(healthy), "cams": len(idx),
                        "per_cam": {i + 1: first_live.get(i) for i in idx},
                        "final": {i + 1: final[i]["status"] for i in idx}})
        prev_idx = set(idx)
    for s in open_streams:
        s.close()
    time.sleep(1.5)
    end = status()
    print(f"  -> all pages closed: [{slots(end)}] strays={strays(end, set()) or 'none'}")
    print("\nPAGES (ms from opening the page's 6 streams):")
    for r in results:
        print(f"  page {r['page']}: first visible {r['first']} | all {r['healthy']} healthy visible "
              f"{r['all_healthy'] if r['all_healthy'] is not None else 'NOT ALL (timeout)'} | live {r['live']}/{r['cams']}")
        for n, t in r["per_cam"].items():
            c = tech[n - 1]
            print(f"      #{n:<2} {c['displayName'][:22]:22} {c['nvr']} ch{c['channel']:<2} "
                  f"visible {t if t is not None else '-':>6}   final: {r['final'][n]}")
    if os.getenv("WALK_JSON"):
        with open(os.getenv("WALK_JSON"), "w") as f:
            json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
