"""Measure time-to-first-frame for the real cameras. Run:  python measure_startup.py
Prints RTSP open time, first-frame time and total, for single cameras (cold/warm),
NVR1 vs NVR2, and 6 cameras opened simultaneously (a page)."""
import os, time, threading, sys
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")
import cv2
from nvr_config import make_url, resolve_nvr_ips

resolve_nvr_ips(verbose=False)

def measure(nvr, ch, label, backend_opts=None):
    url = make_url(nvr, ch)
    t0 = time.time()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    t_open = time.time()
    if not cap.isOpened():
        print(f"  {label:22} OPEN FAILED after {t_open-t0:.2f}s")
        cap.release(); return None
    got = None
    for _ in range(120):
        ok, fr = cap.read()
        if ok and fr is not None:
            got = time.time(); shape = fr.shape; break
    cap.release()
    if got:
        print(f"  {label:22} open {t_open-t0:5.2f}s | first_frame {got-t_open:5.2f}s | TOTAL {got-t0:5.2f}s | {shape[1]}x{shape[0]}")
        return got - t0
    print(f"  {label:22} opened but NO frame after {time.time()-t_open:.2f}s")
    return None

print("=== SINGLE CAMERA, 3 runs (cold then warm) ===")
print("NVR2 ch1 (Floor 19 - Storage):")
for i in range(3):
    measure("nvr2", 1, f"run {i+1}")
print("NVR1 ch3 (NVR1 Cam 3):")
for i in range(3):
    measure("nvr1", 3, f"run {i+1}")

print("\n=== 6 CAMERAS SIMULTANEOUSLY (Page 3: nvr2 ch13 + nvr1 ch3-7) ===")
page3 = [("nvr2", 13), ("nvr1", 3), ("nvr1", 4), ("nvr1", 5), ("nvr1", 6), ("nvr1", 7)]
results = {}
def worker(nvr, ch, key):
    t0 = time.time()
    cap = cv2.VideoCapture(make_url(nvr, ch), cv2.CAP_FFMPEG)
    if not cap.isOpened():
        results[key] = ("OPEN-FAIL", time.time() - t0); cap.release(); return
    for _ in range(120):
        ok, fr = cap.read()
        if ok and fr is not None:
            results[key] = ("ok", time.time() - t0); cap.release(); return
    results[key] = ("no-frame", time.time() - t0); cap.release()

threads = [threading.Thread(target=worker, args=(n, c, f"{n} ch{c}")) for n, c in page3]
t0 = time.time()
for t in threads: t.start()
for t in threads: t.join()
for k in sorted(results):
    st, dur = results[k]
    print(f"  {k:12} {st:10} first frame in {dur:.2f}s")
print(f"  --> all 6 done in {time.time()-t0:.2f}s (wall clock)")
