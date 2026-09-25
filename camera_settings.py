"""User-editable camera display settings (name + order), persisted server-side.

Only PRESENTATION metadata lives here. A camera's technical identity -- NVR key,
channel, RTSP path, credentials -- comes from nvr_config.CAMERAS and is never
changed. A camera is identified by a STABLE key "<nvr>:<channel>" (e.g. "nvr1:8"),
never by its position in a list, so settings survive changes to CAMERAS order.

File (default data/camera-settings.json, override with CCTV_SETTINGS_FILE):

    {"version": 1, "revision": 3, "updatedAt": "2026-09-25T12:00:00+05:30",
     "cameras": {"nvr1:8": {"displayName": "HR Office", "displayOrder": 1}}}

Only customised cameras are stored. No custom name -> the technical name is shown.

ORDER MODEL. A custom displayOrder is a POSITION (1..N). A camera with a custom
order sits exactly at that position; every other camera fills the remaining
positions in its original order. So "HR Office = 1" works without renumbering all
the others, and two cameras can never claim the same position (validated on save;
a hand-edited file with duplicates is tolerated deterministically and reported).

WRITES are all-or-nothing: validated first, then written to a temp file, fsync'ed
and atomically renamed over the real file (os.replace), under a lock. A crash
mid-write leaves the previous file intact. An unreadable file is preserved
(renamed *.corrupt-<time>) and defaults are used, never silently overwritten.
"""
import os
import re
import json
import time
import datetime
import threading

SCHEMA_VERSION = 1
NAME_MAX = 60
# control characters and angle brackets are rejected in names (defence in depth:
# the UI renders names as text anyway)
_BAD_NAME = re.compile(r"[\x00-\x1f\x7f<>]")


def camera_key(cam):
    return f"{cam['nvr']}:{cam['channel']}"


def _norm_name(v):
    """Trim and collapse internal whitespace (incl. tabs/newlines)."""
    return " ".join(v.split())


class CameraSettings:
    def __init__(self, path, cameras, log=None):
        self.path = os.path.abspath(path)
        self.cameras = cameras
        self.keys = [camera_key(c) for c in cameras]
        self._pos = {k: i for i, k in enumerate(self.keys)}
        self._lock = threading.RLock()
        self._log = log or (lambda msg: None)
        self._data = {}            # key -> {"displayName": str, "displayOrder": int}
        self.revision = 0
        self.updated_at = None
        self.warnings = []
        self.load()

    # ── persistence ────────────────────────────────────────────────────────
    def load(self):
        with self._lock:
            self._data, self.revision, self.updated_at = {}, 0, None
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        doc = json.load(f)
                    if not isinstance(doc, dict) or not isinstance(doc.get("cameras"), dict):
                        raise ValueError("missing 'cameras' object")
                    data = {}
                    for k, v in doc["cameras"].items():
                        if not isinstance(k, str) or not isinstance(v, dict):
                            continue
                        e = {}
                        n = v.get("displayName")
                        if isinstance(n, str):
                            n = _norm_name(n)
                            if n and len(n) <= NAME_MAX and not _BAD_NAME.search(n):
                                e["displayName"] = n
                        o = v.get("displayOrder")
                        if isinstance(o, int) and not isinstance(o, bool) and o >= 1:
                            e["displayOrder"] = o
                        if e:
                            data[k] = e
                    rev = doc.get("revision", 0)
                    self._data = data
                    self.revision = rev if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0 else 0
                    self.updated_at = doc.get("updatedAt") if isinstance(doc.get("updatedAt"), str) else None
                except (OSError, ValueError, TypeError) as e:
                    bad = f"{self.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
                    try:
                        os.replace(self.path, bad)
                    except OSError:
                        bad = "(could not be renamed)"
                    self._log(f"[SETTINGS] {self.path} is unreadable ({type(e).__name__}: {e}); "
                              f"kept as {bad}; using default names and order")
            self._check()

    def _write_atomic(self, doc):
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        tmp = f"{self.path}.tmp-{os.getpid()}-{threading.get_ident()}"
        payload = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, self.path)          # atomic on the same volume
                    break
                except PermissionError:                 # Windows: file briefly held open
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ── derived views ──────────────────────────────────────────────────────
    def _custom_orders(self, data):
        """{position: camera index} for valid custom orders; for a duplicate the
        camera that comes first in CAMERAS wins (the other falls back to auto)."""
        n, out = len(self.keys), {}
        for i, k in enumerate(self.keys):
            o = data.get(k, {}).get("displayOrder")
            if o is not None and 1 <= o <= n and o not in out:
                out[o] = i
        return out

    def _ordered(self, data):
        custom = self._custom_orders(data)
        placed = set(custom.values())
        autos = iter(i for i in range(len(self.keys)) if i not in placed)
        return [custom[p] if p in custom else next(autos) for p in range(1, len(self.keys) + 1)]

    def ordered_indices(self):
        """Camera indices (positions in CAMERAS) in display order."""
        with self._lock:
            return self._ordered(self._data)

    def display_name(self, index):
        with self._lock:
            return self._data.get(self.keys[index], {}).get("displayName") or self.cameras[index]["name"]

    def _check(self):
        """Report (never crash on) problems in a hand-edited file."""
        w, n, seen = [], len(self.keys), {}
        for k in sorted(self._data):
            if k not in self._pos:
                w.append(f"'{k}' is not a configured camera (its settings are kept but ignored)")
        for i, k in enumerate(self.keys):
            o = self._data.get(k, {}).get("displayOrder")
            if o is None:
                continue
            if o > n:
                w.append(f"{k}: order {o} is beyond the {n} cameras (treated as automatic)")
            elif o in seen:
                w.append(f"{k}: order {o} is also used by {seen[o]} (treated as automatic)")
            else:
                seen[o] = k
        self.warnings = w
        for m in w:
            self._log(f"[SETTINGS] warning: {m}")

    def snapshot(self):
        """Safe, credential-free view for the API (cameras in technical order)."""
        with self._lock:
            order = self._ordered(self._data)
            pos = {idx: p + 1 for p, idx in enumerate(order)}
            custom = {i: o for o, i in self._custom_orders(self._data).items()}
            cams = []
            for i, cam in enumerate(self.cameras):
                e = self._data.get(self.keys[i], {})
                cams.append({
                    "key": self.keys[i], "index": i,
                    "technicalName": cam["name"], "nvr": cam["nvr"], "nvrLabel": cam["nvr"].upper(),
                    "channel": cam["channel"],
                    "displayName": e.get("displayName") or cam["name"],
                    "customName": e.get("displayName"),
                    "displayOrder": pos[i], "customOrder": custom.get(i), "defaultOrder": i + 1,
                })
            return {"version": SCHEMA_VERSION, "revision": self.revision, "updatedAt": self.updated_at,
                    "limits": {"nameMax": NAME_MAX, "orderMin": 1, "orderMax": len(self.keys)},
                    "warnings": list(self.warnings), "cameras": cams}

    # ── updates ────────────────────────────────────────────────────────────
    def update(self, changes, base_revision=None):
        """Apply {key: {"displayName": str|None, "displayOrder": int|None}}.
        A missing field is left unchanged; None resets it to the default. All
        changes are validated together and written atomically, or nothing is.
        -> (http_status, payload dict)."""
        def err(key, field, msg):
            return {"key": key, "field": field, "message": msg}
        if not isinstance(changes, dict) or not changes:
            return 400, {"ok": False, "errors": [err(None, None, "No changes were sent.")]}
        with self._lock:
            if base_revision is not None and base_revision != self.revision:
                return 409, {"ok": False, "revision": self.revision, "errors": [err(
                    None, None, "Camera settings were changed elsewhere since this page was loaded. "
                                "Reload the page to see the current settings.")]}
            n = len(self.keys)
            new = {k: dict(v) for k, v in self._data.items()}
            errors, changed = [], []
            for k, ch in changes.items():
                if k not in self._pos:
                    errors.append(err(k, None, f"Unknown camera '{k}'."))
                    continue
                if not isinstance(ch, dict):
                    errors.append(err(k, None, "Invalid change."))
                    continue
                extra = set(ch) - {"displayName", "displayOrder"}
                if extra:
                    errors.append(err(k, None, f"Unknown field(s): {', '.join(sorted(extra))}."))
                    continue
                tech = self.cameras[self._pos[k]]["name"]
                e = new.get(k, {})
                old_name, old_order = e.get("displayName"), e.get("displayOrder")
                if "displayName" in ch:
                    v = ch["displayName"]
                    if v is None:
                        e.pop("displayName", None)
                    elif not isinstance(v, str):
                        errors.append(err(k, "displayName", "Display name must be text."))
                    else:
                        v = _norm_name(v)
                        if len(v) > NAME_MAX:
                            errors.append(err(k, "displayName", f"Display name is too long (max {NAME_MAX} characters)."))
                        elif _BAD_NAME.search(v):
                            errors.append(err(k, "displayName", "Display name may not contain < > or control characters."))
                        elif not v or v == tech:
                            e.pop("displayName", None)       # empty / same as technical = default
                        else:
                            e["displayName"] = v
                if "displayOrder" in ch:
                    v = ch["displayOrder"]
                    if v is None:
                        e.pop("displayOrder", None)
                    elif isinstance(v, bool) or not isinstance(v, int):
                        errors.append(err(k, "displayOrder", "Display order must be a whole number."))
                    elif not 1 <= v <= n:
                        errors.append(err(k, "displayOrder", f"Display order must be between 1 and {n}."))
                    else:
                        e["displayOrder"] = v
                if e:
                    new[k] = e
                else:
                    new.pop(k, None)
                if e.get("displayName") != old_name:
                    changed.append(f"{k} name {old_name or tech!r} -> {e.get('displayName') or tech!r}")
                if e.get("displayOrder") != old_order:
                    changed.append(f"{k} order {old_order or 'auto'} -> {e.get('displayOrder') or 'auto'}")
            if not errors:
                claims = {}
                for k in self.keys:                          # order must be unique
                    o = new.get(k, {}).get("displayOrder")
                    if o is not None and 1 <= o <= n:
                        claims.setdefault(o, []).append(k)
                for o, ks in claims.items():
                    if len(ks) < 2:
                        continue
                    for k in ks:
                        if k in changes or not any(x in changes for x in ks):
                            others = ", ".join(f"'{self._name(new, x)}'" for x in ks if x != k)
                            errors.append(err(k, "displayOrder",
                                              f"Order {o} is already used by {others}. "
                                              f"Each order number can be used by one camera only."))
            if errors:
                return 400, {"ok": False, "revision": self.revision, "errors": errors}
            if not changed:
                return 200, {"ok": True, "changed": [], **self.snapshot()}
            doc = {"version": SCHEMA_VERSION, "revision": self.revision + 1,
                   "updatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "cameras": dict(sorted(new.items()))}
            self._write_atomic(doc)                          # raises -> nothing changed in memory
            self._data, self.revision, self.updated_at = new, doc["revision"], doc["updatedAt"]
            self._check()
            return 200, {"ok": True, "changed": changed, **self.snapshot()}

    def _name(self, data, key):
        i = self._pos[key]
        return data.get(key, {}).get("displayName") or self.cameras[i]["name"]
