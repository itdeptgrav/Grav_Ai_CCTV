"""Open N nvr1 substreams concurrently and read them for a while, reporting how
many frames each got and whether any stream DROPPED (read failed). Tells us if
NVR1 can sustain several simultaneous substream pulls over the current path."""
import os, sys, time, threading
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
import cv2
from nvr_config import make_url, resolve_nvr_ips
resolve_nvr_ips(verbose=False)

CHANNELS = [8, 11, 13, 14, 3, 4]     # known-alive nvr1 channels
SECONDS  = 30
results = {}

def reader(ch):
    cap = cv2.VideoCapture(make_url("nvr1", ch), cv2.CAP_FFMPEG,
        [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000])
    if not cap.isOpened():
        results[ch] = "OPEN-FAIL"; cap.release(); return
    frames = 0; drops = 0; t_end = time.time() + SECONDS
    while time.time() < t_end:
        ok, fr = cap.read()
        if ok and fr is not None:
            frames += 1
        else:
            drops += 1
            break            # first drop ends this stream (mirrors server: reconnect)
    dt = SECONDS - max(0, t_end - time.time())
    results[ch] = f"{frames} frames in {dt:.0f}s, {'DROPPED' if drops else 'stable'}"
    cap.release()

n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
chans = CHANNELS[:n]
print(f"Reading {n} nvr1 substreams concurrently for {SECONDS}s: channels {chans}")
threads = [threading.Thread(target=reader, args=(c,)) for c in chans]
for t in threads: t.start()
for t in threads: t.join()
for c in chans:
    print(f"  nvr1 ch{c}: {results.get(c)}")
