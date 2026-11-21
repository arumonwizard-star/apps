#!/usr/bin/env python3
"""Learn the size of ※ notes from finished projects (the 【CH】注釈 titles).

    python3 learn_note_size.py  おかちめんこ.fcpxmld  別のプロジェクト.fcpxmld

A note on lane 2 or higher sits above another telop ("with telop"); on lane 0/1 it is alone.  For each
group a line  scale = base + per_char * (length - 4)  is fitted (least squares over every sample from
every project you give; a single sample gives a flat value) and written to presets/telop_rules.json
under learned.note_size.  Run it again with more projects and the rule gets better.  Samples are kept
in the file so earlier projects need not be given again.
"""
import json, re, sys
from pathlib import Path
import xml.etree.ElementTree as ET

OUT = Path(__file__).resolve().parent / "presets" / "telop_rules.json"
SCALE_KEY = "9999/10003/10043/1/100/105"


def samples_from(path):
    p = Path(path)
    p = p / "Info.fcpxml" if p.is_dir() else p
    root = ET.parse(p).getroot()
    eff = {e.get("id"): e.get("name", "") for e in root.iter("effect")}
    out = []
    for t in root.iter("title"):
        if "注釈" not in eff.get(t.get("ref"), ""):
            continue
        txt = "".join((x.text or "") for x in t.iter("text-style") if x.get("ref"))
        sc = next((q.get("value") for q in t.findall("param") if q.get("key") == SCALE_KEY), None)
        if not txt.startswith("※") or not sc:
            continue
        out.append({"n": len(txt), "with_telop": int(t.get("lane") or 0) >= 2, "scale": float(sc.split()[0]), "src": p.parent.name})
    return out


def fit(pts):
    if not pts:
        return None
    if len(pts) < 2 or len({n for n, _ in pts}) < 2:
        v = sum(s for _, s in pts) / len(pts)
        return {"base": round(v, 3), "per_char": 0.0}
    mx = sum(n for n, _ in pts) / len(pts); my = sum(s for _, s in pts) / len(pts)
    sxx = sum((n - mx) ** 2 for n, _ in pts); b = sum((n - mx) * (s - my) for n, s in pts) / sxx
    b = min(0.0, b)                      # longer never gets bigger
    return {"base": round(my + b * (4 - mx), 3), "per_char": round(b, 3)}


def main(argv):
    d = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"learned": {}, "manual": {}}
    ns = d.setdefault("learned", {}).setdefault("note_size", {})
    smp = ns.get("samples", [])
    for a in argv:
        smp = [s for s in smp if s.get("src") != Path(a).stem + ".fcpxmld" and s.get("src") != Path(a).name] + samples_from(a)
    ns["samples"] = smp
    for key, flag in (("alone", False), ("with_telop", True)):
        f = fit([(s["n"], s["scale"]) for s in smp if s["with_telop"] == flag])
        if f:
            f.update(min=1.2, max=1.9); ns[key] = f
    OUT.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(ns, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
