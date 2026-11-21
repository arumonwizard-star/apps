"""Words the speech recognition keeps getting wrong, learned from the hand fixes of the telops.

presets/user_words.json  {"map": {"おかち麺粉": "おかちめんこ"}, "seen": {"おかち麺粉→おかちめんこ": 3}}
apply(text) puts the fixes into everything the program writes down from the recording (telops, a script made
from what was heard); learn(old, new) is called when a telop is changed by hand."""
import difflib
import json
import re
from pathlib import Path

PATH = Path(__file__).resolve().parent / "presets" / "user_words.json"
_cache = {"mtime": None, "map": {}}


def _load():
    try:
        m = PATH.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != m:
        try:
            _cache["map"] = json.loads(PATH.read_text(encoding="utf-8")).get("map", {})
        except Exception:   # noqa: BLE001
            _cache["map"] = {}
        _cache["mtime"] = m
    return _cache["map"]


def apply(text):
    if not text:
        return text
    for k in sorted(_load(), key=len, reverse=True):
        if k in text:
            text = text.replace(k, _load()[k])
    return text


def _clean(t):
    return re.sub(r"[\s\u0001]+", "", t or "")


def learn(old, new, script=""):
    """Remember what the hand fix changed.  Returns the pairs that were added."""
    a, b = _clean(old), _clean(new)
    if not a or not b or a == b or difflib.SequenceMatcher(None, a, b).ratio() < 0.4:
        return []
    pairs = []
    sm = difflib.SequenceMatcher(None, a, b)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace":
            continue
        x, y = a[i1:i2], b[j1:j2]
        if not (2 <= len(x) <= 12 and 2 <= len(y) <= 12) or x in (script or ""):
            continue
        c = min(3, i1, j1)                 # a little of the word in front, so "麺粉" is learned as "おかち麺粉"
        pairs.append((a[i1 - c:i2], b[j1 - c:j2]))
    if not pairs:
        return []
    try:
        d = json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else {}
    except Exception:   # noqa: BLE001
        d = {}
    mp, seen = d.setdefault("map", {}), d.setdefault("seen", {})
    for x, y in pairs:
        mp[x] = y
        seen[x + "→" + y] = seen.get(x + "→" + y, 0) + 1
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    _cache["mtime"] = None
    return pairs


def groups():
    """[{"to": canonical, "from": [variants...]}] from the map (the same canonical written in several ways)."""
    g = {}
    for k, v in _load().items():
        g.setdefault(v, []).append(k)
    return [{"to": v, "from": sorted(ks)} for v, ks in g.items()]


def save_groups(gs):
    try:
        d = json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else {}
    except Exception:   # noqa: BLE001
        d = {}
    mp = {}
    for g in gs or []:
        to = (g.get("to") or "").strip()
        for x in g.get("from") or []:
            x = (x or "").strip()
            if to and x and x != to:
                mp[x] = to
    d["map"] = mp
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    _cache["mtime"] = None
