"""Test the CORRECT way to set open/read timeout: pass as constructor params
(3rd arg list), which the FFmpeg backend reads BEFORE the open begins."""
import os, sys, time
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
import cv2
from nvr_config import make_url, resolve_nvr_ips
resolve_nvr_ips(verbose=False)

nvr, ch = sys.argv[1], int(sys.argv[2])
open_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 3000
read_ms = int(sys.argv[4]) if len(sys.argv) > 4 else 5000
url = make_url(nvr, ch)
params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, read_ms]
t0 = time.time()
cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
t_open = time.time()
opened = cap.isOpened()
if not opened:
    print(f"{nvr} ch{ch}: OPEN-FAIL in {t_open-t0:.2f}s (open_to={open_ms}ms read_to={read_ms}ms)")
    cap.release(); sys.exit()
got = None
for _ in range(120):
    ok, fr = cap.read()
    if ok and fr is not None:
        got = time.time(); shape = fr.shape; break
cap.release()
if got:
    print(f"{nvr} ch{ch}: open {t_open-t0:.2f}s | first_frame {got-t_open:.2f}s | TOTAL {got-t0:.2f}s | {shape[1]}x{shape[0]} (open_to={open_ms}ms)")
else:
    print(f"{nvr} ch{ch}: opened but NO frame in {time.time()-t_open:.2f}s (read_to={read_ms}ms)")
