"""Map which NVR channels actually have a working camera, independent of the
server's slot logic. Opens each channel directly (serialized), reports alive/dead."""
import os, time
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
import cv2
from nvr_config import make_url, resolve_nvr_ips, CAMERAS
resolve_nvr_ips(verbose=False)

def probe(nvr, ch):
    t0 = time.time()
    cap = cv2.VideoCapture(make_url(nvr, ch), cv2.CAP_FFMPEG,
        [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000])
    if not cap.isOpened():
        cap.release(); return "DEAD (open failed)", time.time()-t0, None
    for _ in range(60):
        ok, fr = cap.read()
        if ok and fr is not None:
            shape = f"{fr.shape[1]}x{fr.shape[0]}"; cap.release()
            return "ALIVE", time.time()-t0, shape
    cap.release(); return "DEAD (no frame)", time.time()-t0, None

alive = dead = 0
for cam in CAMERAS:
    st, dt, shape = probe(cam["nvr"], cam["channel"])
    tag = "ALIVE" if st == "ALIVE" else "dead "
    if st == "ALIVE": alive += 1
    else: dead += 1
    extra = f"{shape}" if shape else ""
    print(f"  {cam['nvr']} ch{cam['channel']:2}  {tag}  {dt:4.1f}s  {extra:10}  {cam['name']}")
print(f"\nTOTAL: {alive} alive, {dead} dead of {len(CAMERAS)}")
