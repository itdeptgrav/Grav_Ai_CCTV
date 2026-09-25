"""Camera Settings tests: persistence, order model, validation, atomic writes, the
HTTP API + auth, display names in grid/status/placeholder, and the preview-stream
lifecycle of the Settings page. Offline: cv2 is stubbed, no NVR needed, and the
settings file lives in a temp folder (the real data/ folder is never touched).

Run:  python test_camera_settings.py        (exits non-zero on any failure)
"""
import os
import sys
import json
import time
import types
import shutil
import socket
import tempfile
import threading
import http.client as hc

TMP = tempfile.mkdtemp(prefix="cctv-settings-test-")
SERVER_FILE = os.path.join(TMP, "server", "data", "camera-settings.json")
os.environ["CCTV_SETTINGS_FILE"] = SERVER_FILE
os.environ["CCTV_PREFLIGHT"] = "0"
os.environ["CCTV_LOG_EVENTS"] = "0"

import numpy as np                                   # noqa: E402

DRAWN = []                                           # text drawn by cv2.putText


class _Cap:
    def __init__(self, *a, **k):
        pass

    def isOpened(self):
        return True

    def grab(self):
        time.sleep(0.02)
        return True

    def retrieve(self):
        return True, np.zeros((8, 8, 3), dtype="uint8")

    def read(self):
        return (True, self.retrieve()[1]) if self.grab() else (False, None)

    def release(self):
        pass


cv2 = types.ModuleType("cv2")
cv2.CAP_FFMPEG = 0
cv2.IMWRITE_JPEG_QUALITY = 1
cv2.FONT_HERSHEY_SIMPLEX = 0
cv2.VideoCapture = _Cap
cv2.resize = lambda frame, size: frame
cv2.imencode = lambda ext, frame, *a: (True, memoryview(b"jpegbytes"))
cv2.putText = lambda img, text, *a, **k: DRAWN.append(text)
sys.modules["cv2"] = cv2

import server                                        # noqa: E402
from nvr_config import CAMERAS, NVRS                 # noqa: E402
from camera_settings import CameraSettings, camera_key, NAME_MAX   # noqa: E402


class _Up:
    checked, reachable, label = time.time(), True, "NVR REACHABLE"


server.MONITOR.get = lambda nvr: _Up()

N = len(CAMERAS)
IDX = {camera_key(c): i for i, c in enumerate(CAMERAS)}
HR, REC = "nvr1:8", "nvr2:8"                         # "NVR1 Cam 8", "Floor 10 - Reception"
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def new_settings(path=None):
    return CameraSettings(path or os.path.join(tempfile.mkdtemp(dir=TMP), "data", "cs.json"), CAMERAS)


def names_in_order(s, n=4):
    return [s.display_name(i) for i in s.ordered_indices()[:n]]


# ── model / persistence ──────────────────────────────────────────────────────
def test_defaults_without_file():
    s = new_settings()
    check("no file: every camera shows its technical name", all(s.display_name(i) == c["name"] for i, c in enumerate(CAMERAS)))
    check("no file: original order", s.ordered_indices() == list(range(N)))
    check("no file: nothing is written just by loading", not os.path.exists(s.path))
    snap = s.snapshot()
    check("snapshot has stable keys nvr:channel", [c["key"] for c in snap["cameras"]] == [camera_key(c) for c in CAMERAS])


def test_rename_persists_across_restart():
    s = new_settings()
    st, _ = s.update({HR: {"displayName": "HR Office"}})
    check("rename NVR1 Cam 8 -> HR Office accepted", st == 200)
    check("folder + file created automatically", os.path.isfile(s.path))
    s2 = CameraSettings(s.path, CAMERAS)                      # == server restart
    check("after restart: HR Office is kept", s2.display_name(IDX[HR]) == "HR Office")
    check("technical identity unchanged (nvr/channel/name in CAMERAS)",
          CAMERAS[IDX[HR]]["nvr"] == "nvr1" and CAMERAS[IDX[HR]]["channel"] == 8 and CAMERAS[IDX[HR]]["name"] == "NVR1 Cam 8")
    doc = json.load(open(s.path, encoding="utf-8"))
    check("file schema: version/revision/cameras keyed by nvr:channel",
          doc["version"] == 1 and doc["revision"] == 1 and doc["cameras"] == {HR: {"displayName": "HR Office"}})


def test_order_positions():
    s = new_settings()
    st, _ = s.update({HR: {"displayName": "HR Office", "displayOrder": 1},
                      REC: {"displayName": "Reception", "displayOrder": 2}})
    check("HR Office = 1, Reception = 2 accepted (no need to renumber the others)", st == 200)
    order = s.ordered_indices()
    check("grid order starts HR Office, Reception", names_in_order(s, 2) == ["HR Office", "Reception"])
    rest = [i for i in range(N) if i not in (IDX[HR], IDX[REC])]
    check("all other cameras keep their original relative order", order[2:] == rest)
    check("every camera appears exactly once", sorted(order) == list(range(N)))
    s2 = CameraSettings(s.path, CAMERAS)
    check("order survives a restart", s2.ordered_indices() == order)


def test_duplicate_order_rejected():
    s = new_settings()
    s.update({HR: {"displayName": "HR Office", "displayOrder": 1}})
    before = open(s.path, "rb").read()
    st, pl = s.update({"nvr1:9": {"displayOrder": 1}})
    check("duplicate order -> HTTP 400", st == 400)
    check("duplicate order: clear message naming the other camera",
          "Order 1 is already used by 'HR Office'" in pl["errors"][0]["message"], pl)
    check("duplicate order: file untouched", open(s.path, "rb").read() == before)
    check("duplicate order: revision unchanged", s.revision == 1)
    st, pl = s.update({"nvr1:3": {"displayOrder": 5}, "nvr1:4": {"displayOrder": 5}})
    check("two cameras given the same order in one save -> 400 for both",
          st == 400 and {e["key"] for e in pl["errors"]} == {"nvr1:3", "nvr1:4"}, pl)


def test_resets():
    s = new_settings()
    s.update({HR: {"displayName": "HR Office", "displayOrder": 1}, REC: {"displayOrder": 2}})
    st, _ = s.update({HR: {"displayName": None}})
    check("reset name -> technical name again", st == 200 and s.display_name(IDX[HR]) == "NVR1 Cam 8")
    check("reset name keeps its custom order", s.ordered_indices()[0] == IDX[HR])
    st, _ = s.update({k: {"displayOrder": None} for k in (camera_key(c) for c in CAMERAS)})
    check("reset all ordering -> original order", st == 200 and s.ordered_indices() == list(range(N)))
    doc = json.load(open(s.path, encoding="utf-8"))
    check("fully reset cameras are removed from the file", doc["cameras"] == {}, doc["cameras"])


def test_validation():
    s = new_settings()
    cases = [
        ("name too long", {HR: {"displayName": "x" * (NAME_MAX + 1)}}),
        ("HTML in name", {HR: {"displayName": "<script>alert(1)</script>"}}),
        ("control char in name", {HR: {"displayName": "HR\x00Office"}}),
        ("name not text", {HR: {"displayName": 5}}),
        ("order 0", {HR: {"displayOrder": 0}}),
        ("order > cameras", {HR: {"displayOrder": N + 1}}),
        ("order not whole", {HR: {"displayOrder": 1.5}}),
        ("order as string", {HR: {"displayOrder": "3"}}),
        ("order boolean", {HR: {"displayOrder": True}}),
        ("unknown camera", {"nvr9:1": {"displayName": "X"}}),
        ("unknown field", {HR: {"rtsp": "rtsp://x"}}),
        ("empty change set", {}),
    ]
    for label, ch in cases:
        st, _ = s.update(ch)
        check(f"rejected: {label}", st == 400)
    check("nothing was written by rejected saves", not os.path.exists(s.path))
    st, _ = s.update({HR: {"displayName": "  HR    Office \t"}})
    check("whitespace trimmed + collapsed", st == 200 and s.display_name(IDX[HR]) == "HR Office")
    s.update({HR: {"displayName": "   "}})
    check("empty name = back to the technical name", s.display_name(IDX[HR]) == "NVR1 Cam 8")
    st, _ = s.update({HR: {"displayName": "R&D Lab / Store #2"}})
    check("normal punctuation allowed", st == 200 and s.display_name(IDX[HR]) == "R&D Lab / Store #2")
    st, _ = s.update({HR: {"displayName": "एचआर ऑफिस"}})
    check("non-English names allowed", st == 200 and s.display_name(IDX[HR]) == "एचआर ऑफिस")


def test_revision_conflict():
    s = new_settings()
    s.update({HR: {"displayName": "HR Office"}})
    st, pl = s.update({REC: {"displayName": "Reception"}}, base_revision=0)
    check("stale page (old revision) -> 409, nothing saved", st == 409 and s.display_name(IDX[REC]) != "Reception")
    st, _ = s.update({REC: {"displayName": "Reception"}}, base_revision=s.revision)
    check("current revision -> saved", st == 200)


def test_atomic_write_failure_keeps_old_file():
    s = new_settings()
    s.update({HR: {"displayName": "HR Office"}})
    before = open(s.path, "rb").read()
    real = os.replace

    def boom(src, dst):
        raise OSError("disk yanked mid-save (simulated)")
    os.replace = boom
    try:
        try:
            s.update({REC: {"displayName": "Reception"}})
            raised = False
        except OSError:
            raised = True
    finally:
        os.replace = real
    leftovers = [f for f in os.listdir(os.path.dirname(s.path)) if ".tmp-" in f]
    check("failed write raises (server answers 500)", raised)
    check("failed write: previous file intact byte-for-byte", open(s.path, "rb").read() == before)
    check("failed write: no temp files left behind", not leftovers, leftovers)
    check("failed write: in-memory settings unchanged", s.display_name(IDX[REC]) != "Reception" and s.revision == 1)


def test_corrupt_file_is_preserved():
    path = os.path.join(tempfile.mkdtemp(dir=TMP), "data", "cs.json")
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:
        f.write('{"version": 1, "cameras": {"nvr1:8": {"displayName": "HR Off')   # cut mid-write
    s = CameraSettings(path, CAMERAS)
    kept = [f for f in os.listdir(os.path.dirname(path)) if ".corrupt-" in f]
    check("corrupt file: server still starts with default names", s.display_name(IDX[HR]) == "NVR1 Cam 8")
    check("corrupt file: kept as *.corrupt-<time> (not silently overwritten)", len(kept) == 1, kept)


def test_hand_edited_duplicates_tolerated():
    path = os.path.join(tempfile.mkdtemp(dir=TMP), "data", "cs.json")
    os.makedirs(os.path.dirname(path))
    json.dump({"version": 1, "revision": 4, "cameras": {
        "nvr2:1": {"displayOrder": 3}, "nvr2:2": {"displayOrder": 3}, "nvr9:9": {"displayName": "Gone"}}},
        open(path, "w"))
    s = CameraSettings(path, CAMERAS)
    order = s.ordered_indices()
    check("hand-edited duplicate: deterministic order, every camera once", sorted(order) == list(range(N)) and order[2] == IDX["nvr2:1"])
    check("hand-edited problems are reported as warnings", len(s.warnings) == 2, s.warnings)


def test_concurrent_saves():
    s = new_settings()
    keys = [camera_key(c) for c in CAMERAS][:20]
    res = []

    def go(k, n):
        res.append(s.update({k: {"displayName": f"Room {n}"}})[0])
    th = [threading.Thread(target=go, args=(k, n)) for n, k in enumerate(keys)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    doc = json.load(open(s.path, encoding="utf-8"))
    check("20 concurrent saves all succeed", res.count(200) == 20, res)
    check("file is valid JSON with all 20 names and revision 20",
          doc["revision"] == 20 and len(doc["cameras"]) == 20)


# ── HTTP API / pages (real server on a random port) ─────────────────────────
srv = server.QuietServer(("127.0.0.1", 0), server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
TOKEN = server.TOKEN


def http(method, path, body=None, ctype="application/json", key=True, raw=None):
    sep = "&" if "?" in path else "?"
    url = path + (f"{sep}key={TOKEN}" if key and TOKEN else "")
    c = hc.HTTPConnection("127.0.0.1", PORT, timeout=10)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": ctype} if data is not None and ctype else {}
    c.request(method, url, body=data, headers=headers)
    r = c.getresponse()
    out = r.status, r.read()
    c.close()
    return out


def test_http_auth():
    if not TOKEN:
        print("NOT TESTED auth (CCTV_TOKEN is empty in this environment)")
        return
    check("/settings without the key -> 401", http("GET", "/settings", key=False)[0] == 401)
    check("GET /api/camera-settings without the key -> 401", http("GET", "/api/camera-settings", key=False)[0] == 401)
    st, _ = http("PUT", "/api/camera-settings", {"cameras": {HR: {"displayName": "Hacked"}}}, key=False)
    check("PUT /api/camera-settings without the key -> 401 (not publicly writable)", st == 401)
    check("the unauthorised PUT changed nothing", server.SETTINGS.display_name(IDX[HR]) != "Hacked")
    st, body = http("GET", "/settings")
    check("/settings with the key -> 200 settings page", st == 200 and b"Camera Settings" in body)


def test_http_api_is_safe():
    st, body = http("GET", "/api/camera-settings")
    d = json.loads(body)
    secrets = [v for n in NVRS.values() for v in (n["user"], n["pass"]) if v and len(v) >= 4]
    text = body.decode("utf-8")
    check("GET /api/camera-settings -> 200 with all cameras", st == 200 and len(d["cameras"]) == N)
    check("no username/password/RTSP URL in the settings API",
          not any(s in text for s in secrets) and "rtsp://" not in text)
    need = {"key", "index", "technicalName", "nvr", "channel", "displayName", "displayOrder", "customName", "customOrder"}
    check("settings API fields present", all(need <= set(c) for c in d["cameras"]))


def test_http_put_errors():
    check("wrong content type -> 415", http("PUT", "/api/camera-settings", raw=b"x=1", ctype="application/x-www-form-urlencoded")[0] == 415)
    check("invalid JSON -> 400", http("PUT", "/api/camera-settings", raw=b"{nope")[0] == 400)
    check("oversized body -> 413", http("PUT", "/api/camera-settings", raw=b"{" + b" " * (70 * 1024) + b"}")[0] == 413)


def test_http_rename_and_order_reach_grid_and_status():
    st, body = http("PUT", "/api/camera-settings", {"cameras": {
        HR: {"displayName": "HR Office", "displayOrder": 1}, REC: {"displayName": "Reception", "displayOrder": 2}}})
    check("PUT rename + order -> 200", st == 200 and json.loads(body)["ok"], body[:200])
    st, body = http("PUT", "/api/camera-settings", {"cameras": {"nvr1:9": {"displayOrder": 2}}})
    d = json.loads(body)
    check("PUT duplicate order -> 400 with a clear message",
          st == 400 and "Order 2 is already used by 'Reception'" in d["errors"][0]["message"], d)
    cams = json.loads(http("GET", "/api/cameras")[1])
    grid = sorted(cams, key=lambda c: (c["displayOrder"], c["index"]))
    check("/api/cameras: grid order (sorted by displayOrder) starts HR Office, Reception",
          [c["displayName"] for c in grid[:2]] == ["HR Office", "Reception"])
    check("/api/cameras: technical index/name unchanged (stream still /stream/18 for NVR1 Cam 8)",
          grid[0]["index"] == IDX[HR] and grid[0]["technicalName"] == "NVR1 Cam 8" and grid[0]["name"] == "NVR1 Cam 8")
    check("grid page 1 = orders 1-6 (pagination after sorting)",
          [c["displayOrder"] for c in grid[:6]] == [1, 2, 3, 4, 5, 6])
    stat = json.loads(http("GET", "/api/status")[1])["cameras"][IDX[HR]]
    check("/api/status shows displayName AND technicalName",
          stat["displayName"] == "HR Office" and stat["technicalName"] == "NVR1 Cam 8" and stat["displayOrder"] == 1)
    check("settings saved to the configured server file", json.load(open(SERVER_FILE, encoding="utf-8"))["cameras"][HR]["displayName"] == "HR Office")


def test_placeholder_uses_display_name():
    cam = server.STREAMS[IDX[HR]]
    DRAWN.clear()
    cam._ph_key = None
    cam._placeholder()
    check("status image (placeholder) is labelled with the display name", "HR Office" in DRAWN, DRAWN)


def _open_stream(i, fps=None):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    path = f"/stream/{i}?key={TOKEN}" + (f"&fps={fps}" if fps else "") if TOKEN else f"/stream/{i}" + (f"?fps={fps}" if fps else "")
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: t\r\n\r\n".encode())
    return s


def _count_frames(s, seconds):
    s.settimeout(0.3)
    data, end = b"", time.time() + seconds
    while time.time() < end:
        try:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        except socket.timeout:
            pass
    return data.count(b"--frame")


def _viewers():
    return {i: server.STREAMS[i].viewers for i in range(N) if server.STREAMS[i].viewers}


def _owners():
    with server._ACTIVE_LOCK:
        return {i for o in server.NVR_OWNERS.values() for i in o}


def _wait(cond, t=4.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def test_settings_previews_lifecycle():
    page1 = [0, 1, 2, 3]
    socks = [_open_stream(i, fps=2) for i in page1]
    ok = _wait(lambda: set(_viewers()) == set(page1) and _owners() == set(page1))
    check("settings page 1: exactly its 4 previews are viewers and hold slots", ok, (_viewers(), _owners()))
    low = _count_frames(socks[0], 2.0)
    full = _open_stream(4)
    high = _count_frames(full, 2.0)
    full.close()
    check("preview at ?fps=2 sends far fewer frames than the grid stream", low <= 7 and high >= 10, (low, high))
    for s in socks:
        s.close()
    page2 = [6, 7, 8, 9]
    socks = [_open_stream(i, fps=2) for i in page2]
    ok = _wait(lambda: set(_viewers()) == set(page2) and _owners() == set(page2))
    check("settings page switch: old previews released, only the new 4 hold viewers/slots", ok, (_viewers(), _owners()))
    for s in socks:
        s.close()
    ok = _wait(lambda: not _viewers() and not _owners())
    check("leaving the settings page: all preview viewers and slots cleaned up", ok, (_viewers(), _owners()))


def test_pages_render_names_as_text():
    page, sp = server.PAGE, server.SETTINGS_PAGE
    check("grid sorts by displayOrder before paging", "(a.displayOrder - b.displayOrder)" in page)
    check("grid tile shows the display name as text", "c.span.textContent = cam.displayName" in page)
    check("grid opens streams by technical index", "c.idx = cam.index" in page)
    check("fullscreen shows display name + technical line",
          "getElementById('title').textContent = cam.displayName" in page and "getElementById('tech')" in page)
    check("grid has a Settings link (keeps the ?key)", "settingsLink" in page and "'/settings' + q" in page)
    check("grid still has the stream fixes (fetchpriority, unload clean-up, retry URL)",
          "<img fetchpriority=high>" in page and "addEventListener('beforeunload', stopAllStreams)" in page
          and "(q ? '&' : '?')" in page)
    check("settings page never injects HTML (no innerHTML at all)", "innerHTML" not in sp)
    check("settings page: 4 previews per page at low fps via the normal stream",
          "PER = 4" in sp and "'fps=' + PREVIEW_FPS" in sp and "'/stream/' + i" in sp)
    check("settings page: Save All, per-camera Save, search, reset name/order, drag+drop, unsaved warning",
          all(x in sp for x in ("Save All Changes", "class=\"save primary\"", "id=search", "Reset name",
                                 "Reset all ordering", "dragstart", "beforeunload", "e.returnValue")))


def test_http_resets_restore_defaults():
    st, _ = http("PUT", "/api/camera-settings", {"cameras": {HR: {"displayName": None}}})
    check("reset name via API -> technical name", st == 200 and server.SETTINGS.display_name(IDX[HR]) == "NVR1 Cam 8")
    st, _ = http("PUT", "/api/camera-settings", {"cameras": {k: {"displayOrder": None} for k in IDX}})
    cams = json.loads(http("GET", "/api/cameras")[1])
    check("reset all ordering via API -> grid back to original order",
          st == 200 and [c["index"] for c in sorted(cams, key=lambda c: c["displayOrder"])] == list(range(N)))


if __name__ == "__main__":
    t0 = time.time()
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                print(f"\n-- {name}")
                fn()
    finally:
        srv.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}  ({time.time() - t0:.1f}s)")
    sys.exit(1 if FAILS else 0)
