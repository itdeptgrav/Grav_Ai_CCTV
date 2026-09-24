"""Re-measure the 6-camera page (Page 3: nvr2 ch13 dead + nvr1 ch3-7) using the
CORRECT constructor-param open/read timeout. Compares three strategies to reveal
whether OpenCV serializes opens globally, and what first-healthy / all-healthy are."""
import os, time, threading
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
import cv2
from nvr_config import make_url, resolve_nvr_ips
resolve_nvr_ips(verbose=False)

PAGE3 = [("nvr2", 13), ("nvr1", 3), ("nvr1", 4), ("nvr1", 5), ("nvr1", 6), ("nvr1", 7)]
OPEN_MS, READ_MS = 6000, 6000

def open_cap(nvr, ch):
    return cv2.VideoCapture(make_url(nvr, ch), cv2.CAP_FFMPEG,
                            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_MS,
                             cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_MS])

def run(label, connect_limit):
    sem = threading.Semaphore(connect_limit) if connect_limit else None
    timeline = []
    start = time.time()
    def worker(nvr, ch):
        if sem: sem.acquire()
        released = False
        try:
            cap = open_cap(nvr, ch)
            if sem: sem.release(); released = True
            if not cap.isOpened():
                timeline.append((f"{nvr} ch{ch}", "OPEN-FAIL", time.time()-start)); cap.release(); return
            for _ in range(120):
                ok, fr = cap.read()
                if ok and fr is not None:
                    timeline.append((f"{nvr} ch{ch}", "LIVE", time.time()-start)); cap.release(); return
            timeline.append((f"{nvr} ch{ch}", "no-frame", time.time()-start)); cap.release()
        finally:
            if sem and not released: sem.release()
    threads = [threading.Thread(target=worker, args=(n, c)) for n, c in PAGE3]
    for t in threads: t.start()
    for t in threads: t.join()
    timeline.sort(key=lambda x: x[2])
    print(f"\n--- {label} (open_to={OPEN_MS}ms) ---")
    for name, st, ts in timeline:
        print(f"  {ts:6.2f}s  {name:12} {st}")
    live = [ts for _, st, ts in timeline if st == "LIVE"]
    if live:
        print(f"  >>> first healthy: {min(live):.2f}s | all healthy: {max(live):.2f}s")

run("A: all 6 concurrent, no connect limit", connect_limit=0)
run("B: connect limit 2", connect_limit=2)
run("C: connect limit 3", connect_limit=3)
