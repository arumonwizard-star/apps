#!/usr/bin/env python3
"""
CH Cut - step 2b: transcription (local whisper.cpp, offline)

Transcribes the separately recorded audio and saves what was actually said,
with start/end times (in seconds, measured on the AUDIO file's own timeline).

Usage:
    python3 transcribe.py AUDIO [--out FILE.json] [--model MODEL.bin]
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_MODEL = Path.home() / ".local/share/whisper-cpp/ggml-large-v3-turbo-q5_0.bin"


def find_whisper():
    for name in ("whisper-cli", "whisper-cpp"):
        p = shutil.which(name)
        if p:
            return p
    for p in ("/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli"):
        if Path(p).exists():
            return p
    sys.exit("whisper-cli not found. Install it with:  brew install whisper-cpp")


def mmss(sec):
    tenths = int(round(sec * 10))
    return "%d:%02d.%d" % (tenths // 600, (tenths // 10) % 60, tenths % 10)


def parse_whisper_json(data):
    segments, tokens = [], []
    for seg in data.get("transcription", []):
        off = seg.get("offsets", {})
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        segments.append({
            "start": round(off.get("from", 0) / 1000.0, 3),
            "end": round(off.get("to", 0) / 1000.0, 3),
            "text": text,
        })
        for tk in seg.get("tokens", []) or []:
            tt = tk.get("text", "")
            if tt.startswith("[_") or not tt.strip():
                continue  # special tokens like [_BEG_], [_TT_123]
            toff = tk.get("offsets", {})
            tokens.append({
                "start": round(toff.get("from", 0) / 1000.0, 3),
                "end": round(toff.get("to", 0) / 1000.0, 3),
                "text": tt,
                "p": round(float(tk.get("p", 0.0)), 3),
            })
    return segments, tokens


def run_whisper(whisper, model, wav, lang, tmp, tag, offset=0.0, prompt=None):
    prefix = str(Path(tmp) / ("result_" + tag))
    # -mc 0: do not feed earlier text back in (stops Whisper from looping on one phrase)
    cmd = [whisper, "-m", str(model), "-f", wav, "-l", lang, "-mc", "0", "-ojf", "-of", prefix]
    if prompt:
        cmd += ["--prompt", prompt]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0 or not Path(prefix + ".json").exists():
        tail = r.stderr.decode(errors="replace")[-1500:]
        sys.exit("whisper failed (code %s):\n%s" % (r.returncode, tail))
    with open(prefix + ".json", encoding="utf-8") as f:
        segs, toks = parse_whisper_json(json.load(f))
    for x in segs + toks:
        x["start"] = round(x["start"] + offset, 3)
        x["end"] = round(x["end"] + offset, 3)
    return segs, toks


def find_loop(segments, min_run=4):
    """Return the start time of the first run of >= min_run identical consecutive segments, else None."""
    run = 1
    for i in range(1, len(segments)):
        if segments[i]["text"] == segments[i - 1]["text"]:
            run += 1
            if run == min_run:
                return segments[i - min_run + 1]["start"]
        else:
            run = 1
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--out")
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--lang", default="ja")
    ap.add_argument("--normalize", action="store_true",
                    help="raise quiet speech to a steady level before transcribing")
    ap.add_argument("--start", type=float, default=None, help="only transcribe from this second")
    ap.add_argument("--end", type=float, default=None, help="only transcribe up to this second")
    args = ap.parse_args()

    audio = Path(args.audio).expanduser().resolve()
    model = Path(args.model).expanduser()
    if not model.exists():
        sys.exit("Model file not found: %s" % model)
    out = Path(args.out).expanduser() if args.out else Path.home() / "Desktop" / (audio.stem + "_transcript.json")
    whisper = find_whisper()

    with tempfile.TemporaryDirectory() as tmp:
        wav = str(Path(tmp) / "audio16k.wav")
        r = subprocess.run(
            ["ffmpeg", "-v", "error"]
            + (["-ss", "%.3f" % args.start] if args.start else [])
            + (["-t", "%.3f" % (args.end - (args.start or 0.0))] if args.end else [])
            + ["-i", str(audio), "-vn", "-ac", "1", "-ar", "16000"]
            + (["-af", "dynaudnorm=f=150:g=15:p=0.95:m=15"] if args.normalize else [])
            + ["-c:a", "pcm_s16le", wav, "-y"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            sys.exit("ffmpeg failed:\n" + r.stderr.decode(errors="replace"))

        print("文字起こし中です。数分かかることがあります…")
        t0 = time.time()
        segments, tokens = run_whisper(whisper, model, wav, args.lang, tmp, "all")
        # Whisper sometimes gets stuck repeating one phrase. If so, redo that part in short chunks.
        for attempt in range(5):
            bad = find_loop(segments)
            if bad is None:
                break
            start = max(0.0, bad - 5.0)
            print("繰り返し(暴走)を検出: %.0f秒以降を短く区切ってやり直します…" % start)
            total = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", wav],
                stdout=subprocess.PIPE).stdout.decode().strip())
            segments = [x for x in segments if x["end"] <= start]
            tokens = [x for x in tokens if x["end"] <= start]
            pos, chunk = start, 30.0
            while pos < total - 0.2:
                cw = str(Path(tmp) / ("chunk_%d.wav" % int(pos * 10)))
                subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % pos, "-t", "%.3f" % chunk,
                                "-i", wav, "-c:a", "pcm_s16le", cw, "-y"], check=True)
                s2, t2 = run_whisper(whisper, model, cw, args.lang, tmp, "c%d" % int(pos * 10), pos)
                segments += s2
                tokens += t2
                pos += chunk
            break
        print("done in %.0f s" % (time.time() - t0))
        if args.start:
            for x in segments + tokens:
                x["start"] = round(x["start"] + args.start, 3)
                x["end"] = round(x["end"] + args.start, 3)

    result = {
        "audio": str(audio),
        "model": model.name,
        "language": args.lang,
        "segments": segments,
        "tokens": tokens,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print()
    for s in segments:
        print("[%s - %s] %s" % (mmss(s["start"]), mmss(s["end"]), s["text"]))
    print()
    print("saved:", out)


if __name__ == "__main__":
    main()
