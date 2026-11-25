# -*- coding: utf-8 -*-
"""Learn from what the user changes by hand (timeline, text list): every save is compared with the automatic
result; the start-time changes are summed up per kind of telop head and, once they agree, applied to the next analysis."""
import json
import re
import statistics
from pathlib import Path

BIAS_FILE = Path(__file__).resolve().parent / "presets" / "timing_bias.json"
MIN_N, MIN_SHIFT, AGREE, CLAMP = 6, 0.03, 0.7, 0.3


def _n(t):
    return re.sub(r"[\s\n、。！？!?…]", "", t or "")


def category(pieces, k):
    """pieces: live speech pieces of one block sorted by start; k: index."""
    if k == 0:
        return "block_first"
    return "speaker_change" if pieces[k - 1].get("speaker") != pieces[k].get("speaker") else "same_speaker"


CUT_FILE = Path(__file__).resolve().parent / "presets" / "cut_prefs.json"


def _bounds(pieces):
    """The text of the pieces joined, and the offsets (in the joined text) where one piece ends and the next begins, with the speakers."""
    s, offs = "", []
    for k, p in enumerate(pieces):
        s += _n(p["text"])
        offs.append((len(s), p.get("speaker")))
    return s, offs


def _key(s, i):
    return s[max(i - 2, 0):i] + "|" + s[i:i + 2]


def _cut_changes(bid, live, autos):
    """Where the person cut a telop in two (split) or joined two telops (join), as keys of the letters around the cut."""
    autos = sorted([p for p in autos if (p.get("text") or "").strip() and not p["text"].startswith("※") and p["text"] != "www"], key=lambda p: p["start"])
    live = [p for p in live if p["text"] != "www"]
    s1, o1 = _bounds(live)
    s2, o2 = _bounds(autos)
    if not s1 or s1 != s2:
        return []                                  # the words themselves changed: not a pure cut change
    b1 = {o for o, _sp in o1[:-1]}
    b2 = {o for o, _sp in o2[:-1]}
    sp1 = {o: sp for o, sp in o1}
    out = []
    for o in sorted(b1 - b2):
        if 2 <= o <= len(s1) - 2:
            out.append({"block": bid, "op": "split", "k": _key(s1, o)})
    for o in sorted(b2 - b1):
        if 2 <= o <= len(s1) - 2:
            out.append({"block": bid, "op": "join", "k": _key(s1, o)})
    return out


def update_cuts(projects_root, out=CUT_FILE):
    """Count the cuts / joins of every project into presets/cut_prefs.json (a key is used once the person did the same thing and never the opposite more often)."""
    cnt = {"split": {}, "join": {}}
    for f in Path(projects_root).glob("*/edits_log.json"):
        try:
            for e in json.loads(f.read_text(encoding="utf-8")).get("cuts", []):
                cnt[e["op"]][e["k"]] = cnt[e["op"]].get(e["k"], 0) + 1
        except Exception:   # noqa: BLE001
            continue
    res = {"split": {k: n for k, n in cnt["split"].items() if n > cnt["join"].get(k, 0)},
           "join": {k: n for k, n in cnt["join"].items() if n > cnt["split"].get(k, 0)}}
    try:
        Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:   # noqa: BLE001
        pass
    return res


def apply_cuts(pieces, path=CUT_FILE):
    """Cut and join the automatic telops where the person did it by hand before."""
    try:
        pref = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:   # noqa: BLE001
        return pieces
    sp_k, jo_k = pref.get("split", {}), pref.get("join", {})
    if not sp_k and not jo_k:
        return pieces
    pieces = sorted(pieces, key=lambda p: p["start"])
    out = []
    for p in pieces:                                       # joins
        q = out[-1] if out else None
        if q is not None and jo_k and q.get("speaker", 0) >= 0 and q.get("speaker") == p.get("speaker") and p["text"] != "www" and q["text"] != "www" \
                and not q.get("_fixed_by_user") and not p.get("_fixed_by_user") and 0 <= p["start"] - q.get("end", p["start"]) < 0.15 \
                and _key(_n(q["text"]) + _n(p["text"]), len(_n(q["text"]))) in jo_k and len(_n(q["text"]) + _n(p["text"])) <= 24:
            q["text"] = q["text"].rstrip("\n") + p["text"]
            q["end"] = p.get("end", q.get("end"))
            if "s1" in p:
                q["s1"] = p["s1"]
            if q.get("ct") is not None and p.get("ct") is not None:
                q["ct"] = list(q["ct"]) + list(p["ct"])
            continue
        out.append(p)
    res = []
    for p in out:                                          # splits
        n = _n(p["text"])
        if p.get("speaker", 0) < 0 or p["text"] == "www" or p.get("_fixed_by_user") or len(n) < 4 or not sp_k:
            res.append(p)
            continue
        cut = next((i for i in range(2, len(n) - 1) if _key(n, i) in sp_k), None)
        if cut is None:
            res.append(p)
            continue
        raw = p["text"]
        idx, seen = 0, 0                                   # position in the raw text of the cut (letters that count)
        while idx < len(raw) and seen < cut:
            if _n(raw[idx]):
                seen += 1
            idx += 1
        s0, e0 = p["start"], p.get("end", p["start"] + 1.0)
        ct = p.get("ct")
        t = ct[cut] if ct is not None and len(ct) == len(n) and cut < len(ct) else s0 + (e0 - s0) * cut / len(n)
        if not (s0 + 0.12 < t < e0 - 0.12):
            res.append(p)
            continue
        p1, p2 = dict(p), dict(p)
        p1["text"], p2["text"] = raw[:idx], raw[idx:]
        p1["end"] = p2["start"] = round(t, 3)
        if "s1" in p1:
            p1["s1"] = round(t, 3)
        if "s0" in p2:
            p2["s0"] = round(t, 3)
        if ct is not None and len(ct) == len(n):
            p1["ct"], p2["ct"] = list(ct[:cut]), list(ct[cut:])
        res.extend([p1, p2])
    return res


def record(project_dir, edited, auto):
    """Write edits_log.json of the project: the start changes (and counts of other changes) against the automatic telops."""
    events, other, cuts = [], {"text": 0, "speaker": 0, "deleted": 0, "added": 0}, []
    for bid, b in edited.items():
        a = auto.get(bid)
        if not a or abs(a.get("a0", -9) - b.get("a0", 99)) > 0.3:
            continue
        live = sorted([p for p in b["pieces"] if p.get("speaker", 0) >= 0 and not p.get("deleted") and (p.get("text") or "").strip()
                       and not (p["text"].startswith("※"))], key=lambda p: p["start"])
        autos = [p for p in a["pieces"] if p.get("speaker", 0) >= 0]
        used = set()
        for k, p in enumerate(live):
            best, bd = None, 9.0
            for j, q in enumerate(autos):
                if j in used:
                    continue
                if _n(q["text"]) == _n(p["text"]) and abs(q["start"] - p["start"]) < bd and abs(q["start"] - p["start"]) < 2.5:
                    best, bd = j, abs(q["start"] - p["start"])
            if best is None:
                near = [j for j, q in enumerate(autos) if j not in used and abs(q["start"] - p["start"]) < 0.6]
                if near:
                    j = min(near, key=lambda j: abs(autos[j]["start"] - p["start"]))
                    used.add(j)
                    other["text"] += 1
                    if autos[j].get("speaker") != p.get("speaker"):
                        other["speaker"] += 1
                else:
                    other["added"] += 1
                continue
            used.add(best)
            q = autos[best]
            if q.get("speaker") != p.get("speaker"):
                other["speaker"] += 1
            events.append({"block": bid, "cat": category(live, k), "delta": round(p["start"] - q["start"], 3)})
        other["deleted"] += sum(1 for p in b["pieces"] if p.get("deleted"))
        cuts.extend(_cut_changes(bid, live, autos))
    Path(project_dir, "edits_log.json").write_text(json.dumps({"events": events, "other": other, "cuts": cuts}, ensure_ascii=False), encoding="utf-8")


def update(projects_root, out=BIAS_FILE):
    """Sum up every project's edits_log.json into presets/timing_bias.json."""
    by = {}
    for f in Path(projects_root).glob("*/edits_log.json"):
        try:
            for e in json.loads(f.read_text(encoding="utf-8")).get("events", []):
                if abs(e["delta"]) <= 1.5:
                    by.setdefault(e["cat"], []).append(e["delta"])
        except Exception:   # noqa: BLE001
            continue
    res = {}
    for cat, ds in by.items():
        moved = [d for d in ds if abs(d) >= 0.02]
        if len(ds) < MIN_N:
            continue
        med = statistics.median(ds)
        same = sum(1 for d in ds if d * med > 0) / float(len(ds))
        if abs(med) >= MIN_SHIFT and same >= AGREE:
            res[cat] = {"shift": round(max(-CLAMP, min(CLAMP, med * 0.8)), 3), "n": len(ds), "agree": round(same, 2)}
    try:
        Path(out).write_text(json.dumps({"bias": res, "samples": {k: len(v) for k, v in by.items()}}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:   # noqa: BLE001
        pass
    return res


def apply(pieces, a0, path=BIAS_FILE):
    """Move the heads of the automatic telops by what the user has been correcting."""
    try:
        bias = json.loads(Path(path).read_text(encoding="utf-8")).get("bias", {})
    except Exception:   # noqa: BLE001
        return pieces
    if not bias:
        return pieces
    live = sorted([p for p in pieces if p.get("speaker", 0) >= 0 and (p.get("text") or "").strip()], key=lambda p: p["start"])
    prev_start = None
    for k, p in enumerate(live):
        b = bias.get(category(live, k))
        if b and not p.get("_fixed_by_user"):
            old = p["start"]
            new = max(old + b["shift"], a0)
            if prev_start is not None:
                new = max(new, prev_start + 0.1)
            if k + 1 < len(live):
                new = min(new, live[k + 1]["start"] - 0.1)
            d = new - old
            if abs(d) > 1e-6:
                p["start"] = round(new, 3)
                if "s0" in p:
                    p["s0"] += d
                if k > 0 and abs(live[k - 1].get("end", -9) - old) < 0.011:
                    live[k - 1]["end"] = round(new, 3)
        prev_start = p["start"]
    return pieces
