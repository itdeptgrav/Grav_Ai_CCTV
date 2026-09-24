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
| `server.py` | The web server (grid page + `/stream`, `/snapshot`, `/api/cameras`) |
| `nvr_config.py` | Camera list + NVR endpoints (from env) |
| `netcheck.py` | Reachability probes (cross-platform) |
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
