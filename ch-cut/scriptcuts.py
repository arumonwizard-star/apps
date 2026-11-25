"""Where the script itself has a 、 or 。: a telop is cut there (and the mark is not shown).  Later steps that move or join
cuts (word-cut repair, learned joins) must not cross such a place."""
import re

_CUTS = set()


def _n(s):
    return re.sub(r"[\s、。，．,.！？!?…「」｢｣※]", "", s or "")


def load(text):
    _CUTS.clear()
    for ln in (text or "").splitlines():
        for m in re.finditer(r"[、。，]", ln):
            a, b = _n(ln[:m.start()]), _n(ln[m.end():])
            for k in (1, 2, 3):
                if len(a) >= k and len(b) >= k:
                    _CUTS.add((a[-k:], b[:k]))


def is_cut(ta, tb):
    a, b = _n(ta), _n(tb)
    k = min(3, len(a), len(b))
    return k > 0 and (a[-k:], b[:k]) in _CUTS


def finalize(text):
    """A telop never starts with 、。 and does not end with one (except a short head such as "あ、" / "いや、")."""
    if not text:
        return text
    lines = [re.sub(r"^[、。，,．.\s]+", "", l) for l in text.split("\n")]
    lines = [l for l in lines if l != ""] or [text.strip("、。，, \n")]
    t = "\n".join(lines)
    body = t.replace("\n", "").rstrip("、。，,")
    if t[-1:] in "、。，," and (len(body) > 3 or body.endswith("ー")):
        t = t.rstrip("、。，,")
    return t


def collapse_repeat(text, dur):
    """The same short phrase said twice in a time that cannot hold both ("言い過ぎ言い過ぎ" in 0.4 s): written once."""
    flat = text.replace("\n", "")
    m = re.fullmatch(r"(.{2,8}?)\1+", flat)
    if m and len(flat) / max(dur, 0.05) > 12:
        return m.group(1)
    return text
