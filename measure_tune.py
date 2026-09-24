"""Measure how FFmpeg probesize/analyzeduration/low-delay tuning affects RTSP OPEN
time (the serialized bottleneck). Constructor-param open/read timeout is always set.
Each opts string is tested in a FRESH subprocess (env re-read). argv: nvr ch opts"""
import os, sys, time
opts = sys.argv[3]
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = opts
import cv2
from nvr_config import make_url, resolve_nvr_ips
resolve_nvr_ips(verbose=False)
nvr, ch = sys.argv[1], int(sys.argv[2])
url = make_url(nvr, ch)
best = None
for i in range(2):  # cold then warm
    t0 = time.time()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG,
                           [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000])
    t_open = time.time()
    if not cap.isOpened():
        print(f"  [{nvr} ch{ch}] OPEN-FAIL {t_open-t0:.2f}s | opts={opts}"); cap.release(); break
    got = None
    for _ in range(120):
        ok, fr = cap.read()
        if ok and fr is not None:
            got = time.time(); shape = fr.shape; break
    cap.release()
    if got and (best is None or (got-t0) < best):
        best = got - t0
        print(f"  [{nvr} ch{ch}] run{i+1}: open {t_open-t0:.2f}s | first_frame {got-t_open:.2f}s | TOTAL {got-t0:.2f}s | {shape[1]}x{shape[0]}")
