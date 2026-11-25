"""Read ids of media already in the Final Cut library so the exported XML points at the real items
(FCP then reuses them instead of creating copies)."""
import re, sqlite3, shutil, tempfile
from pathlib import Path

LIB = Path("/Volumes/編集用/外出CH/外出CH.fcpbundle")
EVENT = "CH Cut"
EVENT_UID_DEFAULT = "26C8F298-A818-4A19-B6F5-E7D4F1779454"


def _str_at(d, i):
    """Decode an NSKeyedArchiver string: 0x5n = n bytes UTF-8, 0x6n = n UTF-16BE chars (0x_f 0x10 len = longer)."""
    b = d[i]
    hi, lo = b & 0xf0, b & 0x0f
    if hi not in (0x50, 0x60):
        return None
    if lo == 0x0f:
        if d[i + 1] != 0x10:
            return None
        n, st = d[i + 2], i + 3
    else:
        n, st = lo, i + 1
    if hi == 0x50:
        return d[st:st + n].decode("utf-8", "replace")
    return d[st:st + 2 * n].decode("utf-16-be", "replace")


def media_ids(lib=LIB, event=EVENT):
    """{displayName: uid} of the assets in the event."""
    out = {}
    p = Path(lib) / event / "CurrentVersion.fcpevent"
    if not p.exists():
        return out
    d = p.read_bytes()
    for m in re.finditer(rb"displayName_\x10 ([0-9A-F]{32})", d):
        s = _str_at(d, m.end())
        if s:
            out[s] = m.group(1).decode()
    return out


def event_uid(lib=LIB, event=EVENT):
    return EVENT_UID_DEFAULT
