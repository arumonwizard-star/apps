#!/usr/bin/env python3
"""
CH Cut - step 1: audio sync

Finds where the separately-recorded audio (e.g. .m4a) sits on the camera
video's timeline by comparing it with the camera's built-in audio.

Usage:
    python3 sync_audio.py VIDEO AUDIO [--json out.json]

Output convention:
    offset_sec > 0  -> the external audio starts that many seconds AFTER the
                       start of the video (put it later on the timeline).
    offset_sec < 0  -> the external audio starts BEFORE the video.

Originals are only read, never modified.
"""
import argparse
import json
import subprocess
import sys

import numpy as np

SR = 16000  # analysis sample rate (Hz)


def load_mono(path):
    """Decode any media file's audio to mono float32 at SR via ffmpeg."""
    cmd = [
        "ffmpeg", "-v", "error", "-i", path,
        "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-",
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        sys.exit("ffmpeg failed on %s:\n%s" % (path, p.stderr.decode(errors="replace")))
    x = np.frombuffer(p.stdout, dtype=np.float32).copy()
    if x.size == 0:
        sys.exit("No audio found in %s" % path)
    return x


def gcc_phat(ref, sig, max_lag=None):
    """
    Generalized cross-correlation with phase transform.
    Robust to different microphones / EQ / level, which is exactly our case
    (iPhone built-in mic vs. separate recorder).

    Returns (lag_samples, confidence) where lag is the position of `sig`
    relative to `ref`: ref[t] ~ sig[t - lag].
    """
    n = 1
    while n < ref.size + sig.size:
        n *= 2
    R = np.fft.rfft(ref, n)
    S = np.fft.rfft(sig, n)
    cross = R * np.conj(S)
    cross /= np.abs(cross) + 1e-12
    cc = np.fft.irfft(cross, n)
    # reorder so index 0..n-1 maps to lags -(n/2)..(n/2)-1 ... use explicit lags
    lags = np.arange(n)
    lags[lags >= n // 2] -= n
    if max_lag is not None:
        mask = np.abs(lags) <= max_lag
    else:
        mask = np.ones(n, dtype=bool)
    mag = np.abs(cc)
    cand = np.where(mask, mag, 0.0)
    k = int(np.argmax(cand))
    peak = cand[k]
    # confidence: peak vs. the strongest value far from the peak (>50 ms away)
    far = mask & (np.abs(lags - lags[k]) > int(0.05 * SR))
    second = mag[far].max() if far.any() else 1e-12
    conf = float(peak / (second + 1e-12))
    return int(lags[k]), conf


def quality_label(conf):
    if conf >= 3.0:
        return "high"
    if conf >= 1.8:
        return "medium"
    return "LOW (check manually)"


def drift_check(video_a, ext_a, lag0, n_windows=5, win_sec=30, search_sec=1.0):
    """
    Re-measure the offset in several short windows across the recording.
    If the offsets change over time, the two recorders' clocks drift apart.
    """
    results = []
    win = int(win_sec * SR)
    if video_a.size < win * 2:
        return results
    starts = np.linspace(0, video_a.size - win, n_windows).astype(int)
    for s in starts:
        seg = video_a[s:s + win]
        # where this segment should be in the external audio
        e0 = s - lag0
        pad = int(search_sec * SR)
        a = max(0, e0 - pad)
        b = min(ext_a.size, e0 + win + pad)
        if b - a < win // 2 or e0 < -pad:
            continue
        ext_seg = ext_a[a:b]
        lag, conf = gcc_phat(ext_seg, seg, max_lag=int(2 * pad))
        # lag: ext_seg[t] ~ seg[t - lag] ; seg starts at ext index e0 -> expected lag = e0 - a
        err = lag - (e0 - a)
        results.append({
            "video_time_sec": round(s / SR, 2),
            "extra_offset_ms": round(-err * 1000.0 / SR, 2),
            "confidence": round(conf, 2),
        })
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("audio")
    ap.add_argument("--json", help="write result to this JSON file")
    args = ap.parse_args()

    v = load_mono(args.video)
    e = load_mono(args.audio)
    print("video audio: %.2f s / external audio: %.2f s" % (v.size / SR, e.size / SR))

    # lag such that video_audio[t] ~ ext_audio[t - lag]
    # => the external audio starts at +lag/SR seconds on the video timeline.
    lag, conf = gcc_phat(v, e)
    offset = lag / SR
    print("offset: %+.3f s (%d samples @ %d Hz)" % (offset, lag, SR))
    print("sync confidence: %.2f -> %s" % (conf, quality_label(conf)))

    drift = drift_check(v, e, lag)
    drift_ms = None
    if drift:
        vals = [d["extra_offset_ms"] for d in drift if d["confidence"] >= 1.8]
        if len(vals) >= 2:
            drift_ms = max(vals) - min(vals)
        print("drift check (extra offset per window):")
        for d in drift:
            print("  t=%7.1fs  %+8.2f ms  (conf %.2f)" %
                  (d["video_time_sec"], d["extra_offset_ms"], d["confidence"]))
        if drift_ms is not None:
            verdict = "OK" if drift_ms < 10 else "DRIFT DETECTED"
            print("drift spread: %.2f ms -> %s" % (drift_ms, verdict))
    else:
        print("drift check skipped (recording shorter than 60 s)")

    out = {
        "video": args.video,
        "audio": args.audio,
        "offset_sec": round(offset, 4),
        "confidence": round(conf, 2),
        "quality": quality_label(conf),
        "video_audio_duration_sec": round(v.size / SR, 3),
        "external_audio_duration_sec": round(e.size / SR, 3),
        "drift_windows": drift,
        "drift_spread_ms": None if drift_ms is None else round(drift_ms, 2),
    }
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("saved:", args.json)


if __name__ == "__main__":
    main()
