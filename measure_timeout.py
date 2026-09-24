"""Open ONE camera and print how long the open took. The FFmpeg option string is
taken from argv[3] (set into OPENCV_FFMPEG_CAPTURE_OPTIONS) so a parent can test
different timeout options in fresh subprocesses.
Usage: python measure_timeout.py <nvr> <channel> "<ffmpeg-opts>" [open_to_ms]"""
import os, sys, time
nvr, ch, opts = sys.argv[1], int(sys.argv[2]), sys.argv[3]
open_to = int(sys.argv[4]) if len(sys.argv) > 4 else 0
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = opts
import cv2
from nvr_config import make_url, resolve_nvr_ips
resolve_nvr_ips(verbose=False)
url = make_url(nvr, ch)
t0 = time.time()
if open_to and hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
    cap = cv2.VideoCapture()
    cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_to)
    cap.open(url, cv2.CAP_FFMPEG)
else:
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
dt = time.time() - t0
print(f"opts={opts!r} open_to={open_to} -> opened={cap.isOpened()} in {dt:.2f}s")
cap.release()
