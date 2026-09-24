"""Measure 6-camera page startup with a per-NVR CONNECT (handshake) limit, so only
N RTSP opens happen at once. Records when each camera gets its first frame relative
to page start (progressive timeline). Tests connect-limit 1 and 2."""
import os, time, threading
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")
import cv2
from nvr_config import make_url, resolve_nvr_ips
resolve_nvr_ips(verbose=False)

# probe whether explicit open-timeout property is supported by this build
HAS_OPEN_TO = hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC")
HAS_READ_TO = hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC")
print(f"CAP_PROP_OPEN_TIMEOUT_MSEC supported: {HAS_OPEN_TO} | READ: {HAS_READ_TO}")

PAGE3 = [("nvr2", 13), ("nvr1", 3), ("nvr1", 4), ("nvr1", 5), ("nvr1", 6), ("nvr1", 7)]

def run(connect_limit, open_to_ms=None):
    sems = {"nvr1": threading.Semaphore(connect_limit), "nvr2": threading.Semaphore(connect_limit)}
    timeline = []
    start = time.time()
    def worker(nvr, ch):
        sem = sems[nvr]
        sem.acquire()
        held = True
        try:
            if open_to_ms and HAS_OPEN_TO:
                cap = cv2.VideoCapture()
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_to_ms)
                cap.open(make_url(nvr, ch), cv2.CAP_FFMPEG)
            else:
                cap = cv2.VideoCapture(make_url(nvr, ch), cv2.CAP_FFMPEG)
            opened = cap.isOpened()
            # release the connect slot as soon as the handshake is done
            sem.release(); held = False
            if not opened:
                timeline.append((f"{nvr} ch{ch}", "OPEN-FAIL", time.time() - start)); cap.release(); return
            for _ in range(120):
                ok, fr = cap.read()
                if ok and fr is not None:
                    timeline.append((f"{nvr} ch{ch}", "LIVE", time.time() - start)); cap.release(); return
            timeline.append((f"{nvr} ch{ch}", "no-frame", time.time() - start)); cap.release()
        finally:
            if held: sem.release()
    threads = [threading.Thread(target=worker, args=(n, c)) for n, c in PAGE3]
    for t in threads: t.start()
    for t in threads: t.join()
    timeline.sort(key=lambda x: x[2])
    print(f"\n--- connect_limit={connect_limit} open_to={open_to_ms} ---")
    for name, st, ts in timeline:
        print(f"  {ts:6.2f}s  {name:12} {st}")
    live = [ts for _, st, ts in timeline if st == "LIVE"]
    if live:
        print(f"  first healthy camera: {min(live):.2f}s | all healthy: {max(live):.2f}s")

run(connect_limit=2)
run(connect_limit=1)
if HAS_OPEN_TO:
    run(connect_limit=2, open_to_ms=3500)
