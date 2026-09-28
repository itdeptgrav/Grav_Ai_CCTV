"""Time rules for recorded playback -- the ONLY place that converts playback times.

Measured on both NVRs (NVR_PLAYBACK_CAPABILITY_REPORT.txt):
  * the playback URL's starttime / endtime are NVR LOCAL time (IST, UTC+5:30, no DST)
    written as YYYY_MM_DD_HH_MM_SS;
  * an absolute RTSP seek "Range: clock=YYYYMMDDTHHMMSSZ-" is UTC -- sending the local
    digits there made the NVR answer 500 and drop the connection;
  * the vendor search API (mediaFileFind) takes and returns NVR local time
    "YYYY-MM-DD HH:MM:SS".
Every datetime handled by the playback code is a NAIVE datetime in NVR local time.
The browser gets milliseconds since the Unix epoch (a real instant) plus the offset,
and shows NVR time whatever the browser's own time zone is.
The NVR time zone is configurable (CCTV_NVR_TZ_OFFSET_MIN, default 330 = IST); the
NVRs report TimeZone "Chennai" with daylight saving off.
"""
import os
import datetime

NVR_TZ_OFFSET_MIN = int(os.environ.get("CCTV_NVR_TZ_OFFSET_MIN", "330") or 330)
NVR_TZ = datetime.timezone(datetime.timedelta(minutes=NVR_TZ_OFFSET_MIN))
NVR_TZ_LABEL = os.environ.get("CCTV_NVR_TZ_LABEL", "IST")
UTC = datetime.timezone.utc
_EPOCH = datetime.datetime(1970, 1, 1)


def nvr_now():
    """Current time in NVR local time (naive), independent of the server's time zone."""
    return datetime.datetime.now(UTC).astimezone(NVR_TZ).replace(tzinfo=None, microsecond=0)


def parse_local(s):
    """'YYYY-MM-DDTHH:MM[:SS]' / 'YYYY-MM-DD HH:MM[:SS]' (NVR local) -> naive datetime.
    Raises ValueError for anything else."""
    s = str(s or "").strip().replace("T", " ")
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(s, f)
        except ValueError:
            pass
    raise ValueError(f"not a date/time: {s!r}")


def fmt_local(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def url_time(dt):
    """Playback URL starttime / endtime (NVR local)."""
    return dt.strftime("%Y_%m_%d_%H_%M_%S")


def to_utc(dt):
    """NVR local (naive) -> UTC (naive)."""
    return dt - datetime.timedelta(minutes=NVR_TZ_OFFSET_MIN)


def from_utc(dt):
    return dt + datetime.timedelta(minutes=NVR_TZ_OFFSET_MIN)


def rtsp_clock(dt):
    """Absolute RTSP seek value for 'Range: clock=<this>-' -- converted to UTC."""
    return to_utc(dt).strftime("%Y%m%dT%H%M%SZ")


def api_time(dt):
    """Vendor search API time (NVR local)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_api(s):
    return datetime.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")


def to_ms(dt):
    """NVR local (naive) -> ms since the Unix epoch (the real instant) for the browser."""
    return int(round((to_utc(dt) - _EPOCH).total_seconds() * 1000))


def from_ms(ms):
    return from_utc(_EPOCH + datetime.timedelta(milliseconds=int(ms)))


def span_text(hours):
    """Human text of a length in hours: '31 days', '36 hours'."""
    return f"{hours / 24:g} days" if hours >= 48 and hours % 24 == 0 else f"{hours:g} hours"


def validate_range(a, b, max_hours=None, now=None):
    """-> None if [a, b] is a usable playback window, else a message for the user.
    max_hours None: no length limit (the NVRs' oldest recordings bound the search)."""
    now = now or nvr_now()
    if b <= a:
        return "To time must be after From time."
    if max_hours is not None and (b - a).total_seconds() > max_hours * 3600:
        return f"The time range is too long: at most {span_text(max_hours)} per search."
    if a >= now:
        return "From time is in the future."
    return None


def plan_ranges(a, b, chunk_hours):
    """Split [a, b) into consecutive NVR-safe request windows of at most chunk_hours
    (in NVR local time; the last one ends exactly at b). A day boundary inside a
    window is fine -- the NVRs take local times across midnight."""
    out, step = [], datetime.timedelta(hours=chunk_hours)
    x = a
    while x < b:
        y = min(b, x + step)
        out.append((x, y))
        x = y
    return out
