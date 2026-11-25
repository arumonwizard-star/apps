"""Japanese word splitting for the telop word boxes (a small MeCab-style lattice search over the IPA dictionary).
chunks(text) -> [(start, end), ...]  the boxes of the text, e.g. 一番可愛い子にはなれない -> 一番|可愛い|子|には|なれない"""
import gzip
import os
import threading
from array import array

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jpdic")
_LEX = None
_CONN = None
_POS = None
_N = 1316
_lock = threading.Lock()
_MAXW = 12
# unknown words: (left/right id, cost, longest run)
_UNK = {"KANJI": (1285, 11426, 2), "HIRAGANA": (1285, 13069, 2), "KATAKANA": (1285, 9461, 30), "ALPHA": (1285, 13398, 30),
        "NUMERIC": (1295, 27386, 30), "SYMBOL": (1283, 17585, 1), "OTHER": (5, 4769, 1)}


def _cat(c):
    o = ord(c)
    if 0x3041 <= o <= 0x309F:
        return "HIRAGANA"
    if 0x30A1 <= o <= 0x30FF or o == 0x30FC or 0xFF66 <= o <= 0xFF9F:
        return "KATAKANA"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or o in (0x3005, 0x3007):
        return "KANJI"
    if c.isdigit() or 0xFF10 <= o <= 0xFF19:
        return "NUMERIC"
    if c.isalpha():
        return "ALPHA"
    if c.isspace():
        return "SPACE"
    return "SYMBOL"


def _load():
    global _LEX, _CONN, _POS
    if _LEX is not None:
        return
    with _lock:
        if _LEX is not None:
            return
        lex = {}
        with gzip.open(os.path.join(_DIR, "lex.tsv.gz"), "rt", encoding="utf-8") as f:
            pos = []
            for line in f:
                line = line.rstrip("\n")
                if line == "":
                    break
                pos.append(tuple(line.split(",")[:2]))
            for line in f:
                f_ = line.rstrip("\n").split("\t")
                lex.setdefault(f_[0], []).append((int(f_[1]), int(f_[2]), int(f_[3]), int(f_[4]), f_[5] if len(f_) > 5 else ""))
        a = array("h")
        with gzip.open(os.path.join(_DIR, "conn.bin.gz"), "rb") as f:
            a.frombytes(f.read())
        _POS, _CONN, _LEX = pos, a, lex


def tokens(text):
    """[(start, end, (pos1, pos2))] for text without line breaks."""
    _load()
    n = len(text)
    if n == 0:
        return []
    INF = 1 << 60
    ends = [dict() for _ in range(n + 1)]       # ends[j][rid] = (cost, start, back_rid, pos)
    ends[0][0] = (0, -1, -1, None)
    cats = [_cat(c) for c in text]
    for i in range(n):
        if not ends[i]:
            continue
        cand = []
        for L in range(1, min(_MAXW, n - i) + 1):
            for (l, r, c, p, bf) in _LEX.get(text[i:i + L], ()):
                cand.append((L, l, r, c, _POS[p] + ((bf,) if bf else ())))
        ct = cats[i]
        u = _UNK.get(ct, _UNK["OTHER"])
        if ct == "SPACE":
            cand.append((1, 9, 9, 8903, ("記号", "空白")))
        else:
            run = 1
            while i + run < n and cats[i + run] == ct and run < u[2]:
                run += 1
            hasdict = any(L == 1 for L, *_ in cand) or bool(cand)
            for L in range(1, run + 1):
                if ct in ("KATAKANA", "ALPHA", "NUMERIC") and L != run:
                    continue
                if hasdict and ct in ("HIRAGANA", "KANJI") and L > 1:
                    continue
                cand.append((L, u[0], u[0], u[1] + (0 if ct in ("KATAKANA", "ALPHA", "NUMERIC") else 3000 * (L - 1)), ("名詞", "一般") if ct != "SYMBOL" else ("記号", "一般")))
        for (L, l, r, c, p) in cand:
            best = None
            for prid, (pc, *_x) in ends[i].items():
                v = pc + _CONN[prid * _N + l] + c
                if best is None or v < best[0]:
                    best = (v, prid)
            if best is None:
                continue
            j = i + L
            cur = ends[j].get(r)
            if cur is None or best[0] < cur[0]:
                ends[j][r] = (best[0], i, best[1], p)
    # EOS
    fin = None
    for rid, (pc, *_x) in ends[n].items():
        v = pc + _CONN[rid * _N + 0]
        if fin is None or v < fin[0]:
            fin = (v, rid)
    out = []
    j, rid = n, fin[1]
    while j > 0:
        c, i, prid, p = ends[j][rid]
        out.append((i, j, p))
        j, rid = i, prid
    out.reverse()
    return out


_firsts = []
_AUXV = {"いる", "ある", "くれる", "くる", "来る", "いく", "行く", "しまう", "おく", "みる", "見る", "あげる", "もらう", "やる", "くださる", "いただく", "いらっしゃる", "ほしい", "欲しい", "ちゃう", "する", "なる", "できる", "れる", "られる", "せる", "させる"}
_SEP = "、。，．,.！？!?…・「」『』（）()～~ー"


def chunks(text):
    """Word boxes of a telop text: list of (start, end) into text; line breaks / blanks are skipped."""
    res = []
    k = 0
    n = len(text)
    while k < n:
        if text[k].isspace():
            k += 1
            continue
        e = k
        while e < n and not text[e].isspace():
            e += 1
        res.extend((a, b) for a, b, _p in _chunks_run(text[k:e], k))
        k = e
    return res


def _chunks_run(s, off):
    tk = tokens(s)
    boxes = []          # [start, end, p1, p2, firstTok]
    for (a, b, p) in tk:
        p1, p2 = p[0], p[1]
        bf = p[2] if len(p) > 2 else ""
        surf = s[a:b]
        glue = False
        if boxes:
            L = boxes[-1]
            lp1 = L[5]       # pos of the last token in the box
            lp2 = L[6]
            lsurf = s[L[0]:L[1]]
            if p1 == "記号":
                glue = True
            elif lp1 == "接頭詞":
                glue = True
            elif p1 == "助動詞":
                glue = True
            elif p1 == "動詞" and (p2 == "接尾" or (p2 == "非自立" and bf in _AUXV)):
                glue = True
            elif p1 == "助詞" and p2 == "接続助詞" and surf in ("て", "で", "ば", "ちゃ", "じゃ", "ちゃう") and lp1 in ("動詞", "形容詞", "助動詞"):
                glue = True
            elif p1 == "助詞" and lp1 == "助詞" and p2 != "終助詞x":
                glue = True
            elif p1 == "名詞" and p2 == "接尾":
                glue = True
            elif p1 == "名詞" and lp1 == "名詞" and p2 not in ("非自立", "代名詞", "数") and lp2 not in ("代名詞", "数", "非自立") and (b - L[0]) <= 5:
                glue = True
            elif p1 == "形容詞" and p2 == "接尾":
                glue = True
            elif surf in ("ちゃう", "ちゃっ", "ちゃ", "じゃう", "じゃっ", "ちまう", "ちまっ") and lp1 in ("動詞", "助動詞"):
                glue = True
            elif p1 == "名詞" and p2 == "非自立" and surf in ("の", "ん") and lp1 in ("動詞", "形容詞", "助動詞"):
                glue = True
            elif p1 == "助詞" and lp1 == "名詞" and lp2 == "非自立" and surf in ("は", "が", "を", "も", "に", "で", "と"):
                glue = True
            elif lp1 == "記号" and False:
                glue = True
        if glue:
            boxes[-1][1] = b
            boxes[-1][5] = p1
            boxes[-1][6] = p2
        else:
            boxes.append([a, b, p1, p2, p1, p1, p2])
    out = []
    pend = None
    for bx in boxes:
        a, b = bx[0], bx[1]
        if pend is not None:
            a = pend
            pend = None
        if all(c in _SEP for c in s[a:b]) and not out:
            pend = a
            continue
        out.append([a, b, bx[4]])
    if pend is not None:
        out.append([pend, len(s), "記号"])
    return [(off + a, off + b, p1) for a, b, p1 in out]


def chunk_info(text):
    """[(start, end, first part of speech)] of the boxes: the first token's class tells what kind of box it is."""
    res = []
    k = 0
    n = len(text)
    while k < n:
        if text[k].isspace():
            k += 1
            continue
        e = k
        while e < n and not text[e].isspace():
            e += 1
        res.extend(_chunks_run(text[k:e], k))
        k = e
    return res
