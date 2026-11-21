#!/usr/bin/env python3
"""
CH Cut - subtitles and speaker separation

* who is speaking: with a stereo recording where each person has their own microphone the two
  channels differ in level (the closer microphone is louder), so the level difference between the
  channels of every phrase separates the speakers.  Without that, the pitch of the voice is used.
* what is written: the script's own wording where Whisper's text matches a script line, otherwise
  what Whisper heard (ad-libs).  Text is cut into short phrases of at most two lines.
* output: editable Final Cut titles (<title>), one per phrase, using a title template per speaker.

Used by make_rough_cut.py (--subtitles).
"""
import difflib
import json
import os
import sys
from pathlib import Path
import re
import subprocess
from fractions import Fraction

import numpy as np

import find_takes as ft

SR = 16000
LAUGH_RE_TXT = r"^(?=.*[はひふへほハヒフヘホwｗ笑])[あはひふへほっハヒフヘホwｗ笑!！、。…\s]+$"   # no "?": "あ？" is speech
STOP_RE = re.compile(r"[、。，．,.!！?？…]")
END_KEEP = "！？!?"
PARTICLES = "はがをにでとものてしるたいねよかだ"
BASIC_TITLE_UID = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
DEFAULT_SPEAKERS = [
    {"name": "話者1", "effect": "Basic Title", "uid": BASIC_TITLE_UID, "color": "1 1 1 1"},
    {"name": "話者2", "effect": "Basic Title", "uid": BASIC_TITLE_UID, "color": "1 0.85 0.25 1"},
]


# the note title until the real 【CH】注釈 template is known: a plain title above the telops
NOTE_FALLBACK = {"name": "注釈", "effect": "Basic Title", "uid": BASIC_TITLE_UID, "note": True,
                 "size": 56, "color": "1 0.9 0.3 1",
                 "params": [{"name": "Position", "key": "9999/999166631/999166633/1/100/101", "value": "0 330"}]}


# ------------------------------------------------------------------ audio / speakers
def load_stereo(path):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", str(SR),
                        "-f", "f32le", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0 or not p.stdout:
        return None
    x = np.frombuffer(p.stdout, dtype=np.float32)
    return x[: len(x) // 2 * 2].reshape(-1, 2).copy()


def channel_diff(x, t0, t1):
    """Level difference L - R (dB) over the louder half of the frames of [t0, t1]."""
    s = x[int(t0 * SR):int(t1 * SR)]
    n = 160
    m = len(s) // n
    if m < 3:
        return None
    e = (s[:m * n].reshape(m, n, 2) ** 2).mean(axis=1)
    tot = e.sum(axis=1)
    sel = tot >= np.median(tot)
    if not sel.any() or tot[sel].mean() < 1e-9:
        return None
    return 10 * np.log10(e[sel, 0].mean() + 1e-12) - 10 * np.log10(e[sel, 1].mean() + 1e-12)


def pitch(x, t0, t1):
    """Median pitch (Hz) of the voiced frames of mono x between t0 and t1 (None if unvoiced)."""
    s = x[int(t0 * SR):int(t1 * SR)]
    win, hop = int(0.04 * SR), int(0.02 * SR)
    lo, hi = int(SR / 400), int(SR / 70)
    vals = []
    for a in range(0, len(s) - win, hop):
        f = s[a:a + win] - s[a:a + win].mean()
        if (f ** 2).mean() < 1e-7:
            continue
        ac = np.correlate(f, f, "full")[win - 1:]
        if ac[0] <= 0:
            continue
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] / ac[0] > 0.45:
            vals.append(SR / k)
    return float(np.median(vals)) if len(vals) >= 3 else None


def kmeans_1d(v, iters=30):
    v = np.asarray(v, dtype=float)
    c = np.array([np.percentile(v, 15), np.percentile(v, 85)])
    a = np.zeros(len(v), dtype=int)
    for _ in range(iters):
        a = np.abs(v[:, None] - c[None]).argmin(axis=1)
        nc = np.array([v[a == k].mean() if (a == k).any() else c[k] for k in (0, 1)])
        if np.allclose(nc, c):
            break
        c = nc
    return c, a


def _fuse_timbre(fr, d, thr, speech, lab):
    """Who is talking, from the level difference between the channels AND the sound of the voice (the
    spectrum seen by each microphone, learned from the frames where the level difference is clear)."""
    m, hop = fr.shape[0], fr.shape[1]
    w = np.hanning(hop)[None, :, None]
    sp = np.abs(np.fft.rfft(fr * w, axis=1)) ** 2
    hz = np.fft.rfftfreq(hop, 1.0 / SR)
    edges = np.geomspace(80, 7500, 25)
    feats = []
    for c in (0, 1):
        b = np.stack([sp[:, (hz >= edges[i]) & (hz < edges[i + 1]), c].sum(axis=1) for i in range(24)], axis=1)
        b = 10 * np.log10(b + 1e-12)
        feats.append(b - b.mean(axis=1, keepdims=True))
    F = np.concatenate(feats, axis=1)
    dd = np.abs(d - thr)
    cm = speech & (dd > np.percentile(dd[speech], 40))
    y = lab
    if (y[cm] == 0).sum() < 30 or (y[cm] == 1).sum() < 30:
        return lab
    X, yy = F[cm], y[cm]
    mu0, mu1 = X[yy == 0].mean(0), X[yy == 1].mean(0)
    Sw = np.cov(X[yy == 0].T) + np.cov(X[yy == 1].T) + np.eye(X.shape[1]) * 1e-3
    wv = np.linalg.solve(Sw, mu1 - mu0)
    sc = F @ wv - wv @ (mu0 + mu1) / 2
    zs = sc / (sc[speech].std() + 1e-9)
    zd = -(d - thr) / (d[speech].std() + 1e-9)
    return np.where(zd + zs > 0, 1, 0)


class Speakers:
    """Decides which of the two speakers is talking at any moment (0 or 1).

    Every 0.1 s window of the whole recording gets a feature (channel level difference, or pitch);
    two clusters are found, the labels are smoothed, and silences take the neighbouring label."""

    HOP = 0.1

    def __init__(self, stereo, segs=None, swap=False, enabled=True):
        self.mode, self.swap, self.labels, self.n_dur = "single", swap, None, 0.0
        self.speech = None
        self.env = None
        self.mono = None
        self.audio_path = None
        if stereo is not None:
            self.mono = stereo.mean(axis=1)
        if stereo is not None:
            h = int(0.01 * SR)
            k = len(stereo) // h
            self.env = 10 * np.log10((stereo[:k * h].mean(axis=1).reshape(k, h) ** 2).mean(axis=1) + 1e-12)
        if stereo is None or not enabled:
            return
        hop = int(self.HOP * SR)
        m = len(stereo) // hop
        self.n_dur = m * self.HOP
        fr = stereo[:m * hop].reshape(m, hop, 2)
        e = (fr ** 2).mean(axis=1) + 1e-12                     # m x 2
        tot = 10 * np.log10(e.sum(axis=1))
        noise = np.percentile(tot, 10)
        speech = tot > noise + 18
        self.speech = speech
        # features are measured over 0.3 s so that they are steady
        w = 3
        e_s = np.stack([np.convolve(e[:, c], np.ones(w) / w, mode="same") for c in (0, 1)], axis=1)
        d = np.clip(10 * np.log10(e_s[:, 0]) - 10 * np.log10(e_s[:, 1]), -30, 30)
        lab = None
        if speech.sum() >= 40:
            c, a = kmeans_1d(d[speech])
            sizes = [(a == 0).mean(), (a == 1).mean()]
            if abs(c[1] - c[0]) >= 5.0 and min(sizes) >= 0.08:
                self.mode, thr = "channels", float(c.mean())
                lab = np.where(d >= thr, 0, 1)                   # larger L-R = speaker 1
                try:
                    lab = _fuse_timbre(fr, d, thr, speech, lab)
                except Exception:   # noqa: BLE001
                    pass
        if lab is None:
            try:                                      # one microphone / a mono recording: the sound (timbre) of the voices
                lab = _voice_labels(stereo.mean(axis=1), m, speech)
                if lab is not None:
                    self.mode = "voice"
            except Exception:   # noqa: BLE001
                lab = None
        if lab is None:
            mono = stereo.mean(axis=1)
            f = np.full(m, np.nan)
            win = int(0.3 * SR)
            for i in np.where(speech)[0]:
                a0 = int((i + 0.5) * hop - win // 2)
                if a0 >= 0:
                    p = pitch(mono, a0 / SR, (a0 + win) / SR)
                    if p:
                        f[i] = np.log(p)
            ok = ~np.isnan(f)
            if ok.sum() >= 40:
                c, a = kmeans_1d(f[ok])
                sizes = [(a == 0).mean(), (a == 1).mean()]
                if c[1] - c[0] >= np.log(1.25) and min(sizes) >= 0.15:
                    self.mode = "pitch"
                    lab = np.where(f >= c.mean(), 1, 0)          # higher voice = speaker 2
                    lab = np.where(ok, lab, -1)
        if lab is None:
            self.mode = "single"
            return
        lab = np.where(speech | (self.mode == "pitch"), lab, -1) if self.mode == "channels" else lab
        lab = np.where(speech, lab, -1)
        # smooth: a label needs 0.3 s to count; fill silences from the nearest speech label
        out = lab.copy()
        for i in range(m):
            if out[i] == -1:
                continue
            seg = lab[max(0, i - 2):i + 3]
            seg = seg[seg >= 0]
            if len(seg):
                out[i] = int(round(seg.mean() + 1e-9)) if seg.mean() not in (0.5,) else lab[i]
        last = -1
        for i in range(m):
            if out[i] >= 0:
                last = out[i]
            elif last >= 0:
                out[i] = last
        nxt = -1
        for i in range(m - 1, -1, -1):
            if out[i] >= 0:
                nxt = out[i]
            elif nxt >= 0:
                out[i] = nxt
        self.labels = out

    def onset(self, t, back=0.15, fwd=0.45, min_pause=0.07):
        """If a real pause (not a breath) lies near t, the moment the voice starts again after it; else t.
        Whisper's phrase starts are rough, the pause between two phrases is not."""
        if self.env is None:
            return t
        i0, i1 = max(int((t - back) / 0.01), 0), min(int((t + fwd) / 0.01), len(self.env))
        w = self.env[i0:i1]
        if len(w) < 10:
            return t
        thr = w.max() - 28
        quiet = w < thr
        best, best_d = None, 1e9
        i = 0
        while i < len(w):
            if quiet[i]:
                j = i
                while j < len(w) and quiet[j]:
                    j += 1
                if (j - i) * 0.01 >= min_pause and j < len(w):       # ends inside the window: voice comes back
                    d = abs((i0 + j) * 0.01 - t)
                    if d < best_d:
                        best, best_d = (i0 + j) * 0.01, d
                i = j
            else:
                i += 1
        return best if best is not None else t

    def onset_far(self, t, s1, run=0.5, maxfwd=1.4):
        """A phrase start that sits inside a long silence (Whisper puts it at the end of the previous line):
        the moment the voice comes back, up to maxfwd later.  Else t."""
        if self.env is None:
            return t
        sm = np.convolve(self.env, np.ones(5) / 5.0, mode="same")
        i0 = int(t * 100)
        n1 = i0 + int(maxfwd * 100) + 60
        if n1 >= len(sm):
            return t
        ref = float(np.max(sm[i0:n1]))
        thr = ref - 20.0
        if float(np.max(sm[i0 + 3:i0 + 3 + int(run * 100)])) >= thr:
            return t                                     # not a clear silence from here on
        thr = min(thr, float(np.min(sm[i0 + 3:i0 + 3 + int(run * 100)])) + 22.0)     # just above the silence floor
        j = i0 + 3
        while j < n1 and sm[j] < thr:
            j += 1
        on = j / 100.0 - 0.07
        return on if (on - t) <= maxfwd and on < s1 - 0.4 else t

    def at(self, t):
        if self.labels is None:
            return 0
        i = min(max(int(t / self.HOP), 0), len(self.labels) - 1)
        k = int(self.labels[i])
        k = 0 if k < 0 else k
        return 1 - k if self.swap else k

    def who(self, t0, t1):
        if self.labels is None:
            return 0
        i0, i1 = int(t0 / self.HOP), max(int(t0 / self.HOP) + 1, int(t1 / self.HOP))
        seg = self.labels[i0:i1]
        if self.speech is not None:                  # only moments with a voice vote, not the silences
            vm = self.speech[i0:i1]
            if vm.any():
                seg = seg[vm[:len(seg)]]
        seg = seg[seg >= 0]
        k = int(round(seg.mean())) if len(seg) else 0
        return 1 - k if self.swap else k


# ------------------------------------------------------------------ text
def load_tokens(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("tokens", [])


def _mora_w(ch):
    """How long a character takes to say, roughly (kanji are usually two kana, little ゃゅょ are part of the one before)."""
    if ch in "ゃゅょャュョぁぃぅぇぉ":
        return 0.3
    if ch in "っッ":
        return 0.7
    if "\u4e00" <= ch <= "\u9fff":
        return 2.0
    if ch.isdigit():
        return 2.0
    if ch in "、。，．,.!！?？…・「」":
        return 0.2
    return 1.0


def _char_stream(seg, tokens, runs, a0, a1):
    """[(char, time)] for one Whisper segment.  Whisper's own token times are rough, so the
    characters are spread over the stretches of the segment where someone is actually talking."""
    text = [ch for ch in seg["text"] if not ch.isspace()]
    if not text:
        return []
    lo, hi = max(seg["start"], a0 - 0.3), min(seg["end"], a1 + 0.3)
    act = []
    for s_, e_ in (runs or []):
        s2, e2 = max(s_, max(seg["start"], a0)), min(e_, seg["end"])
        if e2 - s2 > 0.03:
            act.append((s2, e2))
    if not act:
        a, b = max(seg["start"], a0), seg["end"]
        if b <= a:
            return []
        ws_ = [_mora_w(c_) for c_ in text]
        tw_, cw_, o_ = sum(ws_), 0.0, []
        for ch, w_ in zip(text, ws_):
            o_.append((ch, a + (b - a) * (cw_ + w_ / 2) / tw_))
            cw_ += w_
        return o_
    long_ = [(a_, b_) for a_, b_ in act if b_ - a_ >= 0.08]
    if long_ and sum(b_ - a_ for a_, b_ in long_) >= 0.1:
        act = long_                      # drop little scraps of the neighbouring line at the segment's edge
    total = sum(e_ - s_ for s_, e_ in act)
    out = []
    ws_ = [_mora_w(c_) for c_ in text]
    tw_, cw_ = sum(ws_), 0.0
    for i, ch in enumerate(text):
        pos = total * (cw_ + ws_[i] / 2) / tw_
        cw_ += ws_[i]
        acc = 0.0
        for s_, e_ in act:
            if pos <= acc + (e_ - s_):
                out.append((ch, s_ + (pos - acc)))
                break
            acc += e_ - s_
    return out


RULES_FILE = Path(__file__).resolve().parent / "presets" / "telop_rules.json"
_DEFAULT_RULES = {"break_min_chars": 8, "max_title_chars": 18, "line_max": 11, "first_line_longer_by": 0.5,
                  "break_after": {}, "drop_chars": ["。", "．"], "pause_cut": True}


def load_rules(path=None):
    """The telop rules: what was learned from finished projects (learn_telop_rules.py), with the
    hand-written "manual" part on top."""
    rules = dict(_DEFAULT_RULES)
    try:
        d = json.loads(Path(path or RULES_FILE).read_text(encoding="utf-8"))
        rules.update(d.get("learned", {}))
        rules.update(d.get("manual", {}))
    except Exception:   # noqa: BLE001
        pass
    return rules


RULES = load_rules()

EX_FILE = Path(__file__).resolve().parent / "presets" / "break_examples.json"
_EX = {"telop": {}, "full": {}, "pb": {}, "pk": {}}


def load_examples(path=None):
    """Telop breaks made by hand in the app's "改行テスト": the exact sentences, and for every pair of neighbouring
    characters how often a break was put between them (pb) and how often it was not (pk)."""
    ex = {"telop": {}, "full": {}, "pb": {}, "pk": {}}
    try:
        d = json.loads(Path(path or EX_FILE).read_text(encoding="utf-8"))
        for e in d.get("examples", []):
            tel = [[str(l) for l in t] for t in e.get("telops", []) if t]
            if not tel:
                continue
            full = "".join("".join(t) for t in tel)
            ex["full"][full] = tel
            for t in tel:
                ex["telop"]["".join(t)] = t
            chars = []
            kinds = []                                   # kind of the boundary after chars[k]: 0 keep, 1 line, 2 telop
            for ti, t in enumerate(tel):
                for li, l in enumerate(t):
                    for ci, ch in enumerate(l):
                        chars.append(ch)
                        last_c = ci == len(l) - 1
                        kinds.append(0 if not last_c else (1 if li < len(t) - 1 else 2))
            for k in range(len(chars) - 1):
                key = chars[k] + chars[k + 1]
                tgt = ex["pb"] if kinds[k] else ex["pk"]
                tgt[key] = tgt.get(key, 0) + 1
    except Exception:   # noqa: BLE001
        pass
    return ex


_EX = load_examples()


def _pair_bonus(a, b):
    """What the hand-made examples say about a break between these two characters (+ for it, - against it)."""
    k = a + b
    return 1.5 * min(_EX["pb"].get(k, 0), 3) - 1.5 * min(_EX["pk"].get(k, 0), 3)


def _cut_piece(p, cut, speakers=None):
    n = len(p["text"].replace("\n", ""))
    s0, s1 = p.get("s0", p["start"]), p.get("s1", p["end"])
    ct = p.get("ct") or []
    tm = ct[cut] if len(ct) >= n else s0 + (s1 - s0) * cut / float(n)
    tm = _clamp_cut(_refine_cut(speakers, tm, s0, s1), p, cut / float(n))
    t = p["text"].replace("\n", "")
    a = dict(p, text=t[:cut], end=tm, s1=tm)
    b = dict(p, text=t[cut:], start=tm, s0=tm)
    if len(ct) >= n:
        a["ct"], b["ct"] = ct[:cut], ct[cut:]
    return a, b


_WORDS = ["遊んでいき", "遊んでいく", "遊んでい", "落ち着いて", "おちついて", "落ち着い", "大事なことを", "大事なこと", "やっぱり", "やっぱ",
          "思ってもみなかった", "思っても見なかった", "もらって", "もらっ", "思ってもみな", "繰り返す", "繰り返し", "繰り返", "よろしく", "ありがとう", "じゃあ", "ちゃんと", "だから", "けど", "から", "ので", "のに", "ため", "こと", "もの",
          "ほど", "ない", "ます", "です", "てる", "てた", "とか", "って", "ように", "みたい", "ばかり", "なんで", "どう", "そう",
          "ちょう", "めっちゃ", "やっぱ", "でも", "まぁ", "もう", "けない", "いけ", "なきゃ", "いうか", "ってい", "って感", "んだな", "罰ゲーム", "ゲーム", "ありえ", "ねえ", "えんだ", "じゃね", "んだよ", "歯磨き", "やめ", "最新話", "かった", "んじゃ", "だった", "めちゃくちゃ", "めちゃ", "かっこよすぎ", "かっこよかった", "カッコよかった", "かっこよ", "カッコよ", "いいね", "フォロー", "完結", "決断", "ピンチ", "として", "ては", "たら", "だと", "なら", "ので", "ている", "ていた", "いつ", "わかっ", "なんで", "どこ", "くなかっ", "くない", "くなっ", "だった", "とはね"]


_LEAD_ADV = ("また", "やっぱ", "やっぱり", "ちょっと", "まず", "でも", "だから", "じゃあ", "ほんと", "本当に", "絶対", "全然", "結構", "多分", "たぶん", "マジで", "めっちゃ", "さっき")


def _char_class(ch):
    if "ァ" <= ch <= "ヿ" or ch == "ー":
        return "k"          # katakana
    if ch.isascii() and ch.isalnum():
        return "a"          # digits and letters
    if "぀" <= ch <= "ゟ":
        return "h"
    return "x"


def _char_class2(ch):
    if "一" <= ch <= "鿿" or ch in "々〆":
        return "kanji"
    if "぀" <= ch <= "ゟ":
        return "hira"
    if "゠" <= ch <= "ヿ":
        return "kata"
    return "other"


_PREV_BOTTOM = [False]      # the telop just before was bottom-heavy (user: then keep bottom-heavy, for readability)


def _split_two_lines(text):
    if "\n" not in text and text in _EX["telop"]:
        return "\n".join(_EX["telop"][text])            # broken by hand in the "改行テスト": the same sentence is broken the same way
    if "\n" in text or len(text) < RULES["break_min_chars"]:
        return text
    banned = set()
    for w in list(_WORDS) + ["もんじゃ", "よし", "ちゃう", "ちゃっ", "ちゃえ", "ちゃお", "なっち", "んじゃん"]:
        k = text.find(w)
        while k != -1:
            banned.update(range(k + 1, k + len(w)))
            k = text.find(w, k + 1)
    # user rule: top-heavy first; equal halves only when nothing else works; after a bottom-heavy telop stay bottom-heavy
    want = (len(text) + (-1.0 if _PREV_BOTTOM[0] else max(RULES["first_line_longer_by"], 1.0))) / 2.0
    learned = {c: n for c, n in (RULES.get("break_after") or {}).items() if (n >= 2 or c in PARTICLES) and c in "でがはをにのともやてたるだねよかなじ"}
    best, best_d = None, 1e9
    for i in range(2, len(text) - 1):
        prev, nxt = text[i - 1], text[i]
        if i in banned:
            continue
        if nxt in "ぁぃぅぇぉゃゅょん" or prev == "っ" or (i + 1 < len(text) and text[i + 1] in "ゃゅょ"):
            continue                         # never inside a word's ending ("なっ|ちゃ", "もん|じゃ")
        if text[i - 2:i] in ("ちゃ", "じゃ", "しゃ", "きゃ", "ひゃ", "にゃ", "みゃ") and nxt in "うえおいんっあ":
            continue                         # "なっちゃ|う"
        if _char_class(prev) == _char_class(nxt) and _char_class(prev) in "ka":
            continue                         # not inside a katakana word or a number
        bonus = 0.0
        if prev in PARTICLES:
            bonus = 1.5
        if prev in learned:
            bonus = max(bonus, 1.5 + min(learned[prev], 3) * 0.2)
        if prev in "！？!?":
            bonus = 3
        if text[:i] in _LEAD_ADV or (text[:2] in ("お前", "俺は", "みんな"[:2]) and text[2:i] in _LEAD_ADV) or (text[:3] == "みんな" and text[3:i] in _LEAD_ADV):
            bonus += 3.0                     # "また / 動画消されんだろ": a short opening adverb stands alone
        if text[i:i + 2] in ("って", "という", "みたい") or text[i:i + 3] == "ていう":
            bonus += 3.0                     # "…やるんだな / って感じ"
        bonus += _pair_bonus(prev, nxt)
        kp, kn = _char_class2(prev), _char_class2(nxt)
        if kp == "kanji" and kn == "hira" and nxt not in "はがをにでのとも":
            bonus -= 4.0                     # the kana tail of a word ("好|きだった")
        if text[max(i - 2, 0):i] in ("さん", "くん", "ちゃん", "先輩", "社長") and kn != "hira" or (text[max(i - 2, 0):i] in ("さん", "くん") and nxt not in "はがをにでのともへ"):
            bonus += 10.0                    # "林原さん|いい声"
        if kp == "hira" and kn == "hira" and prev in "いしだ" and nxt not in "はがをにでのともへねよかな":
            bonus -= 5.0                     # "い|い声", "し|てる": these endings are not break points on their own
        if text[max(i - 2, 0):i] == "たら" and nxt not in "はがをにでのともへ":
            bonus += 3.0                     # "増えたら|一緒に": the end of an if-clause
        if text[i:i + 2] in ("どこ", "どう", "なに", "なん", "いつ") and i >= 4:
            bonus += 2.5
        if text[max(i - 2, 0):i] == "いつ" and nxt not in "はがをにでのともへ":
            bonus += 3.5                     # "お前いつ|わかってんだよ", "いつ|気づいてんだよ"
        if text[i:i + 3] == "だった" and kp == "kanji":
            bonus += 4.0                     # "まさか堺雅人|だったとはね"
        if text[i:i + 2] == "とは":
            bonus -= 4.0
        if kp == "hira" and text[i:i + 2] in ("れる", "れた", "れて", "れば", "れま", "せる", "せた", "せて", "ない", "たい", "ます", "まし", "ちゃ", "てる", "てた", "ている", "ていた") and prev not in PARTICLES:
            bonus -= 6.0                     # "撃た|れる": a verb's ending stays with its stem
        if nxt == "誰":
            bonus += 2.5                     # "登場人物|誰好き"
        if nxt in "方人目回中的性化者達版話" and kn == "kanji":
            bonus -= 3.0                     # a suffix ("言い|方")
        if kp == "hira" and kn in ("kanji", "kata"):
            bonus += 1.0                     # a new content word starts
        if kp == "kanji" and kn == "kanji":
            bonus -= 2.5                     # inside a kanji word ("堺雅|人")
        if kp == "hira" and kn == "hira" and prev not in PARTICLES and prev not in learned:
            bonus -= 6.0                     # inside a kana word ("い|い声", "かっこ|よすぎ")
        if kp == "hira" and kn == "hira" and prev in "てで" and nxt not in "はがをにのともへ":
            bonus -= 4.0                     # "見て|ねえ", "言って|る": the te-form goes on
        if kp == "hira" and kn == "hira" and nxt in "っーんてたでだ" and prev not in "ねよ":
            bonus -= 5.0                     # "わか|って", "い|ない": the next kana is part of the word before it
        if nxt in "ぁぃぅぇぉっゃゅょんー！？!?、。":
            bonus -= 3                       # never start a line with a small kana or a mark
        score = 1.5 * abs(i - want) - bonus
        if i * 2 == len(text):
            score += 2.5                     # same number of characters above and below
        elif (len(text) - i > i) != _PREV_BOTTOM[0] and abs(len(text) - 2 * i) <= 2 and not _PREV_BOTTOM[0]:
            score += 0.8                     # bottom-heavy only a little: top-heavy is preferred
        score += 3.0 * (max(i - LINE_LIMIT, 0) + max(len(text) - i - LINE_LIMIT, 0))      # a line longer than 10 characters
        if score < best_d:
            best, best_d = i, score
    if best is None:
        return text
    return text[:best] + "\n" + text[best:]


def _units(text):
    """A script line cut into sentences: at 。！？ and at quote marks.  ！？ stay with their sentence."""
    out, cur = [], ""
    text = re.sub(r"(?:…+|\.{2,}|・{3,}|。{2,})", "。", text)      # "…" / "..." : the telop may be cut there (user rule)
    for k, ch in enumerate(text):
        if ch in "！？!?":
            cur += ch
            if not (k + 1 < len(text) and text[k + 1] in "！？!?"):        # "！？" stays together
                out.append(cur)
                cur = ""
        elif ch in "｢｣「」":
            pass                                  # quote marks are not spoken: they neither cut the line nor show
        elif ch in "。":
            if cur.strip():
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [re.sub(r"^[wｗ]+", "", u.strip()) for u in out if u.strip()]       # laughter ("w") at the start is not text


def _expand_cuts(p):
    """A phrase whose (script) wording holds a sentence end is cut there; a "、" between two parts of at
    least 3 characters is a cut as well.  The time is shared out by the number of characters."""
    t = p["text"]
    segs = []
    for chunk in t.split("\u0001"):
        chunk = re.sub(r"([wｗ])(?=[^wｗ\s]{3})", "\\1、", chunk)          # cut after laughter: "ほぉw|それは…"
        parts = re.split(r"、", chunk)
        cur = parts[0]
        for nxt in parts[1:]:
            if len(cur.replace("\n", "")) >= 3 and len(nxt) >= 3:
                segs.append(cur)
                cur = nxt
            else:
                cur += "、" + nxt
        segs.append(cur)
    segs = [x for x in segs if x.strip()]
    if len(segs) <= 1:
        p["text"] = (segs[0] if segs else t.replace("\u0001", ""))
        return [p]
    total = sum(len(x) for x in segs)
    out, acc = [], 0
    for x in segs:
        q = dict(p)
        f0, f1 = acc / total, (acc + len(x)) / total
        q["text"] = x
        q["start"] = p["start"] + (p["end"] - p["start"]) * f0
        q["end"] = p["start"] + (p["end"] - p["start"]) * f1
        q["s0"] = p["s0"] + (p["s1"] - p["s0"]) * f0
        q["s1"] = p["s0"] + (p["s1"] - p["s0"]) * f1
        out.append(q)
        acc += len(x)
    return out


def _cut_at_pause(p, speakers, min_chars=8, min_pause=0.12):
    """A long phrase with a real pause inside is cut at the pause (where the speaker takes a breath)."""
    plain = p["text"].replace("\n", "")
    if len(plain) < min_chars or speakers.env is None:
        return [p]
    t0, t1 = p.get("s0", p["start"]), p.get("s1", p["end"])
    i0, i1 = int((t0 + 0.3) / 0.01), int((t1 - 0.3) / 0.01)
    if i1 - i0 < 20:
        return [p]
    w = speakers.env[i0:i1]
    top = speakers.env[int(t0 / 0.01):int(t1 / 0.01) + 1].max()
    quiet = w < top - 22
    best, i = None, 0
    while i < len(w):
        if quiet[i]:
            j = i
            while j < len(w) and quiet[j]:
                j += 1
            if (j - i) * 0.01 >= min_pause and (best is None or (j - i) > best[1] - best[0]):
                best = (i, j)
            i = j
        else:
            i += 1
    if best is None:
        return [p]
    tc = (i0 + best[1]) * 0.01                       # the voice comes back here
    full = speakers.env[int(t0 / 0.01):int(t1 / 0.01) + 1]
    voiced = full > top - 22                           # characters are spread over the voiced time only
    cut_i = max(int((tc - t0) / 0.01), 1)
    frac = voiced[:cut_i].sum() / max(voiced.sum(), 1)
    k = int(round(len(plain) * frac))
    # nudge to a particle or a kana/kanji boundary within 2 characters
    cands = [x for x in range(max(2, k - 3), min(len(plain) - 2, k + 3) + 1) if plain[x - 1] in "はがをにでともの"]
    if not cands:
        return [p]
    k = min(cands, key=lambda x: abs(x - k))
    a, b = plain[:k], plain[k:]
    q1, q2 = dict(p), dict(p)
    q1.update(text=a, end=tc, s1=tc)
    q2.update(text=b, start=tc, s0=tc)
    return [q1, q2]


_FILL = r"(?:あー+|あ|えー+|え|うーん|んー|まぁ|まあ|いや|ええと|えっと|ほぉ|おー|へー|はー)"
_FILL_RE = re.compile(r"^((?:%s)+)(.+)$" % _FILL, re.S)


def _split_filler(p):
    """A phrase that starts with a filler ("あーまぁ", "あー", "いや") and goes on: the filler is cut off.
    "あーまぁ" + a long line: its own telop.  "あー" + a short line: one telop, two lines.  A lone "まぁ"
    in front of a long line stays attached."""
    m = _FILL_RE.match(p["text"].replace("\n", ""))
    if not m:
        return [p]
    f, rest = m.group(1), m.group(2)
    if len(rest) < 3 or not (len(f) >= 3 or (len(f) >= 2 and len(rest) <= 8)):
        return [p]
    frac = len(f) / float(len(f) + len(rest))
    tc = p["start"] + (p["end"] - p["start"]) * frac
    sc = p.get("s0", p["start"]) + (p.get("s1", p["end"]) - p.get("s0", p["start"])) * frac
    a, b = dict(p), dict(p)
    a.update(text=f.replace("まあ", "まぁ"), end=tc, s1=sc, is_filler=True)
    b.update(text=rest, start=tc, s0=sc, after_filler=f)
    return [a, b]


def _fix_fillers(pieces):
    """After the script's wording went in: don't repeat the filler, and join "あー"+short line as 2 lines."""
    out = []
    for p in pieces:
        f = p.get("after_filler")
        if f:
            p["text"] = re.sub(r"^(?:%s)+" % _FILL, "", p["text"].replace("\n", "")) or p["text"]
            prev = out[-1] if out else None
            if prev is not None and prev.get("is_filler") and len(p["text"].replace("\n", "")) <= 8:
                prev["text"] = prev["text"] + "\n" + p["text"]
                prev["end"], prev["s1"] = p["end"], p.get("s1", p["end"])
                prev.pop("is_filler", None)
                continue
        out.append(p)
    return out


def _is_script_prefix(cur, lines):
    """True when the characters so far are the start of a (not much longer) script line: keep it in one title."""
    n = ps_norm("".join(c for c, _ in cur))
    for ln in lines:
        m = ln["norm"]
        if len(m) > len(n) and len(m) <= RULES['max_title_chars'] + 2 and ft.ratio(m[:len(n)], n) >= 0.85:
            return True
    return False


def _voiced_spans(speakers, lo, hi, margin_db=8.0, min_len=0.1, join=0.15):
    """Stretches of [lo, hi] where somebody is talking, also quietly (the other speaker's voice leaking into the
    microphone is only a few dB above the room noise).  [] when the sound level is not known."""
    env = getattr(speakers, "env", None)
    if env is None or hi <= lo:
        return []
    fl = getattr(speakers, "_floor", None)
    if fl is None:
        fl = float(np.percentile(env, 10))
        try:
            speakers._floor = fl
        except Exception:
            pass
    i0, i1 = max(int(lo * 100), 0), min(int(hi * 100), len(env))
    on = env[i0:i1] > fl + margin_db
    spans, st = [], None
    for k, v in enumerate(list(on) + [False]):
        if v and st is None:
            st = k
        if not v and st is not None:
            spans.append([(i0 + st) / 100.0, (i0 + k) / 100.0])
            st = None
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= join:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_len]


def _loud_onset(speakers, t0, t1):
    """First moment in [t0, t1] where the level stays well above the noise for 0.15 s (a full-voice start)."""
    for a, b in _voiced_spans(speakers, t0, t1, margin_db=16.0, min_len=0.15, join=0.1):
        return a
    return None


def _fill_missing_lines(pieces, lines, speakers, a0, a1):
    """A script line that no phrase carries but where there is talking between its neighbours was said
    (Whisper dropped or mis-heard it, e.g. a quiet "そうだな"): put the script's wording there."""
    units = [u for ln in lines for u in _units(ln["text"])]
    norms = [ps_norm(u) for u in units]
    pn = [ps_norm(p["text"]) for p in pieces]
    where = {}                                   # unit index -> piece index
    for pi, p_ in enumerate(pieces):
        for u_ in (p_.get("_uidxs") or [p_.get("_uidx")]):
            if u_ is not None and u_ not in where:
                where[u_] = pi
    for ui, un in enumerate(norms):
        if ui in where:
            continue
        for pi, n in enumerate(pn):
            if un and n == un and pi not in where.values():
                where[ui] = pi
                break
    if getattr(speakers, "env", None) is not None:
        for ui, pi in sorted(where.items()):              # a short answer ("見た") that has no time of its own
            X = pieces[pi]
            if X.get("s1", 0) - X.get("s0", 0) >= 0.15 or ui - 1 not in where or ui + 1 not in where:
                continue
            P, N = pieces[where[ui - 1]], pieces[where[ui + 1]]
            if N is X or P is X or N["s0"] >= X["s0"] + 0.2:
                continue                                   # the next line does not start in front of it
            lo = P["s1"]
            sp_ = _voiced_spans(speakers, lo - 0.3, N["s1"], margin_db=8.0, min_len=0.05, join=0.05)
            sp_ = [(a, b) for a, b in sp_ if b > lo + 0.1]
            if len(sp_) < 2:
                continue
            x0, x1 = max(sp_[0][0], lo), sp_[0][1]
            if x1 - x0 < 0.12:
                continue
            X["s0"] = X["start"] = x0
            X["s1"] = X["end"] = x1
            X["ct"] = [x0, x1]
            n0 = sp_[1][0]
            N["start"] = N["s0"] = n0
            if N.get("ct"):
                N["ct"] = [max(c, n0) for c in N["ct"]]
    pieces.sort(key=lambda q: q["s0"])
    where = {}
    for pi, p_ in enumerate(pieces):
        for u_ in (p_.get("_uidxs") or [p_.get("_uidx")]):
            if u_ is not None and u_ not in where:
                where[u_] = pi
    for ui, un in enumerate(norms):
        if ui in where:
            continue
        for pi, n in enumerate(pn if len(pn) == len(pieces) else [ps_norm(p["text"]) for p in pieces]):
            if un and n == un and pi not in where.values():
                where[ui] = pi
                break
    missing = [ui for ui in range(len(units)) if ui not in where and len(norms[ui]) >= 2]
    if not missing or getattr(speakers, "env", None) is None:
        return pieces
    groups, cur_g = [], []
    for ui in missing:
        if cur_g and not any(uj in where for uj in range(cur_g[-1] + 1, ui)):
            cur_g.append(ui)
        else:
            if cur_g:
                groups.append(cur_g)
            cur_g = [ui]
    if cur_g:
        groups.append(cur_g)
    add = []
    for g in groups:
        prev_u = max([u for u in where if u < g[0]], default=None)
        next_u = min([u for u in where if u > g[-1]], default=None)
        P = pieces[where[prev_u]] if prev_u is not None else None
        N = pieces[where[next_u]] if next_u is not None else None
        if P is not None and N is not None and N["s0"] < P["s1"] - 0.3:
            continue                              # the neighbours are not in script order: do not guess
        lo = P["s1"] if P is not None else a0
        hi = N["s0"] if N is not None else a1
        if N is not None:
            lo_ = _loud_onset(speakers, N["s0"], N["s1"])       # Whisper started the next phrase too early
            if lo_ is not None and lo_ > hi + 0.05:
                hi = lo_
        spans = [(a, b) for a, b in _voiced_spans(speakers, lo, hi)
                 if not any(a < q["s1"] - 0.05 and b > q["s0"] + 0.05 for q in pieces if q is not N and q is not P)]
        tot = sum(b - a for a, b in spans)
        if tot < 0.25:
            continue
        chars = [len(norms[u]) for u in g]
        acc, k = 0.0, 0
        for gi, u in enumerate(g):
            share = tot * chars[gi] / float(sum(chars))
            want0, want1 = acc, acc + share
            acc = want1
            t0 = t1 = None
            run = 0.0
            for a, b in spans:
                if run + (b - a) > want0 and t0 is None:
                    t0 = a + max(want0 - run, 0.0)
                if run + (b - a) >= want1 - 1e-6:
                    t1 = a + (want1 - run)
                    break
                run += b - a
            if t0 is None or t1 is None or t1 - t0 < 0.1:
                continue
            add.append({"start": t0, "end": t1 + 0.1, "s0": t0, "s1": t1 + 0.1, "text": units[u], "ct": [t0, t1], "_missing": True})
        if N is not None and hi > N["s0"]:
            N["start"] = N["s0"] = hi
            if N.get("ct"):
                N["ct"] = [max(c, hi) for c in N["ct"]]
    out = sorted(pieces + add, key=lambda q: q["s0"])
    return out


# ------------------------------------------------------------------ script first
def _norm_chars(text):
    """[(original char, normalized char or '')] : punctuation has no normalized form."""
    return [(ch, ps_norm(ch)) for ch in text]


def _match_ratio(a, b):
    if not a or not b:
        return 0.0
    mt = sum(x.size for x in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return 2.0 * mt / (len(a) + len(b))


def _merge_wording(script_parts, heard_text):
    """The script's wording (a list of parts: one per script line), except where the speaker added something
    (kept) or left the end off (dropped).  Returns one text per part."""
    script_text = "".join(script_parts)
    sc, wc = _norm_chars(script_text), _norm_chars(heard_text)
    ns = "".join(n for _, n in sc)
    nw = "".join(n for _, n in wc)
    s_idx = [i for i, (_, n) in enumerate(sc) if n]
    w_idx = [i for i, (_, n) in enumerate(wc) if n]
    if len(s_idx) != len(ns) or len(w_idx) != len(nw):
        return list(script_parts)          # normalization changed the length: do not guess
    bounds, acc = [], 0                      # norm index where each part starts
    for p in script_parts:
        bounds.append(acc)
        acc += len(ps_norm(p))

    def part_of(i, ins=False):
        k = 0
        for pi, bd in enumerate(bounds):
            if i >= bd:
                k = pi
        return k

    def s_slice(i1, i2):
        a0_ = 0 if i1 == 0 else s_idx[i1]
        b0_ = s_idx[i2] if i2 < len(s_idx) else len(sc)
        return "".join(c for c, _ in sc[a0_:b0_])

    out = [[] for _ in script_parts]
    ops = difflib.SequenceMatcher(None, ns, nw, autojunk=False).get_opcodes()
    for oi, (tag, i1, i2, j1, j2) in enumerate(ops):
        first, last = oi == 0, oi == len(ops) - 1
        wch = "".join(wc[w_idx[j]][0] for j in range(j1, j2))
        if tag in ("equal", "replace"):
            # a replaced stretch that crosses a part boundary is cut at it
            cut = [i1] + [bd for bd in bounds if i1 < bd < i2] + [i2]
            for x, y in zip(cut, cut[1:]):
                out[part_of(x)].append(s_slice(x, y))
        elif tag == "insert":
            pv = ops[oi - 1][2] - ops[oi - 1][1] if oi and ops[oi - 1][0] == "equal" else 0
            nx = ops[oi + 1][2] - ops[oi + 1][1] if oi + 1 < len(ops) and ops[oi + 1][0] == "equal" else 0
            one_ok = j2 - j1 == 1 and (wch in "がはをにもでとの" or not re.fullmatch(r"[ぁ-ん]", wch))
            if first and nx >= 3 and j2 - j1 <= 3 and re.fullmatch(r"[あいうえおんーやまぁっ]+", wch):
                out[part_of(i1)].insert(0, wch)       # "いや" / "あー" the speaker put in front of the line
            elif pv >= 2 and nx >= 2 and (j2 - j1 >= 2 or one_ok):
                out[part_of(i1)].append(wch)        # something the speaker added inside a line that matches
        elif tag == "delete":
            sl_ = s_slice(i1, i2)
            if first and not last and i2 - i1 <= 3 and re.fullmatch(r"[あいうえおんーやまぁっ、]+", sl_):
                out[part_of(i1)].append(sl_)          # "あー、" said before the line, Whisper left it out
            elif not (first or last):
                if not (i2 - i1 == 1 and ns[i1] in "がはをにのもでとへ" and
                        ops[oi - 1][0] == "equal" and ops[oi + 1][0] == "equal"):
                    out[part_of(i1)].append(sl_)
    res = ["".join(o) for o in out]
    return [r if ps_norm(r) else sp for r, sp in zip(res, script_parts)]


def _script_units(block):
    """Sentences of the script with their laughter marks.  [{text, sent, parts: [(text, norm)], lb, la}]
    `sent` numbers the sentences like _fill_missing_lines does (speech lines only)."""
    units, pend_lb, sent, lid = [], False, 0, 0
    for ln in block["lines"]:
        if ln["kind"] == "laugh":
            if units:
                units[-1]["la"] = True
            else:
                pend_lb = True
            continue
        if ln["kind"] != "speech" or not ln["norm"]:
            continue
        us = _units(ln["text"])
        lid += 1
        for k, u in enumerate(us):
            parts = [x for x in re.split(r"(?<=[、，,])", u) if x.strip(" 、，,")]
            end_mark = re.search(r"[！？!?]+$", u)
            ps_ = [(x, ps_norm(x)) for x in parts]
            ps_ = [(x, n) for x, n in ps_ if n]
            if not ps_:
                continue
            units.append({"text": u, "line": lid, "sent": sent + k, "parts": ps_, "end": end_mark.group(0) if end_mark else "",
                          "lb": (ln.get("laugh_before") and k == 0) or (pend_lb and not units),
                          "la": bool(ln.get("laugh_after") and k == len(us) - 1)})
            pend_lb = False
        sent += len(us)
    return units


def _script_first(gsegs, block, speakers, a0, a1):
    """Fit the Whisper segments to the script in order.  Every segment is the script's wording for the
    lines it covers (even if Whisper heard it badly), or an ad-lib.  Returns (pieces, laughs)."""
    units = _script_units(block)
    subs = []                                  # flat list of parts: (unit index, text, norm)
    for ui, u in enumerate(units):
        for pi_, (tx, nn) in enumerate(u["parts"]):
            subs.append((ui, tx, nn, pi_ == len(u["parts"]) - 1))
    G = []
    for g in gsegs:
        chars = [(c, t) for c, t in g["chars"] if not STOP_RE.match(c)]
        if not chars:
            continue
        G.append({"text": "".join(c for c, _ in chars), "ct": [t for _, t in chars], "partial": g["partial"],
                  "s": g["s"], "e": g["e"],
                  "norm": ps_norm("".join(c for c, _ in chars))})
    G = [g for g in G if g["norm"]]
    nG, nS = len(G), len(subs)
    INF = 1e9
    dp = [[INF] * (nS + 1) for _ in range(nG + 1)]
    bk = [[None] * (nS + 1) for _ in range(nG + 1)]
    dp[0][0] = 0.0
    for gi in range(nG + 1):
        for si in range(nS + 1):
            cur = dp[gi][si]
            if cur >= INF:
                continue
            if gi < nG:
                c = cur + len(G[gi]["norm"]) * 0.7
                if c < dp[gi + 1][si]:
                    dp[gi + 1][si], bk[gi + 1][si] = c, (gi, si, None)
            if si < nS:
                c = cur + len(subs[si][2]) * 1.0
                if c < dp[gi][si + 1]:
                    dp[gi][si + 1], bk[gi][si + 1] = c, (gi, si, "skipu")
            if gi < nG and si < nS:
                for v in range(si + 1, min(si + 4, nS) + 1):
                    jn = "".join(subs[k][2] for k in range(si, v))
                    mt_ = _match_ratio(jn, G[gi]["norm"]) * (len(jn) + len(G[gi]["norm"])) / 2.0
                    c = cur + ((len(jn) - mt_) + (len(G[gi]["norm"]) - mt_)) * 0.9 + 0.5 * (v - si - 1)
                    if c < dp[gi + 1][v]:
                        dp[gi + 1][v], bk[gi + 1][v] = c, (gi, si, v)
    gi, si, path = nG, nS, []
    while gi or si:
        pg, ps_, how = bk[gi][si]
        if how is None:
            path.append(("adlib", pg, None, None))
        elif how == "skipu":
            pass
        else:
            path.append(("map", pg, ps_, how))
        gi, si = pg, ps_
    path.reverse()
    mapped = {p[1]: (p[2], p[3]) for p in path if p[0] == "map"}
    # speaking speed from the segments that are said clearly
    rates = []
    for gi_, (s0, s1) in mapped.items():
        jn = "".join(subs[k][2] for k in range(s0, s1))
        g = G[gi_]
        if not g["partial"] and _match_ratio(jn, g["norm"]) >= 0.8 and len(jn) >= 6:
            rates.append((g["ct"][-1] + 0.12 - g["ct"][0]) / len(g["norm"]))
    rate = float(np.clip(np.percentile(rates, 25), 0.09, 0.14)) if len(rates) >= 4 else 0.115
    if os.environ.get('SF_DEBUG'):
        print('rate', rate, rates)
    pieces, laughs = [], []
    # lines Whisper dropped right before a segment that is long for its text ("はい" before "巨人進撃の"):
    # they were said inside that segment
    pre, order = {}, sorted(mapped)
    for idx_, gi_ in enumerate(order):
        a_, b_ = mapped[gi_]
        prev_end = max([mapped[x][1] for x in order[:idx_]], default=0)
        sk = list(range(prev_end, a_))
        g = G[gi_]
        if not sk or len(sk) > 3 or g["partial"]:
            continue
        gap_ = g["s"] - (G[order[idx_ - 1]]["e"] if idx_ else a0)
        skch = sum(len(subs[k][2]) for k in sk)
        if gap_ < 0.35 and (g["e"] - g["s"]) >= len(g["norm"]) * rate + skch * rate * 0.8 + 0.1:
            pre[gi_] = sk
    for gi_, g in enumerate(G):
        if gi_ not in mapped:
            if g["partial"] or len(g["norm"]) < 2:
                continue                              # a bit of the talk before/after the take
            pieces.append({"start": g["ct"][0], "end": g["ct"][-1] + 0.12, "s0": g["ct"][0], "s1": g["ct"][-1] + 0.12,
                           "text": g["text"], "ct": list(g["ct"]), "_adlib": True})
            continue
        s0, s1 = mapped[gi_]
        jn = "".join(subs[k][2] for k in range(s0, s1))
        r = _match_ratio(jn, g["norm"])
        if g["partial"] and r < 0.45:
            continue
        _rest = g["norm"][:len(g["norm"]) - len(jn)] if jn and g["norm"].endswith(jn) else ""
        _said_more = bool(jn) and 2 <= len(_rest) <= 5 and r >= 0.45 and not re.fullmatch(r"[あいうえおぁぃぅぇぉっんーはひふへほwｗ笑]*", _rest)
        if r < 0.45 or _said_more:         # not the script: the speaker said something else, or more ("それは"+"人ですか") (kept as heard)
            u0, u1 = subs[s0][0], subs[s1 - 1][0]
            pieces.append({"start": g["ct"][0], "end": g["ct"][-1] + 0.12, "s0": g["ct"][0], "s1": g["ct"][-1] + 0.12,
                           "text": g["text"], "ct": list(g["ct"]), "_adlib": True,
                           "_uidxs": [units[u]["sent"] for u in range(u0, u1 + 1)], "_unit": u1, "_unit0": u0})
            continue

        def mk_groups(rng_):
            gr = []
            for k in rng_:
                ui, tx, nn, lastp = subs[k]
                if gr and gr[-1][0] == ui:
                    gr[-1][1].append(tx)
                else:
                    gr.append((ui, [tx]))
            return gr
        groups_sk = mk_groups(pre.get(gi_, []))
        groups = mk_groups(range(s0, s1))
        gtexts = ["".join(txs) for ui, txs in groups]
        if r >= 0.75:
            gtexts = _merge_wording(gtexts, g["text"])
        if len(groups) > 1 and len(g["norm"]) <= 7 and len({units[u]["line"] for u, _ in groups}) == 1:
            groups, gtexts = [(groups[0][0], None)] + [], ["".join(gtexts)]      # said in a breath: one telop
            uids_all = [units[u]["sent"] for u in range(subs[s0][0], subs[s1 - 1][0] + 1)]
        else:
            uids_all = None
        gtexts = ["".join(txs) if False else t for t in ([ "".join(txs) for ui, txs in groups_sk] + gtexts)]
        groups = groups_sk + groups
        ct = list(g["ct"])
        T0, T1 = ct[0], ct[-1] + 0.12
        if not groups_sk and len(ct) >= 8:        # a short filler ("あー") before the words: the words begin after it
            sp_ = _voiced_spans(speakers, T0 - 0.05, T0 + 1.5)
            if len(sp_) >= 2 and sp_[0][1] - sp_[0][0] <= 0.4 and sp_[1][0] - sp_[0][1] >= 0.08 and sp_[1][0] < T1 - 0.5 and abs(sp_[0][0] - T0) < 0.12 and sp_[1][0] > T0 + 0.1:
                T0n = sp_[1][0]
                ct = [T0n + (c - T0) * (T1 - T0n) / (T1 - T0) for c in ct]
                T0 = T0n
        lead0 = units[groups[0][0]]["lb"] or (groups[0][0] > 0 and units[groups[0][0] - 1]["la"])
        if not lead0 and not g["partial"] and T0 - g["s"] > 0.1:
            # Whisper's first characters can sit late: the voice starts at the first sound after the pause
            sp2 = _voiced_spans(speakers, g["s"] - 0.1, T0 + 0.05, margin_db=10.0, min_len=0.08, join=0.05)
            if sp2 and sp2[0][0] < T0 - 0.1 and sp2[0][0] > g["s"] - 0.1:
                T0n = sp2[0][0]
                ct = [T0n + (c - T0) * (T1 - T0n) / (T1 - T0) for c in ct]
                T0 = T0n
        nch = sum(len(ps_norm(t)) for t in gtexts)
        D = min(nch * rate * 1.25, T1 - T0)
        lead = units[groups[0][0]]["lb"] or (groups[0][0] > 0 and units[groups[0][0] - 1]["la"]
                                             and (s0 == 0 or subs[s0 - 1][0] != groups[0][0] or bool(groups_sk)))
        trail = units[groups[-1][0]]["la"]
        if (lead or trail) and (T1 - T0) > D * 1.2 + 0.3 and not g["partial"]:
            if lead:
                ct = [T1 - D + D * (i + 0.5) / len(ct) for i in range(len(ct))]
                T0 = T1 - D
            else:
                ct = [T0 + D * (i + 0.5) / len(ct) for i in range(len(ct))]
                T1 = T0 + D
        tot = float(sum(max(len(ps_norm(t)), 1) for t in gtexts))
        n = len(ct)
        xs = [0.0] + [(i + 0.5) / n for i in range(n)] + [1.0]
        ys = [T0] + ct + [T1]
        # the dropped lines come first: their time is the start of the segment's voiced time
        acc = 0.0
        seg_pcs = []
        for gix_, ((ui, _), t_) in enumerate(zip(groups, gtexts)):
            f0 = acc / tot
            acc += max(len(ps_norm(t_)), 1)
            f1 = acc / tot
            m_ = max(len(ps_norm(t_)), 1)
            seg_ct = [float(np.interp(f0 + (f1 - f0) * (j + 0.5) / m_, xs, ys)) for j in range(m_)]
            t_ = re.sub(r"[、，,\s]+$", "", t_)
            pc = {"start": seg_ct[0], "end": seg_ct[-1] + 0.12, "s0": seg_ct[0], "s1": seg_ct[-1] + 0.12,
                  "text": t_, "ct": seg_ct, "_uidx": units[ui]["sent"], "_unit": ui, "_unit0": ui}
            if uids_all and gix_ >= len(groups_sk):
                pc["_uidxs"] = uids_all
                pc["_unit"] = subs[s1 - 1][0]
            seg_pcs.append(pc)
        # the boundary between two lines said in one breath: the dip in the sound nearest to the calculated one
        env_ = getattr(speakers, "env", None)
        if env_ is not None and len(seg_pcs) > 1:
            sm_ = np.convolve(env_, np.ones(4) / 4.0, mode="same")
            for j_ in range(1, len(seg_pcs)):
                b_ = seg_pcs[j_]["s0"]
                lo_i, hi_i = int((b_ - 0.3) * 100), int((b_ + 0.3) * 100)
                if lo_i < 0 or hi_i >= len(sm_) or seg_pcs[j_ - 1]["s0"] > b_ - 0.2 or seg_pcs[j_]["s1"] < b_ + 0.2:
                    continue
                w_ = sm_[lo_i:hi_i]
                k_ = int(np.argmin(w_))
                around = max(w_[:max(k_ - 4, 1)].max() if k_ > 4 else w_.max(), w_[k_ + 4:].max() if k_ + 4 < len(w_) else w_.max())
                if around - w_[k_] >= 5.0 and w_[k_] > np.percentile(env_, 10) + 6:
                    nb_ = (lo_i + k_) / 100.0 + 0.02
                    for key_ in ("s1",):
                        seg_pcs[j_ - 1][key_] = nb_
                    seg_pcs[j_ - 1]["end"] = nb_
                    seg_pcs[j_]["s0"] = seg_pcs[j_]["start"] = nb_
                    for q_ in (seg_pcs[j_ - 1], seg_pcs[j_]):
                        m2_ = len(q_["ct"])
                        q_["ct"] = [q_["s0"] + (q_["s1"] - q_["s0"]) * (x + 0.5) / m2_ for x in range(m2_)]
        pieces.extend(seg_pcs)
    pieces.sort(key=lambda p: p["s0"])
    # laughter from the script: between the speech before and after it
    for i, p in enumerate(pieces):
        ui = p.get("_unit")
        if ui is None:
            continue
        u0 = p.get("_unit0", ui)
        if units[u0]["lb"] and not any(q.get("_unit") == u0 for q in pieces[:i]):
            lo = pieces[i - 1]["s1"] - 0.10 if i else a0
            if i:
                ro_ = _rise_onset(speakers, pieces[i - 1]["s1"], back=0.35, fwd=0.35, deepest=True)
                if ro_ is not None:
                    lo = ro_ - 0.02
            if p["s0"] - lo >= 0.3:
                laughs.append([lo, p["s0"]])
        if units[ui]["la"] and not any(q.get("_unit0", q.get("_unit")) == ui for q in pieces[i + 1:]):
            nxt = pieces[i + 1] if i + 1 < len(pieces) else None
            lo = p["s1"] - 0.10
            ro_ = _rise_onset(speakers, p["s1"], back=0.35, fwd=0.35, deepest=True)
            if ro_ is not None:
                lo = ro_ - 0.02
            hi = nxt["s0"] if nxt is not None else a1
            if nxt is not None and hi - lo < 0.3:
                al_ = _after_laugh(speakers, lo, nxt["s1"])
                if al_ is not None:                    # the laughter was swallowed by the next line: the line starts after it
                    hi = al_[1]
                    nxt["s0"] = nxt["start"] = hi
                    nxt["_fixed"] = True
                    m3_ = len(nxt["ct"])
                    nxt["ct"] = [hi + (nxt["s1"] - hi) * (x + 0.5) / m3_ for x in range(m3_)]
            if hi - lo >= 0.3:
                laughs.append([lo, hi])
    return pieces, laughs


def _rise_onset(speakers, t, back=0.3, fwd=0.2, rise_db=7.0, deepest=False):
    """The sound gets quiet (a pause, or the end of a word) and then rises again: the moment of the rise
    that lies closest to t, or None."""
    env = getattr(speakers, "env", None)
    if env is None:
        return None
    i0, i1 = max(int((t - back) * 100), 0), min(int((t + fwd) * 100), len(env))
    if i1 - i0 < 10:
        return None
    sm = np.convolve(env, np.ones(3) / 3.0, mode="same")
    w = sm[i0:i1]
    best = None
    # every local minimum is a candidate; the rise after it is the onset
    for k in range(2, len(w) - 2):
        if w[k] <= w[k - 1] and w[k] <= w[k + 1] and w[max(k - 8, 0):k].max() - w[k] >= 4.0:
            for j in range(k, min(k + 25, len(w))):
                if env[i0 + j] > w[k] + rise_db:
                    c = (i0 + j) / 100.0
                    key = w[k] if deepest else abs(c - t)
                    if best is None or key < best[1]:
                        best = (c, key)
                    break
    return best[0] if best else None


def _after_laugh(speakers, lo, s1):
    """Laughter that Whisper folded into the next line: the loud, bursty stretch from lo ends in a lull, and the
    words come back after it.  Returns (laugh_end, speech_onset) or None."""
    env = getattr(speakers, "env", None)
    if env is None:
        return None
    sm = np.convolve(env, np.ones(5) / 5.0, mode="same")
    i0 = int(lo * 100)
    if i0 + 60 >= len(sm):
        return None
    peak = float(np.max(sm[i0:i0 + 60]))
    thr = peak - 14.0
    j = i0 + 40
    lim = min(i0 + 450, len(sm) - 15)
    while j < lim:                                           # laughter has dips: try every lull in turn
        while j < lim and float(np.max(sm[j:j + 12])) >= thr:
            j += 1
        if j >= lim:
            return None
        tl = j / 100.0
        if tl - lo >= 0.5:
            base = float(np.min(sm[j:j + 12]))
            for k in range(j + 6, min(j + 90, len(sm) - 3)):     # the words come back out of the lull
                if sm[k] > base + 8.0 and float(np.mean(sm[k:k + 25])) > base + 10.0 and float(np.min(sm[k:k + 20])) > base + 5.0:
                    mid = (float(np.min(sm[j:k + 1])) + float(sm[k])) / 2.0
                    while k - 1 > j and sm[k - 1] > mid:     # back to where the level first rose out of the lull
                        k -= 1
                    on = k / 100.0 - 0.03
                    return (tl, on) if on < s1 - 0.3 else None
        while j < lim and float(np.max(sm[j:j + 12])) < thr:   # skip to the end of this lull
            j += 1
    return None


def _after_filler(speakers, t0, s1):
    """A quiet filler ("あー") in front of the words: the words begin where the level steps up."""
    env = getattr(speakers, "env", None)
    if env is None:
        return t0
    sm = np.convolve(env, np.ones(5) / 5.0, mode="same")
    i0 = int(t0 * 100)
    if i0 + 80 >= len(sm):
        return t0
    base = float(np.mean(sm[i0 + 5:i0 + 25]))
    if float(np.std(sm[i0 + 5:i0 + 25])) > 2.0 or base < float(np.percentile(env, 10)) + 12:
        return t0                       # a filler is a flat, quiet stretch
    for j in range(i0 + 20, min(i0 + 60, len(sm) - 12)):
        if np.mean(sm[j:j + 10]) >= base + 5.0 and np.mean(sm[j - 10:j]) < base + 2.5:
            t = j / 100.0 - 0.07
            return t if t < s1 - 0.5 else t0
    return t0


def _nearest_boundary(speakers, s, k, win=0.4):
    """Where the speaker's voice really starts near s: a sound after silence, or the moment the speaker changes
    to k.  s itself when there is no such moment."""
    env = getattr(speakers, "env", None)
    labels = getattr(speakers, "labels", None)
    if env is None or labels is None:
        return s
    hop = speakers.HOP
    cands = []
    lo, hi = max(s - win, 0.2), s + win
    for a, b in _voiced_spans(speakers, lo - 0.15, hi + 0.3, margin_db=10.0, min_len=0.1, join=0.05):
        if a >= lo and a > lo - 0.15 + 0.02:
            i = min(max(int((a + 0.05) / hop), 0), len(labels) - 1)
            kk = int(labels[i])
            if ((1 - kk if speakers.swap else kk) == k):
                cands.append(a - 0.03)
    i0, i1 = max(int(lo / hop), 1), min(int(hi / hop) + 1, len(labels) - 1)
    for i in range(i0, i1):
        if labels[i] != labels[i - 1]:
            kk = int(labels[i])
            if (1 - kk if speakers.swap else kk) == k and labels[i - 1] >= 0:
                t = i * hop + 0.04
                ro_ = _rise_onset(speakers, i * hop, back=0.2, fwd=0.2)
                if ro_ is not None:
                    t = ro_ - 0.03
                if _voiced_spans(speakers, t, t + 0.2, margin_db=8.0, min_len=0.1):
                    cands.append(t)
    if not cands:
        return s
    t = min(cands, key=lambda x: abs(x - s))
    return t if abs(t - s) >= 0.03 else s


def _skip_breath(speakers, t, s1):
    """A breath (or lip noise) in front of the words is far quieter than the words.  If the start sits on a
    quiet stretch of >= 0.12 s that is followed by a clear step up (>= 15 dB), the words begin at the step."""
    env = getattr(speakers, "env", None)
    if env is None:
        return t
    i0 = max(int(t * 100), 0)
    i1 = min(int(min(t + 1.0, s1) * 100), len(env))
    if i1 - i0 < 40:
        return t
    sm = np.convolve(env, np.ones(3) / 3.0, mode="same")
    pk = float(np.percentile(sm[i0:i1], 95))
    for j in range(i0 + 12, min(i0 + 70, i1 - 8)):
        pre = sm[j - 12:j]
        if sm[j] >= pk - 10.0 and float(pre.max()) <= pk - 16.0 and float(sm[j:j + 8].min()) >= pk - 14.0:
            c = j / 100.0 - 0.04
            if os.environ.get("CHCUT_DEBUG"):
                print("BREATH", round(t, 3), "->", round(c, 3), file=sys.stderr)
            return c
        if float(sm[i0:j].max()) >= pk - 12.0:
            return t                        # loud sound already: no breath in front
    return t


def _pull_back(speakers, t, floor_t):
    """The first sound of a phrase can be soft ("先に…"): if a rise out of real silence lies up to 0.2 s before
    the start and the sound goes on without a pause until the start, the phrase begins at that rise."""
    env = getattr(speakers, "env", None)
    if env is None:
        return t
    fl = float(np.percentile(env, 10))
    i1 = int(t * 100)
    lo = max(int(max(t - 0.2, floor_t) * 100), 3)
    if i1 - lo < 3 or i1 >= len(env):
        return t
    j = None
    for k in range(i1 - 1, lo - 1, -1):
        if env[k] <= fl + 8.0:
            break                                   # silence: the sound does not reach back further than this
        j = k
    if j is None or j >= i1 - 1:
        return t
    if float(np.max(env[max(j - 4, 0):j])) > fl + 8.0:
        return t                                    # no silence in front of it: part of an earlier sound
    c = j / 100.0 - 0.02
    if os.environ.get("CHCUT_DEBUG"):
        print("PULLBACK", round(t, 3), "->", round(c, 3), file=sys.stderr)
    return c


def _rate_fix(speech, speakers, a0):
    """A phrase cannot be much longer than it takes to say it.  When a start is far earlier than the phrase's own length
    allows (the Whisper start of a segment that begins with laughter or noise), the phrase begins at the voice onset
    nearest to "end - length"."""
    env = getattr(speakers, "env", None)
    if env is None:
        return
    def moras(p):
        return sum(_mora_w(c) for c in p["text"] if not c.isspace() and c not in "wｗ")
    rs = []
    for p in speech:
        m = moras(p)
        d = p["end"] - p["start"]
        if m >= 5 and 0.3 < d < 3.0 and not re.search(r"[wｗ]{2}", p["text"]):
            rs.append(d / m)
    if len(rs) < 4:
        return
    rs.sort()
    r = min(max(rs[len(rs) // 3], 0.07), 0.16)          # the quicker third: pauses make the others look slow
    for i, p in enumerate(speech):
        m = moras(p)
        if i == 0 or m < 5 or p.get("_fixed") or re.search(r"[wｗ]{2}", p["text"]):
            continue
        exp = p["end"] - m * r * 1.15
        if p["start"] < exp - 0.5:
            c = _rise_onset(speakers, exp, 0.3, 0.3, rise_db=10.0)
            if c is not None and c - 0.02 > p["start"] + 0.3 and c < p["end"] - 0.25 and c - 0.02 > speech[i - 1]["start"] + 0.2:
                if os.environ.get("CHCUT_DEBUG"):
                    print("RATEFIX", p["text"][:8], round(p["start"], 3), "->", round(c - 0.02, 3), file=sys.stderr)
                p["start"] = c - 0.02


def _pace_fix(speech, speakers, a1):
    """Nobody says 15+ mora a second.  A phrase that fast has a wrong edge: the boundary to its slower neighbour is moved
    towards an even pace (snapped to the nearest voice onset), then the phrase is checked again."""
    n = len(speech)
    if n < 2:
        return

    def moras(q):
        return sum(_mora_w(c) for c in q["text"] if not c.isspace() and c not in "wｗ")

    def end(i):
        return speech[i + 1]["start"] if i + 1 < n else a1

    def rate(i):
        m = moras(speech[i])
        return (end(i) - speech[i]["start"]) / m if m else 9.0

    FAST, SLACK = 0.035, 0.115          # only the impossible ones: a quicker-than-human phrase beside a slow one

    def who(q):
        try:
            return speakers.who(q.get("s0", q["start"]), max(q.get("s1", q["start"] + 0.3), q.get("s0", q["start"]) + 0.3))
        except Exception:   # noqa: BLE001
            return None
    for _round in range(2):
        for i in range(n):
            q = speech[i]
            m = moras(q)
            if m < 3 or q.get("_fixed") or re.search(r"[wｗ]{2}", q["text"]) or rate(i) >= FAST:
                continue
            opts = []
            if i > 0 and not speech[i - 1].get("_fixed") and rate(i - 1) > SLACK and who(speech[i - 1]) == who(q):
                opts.append((rate(i - 1), "prev"))
            if i + 1 < n and not speech[i + 1].get("_fixed") and rate(i + 1) > SLACK and who(speech[i + 1]) == who(q):
                opts.append((rate(i + 1), "next"))
            if not opts:
                continue
            _, side = max(opts)
            if side == "prev":
                a, b, m1, m2, tgt = speech[i - 1]["start"], end(i), moras(speech[i - 1]), m, i
            else:
                a, b, m1, m2, tgt = q["start"], end(i + 1), m, moras(speech[i + 1]), i + 1
            if m1 + m2 == 0:
                continue
            bs = a + (b - a) * m1 / (m1 + m2)
            ro = _rise_onset(speakers, bs, 0.25, 0.25, rise_db=8.0)
            nb = (ro - 0.02) if ro is not None else bs
            if not (a + 0.15 < nb < b - 0.15):
                nb = bs
            old = speech[tgt]["start"]
            speech[tgt]["start"] = nb
            if os.environ.get("CHCUT_DEBUG"):
                print("PACEFIX", q["text"][:8], round(old, 3), "->", round(nb, 3), file=sys.stderr)


def _snap_start(speakers, s0, s1, k):
    """Move a phrase start forward to where its own speaker's voice really begins (Whisper starts a phrase
    at the end of the other person's line, or at laughter).  Never earlier, at most 1 s later."""
    env = getattr(speakers, "env", None)
    labels = getattr(speakers, "labels", None)
    if env is None or labels is None:
        return s0
    hop = speakers.HOP

    def mine(t):
        i = min(max(int(t / hop), 0), len(labels) - 1)
        kk = int(labels[i])
        kk = 0 if kk < 0 else kk
        return (1 - kk if speakers.swap else kk) == k

    t = max(s0 - 0.15, 0.0)
    limit = min(s0 + 1.0, s1 - 0.25)
    while t <= limit:
        spans = _voiced_spans(speakers, t, t + 0.25, margin_db=8.0, min_len=0.05, join=0.05)
        vt = sum(b - a for a, b in spans)
        mt = sum(b - a for a, b in spans if mine((a + b) / 2))
        if vt >= 0.2 and mt >= 0.18:
            return s0 if t <= s0 + 0.02 else max(t, spans[0][0])
        t += 0.05
    return s0


LINE_LIMIT = 10


def _telop_max():
    """Longest telop (both lines) that is still left whole: the length that 95% of the finished telops stay within."""
    h = RULES.get("length_histogram") or {}
    try:
        tot = sum(h.values())
        acc = 0
        for n in sorted(int(k) for k in h):
            acc += h[str(n)]
            if acc >= 0.95 * tot:
                return max(17, min(n + 4, 18))
    except Exception:   # noqa: BLE001
        pass
    return 14
_LEAD_WORD_RE = re.compile(r"^((?:あ|え|う|ま)ー?)?(?:じゃあ|じゃ|でも|だから|それで|ほんで|あと|まず|ちなみに|なんか|やっぱ(?!り))")


def _dip_time(speakers, t, w=0.25):
    """The quietest moment around t (the pause between two phrases)."""
    env = getattr(speakers, "env", None)
    if env is None:
        return t
    hz = 100.0
    i0, i1 = max(int((t - w) * hz), 0), min(int((t + w) * hz), len(env))
    if i1 - i0 < 5:
        return t
    seg = env[i0:i1]
    lo = seg.min()
    idx = [i for i in range(len(seg)) if seg[i] <= lo + 1.5]
    return (i0 + (idx[0] + idx[-1]) / 2.0) / hz


def _step_ups(speakers, t, back, fwd, step_db=7.0):
    """Moments where the level steps up by step_db or more (a new word starting without a pause before it)."""
    env = getattr(speakers, "env", None)
    if env is None:
        return []
    i0, i1 = max(int((t - back) * 100), 8), min(int((t + fwd) * 100), len(env) - 6)
    out, last = [], -99
    for i in range(i0, i1):
        before = float(np.mean(env[i - 8:i - 1]))
        after = float(np.mean(env[i:i + 5]))
        if after - before >= step_db and i - last > 8:
            out.append(i / 100.0)
            last = i
    return out


def _refine_cut(speakers, tm, s0, s1):
    """A cut between two telops of one phrase lies where the next words begin: a rise out of a dip or a step up
    of the level - whichever lies nearest to the estimated time."""
    if speakers is None or getattr(speakers, "env", None) is None:
        return tm
    cands = _step_ups(speakers, tm, 0.4, 0.45)
    r = _rise_onset(speakers, tm, 0.4, 0.45)
    if r is not None:
        cands.append(r)
    env = speakers.env
    pk = float(np.percentile(env[int(s0 * 100):max(int(s1 * 100), int(s0 * 100) + 5)], 95))
    ok = []
    for c in sorted(set(round(x, 2) for x in cands)):
        if not (s0 + 0.15 <= c - 0.03 <= s1 - 0.15) or c - s0 < 0.2:
            continue
        lvl = float(np.mean(env[int(c * 100):int(c * 100) + 15]))
        pk = float(np.percentile(env[int(c * 100):int(c * 100) + 60], 95))
        if lvl >= pk - 14.0:            # a breath after a pause is no start of words
            ok.append(c)
    if not ok:
        return tm
    c = ok[0]
    if os.environ.get("CHCUT_DEBUG"):
        print("REFINE", round(tm, 3), ok, round(c, 3), file=sys.stderr)
    return c - 0.03


def _clamp_cut(tm, p, frac=0.5):
    """A cut time always lies inside its phrase (the character times of a merged phrase can run backwards)."""
    lo, hi = p["start"], p["end"]
    if hi - lo < 0.3:
        return (lo + hi) / 2.0
    if not (lo + 0.12 <= tm <= hi - 0.12):
        tm = lo + (hi - lo) * frac               # the character times do not fit the phrase: cut in proportion
    return min(max(tm, lo + 0.12), hi - 0.12)


def _heard_inflection_fix(pieces, segs):
    """The script says "見てます" but the person said "見てるし": same stem, a different ending.  Whisper does not
    mishear an ending like that, so what was said stays (kanji / katakana differences are mishearings: the script wins)."""
    pl = lambda x: re.sub(r"[、。，．,.!！?？…〜~\s]", "", x)
    hira = re.compile(r"^[\u3041-\u3096ー]*$")
    for p in pieces:
        t = p["text"].replace("\n", "")
        if p["text"] == "www" or p.get("_fixed") or len(pl(t)) < 3 or len(pl(t)) > 8:
            continue
        s0, s1 = p.get("s0", p["start"]), p.get("s1", p["end"])
        cand = [g for g in segs if not g.get("skip") and abs(g["start"] - s0) < 0.4 and abs(g["end"] - s1) < 0.6]
        if len(cand) != 1:
            continue
        h, sc = pl(cand[0]["text"]), pl(t)
        k = 0
        while k < min(len(h), len(sc)) and h[k] == sc[k]:
            k += 1
        if k < 2 or k >= len(h) or k >= len(sc) or k < 0.5 * min(len(h), len(sc)):
            continue
        if not (hira.match(h[k:]) and hira.match(sc[k:])) or len(h) > 8:
            continue
        tail = re.search(r"[、。，．,.!！?？…〜~]+$", p["text"])
        p["text"] = cand[0]["text"].strip().replace(" ", "") if not tail else pl(cand[0]["text"]) + tail.group(0)
        p["_heard"] = True


def _restore_lead_words(pieces, lines, speakers):
    """Whisper often drops a short word at the start of a line ("あ、" "ま、" "だから").  When a phrase is exactly the
    rest of a script sentence and sound lies right in front of it, the script's opening word is put back
    and the phrase starts where that sound starts."""
    units = [u for ln in lines for u in _units(ln["text"])]
    pl = lambda x: re.sub(r"[、。，．,.!！?？…〜~\s]", "", x)
    order = sorted(range(len(pieces)), key=lambda i: pieces[i]["start"])
    drop = []
    for pos, i in enumerate(order):
        p = pieces[i]
        t = p["text"].replace("\n", "")
        if p["text"] == "www" or p.get("_fixed") or len(pl(t)) < 3:
            continue
        body = pl(t)
        for u in units:
            un = pl(u)
            k = un.find(body)
            if k < 1 or k > 4 or un[k:] != body and not un[k:].startswith(body):
                continue
            if un[k:] != body:
                continue
            prefix = re.match(r"^(.{%d})" % k, re.sub(r"[\s]", "", u))
            lead = u[:u.find(body[0])] if body[0] in u else ""
            lead = lead if pl(lead) == un[:k] else un[:k]
            prev = pieces[order[pos - 1]] if pos else None
            if prev is not None and pl(prev["text"]) and (un[:k].endswith(pl(prev["text"])) or pl(prev["text"]).endswith(un[:k])):
                if pl(prev["text"]) in ("あ", "ま", "えっと", "えー", "あー", "うん", "ん") and p["start"] - prev["end"] < 0.3:
                    p["text"] = u[:u.find(body[0])] + p["text"] if body[0] in u and pl(u[:u.find(body[0])]) == un[:k] else un[:k] + p["text"]
                    p["start"] = prev["start"]
                    drop.append(prev)           # "あ、" and the phrase after it are one telop
                break                           # the opening word is already a telop of its own
            floor = (prev["start"] + 0.15) if prev is not None else p["start"] - 0.6
            sp_ = _voiced_spans(speakers, max(p["start"] - 0.7, 0), p["start"] + 0.05, margin_db=8.0, min_len=0.06, join=0.05) if getattr(speakers, "env", None) is not None else []
            sp_ = [(a, b) for a, b in sp_ if b >= p["start"] - 0.08 and a < p["start"] - 0.05]
            if not sp_:
                break
            est = 0.1 + 0.12 * len(pl(lead))
            a0_ = max(floor, p["start"] - est)
            if p["start"] - a0_ < 0.1:
                break
            try:
                ws_, wp_ = speakers.who(a0_, p["start"]), speakers.who(p["start"], p["start"] + 0.5)
            except Exception:   # noqa: BLE001
                break
            if ws_ != wp_:                      # the sound in front is the other person's voice
                break
            p["text"] = lead + p["text"]
            p["start"] = a0_
            if prev is not None and prev["end"] > a0_:
                prev["end"] = a0_
            break
    for d_ in drop:
        if d_ in pieces:
            pieces.remove(d_)


def _script_context_fix(pieces, lines, speakers=None):
    """A phrase that is nearly (not exactly) a stretch of the script is written as the script has it
    ("リューウミン…" -> "リューミン…"): the script around it tells what was meant.  Only close matches (>= 0.8)."""
    texts = [re.sub(r"[「」｢｣\s]", "", ln["text"]) for ln in lines]
    flat = "".join(texts)
    for p in pieces:
        t = p["text"].replace("\n", "")
        tt = re.sub(r"[、。，．,.!！?？…〜~]", "", t)
        if len(tt) < 6 or tt in re.sub(r"[、。，．,.!！?？…〜~]", "", flat) or p.get("_fixed") or p["text"] == "www":
            continue
        best, best_r = None, 0.0
        for ln in texts:
            ln2 = re.sub(r"[、。，．,.!！?？…〜~]", "", ln)
            for L in range(max(4, len(tt) - 2), len(tt) + 3):
                for i in range(0, max(len(ln2) - L, 0) + 1):
                    w = ln2[i:i + L]
                    if len(w) < L:
                        continue
                    r = difflib.SequenceMatcher(None, tt, w).ratio()
                    if r > best_r:
                        best, best_r = w, r
        if best and best_r >= 0.8 and best != tt and "\n" not in p["text"]:
            tail = re.search(r"[、。，．,.!！?？…〜~]+$", p["text"])
            p["text"] = best + (tail.group(0) if tail else "")


def _limit_lines(p, speakers=None):
    """No line of a telop is longer than LINE_LIMIT characters.  If two lines do not fit, or the only place for a
    break is inside a word, the phrase is made two telops (cut after a conjunction like "じゃあ" when there is one)."""
    t = p["text"].replace("\n", "")
    if p.get("_exact"):
        return [p]
    tel = _EX["full"].get(t)
    if tel and len(tel) > 1:                             # cut by hand in the "改行テスト": cut the same way
        out, cur = [], p
        for k in range(len(tel) - 1):
            cut = len("".join("".join(l) for l in [tel[k]][0]))
            a, b = _cut_piece(cur, cut, speakers)
            a["_exact"] = True
            out.append(a)
            cur = b
        cur["_exact"] = True
        out.append(cur)
        return out
    if len(t) <= LINE_LIMIT:
        return [p]
    if not p.get("_nocomma") and not p.get("_noclause") and len(t) >= 11:
        _bn = set()
        for _w in list(_WORDS) + ["もらって", "もらっ", "みなかった", "いただ"]:
            _k = t.find(_w)
            while _k != -1:
                _bn.update(range(_k + 1, _k + len(_w)))
                _k = t.find(_w, _k + 1)
        m_ = next((x for x in re.finditer(r"(たら|だと|けど|ので|ても|なら|けれど)(?=.{4,}$)", t) if x.end() not in _bn), None)
        if m_ and m_.end() >= 5:
            cut_ = m_.end()
            n = len(t)
            s0, s1 = p.get("s0", p["start"]), p.get("s1", p["end"])
            ct = p.get("ct") or []
            tm = ct[cut_] if len(ct) >= n else s0 + (s1 - s0) * cut_ / float(n)
            tm = _clamp_cut(_refine_cut(speakers, tm, s0, s1), p, cut_ / float(n))
            a = dict(p, text=t[:cut_], end=tm, s1=tm, _noclause=True)
            b = dict(p, text=t[cut_:], start=tm, s0=tm, _noclause=True)
            if len(ct) >= n:
                a["ct"], b["ct"] = ct[:cut_], ct[cut_:]
            return _limit_lines(a, speakers) + _limit_lines(b, speakers)
    if not p.get("_nocomma"):
        # a long phrase with a comma inside is cut at the comma first (both parts at least 3 characters)
        cs = [i for i, ch in enumerate(t[:-1]) if ch in "、，" and len(t[:i].strip("、，")) >= 3 and len(t[i + 1:].strip("、，")) >= 3]
        if cs:
            i = min(cs, key=lambda i: abs(i + 1 - len(t) / 2.0))
            n = len(t)
            s0, s1 = p.get("s0", p["start"]), p.get("s1", p["end"])
            ct = p.get("ct") or []
            tm = ct[i + 1] if len(ct) >= n else s0 + (s1 - s0) * (i + 1) / float(n)
            tm = _refine_cut(speakers, tm, s0, s1)
            tm = _clamp_cut(tm, p, (i + 1) / float(n))
            a = dict(p, text=t[:i + 1], end=tm, s1=tm)
            b = dict(p, text=t[i + 1:], start=tm, s0=tm)
            if len(ct) >= n:
                a["ct"], b["ct"] = ct[:i + 1], ct[i + 1:]
            out = []
            for x in (a, b):
                out += _limit_lines(x, speakers)
            return out
    banned = set()
    for w in _WORDS:
        k = t.find(w)
        while k != -1:
            banned.update(range(k + 1, k + len(w)))
            k = t.find(w, k + 1)
    ok = [i for i in range(2, len(t) - 1) if len(t) - LINE_LIMIT <= i <= LINE_LIMIT and i not in banned
          and not (_char_class(t[i - 1]) == _char_class(t[i]) and _char_class(t[i - 1]) in "ka")
          and t[i] not in "ぁぃぅぇぉっゃゅょんー！？!?、。"]
    if ok and not _LEAD_WORD_RE.match(t) and len(t) <= _telop_max():
        return [p]                  # _split_two_lines can break it within the limit
    cut = None
    m = _LEAD_WORD_RE.match(t)
    if m and len(t) - m.end() >= 3:
        cut = m.end()
    if cut is None:
        best, best_d = None, 1e9
        for i in range(3, len(t) - 2):
            if i in banned or t[i] in "ぁぃぅぇぉっゃゅょんー！？!?、。":
                continue
            if _char_class(t[i - 1]) == _char_class(t[i]) and _char_class(t[i - 1]) in "ka":
                continue
            d = abs(i - len(t) / 2.0) - (3.0 if t[i - 1] in PARTICLES else 0.0)
            if t[i] in "はがをにでのともへ" and t[i - 1] in "てしでにらい":
                d += 4.0                          # "として|は": a particle does not start a telop
            if t[i:i + 2] in ("くな", "くて", "しい", "しく", "くな") or t[i:i + 3] == "くない":
                d += 4.0                          # "エグ|くなかった": the ending of the adjective stays with it
            if t[i:i + 2] in ("とは", "とか", "って", "とも", "では", "には", "でも") or t[i:i + 3] == "という":
                d += 6.0                          # "再会する|とは…": a particle phrase stays with the word before it
            if t[i] in "方人目回中的性化者達" and _char_class2(t[i - 1]) in ("kanji", "hira"):
                d += 4.0                          # "言い|方": a suffix stays with its word
            if t[i] in "てたでだっ" and _char_class2(t[i - 1]) == "hira" and t[i - 1] not in "ねよかな":
                d += 4.0                          # "聞き流し|てたら": the ending of the verb stays with it
            if t[i - 1].isdigit() or (t[i - 1] in "０１２３４５６７８９"):
                d += 6.0                          # "100|人"
            if t[i - 1] in "。！？!?":
                d -= 3.0
            d += 2.0 * (max(i - _telop_max(), 0) + max(len(t) - i - _telop_max(), 0))
            d -= _pair_bonus(t[i - 1], t[i])
            if _char_class2(t[i - 1]) == "hira" and _char_class2(t[i]) == "hira" and t[i - 1] not in PARTICLES:
                d += 1.5                          # inside a kana word
            if d < best_d:
                best, best_d = i, d
        cut = best
    if cut is None:
        return [p]
    n = len(t)
    s0, s1 = p.get("s0", p["start"]), p.get("s1", p["end"])
    ct = p.get("ct") or []
    if len(ct) >= n:
        tm = ct[cut]
    else:
        tm = s0 + (s1 - s0) * cut / float(n)
    if m and cut == m.end():
        tm = _refine_cut(speakers, tm, s0, s1)
    tm = _clamp_cut(tm, p, cut / float(n))
    a = dict(p, text=t[:cut], end=tm, s1=tm)
    b = dict(p, text=t[cut:], start=tm, s0=tm)
    if len(ct) >= n:
        a["ct"], b["ct"] = ct[:cut], ct[cut:]
    if p.get("_depth", 0) < 3:
        a["_depth"] = b["_depth"] = p.get("_depth", 0) + 1
        return _limit_lines(a, speakers) + _limit_lines(b, speakers)
    return [a, b]


_REHEAR_BAD = re.compile(r"ご視聴|ありがとう|字幕|チャンネル|おやすみ|ご覧|登録")


def _rehear(speakers, t0, t1):
    """Ask Whisper again about a short stretch it left out (a quick "そう" between two lines)."""
    import tempfile
    path = getattr(speakers, "audio_path", None)
    if not path:
        return ""
    try:
        import transcribe as tr
        whisper = tr.find_whisper()
        model = tr.DEFAULT_MODEL
        if not Path(model).exists():
            return ""
    except (SystemExit, Exception):     # noqa: BLE001
        return ""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            wav = str(Path(tmp) / "c.wav")
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % max(t0, 0), "-t", "%.3f" % (t1 - t0), "-i", path,
                                "-ac", "1", "-ar", "16000", "-y", wav], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0:
                return ""
            segs, _toks = tr.run_whisper(whisper, model, wav, "ja", tmp, "r", 0.0)
            return "".join(sg["text"] for sg in segs).strip()
    except (SystemExit, Exception):     # noqa: BLE001
        return ""


def _rehear_bursts(pieces, speakers, a0, a1, laughs):
    """Somebody said something short that Whisper dropped and no script line explains it: listen to it again."""
    if getattr(speakers, "env", None) is None or getattr(speakers, "audio_path", None) is None:
        return pieces
    cover = [(q.get("s0", q["start"]) - 0.12, q.get("s1", q["end"]) + 0.12) for q in pieces]
    add, n = [], 0
    for a, b in _voiced_spans(speakers, a0 + 0.15, a1 - 0.1, margin_db=12.0, min_len=0.12, join=0.1):
        if b - a > 0.9 or n >= 4:
            continue
        if any(a < y and b > x for x, y in cover) or any(a < y_ + 0.05 and b > x_ - 0.05 for x_, y_ in laughs):
            continue
        k = speakers.who(a, b)
        prev = [q for q in pieces if q.get("s1", q["end"]) <= a + 0.05 and q["text"] != "www"]
        if prev and speakers.who(prev[-1].get("s0", prev[-1]["start"]), prev[-1].get("s1", prev[-1]["end"])) == k:
            continue
        n += 1
        txt = _rehear(speakers, a - 0.3, b + 0.3)
        txt = re.sub(r"[\s、。，．,.…]+$", "", txt)
        if not txt or len(ps_norm(txt)) < 1 or len(txt) > 8 or _REHEAR_BAD.search(txt) or re.match(LAUGH_RE_TXT, txt):
            continue
        add.append({"start": a, "end": b, "s0": a, "s1": b, "text": txt, "ct": [a, b], "_fixed": True, "_heard_again": True})
    return sorted(pieces + add, key=lambda q: q.get("s0", q["start"])) if add else pieces


def make_pieces(a0, a1, segs, tokens, block, speakers, runs=None, until=None, max_chars=16, gap=0.45):
    """Subtitle phrases for the audio range [a0, a1]: [{start, end, text, speaker}].

    Phrases are cut where the speaker changes, at punctuation and at pauses.  A phrase that matches
    a script line gets the script's wording; otherwise Whisper's wording (ad-libs) stays."""
    lines = [l for l in block["lines"] if l["kind"] == "speech" and l["norm"]]
    stream, protected, gsegs = [], [], []
    for sg in segs:
        if sg["end"] < a0 - 0.05 or sg["start"] > a1 + 0.05 or sg["skip"]:
            continue
        if until is not None and sg["start"] >= until - 0.05 and sg["start"] > a0 + 0.5:
            continue            # Whisper already counts this as the next take
        if re.match(LAUGH_RE_TXT, sg["text"].strip()):
            continue            # laughter
        if sg["end"] > a1 + 0.5 and sg["start"] > a0 + 0.3:
            nn0 = ps_norm(sg["text"])      # talk that goes on past the cut and is not the script: not part of this take
            if not any(ln["norm"][k:k + 3] in nn0 for ln in lines for k in range(max(len(ln["norm"]) - 2, 0))):
                continue
        cs = _char_stream(sg, tokens, runs, a0, a1)
        nn_ = ps_norm(sg["text"])
        for ln in lines:                      # Whisper heard the whole script line plus something extra
            if len(ln["norm"]) < 4:
                continue
            mb_ = difflib.SequenceMatcher(None, ln["norm"], nn_).get_matching_blocks()
            mt = sum(b_.size for b_ in mb_)
            gap_, pos_ = 0, 0                    # longest run of Whisper characters that are not in the script line
            for b_ in mb_:
                gap_ = max(gap_, b_.b - pos_)
                pos_ = b_.b + b_.size
            if mt >= 0.9 * len(ln["norm"]) and len(nn_) - mt >= 3 and gap_ >= 3:
                protected.append((sg["start"], sg["end"]))
                break
        # punctuation takes the time of the character before it
        cs = [(c, cs[i - 1][1] + 0.01) if (i and STOP_RE.match(c)) else (c, t) for i, (c, t) in enumerate(cs)]
        gsegs.append({"chars": [(c, t) for c, t in cs if c.strip() and a0 - 0.05 <= t <= a1 + 0.05],
                      "partial": sg["start"] < a0 - 0.3 or sg["end"] > a1 + 0.3, "s": sg["start"], "e": sg["end"]})
        # a segment that is a short line of its own in the script ("あ？") starts a new phrase
        nn = ps_norm(sg["text"])
        if cs and 0 < len(nn) <= 3 and any(ln["norm"] == nn for ln in lines):
            cs = [("\x00", cs[0][1])] + cs
        if cs and stream:
            cs = [("\x01", cs[0][1])] + cs     # Whisper starts a new sentence here
        stream += cs
    stream = [(c, t) for c, t in stream if (c.strip()) and a0 - 0.05 <= t <= a1 + 0.05]
    pieces, cur = [], []

    def flush():
        nonlocal cur
        txt = "".join(c for c, _ in cur).strip()
        txt = re.sub(r"[、。，．,.…]+$", "", txt)
        if txt and any(ch not in END_KEEP for ch in txt):
            pieces.append({"start": cur[0][1], "end": cur[-1][1] + 0.12, "text": txt,
                           "s0": cur[0][1], "s1": cur[-1][1] + 0.12, "ct": [t for _, t in cur]})
        cur = []

    short_kanji_line = any(2 <= len(ln['norm']) <= 3 and re.search(r'[\u4e00-\u9fff]', ln['text']) for ln in lines)
    segno, cur_seg = 0, 0
    for si, (c, t) in enumerate(stream):
        if c == "\x00":
            flush()
            continue
        if c == "\x01":
            segno += 1
            if len(cur) >= 5 or (len(cur) >= 2 and cur_seg == segno - 1 and short_kanji_line):
                flush()         # (a whole Whisper sentence of 2-4 characters can be a short script line of its own: "野比？")
            continue
        if cur:
            # punctuation belongs to the word before it, however long the pause before it was
            # a speaker change inside one Whisper sentence is only believed when both parts are long enough
            # (the speaker detector wavers for a few characters: "坂井雅人|かっこよすぎ")
            spk_change = speakers.at(t) != speakers.at(cur[-1][1])
            if spk_change and segno == cur_seg and len(cur) < 5:
                spk_change = False
            if (t - cur[-1][1] > gap and not STOP_RE.match(c)) or spk_change:
                flush()
        if STOP_RE.match(c):
            if c in END_KEEP and cur:
                cur.append((c, t))
            flush()
            continue
        if not cur:
            cur_seg = segno
        cur.append((c, t))
        rest_ = 0                       # characters left in this sentence: do not leave a 1-4 character tail
        for c2, _t2 in stream[si + 1:si + 7]:
            if c2 in ("\x00", "\x01") or STOP_RE.match(c2):
                break
            rest_ += 1
        else:
            rest_ = 9 if rest_ >= 6 else rest_
        if len(cur) >= max_chars and not (len(cur) < max_chars + 5 and rest_ <= 4) and \
                not (len(cur) < RULES['max_title_chars'] + 2 and _is_script_prefix(cur, lines)):
            k = next((i for i in range(len(cur) - 1, len(cur) // 2, -1) if cur[i - 1][0] in PARTICLES), len(cur))
            tail = cur[k:]
            cur = cur[:k]
            flush()
            cur = tail
    flush()
    # a stray last character that Whisper added to a phrase that otherwise is in the script ("見てみ遅")
    for p in pieces:
        n = ps_norm(p["text"])
        if n and not any(n in ln["norm"] for ln in lines):
            for k in (1, 2):
                head = p["text"][:-k]
                hn = ps_norm(head)
                if len(hn) >= 3 and any(hn in ln["norm"] for ln in lines):
                    tail_n = ps_norm(p["text"][-k:])
                    if any(hn in ln["norm"] and tail_n and tail_n in ln["norm"].split(hn, 1)[1] for ln in lines):
                        break           # the script goes on with these characters ("それのび|だ" -> "それのび太だよ")
                    p["text"] = head
                    break
    for p in pieces:
        p["text"] = re.sub(r"^[ょゃゅぁぃぅぇぉっー]+", "", p["text"])
    pieces = [q for p in pieces for q in _split_filler(p)]
    # a phrase that is (most of) a script line, or of a part of it: use the script's wording.  A script line
    # is cut into sentences at 。！？ and quote marks (a telop always ends there); candidates are single
    # sentences, parts of them between "、", and runs of several sentences (cut again afterwards).
    cands = []
    for ln in lines:
        us = _units(ln["text"])
        for i in range(len(us)):
            parts = [x for x in re.split(r"[、，,]", us[i]) if x.strip()]
            for i2 in range(len(parts)):
                for j2 in range(i2 + 1, len(parts) + 1):
                    txt = "、".join(parts[i2:j2])
                    cands.append((ps_norm(txt), txt, txt))
            for j in range(i + 2, len(us) + 1):
                plain = "".join(us[i:j])
                cands.append((ps_norm(plain), "\u0001".join(us[i:j]), plain))
    # Whisper sentences that were cut at the character limit but are one script sentence: join them
    units_ = [(ps_norm(u), u) for ln in lines for u in _units(ln["text"])]
    merged_, i_ = [], 0
    while i_ < len(pieces):
        p = pieces[i_]
        if i_ + 1 < len(pieces) and not p.get("is_filler") and not pieces[i_ + 1].get("is_filler"):
            q = pieces[i_ + 1]
            if q["start"] - p["end"] < 0.8 and speakers.at(p["start"]) == speakers.at(q["start"]):
                nj = ps_norm(p["text"] + q["text"])
                rj = max([ft.ratio(cn_, nj) for cn_, _u in units_ if cn_] + [0])
                r1 = max([ft.ratio(cn_, ps_norm(p["text"])) for cn_, _u in units_ if cn_] + [0])
                r2 = max([ft.ratio(cn_, ps_norm(q["text"])) for cn_, _u in units_ if cn_] + [0])
                if rj >= 0.8 and rj > max(r1, r2) + 0.1:
                    p = dict(p, text=p["text"] + q["text"], end=q["end"], s1=q.get("s1", q["end"]),
                             ct=p.get("ct", []) + q.get("ct", []))
                    i_ += 1
        merged_.append(p)
        i_ += 1
    pieces = merged_
    # a short script line ("そうだな") that Whisper heard as the first syllables of the next phrase: split it off
    out_ = []
    for p in pieces:
        n = ps_norm(p["text"])
        done = False
        for ln in lines:
            ln_n = ln["norm"]
            if not (3 <= len(ln_n) <= 5) or ps_norm(ln["text"]) != ln_n:
                continue
            for k in (2, 3):
                if k < len(ln_n) and n.startswith(ln_n[:k]) and len(n) - k >= 5 and \
                        not any(ln_n in ps_norm(x["text"]) for x in pieces) and len(p.get("ct", [])) >= len(p["text"]):
                    head = {"start": p["start"], "end": p["ct"][k], "s0": p["start"], "s1": p["ct"][k],
                            "text": p["text"][:k], "ct": p["ct"][:k]}
                    rest = dict(p, text=p["text"][k:], start=p["ct"][k], s0=p["ct"][k], ct=p["ct"][k:])
                    out_ += [head, rest]
                    done = True
                    break
            if done:
                break
        if not done:
            out_.append(p)
    pieces = out_
    for i in range(len(lines)):             # runs of whole script lines (short lines said back to back)
        for k in range(2, 4):
            if i + k > len(lines):
                break
            us2 = [u for ln in lines[i:i + k] for u in _units(ln["text"])]
            cands.append((ps_norm("".join(us2)), "\u0001".join(us2), "".join(us2), True))
    used = set()
    for p in pieces:
        if p.get("is_filler"):
            continue
        n = ps_norm(p["text"])
        best, best_r = None, 0.0
        for cn, ct, plain, *_run in cands:
            if not cn or ct in used:
                continue
            r = ft.ratio(cn, n)
            if ASCII_RE.search(plain):          # "VIVANT最新話…": Whisper writes the Latin word in katakana
                cs_ = ps_norm(ASCII_RE.sub("", plain))
                if len(cs_) >= 3:
                    mt_ = sum(b_.size for b_ in difflib.SequenceMatcher(None, cs_, n).get_matching_blocks())
                    r = max(r, 2.0 * mt_ / (len(cs_) + max(len(n) - 4, mt_)))
            if _run:
                mt2 = sum(b_.size for b_ in difflib.SequenceMatcher(None, cn, n).get_matching_blocks())
                if len(n) <= len(cn) + 4:
                    r = max(r, 0.9 * mt2 / len(cn))     # ad-lib particles added to short lines said back to back
                r -= 0.03           # several lines at once only when clearly better than a single one
            if len(cn) >= 3 and cn in n and len(n) - len(cn) >= 2 and not re.fullmatch(r"[あいうえおぁぃぅぇぉっんーはひふへほwｗ笑]*", n.replace(cn, "", 1)):
                continue        # the speaker said more than the script ("それは" + "人ですか"): keep what was said
            if len(n) >= 3 and n in cn and len(cn) - len(n) >= 3:
                rest = cn.replace(n, "", 1)
                one_side = cn.startswith(n) or cn.endswith(n)       # cut at both ends = Whisper lost the edges
                if one_side and not re.fullmatch(r"[あいうえおぁぃぅぇぉっんーはひふへほwｗ笑]*", rest):
                    continue        # the speaker shortened the line ("本当にやめろ" -> "やめろ"): keep what was said
            if r > best_r + 1e-9 or (abs(r - best_r) < 1e-9 and best is not None and len(ct) > len(best)):
                best, best_r, best_plain = ct, r, plain
        if best is not None and best_r >= 0.6 and len(best_plain) <= 2 * RULES["max_title_chars"]:
            if any(a_ - 0.05 <= p["s0"] <= b_ + 0.05 for a_, b_ in protected):
                used.add(best)      # an ad-lib was added to the line ("…女の「インスタの」フォロワー数"): keep what was said
                continue
            p["text"] = best
            p["_sub"] = True
            used.add(best)
    # a short kana phrase ("のび") that is a short script line with kanji ("野比？") nobody else used
    for p in pieces:
        n = ps_norm(p["text"])
        if p.get("_sub") or p.get("is_filler") or not (2 <= len(n) <= 3) or not re.fullmatch(r"[ぁ-んァ-ヶー]+", p["text"]):
            continue
        free = [u for ln in lines for u in _units(ln["text"])
                if re.search(r"[\u4e00-\u9fff]", u) and abs(len(ps_norm(u)) - len(n)) <= 1 and 2 <= len(ps_norm(u)) <= 3 and u not in used]
        if len(free) == 1:
            p["text"] = free[0]
            p["_sub"] = True
            used.add(free[0])
    pieces = [q for p in pieces for q in _expand_cuts(p)]
    pieces = _fix_fillers(pieces)
    sf_laughs = []
    if RULES.get("script_first", True) and lines:
        pieces, sf_laughs = _script_first(gsegs, block, speakers, a0, a1)
    out_ = []
    for p in pieces:                          # "えー、杉田智和さん…": a short word and a comma is a telop of its own
        txt_ = p["text"]
        INTJ = r"(?:あ|え|う|ん|お|いや|はい|おい|ほら|まあ|まぁ|うん|ああ|おお|えっ|へー|ふーん|いやー|えー|あー|うーん|んー|まー)"
        m_ = re.match(r"^(" + INTJ + r"ー?)、(.{2,})$", txt_, re.S)
        keep_comma = bool(m_)
        if not m_:
            m_ = re.match(r"^(いやー|えー|あー|うーん|んー|まー)(?!じゃあ|、|[！？!?])(.{3,})$", txt_, re.S)
        if m_ and len(ps_norm(m_.group(1))) >= 1 and len(ps_norm(m_.group(2))) >= 2 and p.get("ct") and len(p["ct"]) >= 3:
            head_txt = m_.group(1) + ("、" if keep_comma else "")
            n1, n2 = len(ps_norm(m_.group(1))) or 1, len(ps_norm(m_.group(2)))
            ct_ = p["ct"]
            k_ = min(max(int(round(len(ct_) * n1 / float(n1 + n2))), 1), len(ct_) - 1)
            c1, c2 = ct_[:k_], ct_[k_:]
            ps0, ps1 = p.get("s0", p["start"]), p.get("s1", p["end"])
            tm_ = ps0 + min(max(0.14 * len(m_.group(1)), 0.25), 0.4 * (ps1 - ps0))
            if c2[0] < tm_:
                c2 = [tm_ + (ps1 - tm_) * (x + 0.5) / len(c2) for x in range(len(c2))]
            c1 = [ps0 + (tm_ - ps0) * (x + 0.5) / max(len(c1), 1) for x in range(len(c1))] if c1 else [ps0]
            h0 = None
            if getattr(speakers, "env", None) is not None and p.get("s0") is not None:
                # the head was not heard (Whisper left it out): it is the sound right before the phrase
                kk_ = speakers.who(p["s0"] + 0.05, max(p["s1"], p["s0"] + 0.25))
                for a_, b_ in _voiced_spans(speakers, p["s0"] - 0.7, p["s0"] + 0.05, margin_db=10.0, min_len=0.1, join=0.05):
                    if a_ >= a0 - 0.01 and p["s0"] - 0.25 <= b_ <= p["s0"] + 0.05 and 0.25 <= b_ - a_ <= 0.6 and \
                            float(np.mean(speakers.env[max(int((a_ - 0.08) * 100), 0):max(int((a_ - 0.02) * 100), 1)])) <= float(np.max(speakers.env[int(a_ * 100):int(b_ * 100)])) - 20.0 and \
                            speakers.who(a_, b_) == kk_ and not any(q is not p and q.get("s0", 0) < b_ - 0.05 and q.get("s1", 0) > a_ + 0.1 and
                                                                     speakers.who(q["s0"], q["s1"]) == kk_ for q in pieces):
                        h0 = (a_, b_)
                        break
            if os.environ.get('CHCUT_DEBUG'):
                print('HSPLIT', head_txt, ps0, ps1, tm_, h0, file=sys.stderr)
            if h0:
                out_.append(dict(p, text=head_txt, ct=[h0[0], h0[1]], start=h0[0], s0=h0[0], end=h0[1], s1=h0[1], _fixed=True))
                out_.append(dict(p, text=m_.group(2), _fixed=True))
            else:
                # the head is part of the heard phrase: the body starts where the sound dips and rises again
                ro_ = _rise_onset(speakers, ps0 + 0.42, 0.24, 0.28) if ("ー" in m_.group(1) and getattr(speakers, "env", None) is not None) else None
                if ro_ is not None and ps0 + 0.18 <= ro_ <= ps0 + 0.75 and ro_ < ps1 - 0.2:
                    tm_ = ro_ - 0.03
                    c2 = [tm_ + (ps1 - tm_) * (x + 0.5) / len(c2) for x in range(len(c2))]
                    c1 = [ps0 + (tm_ - ps0) * (x + 0.5) / max(len(c1), 1) for x in range(len(c1))] if c1 else [ps0]
                hs_ = None
                if "ー" not in m_.group(1) and getattr(speakers, "env", None) is not None:
                    # the segment starts before the voice (the tail of the other person's word): the head begins at the rise out of the lull
                    ro2_ = _rise_onset(speakers, ps0 + 0.2, 0.2, 0.3, rise_db=10.0)
                    if ro2_ is not None and ps0 + 0.05 < ro2_ < ps0 + 0.5 and ro2_ + 0.45 < ps1:
                        hs_ = ro2_ - 0.02
                        tm_ = hs_ + 0.2
                        c2 = [tm_ + (ps1 - tm_) * (x + 0.5) / len(c2) for x in range(len(c2))]
                        c1 = [hs_ + (tm_ - hs_) * (x + 0.5) / max(len(c1), 1) for x in range(len(c1))] if c1 else [hs_]
                if hs_ is not None:
                    out_.append(dict(p, text=head_txt, ct=c1, start=hs_, s0=hs_, end=tm_, s1=tm_, _fixed=True))
                else:
                    out_.append(dict(p, text=head_txt, ct=c1, end=tm_, s1=tm_, _fixed=True))
                out_.append(dict(p, text=m_.group(2), ct=c2, start=tm_, s0=tm_, _fixed=True))
        else:
            out_.append(p)
    if os.environ.get('CHCUT_DEBUG'):
        print('HEAD', [(round(q.get('s0',q['start']),3), round(q.get('s1',q['end']),3), q['text'], len(q.get('ct') or [])) for q in pieces][:4], file=sys.stderr)
    pieces = out_
    pieces = _fill_missing_lines(pieces, lines, speakers, a0, a1)
    if RULES.get("pause_cut"):                 # off: a pause inside a word (っ etc.) made too many wrong cuts
        pieces = [q for q in (_cut_at_pause(p, speakers) for p in pieces) for q in q]
    # drop a phrase that only repeats the end of the one before (same speaker)
    keep = []
    for p in pieces:
        if keep and speakers.at(p["start"]) == speakers.at(keep[-1]["start"]) and \
                ps_norm(p["text"]) and ps_norm(p["text"]) in ps_norm(keep[-1]["text"]):
            continue
        keep.append(p)
    pieces = keep
    for p in pieces:
        for dc in RULES.get("drop_chars", []):
            p["text"] = p["text"].replace(dc, "")
    for p in pieces:                          # a question that has no "？" in the script: heard as one
        if p.get("_adlib") and not re.search(r"[？?！!]$", p["text"]) and re.search(r"(か|かな|の|でしょ|的な|じゃない)$", p["text"]):
            p["text"] += "？"
    if RULES.get("rehear", True) and not os.environ.get("CHCUT_NO_REHEAR"):
        for p in pieces:                       # a script line nobody heard (Whisper left it out): listen again
            if not p.get("_missing") or getattr(speakers, "audio_path", None) is None:
                continue
            t0_, t1_ = p.get("s0", p["start"]), p.get("s1", p["end"])
            heard = re.sub(r"[\s。，．,.…]+$", "", _rehear(speakers, t0_ - 0.15, t1_ + 0.15))
            if not heard or _REHEAR_BAD.search(heard) or re.match(LAUGH_RE_TXT, heard) or len(heard) > len(p["text"]) + 4:
                continue
            if difflib.SequenceMatcher(None, ps_norm(heard), ps_norm(p["text"])).ratio() >= 0.35:
                p["text"] = heard
                p["_heard_again"] = True
    if RULES.get("rehear", True) and sf_laughs is not None and RULES.get("script_first", True):
        lz_ = [list(x) for x in sf_laughs]
        for sg_ in segs:
            if sg_["end"] > a0 and sg_["start"] < a1 and re.match(LAUGH_RE_TXT, sg_["text"].strip()):
                lz_.append([sg_["start"], sg_["end"]])
        pieces = _rehear_bursts(pieces, speakers, a0, a1, lz_)
    for e_ in (getattr(speakers, "extra", None) or []):       # a wording fixed by hand: {"match": "なるほど", "text": "なるほど？"}
        if e_.get("match") and a0 <= e_["t0"] < a1:
            for q in pieces:
                if q["text"].replace("\n", "") == e_["match"] and abs(q.get("s0", q["start"]) - e_["t0"]) < 0.5:
                    q["text"] = e_["text"]
    for e_ in (getattr(speakers, "extra", None) or []):       # telops given by hand (--extra-telops)
        if e_.get("match"):
            continue
        if a0 <= e_["t0"] < a1 and not any(abs(q.get("s0", q["start"]) - e_["t0"]) < 0.1 and q["text"] == e_["text"] for q in pieces):
            pieces.append({"start": e_["t0"], "end": e_["t1"], "s0": e_["t0"], "s1": e_["t1"], "text": e_["text"],
                           "ct": [e_["t0"], e_["t1"]], "_fixed": True})
    pieces.sort(key=lambda q: q.get("s0", q["start"]))
    pieces = [q for p in pieces for q in _limit_lines(p, speakers)]
    _script_context_fix(pieces, lines, speakers)
    _heard_inflection_fix(pieces, segs)
    _restore_lead_words(pieces, lines, speakers)
    for p in pieces:
        p["text"] = _split_two_lines(p["text"])
        _ls = p["text"].split("\n")
        _PREV_BOTTOM[0] = len(_ls) == 2 and len(_ls[1]) > len(_ls[0])
        p["start"] = max(p["start"], a0)
        _t = p["text"]                      # a line-final 、。 is dropped, except after a short head such as "あ、"
        _body = _t.replace("\n", "")
        if _t and _t[-1] in "、。，," and (len(_body.rstrip("、。，,")) > 3 or _body.rstrip("、。，,").endswith("ー")):
            p["text"] = _t.rstrip("、。，,")
    pieces = [p for p in pieces if len(p["text"].replace("\n", "")) >= 2 or p["text"][-1:] in END_KEEP]

    # laughter is always written "www": Whisper's laughter segments, and the laughter that is kept
    # after the last line when the block has laughter in the script
    laughs = []
    for sg in segs:
        if sg["end"] < a0 or sg["start"] > a1 or not re.match(LAUGH_RE_TXT, sg["text"].strip()):
            continue
        s_, e_ = max(sg["start"], a0), min(sg["end"], a1)
        inside = [(max(x, s_), min(y, e_)) for x, y in (runs or []) if y > s_ and x < e_]
        if inside:
            s_, e_ = min(x for x, _ in inside), max(y for _, y in inside)
        if e_ - s_ >= 0.2:
            laughs.append([s_, e_])
    laughs = [l for l in laughs if not sf_laughs or not any(l[0] < b_ and l[1] > a_ for a_, b_ in sf_laughs)]
    laughs += [list(x) for x in sf_laughs]
    last_end = max([p["end"] for p in pieces], default=a0)
    items_ = [l for l in block["lines"] if l["kind"] in ("speech", "laugh") and (l["kind"] == "laugh" or l["norm"])]
    has_laugh_line = bool(items_) and (items_[-1]["kind"] == "laugh" or items_[-1].get("laugh_after")) \
        if sf_laughs or RULES.get("script_first", True) else \
        any(l["kind"] == "laugh" or l.get("laugh_after") or l.get("laugh_before") for l in block["lines"])
    if has_laugh_line and a1 - last_end >= 0.35 and not any(l[1] > last_end for l in laughs):
        laughs.append([last_end, a1])
    laughs.sort()
    merged = []
    for l in laughs:
        if merged and l[0] - merged[-1][1] < 0.4:
            merged[-1][1] = max(merged[-1][1], l[1])
        else:
            merged.append(l)
    for s_, e_ in merged:
        pieces.append({"start": s_, "end": e_, "text": "www"})
    pieces.sort(key=lambda p: p["start"])

    # no gaps between titles: each one runs until the next speech title begins, the first starts with the
    # clip and the last ends with it.  Laughter ("www") is laid over the title before it, which stays up
    # until somebody speaks again.
    speech = [p for p in pieces if p["text"] != "www"]
    def _tr(stage):
        if os.environ.get("CHCUT_TRACE"):
            print("TRACE", stage, [(p["text"][:5], round(p["start"], 2), bool(p.get("_fixed"))) for p in speech if any(k in p["text"] for k in os.environ["CHCUT_TRACE"].split(","))], file=sys.stderr)
    _tr("init")
    try:
        _quant = [(sg["start"], sg["end"]) for sg in segs if abs(sg["start"] - round(sg["start"])) < 0.006 or abs(sg["end"] - round(sg["end"])) < 0.006]
        if _quant and a1 - a0 < 120 and not os.environ.get("CHCUT_NO_ALIGN"):
            _align_to_turns(speech, speakers, _quant, a0, a1)
    except Exception:   # noqa: BLE001
        pass
    for i, p in enumerate(speech):             # snap each start to where the voice really begins after a pause
        if i and p["start"] > a0 + 0.3 and not p.get("_fixed"):
            if not any(abs(p["start"] - e_) < 0.12 for _s, e_ in list(merged) + list(sf_laughs)):
                p["start"] = max(speakers.onset(p["start"]), speech[i - 1]["start"] + 0.1)
                far_ = speakers.onset_far(p["start"], p.get("s1", p["end"]))
                if far_ - p["start"] > 0.3:            # it was sitting in a long silence: the voice onset is final
                    p["start"] = far_
                    continue
            kk_ = speakers.who(p.get("s0", p["start"]) + 0.4 * (p.get("s1", p["end"]) - p.get("s0", p["start"])),
                               max(p.get("s1", p["end"]), p["start"] + 0.2))
            nb_ = _nearest_boundary(speakers, p["start"], kk_)
            if nb_ < p.get("s1", p["end"]) - 0.25:
                p["start"] = max(nb_, speech[i - 1]["start"] + 0.1)
            p["start"] = max(_snap_start(speakers, p["start"], p.get("s1", p["end"]), kk_), speech[i - 1]["start"] + 0.1)
            if len(p["text"].replace("\n", "")) >= 8 and speakers.who(speech[i - 1]["start"], max(speech[i - 1]["start"] + 0.2, p["start"])) != kk_:
                p["start"] = _after_filler(speakers, p["start"], p.get("s1", p["end"]))
    _tr("snap")
    for i, p in enumerate(speech):             # a breath in front of the words is not the start of the line
        if i and not p.get("_fixed") and p["start"] > a0 + 0.3:
            nb2_ = _skip_breath(speakers, p["start"], p.get("s1", p["end"]))
            if nb2_ > p["start"] and nb2_ < p.get("s1", p["end"]) - 0.2:
                p["start"] = nb2_
    _tr("breath")
    _rate_fix(speech, speakers, a0)
    _tr("rate")
    for i, p in enumerate(speech):             # a soft first sound just before the start belongs to the phrase
        if i and not p.get("_fixed") and p["start"] > a0 + 0.3:
            pb_ = _pull_back(speakers, p["start"], speech[i - 1]["start"] + 0.25)
            if pb_ < p["start"]:
                p["start"] = pb_
    for p in speech:                           # right after laughter: the words begin where the sound comes back out of the lull
        if p.get("_fixed"):
            continue
        lz_ = [w for w in pieces if w["text"] == "www" and w["start"] + 0.12 < p["start"] < w["end"] + 0.8]
        if not lz_ or getattr(speakers, "env", None) is None:
            continue
        ro_ = _rise_onset(speakers, p["start"], 0.35, 0.02, rise_db=10.0)
        if os.environ.get("CHCUT_DEBUG"):
            print("POSTLAUGH", p["text"], round(p["start"], 3), ro_, file=sys.stderr)
        if ro_ is not None and lz_[-1]["start"] + 0.12 < ro_ < p["start"] - 0.02:
            p["start"] = ro_ - 0.02
    _tr("postlaugh")
    _pace_fix(speech, speakers, a1)
    _tr("pace")
    for i, p in enumerate(speech):
        p["end"] = speech[i + 1]["start"] if i + 1 < len(speech) else a1
    if speech:
        speech[0]["start"] = a0
    for p in pieces:                           # laughter ends together with the title it lies on
        if p["text"] == "www":
            under = [q for q in speech if q["start"] <= p["start"] + 0.2]
            if under and under[-1]["end"] > p["start"] + 0.2:
                p["end"] = under[-1]["end"]
    for p in pieces:
        if p["text"] == "www":
            p["speaker"] = -1
        else:
            q0, q1 = p.get("s0", p["start"]), max(p.get("s1", p["end"]), p.get("s0", p["start"]) + 0.25)
            if q1 - q0 > 0.5:                  # the first part can still hold the other person's voice or laughter
                q0 += 0.35 * (q1 - q0)
            p["speaker"] = speakers.who(q0, q1)
    try:
        _alternate_speakers(pieces, lines, speakers)
    except Exception:   # noqa: BLE001
        pass
    return [p for p in pieces if p["end"] - p["start"] > 0.05 or p["text"] == "www"]


def _voice_labels(mono, m, speech, hop_s=0.1, win_s=0.3, nm=26, nc=13):
    """Two voices in one channel: the spectral shape of each 0.3 s stretch (MFCC) is clustered into two groups.
    The group with the lower voice is speaker 0.  None when the two groups are not clearly there."""
    hop, win = int(hop_s * SR), int(win_s * SR)
    f = np.fft.rfftfreq(win, 1.0 / SR)
    mel = lambda h: 2595 * np.log10(1 + h / 700.0)
    inv = lambda v: 700 * (10 ** (v / 2595.0) - 1)
    pts = inv(np.linspace(mel(80), mel(7000), nm + 2))
    fb = np.zeros((nm, len(f)))
    for i in range(nm):
        l, c, r = pts[i:i + 3]
        fb[i] = np.clip(np.minimum((f - l) / (c - l), (r - f) / (r - c)), 0, None)
    dct = np.cos(np.pi * np.outer(np.arange(1, nc + 1), (np.arange(nm) + 0.5)) / nm)
    w = np.hanning(win)
    idx, feats, f0 = [], [], []
    for i in np.where(speech)[0]:
        c0 = int((i + 0.5) * hop - win // 2)
        if c0 < 0 or c0 + win > len(mono):
            continue
        sg = mono[c0:c0 + win] * w
        feats.append(dct @ np.log(fb @ (np.abs(np.fft.rfft(sg)) ** 2) + 1e-10))
        p = pitch(mono, c0 / SR, (c0 + win) / SR)
        f0.append(np.log(p) if p else np.nan)
        idx.append(i)
    if len(idx) < 60:
        return None
    X = np.array(feats)[:, 1:]
    X = (X - X.mean(0)) / (X.std(0) + 1e-9)
    best = None
    for sd in range(8):
        r = np.random.RandomState(sd)
        c = X[r.choice(len(X), 2, replace=False)]
        for _ in range(60):
            a = ((X[:, None] - c[None]) ** 2).sum(2).argmin(1)
            nc_ = np.array([X[a == j].mean(0) if (a == j).any() else c[j] for j in (0, 1)])
            if np.allclose(nc_, c):
                break
            c = nc_
        inert = ((X - c[a]) ** 2).sum()
        if best is None or inert < best[0]:
            best = (inert, a)
    a = best[1]
    if min((a == 0).mean(), (a == 1).mean()) < 0.15:
        return None
    f0 = np.array(f0)
    m0, m1 = np.nanmean(f0[a == 0]), np.nanmean(f0[a == 1])
    if np.isfinite(m0) and np.isfinite(m1) and m0 > m1:
        a = 1 - a                                   # the lower voice is speaker 0
    lab = np.full(m, -1)
    lab[np.array(idx)] = a
    return lab


def _turn_onsets(speakers, lo, hi):
    """Where somebody starts to talk: after a pause (0.2 s of quiet) or where the speaker changes.  [seconds]"""
    sp, lab, hop = getattr(speakers, "speech", None), getattr(speakers, "labels", None), speakers.HOP
    if sp is None or lab is None:
        return []
    env = getattr(speakers, "env", None)
    out = []
    for i in range(2, len(sp) - 3):
        t = i * hop
        if t < lo or t > hi or not sp[i]:
            continue
        quiet_before = not sp[i - 1]
        change = False
        if not quiet_before and sp[i - 1] and lab[i] >= 0 and lab[i - 1] >= 0 and lab[i] != lab[i - 1]:
            change = bool((lab[i:i + 3] == lab[i]).all() and (lab[max(0, i - 3):i] == lab[i - 1]).all())
        if not (quiet_before or change):
            continue
        if quiet_before and env is not None:
            a, b = max(int((t - 0.1) * 100), 0), int((t + 0.3) * 100)
            seg = env[a:b]
            if len(seg):
                k = np.where(seg > seg.max() - 25.0)[0]
                if len(k):
                    t = (a + int(k[0])) / 100.0
        out.append(t)
    return out


def _align_to_turns(speech, speakers, quant, a0, a1, cap=1.3, skip=0.15):
    """Whisper's times of some recordings are rounded to whole seconds, which puts a short answer (いいえ！) up to a
    second late.  The pieces whose Whisper segment has such times are lined up, in order, with the places where
    somebody starts talking (after a pause, or where the speaker changes)."""
    if os.environ.get("CHCUT_DEBUG"):
        print("QUANT", [(round(p.get("s0", p["start"]), 2), bool(p.get("_fixed")), p["text"][:4]) for p in speech], file=sys.stderr)
    flag = [i for i, p in enumerate(speech) if not p.get("_fixed") and p["start"] > a0 + 0.3
            and any(q0 - 0.05 <= p.get("s0", p["start"]) <= q1 + 0.05 for q0, q1 in quant)]
    if not flag:
        return
    taken = [p.get("s0", p["start"]) for i, p in enumerate(speech) if i not in flag]
    on = [t for t in _turn_onsets(speakers, a0, a1) if not any(abs(t - x) < 0.2 for x in taken)]
    if not on:
        return
    n, m = len(flag), len(on)
    INF = 1e9
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    bk = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for k in range(m + 1):
            v = dp[i][k]
            if v >= INF:
                continue
            if k < m and v + skip < dp[i][k + 1]:            # an onset nobody uses (breath, pause inside a line)
                dp[i][k + 1], bk[i][k + 1] = v + skip, ("s", i, k)
            if i < n:
                if v + cap < dp[i + 1][k]:                    # this piece keeps Whisper's time
                    dp[i + 1][k], bk[i + 1][k] = v + cap, ("u", i, k)
                if k < m:
                    d = abs(on[k] - speech[flag[i]].get("s0", speech[flag[i]]["start"]))
                    if d <= cap and v + d < dp[i + 1][k + 1]:
                        dp[i + 1][k + 1], bk[i + 1][k + 1] = v + d, ("m", i, k)
    i, k, asg = n, m, {}
    while (i, k) != (0, 0):
        mv, pi, pk = bk[i][k]
        if mv == "m":
            asg[pi] = on[pk]
        i, k = pi, pk
    for j, t in asg.items():
        p = speech[flag[j]]
        d = t - p.get("s0", p["start"])
        if abs(d) < 0.08:
            continue
        for key in ("start", "s0", "end", "s1"):
            if key in p:
                p[key] += d
        if p.get("ct"):
            p["ct"] = [x + d for x in p["ct"]]
        p["_aligned"] = True
        p["_fixed"] = True
    for a_, b_ in zip(speech, speech[1:]):           # a piece does not reach into the next one (its speaker is read from its own time)
        if a_.get("_aligned") and "s1" in a_:
            a_["s1"] = max(min(a_["s1"], b_["start"]), a_.get("s0", a_["start"]) + 0.25)
        if os.environ.get("CHCUT_DEBUG"):
            print("ALIGN", p["text"].replace("\n", "")[:8], "%+.2f" % d, file=sys.stderr)


def _alternate_speakers(pieces, lines, speakers):
    """One-microphone recordings: a dialogue script gives the speakers in turn.  Every script line is one speaker, the
    next line the other one; the sound of the voices (Speakers.labels) only decides which of the two comes first,
    and can overrule the turn-taking where it is clear."""
    if getattr(speakers, "mode", "") not in ("voice", "pitch") or getattr(speakers, "labels", None) is None:
        return
    sp = [p for p in pieces if p["text"] != "www"]
    if len(sp) < 2 or len(lines) < 2:
        return
    norms = [ln["norm"] for ln in lines]
    cur, ln_of = 0, []
    for p in sp:
        t = re.sub(r"[0-9０-９]", "", ps_norm(p["text"]))
        best_j, best_r, best_c = cur, 0.0, 0.0
        for j in range(cur, min(cur + 3, len(norms))):
            nj = re.sub(r"[0-9０-９]", "", norms[j])
            if not t or not nj:
                continue
            mt = sum(b_.size for b_ in difflib.SequenceMatcher(None, t, nj).get_matching_blocks())
            r = mt / float(len(t))
            c = 2.0 * mt / (len(t) + len(nj)) - 0.01 * (j - cur)
            if (r, c) > (best_r - 0.05 if False else best_r, best_c) and (r > best_r + 0.1 or (abs(r - best_r) <= 0.1 and c > best_c)):
                best_j, best_r, best_c = j, r, c
        if best_r >= 0.5:
            cur = best_j
        ln_of.append(cur)
    groups = {}
    for p, j in zip(sp, ln_of):
        groups.setdefault(j, []).append(p)
    order = sorted(groups)
    if len(order) < 2:
        return
    hop = speakers.HOP
    ev = []                                           # per line: evidence for speaker 1 (+) or 0 (-)
    for j in order:
        tot = 0.0
        for p in groups[j]:
            i0, i1 = int(p.get("s0", p["start"]) / hop), max(int(p.get("s0", p["start"]) / hop) + 1, int(max(p.get("s1", p["end"]), p["start"] + 0.1) / hop))
            seg = speakers.labels[i0:i1]
            vm = speakers.speech[i0:i1] if speakers.speech is not None else None
            if vm is not None and vm.any():
                seg = seg[vm[:len(seg)]]
            seg = seg[seg >= 0]
            tot += float((seg.mean() - 0.5) * len(seg) * hop) if len(seg) else 0.0
        ev.append(max(min(tot, 2.5), -2.5))
    K, SW = 1.2, np.log(0.85 / 0.15)                  # weight of the voice; reward for taking turns
    n = len(order)
    sc = np.zeros((n, 2))
    bp = np.zeros((n, 2), int)
    sc[0] = [-K * ev[0], K * ev[0]]
    for i in range(1, n):
        for s_ in (0, 1):
            cand = [sc[i - 1][t_] + (SW if t_ != s_ else 0.0) for t_ in (0, 1)]
            bp[i][s_] = int(np.argmax(cand))
            sc[i][s_] = max(cand) + (K * ev[i] if s_ == 1 else -K * ev[i])
    st = [int(np.argmax(sc[-1]))]
    for i in range(n - 1, 0, -1):
        st.append(int(bp[i][st[-1]]))
    st.reverse()
    if os.environ.get('CHCUT_DEBUG'):
        print('ALT', ln_of, [round(e_, 2) for e_ in ev], st, file=sys.stderr)
    for j, k in zip(order, st):
        for p in groups[j]:
            p["speaker"] = (1 - k) if speakers.swap else k
            p["_spk_done"] = True


def find_unheard(a0, a1, segs, runs, extra_min=0.15):
    """Places where somebody makes sounds that the text does not account for: a Whisper segment whose
    voiced time is clearly longer than its characters need (an ad-lib like "おっほw" that Whisper could not
    make out and swallowed into the next line).  [(t0, t1)] in audio time."""
    import statistics
    rates = []
    for sg in segs:
        if sg.get("skip"):
            continue
        n = len([c for c in sg["text"] if c.strip() and c not in "、。!?！？"])
        if n:
            v = sum(max(0, min(sg["end"], y) - max(sg["start"], x)) for x, y in (runs or []))
            rates.append(v / n)
    if not rates:
        return []
    med = statistics.median(rates)
    out = []
    for sg in segs:
        if sg.get("skip") or not (a0 - 0.3 <= sg["start"] < a1):
            continue
        s0, s1 = max(sg["start"], a0), min(sg["end"], a1)
        n = len([c for c in sg["text"] if c.strip() and c not in "、。!?！？"])
        if not n or re.match(LAUGH_RE_TXT, sg["text"].strip()):
            continue
        v = sum(max(0, min(s1, y) - max(s0, x)) for x, y in (runs or []))
        if v - (n * med * 1.3 + 0.25) >= extra_min:
            out.append((s0, s1))
    return out


def unheard_pieces(pieces, a0, a1, segs, runs, speakers, min_len=0.45):
    """A voice that Whisper did not hear at all and that no telop covers (a mumble, a word it dropped): a telop "（？）"
    for the stretch, so that the sound does not pass without a mark.  [{start, end, text, speaker}]"""
    cover = []
    for sg in segs:
        cover.append((sg["start"] - 0.15, sg["end"] + 0.15))
    for p in pieces:
        cover.append((p["start"] - 0.15, p.get("end", p["start"] + 0.5) + 0.15))
    cover.sort()
    merged = []
    for x, y in cover:
        if merged and x <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], y)
        else:
            merged.append([x, y])
    out = []
    for x, y in (runs or []):
        x, y = max(x, a0), min(y, a1)
        if y - x < min_len:
            continue
        cur = x
        gaps = []
        for c0, c1 in merged:
            if c1 <= cur or c0 >= y:
                continue
            if c0 > cur:
                gaps.append((cur, c0))
            cur = max(cur, c1)
        if cur < y:
            gaps.append((cur, y))
        for g0, g1 in gaps:
            if g1 - g0 >= min_len:
                try:
                    sp = speakers.who(g0, g1) if speakers is not None else 0
                except Exception:   # noqa: BLE001
                    sp = 0
                out.append({"start": round(g0, 3), "end": round(g1, 3), "text": "（？）", "speaker": sp if sp in (0, 1) else 0})
    return out


ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def ps_norm(text):
    import parse_script as ps
    return ps.normalize(text)


# ------------------------------------------------------------------ FCPXML
def add_effects(res, speakers_cfg, first_id=100):
    """Write one <effect> per speaker into <resources>; returns the effect ids."""
    ids = []
    for k, sp in enumerate(speakers_cfg):
        rid = "r%d" % (first_id + k)
        attrs = {"id": rid, "name": sp["effect"], "uid": sp["uid"]}
        if sp.get("src"):
            attrs["src"] = sp["src"]
        import xml.etree.ElementTree as ET
        ET.SubElement(res, "effect", **attrs)
        ids.append(rid)
    return ids


def place_notes(pieces, block):
    """Notes (※〇〇 in the script) -> [{start, end, text}].  A note is shown from the first to the last
    telop of the script line it belongs to."""
    out = []
    for ln in block["lines"]:
        notes = ln.get("notes") or []
        if not notes or ln["kind"] != "speech" or not ln["norm"]:
            continue
        idx = [i for i, p in enumerate(pieces) if p["speaker"] >= 0 and ps_norm(p["text"])
               and ps_norm(p["text"]) in ln["norm"]]
        if not idx:
            continue
        runs, cur = [], [idx[0]]
        for i in idx[1:]:
            if i == cur[-1] + 1:
                cur.append(i)
            else:
                runs.append(cur)
                cur = [i]
        runs.append(cur)
        best = max(runs, key=lambda r: sum(len(ps_norm(pieces[i]["text"])) for i in r))
        for n in notes:
            out.append({"start": pieces[best[0]]["start"], "end": pieces[best[-1]]["end"], "text": n,
                        "i0": best[0], "i1": best[-1], "with_telop": True})
    if pieces:                              # a note with no line to belong to: the whole block, no telop under it
        for n in block.get("loose_notes") or []:
            out.append({"start": pieces[0]["start"], "end": pieces[-1]["end"], "text": n,
                        "i0": 0, "i1": len(pieces) - 1, "with_telop": False})
    return out


def add_titles(parent, pieces, effect_ids, speakers_cfg, to_video, fps, counter, lo=None, hi=None, block=None, notes_in=None):
    """Connected titles (lane 1) for the phrases.  to_video(t_audio) -> Fraction in the parent's time.
    Titles touch each other; the first starts at the clip start and the last ends at the clip end."""
    import xml.etree.ElementTree as ET
    import make_fcpxml as mf
    F = Fraction(fps)
    fdur = 1 / F
    if not pieces:
        return
    sp_idx = [i for i, p in enumerate(pieces) if p["speaker"] >= 0 and not p.get("ov")]
    starts = [round(to_video(p["start"]) * F) for p in pieces]
    ends = [round(to_video(p["end"]) * F) for p in pieces]
    if sp_idx:
        if lo is not None:
            starts[sp_idx[0]] = round(lo * F)
        for a_, b_ in zip(sp_idx, sp_idx[1:]):
            ends[a_] = starts[b_]
        ends[sp_idx[-1]] = round(hi * F) if hi is not None else ends[sp_idx[-1]]
    for i, p in enumerate(pieces):
        f0, f1 = starts[i], ends[i]
        if lo is not None:
            f0 = max(f0, round(lo * F))
        if hi is not None:
            f1 = min(f1, round(hi * F))
        if p["speaker"] < 0:                                     # laughter: no one-frame gap to the title that follows / the clip end
            edges = [starts[j] for j in sp_idx if starts[j] > f0] + ([round(hi * F)] if hi is not None else [])
            near = [e_ for e_ in edges if 0 < e_ - f1 <= 2]
            if near:
                f1 = min(near)
            if lo is not None and 0 < f0 - round(lo * F) <= 1:
                f0 = round(lo * F)
        if f1 <= f0:
            continue
        if p["speaker"] < 0:                                     # laughter: the "笑" template
            k = next((j for j, c in enumerate(speakers_cfg) if c.get("laugh")), 0)
        else:
            k = min(p["speaker"], len([c for c in speakers_cfg if not c.get("laugh") and not c.get("note")]) - 1)
        cfg = speakers_cfg[k]
        counter[0] += 1
        ts = "ts%d" % counter[0]
        tattrs = {"ref": effect_ids[k], "lane": "2" if p["speaker"] < 0 else ("3" if p.get("ov") else "1"), "offset": mf.t(f0 * fdur),
                  "name": p["text"].replace("\n", "")[:24], "start": "3600s", "duration": mf.t((f1 - f0) * fdur)}
        if cfg.get("role"):
            tattrs["role"] = cfg["role"]
        t = ET.SubElement(parent, "title", **tattrs)
        for prm in cfg.get("params", []):                        # the template's own settings
            ET.SubElement(t, "param", **prm)
        te = ET.SubElement(t, "text")
        st = ET.SubElement(te, "text-style", ref=ts)
        st.text = p["text"]
        d = ET.SubElement(t, "text-style-def", id=ts)
        if cfg.get("style"):                                    # copied from a finished project
            sty = dict(cfg["style"])
            e = ET.SubElement(d, "text-style", **sty)
            if sty.get("kerning"):
                pm = ET.SubElement(e, "param", name="MotionSimpleValues", key="MotionTextStyle:SimpleValues")
                ET.SubElement(pm, "param", name="motionTextTracking", key="tracking", value=str(sty["kerning"]))
        elif cfg.get("plain"):                                  # the template decides the look
            ET.SubElement(d, "text-style", alignment="center")
        else:
            ET.SubElement(d, "text-style", font=cfg.get("font", "Hiragino Sans"), fontSize=str(cfg.get("size", 70)),
                          fontFace=cfg.get("face", "W6"), fontColor=cfg.get("color", "1 1 1 1"), alignment="center")
    if notes_in is not None:                       # notes as set by hand in the app (range = start..end in time)
        notes = []
        for n in notes_in:
            if n.get("deleted") or not (n.get("text") or "").strip():
                continue
            idx = [i for i, p in enumerate(pieces) if p["speaker"] >= 0 and n["start"] - 0.05 <= p["start"] < n["end"] - 0.05]
            if idx:
                notes.append({"start": n["start"], "end": n["end"], "text": n["text"], "i0": idx[0], "i1": idx[-1],
                              "with_telop": n.get("with_telop", True)})
        _note_titles(parent, pieces, notes, effect_ids, speakers_cfg, starts, ends, fdur, counter, mf, ET)
    elif block is not None:
        _note_titles(parent, pieces, place_notes(pieces, block), effect_ids, speakers_cfg, starts, ends, fdur,
                     counter, mf, ET)


def _note_titles(parent, pieces, notes, effect_ids, speakers_cfg, starts, ends, fdur, counter, mf, ET):
    k = next((j for j, c in enumerate(speakers_cfg) if c.get("note")), None)
    if k is None:
        return
    cfg = speakers_cfg[k]
    for n in notes:
        f0, f1 = starts[n["i0"]], ends[n["i1"]]
        if f1 <= f0:
            continue
        counter[0] += 1
        ts = "ts%d" % counter[0]
        tattrs = {"ref": effect_ids[k], "lane": "3", "offset": mf.t(f0 * fdur), "name": "※" + n["text"][:23],
                  "start": "3600s", "duration": mf.t((f1 - f0) * fdur)}
        if cfg.get("role"):
            tattrs["role"] = cfg["role"]
        t = ET.SubElement(parent, "title", **tattrs)
        for prm in cfg.get("params", []):
            prm = dict(prm)
            if prm.get("name") == "位置":       # a telop under the note: a little lower so it can be read; else centre
                if n.get("with_telop"):
                    prm["value"] = RULES.get("note_pos_with_telop", "0 -450")
            ET.SubElement(t, "param", **prm)
        if not any(q.get("name") == "調整" for q in cfg.get("params", [])):
            # size learned from finished edits (learn_note_size.py): a line over the text length, per group
            nn = len("※" + n["text"])
            ns = RULES.get("note_size") or {}
            g = ns.get("with_telop" if n.get("with_telop") else "alone") or {"base": 1.5, "per_char": 0.0, "min": 1.2, "max": 1.9}
            sc = max(g.get("min", 1.2), min(g.get("max", 1.9), g["base"] + g.get("per_char", 0.0) * (nn - 4)))
            ET.SubElement(t, "param", name="調整", key="9999/10003/10043/1/100/105", value="%.2f %.2f" % (sc, sc))
        te = ET.SubElement(t, "text")
        st = ET.SubElement(te, "text-style", ref=ts)
        st.text = "※" + n["text"]
        d = ET.SubElement(t, "text-style-def", id=ts)
        if cfg.get("style"):
            sty = dict(cfg["style"])
            e = ET.SubElement(d, "text-style", **sty)
            if sty.get("kerning"):
                pm = ET.SubElement(e, "param", name="MotionSimpleValues", key="MotionTextStyle:SimpleValues")
                ET.SubElement(pm, "param", name="motionTextTracking", key="tracking", value=str(sty["kerning"]))
        elif cfg.get("plain"):
            ET.SubElement(d, "text-style", alignment="center")
        else:
            ET.SubElement(d, "text-style", font=cfg.get("font", "Hiragino Sans"), fontSize=str(cfg.get("size", 56)),
                          fontFace=cfg.get("face", "W6"), fontColor=cfg.get("color", "1 1 1 1"), alignment="center")


def load_speakers_config(path):
    if not path:
        return list(DEFAULT_SPEAKERS) + [NOTE_FALLBACK]
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not any(c.get("note") for c in cfg):
        cfg = list(cfg) + [NOTE_FALLBACK]
    return cfg
