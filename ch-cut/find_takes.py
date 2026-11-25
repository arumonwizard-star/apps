#!/usr/bin/env python3
"""
CH Cut - step 3a: take detection

Compares what was actually said (transcript.json) with the script (台本.txt):
  1. finds every attempt (take) at every script block,
  2. checks which script lines each take contains (fuzzy, so small
     recognition errors and ad-libs are tolerated),
  3. judges each take: OK / CHECK (human review) / NG,
  4. proposes the take to use: the LAST take that succeeded.

Usage:
    python3 find_takes.py TRANSCRIPT.json SCRIPT.txt [--json takes.json]

Times are in seconds on the AUDIO file's timeline.
"""
import argparse
import difflib
import json
import re
import sys
import unicodedata

import parse_script as ps

# Typical Whisper hallucinations on silence / noise
HALLUCINATIONS = [
    "ご視聴ありがとうございました", "ご視聴ありがとうございます",
    "チャンネル登録お願いします", "チャンネル登録よろしくお願いします",
    "最後までご視聴いただきありがとうございました", "おやすみなさい",
]
HALLU_NORM = [ps.normalize(h) for h in HALLUCINATIONS]

# Phrases that mean "this take is being abandoned / redone"
RESTART_RE = re.compile(r"もう一回|もう1回|もう一度|もっかい|もういっかい|やり直|やりなお|噛ん|間違え|間違った|ごめん|ミス|NG|ストップ")

CUE_THRESHOLD = 0.70    # similarity needed to recognise the first line of a block
CUE_EXPECTED_THRESHOLD = 0.58  # same, for the current/next block in script order
LINE_OK = 0.62          # line is clearly present
LINE_MAYBE = 0.45       # line is possibly present -> human check
MAX_LINE_GAP = 6.0       # a short reaction line must follow the previous line within this many seconds
SHORT_LINE = 3          # lines this short are matched against single segments only


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def partial(line, text):
    """Best similarity of `line` against any window of `text`."""
    if not line or not text:
        return 0.0
    m = len(line)
    if len(text) <= m + 2:
        return ratio(line, text)
    best = 0.0
    for w in range(max(2, m - 2), m + 3):
        for i in range(0, len(text) - w + 1):
            best = max(best, ratio(line, text[i:i + w]))
    return best


def contain(piece, whole):
    """Share of `piece`'s characters that also occur (in order) inside `whole`."""
    if not piece:
        return 0.0
    sm = difflib.SequenceMatcher(None, piece, whole, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(piece)


def mmss(sec):
    tenths = int(round(sec * 10))
    return "%d:%02d.%d" % (tenths // 600, (tenths // 10) % 60, tenths % 10)


def load_segments(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    segs = []
    try:
        import words as _words
    except Exception:   # noqa: BLE001
        _words = None
    for i, s in enumerate(data["segments"]):
        if _words is not None:
            s["text"] = _words.apply(s["text"])
        n = ps.normalize(s["text"])
        hallu = any(h and (n == h or (len(n) <= len(h) + 4 and ratio(n, h) > 0.85)) for h in HALLU_NORM)
        segs.append({"i": i, "start": s["start"], "end": s["end"], "text": s["text"],
                     "norm": n, "skip": hallu or not n})
    return segs


def find_takes(segs, blocks):
    speech = []
    for b in blocks:
        lines = [l for l in b["lines"] if l["kind"] == "speech" and l["norm"]]
        speech.append(lines)

    # ---- 1. find where each block's first line is spoken ----
    # Blocks are normally shot in script order, so a block that is the current
    # one (retake) or the next one is favoured and accepted with a lower bar.
    starts = []
    cur = -1
    i = 0
    n = len(segs)
    while i < n:
        if segs[i]["skip"]:
            i += 1
            continue
        best = None
        for bi, lines in enumerate(speech):
            if not lines:
                continue
            cue = lines[0]["norm"]
            joined_ = False
            if len(cue) < 4:            # a one-word first line ("おい") is not a cue by itself: use the first real sentence
                allv_ = [l["norm"] for l in lines]
                cands_ = [l["norm"] for l in lines[:4] if len(l["norm"]) >= 6 and allv_.count(l["norm"]) == 1]
                if cands_:
                    cue = max(cands_, key=lambda c_: contain(c_, segs[i]["norm"]))
                elif len(lines) > 1 and allv_.count(lines[1]["norm"]) >= 2:
                    joined_ = True
                    cue = lines[0]["norm"] + lines[1]["norm"]      # the first line is tiny and the others repeat ("中華"+"おかちめんこ")
            c_in = contain(cue, segs[i]["norm"]) if len(cue) >= 8 else 0.0     # a long segment that says the cue inside it
            if len(cue) >= 3 and c_in < 0.75 and cue in segs[i]["norm"] and len(segs[i]["norm"]) - len(cue) <= 4:
                c_in = 1.0      # a short cue with a small lead-in ("それは"+"人ですか"): exact containment beats a fuzzy match to another block
            if contain(segs[i]["norm"], cue) <= 0.6 and c_in < 0.75:
                continue  # the first segment must really belong to the cue
            expected = bi in (cur, cur + 1)
            # a retake repeats the cue word for word (strict); the NEXT block may
            # be misheard more (names etc.), so it gets the lower bar
            if bi == cur + 1:
                thr = CUE_EXPECTED_THRESHOLD
            elif bi == cur and len(cue) >= 10:
                thr = 0.62   # long cue repeated: a name may be misheard
            else:
                thr = CUE_THRESHOLD
            acc = ""
            for k in range(4):
                if i + k >= n or segs[i + k]["skip"]:
                    break
                if k >= 1 and not joined_ and ratio(cue, segs[i + k]["norm"]) >= 0.8:
                    break       # this segment says the whole cue by itself: it is a retake, not part of the first
                acc += segs[i + k]["norm"]
                sc = ratio(cue, acc)
                if joined_ and lines[0]["norm"] not in acc[:len(lines[0]["norm"]) + 2]:
                    continue
                if k == 0 and c_in >= 0.75:
                    sc = max(sc, c_in)
                if sc < thr:
                    continue
                rank = sc + (0.08 if expected else 0.0) + (0.03 if bi == cur + 2 else 0.0)
                if best is None or rank > best[3]:
                    best = (bi, k + 1, sc, rank)
        if best is None and cur >= 0 and i > 0 and segs[i]["start"] - segs[i - 1]["end"] >= 2.0:
            # second chance: the cue itself was misheard ("じゃあまずいんだな"), but the lines that
            # follow it are clearly this block's (a retake of the current block, or the next one)
            for bi in (cur, cur + 1):
                if bi >= len(speech) or len(speech[bi]) < 3 or segs[i]["skip"]:
                    continue
                cue = speech[bi][0]["norm"]
                if len(cue) < 8 or ratio(cue, segs[i]["norm"]) < 0.45:
                    continue
                after = "".join(s_["norm"] for s_ in segs[i + 1:i + 5]
                                if s_["start"] - segs[i]["end"] < 4.0 and not s_["skip"])
                third = speech[bi][2]["norm"]
                if len(third) >= 2 and contain(third, after) >= 0.66:
                    best = (bi, 1, 0.58, 0.58)
                    break
        if best is None and cur >= -1 and not segs[i]["skip"]:
            # third chance (short cue misheard, e.g. "おかめちよこ" -> "おかめ千代子"): the next block is
            # shot right after the current one, and its other lines follow within a few segments
            bi = cur + 1
            if bi < len(speech) and len(speech[bi]) >= 3:
                cue = speech[bi][0]["norm"]
                nxt = [l["norm"] for l in speech[bi][1:5] if len(l["norm"]) >= 3]
                if len(cue) >= 4 and nxt and ratio(cue, segs[i]["norm"]) >= 0.4:
                    after = "".join(s_["norm"] for s_ in segs[i + 1:i + 8]
                                    if s_["start"] - segs[i]["end"] < 8.0 and not s_["skip"])
                    hit_ = sum(1 for t_ in nxt if contain(t_, after) >= 0.6)
                    if hit_ >= max(1, (len(nxt) + 1) // 2):
                        best = (bi, 1, 0.58, 0.58)
        if best and best[1] >= 2 and i + 1 < n:
            # a false start ("恋愛長続き…" and then the whole line said again from the top): the take begins at the repeat
            n0_, n1_ = segs[i]["norm"], segs[i + 1]["norm"]
            if 3 <= len(n0_) < 0.8 * len(n1_) and n1_.startswith(n0_[:max(3, len(n0_) - 1)]) and segs[i + 1]["start"] - segs[i]["end"] < 3.0:
                best = (best[0], best[1] - 1, best[2], best[3])
                i += 1
        if best:
            starts.append({"seg": i, "block": best[0], "cue_score": best[2], "k": best[1]})
            cur = best[0]
            i += best[1]  # the cue may span several segments
        else:
            i += 1

    # the video is shot in script order: keep the longest in-order chain of cue matches (a stray match of a
    # repeated word must not cut the real take short)
    if starts:
        bestv, prv = [], [-1] * len(starts)
        for a_, st_ in enumerate(starts):
            bestv.append(st_["cue_score"] + 0.05)
            for b_ in range(a_):
                if starts[b_]["block"] <= st_["block"] and bestv[b_] + st_["cue_score"] + 0.05 > bestv[a_]:
                    bestv[a_], prv[a_] = bestv[b_] + st_["cue_score"] + 0.05, b_
        a_ = max(range(len(starts)), key=lambda k_: bestv[k_])
        keep_ = set()
        while a_ != -1:
            keep_.add(a_)
            a_ = prv[a_]
        starts = [st_ for k_, st_ in enumerate(starts) if k_ in keep_]

    # ---- 2. build takes ----
    takes = []
    for ti, st in enumerate(starts):
        end_seg = starts[ti + 1]["seg"] - 1 if ti + 1 < len(starts) else n - 1
        rng = [s for s in segs[st["seg"]:end_seg + 1] if not s["skip"]]
        lines = speech[st["block"]]
        # a spoken "let's redo it" ends the take; anything after it is not part of it
        script_text = "".join(l["raw"] for l in lines)
        restart_at = None
        if not RESTART_RE.search(unicodedata.normalize("NFKC", script_text)):
            for ri, s_ in enumerate(rng):
                if ri >= st["k"] and RESTART_RE.search(unicodedata.normalize("NFKC", s_["text"])):
                    restart_at = s_["start"]
                    rng = rng[:ri]
                    break
        text = "".join(s["norm"] for s in rng)

        present, maybe, missing, minor, last_seg = [], [], [], [], st["seg"]
        last_li = 0
        for li, ln in enumerate(lines):
            if li == 0:
                sc = st["cue_score"]
                hit = segs[st["seg"] + st["k"] - 1]
            elif len(ln["norm"]) <= SHORT_LINE:
                sc, hit = 0.0, None
                for s_ in rng[st["k"]:]:
                    # a tiny reaction belongs right after the previous line, not 15 s later
                    if s_["start"] - segs[last_seg]["end"] > MAX_LINE_GAP:
                        continue
                    r = ratio(ln["norm"], s_["norm"])
                    if r < LINE_OK:
                        if s_["norm"].startswith(ln["norm"]) or s_["norm"].endswith(ln["norm"]):
                            r = 0.63   # said as part of a longer utterance
                        elif ln["norm"] in s_["norm"]:
                            r = 0.63 if len(ln["norm"]) >= 3 else 0.5   # inside a longer utterance
                        else:
                            # leading interjection heard differently ("お、正解" vs "あ、マジ正解")
                            core = ln["norm"].lstrip("あいうえおんー")
                            if len(core) >= 2 and core != ln["norm"] and core in s_["norm"]:
                                r = 0.63
                    if r > sc:
                        sc, hit = r, s_
            else:
                sc, hit = partial(ln["norm"], text), None
                # a leading interjection is often heard differently ("お、正解" vs "あ、マジ正解")
                core = ln["norm"].lstrip("あいうえおんー")
                if sc < LINE_OK and len(core) >= 2 and core != ln["norm"] and core in text:
                    sc = 0.63
                # where does the line END? A line can be spoken as several segments
                # ("じゃないよ。" + "出さねえよ"), so use the best window of up to 3 segments.
                best_w = (0.0, None)
                for a_ in range(len(rng)):
                    acc = ""
                    for w_ in range(3):
                        if a_ + w_ >= len(rng):
                            break
                        acc += rng[a_ + w_]["norm"]
                        if w_ > 0 and rng[a_ + w_]["end"] - rng[a_]["start"] > len(ln["norm"]) * 0.35 + 3.0:
                            break           # a window stretched over a long stretch is not one spoken line
                        r_ = ratio(ln["norm"], acc)
                        if r_ > best_w[0] + 0.02:
                            best_w = (r_, rng[a_ + w_])
                hit = best_w[1] if best_w[0] >= 0.5 else None   # a weak window match says nothing about where the line ends
            entry = {"line": ln["raw"], "score": round(sc, 2)}
            if sc >= LINE_OK:
                present.append(entry)
                if hit:
                    last_seg = max(last_seg, hit["i"])
                    last_li = max(last_li, li)
            elif sc >= LINE_MAYBE:
                maybe.append(entry)
                if hit:
                    last_seg = max(last_seg, hit["i"])
                    last_li = max(last_li, li)
            elif len(ln["norm"]) <= SHORT_LINE:
                entry["minor"] = True   # a tiny reaction ("ほぉ" etc.): tolerated
                minor.append(entry)
            else:
                missing.append(entry)

        strict_end = segs[last_seg]["end"]
        # script lines after the last one that was found ("藤原道長じゃねえんだぞ" heard as "藤原の道長的な"):
        # the segments that follow are those lines, however badly they were heard, so the take runs through them
        left_ = len(lines) - 1 - last_li
        for s_ in rng:
            if left_ <= 0:
                break
            if s_["i"] > last_seg and len(s_["norm"]) >= 2 and s_["start"] - segs[last_seg]["end"] < 3.0:
                last_seg = s_["i"]
                left_ -= 1

        total = len(lines)
        if not missing and not maybe:
            status = "OK"
        elif not missing:
            status = "CHECK"
        elif len(missing) <= 1 and len(present) + len(maybe) + len(minor) >= total - 1 and total >= 4:
            status = "CHECK"
        else:
            status = "NG"
        takes.append({
            "block": blocks[st["block"]]["id"],
            "block_index": st["block"],
            "start": segs[st["seg"]]["start"],
            "cue_end": segs[st["seg"] + st["k"] - 1]["end"],
            "restart": restart_at is not None,
            "restart_at": restart_at,
            "cue_first_end": segs[st["seg"]]["end"],
            "cue_first_chars": len(segs[st["seg"]]["norm"]),
            "end": segs[last_seg]["end"],
            "end_strict": strict_end,
            "status": status,
            "present": present, "maybe": maybe, "missing": missing, "minor": minor,
            "lines_total": total,
        })
    return takes


def _in_order(takes, n_blocks):
    """ids of takes kept by the best in-order selection: block numbers rise with time.  Weight = tier-ish
    quality of the take, so a clean take beats a stray cue match."""
    ts = sorted(takes, key=lambda t: t["start"])
    def w(t):
        q = {"OK": 5, "CHECK": 3}.get(t["status"], 1)
        return q + len(t["present"]) * 0.2
    best, prev = [], [-1] * len(ts)
    for i, t in enumerate(ts):
        best.append(w(t))
        for j in range(i):
            if ts[j]["block_index"] <= t["block_index"] and best[j] + w(t) > best[i]:
                best[i], prev[i] = best[j] + w(t), j
    if not ts:
        return set()
    i = max(range(len(ts)), key=lambda k: best[k])
    keep = set()
    while i != -1:
        keep.add(id(ts[i]))
        i = prev[i]
    return keep


def choose(takes, n_blocks, blocks, picks=None):
    """
    Pick one take per block: the LAST take of the best quality tier.
      tier 5: success                       tier 4: success, but a "let's redo it" was said after it
      tier 3: needs-check                   tier 2: needs-check, redo remark after it
      tier 1: failure candidates without a redo remark (best we have: needs a human)
      tier 0: failure candidates with a redo remark
    picks = {"b09": 3} forces the 3rd take of that block.
    """
    picks = picks or {}
    chosen = {}
    # The video is shot in script order: a take that sits out of order (a later block's cue heard
    # earlier, or a block's cue matched at the very end) is dropped.  Keep the largest in-order set.
    ok_ids = _in_order(takes, n_blocks)
    for bi in range(n_blocks):
        ts = [t for t in takes if t["block_index"] == bi and id(t) in ok_ids]
        if not ts:
            ts = [t for t in takes if t["block_index"] == bi] if not any(
                t["block_index"] == bi for t in takes) else []
        if not ts:
            chosen[bi] = None
            continue
        bid = blocks[bi]["id"]
        if bid in picks and 1 <= picks[bid] <= len(ts):
            chosen[bi] = (ts[picks[bid] - 1], "MANUAL")
            continue

        def tier(t):
            r = bool(t.get("restart"))
            if t["status"] == "OK" or (t["status"] == "CHECK" and not t["missing"]):
                # (a take with only unsure lines and nothing missing ranks with the sure ones:
                #  people keep shooting until the last take is the good one)
                return 4 if r else 5
            if t["status"] == "CHECK":
                return 2 if r else 3
            return 0 if r else 1
        # the final block of the script: the last take is the keeper unless it is clearly broken
        # (at least half of its lines heard) - Whisper often cannot read a tired last take
        lastt = ts[-1]
        if bi == n_blocks - 1 and len(ts) > 1 and lastt["status"] != "OK" \
                and 2 * (len(lastt["present"]) + len(lastt["maybe"])) >= lastt["lines_total"]:
            chosen[bi] = (lastt, "CHECK")
            continue
        best = max(tier(t) for t in ts)
        pool = [t for t in ts if tier(t) == best]
        pick = pool[-1]
        # people keep shooting until the last take is the good one: if the last take of the block is
        # not a clear failure (nothing wrong beyond a line or two Whisper could not confirm) and is
        # at least half there, it wins over an earlier sure take
        lt = ts[-1]
        if lt is not pick and lt["status"] in ("OK", "CHECK") and tier(lt) >= 2 and best >= 4:
            pick = lt
        kind = "OK" if (best == 5 and tier(pick) == 5) else ("CHECK" if tier(pick) >= 2 else "NG-only")
        chosen[bi] = (pick, kind)
    return chosen


def parse_picks(items):
    out = {}
    for it in items:
        try:
            k, v = it.split("=")
            out[k.strip().lower()] = int(v)
        except ValueError:
            sys.exit("--pick must look like b09=3, got: %s" % it)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript")
    ap.add_argument("script")
    ap.add_argument("--json")
    ap.add_argument("--pick", action="append", default=[],
                    help="force a take, e.g. --pick b09=3 (3rd take of block b09)")
    args = ap.parse_args()

    with open(args.script, encoding="utf-8") as f:
        blocks = ps.parse(f.read())
    segs = load_segments(args.transcript)
    takes = find_takes(segs, blocks)
    chosen = choose(takes, len(blocks), blocks, parse_picks(args.pick))

    label = {"OK": "成功", "CHECK": "要確認", "NG": "失敗候補"}
    for bi, b in enumerate(blocks):
        ts = [t for t in takes if t["block_index"] == bi]
        print("■ %s  %s" % (b["id"], b["cue"]))
        if not ts:
            print("    (テイクが見つかりません)")
            continue
        pick = chosen[bi][0] if chosen[bi] else None
        for k, t in enumerate(ts, 1):
            mark = "  ← 採用" if t is pick else ""
            if t.get("restart"):
                mark += "  (やり直し発言あり)"
            print("    テイク%d  %s - %s  %s  (%d/%d行)%s" % (
                k, mmss(t["start"]), mmss(t["end"]), label[t["status"]],
                len(t["present"]), t["lines_total"], mark))
            for m in t["maybe"]:
                print("        ? 不確か: %s (%.2f)" % (m["line"], m["score"]))
            for m in t["missing"]:
                print("        × 見つからない: %s" % m["line"])
            for m in t["minor"]:
                print("        △ 軽微な欠け(許容): %s" % m["line"])
        if chosen[bi] and chosen[bi][1] not in ("OK", "MANUAL"):
            print("    ※ 完全に成功したテイクがありません。人の確認が必要です。")
    if args.json:
        out = {"takes": takes,
               "chosen": {blocks[bi]["id"]: (None if c is None else {"start": c[0]["start"], "end": c[0]["end"], "status": c[1]})
                          for bi, c in chosen.items()}}
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("saved:", args.json)


if __name__ == "__main__":
    main()
