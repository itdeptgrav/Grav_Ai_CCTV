# GRAV CCTV — standalone live-viewing server

A small, self-contained Python web server that pulls RTSP from the NVRs and serves
a browser camera grid (MJPEG). No camera credentials reach the browser; one shared
RTSP connection per camera, opened only while someone is watching, with a per-NVR
connection limit.

This is the **hosting** build of the CCTV app: all settings come from the
environment, nothing is hardcoded, and it runs on any server (Windows or Linux).
It is independent of the CMS and of `D:\Ai_cctv` (the original dev copy).

## Files
| File | Purpose |
|---|---|
| `server.py` | The web server + persistent relay pool (`/stream`, `/snapshot`, `/api/*`) |
| `grid_page.py`, `settings_page.py`, `ui_theme.py` | The live grid, the camera settings page, shared look |
| `camera_settings.py` | Display names / grid order (stored in `data/`, never in git) |
| `rtsp_preflight.py` | Fast RTSP check before OpenCV opens a camera |
| `rtsp_audio.py` | Audio-only RTSP client for a camera's G.711 microphone track |
| `nvr_config.py` | Camera list + NVR endpoints (from env) |
| `netcheck.py` | Reachability probes (cross-platform) |
| `relay_bench.py` | Viewer-experience benchmark against a running server |
| `quality_bench.py` | Standard vs Original benchmark (resolution, fps, bandwidth, CPU) |
| `stability_bench.py` | Long viewing test: records every LIVE/CACHED change, client and server side |
| `nvr_capacity_probe.py` | SUPERVISED NVR capacity / direct-camera test (see its .txt) |
| `test_*.py` | Offline tests (no NVR needed): lifecycle, slots, settings, relay, quality, live stability, audio |
| `fake_nvr.py` | Fake NVR (RTSP + G.711) used only by `test_audio.py` |
| `requirements.txt` | `opencv-python`, `numpy`, `python-dotenv` |
| `.env.example` | Copy to `.env` and edit |
| `run.sh` / `run.bat` | One-command run (creates venv, installs, starts) |

## Quick start
1. Copy the config template and edit it:
   ```
   cp .env.example .env       # Windows: copy .env.example .env
   ```
   **Set `CCTV_TOKEN` to a strong secret.** Adjust NVR credentials/hosts if needed.
2. Run:
   - Linux/macOS: `./run.sh`
   - Windows: `run.bat`
   (or manually: `pip install -r requirements.txt` then `python server.py`)
3. Open `http://<server-ip>:8000/?key=<your CCTV_TOKEN>`.

## How it reaches the cameras
`CCTV_ACCESS_MODE=auto` (default): if the server sits on the CCTV LAN it uses the
private NVR IPs; otherwise (a hosted/cloud server) it uses `CCTV_PUBLIC_IP` and the
forwarded ports (`NVR*_PUBLIC_PORT`). The router forwards
`PUBLIC_IP:10554 -> NVR1:554` and `:20554 -> NVR2:554`.

## Persistent relay (default, `CCTV_PERSISTENT=1`)
The server runs 24/7, so it keeps up to the per-NVR cap (6) of camera streams
HOT even when nobody is watching: cameras being viewed first (fullscreen before
the grid), then recently viewed ones, then the grid order (page 1 first). A
browser subscribes to frames that are already flowing -- no RTSP handshake --
and however many browsers watch a camera, there is one upstream connection.
A camera outside the HOT pool is shown at once from its last frame in RAM,
darkened and stamped `CACHED hh:mm:ss · Connecting...`, never passed off as live,
while it is promoted. Nothing is recorded to disk. `/api/status` shows every
camera's tier (HOT / CONNECTING / WARM / COLD / OFFLINE) and role.

* Run the pool on **one** server only (production). Any other machine that runs
  `server.py` against the same NVRs must set `CCTV_PERSISTENT=0`.
* The cap stays at the verified-safe 6 per NVR. Raise it per NVR
  (`CCTV_NVR1_MAX_CONN`, `CCTV_NVR2_MAX_CONN`) only after the supervised test in
  `SUPERVISED_NVR_CAPACITY_TEST.txt`.
* `python relay_bench.py --port 8000 --key <CCTV_TOKEN>` measures what a viewer
  experiences (first image, first live frame, upstream connections).
* Details and measurements: `FINAL_CCTV_RELAY_REPORT.txt`.

## Video quality: Standard / Original
The header has a `[ Standard | Original ]` switch (also in the fullscreen bar; key `Q`).

* **Standard** (default -- also for every new browser): the NVR sub-stream
  (`subtype=1`) resized to 640x360, JPEG 70, 8 fps. Light; this is what the
  persistent pool keeps HOT.
* **Original**: the camera's main stream (`subtype=0`; verified 2560x1440 on both
  NVRs), never upscaled, JPEG 90. Fullscreen: the full source picture, up to 12 fps.
  Grid tiles: 6 fps, at most 1280 px wide (`CCTV_ORIGINAL_GRID_MAX_W`; a tile is
  never displayed larger).
* The choice is remembered per browser (localStorage), not server-wide. Settings
  page previews always stay Standard.
* Only cameras on screen use Original: one Original worker per camera, shared by
  every browser, stopped when the last Original viewer leaves (never kept in the
  background). Original streams count toward the per-NVR cap; fullscreen Original
  goes first, and nothing that is being watched is stopped to make room. A page
  switched to Original changes tile by tile -- the other tiles keep their live
  Standard picture until their turn.
* While Original starts, a tile shows the Standard picture labelled `STANDARD -
  switching to Original...`. If the main stream fails, it shows Standard labelled
  `Original unavailable` (fullscreen: "Original unavailable -- showing Standard")
  and switches to Original by itself when the main stream works. Waiting for an NVR
  slot is shown as such. Standard is never passed off as Original.
* Cost, measured 2026-09-25: a full-size Original tile (2560x1440, q90, 6 fps) is
  400-720 KB per frame = 19-36 Mbps to the browser and about 0.9 CPU core (4 MP
  decode), against 1.3-1.9 Mbps for Standard. Use Original mainly in fullscreen.
* `GET /api/stream-info/<index>`: both qualities of one camera (subtype, source and
  output size, fps, JPEG quality, slot, fallback) -- no credentials.
* `python quality_bench.py --port 8000 --key <CCTV_TOKEN>` measures both modes.
* Details: `FINAL_CCTV_QUALITY_MODE_REPORT.txt`.

## Live-stream stability and diagnostics
* A camera someone is watching is never taken off its NVR slot: only streams with no
  viewer (background, recently viewed, cache refresh, a finished quality switch, a
  lingering Original) can be preempted. Slot priorities: `FULLSCREEN_ORIGINAL` 100,
  `FULLSCREEN_STANDARD` 90, `GRID_ORIGINAL` 82, `GRID_STANDARD` 80, then the 0-viewer
  roles (`HANDOFF` 40, `RECENT` 30, `LINGER` 25, `BACKGROUND_WARM` 20,
  `CACHE_REFRESH` 10). A fullscreen view is *pinned*.
* Standard <-> Original switches are make-before-break: the old stream keeps running
  until the new one is live, then it is released (no duplicate upstream).
* Every state change of a stream is logged with its exact reason, e.g.
  `[Cam 20 NVR1 Cam 9] LIVE -> STALLED reason=NO_FRAME_AGE_2515MS (connection open,
  waiting for data from the NVR/network ...)`, `STALLED -> RECONNECTING
  reason=READ_TIMEOUT`, `RECONNECTING -> LIVE reason=RECONNECTED`,
  `LIVE -> WARM reason=POOL_DEMOTION`. Every slot taken / released is logged too.
* `/api/status`: per NVR `slotsText` ("6/6") and a `slots` table (camera, quality,
  viewers, priority, pinned, state, slot age); per camera and quality: `state`,
  `priorityName`, `pinned`, `slotAgeMs`, `reconnectCount`, `dropReasons`, `stalls`,
  `maxFrameGapMs`, `cachedReason`, `lastTransitionReason` and the last transitions.
* `python stability_bench.py --base <url> --key <K> --page 1 --minutes 10` watches
  like a browser and records every LIVE/CACHED change on both sides. When only the
  client side shows gaps (server says LIVE, 0 reconnects), the network between the
  server and that browser is the cause.

## Camera audio
* Audio is OFF for everyone until someone clicks a speaker: in a tile's info bar, in
  the fullscreen bar, or key `M`. One camera at a time per browser -- starting another
  stops the previous one. The header shows which camera is audible (click it to stop).
  Fullscreen has a volume slider (remembered per browser; 50 % = the camera's own
  level). Mute is instant, and the session stays 10 s so unmuting is instant too.
  Leaving the fullscreen view (or the page with that camera) stops its audio.
* The NVR streams carry a G.711 microphone track (NVR1 mu-law, NVR2 A-law, 8 kHz).
  OpenCV (the video path) cannot deliver audio, so a camera being LISTENED to gets its
  own audio-only RTSP session (the NVR sends ~64 kbit/s, no video): one per camera,
  shared by every listener, never kept in the background. It uses a normal NVR slot:
  a background stream yields for it, a watched video stream is never stopped for it.
  If every slot of that NVR holds watched video (e.g. a grid page whose 6 tiles are
  all on one NVR), the speaker shows "Waiting for NVR capacity" -- the fullscreen
  view (one video) always leaves room.
* The NVRs choose the interleaved RTP channel themselves: an audio-only SETUP asking
  for 0-1 is answered "interleaved=2-3", so the client always reads the channel from
  the NVR's SETUP reply (reading the requested one was the cause of "Audio
  reconnecting..." with no sound).
* A silent microphone is a working stream: "Audio connected — no sound detected",
  never a reconnect. Only missing RTP packets reconnect (after the same 8 s as video);
  a shorter network stall shows "Audio interrupted — waiting for the NVR…" and resumes
  on the same session.
* Transport: WebSocket `/audio/<index>` (same access key as video; not limited by
  the browser's 6 connections per host), G.711 passed through and decoded in the
  browser, ~0.25 s jitter buffer, packets that would play late are dropped.
* Audio is independent of video: Standard <-> Original never touches it, an audio
  failure never touches video, each reconnects on its own.
* Detection is automatic (every video pre-flight reads the stream's SDP); a camera
  without an audio track gets a disabled speaker ("No audio available"). Settings page:
  Audio Automatic / On / Off per camera.
* `/api/status`: per camera `audio` {available, codec, state, listeners, active,
  lastPacketAgeMs, levelDb, peakDb, silent, reconnects, slotHeld, transitions, rtsp:
  {handshake (NVR address masked), nvrInterleaved, tcpBytes, framesByChannel,
  audioRtpPackets}}; audio sessions appear in the NVR slot table as `AUDIO` /
  `AUDIO_FULLSCREEN`. The browser console shows `[AUDIO UI]` lines (socket, first
  packet, a summary every 10 s).
* Details: `FINAL_CCTV_AUDIO_REPORT.txt`.

## Run it as a service (Linux, systemd)
Create `/etc/systemd/system/grav-cctv.service`:
```ini
[Unit]
Description=GRAV CCTV server
After=network-online.target

[Service]
WorkingDirectory=/opt/grav-cctv
ExecStart=/opt/grav-cctv/.venv/bin/python server.py
EnvironmentFile=/opt/grav-cctv/.env
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```
Then:
```
python3 -m venv /opt/grav-cctv/.venv
/opt/grav-cctv/.venv/bin/pip install -r /opt/grav-cctv/requirements.txt
systemctl enable --now grav-cctv
```

## Run it as a service (Windows)
Use Task Scheduler (trigger *At startup*, action `run.bat`), or NSSM:
```
nssm install GravCCTV "C:\path\to\grav-cctv\.venv\Scripts\python.exe" "C:\path\to\grav-cctv\server.py"
```

## Hosting notes
- **HTTPS:** to serve over `https://`, put it behind a reverse proxy (nginx/Caddy)
  that terminates TLS and forwards to `127.0.0.1:8000`. MJPEG works fine through a
  proxy; do not buffer the `/stream` response (nginx: `proxy_buffering off;`).
- **Bandwidth:** each viewer streams from the server's uplink (~0.3–0.5 Mbps per
  camera at the default size). The grid shows 6 per page to stay within the 6-per-NVR
  connection cap.
- **Security:** the `?key=` token is the only gate — keep the link private and use a
  strong `CCTV_TOKEN`. Consider IP-allowlisting at the proxy for extra safety.
