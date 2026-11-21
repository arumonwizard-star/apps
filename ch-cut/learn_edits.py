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


def record(project_dir, edited, auto):
    """Write edits_log.json of the project: the start changes (and counts of other changes) against the automatic telops."""
    events, other = [], {"text": 0, "speaker": 0, "deleted": 0, "added": 0}
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
    Path(project_dir, "edits_log.json").write_text(json.dumps({"events": events, "other": other}, ensure_ascii=False), encoding="utf-8")


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
