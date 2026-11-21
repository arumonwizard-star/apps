#!/usr/bin/env python3
"""
Learn the telop rules (where to break a line, when to use two lines, how long a telop may be) from
projects you finished by hand.

    python3 learn_telop_rules.py  数字当て.fcpxmld  別のプロジェクト.fcpxml  [--out presets/telop_rules.json]

It reads the 【CH】チ / 【CH】ホ titles (and nothing else), counts, and writes the "learned" part of the
rules file.  Anything you put under "manual" in that file is kept and always wins, so you can correct a
rule by hand without it being overwritten the next time.
"""
import argparse
import collections
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "presets" / "telop_rules.json"
DEFAULT_PATTERN = r"【CH】(チ|ホ)$"


def read_titles(path, pattern):
    p = Path(path)
    if p.is_dir():
        p = p / "Info.fcpxml"
    root = ET.parse(p).getroot()
    eff = {e.get("id"): e.get("name", "") for e in root.iter("effect")}
    rx = re.compile(pattern)
    rows = []
    for t in root.iter("title"):
        if not rx.search(eff.get(t.get("ref"), "")):
            continue
        txt = "".join(x.text or "" for x in t.iter("text-style") if x.text)
        lines = [l.strip() for l in txt.split("\n") if l.strip()]
        if lines:
            rows.append(lines)
    return rows


def learn(rows):
    n = len(rows)
    L = [len("".join(r)) for r in rows]
    two = [len(r) > 1 for r in rows]
    by = collections.defaultdict(lambda: [0, 0])
    for l, t in zip(L, two):
        by[l][0] += 1
        by[l][1] += t
    # smallest length from which a two-line title is the usual case
    brk = None
    for l in sorted(by):
        tot = sum(v[0] for k, v in by.items() if k >= l)
        tw = sum(v[1] for k, v in by.items() if k >= l)
        if tot >= 3 and tw / tot >= 0.75:
            brk = l
            break
    first_last = collections.Counter(r[0][-1] for r in rows if len(r) > 1)
    first_minus_second = [len(r[0]) - len(r[1]) for r in rows if len(r) == 2]
    return {
        "titles_seen": n,
        "max_title_chars": max(L) if L else 20,
        "line_max": max(len(x) for r in rows for x in r) if rows else 11,
        "break_min_chars": brk if brk is not None else 8,
        "first_line_longer_by": round(sum(first_minus_second) / len(first_minus_second), 2) if first_minus_second else 0,
        "break_after": dict(first_last.most_common()),
        "period_used": any("。" in "".join(r) for r in rows),
        "length_histogram": {str(k): v for k, v in sorted(collections.Counter(L).items())},
        "two_line_by_length": {str(k): "%d/%d" % (v[1], v[0]) for k, v in sorted(by.items())},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("projects", nargs="+")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--pattern", default=DEFAULT_PATTERN, help="regex for the speaker title names")
    a = ap.parse_args()
    rows = []
    for p in a.projects:
        got = read_titles(p, a.pattern)
        print("%s: %d titles" % (p, len(got)))
        rows += got
    if not rows:
        sys.exit("no telops found")
    out = Path(a.out)
    old = {}
    if out.exists():
        old = json.loads(out.read_text(encoding="utf-8"))
    data = {"learned": learn(rows), "manual": old.get("manual", {})}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written:", out)
    L = data["learned"]
    print("two lines from %d chars up, longest title %d, longest line %d" % (L["break_min_chars"], L["max_title_chars"], L["line_max"]))


if __name__ == "__main__":
    main()
