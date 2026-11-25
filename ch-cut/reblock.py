#!/usr/bin/env python3
"""Re-transcribe one block only, using that block's script lines as the Whisper prompt, and patch transcript.json."""
import argparse, json, subprocess, sys, tempfile
from pathlib import Path
import transcribe as T
import parse_script as ps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript"); ap.add_argument("script"); ap.add_argument("report"); ap.add_argument("bid")
    a = ap.parse_args()
    tj = json.load(open(a.transcript, encoding="utf-8"))
    rep = json.load(open(a.report, encoding="utf-8"))
    blocks = ps.parse(open(a.script, encoding="utf-8").read())
    blk = next((b for b in blocks if b["id"] == a.bid), None)
    rb = next((b for b in rep["blocks"] if b["id"] == a.bid), None)
    if not blk or not rb:
        sys.exit("ブロックが見つかりません")
    ts = rb.get("takes") or []
    if ts:
        lo = min(t["start"] for t in ts) - 3.0
        hi = max(t["end"] for t in ts) + 3.0
    elif rb.get("cut"):
        lo, hi = rb["cut"][0] - 12.0, rb["cut"][1] + 12.0
    else:
        sys.exit("範囲が分かりません")
    lo = max(lo, 0.0)
    prompt = "".join(l["text"] for l in blk["lines"] if l.get("kind") == "speech")[:200]
    audio = tj["audio"]
    model = Path(T.DEFAULT_MODEL).expanduser()
    wh = T.find_whisper()
    with tempfile.TemporaryDirectory() as tmp:
        wav = str(Path(tmp) / "b.wav")
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % lo, "-t", "%.3f" % (hi - lo), "-i", audio, "-vn", "-ac", "1",
                            "-ar", "16000", "-af", "dynaudnorm=f=150:g=15:p=0.95:m=15", "-c:a", "pcm_s16le", wav, "-y"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode:
            sys.exit(r.stderr.decode(errors="replace"))
        segs, toks = T.run_whisper(wh, model, wav, tj.get("language", "ja"), tmp, "blk", lo, prompt)
    mid = lambda x: (x["start"] + x["end"]) / 2
    keep = lambda x: not (lo <= mid(x) <= hi)
    tj["segments"] = sorted([x for x in tj["segments"] if keep(x)] + segs, key=lambda x: x["start"])
    tj["tokens"] = sorted([x for x in tj["tokens"] if keep(x)] + toks, key=lambda x: x["start"])
    tj.setdefault("reblocks", []).append(a.bid)
    json.dump(tj, open(a.transcript, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("%s を再解析しました（%d 区間）" % (a.bid, len(segs)))


if __name__ == "__main__":
    main()
