#!/usr/bin/env python3
"""
CH Cut - step 3b: rough cut (仮編集) -> Final Cut Pro

Pipeline:
  1. sync the separate audio to the camera video          (sync_audio.py)
  2. find takes in the transcript and pick the last good one per script block
                                                           (find_takes.py)
  3. refine each take's start/end from the audio loudness, keeping a little
     room before the first word and after the last word / laughter
  4. write an FCPXML: chosen takes in script order, video + separate audio as
     independent clips, NG takes left out, markers on every clip.

Usage:
    python3 make_rough_cut.py VIDEO AUDIO TRANSCRIPT.json SCRIPT.txt [--out FILE.fcpxml]

Originals are never modified.
"""
import argparse
import copy
import json
import math
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

import find_takes as ft
import make_fcpxml as mf
import parse_script as ps
import sync_audio as sa

FRAME = 0.01          # loudness analysis step (s)
JOIN_GAP = 0.5        # pause (s) that still belongs to the same sentence
ADLIB_MAX = 1.6        # ad-libs count only if they end within this many seconds after the last script line
ADLIB_GAP = 1.5       # pause (s) allowed between a script line and an ad-lib after it


# ---------------------------------------------------------------- loudness
import re

LAUGH_SEG_RE = re.compile(r"^[あはひふへほっハヒフヘホwｗ笑!！?？、。…\s]+$")


def _db_series(x, step):
    n_samp = int(step * sa.SR)
    n = len(x) // n_samp
    fr = x[:n * n_samp].reshape(n, n_samp)
    rms = np.sqrt((fr ** 2).mean(axis=1))
    return 20 * np.log10(rms + 1e-9), n_samp


def detect_claps(x, sensitivity=1.0):
    """
    Find hand claps / slate claps: very sudden, short, loud, broadband bangs.
    Speech and laughter rise more slowly, last longer and carry little energy
    above 2 kHz. Returns a list of times (s) of the bangs.
    """
    STEP = 0.005
    db, n_samp = _db_series(x, STEP)
    if len(db) < 50:
        return []
    floor, p90 = np.percentile(db, 10), np.percentile(db, 90)
    claps, i = [], 4
    last = -1.0
    while i < len(db) - 32:
        rise = db[i] - db[i - 3]
        is_peak = db[i] >= db[i - 1] and db[i] >= db[i + 1]
        if is_peak and rise >= 16 / sensitivity and db[i] >= p90 - 3 * sensitivity:
            # must die away quickly (a clap is over within ~150 ms)
            after = db[i + 6:i + 32]
            if len(after) and after.min() <= db[i] - 14:
                # and must be broadband: much energy above 2 kHz
                a = i * n_samp
                seg = x[a:a + int(0.04 * sa.SR)]
                if len(seg) > 64:
                    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
                    freqs = np.fft.rfftfreq(len(seg), 1.0 / sa.SR)
                    hf = spec[freqs >= 2000].sum() / (spec.sum() + 1e-12)
                    t = i * STEP
                    if hf >= 0.12 * sensitivity and t - last > 0.15:
                        claps.append(round(t, 3))
                        last = t
        i += 1
    return claps


def voiced_runs(x, claps=(), level=0.30):
    """Return [(start_sec, end_sec)] of speech-like activity in mono audio x @ SR."""
    db, n_samp = _db_series(x, FRAME)
    n = len(db)
    if n == 0:
        return []
    floor, peak = np.percentile(db, 10), np.percentile(db, 95)
    thr = floor + level * (peak - floor)
    mask = db > thr
    for c in claps:                      # claps are not speech: blank them out
        a = max(0, int((c - 0.03) / FRAME))
        b = min(n, int((c + 0.18) / FRAME) + 1)
        mask[a:b] = False
    runs, i = [], 0
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            runs.append([i * FRAME, j * FRAME])
            i = j
        else:
            i += 1
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < 0.25:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [tuple(r) for r in merged if r[1] - r[0] >= 0.10]


def voicing_ratio(x, t0, t1):
    """Share of 10 ms frames in [t0,t1] that are periodic (a pitched voice).
    Speech vowels and most laughs are pitched; coughs, throat clearing and
    breaths are mostly noisy."""
    seg = x[int(t0 * sa.SR):int(t1 * sa.SR)]
    win, hop = int(0.032 * sa.SR), int(0.010 * sa.SR)
    lo, hi = int(sa.SR / 400), int(sa.SR / 70)
    voiced = total = 0
    for i in range(0, len(seg) - win - hi, hop):
        f0 = seg[i:i + win]
        f0 = f0 - f0.mean()
        e0 = float((f0 ** 2).sum())
        if e0 < 1e-9:
            continue
        total += 1
        best = 0.0
        for lag in range(lo, hi, 2):
            g = seg[i + lag:i + lag + win]
            g = g - g.mean()
            c = float((f0 * g).sum()) / (e0 * float((g ** 2).sum()) + 1e-12) ** 0.5
            if c > best:
                best = c
        if best > 0.45:
            voiced += 1
    return voiced / total if total else 0.0


def closing_clap_after(t, claps, runs, env=None):
    """First clap at/after t that CLOSES a take: the voice does not go on after it.
    A clap in the middle of laughter or speech is followed by more voiced sound, either
    in a run that contains the clap and goes on, or in one starting soon after it (env: the level in dB per FRAME,
    so a bang inside a word - a plosive that looks like a clap - is told from a real clap by the voice right after it)."""
    for c in claps:
        if c < t or any(r[1] > c + 0.2 and r[0] < c + 0.6 for r in runs):
            continue
        if env is not None:
            db = env[0]
            a0_, a1_ = int((c - 0.5) / FRAME), int((c - 0.08) / FRAME)
            b0_, b1_ = int((c + 0.19) / FRAME), int((c + 0.4) / FRAME) + 1
            if 0 <= a0_ < a1_ <= len(db) and b1_ <= len(db) and float(max(db[b0_:b1_])) >= float(max(db[a0_:a1_])) - 14.0:
                continue
        return c
    return None


def _much_quieter(env, r_prev, r_main, gap_db=20.0):
    """A sound in front of the line (a breath, a lip noise) that is far quieter than the line itself is not part of it."""
    if env is None:
        return False
    db, _thr = env
    def pk(r):
        a, b = int(r[0] / FRAME), max(int(r[1] / FRAME), int(r[0] / FRAME) + 1)
        return float(max(db[a:b])) if b > a and a < len(db) else -999.0
    return (r_prev[1] - r_prev[0]) < 0.6 and pk(r_prev) < pk(r_main) - gap_db


def refine(take, runs, claps, cue_chars, cap_after, pad_before, pad_after,
           x=None, tail=True, tail_gap=0.9, max_tail=2.0, env=None, runs_lo=None, debug=None, speech_until=None,
           clamp_to=None):
    """
    Tighten a coarse Whisper range using loudness, claps and pitch. Times: audio timeline.
      start: just before the first word of the cue sentence
             - never reaches back further than that sentence could plausibly last
             - never crosses a clap before it (claps separate takes)
             - short non-pitched noises before it (coughs) are left out
      end:   after the last script line, ad-libs and laughter are kept
             (while the voice keeps going), up to the clap that closes the take, a
             "let's redo it" remark, or the next take.
    """
    t0, t1 = take["start"], take["end"]
    cue_end = take.get("cue_first_end", take["cue_end"])
    chars = take.get("cue_first_chars") or cue_chars
    est = max(1.2, chars * 0.17)
    floor_start = max(t0 - 0.3, cue_end - max(2.0, est * 2.0))
    pc = [c for c in claps if floor_start - 0.3 <= c <= cue_end]
    # a "clap" inside the cue's own Whisper segment, right after a stretch of talking, is a consonant or a breath of the
    # sentence itself ("…枚数を食べた|どう"): it does not separate takes
    pc = [c for c in pc if c <= t0 + 0.3 or not any(r[1] - r[0] >= 0.4 and c - 0.35 <= r[1] <= c + 0.1 for r in runs)]
    if pc:
        floor_start = max(floor_start, pc[-1] + 0.12)

    # The cue sentence is the sound that is still going on around the END of the cue
    # (Whisper's segment ends are far better than its starts, which often include silence).
    # Never take "the earliest sound": that is where coughs and throat clearing sit.
    win = [r for r in runs if r[1] > floor_start and r[0] <= cue_end + 0.3]
    over = [r for r in win if r[1] >= cue_end - 1.0]
    if over:
        first = over[0]
    elif win:
        first = win[-1]
    else:
        return t0, t1, False
    def speechlike(r):
        if r[1] - r[0] >= 0.5:
            return True
        return x is None or voicing_ratio(x, r[0], r[1]) >= 0.5

    # Whisper spreads the first words of a line over the silence in front of them: a short burst (a clap, a click, a
    # breath) with a long silence after it and then a long stretch of voice that the cue needs is not the line;
    # the line begins with the voice.
    try:
        if first[1] - first[0] < 0.4 and not os.environ.get("CHCUT_NO_BURST"):
            for r_ in runs[runs.index(first) + 1:]:
                if r_[0] > first[1] + 6.0 or r_[0] > t1:
                    break
                if r_[0] - first[1] >= 1.0 and r_[1] - r_[0] >= max(0.6, 0.35 * est) and speechlike(r_):
                    first = r_
                    break
    except Exception:   # noqa: BLE001
        pass
    onset = max(first[0], floor_start)
    idx = runs.index(first)

    # join an earlier sound only when it is part of the same sentence (short pause)
    # the cue sentence must last about as long as its characters need: if what was found is far too short,
    # an earlier sound after a longer pause is the beginning of the same sentence ("お寿司…この皿の枚数…どう")
    while idx > 0 and onset - runs[idx - 1][1] < (max(JOIN_GAP, 0.7) if (cue_end - onset) < 0.7 * est else JOIN_GAP) \
            and runs[idx - 1][0] >= floor_start \
            and runs[idx - 1][1] - runs[idx - 1][0] >= 0.12 and speechlike(runs[idx - 1]) \
            and not _much_quieter(env, runs[idx - 1], runs[idx]):
        idx -= 1
        onset = max(runs[idx][0], floor_start)
    prev_end = runs[idx - 1][1] if idx > 0 else None
    if debug is not None:
        for r in runs:
            if t0 - 0.6 <= r[0] <= cue_end + 0.3:
                vr = voicing_ratio(x, r[0], r[1]) if x is not None else -1
                debug.append("    %s - %s (%.2fs)  声の高さ %.2f%s" % (
                    ft.mmss(r[0]), ft.mmss(r[1]), r[1] - r[0], vr,
                    "   <- 採用した頭" if abs(r[0] - onset) < 0.011 else ""))
        for c in claps:
            if t0 - 0.6 <= c <= cue_end + 0.3:
                debug.append("    拍手 %s" % ft.mmss(c))
    # the waveform really starts a little before the loud part: walk back while it is audible
    if env is not None:
        db, thr_lo = env
        i = int(onset / FRAME)
        try:     # the run starts with a breath / click far quieter than the voice: the head is where the voice really begins
            if not os.environ.get("CHCUT_NO_QUIETHEAD") and i + int(0.8 / FRAME) < len(db):
                pk0 = float(max(db[i:i + int(0.8 / FRAME) + 1]))
                j0 = i
                while j0 < i + int(0.8 / FRAME) and db[j0] < pk0 - 26.0:
                    j0 += 1
                if (j0 - i) * FRAME > 0.1 and (j0 - i) * FRAME < 0.7:
                    onset = max(onset, j0 * FRAME - 0.02)
                    i = int(onset / FRAME)
        except ValueError:
            pass
        lowest = max(floor_start, (prev_end or 0.0))
        try:     # a breath or a click far quieter than the voice that follows is not part of it (it only adds silence to the head)
            pk_db = float(max(db[i:i + int(0.4 / FRAME) + 1])) if i < len(db) else None
        except ValueError:
            pk_db = None
        floor_db = max(thr_lo, pk_db - 26.0) if (pk_db is not None and not os.environ.get("CHCUT_NO_QUIETHEAD")) else thr_lo
        while i > 0 and db[i - 1] > floor_db and onset - (i - 1) * FRAME <= 0.25 \
                and (i - 1) * FRAME >= lowest:
            i -= 1
        onset = i * FRAME
    pb = pad_before
    start = max(0.0, onset - pb)

    # ---- end ----
    inside = [r for r in runs if r[0] < t1 + 0.3 and r[1] > onset]
    if not inside:
        return start, t1, False
    end = inside[-1][1]
    caps = []
    cc = closing_clap_after(max(onset + 0.5, t1 - 0.3), claps, runs, env)
    if cc is not None:
        caps.append(cc - 0.05)
    if cap_after:
        caps.append(cap_after)
    if take.get("restart_at"):
        caps.append(take["restart_at"])
    hard = min(caps) if caps else 1e9
    if debug is not None:
        debug.append("    終わり: 音の塊の終わり %.2f 上限 %s 拍手 %s 台本後 %s" % (end, [round(c_, 2) for c_ in caps], cc, cap_after))
    end = min(end, hard)
    soft = runs_lo if runs_lo is not None else runs
    if tail and speech_until:
        # ad-libs: real speech that follows the last script line (Whisper heard it, so it
        # is part of the take), no matter how many seconds it takes. Stops at `hard`.
        grew = True
        while grew:
            grew = False
            for r in runs:
                if r[1] > end + 0.01 and r[0] <= end + ADLIB_GAP and r[0] < speech_until - 0.1 \
                        and r[0] < hard:
                    new_end = min(r[1], hard)
                    if new_end > end + 1e-6:
                        end = new_end
                        grew = True
                        break
    if tail:
        # the laughter budget starts after the last ad-lib, not after the last script line
        limit = min(hard, max(end, speech_until or 0.0) + max_tail)
        grew = True
        while grew:
            grew = False
            for r in soft:
                if r[1] > end + 0.01 and r[0] - end < tail_gap and r[0] < limit:
                    new_end = min(r[1], limit)
                    if new_end > end + 1e-6:      # must really move, or we would loop forever
                        end = new_end
                        grew = True
                        break
    # the waveform really ends a little after the loud part: walk forward while audible
    if env is not None:
        db, thr_lo = env
        j = int(end / FRAME)
        while j < len(db) and db[j] > thr_lo and j * FRAME - end <= 0.4 and j * FRAME <= hard:
            j += 1
        end = min(max(end, j * FRAME), hard)
    end = min(end + pad_after, hard)
    if clamp_to is not None:
        # laughter after the last line: the voiced run may be longer (speech and laughter
        # form one sound), but a human keeps only about a second of it
        end = min(end, clamp_to + pad_after)
    return start, max(end, start + 0.2), True


def frame_peak_db(e, offset, fps, k):
    """Peak 5 ms-bin RMS (dB) of the audio that plays during video frame k."""
    t0 = k / fps - offset
    a = int(round(t0 * sa.SR))
    n = int(round(sa.SR / fps))
    if a < 0 or a + n > e.size:
        return -120.0
    seg = e[a:a + n]
    b = max(1, int(0.005 * sa.SR))
    m = len(seg) // b
    if m == 0:
        return -120.0
    r = np.sqrt((seg[:m * b].reshape(m, b) ** 2).mean(axis=1) + 1e-12)
    return float(20 * np.log10(r.max() + 1e-12))


def snap_start_frame(e, offset, fps, k0):
    """First video frame of the sound: peak >= local noise + 8 dB for 3 frames in a row."""
    ctx = [frame_peak_db(e, offset, fps, k) for k in range(k0 - 45, k0 - 3)]
    noise = float(np.percentile(ctx, 20))
    thr = noise + 8.0
    for k in range(k0 - 5, k0 + 7):
        if all(frame_peak_db(e, offset, fps, k + i) >= thr for i in range(3)):
            return k
    return k0


def snap_end_frame(e, offset, fps, k1, margin):
    """End (exclusive) = last frame with sound + margin frames of trailing near-silence."""
    ctx = [frame_peak_db(e, offset, fps, k) for k in range(k1 + 3, k1 + 3)] or []
    pre = [frame_peak_db(e, offset, fps, k) for k in range(k1 - 60, k1 - 10)]
    noise = float(np.percentile(pre, 10))
    if frame_peak_db(e, offset, fps, k1 - 1) >= noise + 20:
        return k1                      # cut inside loud sound (laughter): keep the chosen point
    last = k1 - 1
    while last > k1 - 15 and frame_peak_db(e, offset, fps, last) < noise + 6:
        last -= 1
    return last + 1 + margin


def is_real_speech(sg):
    """A transcript segment that is real talking (not laughter, not a hallucinated filler)."""
    dur = max(0.1, sg["end"] - sg["start"])
    n = len(sg["norm"])
    if sg["skip"] or LAUGH_SEG_RE.match(sg["text"].strip()):
        return False
    return n / dur >= 2.0 or (dur <= 2.0 and n >= 3)


def _lead_in_start(a0, segs, tokens, cue_norm, runs, pad):
    """The speaker added a few words before the script line ("それは" + "人ですか"): Whisper heard them in the same segment,
    but the block head was found at the script words.  The head moves back to the voice that starts the added words."""
    import parse_script as ps
    if len(cue_norm) < 3 or not tokens:
        return a0
    for sg in segs:
        if sg.get("skip") or not (sg["start"] - 0.6 <= a0 <= sg["end"] + 0.3):
            continue
        tx = ps.norm_script(sg["text"])
        k = tx.rfind(cue_norm)
        if k < 2 or k > 5 or len(tx) != k + len(cue_norm):
            continue
        cand = [t for t in tokens if sg["start"] - 1.5 <= t["start"] < sg["end"] and (t.get("text") or "").strip()]
        tk, acc = [], ""
        for t in reversed(cand):              # the tokens that spell this segment, counted back from its end
            tk.insert(0, t)
            acc = ps.norm_script("".join(x["text"] for x in tk))
            if len(acc) >= len(tx):
                break
        if acc != tx:
            continue
        tf = tk[0]["start"]
        if not (0.2 < a0 - tf < 3.0):
            continue
        on = [x for x, y in runs if x <= tf + 0.15 and y >= tf - 0.05]
        return min(a0, (max(on) if on else tf) - pad)
    return a0


def build_plan(e, blocks, segs, takes, chosen, args):
    """Cut range (audio timeline) for the chosen take of every block."""
    alts_only = {}
    for s_ in getattr(args, "alts_only", []) or []:
        k_, _, v_ = s_.partition("=")
        alts_only[k_] = {int(x) for x in v_.split(",") if x.strip()}
    claps = detect_claps(e, args.clap_sensitivity)
    runs = voiced_runs(e, claps)
    edge = getattr(args, "edge_level", 0.22)
    runs_lo = voiced_runs(e, claps, level=edge)      # soft sounds (quiet laughter, breaths)
    db_env, _ = _db_series(e, FRAME)
    _fl, _pk = np.percentile(db_env, 10), np.percentile(db_env, 95)
    env = (db_env, _fl + max(8.0, edge * (_pk - _fl)))
    print("拍手らしい音: %d 個  %s" % (len(claps), " ".join(ft.mmss(c) for c in claps[:40])))
    all_starts = sorted(t["start"] for t in takes)
    plan = []
    caps = {}

    def one(bi, b, cap_override=None, take_override=None):
        c = (take_override, take_override["status"] if take_override["status"] in ("OK", "CHECK") else "NG") \
            if take_override else chosen.get(bi)
        if not c:
            print("  %s: no take found - skipped" % b["id"])
            return None
        take, status = c
        speech = [l for l in b["lines"] if l["kind"] == "speech" and l["norm"]]
        cue_chars = len(speech[0]["norm"]) if speech else 10
        nxt = [st_ for st_ in all_starts if st_ > take["start"] + 0.01]
        cap_after = nxt[0] if nxt else None      # never run into the next take
        cap_scan = cap_after                      # ad-lib scan stops at Whisper's next start
        if cap_override is not None:
            cap_after = cap_override              # the cut itself may run up to the real next start
        last = [l for l in b["lines"] if l["kind"] != "note"][-1]
        ends_laughing = last["kind"] == "laugh" or last.get("laugh_after")
        # does the talk right after the block run on into laughter? then it is kept as a whole
        chain_t, laugh_chain = take["end"], False
        for sg in segs:
            if sg["start"] < take["end"] - 0.05:
                continue
            if sg["start"] - chain_t > ADLIB_GAP or (cap_scan and sg["start"] >= cap_scan) \
                    or (take.get("restart_at") and sg["start"] >= take["restart_at"]):
                break
            if LAUGH_SEG_RE.match(sg["text"].strip()):
                laugh_chain = True
                break
            chain_t = max(chain_t, sg["end"])
        # ad-libs after the last script line: real utterances right after it, up to a redo remark
        speech_until, cur_t = None, take["end"]
        for sg in segs:
            if sg["start"] < take["end"] - 0.05:
                continue
            if sg["start"] - cur_t > ADLIB_GAP or (cap_scan and sg["start"] >= cap_scan) \
                    or (take.get("restart_at") and sg["start"] >= take["restart_at"]):
                break
            if sg["skip"]:
                continue
            if is_real_speech(sg):
                if not ends_laughing and not laugh_chain and sg["end"] - take["end"] > ADLIB_MAX:
                    break        # a block that ends on a spoken line: only a short ad-lib after it
                cur_t = max(cur_t, sg["end"])
                speech_until = sg["end"]
            else:
                break        # laughter, or Whisper's sparse text over a long stretch: ad-libs end here
        gap = getattr(args, "laugh_gap", 1.0) if ends_laughing else args.tail_gap
        # the block's last script line is a tiny reaction ("あ？") that Whisper did not write down:
        # it is still said right after the previous line, so look for it by sound
        tail_line = next((l for l in reversed(b["lines"]) if l["kind"] == "speech"), None)
        if tail_line and any(m_["line"] == tail_line["raw"] for m_ in take.get("minor", [])) \
                and not args.no_tail:
            speech_until = max(speech_until or 0.0, take["end"] + 1.6)
        # measured from the end of the last line / ad-lib (Whisper's end), not from the end of
        # the loud sound: speech and the laughter after it are often one merged sound
        clamp_to = None
        has_laugh = any(l["kind"] == "laugh" or l.get("laugh_after") or l.get("laugh_before")
                        for l in b["lines"])
        if has_laugh and not args.no_tail:
            clamp_to = max(take["end"], speech_until or 0.0) + args.max_tail
        # what follows the last heard script line after a real silence (>= 1 s) is the crew talking
        # ("おかしいな", "このまま行くよ"): the block ends at that line
        try:
            es_ = take.get("end_strict")
            if es_ and take["end"] - es_ > 1.5 and not args.no_tail:
                prev_ = None
                for r_ in runs:
                    if r_[1] <= es_ - 0.2:
                        prev_ = r_
                        continue
                    if prev_ is not None and r_[0] - prev_[1] >= 1.0 and r_[0] >= es_ - 0.2:
                        take = dict(take, end=es_)
                        speech_until = min(speech_until or es_, es_)
                        break
                    prev_ = r_
        except Exception:   # noqa: BLE001
            pass
        dbg = [] if args.debug else None
        s_a, e_a, ok = refine(take, runs, claps, cue_chars, cap_after,
                              args.pad_before, args.pad_after, x=e,
                              tail=not args.no_tail, tail_gap=gap,
                              max_tail=args.max_tail, env=env, runs_lo=runs_lo, debug=dbg,
                              speech_until=speech_until, clamp_to=clamp_to)
        try:        # the script ends with a slap / clap as an action line ("（叩）"): the block lasts until that sound has rung out
            dl_ = [l for l in b["lines"] if l["kind"] != "laugh"]
            if dl_ and dl_[-1]["kind"] == "direction" and re.search(r"叩|拍手|パン|はたく|ビンタ", dl_[-1]["raw"]):
                cl_ = [c_ for c_ in claps if e_a - 0.6 <= c_ <= e_a + 1.5]
                if cl_:
                    e_a = max(e_a, cl_[0] + 0.4)
        except Exception:   # noqa: BLE001
            pass
        try:
            if not os.environ.get("CHCUT_NO_LEADIN") and speech:
                import subtitles as _sbl
                _tk = getattr(args, "_tokens", None)
                if _tk is None:
                    _tk = args._tokens = _sbl.load_tokens(args.transcript)
                s_new = _lead_in_start(s_a, segs, _tk, speech[0]["norm"], runs_lo, args.pad_before)
                if s_new < s_a - 1e-6:
                    print("  %s: 台本の前の言い足しに合わせて頭を %.2f 秒前へ" % (b["id"], s_a - s_new))
                    s_a = s_new
        except Exception:   # noqa: BLE001
            pass
        if dbg is not None:
            print("[debug] %s の頭まわり（音量の塊 / 拍手）" % b["id"])
            print("\n".join(dbg))
        caps[bi] = cap_scan
        return {"block": b, "take": take, "status": status, "a0": s_a, "a1": e_a,
                "refined": ok, "speech_until": speech_until, "runs": runs_lo, "cap": cap_scan}

    for bi, b in enumerate(blocks):
        r = one(bi, b)
        if r:
            plan.append((bi, r))
    # Whisper often starts the next take early (inside the laughter of this one). Once the next
    # take's real start is known from the sound, let this block run up to it.
    for k in range(len(plan) - 1):
        bi, r = plan[k]
        nxt = plan[k + 1][1]
        cap = caps.get(bi)
        if cap is not None and nxt["a0"] > cap + 0.3 and nxt["a0"] > r["take"]["end"]:
            r2 = one(bi, blocks[bi], cap_override=nxt["a0"] - 0.05)
            if r2:
                plan[k] = (bi, r2)
    # the other takes of every block, as candidates the person can switch to in Final Cut (audition)
    if not getattr(args, "no_alts", False):
        for k, (bi, r) in enumerate(plan):
            others = [t for t in takes if t["block_index"] == bi and t is not r["take"]]
            alts = []
            for t in reversed(others):                    # newest first
                a = one(bi, blocks[bi], take_override=t)
                if a:
                    a["alt_no"] = 1 + sorted(x["start"] for x in takes if x["block_index"] == bi).index(t["start"])
                    alts.append(a)
            sel = alts_only.get(blocks[bi]["id"])
            if sel is not None:
                alts = [x for x in alts if x["alt_no"] in sel]
            r["alts"] = alts
    plan = [r for _, r in plan]

    return plan


# ---------------------------------------------------------------- main
def clipped_spans(audio_path, a0, a1, env=None, thr=0.97):
    """Stretches where mic B (channel 2) is distorted by clipping while mic A (channel 1) is not.
    Returns [(t0, t1)] in audio time, widened to the whole word (the sound dips on both sides)."""
    import subprocess
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % max(a0, 0), "-t", "%.3f" % (a1 - a0), "-i", str(audio_path),
                        "-vn", "-ac", "2", "-ar", "48000", "-f", "f32le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    x = np.frombuffer(r.stdout, dtype=np.float32)
    x = x[: len(x) // 2 * 2].reshape(-1, 2)
    if len(x) < 4800:
        return []
    sr = 48000
    def flat_runs(ch):
        hit = np.abs(x[:, ch]) >= thr
        d = np.diff(np.concatenate([[0], hit.astype(np.int8), [0]]))
        st, en = np.where(d == 1)[0], np.where(d == -1)[0]
        return [(a, b) for a, b in zip(st, en) if b - a >= 3]
    rb, ra = flat_runs(1), flat_runs(0)
    if len(rb) < 3:
        return []
    def cnt(runs, lo, hi):
        return sum(1 for a, b in runs if lo <= a < hi)
    win = int(0.05 * sr)
    flagged = []
    for k in range(0, len(x) - win, win // 2):
        if cnt(rb, k, k + win) >= 2 and cnt(ra, k, k + win) < 2:
            flagged.append(k / sr)
    if not flagged:
        return []
    groups = [[flagged[0], flagged[0] + 0.05]]
    for t in flagged[1:]:
        if t - groups[-1][1] <= 0.25:
            groups[-1][1] = t + 0.05
        else:
            groups.append([t, t + 0.05])
    out = []
    for g0, g1 in groups:
        g0, g1 = a0 + g0, a0 + g1
        if env is not None:                    # the whole word: out to where the level falls well below its peak
            pk = float(np.max(env[int(g0 * 100):int(g1 * 100) + 1]))
            i = int(g0 * 100)
            while i > int((g0 - 0.6) * 100) and env[i] > pk - 14:
                i -= 1
            j = int(g1 * 100)
            while j < min(int((g1 + 0.6) * 100), len(env) - 1) and env[j] > pk - 14:
                j += 1
            g0, g1 = i / 100.0, j / 100.0
        out.append((g0 - 0.03, g1 + 0.04))
    return out


def load_common_edits(res, args):
    """Edits that all the hand-made examples (お手本) have in common: the closing アイキャッチ, the 1-second zoom layer at the
    start and the BGM bed.  presets/お手本共通.xml holds their resources, taken from a finished project.  Returns a dict."""
    if getattr(args, "no_common_edits", False):
        return None
    pf = Path(__file__).parent / "presets" / "お手本共通.xml"
    if not pf.exists():
        return None
    root = ET.parse(pf).getroot()
    rs = root.find("resources")
    ids = {e.get("id") for e in rs}
    # The アイキャッチ already exists in the user's Final Cut library: do not ship a second copy of it (it would be
    # imported as a duplicate compound clip + clips).  The real one is placed (CHCUT_LOGO=0 to turn off).
    skip = set()
    if os.environ.get("CHCUT_LOGO") == "0":
        lg = root.find("logo")
        for e in rs:
            if e.tag == "media" and lg is not None and e.get("id") == lg.get("ref"):
                skip.add(e.get("id"))
                for el in e.iter():
                    r_ = el.get("ref")
                    if r_ and any(a.get("id") == r_ and a.tag == "asset" for a in rs):
                        skip.add(r_)
        keep = {el.get("ref") for k in ("zoom", "bgm") if root.find(k) is not None for el in [root.find(k)] + list(root.find(k).iter())}
        for k in ("zoom", "bgm"):
            if root.find(k) is not None:
                keep.add(root.find(k).get("ref"))
        for e in rs:
            if e.get("id") not in skip:
                for el in e.iter():
                    if el.get("ref"):
                        keep.add(el.get("ref"))
        skip = {i for i in skip if i not in keep or i == (lg.get("ref") if lg is not None else None)}
    _b = root.find("bgm")
    if _b is not None:
        skip.add(_b.get("ref"))             # BGM is not wanted
    for e in rs:
        if e.get("id") in skip:
            continue
        for el in e.iter():
            for a in ("id", "ref", "format", "effect"):
                if el.get(a) in ids:
                    el.set(a, "x" + el.get(a))
        res.append(e)
    cfg = {"logo": root.find("logo") if os.environ.get("CHCUT_LOGO") != "0" else None, "zoom": root.find("zoom"), "bgm": None}
    for k in cfg:
        if cfg[k] is not None:
            cfg[k].set("ref", "x" + cfg[k].get("ref"))
    return cfg


def _has_u(tb):
    return any(x.get("_u") for x in (tb or {}).get("pieces", []))


def merge_user_edits(auto, edited):
    """The fresh automatic telops of a block with only what the person changed by hand laid over them (a flag _u on each
    hand-changed piece: n = added by hand, t = time moved, x = line break changed, s = speaker changed, d = deleted).
    An edit is carried over only onto the piece with the same words at the same place; an edit that no longer fits the new
    analysis is dropped (the new analysis stands), so old text is never laid over different words.  Everything the person
    did not touch follows the new analysis."""
    import re as _re
    nz = lambda s: _re.sub(r"[\s\n、。！？!?…]", "", s or "")
    auto = [dict(a) for a in auto]
    used = set()

    def ov(a, e):
        o = min(a["end"], e["end"]) - max(a["start"], e["start"])
        return o / max(min(a["end"] - a["start"], e["end"] - e["start"]), 0.05)
    extra = []
    for e in sorted((x for x in edited if x.get("_u")), key=lambda x: x["start"]):
        fl = e["_u"]
        if "n" in fl:                                   # added by hand: kept when it does not sit on a new piece
            if (e.get("text") or "").strip() and not any(a.get("speaker", 0) >= 0 and e.get("speaker", 0) >= 0 and ov(a, e) > 0.3 for a in auto):
                extra.append(dict(e))
            continue
        best, bo = None, 0.4
        for i, a in enumerate(auto):
            if i in used or (a.get("speaker", 0) < 0) != (e.get("speaker", 0) < 0):
                continue
            o = ov(a, e)
            same = nz(a["text"]) == nz(e["text"]) or ("x" in fl and o > 0.8 and abs(a["start"] - e["start"]) < 0.15 and abs(a["end"] - e["end"]) < 0.15)
            if same and o > bo:
                best, bo = i, o
        if best is None:
            continue
        a = auto[best]
        if "x" in fl:
            import difflib as _dl0
            _na, _ne = nz(a["text"]), nz(e["text"])
            if not (_na == _ne or _ne in _na or _dl0.SequenceMatcher(None, _na, _ne).ratio() >= 0.8):
                continue                        # the hand edit is of other words than the new analysis has here: dropped
        used.add(best)
        if "d" in fl:
            a["_del"] = True
            continue
        if "x" in fl:
            import difflib as _dl
            na, ne = nz(a["text"]), nz(e["text"])
            if na == ne or ne in na or _dl.SequenceMatcher(None, na, ne).ratio() >= 0.8:
                a["text"] = e["text"]           # the same words (line break / a word dropped); other words are not laid over
        if "t" in fl:
            a["start"], a["end"] = e["start"], e["end"]
            if "s0" in a:
                a["s0"] = e["start"]
            if "s1" in a:
                a["s1"] = e["end"]
        if "s" in fl:
            a["speaker"] = e["speaker"]
        a["_u"] = fl
    res = [a for a in auto if not a.get("_del")] + extra
    for x in extra:
        x.setdefault("s0", x["start"])
        x.setdefault("s1", x["end"])
    lv = sorted((x for x in res if x.get("speaker", 0) >= 0 and not x.get("ov")), key=lambda x: x["start"])
    for x, y in zip(lv, lv[1:]):                       # a hand-moved piece must not run into the neighbour
        if x["end"] > y["start"] + 0.005 and y["start"] - x["start"] >= 0.1:
            x["end"] = y["start"]
            if "s1" in x:
                x["s1"] = min(x["s1"], y["start"])
    return sorted(res, key=lambda x: x["start"])


def mono_gain_db(path):
    """A recording that is really mono but stored with one channel quieter (e.g. left twice as loud as right): when only
    channel B is kept the voice gets quieter.  Returns the dB to add so the kept channel is as loud as the louder one."""
    try:
        p = subprocess.run(["ffmpeg", "-v", "error", "-t", "600", "-i", str(path), "-vn", "-ac", "2", "-ar", "8000", "-f", "f32le", "-"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        x = np.frombuffer(p.stdout, dtype=np.float32)
        x = x[: len(x) // 2 * 2].reshape(-1, 2)
        if len(x) < 8000 * 5:
            return 0.0
        ra, rb = float(np.sqrt((x[:, 0] ** 2).mean())), float(np.sqrt((x[:, 1] ** 2).mean()))
        if rb <= 0 or ra <= 0:
            return 0.0
        cor = float(np.corrcoef(x[::4, 0], x[::4, 1])[0, 1])
        if cor < 0.98:
            return 0.0                                    # two real microphones: leave them as they are
        d = 20 * np.log10(ra / rb)
        return float(min(max(d - 1.0, 0.0), 12.0)) if d > 1.5 else 0.0
    except Exception:   # noqa: BLE001
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("audio")
    ap.add_argument("transcript")
    ap.add_argument("script")
    ap.add_argument("--out")
    ap.add_argument("--pad-before", type=float, default=0.0)
    ap.add_argument("--subtitles", action="store_true", help="add subtitles as editable titles")
    ap.add_argument("--kerning", default="metrics", choices=["metrics", "optical", "mono"],
                    help="letter spacing of the telops: font metrics / optical (even gaps) / monospaced Japanese")
    ap.add_argument("--speakers-config", help="JSON: title template per speaker")
    ap.add_argument("--swap-speakers", action="store_true", help="exchange speaker 1 and 2")
    ap.add_argument("--reference", help="JSON of a finished edit's titles {'titles': [[start_s, dur_s, lane, text], ...]} "
                    "(timeline time); its telops replace the automatic ones")
    ap.add_argument("--no-auto-a", action="store_true", help="do not switch to mic A where mic B is distorted (clipped)")
    ap.add_argument("--extra-telops", help="JSON list of telops added by hand: [{t0, t1, text}] (audio time)")
    ap.add_argument("--no-diarize", action="store_true", help="one speaker only")
    ap.add_argument("--report-json", help="write what was found (takes, choices, cut ranges) to this file")
    ap.add_argument("--audio-preset", help="voice preset XML (default: presets/CH整音デフォ.xml)")
    ap.add_argument("--no-common-edits", action="store_true", help="skip the closing logo / start zoom / BGM that every お手本 has")
    ap.add_argument("--no-dual-mono", action="store_true", help="leave the audio channels as they are")
    ap.add_argument("--preset-only", help="debug: use only these preset filters, e.g. 1,3 (1-based)")
    ap.add_argument("--no-audio-preset", action="store_true", help="do not apply the voice preset")
    ap.add_argument("--telops-out", help="write the telops of every block to this JSON file (for the editor)")
    ap.add_argument("--title", help="name of the Final Cut project (same as the export file name)")
    ap.add_argument("--onsets", help="JSON list of seconds: where a mountain of the waveform starts (telop heads are put there)")
    ap.add_argument("--edited-out", help="write the hand-changed blocks (merged with the fresh analysis) here")
    ap.add_argument("--telops-in", help="use the telops from this (edited) JSON file instead of the automatic ones")
    ap.add_argument("--alts-only", action="append", default=[],
                    help="candidates of a block, e.g. --alts-only b09=1,3 (none: b09=)")
    ap.add_argument("--skip-blocks", default="", help="blocks left out of the cut, e.g. b03,b05")
    ap.add_argument("--no-alts", action="store_true", help="do not add the other takes as candidates")
    ap.add_argument("--no-snap", action="store_true", help="do not snap cuts to sound frames")
    ap.add_argument("--end-margin-frames", type=int, default=2)
    ap.add_argument("--pad-after", type=float, default=0.03)
    ap.add_argument("--clap-sensitivity", type=float, default=1.0,
                    help="higher = detect more claps (default 1.0)")
    ap.add_argument("--no-tail", action="store_true",
                    help="do not keep ad-libs/laughter after the last script line")
    ap.add_argument("--tail-gap", type=float, default=0.9,
                    help="silence (s) that ends the ad-lib/laughter tail (default 0.9)")
    ap.add_argument("--max-tail", type=float, default=2.5,
                    help="how long laughter is kept after the last line (s). Default 0.65, "
                         "measured from the finished edit IMG_4014 (laughter is cut about 0.65 s after the last line)")
    ap.add_argument("--laugh-gap", type=float, default=1.0,
                    help="how long after the last line laughter may start, when the script "
                         "ends with laughter (default 1.0; later laughter is left out)")
    ap.add_argument("--edge-level", type=float, default=0.22,
                    help="how quiet a sound still counts at clip edges, 0..1 (default 0.22; "
                         "lower = keeps quieter sounds, higher = cuts tighter)")
    ap.add_argument("--debug", action="store_true",
                    help="print the loudness runs around each take's start")
    ap.add_argument("--pick", action="append", default=[],
                    help="force a take, e.g. --pick b09=3")
    ap.add_argument("--video-uri-path", default=None,
                    help="path of the video AS THE MAC SEES IT, written into the XML "
                         "(use when this script runs in a VM that mounts the folder elsewhere)")
    ap.add_argument("--audio-uri-path", default=None,
                    help="same for the separate audio file")
    ap.add_argument("--offset", type=float, default=None,
                    help="use this sync offset (s) instead of measuring it (faster)")
    ap.add_argument("--no-block-se", action="store_true", help="do not put a sound effect at the head of each block")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    os.environ["CHCUT_KERN"] = args.kerning

    vpath = Path(args.video).expanduser().resolve()
    apath = Path(args.audio).expanduser().resolve()
    out = Path(args.out).expanduser() if args.out else Path.home() / "Desktop" / (vpath.stem + "_rough.fcpxml")

    # 1. sync -------------------------------------------------------------
    e = sa.load_mono(str(apath))
    if args.offset is not None:
        offset = Fraction(args.offset).limit_denominator(100000)
        print("sync offset: %+.4f s (指定された値を使用)" % float(offset))
    else:
        v = sa.load_mono(str(vpath))
        lag, conf = sa.gcc_phat(v, e)
        offset = Fraction(lag, sa.SR)          # video_time = audio_time + offset
        print("sync offset: %+.4f s  confidence %.2f (%s)" % (float(offset), conf, sa.quality_label(conf)))
        if conf < 1.8:
            # one peak over the whole recording can be blurred by a small drift: measure window by window
            win, offs = 40 * sa.SR, []
            for s0 in range(0, max(len(v) - win, 1), win):
                a_, b_ = max(0, s0 - 5 * sa.SR), min(len(e), s0 + win + 5 * sa.SR)
                if b_ - a_ < win // 2:
                    continue
                lg, cf = sa.gcc_phat(e[a_:b_], v[s0:s0 + win])
                if cf >= 2.5:
                    offs.append(((s0 - a_) - lg) / float(sa.SR))
            if len(offs) >= 3 and max(offs) - min(offs) < 0.15:
                med = sorted(offs)[len(offs) // 2]
                offset = Fraction(med).limit_denominator(100000)
                print("sync offset: %+.4f s (全体では不確かでしたが、区間ごとの測定が%d箇所そろいました)" % (float(offset), len(offs)))
            elif not args.force:
                sys.exit("Sync confidence is low - stopping (use --force to override).")

    # 2. takes ------------------------------------------------------------
    with open(args.script, encoding="utf-8") as f:
        _script_txt = f.read()
    blocks = ps.parse(_script_txt)
    try:
        import scriptcuts as _scx
        _scx.load(_script_txt)
    except ImportError:
        pass
    segs = ft.load_segments(args.transcript)
    takes = ft.find_takes(segs, blocks)
    chosen = ft.choose(takes, len(blocks), blocks, ft.parse_picks(args.pick))

    # 3. refine -----------------------------------------------------------
    plan = build_plan(e, blocks, segs, takes, chosen, args)

    if args.report_json:
        import json as _json
        cuts = {p["block"]["id"]: (p["a0"], p["a1"]) for p in plan}
        rep = {"offset": float(offset), "blocks": []}
        for bi, b in enumerate(blocks):
            ts = sorted([t for t in takes if t["block_index"] == bi], key=lambda t: t["start"])
            c = chosen.get(bi)
            no = (ts.index(c[0]) + 1) if c and c[0] in ts else None
            rep["blocks"].append({
                "id": b["id"], "cue": b["cue"], "chosen": no, "status": c[1] if c else None,
                "cut": cuts.get(b["id"]),
                "takes": [{"no": i + 1, "start": t["start"], "end": t["end"], "status": t["status"],
                           "missing": [m_["line"] for m_ in t["missing"]],
                           "maybe": [m_["line"] for m_ in t["maybe"]]} for i, t in enumerate(ts)]})
        Path(args.report_json).parent.mkdir(parents=True, exist_ok=True)
        with open(args.report_json, "w", encoding="utf-8") as f:
            _json.dump(rep, f, ensure_ascii=False, indent=1)

    # 4. media info ---------------------------------------------------------
    vinfo, ainfo = mf.probe(vpath), mf.probe(apath)
    vs, as_ = mf.first_stream(vinfo, "video"), mf.first_stream(ainfo, "audio")
    width, height = int(vs["width"]), int(vs["height"])
    if abs(mf.rotation_of(vs)) in (90, 270):
        width, height = height, width
    num, den = [int(x) for x in vs["r_frame_rate"].split("/")]
    fps = Fraction(num, den)
    fdur = Fraction(den, num)
    vdur = mf.duration_of(vinfo, vs)
    adur = mf.duration_of(ainfo, as_)
    arate = int(as_["sample_rate"])
    achan = int(as_.get("channels", 2))
    cam_a = mf.first_stream(vinfo, "audio")
    cam_rate = mf.AUDIO_RATES.get(int(cam_a["sample_rate"]), "48k") if cam_a else "48k"
    cam_chan = str(int(cam_a["channels"])) if cam_a else "2"

    # 5. clip list (video time, frame-aligned) ------------------------------
    clips, total = [], Fraction(0)
    label = {"OK": "成功", "CHECK": "要確認", "NG-only": "要確認(成功テイク無し)", "MANUAL": "手動指定", "NG": "失敗候補"}
    print()
    print("%-5s %-19s %-19s %-7s %s" % ("block", "Whisper range", "cut range", "length", "status"))
    def frames_for(p):
        v0 = Fraction(p["a0"]).limit_denominator(1000) + offset
        v1 = Fraction(p["a1"]).limit_denominator(1000) + offset
        v0 = max(v0, Fraction(0))
        v1 = min(v1, vdur)
        f0 = math.floor(v0 * fps)
        f1 = math.ceil(v1 * fps)
        if not args.no_snap:
            if p.get("_snap0", True):
                f0 = max(0, snap_start_frame(e, offset, float(fps), f0))
            if p.get("_snap1", True):
                f1 = min(int(vdur * fps), snap_end_frame(e, offset, float(fps), f1, args.end_margin_frames))
        return f0, f1

    # blocks left out, and the parts deleted in the app (a deleted telop cuts its time out of the clip)
    import json as _j0
    skip_b = {s for s in (args.skip_blocks or "").split(",") if s}
    tin0 = {}
    if args.telops_in and Path(args.telops_in).exists():
        tin0 = _j0.loads(Path(args.telops_in).read_text(encoding="utf-8"))
    pre_out, new_plan = {}, []
    for p in plan:
        bid = p["block"]["id"]
        if bid in skip_b:
            print("  %s: 使わないブロックなので外しました" % bid)
            continue
        t_ = tin0.get(bid)
        a0_o = p["a0"]
        p = dict(p, a0_orig=a0_o)
        if t_ and abs(t_.get("a0", -9) - a0_o) < 0.3 and ("c0" in t_ or "c1" in t_):      # the length of the block was changed in the timeline
            p = dict(p, a0=t_.get("c0", p["a0"]), a1=t_.get("c1", p["a1"]))
        if t_ and abs(t_.get("a0", -9) - a0_o) < 0.3:       # telops pulled out beyond the block: the clip follows them
            live_ = [x for x in t_["pieces"] if not x.get("deleted") and not x.get("cut") and x.get("speaker", 0) >= 0]
            if live_:
                hi_ = max(x.get("end", 0) for x in live_)
                lo_ = min(x["start"] for x in live_)
                if hi_ > p["a1"] + 0.05 and "c1" not in t_:      # a cut made by hand stays a cut, telops or not
                    p = dict(p, a1=hi_)
                if lo_ < p["a0"] - 0.05 and "c0" not in t_:
                    p = dict(p, a0=lo_)
        if t_ and abs(t_.get("a0", -9) - a0_o) < 0.3 and any(x.get("deleted") for x in t_["pieces"]):
            pcs = sorted(t_["pieces"], key=lambda x: x["start"])
            cuts = []
            for i_, x in enumerate(pcs):
                if not x.get("deleted"):
                    continue
                if x["speaker"] < 0:
                    cuts.append((x["start"], min(x["end"], p["a1"])))
                else:
                    nxt = next((y["start"] for y in pcs[i_ + 1:] if y["speaker"] >= 0), p["a1"])
                    cuts.append((x["start"], nxt))
            keep, cur = [], p["a0"]
            for c0, c1 in sorted(cuts):
                if c0 > cur:
                    keep.append((cur, c0))
                cur = max(cur, c1)
            if cur < p["a1"]:
                keep.append((cur, p["a1"]))
            keep = [(a_, b_) for a_, b_ in keep if b_ - a_ >= 0.2]
            pre_out[bid] = {"a0": t_["a0"], "a1": t_["a1"], "pieces": t_["pieces"], "notes": t_.get("notes", [])}
            for k_, (r0, r1) in enumerate(keep):
                p2 = dict(p)
                p2.update(a0=r0, a1=r1, _sub=True, _snap0=(k_ == 0 and r0 == p["a0"]), _snap1=(k_ == len(keep) - 1 and r1 == p["a1"]),
                          _pieces=[dict(x) for x in pcs if not x.get("deleted") and x["start"] < r1 and x["end"] > r0 - 0.05])
                if k_:
                    p2["alts"] = []
                new_plan.append(p2)
            if not keep:
                print("  %s: 全部消したので外しました" % bid)
        else:
            new_plan.append(p)
    plan = new_plan

    for p in plan:
        f0, f1 = frames_for(p)
        if f1 <= f0:
            print("  %s: outside the video - skipped" % p["block"]["id"])
            continue
        start = f0 * fdur
        dur = (f1 - f0) * fdur
        alt_clips = []
        for a in p.get("alts", []):
            g0, g1 = frames_for(a)
            if g1 > g0:
                alt_clips.append({"p": a, "start": g0 * fdur, "dur": (g1 - g0) * fdur})
        clips.append({"p": p, "start": start, "dur": dur, "at": total, "alts": alt_clips})
        total += dur
        print("%-5s %7s - %-9s %7s - %-9s %5.1fs  %s%s" % (
            p["block"]["id"], ft.mmss(p["take"]["start"]), ft.mmss(p["take"]["end"]),
            ft.mmss(p["a0"]), ft.mmss(p["a1"]), float(dur),
            label[p["status"]], "" if p["refined"] else "  (音量で絞れず・粗い範囲)"))
    print("\n合計 %.1f 秒（元素材 %.1f 秒）" % (float(total), float(vdur)))
    if not clips:
        sys.exit("No clips to write")

    # 6. FCPXML ---------------------------------------------------------------
    root = ET.Element("fcpxml", version=mf.FCPXML_VERSION)
    res = ET.SubElement(root, "resources")
    fd_str = "1001/%ds" % num if den == 1001 else "%d/%ds" % (den * 100, num * 100)
    fps_tag = {Fraction(30000, 1001): "2997", Fraction(24000, 1001): "2398",
               Fraction(60000, 1001): "5994"}.get(fps, str(int(fps)))
    color = "9-18-9 (Rec. 2020 HLG)" if vs.get("color_transfer") == "arib-std-b67" else "1-1-1 (Rec. 709)"
    ET.SubElement(res, "format", id="r1", name="FFVideoFormat%dx%dp%s" % (width, height, fps_tag),
                  frameDuration=fd_str, width=str(width), height=str(height), colorSpace=color)
    va = ET.SubElement(res, "asset", id="r2", name=vpath.stem, start="0s", duration=mf.t(vdur),
                       hasVideo="1", hasAudio="1", format="r1", audioSources="1",
                       audioChannels=cam_chan, audioRate=cam_rate)
    ET.SubElement(va, "media-rep", kind="original-media",
                  src=Path(args.video_uri_path).as_uri() if args.video_uri_path else vpath.as_uri())
    aa = ET.SubElement(res, "asset", id="r3", name=apath.stem, start="0s", duration=mf.t(adur),
                       hasAudio="1", audioSources="1", audioChannels=str(achan),
                       audioRate=mf.AUDIO_RATES[arate])
    ET.SubElement(aa, "media-rep", kind="original-media",
                  src=Path(args.audio_uri_path).as_uri() if args.audio_uri_path else apath.as_uri())

    se_cfg = None
    if not getattr(args, "no_block_se", False):
        import json as _jse
        sf = Path(__file__).parent / "presets" / "ブロック頭SE.json"
        if sf.exists():
            se_cfg = _jse.loads(sf.read_text(encoding="utf-8"))
            if se_cfg.get("有効", True):
                sa_ = ET.SubElement(res, "asset", id="r90", name=se_cfg["名前"], uid=se_cfg["uid"], start="0s",
                                    duration=se_cfg["長さ"], hasAudio="1", audioSources="1", audioChannels="2",
                                    audioRate=str(se_cfg.get("サンプルレート", "44100")))
                ET.SubElement(sa_, "media-rep", kind="original-media", sig=se_cfg["uid"], src=se_cfg["ファイル"])
            else:
                se_cfg = None
    se_done = set()
    common = load_common_edits(res, args)
    common_done = []

    sub_ctx = None
    if args.subtitles:
        import subtitles as sb
        cfg = sb.load_speakers_config(args.speakers_config)
        stereo = sb.load_stereo(str(apath))
        spk = sb.Speakers(stereo, segs, swap=args.swap_speakers, enabled=not args.no_diarize)
        spk.audio_path = str(apath)
        spk.extra = json.load(open(args.extra_telops, encoding='utf-8')) if getattr(args, 'extra_telops', None) else []
        print("話者の分け方: %s" % {"channels": "左右のマイクの音量差", "pitch": "声の高さ", "voice": "声の質(1本のマイク)", "single": "分けない(1人)"}[spk.mode])
        sub_ctx = {"cfg": cfg, "ids": sb.add_effects(res, cfg), "spk": spk,
                   "tokens": sb.load_tokens(args.transcript), "n": [0], "sb": sb}

    lib = ET.SubElement(root, "library")
    ev = ET.SubElement(lib, "event", name="CH Cut")
    proj = ET.SubElement(ev, "project", name=(args.title or vpath.stem))
    seq = ET.SubElement(proj, "sequence", format="r1", duration=mf.t(total), tcStart="0s",
                        tcFormat="NDF", audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(seq, "spine")
    audio_extra = []
    unheard_out, telops_out = {}, dict(pre_out)
    auto_keep, merged_blocks, edited_out = {}, set(), {}
    telops_in = {}
    onsets_ = []
    if getattr(args, "onsets", None) and Path(args.onsets).exists():
        try:
            onsets_ = json.loads(Path(args.onsets).read_text(encoding="utf-8"))
        except Exception:   # noqa: BLE001
            onsets_ = []
    if args.telops_in and Path(args.telops_in).exists():
        import json as _json2
        telops_in = _json2.loads(Path(args.telops_in).read_text(encoding="utf-8"))
    mono_db = mono_gain_db(apath)
    if mono_db:
        print("音声は左右が同じ1本のマイク（右が%.1f dB小さい）: 右だけ使うので +%.1f dB 足します" % (mono_db, mono_db))
    preset_filters = []
    if not args.no_audio_preset:
        pf = Path(args.audio_preset) if args.audio_preset else Path(__file__).parent / "presets" / "CH整音デフォ.xml"
        if pf.exists():
            pr = ET.parse(pf).getroot()
            idmap = {}
            for k, e in enumerate(pr.find("resources")):
                nid = "r%d" % (200 + k)
                idmap[e.get("id")] = nid
                e.set("id", nid)
                res.append(e)
            keep_i = {int(x) for x in args.preset_only.split(",")} if args.preset_only else None
            for i_, fa in enumerate(pr.find("filters"), 1):
                if keep_i and i_ not in keep_i:
                    continue
                fa.set("ref", idmap[fa.get("ref")])
                fa.attrib.pop("nameOverride", None)       # not in FCPXML 1.10 either
                for pm in fa.iter("param"):          # FCPXML 1.10 has no auxValue (newer exports do)
                    pm.attrib.pop("auxValue", None)
                preset_filters.append(fa)
        else:
            print("注意: 音声プリセットが見つかりません: %s" % pf)

    ref_titles = None
    if getattr(args, "reference", None) and Path(args.reference).exists():
        import json as _jr
        ref_titles = sorted(_jr.loads(Path(args.reference).read_text(encoding="utf-8"))["titles"], key=lambda x: x[0])

    def _from_reference(rt, c, at, offset, spk, auto):
        """Telops of a finished edit (timeline time) -> pieces in audio time for this clip."""
        t0 = Fraction(at)
        later = [Fraction(x["at"]) for x in clips if Fraction(x["at"]) > t0]
        t1 = min(later) if later else t0 + c["dur"]
        out = []
        for st, du, lane, tx in rt:
            if not (float(t0) - 0.002 <= st < float(t1) - 0.002):
                continue
            a = Fraction(st).limit_denominator(3000) - Fraction(at) + c["start"] - offset
            b = a + Fraction(du).limit_denominator(3000)
            a, b = float(a), float(b)
            if tx == "www":
                out.append({"start": a, "end": b, "s0": a, "s1": b, "text": tx, "speaker": -1, "ct": [a, b]})
            else:
                q0 = a
                if b - a > 0.5:
                    q0 += 0.35 * (b - a)
                out.append({"start": a, "end": b, "s0": a, "s1": b, "text": tx, "speaker": spk.who(q0, b), "ct": [a, b]})
        return out or auto

    def add_clip(parent, c, at, name, note):
        vc = ET.SubElement(parent, "asset-clip", ref="r2", offset=mf.t(at), name=name,
                           start=mf.t(c["start"]), duration=mf.t(c["dur"]), format="r1", tcFormat="NDF")
        ET.SubElement(vc, "adjust-volume", amount="-96dB")
        # separate audio, synced: audio_time = video_time - offset
        a_start = c["start"] - offset
        a_off = c["start"]
        a_len = c["dur"]
        if a_start < 0:
            shift = -a_start
            a_start, a_off, a_len = Fraction(0), a_off + shift, a_len - shift
        a_len = min(a_len, adur - a_start)
        if a_len > 0:
            ac = ET.SubElement(vc, "asset-clip", ref="r3", lane="-1", offset=mf.t(a_off), name=apath.stem,
                               start=mf.t(a_start), duration=mf.t(a_len), tcFormat="NDF", audioRole="dialogue")
            audio_extra.append(ac)
        pp = c["p"]
        is_main = pp.get("alt_no") is None
        bid = pp["block"]["id"]
        if se_cfg and is_main and bid not in se_done:             # a sound effect at the head of every block
            se_done.add(bid)
            se_dur = Fraction(se_cfg["長さ"].rstrip("s")).limit_denominator(1000000)
            se = ET.SubElement(vc, "asset-clip", ref="r90", lane="-2", offset=mf.t(c["start"]), name=se_cfg["名前"],
                               duration=mf.t(min(se_dur, c["dur"])) if se_dur > c["dur"] else se_cfg["長さ"],
                               audioRole="effects.effects-1")
            ET.SubElement(se, "adjust-volume", amount="%.4fdB" % se_cfg.get("音量dB", 0))
        if common and is_main and not common_done:                # first clip: the zoom layer and the BGM bed ride on it
            common_done.append(bid)
            z_ = common.get("zoom")
            if z_ is not None:
                ET.SubElement(vc, "title", ref=z_.get("ref"), lane=z_.get("lane", "10"), offset=mf.t(c["start"]),
                              name=z_.get("name"), start="3600s", duration=z_.get("duration", "1s"))
            b_ = common.get("bgm")
            if b_ is not None:
                b_len = Fraction(b_.get("duration").rstrip("s")).limit_denominator(1000000)
                b_st = Fraction(b_.get("start", "0")).limit_denominator(1000)
                b_dur = min(total, b_len - b_st)
                bg_ = ET.SubElement(vc, "asset-clip", ref=b_.get("ref"), lane="-3", offset=mf.t(c["start"]), name=b_.get("name"),
                                    start=mf.t(b_st), duration=mf.t(b_dur), audioRole="music")
                ET.SubElement(bg_, "adjust-volume", amount="%.4fdB" % float(b_.get("volume_dB", "-15")))
        if sub_ctx:
            if pp.get("_pieces") is not None:
                pieces = pp["_pieces"]
            elif is_main and bid in telops_in and abs(telops_in[bid].get("a0", -9) - pp.get("a0_orig", pp["a0"])) < 0.3 and not _has_u(telops_in[bid]):   # edited in the app
                pieces = [dict(x) for x in telops_in[bid]["pieces"] if not x.get("deleted") and not x.get("cut") and (x.get("text") or "").strip()]
            else:
                if os.environ.get("CHCUT_DUMP") and is_main:
                    _dump = json.load(open(os.environ["CHCUT_DUMP"])) if os.path.exists(os.environ["CHCUT_DUMP"]) else {}
                    _dump[bid] = {"a0": pp["a0"], "a1": pp["a1"], "cap": pp.get("cap"), "runs": pp.get("runs"), "block": pp["block"]}
                    json.dump(_dump, open(os.environ["CHCUT_DUMP"], "w"), ensure_ascii=False)
                pieces = sub_ctx["sb"].make_pieces(pp["a0"], pp["a1"], segs, sub_ctx["tokens"], pp["block"],
                                                   sub_ctx["spk"], runs=pp.get("runs"), until=pp.get("cap"))
                try:
                    import words as _wd
                    for x_ in pieces:
                        x_["text"] = _wd.apply(x_["text"])
                except ImportError:
                    pass
                if is_main and ref_titles is None and not os.environ.get("CHCUT_NO_EXACT"):
                    try:        # what was said as the script has it is written as the script has it (not as Whisper heard it)
                        import exact_script as _ex
                        pieces = _ex.exact_block(pieces, pp["block"], auto=True)
                    except Exception:   # noqa: BLE001
                        pass
                    try:        # the corrections made by hand in earlier projects (timeline etc.)
                        import learn_edits as _le
                        pieces = _le.apply(pieces, pp["a0"])
                    except Exception:   # noqa: BLE001
                        pass
                if is_main and ref_titles is None and not os.environ.get("CHCUT_NO_WORDCUT"):
                    try:        # never a cut inside a word
                        sub_ctx["sb"].fix_word_cuts(pieces)
                    except Exception as ex_:   # noqa: BLE001
                        print("fix_word_cuts:", ex_, file=sys.stderr)
                    try:        # where the person cut the telops by hand before
                        import learn_edits as _le2
                        pieces = _le2.apply_cuts(pieces)
                    except Exception as ex_:   # noqa: BLE001
                        print("apply_cuts:", ex_, file=sys.stderr)
                if is_main and ref_titles is None and onsets_ and not os.environ.get("CHCUT_NO_SNAPON"):
                    try:        # the head of a telop sits where the voice starts (as when it is put right by hand)
                        sub_ctx["sb"].snap_to_onsets(pieces, onsets_)
                    except Exception:   # noqa: BLE001
                        pass
                    if not os.environ.get("CHCUT_NO_ALIGNV"):
                        try:    # all heads together: onsets of the voice, with room for the letters
                            sub_ctx["sb"].align_starts(pieces, onsets_, pp["a0"], pp["a1"], hard=sub_ctx["sb"].silence_ends(getattr(sub_ctx["spk"], "env", None)), env=getattr(sub_ctx["spk"], "env", None))
                        except Exception as ex_:   # noqa: BLE001
                            print("align_starts:", ex_, file=sys.stderr)
                if is_main and ref_titles is None and not os.environ.get("CHCUT_NO_UNHEARD"):
                    try:        # a voice nobody heard: "（？）"
                        pieces = list(pieces) + sub_ctx["sb"].unheard_pieces(pieces, pp["a0"], pp["a1"], segs, pp.get("runs") or [], sub_ctx["spk"])
                        pieces.sort(key=lambda q: q["start"])
                        try:        # a voice nobody heard where the script has a line left over: that line is what was said
                            import exact_script as _ex2
                            for k_, q_ in enumerate(pieces):
                                if q_["text"] == "（？）":
                                    g_ = _ex2.gap_text(pieces, k_, pp["block"])
                                    mo_ = sum(sub_ctx["sb"]._mora_w(c) for c in (g_ or "") if not c.isspace())
                                    if g_ and 0.04 <= (q_["end"] - q_["start"]) / max(mo_, 1) <= 0.45:
                                        q_["text"] = g_
                        except Exception:   # noqa: BLE001
                            pass
                    except Exception:   # noqa: BLE001
                        pass
                if is_main and ref_titles is not None:
                    pieces = _from_reference(ref_titles, c, at, offset, sub_ctx["spk"], pieces)
            if is_main and pp.get("_pieces") is None and bid in telops_in and abs(telops_in[bid].get("a0", -9) - pp.get("a0_orig", pp["a0"])) < 0.3 and _has_u(telops_in[bid]):
                auto_keep[bid] = [dict(x) for x in pieces]
                pieces = merge_user_edits(pieces, telops_in[bid]["pieces"])
                merged_blocks.add(bid)
            if is_main:
                try:        # no telop starts with 、。; a doubled phrase that does not fit the time is written once
                    import scriptcuts as _scy
                    for _l_ in (pieces, auto_keep.get(bid) or []):
                        for x_ in _l_:
                            if x_.get("speaker", 0) >= 0 and x_["text"] != "www":
                                x_["text"] = _scy.finalize(_scy.collapse_repeat(x_["text"], x_["end"] - x_["start"]))
                except ImportError:
                    pass
                try:        # the same word written the same way everywhere (表記統一)
                    import words as _wd2
                    for x_ in pieces:
                        x_["text"] = _wd2.apply(x_["text"])
                except ImportError:
                    pass
            if is_main and not pp.get("_sub") and bid in merged_blocks:
                _pm = [x for x in pieces]
                pieces = auto_keep[bid]
                try:
                    import words as _wd3
                    for x_ in pieces:
                        x_["text"] = _wd3.apply(x_["text"])
                except ImportError:
                    pass
                edited_out[bid] = {"a0": pp["a0"], "a1": pp["a1"],
                                   "pieces": [dict({"start": round(x["start"], 3), "end": round(x["end"], 3), "text": x["text"], "speaker": int(x["speaker"])},
                                                  **({"alt": x["alt"], "src": x.get("src", "s")} if x.get("alt") and x.get("src") in ("s", "h") else {}),
                                                  **({"_u": x["_u"]} if x.get("_u") else {})) for x in _pm],
                                   "notes": telops_in[bid].get("notes", [])}
                for k_ in ("c0", "c1"):
                    if k_ in telops_in[bid]:
                        edited_out[bid][k_] = telops_in[bid][k_]
                pieces_for_titles = _pm
            else:
                pieces_for_titles = None
            if is_main and not pp.get("_sub"):
                telops_out[bid] = {"a0": pp["a0"], "a1": pp["a1"], "pieces": [
                    dict({"start": round(x["start"], 3), "end": round(x["end"], 3), "text": x["text"],
                          "speaker": int(x["speaker"])},
                         **({"alt": x["alt"], "src": x.get("src", "s")} if x.get("alt") and x.get("src") in ("s", "h") else {}),
                         **({"_u": x["_u"]} if x.get("_u") else {}))
                    for x in pieces],
                    "notes": [{"start": round(n_["start"], 3), "end": round(n_["end"], 3), "text": n_["text"],
                               "with_telop": n_["with_telop"]} for n_ in sub_ctx["sb"].place_notes(pieces, pp["block"])]}
            sub_ctx["sb"].add_titles(vc, (pieces_for_titles if pieces_for_titles is not None else pieces), sub_ctx["ids"], sub_ctx["cfg"],
                                     lambda t: Fraction(t).limit_denominator(1000) + offset, fps,
                                     sub_ctx["n"], lo=c["start"], hi=c["start"] + c["dur"], block=pp["block"],
                                     notes_in=(telops_in[bid].get("notes") if (is_main and bid in telops_in and "notes" in telops_in[bid]
                                               and abs(telops_in[bid].get("a0", -9) - pp.get("a0_orig", pp["a0"])) < 0.3) else None))
        ET.SubElement(vc, "marker", start=mf.t(c["start"]), duration=mf.t(fdur), value=note)
        if is_main:
            import subtitles as _sb
            found = _sb.find_unheard(pp["a0"], pp["a1"], segs, pp.get("runs") or [])
            unheard_out[bid] = unheard_out.get(bid, []) + [[round(a, 3), round(b, 3)] for a, b in found]
            for a_, b_ in found:
                vt = Fraction(a_).limit_denominator(1000) + offset
                if c["start"] <= vt < c["start"] + c["dur"]:
                    ET.SubElement(vc, "marker", start=mf.t(vt), duration=mf.t(fdur),
                                  value="聞き取れず？ 台本にない声があるかも（%s〜）" % ("%d:%04.1f" % (int(a_ // 60), a_ % 60)))
        # audio rules: dual mono, channel B only (A while laughing); then the voice preset
        laugh_iv = []
        if sub_ctx and is_main:
            _sbm, _spk = sub_ctx["sb"], sub_ctx["spk"]
            _pl = sorted(pieces, key=lambda q: q["start"])
            for w_ in _pl:
                if w_["text"] != "www":
                    continue
                # the laughter begins where its waveform begins (not on a frame line)
                on = _sbm._rise_onset(_spk, w_["start"], 0.10, 0.10, deepest=True)
                la = (on if on is not None else w_["start"]) - 0.02
                nxt = [q for q in _pl if q["text"] != "www" and q["start"] > w_["start"] + 0.05]
                if nxt and nxt[0]["start"] - w_["start"] < 3.0:
                    tn = nxt[0]["start"]
                    on2 = _sbm._rise_onset(_spk, tn, 0.30, 0.06)
                    lb = (on2 - 0.04) if (on2 is not None and abs(on2 - tn) < 0.30) else tn - 0.10   # no gap: the voice may start earlier than the title
                    kn = nxt[0].get("speaker", -1)
                    if kn >= 0:                  # the next speaker's own voice may already be there before the title
                        tt = max(tn - 0.35, w_["start"] + 0.2)
                        while tt < tn - 0.02:
                            if _spk.who(tt, tt + 0.1) == kn:
                                lb = min(lb, tt - 0.04)
                                break
                            tt += 0.01
                else:
                    lb = w_["end"]
                if lb > la + 0.05:
                    laugh_iv.append((Fraction(la).limit_denominator(2000) + offset, Fraction(lb).limit_denominator(2000) + offset))
        if sub_ctx and is_main and not getattr(args, "no_auto_a", False):
            try:
                for c0, c1 in clipped_spans(apath, pp["a0"], pp["a1"], getattr(sub_ctx["spk"], "env", None)):
                    laugh_iv.append((Fraction(c0).limit_denominator(2000) + offset, Fraction(c1).limit_denominator(2000) + offset))
                if laugh_iv:
                    laugh_iv.sort()
                    merged_iv = [list(laugh_iv[0])]
                    for l0_, l1_ in laugh_iv[1:]:
                        if l0_ <= merged_iv[-1][1] + Fraction(1, 20):
                            merged_iv[-1][1] = max(merged_iv[-1][1], l1_)
                        else:
                            merged_iv.append([l0_, l1_])
                    laugh_iv[:] = [tuple(m_) for m_ in merged_iv]
            except Exception as ex_:   # noqa: BLE001
                print("音割れの検出をスキップ:", ex_)
        def _frac(v):
            v = v.rstrip("s")
            return Fraction(v)
        def _q(x):
            return x
        for ac in audio_extra:
            o0 = _frac(ac.get("offset"))
            d0 = _frac(ac.get("duration"))
            s0_ = _frac(ac.get("start"))
            cuts = [(o0, False)]
            for l0, l1 in sorted(laugh_iv):
                l0, l1 = max(l0, o0), min(l1, o0 + d0)
                if l0 - o0 < 0.05:
                    l0 = o0
                if o0 + d0 - l1 < 0.05:
                    l1 = o0 + d0
                if l1 - l0 >= fdur:
                    cuts.append((l0, True))
                    cuts.append((l1, False))
            cuts.append((o0 + d0, None))
            parts = []
            for (t_a, flag), (t_b, _f) in zip(cuts, cuts[1:]):
                if t_b - t_a > 0:
                    parts.append((t_a, t_b, flag))
            tiny = [k for k, (x, y, f_) in enumerate(parts) if y - x < 0.02]
            for k in reversed(tiny):                       # no sliver clips: join them to a neighbour
                x, y, f_ = parts[k]
                if k > 0:
                    px, py, pf = parts[k - 1]
                    parts[k - 1] = (px, y, pf)
                elif len(parts) > 1:
                    nx, ny, nf = parts[1]
                    parts[1] = (x, ny, nf)
                parts.pop(k)
            idx = list(vc).index(ac)
            vc.remove(ac)
            for k_, (t_a, t_b, flag) in enumerate(parts):
                nc = ET.Element("asset-clip", ref=ac.get("ref"), lane="-1", offset=mf.t(t_a), name=ac.get("name"),
                                start=mf.t(s0_ + (t_a - o0)), duration=mf.t(t_b - t_a), tcFormat="NDF", audioRole="dialogue")
                if achan >= 2 and not args.no_dual_mono:
                    c1 = ET.SubElement(nc, "audio-channel-source", srcCh="1", role="dialogue.dialogue-1")
                    c2 = ET.SubElement(nc, "audio-channel-source", srcCh="2", role="dialogue.dialogue-2")
                    (c2 if flag else c1).set("active", "0")
                    if mono_db > 0 and not flag:               # (intrinsic params come before the channel sources)
                        nc.insert(0, ET.Element("adjust-volume", amount="%.2fdB" % mono_db))
                for fa in preset_filters:
                    nc.append(copy.deepcopy(fa))
                vc.insert(idx + k_, nc)
        audio_extra.clear()
        return vc

    for c in clips:
        p = c["p"]
        name = "%s %s" % (p["block"]["id"], p["block"]["cue"][:12])
        note = "%s %s" % (p["block"]["id"], label[p["status"]])
        if c["alts"]:
            # an audition: the chosen take first, the other takes as alternatives to switch to
            holder = ET.SubElement(spine, "audition", offset=mf.t(c["at"]))
        else:
            holder = spine
        add_clip(holder, c, c["at"], name, note)
        for a in c["alts"]:
            ap_ = a["p"]
            add_clip(holder, a, c["at"], "%s テイク%d %s" % (ap_["block"]["id"], ap_["alt_no"], label[ap_["status"]]),
                     "%s テイク%d %s" % (ap_["block"]["id"], ap_["alt_no"], label[ap_["status"]]))

    if common and common.get("logo") is not None:                 # the closing アイキャッチ
        lg_ = common["logo"]
        lg_dur = Fraction(lg_.get("duration").rstrip("s")).limit_denominator(1000000)
        ET.SubElement(spine, "ref-clip", ref=lg_.get("ref"), offset=mf.t(total), name=lg_.get("name"),
                      duration=mf.t(lg_dur))
        seq.set("duration", mf.t(total + lg_dur))
    if args.report_json and Path(args.report_json).exists():
        import json as _json3
        rep = _json3.loads(Path(args.report_json).read_text(encoding="utf-8"))
        for b_ in rep["blocks"]:
            b_["unheard"] = unheard_out.get(b_["id"], [])
        Path(args.report_json).write_text(_json3.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.edited_out and telops_in:
        import json as _json5
        keep_ = {k_: v_ for k_, v_ in telops_in.items() if k_ not in merged_blocks}
        keep_ = {k_: v_ for k_, v_ in keep_.items() if k_ not in telops_out or not _has_u(v_)}
        keep_.update(edited_out)
        Path(args.edited_out).write_text(_json5.dumps(keep_, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.telops_out:
        import json as _json4
        Path(args.telops_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.telops_out).write_text(_json4.dumps(dict(sorted(telops_out.items())), ensure_ascii=False, indent=1), encoding="utf-8")
    if unheard_out and any(unheard_out.values()):
        print("聞き取れなかった声がありそうな場所:")
        for k_, v_ in unheard_out.items():
            for a_, b_ in v_:
                print("  %s  %d:%04.1f - %d:%04.1f（音声の時間）" % (k_, int(a_ // 60), a_ % 60, int(b_ // 60), b_ % 60))
    for el_ in root.iter():              # FCPXML 1.10 (the version written here) has neither of these (newer exports do)
        el_.attrib.pop("auxValue", None)
        el_.attrib.pop("nameOverride", None)
    for t_ in root.iter("title"):         # title DTD: no adjust-colorConform; fixed child order
        for c_ in [c for c in t_ if c.tag == "adjust-colorConform"]:
            t_.remove(c_)
        _o = ["param", "text", "text-style-def", "note", "object-tracker", "adjust-crop", "adjust-corners",
              "adjust-conform", "adjust-transform", "adjust-blend", "adjust-stabilization",
              "adjust-rollingShutter", "adjust-360-transform", "adjust-reorient", "adjust-orientation",
              "adjust-cinematic"]
        _k = lambda c: _o.index(c.tag) if c.tag in _o else (len(_o) if c.tag not in ("marker", "chapter-marker", "rating", "keyword", "analysis-marker", "filter-video", "filter-video-mask", "filter-audio", "metadata") else len(_o) + 1)
        _ch = list(t_)
        for c_ in _ch:
            t_.remove(c_)
        for c_ in sorted(_ch, key=_k):
            t_.append(c_)
    ET.indent(root, space="  ")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        f.write(ET.tostring(root, encoding="utf-8"))
    print("written:", out)


if __name__ == "__main__":
    main()
