"""SUPERVISED NVR capacity + direct-camera probe.  PREPARED -- NOT RUN AUTOMATICALLY.

Read SUPERVISED_NVR_CAPACITY_TEST.txt first. This tool deliberately opens more live
RTSP streams than the CCTV server uses, so it must only be run by a person who is
watching the NVR, at a quiet time, with every CCTV server stopped.

  python nvr_capacity_probe.py capacity --nvr nvr1 --max 12 --i-am-supervising
  python nvr_capacity_probe.py direct --cameras my_cameras.json --i-am-supervising

capacity : opens live sub-stream sessions ONE AT A TIME (DESCRIBE/SETUP/PLAY, RTP over
           TCP, nothing decoded), keeps all of them flowing, and after each new one
           checks for HOLD seconds that EVERY open session still receives video. Stops
           at the FIRST refusal (4xx/5xx, e.g. 453 Not Enough Bandwidth), timeout,
           stalled session, or --max -- whichever comes first -- then TEARDOWNs all.
           Result = the highest session count at which everything kept flowing.
direct   : for cameras YOU list (IP + optional credentials, from the NVR's own
           "Remote Device" page), checks: TCP 554 open, an RTSP server answering
           (unauthenticated OPTIONS/DESCRIBE), and -- only if you gave credentials --
           ONE authenticated DESCRIBE each for the main stream (subtype=0) and the
           sub-stream (subtype=1). A 401 is never retried (no account lockout).
           Credentials stored in the NVR are never read.

Safety rails: refuses to start without --i-am-supervising; refuses if any server in
RELAY_GUARDS (see relay_bench.py) has viewers/busy slots; 10 s countdown (Ctrl+C
cancels); stops on the first credential rejection; hard ceiling of 24 sessions;
prints no passwords.
"""
import os
import re
import sys
import json
import time
import socket
import argparse
import threading

import diag_cctv as D
import nvr_config as C

KNOWN_DEAD = {("nvr2", 7), ("nvr2", 12), ("nvr2", 13), ("nvr1", 10)}
HARD_CEILING = 24
MIN_KBPS = 30          # a session below this during a hold window counts as stalled


class AuthRejected(Exception):
    pass


class Session(threading.Thread):
    """One live RTSP session that keeps draining RTP (no decoding)."""

    def __init__(self, host, port, user, pw, url):
        super().__init__(daemon=True)
        self.url = url
        self.cli = D.Rtsp(host, port, user, pw, timeout=8.0)
        self.bytes = 0
        self.stop = False
        self.error = None

    def open(self):
        cli, url = self.cli, self.url
        cli.connect()
        code, h, body = cli.request("DESCRIBE", url, {"Accept": "application/sdp"})
        if code == 401:
            cli.auth = D._parse_challenge(h.get("www-authenticate", ""))
            code, h, body = cli.request("DESCRIBE", url, {"Accept": "application/sdp"})
            if code == 401:
                raise AuthRejected("DESCRIBE 401 with the configured credentials")
        if code != 200:
            return f"DESCRIBE -> {code}"
        base = h.get("content-base", url)
        control, in_video = None, False
        for ln in body.splitlines():
            ln = ln.strip()
            if ln.startswith("m="):
                in_video = ln.startswith("m=video")
            elif in_video and ln.startswith("a=control:"):
                control = ln[len("a=control:"):]
        if not control:
            return "SDP has no video track"
        if not control.startswith("rtsp://"):
            control = base.rstrip("/") + "/" + control.lstrip("/")
        code, h, _ = cli.request("SETUP", control, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        if code != 200:
            return f"SETUP -> {code}"
        cli.session = h.get("session", "").split(";")[0].strip()
        code, _, _ = cli.request("PLAY", base, {"Range": "npt=0.000-"})
        if code != 200:
            return f"PLAY -> {code}"
        return None

    def run(self):
        self.cli.sock.settimeout(1.0)
        while not self.stop:
            try:
                _, pkt = self.cli.read_interleaved()
                self.bytes += len(pkt)
            except socket.timeout:
                continue
            except Exception as e:
                self.error = type(e).__name__
                return

    def close(self):
        self.stop = True
        try:
            if self.cli.sock and self.cli.session:
                self.cli.sock.settimeout(1.0)
                self.cli.request("TEARDOWN", self.url)
        except Exception:
            pass
        self.cli.close()


def refuse_if_busy():
    try:
        import relay_bench
        why = relay_bench.guard_check()
    except Exception as e:
        why = f"guard check failed ({type(e).__name__})"
    if why:
        sys.exit(f"REFUSED: {why}. Stop every CCTV server first (see SUPERVISED_NVR_CAPACITY_TEST.txt).")


def countdown(what):
    print(f"\n*** SUPERVISED TEST: {what}\n*** Watch the NVR. Ctrl+C now to cancel.")
    for i in range(10, 0, -1):
        print(f"    starting in {i} s", end="\r", flush=True)
        time.sleep(1)
    print()


def capacity(nvr, max_sessions, hold, allow_repeat, subtype):
    n = C.NVRS[nvr]
    host, port = C.endpoint(nvr)
    chans = [c["channel"] for c in C.CAMERAS if c["nvr"] == nvr and (nvr, c["channel"]) not in KNOWN_DEAD]
    order = list(chans) + (list(chans) if allow_repeat else [])
    limit = min(max_sessions, len(order), HARD_CEILING)
    countdown(f"{nvr.upper()} capacity, up to {limit} live sub-stream sessions, {hold:.0f} s hold each")
    sessions, result = [], {"nvr": nvr, "steps": [], "maxGood": 0, "stoppedBecause": None}
    try:
        for k, ch in enumerate(order[:limit], 1):
            url = f"rtsp://{host}:{port}/cam/realmonitor?channel={ch}&subtype={subtype}"
            s = Session(host, port, n["user"], n["pass"], url)
            try:
                err = s.open()
            except AuthRejected as e:
                result["stoppedBecause"] = f"session {k}: {e} (stopped, not retried)"
                s.close()
                break
            except (OSError, D.RtspError) as e:
                err = f"{type(e).__name__}"
            if err:
                result["stoppedBecause"] = f"session {k} (ch{ch}) refused: {err}"
                s.close()
                break
            s.start()
            sessions.append(s)
            b0 = [x.bytes for x in sessions]
            time.sleep(hold)
            kbps = [round((x.bytes - b) * 8 / hold / 1000) for x, b in zip(sessions, b0)]
            stalled = [i + 1 for i, (x, r) in enumerate(zip(sessions, kbps)) if r < MIN_KBPS or x.error]
            result["steps"].append({"sessions": k, "channel": ch, "kbps": kbps, "stalled": stalled})
            print(f"  {k:>2} sessions open (added ch{ch}): kbps per session {kbps}"
                  + (f"  STALLED: {stalled}" if stalled else "  all flowing"))
            if stalled:
                result["stoppedBecause"] = f"with {k} sessions, session(s) {stalled} stopped receiving video"
                break
            result["maxGood"] = k
        else:
            result["stoppedBecause"] = f"reached the limit ({limit}) with everything flowing"
    except KeyboardInterrupt:
        result["stoppedBecause"] = "cancelled by the operator"
    finally:
        for x in sessions:
            x.close()
        print(f"  all {len(sessions)} sessions torn down")
    print(f"\n{nvr.upper()}: highest session count with every stream flowing = {result['maxGood']}"
          f"  ({result['stoppedBecause']})")
    return result


def direct(path, subtype_main=0, subtype_sub=1):
    cams = json.load(open(path, encoding="utf-8"))
    countdown(f"direct-camera check of {len(cams)} cameras (one DESCRIBE per stream, no retries)")
    out = []
    for cam in cams:
        ip, port = cam["ip"], int(cam.get("port", 554))
        r = {"name": cam.get("name", ip), "ip": ip, "port": port}
        r["tcp"] = _tcp(ip, port)
        if r["tcp"]:
            r["rtspServer"] = _unauth(ip, port)
            if cam.get("user"):
                for label, st in (("main", subtype_main), ("sub", subtype_sub)):
                    r[label] = _describe(ip, port, cam["user"], cam.get("password", ""), st)
                    if r[label].get("result") == "AUTH_FAIL":
                        break                         # never retry a rejected login
        out.append(r)
        print(f"  {r['name'][:24]:24} {ip}:{port}  tcp={'open' if r['tcp'] else 'closed'}  "
              f"rtsp={r.get('rtspServer', '-')}  main={r.get('main', {}).get('result', '-')} "
              f"{r.get('main', {}).get('codec', '')}  sub={r.get('sub', {}).get('result', '-')} "
              f"{r.get('sub', {}).get('codec', '')}")
    return out


def _tcp(ip, port):
    try:
        socket.create_connection((ip, port), timeout=2).close()
        return True
    except OSError:
        return False


def _unauth(ip, port):
    cli = D.Rtsp(ip, port, "", "", timeout=4.0)
    try:
        cli.connect()
        code, h, _ = cli.request("OPTIONS", f"rtsp://{ip}:{port}/")
        realm = (D._parse_challenge(h.get("www-authenticate", "")) or ("", {}))[1].get("realm")
        return f"answers ({code}{', realm ' + realm if realm else ''})"
    except Exception as e:
        return f"no RTSP answer ({type(e).__name__})"
    finally:
        cli.close()


def _describe(ip, port, user, pw, subtype):
    url = f"rtsp://{ip}:{port}/cam/realmonitor?channel=1&subtype={subtype}"
    cli = D.Rtsp(ip, port, user, pw, timeout=6.0)
    try:
        cli.connect()
        code, h, body = cli.request("DESCRIBE", url, {"Accept": "application/sdp"})
        if code == 401:
            cli.auth = D._parse_challenge(h.get("www-authenticate", ""))
            code, h, body = cli.request("DESCRIBE", url, {"Accept": "application/sdp"})
            if code == 401:
                return {"result": "AUTH_FAIL"}
        if code != 200:
            return {"result": f"DESCRIBE {code}"}
        codec = next((ln.split()[1].split("/")[0] for ln in body.splitlines()
                      if ln.startswith("a=rtpmap:") and ("H26" in ln or "HEVC" in ln)), "?")
        return {"result": "OK", "codec": codec}
    except Exception as e:
        return {"result": type(e).__name__}
    finally:
        cli.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["capacity", "direct"])
    ap.add_argument("--nvr", choices=list(C.NVRS))
    ap.add_argument("--max", type=int, default=12)
    ap.add_argument("--hold", type=float, default=10.0)
    ap.add_argument("--allow-repeat", action="store_true", help="a 2nd session per channel after all channels")
    ap.add_argument("--subtype", type=int, default=1, help="1 = sub-stream (what the relay uses), 0 = main")
    ap.add_argument("--cameras", help="JSON list for 'direct' (keep it OUT of the repository)")
    ap.add_argument("--out")
    ap.add_argument("--i-am-supervising", action="store_true")
    a = ap.parse_args()
    if not a.i_am_supervising:
        sys.exit("REFUSED: supervised test only. Read SUPERVISED_NVR_CAPACITY_TEST.txt, then add --i-am-supervising.")
    refuse_if_busy()
    if a.mode == "capacity":
        if not a.nvr:
            sys.exit("--nvr is required")
        res = capacity(a.nvr, a.max, a.hold, a.allow_repeat, a.subtype)
    else:
        if not a.cameras:
            sys.exit("--cameras is required")
        res = direct(a.cameras)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
