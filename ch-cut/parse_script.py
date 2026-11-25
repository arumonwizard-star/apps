#!/usr/bin/env python3
"""
CH Cut - step 2a: script (台本) parser

Splits a plain-text script into blocks (separated by blank lines) and
classifies each line:
    speech     normal line of dialogue
    laugh      laughter such as "www", "（笑）"  (not subtitled)
    direction  a line that is only a stage direction, e.g. （間）, 【SE】

Brackets inside a speech line, e.g. "そうだね（笑）", are stripped from the
spoken text and kept as `directions`.

Usage:
    python3 parse_script.py SCRIPT.txt [--json out.json]
"""
import argparse
import json
import re
import sys
import unicodedata

LAUGH_RE = re.compile(r"^[wWｗＷ草笑ｗ!！?？、。\s]*[wWｗＷ草笑][wWｗＷ草笑!！?？、。\s]*$")
BRACKET_PAIRS = [("（", "）"), ("(", ")"), ("【", "】"), ("［", "］"), ("[", "]"), ("＜", "＞"), ("<", ">")]
BRACKET_RE = re.compile(r"[（(【［\[＜<][^）)】］\]＞>]*[）)】］\]＞>]")
PUNCT_RE = re.compile(r"[\s\u3000、。，．,.!！?？…・ー〜~「」『』\"'’“”\-]+")


def normalize(text):
    """Form used for fuzzy matching: NFKC, no spaces/punctuation, katakana->hiragana."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = PUNCT_RE.sub("", text)
    out = []
    for ch in text:
        o = ord(ch)
        if 0x30A1 <= o <= 0x30F6:  # katakana -> hiragana
            ch = chr(o - 0x60)
        out.append(ch)
    return "".join(out)


EDGE_LAUGH_RE = re.compile(r"^([wWｗＷ]{2,})|([wWｗＷ]{2,})$")


NOTE_MARK = "※"


ASCII_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’._-]*")


LETTER_READ = {"A": "えー", "B": "びー", "C": "しー", "D": "でぃー", "E": "いー", "F": "えふ", "G": "じー", "H": "えいち", "I": "あい",
               "J": "じぇー", "K": "けー", "L": "える", "M": "えむ", "N": "えぬ", "O": "おー", "P": "ぴー", "Q": "きゅー",
               "R": "あーる", "S": "えす", "T": "てぃー", "U": "ゆー", "V": "ぶい", "W": "だぶりゅー", "X": "えっくす",
               "Y": "わい", "Z": "ぜっと"}


def read_letters(text):
    """A capital letter standing alone ("PコートのP") is said as its name ("ぴー"), and Whisper writes it that way."""
    t = unicodedata.normalize("NFKC", text)
    return re.sub(r"(?<![A-Za-z])([A-Z])(?![A-Za-z])", lambda m: LETTER_READ[m.group(1)], t)


def norm_script(spoken):
    """Normalized script text for matching.  Latin-letter words ("VIVANT") are left out: Whisper writes
    them in katakana ("ビバン"), which cannot be matched letter by letter."""
    spoken = read_letters(spoken)
    base = normalize(spoken)
    nf = unicodedata.normalize("NFKC", spoken)
    nf = re.sub(r"(?<![0-9])([1-9])(?![0-9])", lambda m: "〇一二三四五六七八九"[int(m.group(1))], nf)   # "1つ" is heard as "一つ"
    stripped = normalize(ASCII_WORD_RE.sub("", nf))
    return stripped if len(stripped) >= 3 else base


def classify(line):
    s = line.strip()
    if s.startswith(NOTE_MARK):                  # "※〇〇": a note for the telop of the line before it
        return "note", "", [], False, False
    directions = [m.group(0) for m in BRACKET_RE.finditer(s)]
    spoken = BRACKET_RE.sub("", s).strip()
    if LAUGH_RE.match(s):
        return "laugh", s, directions, False, False
    if directions and not spoken:
        return "direction", "", directions, False, False
    # "wwwいや暗いだろ" : laughter at the start/end of a speech line.
    lead = bool(re.match(r"^[wWｗＷ]{2,}", spoken))
    trail = bool(re.search(r"[wWｗＷ]{2,}$", spoken))
    spoken = EDGE_LAUGH_RE.sub("", spoken).strip()
    return "speech", spoken, directions, lead, trail


def split_note(raw):
    """"それはね※〇〇" -> ("それはね", "〇〇").  Inside brackets, "いや(テロップでは※嘘)" -> ("いや", "嘘").
    A line without ※ -> (line, None)."""
    s = raw.strip()
    if NOTE_MARK not in s:
        return s, None
    k = s.index(NOTE_MARK)
    opens = max(s.rfind(c, 0, k) for c in "（(【［[＜<")
    closes = [s.find(c, k) for c in "）)】］]＞>"]
    closes = [c for c in closes if c >= 0]
    if opens >= 0 and not [c for c in "）)】］]＞>" if c in s[opens:k]]:      # the ※ is inside a bracket
        end = min(closes) if closes else len(s)
        note = s[k + 1:end].strip()
        head = (s[:opens] + s[end + 1:]).strip()
    else:
        head, note = s[:k].strip(), s[k + 1:].strip()
    return head, note or None


_SENTENCE_RE = re.compile(r"[？！?!。、…「」『』（）()]|[wWｗＷ]{2,}|" + NOTE_MARK)
_ENDING_RE = re.compile(r"(です|ます|ません|でした|ました|だよ|だね|だな|だわ|かな|って|けど|から|[かよねわぞぜだのさ])$")


_TITLE_RE = re.compile(r"(てみた|でみた|してみた|やってみた)[!！?？】」』)）\s]*$|^[【\[［].*[】\]］]$")


def _drop_title_blocks(blocks):
    """The first lines of a script (up to 3) are usually a title or a memo ("相手の物を推理するゲーム").
    A one-line block there that does not read as spoken dialogue is not block 1: drop it."""
    out, used, leading = [], 0, True
    for lines in blocks:
        if leading and used < 3:
            used += len(lines)
            while len(lines) > 1 and _TITLE_RE.search(lines[0].strip()):
                lines = lines[1:]        # "〜してみた" written on the line right above the dialogue
            if len(lines) == 1:
                t = lines[0].strip()
                if t and (_TITLE_RE.search(t) or (not _SENTENCE_RE.search(t) and not _ENDING_RE.search(t))):
                    continue
        else:
            leading = False
        out.append(lines)
        leading = False if out else leading
    return out


def parse(text):
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks, cur = [], []
    for raw in text.split("\n"):
        if raw.strip() == "":
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(raw)
    if cur:
        blocks.append(cur)

    blocks = _drop_title_blocks(blocks)
    result = []
    for bi, lines in enumerate(blocks, 1):
        items = []
        for li, raw in enumerate(lines, 1):
            note = None
            if raw.strip().startswith(NOTE_MARK):
                note = raw.strip()[1:].strip()
                kind, spoken, dirs, lead, trail = "note", "", [], False, False
            else:
                head, note = split_note(raw)
                kind, spoken, dirs, lead, trail = classify(head)
            items.append({
                "note": note,
                "id": "b%02d-l%02d" % (bi, li),
                "raw": raw.strip(),
                "kind": kind,
                "text": spoken,
                "norm": norm_script(spoken) if kind == "speech" else "",
                "directions": dirs,
                "laugh_before": lead,
                "laugh_after": trail,
            })
        loose = []
        # a note on its own line belongs to the speech line before it (or after it, at the very top)
        for k, it in enumerate(items):
            if it["kind"] == "note" and it["note"]:
                prev = [x for x in items[:k] if x["kind"] == "speech"]
                nxt = [x for x in items[k + 1:] if x["kind"] == "speech"]
                tgt = prev[-1] if prev else (nxt[0] if nxt else None)
                if tgt is not None:
                    tgt.setdefault("notes", []).append(it["note"])
                else:
                    loose.append(it["note"])
        for it in items:
            if it["kind"] == "speech" and it.get("note"):
                it.setdefault("notes", []).append(it["note"])
        result.append({
            "id": "b%02d" % bi,
            "loose_notes": loose,
            "cue": next((i["raw"] for i in items if i["kind"] == "speech"), ""),
            "lines": items,
        })
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--json")
    args = ap.parse_args()
    with open(args.script, encoding="utf-8") as f:
        blocks = parse(f.read())
    for b in blocks:
        n_sp = sum(1 for i in b["lines"] if i["kind"] == "speech")
        n_lf = sum(1 for i in b["lines"] if i["kind"] == "laugh")
        n_dr = sum(1 for i in b["lines"] if i["kind"] == "direction")
        print("%s  speech %d / laugh %d / direction %d   %s" %
              (b["id"], n_sp, n_lf, n_dr, b["cue"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(blocks, f, ensure_ascii=False, indent=2)
        print("saved:", args.json)


if __name__ == "__main__":
    main()
