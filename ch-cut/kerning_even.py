"""Even letter spacing for telops: every gap between two letters looks the same.

The gap between two neighbouring letters is measured from the font itself (ink of the letter before to ink of the letter
after, so "ー", small kana, "、", "！" and "？" that sit in a big empty cell are pulled in), and the kerning of each
letter is set so that all gaps equal the usual gap of the line.  Without the font file, fixed rules by kind of letter
are used.  Kerning is in points like the template's own value (-19 at size 200)."""
import glob
import os
import re
from pathlib import Path

_FONTS = {}
_DIRS = ["~/Library/Fonts", "/Library/Fonts", "/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
         "/Library/Application Support/Apple/Fonts", "/usr/share/fonts"]
_MET = {}


def _norm(s):
    return re.sub(r"[\s_\-]", "", s or "").lower()


def find_font(name):
    if os.environ.get("CHCUT_FONT"):
        return os.environ["CHCUT_FONT"]
    key = _norm(name)
    if key in _FONTS:
        return _FONTS[key]
    hit = None
    for d in _DIRS:
        for f in glob.glob(os.path.join(os.path.expanduser(d), "**", "*"), recursive=True):
            if f.lower().endswith((".ttf", ".otf", ".ttc")) and key and key in _norm(Path(f).stem):
                hit = f
                break
        if hit:
            break
    _FONTS[key] = hit
    return hit


def _metrics(fp, ch):
    k = (fp, ch)
    if k not in _MET:
        from PIL import ImageFont
        f = ImageFont.truetype(fp, 200)
        adv = f.getlength(ch) / 200.0
        bb = f.getmask(ch).getbbox()                 # the ink of the letter (x from the letter's origin)
        _MET[k] = (adv, bb[0] / 200.0, bb[2] / 200.0) if bb else (adv, 0.0, adv)
    return _MET[k]


_CLASS_EM = {                      # fallback: extra tightening after the letter, in em (negative = closer)
    "、": -0.34, "。": -0.34, "，": -0.34, "．": -0.34, "・": -0.22, "！": -0.06, "？": -0.06, "!": -0.06, "?": -0.06,
    "っ": -0.05, "ゃ": -0.05, "ゅ": -0.05, "ょ": -0.05, "ッ": -0.05, "ャ": -0.05, "ュ": -0.05, "ョ": -0.05,
    "「": -0.16, "『": -0.16, "（": -0.16, "」": 0.0, "』": 0.0, "）": 0.0,
}
_BEFORE_EM = {"「": -0.0, "『": 0.0, "（": 0.0, "！": -0.05, "？": -0.05, "!": -0.05, "?": -0.05}


def even_kerning(line, size, base, font_name, clamp=0.075, strength=0.8):
    """Kerning (points) after each letter of `line`; the last one is None (it stays unkerned)."""
    n = len(line)
    out = [base] * n
    if n < 2:
        return [None] * n
    fp = None
    try:
        fp = find_font(font_name)
    except Exception:   # noqa: BLE001
        fp = None
    ks = []
    if fp:
        try:
            m = [_metrics(fp, c) for c in line]
            gaps = [(m[i][0] - m[i][2]) + m[i + 1][1] for i in range(n - 1)]
            ref = sorted(gaps)[len(gaps) // 2]
            ks = [base + max(-(0.42 if line[i] in "、。，．" else clamp), min(clamp, (ref - g) * strength)) * size
                  for i, g in enumerate(gaps)]            # after 、。 the empty half of the cell is closed up fully
        except Exception:   # noqa: BLE001
            ks = []
    if not ks:
        ks = []
        for i in range(n - 1):
            em = _CLASS_EM.get(line[i], 0.0) + _BEFORE_EM.get(line[i + 1], 0.0)
            ks.append(base + max(-0.3, em) * size)
    return [round(k, 2) for k in ks] + [None]


def mono_kerning(line, size, base, font_name):
    """和文等幅: every letter takes the width of a full-width letter (narrow letters such as latin and digits are padded)."""
    n = len(line)
    fp = None
    try:
        fp = find_font(font_name)
    except Exception:   # noqa: BLE001
        fp = None
    out = []
    for c in line:
        adv = None
        if fp:
            try:
                adv = _metrics(fp, c)[0]
            except Exception:   # noqa: BLE001
                adv = None
        if adv is None:
            adv = 0.5 if ord(c) < 0x2000 or 0xFF61 <= ord(c) <= 0xFF9F else 1.0
        out.append(round(base + max(0.0, 1.0 - adv) * size, 2))
    if n:
        out[-1] = None
    return out
