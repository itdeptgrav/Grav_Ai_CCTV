"""NVR / camera configuration for the standalone CCTV server.

Host-ready: NVR credentials, hosts and ports are read from environment variables
(see .env.example), so no secrets are hardcoded. The values below are only
fallbacks. On a server that is NOT on the CCTV LAN, ACCESS_MODE=auto reaches the
NVRs through the public IP + forwarded ports.
"""
import os
import re
import socket
import subprocess
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

# Load .env (real credentials live there, never in this file). Optional dependency;
# if absent, values come from the OS environment. This runs on import so any script
# that imports nvr_config — not only server.py — picks up the .env values.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from netcheck import on_subnet, sanitize_url


def _env(name, default):
    v = os.getenv(name)
    return v if v not in (None, "") else default

def _envint(name, default):
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default

def _envfloat(name, default):
    try:
        return float(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


# ── network settings ──────────────────────────────────────────────────
SUBNET      = _env("CCTV_SUBNET_PREFIX", "192.168.1")
CCTV_SUBNET = _env("CCTV_SUBNET_CIDR", "192.168.1.0/24")
RTSP_PORT   = _envint("CCTV_RTSP_PORT", 554)
NETWORK_CHECK_INTERVAL = 5.0
# TCP-connect timeout for the reachability check. 2 s (not 1 s) tolerates the
# latency/jitter of reaching the NVRs over the public internet, so healthy NVRs
# are not falsely flagged down. Combined with the monitor's failure hysteresis.
NETWORK_TIMEOUT        = _envfloat("CCTV_NETWORK_TIMEOUT", 2.0)

# The site's static public IP (used when the server is off the CCTV LAN).
# Real value comes from CCTV_PUBLIC_IP in .env; empty here on purpose.
PUBLIC_IP   = _env("CCTV_PUBLIC_IP", "")
# "auto" -> LAN when on-site, public IP when remote (default; correct for a
# hosted server, which is not on the CCTV LAN). "lan" / "remote" force it.
ACCESS_MODE = _env("CCTV_ACCESS_MODE", "auto")

# Per-NVR: private ip/port for on-LAN, public_port that the router forwards to
# that NVR's 554, credentials, and MAC (for on-LAN DHCP discovery). Credentials,
# MACs and the public IP are NOT hardcoded -- they come from .env (see
# .env.example). The defaults below are placeholders so no real secret is stored
# in source; the app reads the real values from the environment at runtime.
NVRS = {
    "nvr1": {
        "mac": _env("NVR1_MAC", ""),
        "ip": _env("NVR1_HOST", "192.168.1.48"),
        "port": _envint("NVR1_PORT", 554),
        "public_port": _envint("NVR1_PUBLIC_PORT", 10554),
        "user": _env("NVR1_USERNAME", ""),
        "pass": _env("NVR1_PASSWORD", ""),
    },
    "nvr2": {
        "mac": _env("NVR2_MAC", ""),
        "ip": _env("NVR2_HOST", "192.168.1.44"),
        "port": _envint("NVR2_PORT", 554),
        "public_port": _envint("NVR2_PUBLIC_PORT", 20554),
        "user": _env("NVR2_USERNAME", ""),
        "pass": _env("NVR2_PASSWORD", ""),
    },
}

CAMERAS = [
    {"name": "Floor 19 - Storage",   "nvr": "nvr2", "channel": 1},
    {"name": "NVR2 Cam 2",           "nvr": "nvr2", "channel": 2},
    {"name": "NVR2 Cam 3",           "nvr": "nvr2", "channel": 3},
    {"name": "Floor 9 - Cabin",      "nvr": "nvr2", "channel": 4},
    {"name": "NVR2 Cam 5",           "nvr": "nvr2", "channel": 5},
    {"name": "NVR2 Cam 6",           "nvr": "nvr2", "channel": 6},
    {"name": "NVR2 Cam 7",           "nvr": "nvr2", "channel": 7},
    {"name": "Floor 10 - Reception", "nvr": "nvr2", "channel": 8},
    {"name": "NVR2 Cam 9",           "nvr": "nvr2", "channel": 9},
    {"name": "NVR2 Cam 10",          "nvr": "nvr2", "channel": 10},
    {"name": "NVR2 Cam 11",          "nvr": "nvr2", "channel": 11},
    {"name": "NVR2 Cam 12",          "nvr": "nvr2", "channel": 12},
    {"name": "NVR2 Cam 13",          "nvr": "nvr2", "channel": 13},
    {"name": "NVR1 Cam 3",           "nvr": "nvr1", "channel": 3},
    {"name": "NVR1 Cam 4",           "nvr": "nvr1", "channel": 4},
    {"name": "NVR1 Cam 5",           "nvr": "nvr1", "channel": 5},
    {"name": "NVR1 Cam 6",           "nvr": "nvr1", "channel": 6},
    {"name": "NVR1 Cam 7",           "nvr": "nvr1", "channel": 7},
    {"name": "NVR1 Cam 8",           "nvr": "nvr1", "channel": 8},
    {"name": "NVR1 Cam 9",           "nvr": "nvr1", "channel": 9},
    {"name": "NVR1 Cam 10",          "nvr": "nvr1", "channel": 10},
    {"name": "NVR1 Cam 11",          "nvr": "nvr1", "channel": 11},
    {"name": "NVR1 Cam 12",          "nvr": "nvr1", "channel": 12},
    {"name": "NVR1 Cam 13",          "nvr": "nvr1", "channel": 13},
    {"name": "NVR1 Cam 14",          "nvr": "nvr1", "channel": 14},
]


def _port_open(ip, port=554, timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _arp_table():
    out = subprocess.run(["arp", "-a"], capture_output=True, text=True).stdout
    table = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-:]{17})", line)
        if m:
            table[m.group(2).lower().replace(":", "-")] = m.group(1)
    return table


def _scan_subnets():
    nets = []
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = info[4][0]
        if ip.startswith(("127.", "169.254.", "100.")):
            continue
        prefix = ip.rsplit(".", 1)[0]
        if prefix not in nets:
            nets.append(prefix)
    if SUBNET not in nets:
        nets.append(SUBNET)
    return nets


def remote_mode():
    if ACCESS_MODE == "remote":
        return True
    if ACCESS_MODE == "lan":
        return False
    return not on_subnet(CCTV_SUBNET)      # "auto"


def resolve_nvr_ips(verbose=True):
    """On the CCTV LAN, locate each NVR by MAC (DHCP-safe). Off the LAN (a hosted
    server), use the public IP + forwarded ports and just report reachability."""
    if remote_mode():
        if verbose:
            print(f"Remote mode: using public IP {PUBLIC_IP} + forwarded ports.")
        for key in NVRS:
            host, port = endpoint(key)
            ok = _port_open(host, port)
            if verbose:
                print(f"  {key}: {host}:{port} -> {'reachable' if ok else 'NOT reachable'}")
        return

    scanned = False
    for key, n in NVRS.items():
        if _port_open(n["ip"]):
            if verbose:
                print(f"{key}: {n['ip']} (unchanged)")
            continue
        if not scanned:
            nets = _scan_subnets()
            if verbose:
                print(f"scanning {', '.join(net + '.x' for net in nets)} ...")
            targets = [f"{net}.{i}" for net in nets for i in range(1, 255)]
            with ThreadPoolExecutor(max_workers=128) as pool:
                pool.map(_port_open, targets)
            scanned = True
        found = _arp_table().get(n["mac"].lower())
        if found:
            n["ip"] = found
            if verbose:
                print(f"{key}: found at {found}")
        elif verbose:
            print(f"{key}: NOT FOUND on the local network")


def endpoint(nvr_key):
    """(host, port) to reach this NVR now — private on LAN, public when remote."""
    n = NVRS[nvr_key]
    if remote_mode():
        return PUBLIC_IP, n.get("public_port", n["port"])
    return n["ip"], n["port"]


def make_url(nvr_key, ch, subtype=1):
    n = NVRS[nvr_key]
    host, port = endpoint(nvr_key)
    user = quote(str(n["user"]), safe="")
    pw = quote(str(n["pass"]), safe="")
    return (f"rtsp://{user}:{pw}@{host}:{port}"
            f"/cam/realmonitor?channel={ch}&subtype={subtype}")


def safe_url(nvr_key, ch, subtype=1):
    return sanitize_url(make_url(nvr_key, ch, subtype))
