# -*- coding: utf-8 -*-
"""台本どおり: rewrite the telops of one block with the wording of the script (the speaker said it as written).
The times stay where the heard telops were: every script line takes the time span of the telops that matched it."""
import difflib
import re

import parse_script as ps
import subtitles as sb


def _chars(t):
    return len(re.sub(r"\s", "", t))


def _chunks(unit, limit):
    """A sentence longer than a telop is cut at 、 (or at a particle near the middle)."""
    if _chars(unit) <= limit:
        return [unit]
    parts = [x for x in re.split(r"(?<=[、，,])", unit) if x]
    out, cur = [], ""
    for x in parts:
        if cur and _chars(cur + x) > limit:
            out.append(cur)
            cur = x
        else:
            cur += x
    if cur:
        out.append(cur)
    merged = []
    for o in out:                            # a bit of less than 4 characters is not a telop of its own
        if merged and _chars(merged[-1]) < 4:
            merged[-1] += o
        else:
            merged.append(o)
    if len(merged) > 1 and _chars(merged[-1]) < 4:
        merged[-2] += merged.pop()
    out = merged
    res = []
    for o in out:
        if _chars(o) > limit:
            two = sb._split_two_lines(o).split("\n")          # the same break scoring as inside a telop: never inside a word
            if len(two) == 2:
                k = len(two[0])
            else:
                mid = len(o) // 2
                k = min((i for i in range(2, len(o) - 1) if o[i - 1] in sb.PARTICLES), key=lambda i: abs(i - mid), default=mid)
            res += [o[:k], o[k:]]
        else:
            res.append(o)
    return res


def _match_lines(texts, lines, strict=False):
    """For every (non-laugh) piece the index of the script line it belongs to; in order, never going back."""
    norms = [l["norm"] for l in lines]
    cur, out, last_matched = 0, [], -1
    forced = set()
    for t in texts:
        t = re.sub(r"[0-9０-９]", "", ps.norm_script(t) or "")
        best_j, best_r = None, 0.0
        for j in range(cur, min(cur + 3, len(norms))):
            nj = re.sub(r"[0-9０-９]", "", norms[j])
            if not t or not nj:
                continue
            mt = sum(b.size for b in difflib.SequenceMatcher(None, t, nj).get_matching_blocks())
            r = mt / float(len(t))
            if r > best_r + 0.1 or (best_j is None and r > best_r):
                best_j, best_r = j, r
        if best_j is not None and best_r >= 0.5:
            cur = best_j
            last_matched = best_j
            out.append(best_j)
        else:
            out.append(None)
    # heard wrongly ("黙れ" -> "だわね"): the next script line that nobody has yet takes it
    used = {j for j in out if j is not None}
    for i, j in enumerate(out):
        if j is not None:
            continue
        prev = max([x for x in out[:i] if x is not None], default=-1)
        nxt = min([x for x in out[i + 1:] if x is not None], default=len(lines))
        free = [k for k in range(prev + 1, nxt) if k not in used]
        if strict:
            # automatic mode: an unmatched phrase is an ad-lib and stays as it was, unless it is about as long as a script
            # line that nobody has taken (heard wrongly: "黙れ" -> "だわね")
            if free and abs(len(texts[i]) - len(lines[free[0]]["text"])) <= 3:
                out[i] = free[0]
                forced.add(i)
                used.add(free[0])
        elif free:
            out[i] = free[0]
            used.add(free[0])
        elif prev >= 0:
            out[i] = prev
    return (out, forced) if strict else out


def _alt_script(g, ln, limit):
    """The script line as the other choice of a piece that keeps its heard words."""
    if len(g) != 1 or _chars(ln["text"]) > limit:
        return
    p = g[0][0]
    if "alt" not in p:
        p["alt"] = sb._split_two_lines(ln["text"].strip())
        p["src"] = "h"


def exact_block(pieces, block, auto=False):
    lines = [l for l in block["lines"] if l["kind"] == "speech" and l["norm"]]
    speech = [p for p in pieces if p.get("speaker", 0) >= 0 and (p.get("text") or "").strip() and not p.get("deleted")
              and not (p["text"].replace("\n", "").startswith("※"))
              and not p.get("m")]               # what the person edited by hand is never overwritten
    other = [p for p in pieces if p not in speech]
    if not lines or not speech:
        return pieces
    speech.sort(key=lambda p: p["start"])
    ends = [pe for pe in _ends(pieces, speech)]
    res_ = _match_lines([p["text"].replace("\n", "") for p in speech], lines, strict=auto)
    idx, forced = res_ if auto else (res_, set())
    limit = sb.RULES.get("max_title_chars", 18)
    new, keep = [], []
    for j, ln in enumerate(lines):
        g = [(p, e) for p, e, k in zip(speech, ends, idx) if k == j]
        gf = [i_ for i_, k in enumerate(idx) if k == j and i_ in forced]
        if not g:
            continue
        if auto:
            heard = ps.norm_script("".join(p["text"] for p, _ in g))
            sn = ln["norm"]
            mt = sum(b.size for b in difflib.SequenceMatcher(None, heard, sn).get_matching_blocks())
            dg = lambda x: re.sub(r"[0-9０-９〇一二三四五六七八九十百]", "", x)       # "50連分" heard / "35連分" in the script: not a different sentence
            h2, s2 = dg(heard), dg(sn)
            mt2 = sum(b.size for b in difflib.SequenceMatcher(None, h2, s2).get_matching_blocks())
            if s2 and mt2 >= 0.8 * len(s2) and len(h2) - mt2 >= 2:
                keep.extend(p for p, _ in g)       # said more than the script ("それは" + "人ですか"): what was said stays
                _alt_script(g, ln, limit)
                continue
            if heard == sn and len(g) == 1:
                keep.extend(p for p, _ in g)       # already the script's words
                continue
            if heard == sn and not any(re.search(r"^(あ|え|う|ん|お)ー?$", p["text"].replace("\n", "")) for p, _ in g):
                keep.extend(p for p, _ in g)
                continue
            if not gf and (not heard or mt < 0.75 * len(heard) or mt < 0.6 * len(sn) or len(heard) > len(sn) + 3):
                keep.extend(p for p, _ in g)       # said differently from the script (ad-libs, cut-off line): leave it
                _alt_script(g, ln, limit)
                continue
        units = [c.strip().rstrip('、，,') for u in sb._units(ln["text"]) for c in _chunks(u, limit)]
        units = [u for u in units if u]
        if not units:
            continue
        S = [p["start"] for p, _ in g]
        E = g[-1][1]
        cn = [max(_chars(p["text"]), 1) for p, _ in g]
        tot = float(sum(cn))
        F = [sum(cn[:i]) / tot for i in range(len(g))] + [1.0]
        T = S + [E]

        def at(f):
            for i in range(len(F) - 1):
                if F[i] <= f <= F[i + 1]:
                    w = (f - F[i]) / max(F[i + 1] - F[i], 1e-9)
                    return T[i] + w * (T[i + 1] - T[i])
            return T[-1]
        uc = [max(_chars(u), 1) for u in units]
        ut = float(sum(uc))
        if len(units) == len(g):
            bounds = S + [E]
        else:
            bounds = [at(sum(uc[:i]) / ut) for i in range(len(units))] + [E]
        for i, u in enumerate(units):
            a, b = bounds[i], bounds[i + 1]
            ov = {}
            for p, e in g:
                o = min(b, e) - max(a, p["start"])
                if o > 0:
                    ov[p.get("speaker", 0)] = ov.get(p.get("speaker", 0), 0) + o
            spk = max(ov, key=ov.get) if ov else g[0][0].get("speaker", 0)
            base = dict(g[0][0])
            base.pop("deleted", None)
            for k_ in ("s0", "s1", "ct", "_fixed"):
                base.pop(k_, None)
            hv = [p["text"].replace("\n", "") for p, e in g if a - 1e-6 <= (p["start"] + e) / 2.0 < b + 1e-6]
            if not hv:
                hv = [min(g, key=lambda pe: abs((pe[0]["start"] + pe[1]) / 2.0 - (a + b) / 2.0))[0]["text"].replace("\n", "")]
            base.update(start=round(a, 3), end=round(b, 3), text=sb._split_two_lines(u), speaker=spk,
                        alt=sb._split_two_lines("".join(hv)), src="s")
            new.append(base)
    handled = {id(p) for j_, ln_ in enumerate(lines) for p, k in zip(speech, idx) if k == j_}
    left = []                                # a heard phrase nobody took, lying on the same sound as a script line just written: the same thing twice
    for p, e, k in zip(speech, ends, idx):
        if k is not None:
            continue
        dur = max(e - p["start"], 0.05)
        dup = [n for n in new if n.get("speaker") == p.get("speaker")
               and min(e, n["end"]) - max(p["start"], n["start"]) >= 0.5 * min(dur, max(n["end"] - n["start"], 0.05))]
        if dup:
            n = max(dup, key=lambda n: min(e, n["end"]) - max(p["start"], n["start"]))
            n["alt"] = sb._split_two_lines(p["text"].replace("\n", ""))      # what was heard there stays as the other choice
            n["src"] = "s"
            continue
        left.append(p)
    res = new + other + left + keep
    res = list({id(p): p for p in res}.values())
    res.sort(key=lambda p: p["start"])
    return res


def _ends(pieces, speech):
    """End of each speech piece = start of the next speech piece (or the end given by the piece itself)."""
    out = []
    for i, p in enumerate(speech):
        nxt = speech[i + 1]["start"] if i + 1 < len(speech) else p.get("end", p["start"] + 1)
        out.append(max(nxt, p["start"] + 0.1))
    return out


def _nchars(raw):
    """[(raw index, normalized char)] ; a capital letter is read ("P" -> "ぴー") the way Whisper writes it."""
    out = []
    for i, ch in enumerate(raw):
        rd = ps.read_letters(ch) if ch.isascii() and ch.isalpha() else ch
        for c in rd:
            n = ps.normalize(c)
            if n:
                out.append((i, n))
    return out


def align_alts(pieces, block):
    """The script's wording for each heard telop, found by lining the heard letters up with the script's letters (not by
    time): every telop takes the stretch of the script that its own letters fall on, and the letters nobody heard
    next to it.  Returns a list parallel to `pieces`: the script wording, or None."""
    lines = [l for l in block["lines"] if l["kind"] == "speech" and l["norm"]]
    res = [None] * len(pieces)
    idx = [i for i, p in enumerate(pieces) if p.get("speaker", 0) >= 0 and (p.get("text") or "").strip() and not p.get("deleted")
           and not p.get("ov") and not (p["text"].replace("\n", "").startswith("※"))]
    if not lines or not idx:
        return res
    idx.sort(key=lambda i: pieces[i]["start"])
    quote = "｢｣「」『』"
    S = "".join(re.sub("[%s]" % quote, "", l["text"]) for l in lines)
    Sn = _nchars(S)
    Hn, owner = [], []
    for k, i in enumerate(idx):
        for _, c in _nchars(pieces[i]["text"].replace("\n", "")):
            Hn.append(c)
            owner.append(k)
    if not Sn or not Hn:
        return res
    sm = difflib.SequenceMatcher(None, "".join(Hn), "".join(c for _, c in Sn), autojunk=False)
    hmap = {}
    for b in sm.get_matching_blocks():
        for o in range(b.size):
            hmap[b.a + o] = b.b + o
    n = len(idx)
    lo, hi, head_u, tail_u = [None] * n, [None] * n, [0] * n, [0] * n
    for k in range(n):
        hs = [h for h in range(len(Hn)) if owner[h] == k]
        mt = [hmap[h] for h in hs if h in hmap]
        if not mt or len(mt) < 0.4 * len(hs):
            continue
        lo[k], hi[k] = Sn[min(mt)][0], Sn[max(mt)][0] + 1
        head_u[k] = sum(1 for h in hs[:hs.index(next(h for h in hs if h in hmap))])
        last = max(h for h in hs if h in hmap)
        tail_u[k] = sum(1 for h in hs if h > last)
    ok = [k for k in range(n) if lo[k] is not None]
    for pos, k in enumerate(ok):
        a_lo = ok[pos - 1] if pos else None
        lo_lim = hi[a_lo] if a_lo is not None else 0
        nx = ok[pos + 1] if pos + 1 < len(ok) else None
        hi_lim = lo[nx] if nx is not None else len(S)
        lo[k] = max(lo[k], lo_lim)
        if hi[k] < lo[k]:
            lo[k] = None
            continue
    live = [k for k in ok if lo[k] is not None]
    for pos, k in enumerate(live):
        nx = live[pos + 1] if pos + 1 < len(live) else None
        end_lim = lo[nx] if nx is not None else len(S)
        gap = max(end_lim - hi[k], 0)
        if not gap:
            continue
        ub = head_u[nx] if nx is not None else 0
        ua = tail_u[k]
        give = min(ua, gap)
        punct = re.match(r"[！？!?、。…〜~ー]*", S[hi[k] + give:end_lim])
        hi[k] += give + (len(punct.group(0)) if punct else 0)
        if nx is not None:
            lo[nx] = max(min(ub, end_lim - hi[k]), 0) and end_lim - min(ub, end_lim - hi[k]) or lo[nx]
    for k in ok:
        if lo[k] is None or hi[k] <= lo[k]:
            continue
        txt = re.sub(r"^[！？!?、。…〜~]+", "", S[lo[k]:hi[k]].strip())
        if txt:
            res[idx[k]] = sb._split_two_lines(re.sub(r"\s+", "", txt))
    return res


def gap_text(pieces, k, block):
    """The script wording that fits the empty slot `k` (a （？） telop): the letters of the script between what the telop
    before it and the telop after it have taken.  None when nothing is left over there."""
    lines = [l for l in block["lines"] if l["kind"] == "speech" and l["norm"]]
    idx = [i for i, p in enumerate(pieces) if i != k and p.get("speaker", 0) >= 0 and (p.get("text") or "").strip()
           and not (p["text"].replace("\n", "").startswith("（？")) and not p.get("deleted") and not p.get("ov")
           and not (p["text"].replace("\n", "").startswith("※"))]
    if not lines or not idx:
        return None
    idx.sort(key=lambda i: pieces[i]["start"])
    quote = "｢｣「」『』"
    S = "".join(re.sub("[%s]" % quote, "", l["text"]) for l in lines)
    Sn = _nchars(S)
    Hn, owner = [], []
    for n_, i in enumerate(idx):
        for _, c in _nchars(pieces[i]["text"].replace("\n", "")):
            Hn.append(c)
            owner.append(i)
    if not Sn or not Hn:
        return None
    sm = difflib.SequenceMatcher(None, "".join(Hn), "".join(c for _, c in Sn), autojunk=False)
    lo, hi = {}, {}
    for b in sm.get_matching_blocks():
        for o in range(b.size):
            i = owner[b.a + o]
            si = Sn[b.b + o][0]
            lo[i] = min(lo.get(i, 1 << 30), si)
            hi[i] = max(hi.get(i, -1), si + 1)
    before = [i for i in idx if i < k and i in hi]
    after = [i for i in idx if i > k and i in lo]
    a = hi[before[-1]] if before else 0
    b = lo[after[0]] if after else len(S)
    if b <= a:
        return None
    pos_ = 0
    for l in lines:                        # not across a line of the script (the other person's line in between is not part of it)
        pos_ += len(re.sub("[%s]" % quote, "", l["text"]))
        if pos_ > a:
            b = min(b, pos_)
            break
    txt = re.sub(r"^[！？!?、。…〜~\s]+", "", S[a:b])
    txt = re.sub(r"[\s]+", "", txt)
    if not txt or len(txt) > 24:
        return None
    return sb._split_two_lines(txt)
