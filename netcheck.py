"""Network health for the CCTV viewer.

One NetworkMonitor checks each unique NVR on a timer; camera threads read its
result instead of each probing. `sanitize_url` strips user:pass before logging.

Cross-platform: the ping helper works on both Windows and Linux servers.
"""
import re
import socket
import ipaddress
import threading
import subprocess
import platform
import time

_IS_WINDOWS = platform.system().lower().startswith("win")

# ── credential-safe logging ───────────────────────────────────────────
_CRED = re.compile(r"://[^/@\s]*@")     # matches "://user:pass@"


def sanitize_url(text):
    """Remove user:pass from any rtsp://user:pass@host in a string."""
    return _CRED.sub("://", str(text))


# ── low-level probes ──────────────────────────────────────────────────
def check_port(ip, port, timeout=1.0):
    """True if a TCP connection to ip:port succeeds within timeout."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def check_host(ip, timeout=1.0):
    """True if the host answers a single ICMP ping. A hint, not proof — many
    hosts/firewalls drop ping. Works on Windows and Linux."""
    try:
        if _IS_WINDOWS:
            cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", str(max(1, int(round(timeout)))), ip]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        return "TTL=" in (r.stdout or "").upper()
    except Exception:
        return False


def _src_ip_for(dest):
    """The local source IP the OS would use to reach `dest` (no packet sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def local_ipv4s():
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def on_subnet(cidr):
    """True if any local interface address is inside `cidr`."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    for ip in local_ipv4s():
        try:
            if ipaddress.ip_address(ip) in net:
                return True
        except ValueError:
            continue
    return False


def has_route_to(ip):
    """True if a specific (non-default) route toward `ip` exists — VPN/LAN, not
    'everything via the internet gateway'."""
    src_target = _src_ip_for(ip)
    src_public = _src_ip_for("8.8.8.8")
    if src_target is None:
        return False
    if src_public is None:
        return True
    return src_target != src_public


# ── per-NVR status ────────────────────────────────────────────────────
S_LIVE_OK      = "NVR REACHABLE"
S_RTSP_CLOSED  = "RTSP UNREACHABLE"
S_NVR_DOWN     = "NVR UNREACHABLE"
S_NET_DOWN     = "CCTV NETWORK UNREACHABLE"


class NvrHealth:
    __slots__ = ("port_open", "host_up", "route", "on_lan", "label", "checked")

    def __init__(self):
        self.port_open = False
        self.host_up   = False
        self.route     = False
        self.on_lan    = False
        self.label     = "CHECKING..."
        self.checked   = 0.0

    @property
    def reachable(self):
        return self.port_open

    @property
    def vpn_hint(self):
        if self.on_lan:
            return "on CCTV LAN"
        if self.port_open or self.route:
            return "route OK"
        return "no route"


class NetworkMonitor:
    """Checks each unique NVR every `interval` seconds in one background thread."""

    def __init__(self, nvrs, cctv_subnet, interval=5.0, timeout=1.0, endpoint_fn=None):
        self._nvrs        = nvrs
        self._subnet      = cctv_subnet
        self._interval    = interval
        self._timeout     = timeout
        self._endpoint    = endpoint_fn or (lambda k: (nvrs[k]["ip"], nvrs[k].get("port", 554)))
        self._health      = {k: NvrHealth() for k in nvrs}
        self._lock        = threading.Lock()
        self._running     = False

    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._running = False

    def get(self, nvr_key):
        with self._lock:
            return self._health.get(nvr_key)

    def _classify(self, h):
        if h.port_open:
            return S_LIVE_OK
        if h.host_up:
            return S_RTSP_CLOSED
        if h.on_lan or h.route:
            return S_NVR_DOWN
        return S_NET_DOWN

    def _check_once(self):
        on_lan = on_subnet(self._subnet)
        for key in self._nvrs:
            ip, port = self._endpoint(key)
            h = NvrHealth()
            h.on_lan    = on_lan
            h.port_open = check_port(ip, port, self._timeout)
            if not h.port_open:
                h.route   = has_route_to(ip)
                h.host_up = check_host(ip, self._timeout)
            else:
                h.route = True
            h.label   = self._classify(h)
            h.checked = time.time()
            with self._lock:
                self._health[key] = h

    def _run(self):
        while self._running:
            try:
                self._check_once()
            except Exception:
                pass
            time.sleep(self._interval)

    def summary(self):
        lines = []
        with self._lock:
            for key, h in self._health.items():
                host, port = self._endpoint(key)
                lines.append(f"{key} {host}:{port}: {h.label} ({h.vpn_hint})")
        return lines
