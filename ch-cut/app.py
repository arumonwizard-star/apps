#!/usr/bin/env python3
"""
CH Cut - the app window.

Starts a small web page on your own Mac (nothing goes over the internet) and opens it in the browser.
Pick the video, the separate audio and paste the script; the app syncs, transcribes (offline), finds
the takes, cuts the rough edit and writes the Final Cut Pro file.  Other takes of a block can be
chosen in the list and the project is saved.

Start:  python3 app.py        (or double-click  CH Cut を起動.command)
"""
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import time
import re
import webbrowser
import timetree
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECTS = Path.home() / "CHCut Projects"
PORT = int(os.environ.get("CHCUT_PORT", "8765"))
TOKEN = secrets.token_hex(8)
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")
DEFAULT_SPEAKERS = HERE / "presets" / "字幕テンプレ_CH.json"
LAST_SEEN = [time.time()]
WHISPER_MODEL = Path.home() / ".local/share/whisper-cpp/ggml-large-v3-turbo-q5_0.bin"

def engine_sig():
    """A short fingerprint of every program file: when any of them is replaced the version changes by itself."""
    import hashlib
    h = hashlib.md5()
    for f in sorted(HERE.glob("*.py")):
        try:
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:6]


# (the version changes with every program file)
ENGINE_SIG = engine_sig()
APP_VER = "2026.11.25-" + ENGINE_SIG
BOOT = str(time.time())
STATE = {"project": None, "job": {"state": "idle", "log": [], "label": "", "result": None}}
LOCK = threading.Lock()


# ------------------------------------------------------------------ project files
def safe_name(s):
    return "".join(ch if ch not in '/\\:*?"<>|' else "_" for ch in s).strip() or "project"


def new_project(video="", audio=""):
    stem = clean_label(Path(video).stem) if video else ""
    stem = stem or ("新規 " + time.strftime("%m%d-%H%M"))
    return {"name": stem, "auto_name": True, "video": video, "audio": audio, "script": "", "offset": None,
            "picks": {}, "alts_sel": {}, "skip_blocks": [], "subtitles": True, "swap_speakers": False, "diarize": True,
            "speakers_config": "", "alts": True, "out": "",
            "out_dir": str(Path.home() / "Desktop" / "テスト"), "voice_preset": True, "dual_mono": True, "kerning": "metrics"}


def _has_content(p):
    return bool(p.get("video") or p.get("audio") or (p.get("script") or "").strip())


def project_dir(p):
    """The project's folder.  It is only created once the project holds something (a file or a script):
    a blank "新規" project never leaves an empty folder behind."""
    d = PROJECTS / safe_name(p["name"])
    if _has_content(p):
        d.mkdir(parents=True, exist_ok=True)
    return d


_STOP = set("今日 今回 本当 絶対 最近 時間 自分 一番 以上 感じ 普通 全然 結構 大丈夫 確認 出来 気持ち 最初 最後 最高 完全 意味 理由 問題 今度 一緒 一回 シーン セリフ ポイント パターン ネタ ヤバ ヤバい マジ ゲーム 二回 一個 何個 何話 台本 動画 編集 テロップ ありがとう ちょっと マジで ほんと".split())


def clean_label(s):
    """A name made of real words only: no IMG_, no numbers-only parts, no symbols."""
    s = re.sub(r"(?i)\b(img|dsc|mov|mvi|pxl|gopr|vid|clip|audio|rec)[_\- ]*\d*", " ", s)
    s = re.sub(r"[_\-.・、，。,!?！？()（）\[\]【】「」『』\"'~〜ー＿]+", " ", s) if not re.search(r"[ァ-ヶ]", s) else \
        re.sub(r"[_\-.・、，。,!?！？()（）\[\]【】「」『』\"'~〜＿]+", " ", s)
    s = re.sub(r"\b\d+\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def make_title(script, transcript_text=""):
    """The most characteristic words of the script (names, titles, katakana words) as the project name."""
    lines = [l for l in (script or "").splitlines() if l.strip() and not l.strip().startswith(("※", "*", "＊"))]
    text = "\n".join(re.sub(r"[（(][^）)]*[）)]|[wｗ]{2,}|【[^】]*】", "", l) for l in lines)
    if len(text) < 8:
        text = text + "\n" + (transcript_text or "")
    toks = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}|[ァ-ヶー]{3,}|[\u4e00-\u9fff]{2,}", text)
    score = {}
    for t in toks:
        if t in _STOP or re.fullmatch(r"[ー]+", t):
            continue
        k = t.upper() if re.fullmatch(r"[A-Za-z0-9]+", t) else t
        bonus = 3.0 if re.fullmatch(r"[A-Za-z0-9]+", t) else (1.5 if re.search(r"[ァ-ヶ]", t) else (2.5 if len(t) >= 3 else 0.0))
        score[k] = score.get(k, 0.0) + len(t) + bonus
    ranked = sorted(score, key=lambda k: -score[k])
    pick = []
    for k in ranked:
        if any(k in q or q in k for q in pick):
            continue
        pick.append(k)
        if len(pick) == 2:
            break
    return clean_label("".join(pick))[:30]


def file_title(p):
    """The name the person gave the files (the audio or video file name) - it beats words picked from the script."""
    for k in ("audio", "video"):
        f = p.get(k) or ""
        if not f or Path(f).stem.startswith("merged_"):
            continue
        stem = re.sub(r"(?i)[_\- ]*(vrew|final|edit|edited|audio|voice|rec|record|録音|音声|整音|書き出し|export)\s*\d*$", "", Path(f).stem)
        t = clean_label(stem)
        if len(t) >= 2 and not re.fullmatch(r"(?i)(new|untitled|無題|新規)[ 0-9-]*", t):
            return t[:30]
    return ""


def rename_project(p, title):
    """Give an automatically named project its real name (the folder moves, files inside keep working)."""
    old = project_dir(p)
    new_name = safe_name(title)
    new = PROJECTS / new_name
    n = 2
    while new.exists() and new != old:
        new = PROJECTS / ("%s %d" % (new_name, n))
        n += 1
    if new == old:
        return
    shutil.move(str(old), str(new))
    for k in ("video", "audio"):
        if p.get(k, "").startswith(str(old) + os.sep):
            p[k] = str(new) + p[k][len(str(old)):]
    for k, label in (("video", "動画"), ("audio", "音声")):       # joined files get the project's name too
        f = Path(p.get(k, ""))
        if f.stem == "merged_" + k and f.exists():
            g = f.with_name("%s %s%s" % (new.name, label, f.suffix))
            f.rename(g)
            p[k] = str(g)
    p["name"] = new.name
    p["auto_name"] = False
    STATE["project"] = p
    save_project(p)


def scan_dir(name, hints):
    """A folder dropped on the page: find it on this Mac and sort its videos and audio files by recording time."""
    import unicodedata
    names = {unicodedata.normalize(f, name) for f in ("NFC", "NFD")}
    VEXT = {".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mts", ".m2ts", ".mkv"}
    AEXT = {".m4a", ".wav", ".aif", ".aiff", ".mp3", ".aac", ".flac", ".caf", ".wma"}
    home = Path.home()
    vols = ["/Volumes/" + v for v in sorted(os.listdir("/Volumes"))] if os.path.isdir("/Volumes") else []
    roots = vols + [str(home / d) for d in ("Desktop", "Downloads", "Movies", "Documents")]
    cands = []
    if platform.system() == "Darwin":
        for nm in names:
            try:
                r = subprocess.run(["mdfind", "-name", nm], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
                cands += [x for x in r.stdout.decode().splitlines() if os.path.isdir(x) and os.path.basename(x) in names]
            except Exception:   # noqa: BLE001
                pass

    def holds(dpath):
        try:
            have = set(os.listdir(dpath))
        except OSError:
            return False
        have |= {unicodedata.normalize("NFC", x) for x in have}
        return any(unicodedata.normalize("NFC", h) in have for h in hints) if hints else True

    cands = [c for c in cands if holds(c)]
    t0 = time.time()
    for root in roots:
        if cands or not os.path.isdir(root) or time.time() - t0 > 60:
            continue
        for nm in names:
            try:
                r = subprocess.run(["find", root, "-maxdepth", "5", "-type", "d", "-name", nm, "-not", "-path", "*/.Trash*"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
                cands += [x for x in r.stdout.decode().splitlines() if holds(x)]
            except Exception:   # noqa: BLE001
                pass
    if not cands:
        return {"error": "フォルダ「%s」の場所が見つかりませんでした。" % name}
    vids, auds = [], []
    for dp, dn, fn in os.walk(cands[0]):
        dn[:] = [d for d in dn if not d.startswith(".")]
        if dp[len(cands[0]):].count(os.sep) >= 2:
            dn[:] = []
        for f in fn:
            if f.startswith(".") or f.startswith("._"):
                continue
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(dp, f)
            if ext in VEXT:
                vids.append(full)
            elif ext in AEXT:
                auds.append(full)

    def nat(x):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", os.path.basename(x))]
    key = lambda x: (round(os.path.getmtime(x)), nat(x))      # noqa: E731
    return {"folder": cands[0], "videos": sorted(vids, key=key), "audios": sorted(auds, key=key)}


def _duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def align_audio(p, aparts, merged_video):
    """Audio recorded in several parts: every part is lined up with the merged video on its own (the recorder
    started at a different moment in each part), and the parts are joined into one audio file as long as the
    video, so one offset fits from start to end."""
    sys.path.insert(0, str(HERE))
    import sync_audio as sa
    d = project_dir(p)
    v = sa.load_mono(str(merged_video))
    total = len(v) / float(sa.SR)
    items = []
    for i, ap in enumerate(aparts):
        e = sa.load_mono(str(ap))
        lag, conf = sa.gcc_phat(v, e)
        if conf < 1.8:
            raise RuntimeError("音声「%s」を動画に合わせられませんでした（音が合わない／無音）" % Path(ap).name)
        items.append((lag / float(sa.SR), len(e) / float(sa.SR), ap))
    items.sort()
    cuts = [0.0]                                # where the video switches from one audio part to the next
    for (p0, l0, _a0), (p1, _l1, _a1) in zip(items, items[1:]):
        cuts.append(max((p0 + l0 + p1) / 2.0, cuts[-1] + 0.1))
    cuts.append(total)
    segs = []
    for i, (pos, _ln, ap) in enumerate(items):
        t0, t1 = cuts[i], cuts[i + 1]
        dur = t1 - t0
        if dur <= 0.05:
            continue
        seg = d / ("_seg%d.wav" % i)
        start_in_audio = t0 - pos               # < 0: this audio part starts later than the region does
        if start_in_audio < 0:
            flt = "adelay=%d:all=1,apad,atrim=0:%.3f" % (round(-start_in_audio * 1000), dur)
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(ap), "-af", flt, "-ar", "48000", str(seg)]
        else:
            flt = "apad,atrim=0:%.3f" % dur
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", "%.3f" % start_in_audio, "-i", str(ap), "-af", flt,
                   "-ar", "48000", str(seg)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("音声を合わせられませんでした: " + r.stderr[-300:])
        segs.append(seg)
    lst = d / "audio_aligned.txt"
    lst.write_text("".join("file '%s'\n" % str(x).replace("'", "'\\''") for x in segs), encoding="utf-8")
    out = d / "merged_audio.m4a"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "aac",
                        "-b:a", "256k", str(out)], capture_output=True, text=True)
    for x in segs:
        x.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError("音声をつなげられませんでした: " + r.stderr[-300:])
    return str(out)


def merge_parts(p, parts, kind):
    """Several files recorded one after another (video 1 + 2, audio 1 + 2) become one file in the project folder.
    The originals are not touched.  Video/audio are joined without re-encoding when the files match."""
    d = project_dir(p)
    ext = Path(parts[0]).suffix or (".mov" if kind == "video" else ".m4a")
    out = d / (("merged_video" if kind == "video" else "merged_audio") + ext)
    lst = d / (kind + "_parts.txt")
    lst.write_text("".join("file '%s'\n" % str(x).replace("'", "'\\''") for x in parts), encoding="utf-8")
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0 and kind == "audio":     # different audio settings: join them re-encoded
        out = d / "merged_audio.m4a"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "aac",
                            "-b:a", "256k", str(out)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("つなげられませんでした: " + r.stderr[-300:])
    return str(out)


def save_project(p):
    if not _has_content(p):
        return
    d = project_dir(p)
    with open(d / "project.json", "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)


def list_projects():
    out = []
    if PROJECTS.exists():
        def _last_used(x):
            t = 0.0
            for f in ("project.json", "telops_edited.json"):
                try:
                    t = max(t, (x / f).stat().st_mtime)
                except OSError:
                    pass
            return t
        for d in sorted((x for x in PROJECTS.iterdir() if x.is_dir()), key=_last_used, reverse=True):
            if (d / "project.json").exists():
                out.append(d.name)
    return out


def load_project(name):
    with open(PROJECTS / name / "project.json", encoding="utf-8") as f:
        p = json.load(f)
    return {**new_project(), **p}


# ------------------------------------------------------------------ checks
def doctor():
    items = []

    def add(name, ok, hint):
        items.append({"name": name, "ok": ok, "hint": "" if ok else hint})

    add("ffmpeg", shutil.which("ffmpeg") is not None, "ターミナルで  brew install ffmpeg")
    add("whisper-cli (文字起こし)", shutil.which("whisper-cli") is not None or Path("/opt/homebrew/bin/whisper-cli").exists(),
        "ターミナルで  brew install whisper-cpp")
    add("文字起こしのモデル", WHISPER_MODEL.exists(), "前に入れたモデルが見つかりません: %s" % WHISPER_MODEL)
    try:
        import numpy  # noqa: F401
        add("numpy", True, "")
    except Exception:
        add("numpy", False, "ターミナルで  python3 -m pip install numpy")
    return items


def locate_file(name, size):
    """A file dropped on the page: the browser gives only its name and size, so find the original
    on this Mac (Spotlight first, then a walk through the volumes and the home folder)."""
    import unicodedata
    hits = []
    names = {unicodedata.normalize(f, name) for f in ("NFC", "NFD")}
    def ok(pth):
        try:
            return os.path.isfile(pth) and (not size or os.path.getsize(pth) == size)
        except OSError:
            return False
    if platform.system() == "Darwin":
        try:
            for nm in names:
                r = subprocess.run(["mdfind", "-name", nm], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
                hits += [x for x in r.stdout.decode().splitlines() if os.path.basename(x) in names and ok(x)]
        except Exception:   # noqa: BLE001
            pass
    if not hits:
        home = Path.home()
        vols = ["/Volumes/" + v for v in sorted(os.listdir("/Volumes"))] if os.path.isdir("/Volumes") else []
        roots = vols + [str(home / d) for d in ("Desktop", "Downloads")]       # only these: every other folder asks for its own permission
        t0 = time.time()
        for root in roots:
            if not os.path.isdir(root) or time.time() - t0 > 90:
                continue
            for nm in names:
                try:
                    r = subprocess.run(["find", root, "-name", nm, "-type", "f", "-not", "-path", "*/.Trash*"],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45)
                    hits += [x for x in r.stdout.decode().splitlines() if ok(x)]
                except Exception:   # noqa: BLE001
                    pass
            if hits:
                break
    hits = [h for h in hits if "/.Trash" not in h]
    return {"path": hits[0]} if hits else {"error": "「%s」の場所が見つかりませんでした。クリックして選んでください。" % name}


def pick_file(kind):
    if platform.system() != "Darwin":
        return {"error": "このパソコンではファイル選択の窓が出せません。パスを入力してください。"}
    prompt = {"video": "動画ファイルを選んでください", "audio": "別録りの音声ファイルを選んでください",
              "script": "台本のテキストファイルを選んでください", "config": "話者設定(JSON)を選んでください",
              "folder": "書き出し先のフォルダを選んでください"}.get(kind, "ファイルを選んでください")
    what = "folder" if kind == "folder" else "file"
    r = subprocess.run(["osascript", "-e", 'POSIX path of (choose %s with prompt "%s")' % (what, prompt)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        return {"cancelled": True}
    path = r.stdout.decode().strip()
    if kind == "script":
        try:
            return {"path": path, "text": Path(path).read_text(encoding="utf-8")}
        except Exception as ex:   # noqa: BLE001
            return {"error": "読み込めませんでした: %s" % ex}
    return {"path": path}


# ------------------------------------------------------------------ jobs
def log(line):
    with LOCK:
        STATE["job"]["log"].append(line.rstrip("\n"))
        STATE["job"]["log"] = STATE["job"]["log"][-400:]


def run_cmd(cmd, label):
    with LOCK:
        STATE["job"]["label"] = label
    log("▶ " + label)
    proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    for line in proc.stdout:
        log(line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("%s で失敗しました（上のログを見てください）" % label)


def out_title(p):
    """One name for the export file and the Final Cut project: the word that stands for the video (the audio file's name)."""
    return file_title(p) or clean_label(p["name"]) or p["name"]


def unique_out(p):
    base = None
    for k_ in ("video", "audio"):       # the folder the materials came from
        v_ = p.get(k_)
        if v_ and Path(v_).expanduser().parent.is_dir() and os.access(str(Path(v_).expanduser().parent), os.W_OK):
            base = Path(v_).expanduser().parent
            break
    if base is None:
        base = Path(p.get("out_dir") or (Path.home() / "Desktop" / "テスト")).expanduser()
        base.mkdir(parents=True, exist_ok=True)
    stem = safe_name(out_title(p).replace(" ", ""))      # "おかちめんこ.fcpxml"
    cand = base / (stem + ".fcpxml")
    n = 2
    while cand.exists():
        cand = base / ("%s%d.fcpxml" % (stem, n))
        n += 1
    return cand


def script_from_transcript(transcript):
    """A script made of what Whisper heard: one line per phrase, a new block after a pause of 2 s or more."""
    data = json.loads(Path(transcript).read_text(encoding="utf-8"))
    blocks, cur, last_end = [], [], None
    for s in data.get("segments", []):
        t = re.sub(r"\s+", " ", str(s.get("text", ""))).strip()
        if not t or re.fullmatch(r"[wｗ笑!！、。…\s]+", t):
            continue
        if last_end is not None and s["start"] - last_end >= 2.0 and cur:
            blocks.append(cur)
            cur = []
        cur.append(t)
        last_end = s["end"]
    if cur:
        blocks.append(cur)
    return "\n\n".join("\n".join(b) for b in blocks) + "\n"


def learn_words(d, edited, script):
    """Words that were fixed by hand in the telops are remembered (words.py) and put in automatically next time."""
    try:
        import words
        gen = read_json(d / "telops.json", None) or {}
        for bid, blk in (edited or {}).items():
            old_p = (gen.get(bid) or {}).get("pieces") or []
            new_p = blk.get("pieces") or []
            if len(old_p) != len(new_p):
                continue
            for o, n in zip(old_p, new_p):
                if n.get("deleted") or o.get("text") == n.get("text"):
                    continue
                words.learn(o.get("text", ""), n.get("text", ""), script)
    except Exception:   # noqa: BLE001
        pass


def job_main(stage):
    p = STATE["project"]
    d = project_dir(p)
    try:
        transcript = d / "transcript.json"
        script = d / "script.txt"
        if p.get("script", "").strip():
            script.write_text(p["script"], encoding="utf-8")
        stale = False
        try:
            if transcript.exists():
                tj = json.loads(transcript.read_text(encoding="utf-8"))
                stale = tj.get("audio") != p["audio"] or transcript.stat().st_mtime < Path(p["audio"]).stat().st_mtime
        except Exception:   # noqa: BLE001
            stale = True
        if stage == "all" or stale or not transcript.exists():
            if not Path(p["audio"]).exists():
                raise RuntimeError("音声ファイルが見つかりません: %s" % p["audio"])
            run_cmd([sys.executable, "-u", "transcribe.py", p["audio"], "--normalize", "--out", str(transcript)],
                    "文字起こし（数分かかります）")
        if not p.get("script", "").strip():              # no script: write down what was said, as it was said
            log("台本がないので、聞こえた通りに字幕を起こします")
            script.write_text(script_from_transcript(transcript), encoding="utf-8")
        try:                                       # what was edited by hand is never lost to a new analysis: keep a copy
            te0_ = d / "telops_edited.json"
            if te0_.exists():
                bk_ = d / ".backup"
                bk_.mkdir(exist_ok=True)
                shutil.copy2(str(te0_), str(bk_ / ("telops_edited_%s.json" % time.strftime("%Y%m%d_%H%M%S"))))
                for old_ in sorted(bk_.glob("telops_edited_*.json"))[:-8]:
                    old_.unlink()
        except Exception:   # noqa: BLE001
            pass
        bid_ = stage[6:] if stage.startswith("block:") else ""
        try:                                   # hand edits are never lost: keep a copy before every new analysis
            te0_ = d / "telops_edited.json"
            if stage != "export" and te0_.exists():
                bk_ = d / "backups"
                bk_.mkdir(exist_ok=True)
                shutil.copy2(str(te0_), str(bk_ / ("telops_edited_%s.json" % time.strftime("%m%d_%H%M%S"))))
                for old_ in sorted(bk_.glob("telops_edited_*.json"))[:-12]:
                    old_.unlink()
        except Exception:   # noqa: BLE001
            pass
        if bid_:
            run_cmd([sys.executable, "-u", "reblock.py", str(transcript), str(script), str(d / "report.json"), stage[6:]],
                    "ブロック再解析")
            stage = "analyze"
        export = stage == "export"
        out = unique_out(p) if export else d / "preview.fcpxml"
        report = d / "report.json"
        onsets_f = d / "onsets.json"
        try:                                    # where the mountains of the waveform start: telop heads are put there
            onsets_f.write_text(json.dumps(wave_onsets(get_peaks(p)["peaks"])), encoding="utf-8")
        except Exception as ex:   # noqa: BLE001
            log("波形の頭を取得できませんでした: %s" % ex)
        cmd = [sys.executable, "-u", "make_rough_cut.py", p["video"], p["audio"], str(transcript), str(script),
               "--out", str(out), "--title", out_title(p), "--report-json", str(report), "--telops-out", str(d / "telops.json")]
        if onsets_f.exists():
            cmd += ["--onsets", str(onsets_f)]
        if (d / "telops_edited.json").exists():                # keep only what was changed by hand (older versions saved every block)
            try:
                _ed = read_json(d / "telops_edited.json", {}) or {}
                _kept = touched_blocks(_ed, read_json(d / "telops.json", {}) or {}, False)
                if not any(q.get("_u") for b_ in _kept.values() for q in b_.get("pieces", [])):
                    flag_user(_kept, read_json(d / "telops.json", {}) or {})          # older file: find what was changed by hand
                    (d / "telops_edited.json").write_text(json.dumps(_kept, ensure_ascii=False, indent=1), encoding="utf-8")
                if len(_kept) != len(_ed):
                    (d / "telops_edited.json").write_text(json.dumps(_kept, ensure_ascii=False, indent=1), encoding="utf-8")
                if not _kept:
                    (d / "telops_edited.json").unlink(missing_ok=True)
            except Exception as ex:   # noqa: BLE001
                log("手直しの整理に失敗: %s" % ex)
        if stage == "analyze" and (d / "telops_edited.json").exists() and \
                read_json(d / "telops_edited.json", None) == read_json(d / "telops.json", None):
            (d / "telops_edited.json").unlink()         # never changed by hand: a fresh analysis starts from scratch
        if p.get("subtitles") and (d / "telops_edited.json").exists():
            cmd += ["--telops-in", str(d / "telops_edited.json"), "--edited-out", str(d / "telops_edited.json")]
        if p.get("subtitles") and (d / "extra_telops.json").exists():
            cmd += ["--extra-telops", str(d / "extra_telops.json")]
        if p.get("offset") is not None:
            cmd += ["--offset", str(p["offset"])]
        for bid, no in (p.get("picks") or {}).items():
            cmd += ["--pick", "%s=%s" % (bid, no)]
        if p.get("subtitles"):
            cmd += ["--subtitles", "--kerning", {"オプティカル": "optical", "和文等幅": "mono"}.get(p.get("kerning"), "metrics")]
            cfg = p.get("speakers_config") or (str(DEFAULT_SPEAKERS) if DEFAULT_SPEAKERS.exists() else "")
            if cfg:
                cmd += ["--speakers-config", cfg]
            if p.get("swap_speakers"):
                cmd += ["--swap-speakers"]
            if not p.get("diarize", True):
                cmd += ["--no-diarize"]
        if not p.get("alts", True):
            cmd += ["--no-alts"]
        if p.get("skip_blocks"):
            cmd += ["--skip-blocks", ",".join(p["skip_blocks"])]
        for bid, nos in (p.get("alts_sel") or {}).items():
            cmd += ["--alts-only", "%s=%s" % (bid, ",".join(str(n) for n in nos))]
        if not p.get("voice_preset", True):
            cmd += ["--no-audio-preset"]
        if not p.get("dual_mono", True):
            cmd += ["--no-dual-mono"]
        run_cmd(cmd, "同期・テイク選び・カット")
        if not bid_ and report.exists():       # every block has a good take: where none is found, listen to that block again
            try:
                weak = [b["id"] for b in json.loads(report.read_text(encoding="utf-8")).get("blocks", [])
                        if b.get("status") == "NG-only" and b["id"] not in (p.get("picks") or {})]
                tj0 = json.loads(transcript.read_text(encoding="utf-8"))
                weak = [w for w in weak if w not in tj0.get("reblocks", [])]
                for w in weak:
                    try:
                        run_cmd([sys.executable, "-u", "reblock.py", str(transcript), str(script), str(report), w], "ブロック再解析 " + w)
                    except Exception as ex:   # noqa: BLE001
                        log("再解析できませんでした: %s" % ex)
                if weak:
                    run_cmd(cmd, "同期・テイク選び・カット")
            except Exception as ex:   # noqa: BLE001
                log("再解析を省略: %s" % ex)
        if report.exists():
            rep = json.loads(report.read_text(encoding="utf-8"))
            p["offset"] = rep.get("offset", p.get("offset"))
        if export:
            p["out"] = str(out)
        else:
            p["analyzed_ver"] = APP_VER
        save_project(p)
        with LOCK:
            STATE["job"].update({"state": "done", "result": str(out) if export else None,
                                 "label": "完了" if export else "解析が終わりました", "tick": time.time()})
        log("書き出し完了: %s" % out if export else "解析が終わりました。テロップを見ながら直して、できたら「書き出し」を押してください")
        if export:
            threading.Thread(target=fcp_import, args=(str(out),), daemon=True).start()
    except Exception as ex:   # noqa: BLE001
        log("%s" % ex)
        with LOCK:
            STATE["job"].update({"state": "error", "label": "失敗"})


FCP_LIB = "外出CH"
FCP_LIB_PATHS = ["/Volumes/編集用/外出CH.fcpbundle", "~/Movies/外出CH.fcpbundle"]


def fcp_import(path, library=FCP_LIB):
    """Open the exported file in Final Cut Pro so that it appears in the library 外出CH as a project without asking:
    the library is opened first (an import goes into the open library); if Final Cut still asks which library, the
    row of 外出CH is chosen and 選択 is pressed."""
    if platform.system() != "Darwin":
        return
    try:
        lib = next((str(Path(x).expanduser()) for x in FCP_LIB_PATHS if Path(x).expanduser().exists()), None)
        if lib:
            subprocess.run(["open", "-a", "Final Cut Pro", lib], timeout=30)
            time.sleep(6)
        subprocess.run(["open", "-a", "Final Cut Pro", str(path)], timeout=30)
        scr = r'''
on run argv
  set libName to item 1 of argv
  set done to false
  repeat 40 times
    delay 0.5
    try
      tell application "System Events"
        tell process "Final Cut Pro"
          if exists (window "ライブラリを開く") then
            set w to window "ライブラリを開く"
            set rowsL to rows of table 1 of scroll area 1 of w
            repeat with r in rowsL
              try
                if (value of text field 1 of r) is libName then
                  select r
                  exit repeat
                end if
              end try
            end repeat
            delay 0.3
            click button "選択" of w
            set done to true
          end if
        end tell
      end tell
    end try
    if done then exit repeat
  end repeat
  return done as text
end run
'''
        r = subprocess.run(["osascript", "-e", scr, library], capture_output=True, text=True, timeout=60)
        print("[書き出し] Final Cut への読み込み: dialog=%s %s" % ((r.stdout or "").strip(), (r.stderr or "").strip()[:200]), flush=True)
    except Exception as ex:   # noqa: BLE001
        print("[書き出し] Final Cut への読み込みはできませんでした: %s" % ex, flush=True)


def wave_onsets(pk):
    """Same detector as the editor's (tlOnsets): where a mountain of the waveform starts, in seconds."""
    import math
    n = len(pk)
    db = [20 * math.log10(max(v * v, 1e-7)) for v in pk]
    top = sorted(db)[int(len(db) * 0.99)] if db else 0.0
    sil, lo = top + 4.4 - 42, top + 4.4 - 40          # same levels as when the loudest voice sat at -4.4 dB
    o = []
    k = 8
    while k < n - 3:
        cur = max(db[k], db[k + 1], db[k + 2])
        if cur >= lo and cur - min(db[k - 8:k]) >= 10 and db[k - 1] < cur - 6:
            o.append(k / 100.0)
            k += 8
        k += 1
    q = 0
    for k in range(6, n):
        if db[k] > sil:
            if q >= 6:
                o.append(k / 100.0)
            q = 0
        else:
            q += 1
    o.sort()
    u = []
    for v in o:
        if not u or v - u[-1] > 0.012:
            u.append(v)
    return u


def learn_wave(d, p, new):
    """Remember where the user puts a telop start relative to the waveform (kept in presets/user_wave.json)."""
    try:
        old = read_json(d / "telops_edited.json", {}) or {}
        if not old:
            return
        on = wave_onsets(get_peaks(p)["peaks"])
        if not on:
            return
        f = HERE / "presets" / "user_wave.json"
        hist = read_json(f, {"d": []}) or {"d": []}
        add = []
        for bid, b in (new or {}).items():
            ob = {}
            for q in (old.get(bid) or {}).get("pieces", []):
                ob.setdefault(q.get("text"), q.get("start"))
            for q in b.get("pieces", []):
                st0 = ob.get(q.get("text"))
                if st0 is None or q.get("speaker", 0) < 0 or q.get("ov") or not q.get("m"):
                    continue
                if abs(q["start"] - st0) < 0.005:
                    continue
                near = [x for x in on if abs(x - q["start"]) <= 0.25]
                if near:
                    x = min(near, key=lambda v: abs(v - q["start"]))
                    add.append(round(q["start"] - x, 3))
        if add:
            hist["d"] = (hist.get("d", []) + add)[-150:]
            f.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    except Exception as ex:   # noqa: BLE001
        log("learn_wave: %s" % ex)


def wave_bias():
    h = (read_json(HERE / "presets" / "user_wave.json", {}) or {}).get("d", [])
    if len(h) < 5:
        return 0.0
    h = sorted(h)
    m = h[len(h) // 2]
    return round(max(-0.08, min(0.05, m)), 3)


FX_CHAIN = ("highpass=f=78.5:p=2,highpass=f=78.5:p=2,highpass=f=78.5:p=2,highpass=f=78.5:p=2,"
            "treble=g=8.2:f=8050:t=q:w=0.63,"
            "agate=threshold=0.00126:range=0.063:ratio=2:attack=5:release=120,"
            "acompressor=threshold=0.0316:ratio=8:attack=4:release=200:makeup=1,"
            "acompressor=threshold=0.0316:ratio=12:attack=4:release=200:makeup=1,"
            "alimiter=limit=0.316:attack=2:release=50:level=0")


def get_peaks(p, raw=False):
    """Loudness of the audio every 10 ms (0..1) for the editor's waveform.  It follows what Final Cut shows: channel B
    (dual mono) with the export's gain, channel A where the export switches to A (clipping), and the voice preset
    (CH整音デフォ: low cut, high shelf, noise gate, two compressors, limiter) approximated with ffmpeg."""
    d = project_dir(p)
    f = d / ("peaks_raw.json" if raw else "peaks5.json")
    src = Path(p["audio"])
    if f.exists() and (not MEDIA.get("audio", True) or f.stat().st_mtime >= src.stat().st_mtime):
        return json.loads(f.read_text(encoding="utf-8"))
    import numpy as np
    SR = 24000
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    x = np.frombuffer(r.stdout, dtype=np.float32)
    x = x[: len(x) // 2 * 2].reshape(-1, 2)
    n = len(x) // (SR // 100)
    gain, spans = 1.0, []
    try:
        sys.path.insert(0, str(HERE))
        import make_rough_cut as mrc
        gain = 10 ** (mrc.mono_gain_db(src) / 20.0)
        if n < 60 * 100 * 40:
            spans = mrc.clipped_spans(src, 0, n / 100.0)
    except Exception as ex:   # noqa: BLE001
        log("peaks gain: %s" % ex)
    m = np.clip(np.where(np.abs(x[:, 0]) > np.abs(x[:, 1]), x[:, 0], x[:, 1]) * gain, -1.0, 1.0)   # both voices (チ and ホ)
    try:
        if raw:
            raise RuntimeError("raw")     # the timeline waveform is the recording itself (as Final Cut draws it): every audible sound
        q = subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-", "-af", FX_CHAIN,
                            "-f", "f32le", "-"], input=m.astype(np.float32).tobytes(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        y = np.frombuffer(q.stdout, dtype=np.float32)
        if len(y) < len(m) * 0.9:
            raise RuntimeError(q.stderr.decode("utf-8", "replace")[:200])
        m = y
    except Exception as ex:   # noqa: BLE001
        if not raw:
            log("peaks fx: %s" % ex)
    hop = SR // 100
    n = len(m) // hop
    b = np.abs(m[: n * hop]).reshape(n, hop).max(axis=1)
    out = {"rate": 100, "peaks": [round(float(v) ** 0.5, 3) for v in b]}
    f.write_text(json.dumps(out), encoding="utf-8")
    return out


def ensure_proxy(p):
    """A small H.264 copy of the video for the editor (the original may not play in a browser)."""
    d = project_dir(p)
    px = d / "proxy.mp4"
    try:
        if px.exists() and px.with_suffix(".v2").exists() and px.stat().st_mtime >= Path(p["video"]).stat().st_mtime:
            STATE["proxy"] = "ready"
            return
        if STATE.get("proxy") == "making":
            return
        STATE["proxy"] = "making"
        tmp = d / "proxy_tmp.mp4"
        def work():
            base = ["ffmpeg", "-y", "-v", "error"]
            tail = ["-vf", "scale=-2:360", "-an", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp)]
            r = None
            if sys.platform == "darwin":          # the Mac's video chip: many times faster than the CPU
                r = subprocess.run(base + ["-hwaccel", "videotoolbox", "-i", p["video"]] + tail[:2] +
                                   ["-an", "-c:v", "h264_videotoolbox", "-b:v", "1500k", "-g", "4"] + tail[3:],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r is None or r.returncode != 0:
                r = subprocess.run(base + ["-i", p["video"]] + tail[:2] + ["-an", "-c:v", "libx264", "-preset", "ultrafast",
                                   "-crf", "34", "-g", "4"] + tail[3:], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r.returncode == 0:
                tmp.replace(px)
                px.with_suffix(".v2").write_text("keyframe every 4 frames")
                STATE["proxy"] = "ready"
            else:
                STATE["proxy"] = "error"
        threading.Thread(target=work, daemon=True).start()
    except Exception:   # noqa: BLE001
        STATE["proxy"] = "error"


def ensure_av(p):
    """proxy.mp4 + the project's audio in ONE file (audio shifted by the sync offset), so the preview plays
    with a single clock and cannot drift or glitch."""
    try:
        d = project_dir(p)
        px, av, meta = d / "proxy.mp4", d / "proxy_av.mp4", d / "proxy_av.json"
        off = (get_report() or {}).get("offset")
        if off is None or not px.exists() or not p.get("audio") or not Path(p["audio"]).exists():
            if STATE.get("av") != "making":
                STATE["av"] = ""
            return
        off = round(float(off), 4)
        if av.exists() and meta.exists() and read_json(meta, {}).get("offset") == off \
                and av.stat().st_mtime >= px.stat().st_mtime:
            STATE["av"] = "ready"
            return
        if STATE.get("av") == "making":
            return
        STATE["av"] = "making"
        tmp = d / "proxy_av_tmp.mp4"

        def work():
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(px)]
            cmd += (["-ss", str(-off)] if off < 0 else (["-itsoffset", str(off)] if off > 0 else [])) + ["-i", p["audio"]]
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", str(tmp)]
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r.returncode == 0:
                tmp.replace(av)
                meta.write_text(json.dumps({"offset": off}))
                STATE["av"] = "ready"
            else:
                STATE["av"] = "error"
        threading.Thread(target=work, daemon=True).start()
    except Exception:   # noqa: BLE001
        STATE["av"] = "error"


def _find_media(p):
    """Before a job: a missing video / audio is looked for by name on this Mac (outside the lock: it can take a while)."""
    for k, label in (("video", "動画"), ("audio", "音声")):
        if p.get(k) and _probe(p[k], 2.0):
            continue
        alt = None
        if p.get(k):
            try:
                alt = _relocate(p[k]) or (locate_file(Path(p[k]).name, 0) or {}).get("path")
            except Exception:   # noqa: BLE001
                alt = None
        if alt and Path(alt).exists():
            p[k] = alt
            save_project(p)
            MEDIA[k] = True
            MEDIA["gen"] += 1
        else:
            return "%sが見つかりません（%s）。外付けをつなぐか、左の枠に入れ直してください" % (label, Path(p[k]).name if p.get(k) else "未設定")
    return None


def start_job(stage):
    if STATE.get("project"):
        err = _find_media(STATE["project"])
        if err:
            return {"error": err}
    try:
        import learn_edits
        learn_edits.update(PROJECTS)
        learn_edits.update_cuts(PROJECTS)
        try:
            import learn_voice
            threading.Thread(target=lambda: learn_voice.update(PROJECTS), daemon=True).start()
        except Exception:   # noqa: BLE001
            pass
    except Exception:   # noqa: BLE001
        pass
    with LOCK:
        if STATE["job"]["state"] == "running":
            return {"error": "実行中です"}
        if not STATE["project"]:
            return {"error": "プロジェクトがありません"}
        p = STATE["project"]
        if p.get("auto_name") and p.get("script", "").strip():
            try:
                title = file_title(p) or make_title(p["script"])
                if title:
                    rename_project(p, title)
            except Exception:   # noqa: BLE001
                pass
        STATE["job"] = {"state": "running", "log": [], "label": "準備中", "result": None}
    threading.Thread(target=job_main, args=(stage,), daemon=True).start()
    return {"ok": True}


def speaker_names(p):
    """Names of the telop templates for the editor: [チ, ホ, 笑]."""
    try:
        sys.path.insert(0, str(HERE))
        import subtitles as sb
        cfg = p.get("speakers_config") or (str(DEFAULT_SPEAKERS) if DEFAULT_SPEAKERS.exists() else "")
        cfgs = sb.load_speakers_config(cfg or None)
        names = [c["name"].replace("【CH】", "") for c in cfgs if not c.get("laugh") and not c.get("note")]
        return names + ["笑"]
    except Exception:   # noqa: BLE001
        return ["話者1", "話者2", "笑"]


def telop_styles(p):
    """What the telop templates look like, for the preview in the editor."""
    out = {"speakers": [], "laugh": None, "note": None}
    try:
        sys.path.insert(0, str(HERE))
        import subtitles as sb
        cfg = p.get("speakers_config") or (str(DEFAULT_SPEAKERS) if DEFAULT_SPEAKERS.exists() else "")
        for c in sb.load_speakers_config(cfg or None):
            st = c.get("style") or {}
            pos = next((x["value"] for x in c.get("params", []) if x.get("name") == "位置"), None)
            d = {"size": float(st.get("fontSize", c.get("size", 100))), "color": st.get("fontColor", c.get("color", "1 1 1 1")),
                 "font": st.get("font", ""), "pos": pos}
            if c.get("note"):
                out["note"] = d
            elif c.get("laugh"):
                out["laugh"] = d
            else:
                out["speakers"].append(d)
    except Exception:   # noqa: BLE001
        pass
    return out


def frame_size(p):
    key = p.get("video")
    if STATE.get("_frame", (None,))[0] == key:
        return STATE["_frame"][1]
    wh = [1920, 1080]
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height:stream_side_data=rotation:stream_tags=rotate", "-of", "json", p["video"]],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        s = json.loads(r.stdout)["streams"][0]
        wh = [int(s["width"]), int(s["height"])]
        rot = 0
        for sd in s.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        rot = rot or int(float((s.get("tags") or {}).get("rotate", 0) or 0))
        if abs(rot) in (90, 270):
            wh = wh[::-1]
    except Exception:   # noqa: BLE001
        pass
    STATE["_frame"] = (key, wh)
    return wh


def _block_sig(b, ends):
    ps = [(round(q.get("start", 0), 2), q.get("text", ""), q.get("speaker"), bool(q.get("deleted")), bool(q.get("ov")), q.get("src", "s"))
          for q in sorted(b.get("pieces", []), key=lambda q: (q.get("start", 0), q.get("text", "")))]
    nt = [(round(n.get("start", 0), 2), n.get("text", ""), bool(n.get("deleted"))) for n in b.get("notes", [])]
    return (ps, nt, (round(b.get("a0", 0), 2), round(b.get("a1", 0), 2), b.get("c0"), b.get("c1")) if ends else None)


def flag_user(edited, auto):
    """Mark every piece the person really changed (flag _u: n added, t time, x text / line break, s speaker, d deleted) against
    the automatic result, so that a new analysis keeps exactly those and replaces everything else."""
    for k, b in (edited or {}).items():
        ap = ((auto or {}).get(k) or {}).get("pieces", [])
        used = set()
        for p in b.get("pieces", []):
            p.pop("_u", None)
            best, bo = None, 0.5
            for i, q in enumerate(ap):
                if i in used:
                    continue
                o = min(q["end"], p["end"]) - max(q["start"], p["start"])
                o /= max(min(q["end"] - q["start"], p["end"] - p["start"]), 0.05)
                if o > bo:
                    best, bo = i, o
            fl = ""
            if best is None:
                fl = "n"
            else:
                used.add(best)
                q = ap[best]
                if abs(q["start"] - p["start"]) > 0.02 or abs(q["end"] - p["end"]) > 0.02:
                    fl += "t"
                if q.get("text") != p.get("text"):
                    fl += "x"
                if q.get("speaker") != p.get("speaker"):
                    fl += "s"
            if p.get("deleted"):
                fl += "d"
            if fl:
                p["_u"] = fl
    return edited


def touched_blocks(edited, auto, ends):
    """Only the blocks that were really changed by hand.  A block that still equals the automatic result is not kept, so the
    next analysis (a better program) replaces it instead of a stale copy of the old result."""
    out = {}
    for k, b in (edited or {}).items():
        a_ = (auto or {}).get(k)
        if a_ is None or _block_sig(b, ends) != _block_sig(a_, ends):
            out[k] = b
    return out


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:   # noqa: BLE001
        return default


def get_report():
    p = STATE["project"]
    if not p:
        return None
    rp = project_dir(p) / "report.json"
    if rp.exists():
        return json.loads(rp.read_text(encoding="utf-8"))
    return None


# ------------------------------------------------------------------ web
def mmss(sec):
    t = int(round(sec * 10))
    return "%d:%02d.%d" % (t // 600, (t // 10) % 60, t % 10)

_FONT = {}


def find_ch_font():
    """The telop font (ラノベPOP / LightNovelPopV2) installed on this Mac, so the browser can draw the same letters."""
    if "p" in _FONT:
        return _FONT["p"]
    found = None
    keys = ("lightnovelpop", "ラノベpop", "ラノベポップ")
    dirs = [Path.home() / "Library/Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts"), Path("/Library/Application Support/Apple/Fonts")]
    for d in dirs:
        try:
            for f in d.rglob("*"):
                nm = f.name.lower().replace(" ", "").replace("-", "").replace("_", "")
                if f.suffix.lower() in (".ttf", ".otf", ".ttc") and any(k in nm for k in keys):
                    found = f
                    break
        except Exception:   # noqa: BLE001
            pass
        if found:
            break
    if not found and platform.system() == "Darwin":
        for q in ("LightNovelPop", "ラノベPOP", "ラノベ"):
            try:
                r = subprocess.run(["mdfind", "-name", q], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
                for ln in r.stdout.decode("utf-8", "ignore").splitlines():
                    if ln.lower().endswith((".ttf", ".otf", ".ttc")):
                        found = Path(ln)
                        break
            except Exception:   # noqa: BLE001
                pass
            if found:
                break
    _FONT["p"] = found
    return found


MEDIA = {"audio": True, "video": True, "gen": 0}


def _probe(path, timeout=1.5):
    """Does the file exist?  A pulled-out disk can make a stat() hang, so ask from a thread and stop waiting."""
    res = []

    def run():
        try:
            res.append(os.path.exists(path))
        except Exception:   # noqa: BLE001
            res.append(False)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    return bool(res and res[0])


def _relocate(path):
    """The same disk can come back under another name ("編集用 1"): look for the same file on any mounted volume."""
    try:
        parts = Path(path).parts
        if len(parts) > 3 and parts[1] == "Volumes":
            rest = Path(*parts[3:])
            for v in sorted(Path("/Volumes").iterdir()):
                cand = v / rest
                if _probe(str(cand), 1.0):
                    return str(cand)
    except Exception:   # noqa: BLE001
        pass
    return None


def local_audio(p):
    """A copy of the audio kept inside the project folder, so editing goes on while the disk is out."""
    try:
        d = project_dir(p)
        src = Path(p["audio"])
        dst = d / ("audio_local" + src.suffix.lower())
        if MEDIA.get("audio") and src.exists() and (not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime
                                                    or dst.stat().st_size != src.stat().st_size):
            tmp = dst.with_suffix(dst.suffix + ".part")
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        return dst if dst.exists() else None
    except Exception:   # noqa: BLE001
        return None


def media_watch():
    """Every 1.5 s: are the audio and video reachable?  When a disk comes back, say so (gen changes) so the page
    reloads them by itself; when its name changed, point the project at the new place."""
    while True:
        time.sleep(1.5)
        try:
            p = STATE.get("project")
            if not p:
                continue
            changed = False
            for k in ("audio", "video"):
                if not p.get(k):
                    continue
                ok = _probe(p[k])
                if not ok:
                    alt = _relocate(p[k])
                    if alt:
                        p[k] = alt
                        save_project(p)
                        ok = True
                        STATE["proxy"] = ""
                if ok != MEDIA.get(k, True):
                    changed = True
                MEDIA[k] = ok
            if changed:
                MEDIA["gen"] += 1
            if MEDIA.get("audio") and p.get("audio"):
                local_audio(p)
        except Exception:   # noqa: BLE001
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.replace("__TOKEN__", TOKEN).replace("__VER__", APP_VER), "text/html; charset=utf-8")
        elif self.path.startswith("/font/ch"):
            f = find_ch_font()
            if not f:
                return self._send(404, "no font", "text/plain")
            try:
                data = f.read_bytes()
            except Exception:   # noqa: BLE001
                return self._send(404, "no font", "text/plain")
            return self._send(200, data, "font/ttf" if f.suffix.lower() != ".otf" else "font/otf")
        elif self.path == "/api/state":
            LAST_SEEN[0] = time.time()
            p_ = STATE["project"]
            if p_ and p_.get("video") and MEDIA.get("video", True) and Path(p_["video"]).exists():
                try:
                    if (project_dir(p_) / "proxy.mp4").exists() and (project_dir(p_) / "proxy.v2").exists() \
                            and STATE.get("proxy") != "making":
                        STATE["proxy"] = "ready"
                    elif STATE.get("proxy") not in ("making", "error"):
                        ensure_proxy(p_)
                    if STATE.get("proxy") == "ready":
                        ensure_av(p_)
                except Exception:   # noqa: BLE001
                    pass
            with LOCK:
                job = dict(STATE["job"])
                job["log"] = job["log"][-200:]
                self._json({"project": STATE["project"], "job": job, "report": get_report(),
                            "recent": list_projects(), "boot": BOOT, "app_ver": APP_VER, "doctor": doctor(),
                            "proxy": STATE.get("proxy", ""), "av": STATE.get("av", ""), "media": dict(MEDIA)})
        elif self.path == "/api/words":
            import words as _w
            return self._json({"groups": _w.groups()})
        elif self.path == "/api/wavebias":
            return self._json({"bias": wave_bias()})
        elif self.path.split("?")[0] == "/api/peaks":
            p = STATE["project"]
            if not p or not p.get("audio") or (not MEDIA.get("audio", True) and not (project_dir(p) / ("peaks_raw.json" if "raw=1" in self.path else "peaks5.json")).exists()):
                return self._json({"peaks": []})
            return self._json(get_peaks(p, raw="raw=1" in self.path))
        elif self.path == "/api/breaktest":
            return self._json(breaktest_samples())
        elif self.path == "/api/telops":
            p = STATE["project"]
            if not p:
                return self._json({"auto": None})
            d = project_dir(p)
            _tj = d / "telops.json"
            return self._json({"auto": read_json(d / "telops.json", None), "edited": read_json(d / "telops_edited.json", {}),
                               "gen": str(_tj.stat().st_mtime_ns) if _tj.exists() else "",
                               "names": speaker_names(p), "styles": telop_styles(p), "frame": frame_size(p),
                               "offset": (get_report() or {}).get("offset", 0),
                               "heard": [[s_.get("start"), s_.get("end"), s_.get("text", "")] for s_ in
                                         ((read_json(d / "transcript.json", {}) or {}).get("segments") or [])]})
        elif self.path.startswith("/media/audio2"):
            self._serve_audio("audio2")
        elif self.path.startswith("/media/audio"):
            self._serve_audio()
        elif self.path.startswith("/media/av"):
            self._serve_audio("av")
        elif self.path.startswith("/media/video"):
            self._serve_audio("video")
        elif self.path.startswith("/media/orig"):
            self._serve_audio("orig")
        elif self.path.startswith("/media/audio2"):
            self._serve_audio("audio2")
        else:
            self._send(404, "not found", "text/plain")

    def _serve_audio(self, which="audio"):
        p = STATE["project"]
        if which == "audio2":
            f = (project_dir(p) / "preview_audio.m4a") if p else None
        elif which == "orig":
            f = Path(p["video"]) if p and p.get("video") else None
        elif which == "video":
            f = (project_dir(p) / "proxy.mp4") if p else None
        elif which == "av":
            f = (project_dir(p) / "proxy_av.mp4") if p else None
        else:
            f = Path(p["audio"]) if p and p.get("audio") else None
            if p and p.get("audio") and not MEDIA.get("audio", True):
                f = local_audio(p) or f
        if not f or not f.exists():
            return self._send(404, "no audio", "text/plain")
        size = f.stat().st_size
        ctype = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".wav": "audio/wav", ".mp3": "audio/mpeg",
                 ".aif": "audio/aiff", ".aiff": "audio/aiff", ".aifc": "audio/aiff"}.get(f.suffix.lower(), "application/octet-stream")
        if which in ("video", "av"):
            ctype = "video/mp4"
        elif which == "orig":
            ctype = "video/quicktime" if f.suffix.lower() == ".mov" else "video/mp4"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng and rng.startswith("bytes="):
            a_, _, b_ = rng[6:].partition("-")
            start = int(a_) if a_ else 0
            end = int(b_) if b_ else size - 1
            end = min(end, size - 1)
            code = 206
        n = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(n))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                left = n
                while left > 0:
                    chunk = fh.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.headers.get("X-Token") != TOKEN:
            return self._send(403, "forbidden", "text/plain")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:   # noqa: BLE001
            body = {}
        path = self.path
        if path == "/api/tt/start":
            timetree.start(WEBVIEW, 7)
            return self._json(timetree.status())
        if path == "/api/tt/stop":
            timetree.stop()
            return self._json({"ok": True})
        if path == "/api/tt/status":
            return self._json(timetree.status())
        if path == "/api/locate":
            return self._json(locate_file(body.get("name", ""), int(body.get("size") or 0)))
        if path == "/api/scan_dir":
            return self._json(scan_dir(body.get("name", ""), body.get("hints") or []))
        if path == "/api/pick":
            return self._json(pick_file(body.get("kind")))
        if path == "/api/new":
            STATE["project"] = new_project()
            return self._json({"ok": True})
        if path == "/api/open_project":
            try:
                STATE["project"] = load_project(body["name"])
                try:
                    os.utime(PROJECTS / body["name"] / "project.json")       # the one opened last is the one that opens next time
                except OSError:
                    pass
                return self._json({"ok": True})
            except Exception as ex:   # noqa: BLE001
                return self._json({"error": str(ex)})
        if path == "/api/project":
            with LOCK:
                p = STATE["project"] or new_project()
                if body.pop("fresh", False) and p.get("video"):
                    old_ = p
                    p = new_project()                 # a new set of files dropped: a new project, not a change to the old one
                    try:
                        if old_.get("script", "").strip() and not (project_dir(old_) / "report.json").exists():
                            p["script"] = old_["script"]      # the old one was never analyzed: the same script is meant (files dropped again)
                    except Exception:   # noqa: BLE001
                        pass
                parts_in = {k: body.pop(k + "_parts", None) for k in ("video", "audio")}
                first = parts_in["video"] or parts_in["audio"]
                if first and (p.get("auto_name", True) or not p.get("name")):
                    p["name"] = clean_label(Path(first[0]).stem) or ("新規 " + time.strftime("%m%d-%H%M"))
                    p["auto_name"] = True
                try:
                    for kind, parts in parts_in.items():
                        if kind == "audio" and parts and len(parts) > 1 and (body.get("video") or p.get("video")):
                            body["audio"] = align_audio(p, parts, body.get("video") or p["video"])       # each audio part lined up with the video
                            p["audio_parts"] = parts
                        elif parts and len(parts) > 1:
                            body[kind] = merge_parts(p, parts, kind)
                            p[kind + "_parts"] = parts
                        elif parts:
                            body[kind] = parts[0]
                            p.pop(kind + "_parts", None)
                except Exception as ex:   # noqa: BLE001
                    return self._json({"error": str(ex)})
                if "name" in body and body["name"] != p.get("name"):
                    p["auto_name"] = False
                for kind in ("video", "audio"):
                    if kind in body and not parts_in[kind]:
                        p.pop(kind + "_parts", None)          # one file chosen by hand replaces a joined set
                for k, v in body.items():
                    if k in p:
                        p[k] = v
                if body.get("video") and not p.get("name"):
                    p["name"] = clean_label(Path(body["video"]).stem) or ("新規 " + time.strftime("%m%d-%H%M"))
                    p["auto_name"] = True
                if "video" in body or "audio" in body:
                    p["offset"] = None
                STATE["project"] = p
                if p.get("name"):
                    save_project(p)
            return self._json({"ok": True, "project": p})
        if path == "/api/need_audio":
            p_ = STATE["project"]
            if not p_ or not p_.get("audio") or not Path(p_["audio"]).exists():
                return self._json({"error": "音声ファイルが見つかりません"})
            out_ = project_dir(p_) / "preview_audio.m4a"
            if not out_.exists() or out_.stat().st_mtime < Path(p_["audio"]).stat().st_mtime:
                r_ = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", p_["audio"], "-vn", "-ac", "2", "-ar", "44100",
                                     "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if r_.returncode != 0:
                    return self._json({"error": "音声を変換できませんでした: " + r_.stderr.decode("utf-8", "ignore")[-200:]})
            return self._json({"ok": True})
        if path == "/api/need_proxy":
            if STATE["project"] and STATE["project"].get("video"):
                ensure_proxy(STATE["project"])
            return self._json({"ok": True})
        if path == "/api/words":
            import words as _w
            _w.save_groups(body.get("groups"))
            return self._json({"groups": _w.groups()})
        if path == "/api/breaktest":
            return self._json(breaktest_save(body))
        if path == "/api/telops":
            p = STATE["project"]
            if not p:
                return self._json({"error": "プロジェクトがありません"})
            d = project_dir(p)
            if body.get("reset"):
                (d / "telops_edited.json").unlink(missing_ok=True)
            else:
                data_ = body.get("data", {})
                _tj = d / "telops.json"
                if body.get("gen") and _tj.exists() and str(_tj.stat().st_mtime_ns) != str(body.get("gen")):
                    return self._json({"ok": False, "stale": True})      # the page shows an older analysis: never write it back
                try:                                    # a telop that was added and not filled in yet is not saved
                    for b_ in data_.values():
                        b_["pieces"] = [q for q in b_.get("pieces", []) if (q.get("text") or "").strip() or q.get("speaker", 0) < 0]
                except Exception:   # noqa: BLE001
                    pass
                learn_wave(d, p, data_)
                learn_words(d, data_, p.get("script", ""))
                auto_ = read_json(d / "telops.json", {}) or {}
                (d / "telops_edited.json").write_text(json.dumps(flag_user(touched_blocks(data_, auto_, True), auto_), ensure_ascii=False, indent=1), encoding="utf-8")
                try:                                    # what was changed by hand is remembered (learn_edits.py)
                    import learn_edits
                    learn_edits.record(d, data_, read_json(d / "telops.json", {}) or {})
                except Exception:   # noqa: BLE001
                    pass
            return self._json({"ok": True})
        if path == "/api/chunks":
            try:
                import jp_seg
                return self._json({"chunks": [jp_seg.chunks(str(t_)) for t_ in (body.get("texts") or [])[:400]]})
            except Exception as e_:   # noqa: BLE001
                return self._json({"error": str(e_)})
        if path == "/api/alts":
            p = STATE["project"]
            if not p:
                return self._json({"error": "プロジェクトがありません"})
            try:
                import parse_script as ps_
                import exact_script as ex_
                blocks_ = {b_["id"]: b_ for b_ in ps_.parse(p.get("script", ""))}
                bid_ = body.get("bid")
                if bid_ not in blocks_:
                    return self._json({"alts": []})
                return self._json({"alts": ex_.align_alts(body.get("pieces", []), blocks_[bid_])})
            except Exception as e_:   # noqa: BLE001
                return self._json({"error": str(e_)})
        if path == "/api/gap":
            p = STATE["project"]
            if not p:
                return self._json({"text": None})
            try:
                import parse_script as ps_
                import exact_script as ex_
                blocks_ = {b_["id"]: b_ for b_ in ps_.parse(p.get("script", ""))}
                bid_ = body.get("bid")
                if bid_ not in blocks_:
                    return self._json({"text": None})
                return self._json({"text": ex_.gap_text(body.get("pieces", []), int(body.get("k", 0)), blocks_[bid_])})
            except Exception as e_:   # noqa: BLE001
                return self._json({"error": str(e_)})
        if path == "/api/exact":
            p = STATE["project"]
            if not p:
                return self._json({"error": "プロジェクトがありません"})
            try:
                import parse_script as ps_
                import exact_script as ex_
                blocks_ = {b_["id"]: b_ for b_ in ps_.parse(p.get("script", ""))}
                bid_ = body.get("bid")
                if bid_ not in blocks_:
                    return self._json({"error": "台本にこのブロックがありません"})
                return self._json({"pieces": ex_.exact_block(body.get("pieces", []), blocks_[bid_])})
            except Exception as e_:   # noqa: BLE001
                return self._json({"error": "台本どおりにできませんでした: %s" % e_})
        if path == "/api/run":
            return self._json(start_job(body.get("stage", "all")))
        if path == "/api/cl":
            print("[画面] " + str(body.get("m"))[:300], flush=True)
            return self._json({"ok": True})
        if path == "/api/reveal":
            f = body.get("path")
            if f and Path(f).exists() and platform.system() == "Darwin":
                subprocess.Popen(["open", "-R", f])
            return self._json({"ok": True})
        self._send(404, "not found", "text/plain")


WEBVIEW = None          # the pywebview module while CH Cut runs in its own window


def open_window(url):
    """Open the page in the Mac's normal (default) browser - no extra Chrome window."""
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", url])
            return
    except Exception:   # noqa: BLE001
        pass
    webbrowser.open(url)


def watchdog():
    """Quit by itself once the window has been closed (nothing asks for the state for 2 minutes)."""
    while True:
        time.sleep(5)
        with LOCK:
            running = STATE["job"]["state"] == "running"
        if not running and time.time() - LAST_SEEN[0] > 120:
            os._exit(0)


BREAK_SAMPLES = [
    "今日はみんなで新作のゲームを遊んでいきたいと思います", "えっ、それ本当に昨日買ったばかりのやつなの？",
    "そんなに強くないのにずっと調子に乗ってるのがムカつくわ", "とりあえず一回落ち着いて話を聞いてもらってもいいですか",
    "絶対にこの後すごいことが起きるから最後まで見てね", "いやいや、それはさすがに言いすぎだと思うんだけど",
    "この前ラーメン食べに行ったらめちゃくちゃ並んでてさ", "でも結局その店はもう閉まってたんだよね",
    "もし明日雨だったら家でゆっくり映画でも見ようか", "なんでそんな大事なことを先に言わないのよ",
    "ちょっと待って、今のところもう一回見せてくれる？", "俺が全部悪いみたいな言い方するのやめてもらえる",
    "あっちの店のほうが絶対安いし美味しいと思うよ", "先に言っておくけど今回は本当に負ける気がしない",
    "このあとみんなで焼肉でも食べに行こうぜ", "昨日の夜ずっと考えてたんだけど答えが出なくてさ",
    "一番最初に思いついたのはやっぱりこれだったんだよね", "まさかあんなところで再会するとは思ってもみなかった",
    "どうせまた同じことを繰り返すだけなんでしょ", "嘘でしょ、まだ半分も終わってないじゃん",
    "そろそろ本気出さないとやばいことになるぞ", "それってつまりもう一回最初からやり直しってこと？",
    "みんなのおかげでチャンネル登録者が一万人を超えました", "今度はもっと面白い企画を考えてくるから楽しみにしててね",
    "文句があるなら直接言ってくれたほうがありがたいんだけど", "あれだけ練習したのに全然うまくいかなかった",
    "こんなに美味しいケーキは生まれて初めて食べたかもしれない", "後ろで見てるだけじゃなくてたまには手伝ってよ",
    "冗談だと思って聞き流してたらまさかの本気だった", "とにかく今日はこのまま最後までやり切ろう",
]


def _guess_telops(text):
    import subtitles as S
    r = S._limit_lines({"text": text, "start": 0.0, "end": 4.0, "s0": 0.0, "s1": 4.0}, None)
    return [S._split_two_lines(q["text"]).split("\n") for q in r]


def breaktest_samples():
    ef = Path(__file__).resolve().parent / "presets" / "break_examples.json"
    done = {}
    try:
        for e in (read_json(ef, {}) or {}).get("examples", []):
            done["".join("".join(t) for t in e.get("telops", []))] = 1
    except Exception:   # noqa: BLE001
        pass
    def _norm(t):
        return re.sub(r"[\s、。，．,.！？!?…・「」『』（）()]+", "", t or "")

    def _texts(o, acc):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "text" and isinstance(v, str):
                    acc.append(v)
                else:
                    _texts(v, acc)
        elif isinstance(o, list):
            for v in o:
                _texts(v, acc)

    hand = []                       # sentences the user already cut by hand: asking again teaches nothing
    try:
        for d in PROJECTS.iterdir():
            f = d / "telops_edited.json"
            if f.exists():
                _texts(read_json(f, {}), hand)
    except Exception:   # noqa: BLE001
        pass
    blob = _norm("".join(hand))
    cand = []
    p = STATE.get("project")
    if p and (p.get("script") or "").strip():
        for ln in re.split(r"[\n。！？!?]+", p["script"]):
            ln = ln.strip()
            if len(ln) >= 9 and ln not in cand:
                cand.append(ln)
    cand += BREAK_SAMPLES
    out = []
    for c in cand:
        if c in done or any(o["text"] == c for o in out):
            continue
        n = _norm(c)
        if blob and (n in blob or (len(n) >= 8 and n[:8] in blob and n[-8:] in blob)):
            continue
        try:
            out.append({"text": c, "telops": _guess_telops(c)})
        except Exception:   # noqa: BLE001
            continue
        if len(out) >= 30:
            break
    return {"samples": out, "done": len(done)}


def breaktest_save(body):
    ef = Path(__file__).resolve().parent / "presets" / "break_examples.json"
    d = read_json(ef, {}) or {}
    ex = d.setdefault("examples", [])
    tel = [[str(l) for l in t if str(l)] for t in body.get("telops", []) if t]
    if not tel:
        return {"ok": False}
    full = "".join("".join(t) for t in tel)
    ex[:] = [e for e in ex if "".join("".join(t) for t in e.get("telops", [])) != full]
    ex.append({"text": full, "telops": tel})
    ef.parent.mkdir(parents=True, exist_ok=True)
    ef.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "n": len(ex)}


def open_latest_project():
    """The app always starts on the project that was worked on last (an empty "新規" one is skipped)."""
    try:
        for n in list_projects():
            p = load_project(n)
            if p.get("video") or p.get("audio") or (p.get("script") or "").strip():
                STATE["project"] = p
                return
    except Exception:   # noqa: BLE001
        pass


def self_update_watch():
    """When any program file is replaced by a newer one, start the new one by itself (once nothing is being analysed):
    nobody has to close and reopen the app."""
    launcher = HERE / "CH Cut.app" / "Contents" / "MacOS" / "CHCut"
    while True:
        time.sleep(5)
        try:
            if engine_sig() == ENGINE_SIG:
                continue
            time.sleep(8)                                   # a copy in progress finishes first
            while (STATE.get("job") or {}).get("state") == "running":
                time.sleep(3)
            if engine_sig() == ENGINE_SIG:
                continue
            if launcher.exists():
                subprocess.Popen([str(launcher)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1.0)
                os._exit(0)
        except Exception:   # noqa: BLE001
            pass


def main():
    PROJECTS.mkdir(parents=True, exist_ok=True)
    open_latest_project()
    url = "http://127.0.0.1:%d/" % PORT
    srv = None
    for _ in range(20):                   # the old copy may still be shutting down: wait for the port instead of giving up
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            time.sleep(0.5)
    if srv is None:                       # really still running: just bring up a window for it
        print("CH Cut はすでに起動しています。ウインドウを開きます。")
        open_window(url)
        return
    print("CH Cut を起動しました。ブラウザが開かない場合は次を開いてください:\n  %s\n（終了するには、このウインドウで control+C）" % url)
    threading.Thread(target=media_watch, daemon=True).start()
    threading.Thread(target=self_update_watch, daemon=True).start()
    if os.environ.get("CHCUT_NO_BROWSER") != "1" and os.environ.get("CHCUT_WINDOW", "app") == "app" and platform.system() == "Darwin":
        try:
            import webview                       # an app window of its own (no browser, no address bar)
        except Exception:   # noqa: BLE001
            webview = None
        if webview is not None:
            global WEBVIEW
            WEBVIEW = webview
            threading.Timer(4.0, lambda: timetree.preload(webview)).start()
            try:                                 # the Dock and the menu bar say "CH Cut" (not "Python"), with our icon
                from AppKit import NSApplication, NSImage
                from Foundation import NSBundle
                _info = NSBundle.mainBundle().infoDictionary()
                _info["CFBundleName"] = "CH Cut"
                _info["CFBundleDisplayName"] = "CH Cut"
                _ic = Path(__file__).resolve().parent / "CH Cut.app" / "Contents" / "Resources" / "AppIcon.icns"
                if _ic.exists():
                    NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(_ic)))
            except Exception:   # noqa: BLE001
                pass
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                webview.create_window("CH Cut", url, width=1480, height=940, min_size=(1000, 640), background_color="#1c1d22")
                _st = Path.home() / "Library" / "Application Support" / "CHCut" / "webview"
                _st.mkdir(parents=True, exist_ok=True)
                try:
                    webview.start(private_mode=False, storage_path=str(_st))   # returns when the window is closed
                except TypeError:                # an older pywebview without those options
                    webview.start()
                os._exit(0)
            except Exception as e_:   # noqa: BLE001
                print("アプリ画面を開けなかったので、ブラウザで開きます:", e_)
                open_window(url)
                while True:
                    time.sleep(3600)
    if os.environ.get("CHCUT_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: open_window(url)).start()
        if os.environ.get("CHCUT_AUTOQUIT", "1") == "1":
            threading.Thread(target=watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>CH Cut</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#1c1d22;--panel:#25272e;--panel2:#2c2f37;--ink:#eceef2;--mute:#9aa0ac;--line:#383b45;--acc:#ff5a4f;--acc2:#ff7a70;--ok:#4cc38a;--ng:#ff6b6b;--warn:#f0b34a}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"Hiragino Sans",sans-serif;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--line);-webkit-app-region:drag}
.logo{width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,#ff5a4f,#ff9a5a);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;color:#fff;letter-spacing:-1px}
header{flex-wrap:nowrap;min-width:0}header button,header select,header h1,header .logo{white-space:nowrap;flex:none}header #name{flex:1 1 120px;min-width:90px;width:auto!important;max-width:240px}header #recent{width:auto!important;min-width:0;max-width:200px;flex:0 1 200px}header h1{font-size:16px;margin:0}header .sub{color:var(--mute);font-size:12px}header .sp{flex:1}
select,input[type=text],textarea{background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit;min-width:0}
select:focus,input:focus,textarea:focus{outline:2px solid var(--acc);outline-offset:-1px}
button{padding:8px 14px;border:1px solid var(--line);background:var(--panel2);color:var(--ink);border-radius:8px;font:inherit;cursor:pointer}
button:hover{border-color:#555a68}button:disabled{opacity:.45;cursor:default}
button.p{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}button.p:hover:not(:disabled){background:var(--acc2)}
.wrap{flex:1;display:grid;grid-template-columns:340px 1fr;gap:0;min-height:0}
.side{background:var(--panel);border-right:1px solid var(--line);padding:18px;overflow:auto}
.main{padding:20px 24px;overflow:auto}
.side h3,.main h3{margin:0 0 10px;font-size:12px;color:var(--mute);font-weight:600;letter-spacing:.06em}
.f{margin-bottom:14px}.f label.t{display:block;color:var(--mute);font-size:12px;margin-bottom:4px}
.drop.big{padding:26px 12px;margin-bottom:14px;border-width:2px}.drop.big .dn{font-size:15px;font-weight:600}.drop{border:2px dashed var(--line);border-radius:12px;padding:16px 12px;text-align:center;cursor:pointer;background:var(--panel2);transition:.15s}.drop:hover,.drop.on{border-color:var(--acc);background:#2f2a2c}.drop.has{border-style:solid;border-color:#4a5a50}.drop .dn{word-break:break-all;font-size:13px}.drop .ds{color:var(--mute);font-size:11px;margin-top:2px}.f .r{display:flex;gap:6px}.f .r input{flex:1}
textarea{width:100%;height:230px;font:12.5px/1.6 ui-monospace,Menlo,monospace;resize:vertical}
.opts label{display:flex;gap:8px;align-items:center;margin:6px 0;color:#d7dae1;cursor:pointer}
.opts input{accent-color:var(--acc)}
.hero{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:18px}
.steps{display:flex;gap:6px;margin:0 0 14px}.st{flex:1;height:6px;border-radius:3px;background:var(--panel2)}
.st.on{background:var(--acc)}.st.done{background:var(--ok)}
.stl{display:flex;gap:6px;color:var(--mute);font-size:12px;margin:-8px 0 14px}.stl span{flex:1}
.big{padding:12px 22px;font-size:15px;border-radius:10px}
.vc{min-width:0}.vc .heardrow,.vc .shownrow{display:flex;flex-wrap:wrap;gap:4px;align-items:center}.vc .heardrow{margin-bottom:5px;padding-bottom:5px;border-bottom:1px dashed #3a3e4a;min-height:22px}
.hl{font-size:10px;color:var(--mute);width:42px;flex:none}.hc{font-size:12px;color:#8a90a0;background:#23252c;border:1px solid #30333c;border-radius:6px;padding:1px 6px}
.wc{position:relative;font-size:16px;line-height:1.35;padding:3px 9px;border:1px solid #4a4f5e;border-radius:7px;background:#272a33;cursor:pointer;user-select:none;transition:background .06s}
.wc:hover{border-color:#8f9bff}.wc.on{background:#4a3f7a;border-color:#a89bff;box-shadow:inset 3px 0 0 #fff,0 0 10px #a89bff88}.wc.nl::before{content:"↵";color:var(--mute);font-size:11px;margin-right:4px}
.wc .wx{display:none;position:absolute;right:-6px;top:-8px;width:16px;height:16px;line-height:14px;text-align:center;font-size:12px;border-radius:50%;background:#e25555;color:#fff;font-weight:700}.wc:hover .wx{display:block}
.trow.del .wc{text-decoration:line-through;opacity:.5}

.stk{position:sticky;top:0;z-index:40;background:var(--bg);padding:12px 0 10px;margin:-20px 0 10px;border-bottom:1px solid var(--line)}
#label{margin-left:12px;color:var(--mute)}
pre{background:#111217;color:#bff0d4;padding:12px;border-radius:10px;max-height:200px;overflow:auto;font-size:12px;margin:14px 0 0;white-space:pre-wrap;border:1px solid var(--line)}
.res{margin-top:14px;padding:12px 14px;border-radius:10px;background:rgba(76,195,138,.12);border:1px solid rgba(76,195,138,.4)}
.res b{color:var(--ok)}.small{font-size:12px;color:var(--mute)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:6px 6px 14px;margin-bottom:18px}
table{width:100%;border-collapse:collapse}th,td{padding:10px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
th{color:var(--mute);font-size:12px;font-weight:600}tr:last-child td{border-bottom:none}
.dr{font-size:12px;color:var(--ng);margin-top:2px}.good{color:var(--ok)}.bad{color:var(--ng)}
.warnbox{background:rgba(240,179,74,.12);border:1px solid rgba(240,179,74,.45);border-radius:10px;padding:10px 14px;margin-bottom:16px}
.empty{color:var(--mute);padding:30px;text-align:center}
.thead{padding:14px 16px 6px}.thead h3{margin:0 0 4px}
#dock{position:fixed;right:14px;top:84px;width:420px;max-height:calc(100vh - 100px - var(--tlh,0px));overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;z-index:30;padding:12px;box-shadow:0 8px 30px #0008}
.main.docked{padding-right:456px}
.vwrap{width:fit-content;max-width:100%;margin:0 auto}
#vid{max-height:38vh;max-width:100%;min-height:80px;min-width:140px}
.trow.del{opacity:.45}.trow.del textarea{text-decoration:line-through}.trow.sel{background:#33384a;outline:1px solid #6b7dff}
.bc.skipped{opacity:.4;filter:grayscale(.6)}.bskip{margin-left:8px;font-size:12px;padding:3px 10px;border-radius:999px}
.tgrid{display:block}
.tleft{min-width:0;border-right:1px solid var(--line)}
.tright{position:sticky;top:0;align-self:start}
.vbar{background:var(--panel);padding:12px 14px}
#wave2{width:100%;height:150px;display:block;background:#15161a;border-radius:8px;margin-top:10px;cursor:col-resize;touch-action:none;border:1px solid #2f3340}
#wave{width:100%;height:85px;background:#15161a;border-radius:8px;margin-top:10px;cursor:ew-resize;touch-action:none}
@media(max-width:1100px){.tgrid{grid-template-columns:1fr}.tright{position:static}}
.vwrap{position:relative;background:#000;border-radius:8px;overflow:hidden;min-height:60px}
#vid{display:block;margin:0 auto;max-width:100%;max-height:440px;background:#000}
#vov{position:absolute;left:0;top:0;width:0;height:0;pointer-events:none;overflow:hidden}
#vmsg{position:absolute;left:8px;top:6px;color:#bbb}
.vctl{display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap}
.d2row{padding:6px 10px 12px;border-bottom:1px solid #2f323b;background:#1d1f26}.d2row .vctl{margin-top:6px}.trow.act{background:#2f3340;outline:1px solid var(--acc);border-radius:8px}
.tb{border-top:1px solid var(--line);padding:12px 16px}.tb h4{margin:0 0 8px;font-size:13px}
.trow{position:relative;display:grid;grid-template-columns:64px 1fr 58px;gap:10px;align-items:start;padding:8px 10px;border-bottom:1px solid #2f323b;cursor:text}
.trow:hover{background:#2a2d35}.trow .tools{position:absolute;right:8px;top:-30px;display:none;gap:4px;background:#1c1d22;border:1px solid var(--line);border-radius:8px;padding:3px;z-index:3}
.trow:hover .tools,.trow.act .tools{display:flex}.trow .ttime{font-size:12px;color:var(--mute);line-height:1.3;cursor:pointer}.trow .ttime b{display:block;color:var(--ink);font-size:13px}
.trow textarea{background:transparent;border:1px solid transparent;font-size:16px;line-height:1.45;height:auto;min-height:24px;padding:2px 4px;resize:none;overflow:hidden}
.trow textarea:focus{border-color:var(--acc);background:#1c1d22}.trow select{padding:3px 4px;font-size:12px;border-radius:999px;font-weight:700}
.sp0 select{background:#ffe7ee;color:#7a1030}.sp1 select{background:#e1ecff;color:#123a7a}.sp-1 select{background:#fff3c4;color:#6a4a00}
.trow input[type=number]{width:78px}.trow .nb{padding:4px 8px}.trow .ib{padding:4px 8px;font-size:12px}
.bc{margin:10px 0;padding:12px 14px;border-radius:14px;background:linear-gradient(135deg,hsl(var(--h) 55% 22% / .55),var(--panel2) 60%);border-left:6px solid hsl(var(--h) 85% 62%)}
.bh{display:flex;gap:10px;align-items:center}.bid{background:hsl(var(--h) 85% 62%);color:#1b1b1f;font-weight:800;border-radius:999px;padding:2px 12px;font-size:13px}
.bcue{font-weight:700;font-size:15px;flex:1}.bcut{color:var(--mute);font-size:12px}
.bwarn{color:#ff8f8f;font-size:12px;margin-top:6px}.bunh{color:#ffd166;font-size:12px;margin-top:4px}
.tks{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}
.tk{display:flex;align-items:center;gap:6px;padding:6px 8px;border-radius:12px;border:2px solid var(--c);background:#00000030}
.tk.on{background:var(--c);color:#14151a}.tk.on .tkp{background:#00000030;color:#14151a}
.tkb{background:none;border:none;color:inherit;display:flex;gap:8px;align-items:baseline;cursor:pointer;padding:2px 4px}.tkb b{font-size:15px}.tkb span{font-size:12px;opacity:.8}.tkb i{font-style:normal;font-size:11px;font-weight:700}
.tkp{padding:4px 9px;border-radius:999px}.tkuse{font-size:12px;font-weight:800;padding:0 6px}
.tka{font-size:12px;border-radius:999px;padding:4px 10px;background:transparent;border:1px dashed var(--mute);color:var(--mute)}.tka.yes{background:#2bb67333;border:1px solid #2bb673;color:#8ff0c0}
.r2{display:flex;gap:6px;align-items:center}.altbox{margin-top:8px;padding:8px 10px;background:var(--panel2);border-radius:8px}.altrow{display:flex;gap:8px;align-items:center;font-size:12px;margin-top:4px}.altrow label{display:flex;gap:4px;align-items:center}.unh{color:var(--warn);font-size:12px;margin:2px 0 8px}.lane2{opacity:.8}
.vwrap{width:min(100%,calc(min(230px,28vh)*var(--ar,.5625)));aspect-ratio:var(--ar,.5625);margin:0 auto;max-width:100%;min-height:0}#vid{position:absolute;inset:0;width:100%;height:100%;max-height:none;min-height:0;min-width:0;object-fit:contain;margin:0}#vov{left:0;top:0;width:100%;height:100%}.vc textarea{width:100%;margin-top:2px}
.lane2{border-left:3px solid #f5a}.l2b{grid-column:1/-1;grid-row:1;display:block;font-size:10px;color:#f5a;font-weight:700;margin-top:2px;line-height:1.2}.trow{grid-template-columns:64px 1fr 58px 130px}.ln{align-self:stretch;display:flex;gap:2px;min-height:22px;cursor:pointer;border-radius:3px}.ln:hover{background:rgba(255,255,255,.06)}.lb{flex:1;min-width:0;font-size:10px;line-height:1.15;color:#fff;overflow:hidden;position:relative;border-left:3px solid #9b86ff;background:rgba(155,134,255,.28)}.lb.c1{border-color:#ff9d4d;background:rgba(255,157,77,.28)}.lb.c2{border-color:#4dd6c0;background:rgba(77,214,192,.28)}.lb.c3{border-color:#ff6fa8;background:rgba(255,111,168,.28)}.lb span{display:block;padding:1px 2px;white-space:nowrap}.lb .wx{position:absolute;right:1px;top:0;display:inline;opacity:.7;cursor:pointer}#cap{margin:6px auto 0;min-height:64px;max-width:100%;text-align:center;background:#111318;border:1px solid #2f323b;border-radius:8px;padding:8px 6px;display:flex;flex-direction:column;justify-content:center;gap:2px}.capi{font-family:'ラノベPOP','Rampart One','Hiragino Maru Gothic ProN','Hiragino Sans',sans-serif;font-weight:900;font-size:26px;line-height:1.15;white-space:pre-wrap}.lb{display:flex;align-items:center;gap:3px;padding:0 2px}.lb.f{border-top-left-radius:4px;border-top-right-radius:4px}.lb.l{border-bottom-left-radius:4px;border-bottom-right-radius:4px}.lb.cL{border-color:#e8d24d;background:rgba(232,210,77,.25)}.bhd{position:absolute;left:0;right:0;height:7px;cursor:ns-resize;background:rgba(255,255,255,.55);z-index:2}.bhd.top{top:0;border-radius:4px 4px 0 0}.bhd.bot{bottom:0;border-radius:0 0 4px 4px}.nin{flex:1;min-width:0;width:0;background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.25);border-radius:3px;color:#fff;font-size:11px;padding:1px 3px;margin-top:8px}.nx{cursor:pointer;color:#fff;font-size:11px;padding:0 2px;margin-top:8px;opacity:.9}.nx:hover{color:#ff6b6b}.nl{font-size:10px;margin-top:8px}.lane2{opacity:1;border-left:3px solid #f5a}.l2b{display:none}.lb.cL{border-color:#ff3b4d!important;background:rgba(255,59,77,.35)!important}.wc.cl{box-shadow:-3px 0 0 #ff4d6a}.wc.cr{box-shadow:3px 0 0 #ff4d6a}@font-face{font-family:'CHPOP';src:local('LightNovelPopV2'),local('Light Novel Pop V2'),local('LightNovelPOP'),local('ラノベPOP'),url('/font/ch')}.ln:empty:hover::after{content:'＋※';color:#8a8;font-size:11px;padding:3px}.fb{color:#fff;background:#d33;border-radius:4px;font-size:10px;padding:0 4px;margin-top:3px;display:inline-block;font-weight:700}
.side{display:flex;flex-direction:column;padding:0;overflow:hidden}
.sidein{flex:1;min-height:0;overflow:auto;padding:14px 16px 6px}
.sidego{padding:10px 16px 14px;border-top:1px solid var(--line);background:var(--panel)}
.sidein h3{margin:0 0 6px}
.sidein .f{margin-bottom:8px}.sidein .f label.t{margin-bottom:2px;font-size:11px}
.sidein .two{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.sidein .two .drop{padding:8px 6px}.sidein .two .drop .dn{font-size:11.5px;line-height:1.35}
.sidein .drop.big{padding:8px 10px;margin-bottom:6px}.sidein .drop.big .ds{display:none}
.sidein textarea{height:clamp(54px,10vh,150px)}.sidein h3{margin:6px 0 4px}.sidein .f{margin:5px 0}.sidein .f label.t{margin-bottom:2px}.sidein .opts label{margin:1px 0}.more summary{cursor:pointer;color:var(--mute);font-size:12px;margin-top:8px}
.sidein .opts{display:grid;grid-template-columns:1fr 1fr;gap:0 8px}.sidein .opts label{margin:3px 0;font-size:12px;line-height:1.35}
.sidein h3.o{margin:10px 0 4px}
.sidein .r input{padding:5px 8px;font-size:12px}.sidein .r button{padding:5px 10px}
.tlov{position:fixed;left:0;right:0;bottom:0;height:320px;background:#101014;border-top:1px solid #2a2b33;z-index:40;display:none;flex-direction:column}.tlov.on{display:flex}
.tlon #wave{display:none}.vctl{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.srcsel{margin-top:4px;width:auto;font-size:11px;padding:2px 6px}#capsrc{width:auto;margin:4px 0 0 auto;font-size:12px;padding:3px 8px}#capte{width:100%;box-sizing:border-box;font:700 26px/1.25 'Hiragino Maru Gothic ProN','Hiragino Sans',sans-serif;text-align:center;background:#000;color:#fff;border:1px solid var(--acc);border-radius:8px;padding:8px;resize:none;height:auto}
.tlbar{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid #2a2b33}.tlscroll{flex:1 1 auto;min-height:0;overflow-x:auto;overflow-y:auto;position:relative}#tlgrip input{position:absolute;right:14px;top:-1px;width:110px;height:12px;accent-color:#6b7aa8;opacity:.8}#tlgrip{position:relative;flex:0 0 12px;cursor:ns-resize;background:#1c1d24;border-bottom:1px solid #2a2b33;touch-action:none}#tlgrip:hover{background:#3a3f55}#tlspc{position:absolute;left:0;top:0}
.tlscroll canvas{display:block;cursor:default;touch-action:none}.tled{padding:8px 12px;border-top:1px solid #2a2b33}.tled textarea{width:100%;font-size:18px;background:#15161a;color:#fff;border:1px solid #2a2b33;border-radius:8px;padding:8px}
.btov{position:fixed;inset:0;background:rgba(10,10,14,.92);z-index:50;display:none;flex-direction:column;align-items:center;padding:28px 24px;overflow:auto}
.wdgrid{display:grid;grid-template-columns:200px 1fr 70px;gap:8px;width:min(720px,92vw);align-items:center}.wdgrid input{background:#1d1e26;border:1px solid #2f3140;color:var(--fg,#eee);border-radius:8px;padding:9px 10px;font-size:15px}.wdh{color:var(--mute);font-size:12px}.wdbt{display:flex;gap:10px;margin-top:18px}#wd{justify-content:flex-start}
.btov.on{display:flex}.btov h2{margin:0 0 4px;font-size:18px}.btov .btsub{color:var(--mute);font-size:13px;margin-bottom:18px;text-align:center;line-height:1.7}
.btsrc{display:flex;flex-wrap:wrap;justify-content:center;align-items:stretch;margin:6px 0 22px;max-width:1100px}
.btc{font-size:34px;font-weight:800;padding:6px 0;line-height:1.2;letter-spacing:0}
.btg{display:inline-flex;align-items:center;justify-content:center;width:12px;min-height:56px;cursor:pointer;border-radius:4px;color:#fff;font-size:15px;font-weight:700;margin:0 1px}
.btg:hover{background:rgba(255,255,255,.18)}.btg.g1{width:30px;background:#2f7de0}.btg.g2{width:30px;background:#e0573f}
.btprev{display:flex;flex-wrap:wrap;gap:14px;justify-content:center;margin-bottom:22px;max-width:1100px}
.btcard{background:#000;border:1px solid #444;border-radius:8px;padding:14px 26px;text-align:center;font-size:30px;font-weight:900;line-height:1.25;color:#fff;-webkit-text-stroke:1.5px #000;paint-order:stroke fill;text-shadow:0 0 6px #000}
.btbar{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}.btbar button{padding:9px 20px;font-size:15px}
.wrap{grid-template-columns:1fr}.side{display:none}
body.intake .side{display:flex;position:fixed;top:56px;bottom:36px;left:50%;transform:translateX(-50%);width:min(680px,92vw);z-index:80;border:1px solid var(--line);border-radius:14px;box-shadow:0 0 0 100vmax #000d}
body.intake .sidein textarea{height:clamp(90px,16vh,220px)}
.busy #busybar,.busyjob #busybar{opacity:1}#busybar{position:fixed;left:0;top:0;height:4px;width:100%;z-index:200;opacity:0;pointer-events:none;transition:opacity .15s;background:linear-gradient(90deg,transparent 0,var(--acc) 40%,#ffd60a 60%,transparent 100%);background-size:40% 100%;background-repeat:no-repeat;animation:bb 1s linear infinite}
@keyframes bb{from{background-position:-40% 0}to{background-position:140% 0}}
#busypill{position:fixed;left:50%;top:78px;transform:translateX(-50%);z-index:200;display:none;align-items:center;gap:10px;padding:9px 18px;border-radius:999px;background:#000d;border:1px solid var(--acc);font-size:15px;font-weight:700;color:#fff;pointer-events:none;box-shadow:0 4px 24px #0009}
.busy #busypill,.busyjob #busypill{display:flex}#busypill i{width:18px;height:18px;border-radius:50%;border:3px solid #fff4;border-top-color:var(--acc);animation:bs .7s linear infinite}@keyframes bs{to{transform:rotate(360deg)}}
#tcur,#d2info{font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;display:inline-block;min-width:8ch;white-space:nowrap}#dock{overflow-x:hidden}#vov{display:none!important}#cap{height:96px;min-height:96px;overflow:hidden}.vwrap{width:min(100%,calc(var(--vwh,230px)*var(--ar,.5625)))}.hc.sel{outline:2px solid #ffd60a;border-radius:3px}#dock{padding:8px 10px}#dock .vctl{margin-top:6px;gap:8px}#dock .vctl button{padding:6px 12px}.hc:empty{display:inline-block;min-width:40px;height:16px;border:1px dashed #666;border-radius:3px;vertical-align:middle}
</style></head><body>
<div id="busybar"></div><div id="busypill"><i></i><span id="busytx">読み込み中</span></div><div id="ver" style="position:fixed;left:8px;bottom:3px;font-size:10px;opacity:.45;z-index:5;pointer-events:none">版 __VER__</div><div id="conn" style="display:none;position:fixed;left:0;right:0;top:0;z-index:9999;background:#c0392b;color:#fff;padding:10px 14px;font-weight:700;text-align:center">接続が切れています</div>
<header><div class="logo">CH</div><div><h1>CH Cut</h1><div class="sub"></div></div>
<input type="text" id="name" placeholder="プロジェクト名" onchange="setf('name',this.value)" style="width:240px;margin-left:14px"><span class="sp"></span>
<button onclick="intake(true)">素材</button> <button onclick="run('analyze')">再解析</button> <button onclick="wdOpen()">表記統一</button> <select id="recent" style="width:220px"><option value="">最近のプロジェクト…</option></select>
<button onclick="newProj()">＋ 新規</button>
<button class="p" id="exp" onclick="doExport()" style="background:#2f9e6e">書き出し</button><span id="label" class="small" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span></header>
<div class="wrap">
<div id="dock" style="display:none"><div class="vwrap" id="vwrap"><video id="vid" muted playsinline preload="auto"></video><div id="vov"></div><div id="vmsg" class="small"></div></div><div id="cap"></div><div class="kernrow"><label>カーニング <select id="kerning" onchange="setKern(this.value)"><option>メトリクス</option><option>オプティカル</option><option>和文等幅</option></select></label></div><select id="capsrc" style="display:none" onchange="capSrc(this.value);this.blur()"><option value="s">台本</option><option value="h">聞き取り</option></select>
   <div class="vctl"><button class="p" id="pp" onclick="togglePlay()">▶ 再生</button> <span id="tcur" class="small">0:00.00</span>
    <button onclick="undo()">↶ 復元</button>
    <label class="small" style="display:flex;gap:4px;align-items:center"><input type="checkbox" id="fx" checked onchange="fxToggle()">整音プレビュー</label>
    <span id="tmsg" class="small"></span></div>
   <canvas id="wave" width="900" height="170"></canvas>
   
</div>

<div class="side"><div class="sidein">
 <h3>素材</h3>
 <div class="drop big" id="dz_all"><div class="dn">動画と音声をここにドロップ</div><div class="ds" id="dzmsg"></div></div>
 <div class="two"><div class="f"><label class="t">動画</label><div class="drop" id="dz_video" data-k="video"><div class="dn" id="video">未選択</div></div></div>
 <div class="f"><label class="t">別録り音声</label><div class="drop" id="dz_audio" data-k="audio"><div class="dn" id="audio">未選択</div></div></div></div>
 <div class="f"><label class="t">台本</label>
  <textarea id="script" onchange="setf('script',this.value)"></textarea>
  <div style="margin-top:4px"><button onclick="ttOpen()" style="padding:4px 10px;font-size:12px">タイムツリーから選択</button></div></div>
 <h3 class="o">オプション</h3>
 <div class="opts">
  <label><input type="checkbox" id="subtitles" onchange="setf('subtitles',this.checked)">テロップ字幕</label>
  <label><input type="checkbox" id="diarize" onchange="setf('diarize',this.checked)">話者分離</label>
  <label><input type="checkbox" id="dual_mono" onchange="setf('dual_mono',this.checked)">デュアルモノ</label>
  <label><input type="checkbox" id="voice_preset" onchange="setf('voice_preset',this.checked)">ボイス整音</label>
  <label><input type="checkbox" id="swap" onchange="setf('swap_speakers',this.checked)">チ・ホ入替</label>
 </div>
 <details class="more"><summary>詳細設定</summary>
 <div class="f" style="margin-top:6px;display:none"><input type="text" id="out_dir"></div>
 <div class="f"><label class="t">テロップの種類</label><div class="r"><input type="text" id="cfg" onchange="setf('speakers_config',this.value)"><button onclick="pick('config')">選択</button></div></div>
 </details>
</div>
<div class="sidego"><button class="p big" id="go" onclick="intake(false);run('analyze')" style="width:100%">解析</button><button onclick="intake(false)" style="width:100%;margin-top:6px">完了</button></div>
</div>
<div class="main">
 <div id="doctor"></div>
 <div class="hero">
  <div class="steps"><div class="st" id="s1"></div><div class="st" id="s2"></div><div class="st" id="s3"></div></div>
  <div class="stl"><span>文字起こし</span><span>同期・テイク選び・カット</span><span>書き出し</span></div>
  <pre id="log" style="display:none"></pre>
  <div id="result"></div>
 </div>
 <div class="card" id="reportCard" style="display:none">
  <div style="padding:14px 14px 4px"><h3>ブロックとテイク</h3>
  </div>
  <div id="blocks"></div></div>
 <div class="card" id="telopCard" style="display:none">
  <div class="thead"><h3>テロップを編集</h3></div>
  <div id="telops"></div>
 </div>
 <div class="empty" id="empty"></div>
</div></div>
<script>
const TOKEN="__TOKEN__";let S=null;
let BUSYN=0,BUSYT=null;
function busy(d){BUSYN+=d;if(BUSYN>0){if(!BUSYT)BUSYT=setTimeout(()=>{if(BUSYN>0)document.body.classList.add('busy')},150)}else{BUSYN=0;clearTimeout(BUSYT);BUSYT=null;document.body.classList.remove('busy')}}
async function api(p,b){const q=!/^\/api\/(state|tt\/status)/.test(p)||!INTK0;if(q)busy(1);try{const r=await fetch(p,{method:b===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:b===undefined?undefined:JSON.stringify(b)});
 const j=await r.json();document.getElementById('conn').style.display='none';return j}
 catch(e){document.getElementById('conn').style.display='block';return {error:'接続が切れています'}}finally{if(q)busy(-1)}}
function mmss(s){const t=Math.round(s*10);return Math.floor(t/600)+':'+String(Math.floor(t/10)%60).padStart(2,'0')+'.'+(t%10)}
function esc(x){return String(x).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function dzset(k,v,ph,parts){const el=document.getElementById(k);if(!el)return;const names=(parts&&parts.length>1)?parts.map(x=>x.split('/').pop()):(v?[v.split('/').pop()]:[]);el.textContent=names.length?names.join('\n'):ph;el.style.whiteSpace='pre-line';el.title=(parts&&parts.length>1)?parts.join('\n'):(v||'');document.getElementById('dz_'+k).classList.toggle('has',!!v)}
const VEXT=/\.(mov|mp4|m4v|mxf|avi|mts|m2ts|mkv)$/i,AEXT=/\.(m4a|wav|aif|aiff|mp3|aac|flac|caf|wma)$/i;
function readAll(rd){return new Promise(res=>{const out=[];const step=()=>rd.readEntries(es=>{if(!es.length)return res(out);out.push(...es);step()},()=>res(out));step()})}
function fileOf(en){return new Promise(res=>en.file(f=>res(f),()=>res(null)))}
async function gather(dt){const files=[];const walk=async(en,depth)=>{if(!en)return;
  if(en.isFile){const f=await fileOf(en);if(f)files.push(f)}
  else if(en.isDirectory&&depth<2){for(const c of await readAll(en.createReader()))await walk(c,depth+1)}};
 const ents=[...dt.items].map(i=>i.webkitGetAsEntry?i.webkitGetAsEntry():null);
 if(ents.some(x=>x))for(const en of ents)await walk(en,0);else files.push(...dt.files);return files}
function newest(fs){return fs.slice().sort((a,b)=>b.lastModified-a.lastModified)[0]}
async function dropAll(dt){const msg=document.getElementById('dzmsg');msg.textContent='読み込み中…';
 const ents0=[...dt.items].map(i=>i.webkitGetAsEntry?i.webkitGetAsEntry():null).filter(x=>x);   // must be read before the first await
 const dirs=ents0.filter(e=>e.isDirectory).map(e=>e.name);
 const fs=await gather(dt);
 const vs=fs.filter(f=>VEXT.test(f.name)&&!f.name.startsWith('.')),as=fs.filter(f=>AEXT.test(f.name)&&!f.name.startsWith('.'));
 if(!vs.length&&!as.length){msg.textContent='動画も音声も見つかりませんでした';return}
 const body={};const miss=[];
 const find=async f=>{msg.textContent='場所を探しています… '+f.name;const r=await api('/api/locate',{name:f.name,size:f.size});return r.error?null:r.path};
 let done=false;
 if(dirs.length===1){                      // one folder: the app reads it by itself
  msg.textContent='フォルダの中を読んでいます… '+dirs[0];
  const r=await api('/api/scan_dir',{name:dirs[0],hints:fs.slice(0,30).map(f=>f.name)});
  if(!r.error){done=true;
   if(r.videos.length)body.video_parts=r.videos;else miss.push('動画');
   if(r.audios.length)body.audio_parts=r.audios;else miss.push('音声')}}
 if(!done){                                // files: oldest first
  const byTime=(a,b)=>a.lastModified-b.lastModified||a.name.localeCompare(b.name,'ja',{numeric:true});
  for(const [k,list] of [['video',vs],['audio',as]]){
   if(!list.length){miss.push(k==='video'?'動画':'音声');continue}
   const ps=[];for(const f of list.slice().sort(byTime)){const p=await find(f);if(p)ps.push(p)}
   if(ps.length===list.length)body[k+'_parts']=ps;else miss.push(k==='video'?'動画':'音声')}}
 if(Object.keys(body).length){msg.textContent='セットしています…（つなぐ場合は数十秒かかります）';body.fresh=true;const r=await api('/api/project',body);if(r.error){msg.textContent=r.error;return}}
 const nv=(body.video_parts||[]).length,na=(body.audio_parts||[]).length;
 msg.textContent=miss.length?(miss.join('と')+'が見つかりませんでした。下の枠をクリックして選んでください'):'セットしました（動画'+nv+'本・音声'+na+'本'+((nv>1||na>1)?'、撮影順につなぎます':'')+'）';refresh(true)}
const za=document.getElementById('dz_all');
za.ondragover=e=>{e.preventDefault();za.classList.add('on')};za.ondragleave=()=>za.classList.remove('on');
za.ondrop=e=>{e.preventDefault();za.classList.remove('on');dropAll(e.dataTransfer)};
['video','audio'].forEach(k=>{const z=document.getElementById('dz_'+k);
 z.onclick=()=>pick(k);
 z.ondragover=e=>{e.preventDefault();z.classList.add('on')};z.ondragleave=()=>z.classList.remove('on');
 z.ondrop=async e=>{e.preventDefault();z.classList.remove('on');e.stopPropagation();dropAll(e.dataTransfer)}});
document.addEventListener('dragover',e=>e.preventDefault());document.addEventListener('drop',e=>e.preventDefault());
let TTP=null;
function ttClose(){clearInterval(TTP);TTP=null;api('/api/tt/stop',{});const m=document.getElementById('ttm');if(m)m.remove()}
async function ttOpen(){ttClose();const m=document.createElement('div');m.id='ttm';m.style.cssText='position:fixed;inset:0;background:#000a;z-index:99;display:flex;align-items:center;justify-content:center';
 m.onclick=e=>{if(e.target===m)ttClose()};m.innerHTML='<div style="background:var(--panel);border:1px solid var(--line);border-radius:14px;width:600px;max-width:92vw;max-height:80vh;overflow:auto;padding:18px"><div style="display:flex;align-items:center"><h3 style="margin:0;flex:1;font-size:15px;color:var(--ink)">タイムツリーから台本を選択</h3><button onclick="ttClose()">✕</button></div><div id="ttb" style="margin-top:12px" class="small">準備中…</div></div>';
 document.body.appendChild(m);const r=await api('/api/tt/start',{span:3});ttRender(r);TTP=setInterval(async()=>{const x=await api('/api/tt/status',{});ttRender(x);if(x.state==='done'||x.state==='error')clearInterval(TTP)},1000)}
function ttRender(r){const b=document.getElementById('ttb');if(!b)return;const e=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
 if(r.state==='error'){b.innerHTML='<div class="bad">'+e(r.msg)+'</div>';return}
 if(r.state==='login'){b.innerHTML='<div>'+e(r.msg)+'</div>';return}
 TTI=(r.items||[]).filter(x=>(x.memo||'').trim().length>=10);
 const run=r.state!=='done';
 const st=run?'<div class="small" style="margin-bottom:8px">'+e(r.msg||'読み込み中…')+''+(TTI.length?'　見つかったものから選べます':'')+'</div>':'';
 if(!TTI.length&&!run){ttClose();return}
 if(!TTI.length){b.innerHTML=run?'<div>'+e(r.msg||'読み込み中…')+''+'</div>':'<div class="empty">メモのある予定が見つかりませんでした</div>';return}
 b.innerHTML=st+TTI.map((x,i)=>'<div class="card" style="padding:10px 14px;margin:0 0 8px;cursor:pointer" onclick="ttPick('+i+')"><div class="small">'+e(x.date)+'</div><div style="white-space:pre-wrap">'+e(x.memo.split('\n').filter(Boolean).slice(0,4).join('\n'))+'</div></div>').join('')}
let TTI=[];
async function ttPick(i){const x=TTI[i];if(!x)return;ttClose();await setf('script',x.memo)}
async function setf(k,v){await api('/api/project',{[k]:v});refresh(true)}
async function pick(kind){const r=await api('/api/pick',{kind});if(r.error){alert(r.error);return}if(r.cancelled)return;
 if(kind==='script'){await setf('script',r.text)}
 else{const key={video:'video',audio:'audio',config:'speakers_config',folder:'out_dir'}[kind];await setf(key,r.path)}}
async function doExport(){if(TE)await tsave(true);run('export')}
async function run(stage){if(TE)await tsave(true);const r=await api('/api/run',{stage});if(r.error){document.getElementById('label').textContent=''+r.error;alert(r.error)}refresh(true)}
function intake(on){document.body.classList.toggle('intake',!!on)}
async function newProj(){await api('/api/new',{});intake(true);refresh(true)}
document.getElementById('recent').onchange=async e=>{if(e.target.value){await api('/api/open_project',{name:e.target.value});refresh(true)}};
let PICK={},ALT={},RSIG='';
function stColor(s){return {OK:'#2bb673',CHECK:'#f0a23b',NG:'#ef5b5b'}[s]||'#888'}
function stName(s){return {OK:'成功',CHECK:'要確認',NG:'失敗候補'}[s]||''}
function syncPick(){const p=(S&&S.project)||{};(S.report?S.report.blocks:[]).forEach(b=>{if(PICK[b.id]===undefined)PICK[b.id]=(p.picks&&p.picks[b.id])||b.chosen;
 if(ALT[b.id]===undefined){const sel=p.alts_sel&&p.alts_sel[b.id];ALT[b.id]=sel?sel.slice():b.takes.map(t=>t.no)}})}
function pickTake(id,no){PICK[id]=no;renderBlocks()}
function toggleAlt(id,no){const a=ALT[id]=ALT[id]||[];const k=a.indexOf(no);k>=0?a.splice(k,1):a.push(no);renderBlocks()}
const ZN='０１２３４５６７８９';
function bnum(id){const bl=(S&&S.report&&S.report.blocks)||[];let f=bl.findIndex(b=>b.cut||(b.takes||[]).length);if(f<0)f=0;const i=bl.findIndex(b=>b.id===id);return i<f?0:i-f+1}
function bname(id){const n=bnum(id);return n?'ブロック'+String(n).replace(/\d/g,d=>ZN[d]):'タイトル'}
function skipped(id){return !!(S&&S.project&&(S.project.skip_blocks||[]).includes(id))}
async function toggleSkip(id){const cur=((S.project||{}).skip_blocks||[]).slice();const k=cur.indexOf(id);k>=0?cur.splice(k,1):cur.push(id);await api('/api/project',{skip_blocks:cur});S=await api('/api/state');renderBlocks();renderTelops()}
function renderBlocks(){if(!S||!S.report)return;const el=document.getElementById('blocks');
 el.innerHTML=S.report.blocks.map((b,bi)=>{const chosen=PICK[b.id]||b.chosen,cur=b.takes.find(t=>t.no===chosen);
  const warn=cur&&(cur.missing.length||cur.maybe.length)?'<div class="bwarn">'+esc(cur.missing.map(x=>'見つからない: '+x).concat(cur.maybe.map(x=>'不確か: '+x)).join(' / '))+'</div>':'';
  const unh=(b.unheard||[]).map(u=>'<div class="bunh"> 聞き取れず？ '+mmss(u[0])+'〜'+mmss(u[1])+'</div>').join('');
  const hue=[350,28,48,150,190,215,260,300,330][bi%9];
  const pills=b.takes.map(t=>{const on=t.no===chosen,c=stColor(t.status),alt=(ALT[b.id]||[]).includes(t.no)&&!on;
   return '<div class="tk'+(on?' on':'')+'" style="--c:'+c+'"><button class="tkb" onclick="pickTake(\''+b.id+'\','+t.no+')"><b>テイク'+t.no+'</b><span>'+mmss(t.start)+'</span><i>'+stName(t.status)+'</i></button>'
    +'<button class="tkp" title="聞く" onclick="takePlay('+t.start+','+t.end+')">▶</button>'
    +(on?'<span class="tkuse">使用</span>':'<button class="tka'+(alt?' yes':'')+'" onclick="toggleAlt(\''+b.id+'\','+t.no+')">'+(alt?'✓ 候補':'＋ 候補')+'</button>')+'</div>'}).join('');
  return '<div class="bc'+(skipped(b.id)?' skipped':'')+'" style="--h:'+hue+'"><div class="bh"><span class="bid">'+esc(bname(b.id))+'</span><span class="bcue">'+esc(b.cue)+'</span><button class="tkp" onclick="run(\'block:'+b.id+'\')">再解析</button><button class="tkp" onclick="playBlock(\''+b.id+'\')">▶</button><span class="bcut">'+(b.cut?mmss(b.cut[0])+' – '+mmss(b.cut[1]):'')+'</span><button class="bskip" onclick="toggleSkip(\''+b.id+'\')">'+(skipped(b.id)?'使用':'不使用')+'</button></div>'
   +warn+unh+(b.takes.length?'<div class="tks">'+pills+'</div>':'<div class="bwarn">テイクが見つかりません</div>')+'</div>'}).join('')
  +'<div style="padding:10px 0 4px"><button class="p" onclick="remake()">このテイクで解析</button></div>'}
function altsSel(){const o={};for(const id in ALT)o[id]=ALT[id];return o}
function picks(){const o={};(S.report?S.report.blocks:[]).forEach(b=>{if(PICK[b.id]&&PICK[b.id]!==b.chosen)o[b.id]=PICK[b.id]});return o}
async function remake(){await api('/api/project',{picks:picks(),alts_sel:altsSel()});run('analyze')}
let editing=false;
let INTK0=false;
let LV=null;async function refresh(force){const S0=await api('/api/state');if(S0.error)return;S=S0;if(LV===null)LV=S.app_ver;else if(S.app_ver&&S.app_ver!==LV){LV=S.app_ver;location.reload();return}const p=S.project||{};if(!INTK0){INTK0=true;if(!S.report&&!TE)intake(true)}
 const bad=S.doctor.filter(x=>!x.ok);
 if(S.media){if(MGEN===null)MGEN=S.media.gen;else if(S.media.gen!==MGEN){MGEN=S.media.gen;AUSET='';VKEY='';VSET='';AB=null;AUFALL=false;try{audSrc();vidSrc()}catch(e){}}}
 const mm=S.media&&(!S.media.audio||!S.media.video)?'<div class="warnbox"><b>外付けディスクが見えません</b></div>':'';
 document.getElementById('doctor').innerHTML=mm+(bad.length?'<div class="warnbox"><b>準備が必要です</b>'+bad.map(x=>'<div>'+esc(x.name)+' — '+esc(x.hint)+'</div>').join('')+'</div>':'');
 const rs=document.getElementById('recent');rs.innerHTML='<option value="">最近のプロジェクト…</option>'+S.recent.map(n=>'<option>'+esc(n)+'</option>').join('');
 const act=document.activeElement&&document.activeElement.id;
 {const ks=document.getElementById('kerning');if(ks&&document.activeElement!==ks){const m={optical:'オプティカル',mono:'和文等幅'}[p.kerning]||(['オプティカル','和文等幅'].includes(p.kerning)?p.kerning:'メトリクス');if(ks.value!==m){ks.value=m;const c=document.getElementById('cap');if(c)c.dataset.h='';const o=document.getElementById('vov');if(o)o.dataset.h=''}}}
 const set=(id,v,c)=>{const el=document.getElementById(id);if(!el||act===id)return;if(c)el.checked=!!v;else el.value=v||''};
 set('name',p.name);dzset('video',p.video,'未選択',p.video_parts);dzset('audio',p.audio,'未選択',p.audio_parts);set('script',p.script);set('cfg',p.speakers_config);set('out_dir',p.out_dir);
 const D=(v)=>v===undefined?true:v;set('subtitles',D(p.subtitles),1);set('diarize',D(p.diarize),1);set('swap',p.swap_speakers,1);set('dual_mono',D(p.dual_mono),1);set('voice_preset',D(p.voice_preset),1);document.getElementById('kerning').value=p.kerning||'メトリクス';
 const j=S.job;const run_=j.state==='running';
 document.getElementById('go').disabled=run_;document.getElementById('exp').disabled=run_||!S.report;
 const lg=j.log.join('\n');
 const stage=j.state==='done'?4:(run_?(/書き出し|同期・テイク/.test(lg)&&!/文字起こし（数分/.test(j.label)?2:1):0);
 ['s1','s2','s3'].forEach((id,i)=>{const e=document.getElementById(id);e.className='st'+(stage>i+1||j.state==='done'?' done':(stage===i+1?' on':''))});
 document.body.classList.toggle('busyjob',!!run_);document.getElementById('busytx').textContent=run_?'処理中':'読み込み中';
 document.getElementById('label').textContent=run_?('実行中… '+j.label):(j.state==='done'?'完了':(j.state==='error'?'失敗しました（ログを見てください）':''));
 const l=document.getElementById('log');if(j.log.length){l.style.display='block';l.textContent=lg.replace(/\bb(\d\d)(?![\d-])/g,(m,d)=>bname('b'+d));l.scrollTop=l.scrollHeight}
 document.getElementById('result').innerHTML=(j.state==='done'&&j.result)?'<div class="res"><b>書き出し完了</b><div class="small" style="word-break:break-all;margin:4px 0 8px">'+esc(j.result)+'</div><button id="rev">Finderで表示</button></div>':'';
 const rv=document.getElementById('rev');if(rv)rv.onclick=()=>api('/api/reveal',{path:j.result});
 const key=(p.name||'')+'|'+(j.tick||'')+'|'+(S.boot||'');if(key!==TKEY&&!run_){TKEY=key;loadTelops()}vidSrc();
 if(S.project&&S.report&&S.app_ver&&p.analyzed_ver!==S.app_ver&&!run_&&!AUTOAN&&!mm&&!bad.length&&p.video){AUTOAN=true;run('analyze')}
 document.getElementById('dock').style.display=S.report?'block':'none';document.querySelector('.main').classList.toggle('docked',!!S.report);if(S.report&&S.report.offset!==undefined&&!TE)OFF=S.report.offset;const rc=document.getElementById('reportCard');document.getElementById('empty').style.display=S.report?'none':'block';
 if(S.report&&!editing){rc.style.display='block';
  const sg=JSON.stringify(S.report),chg=sg!==RSIG;if(chg){RSIG=sg;PICK={};ALT={}}syncPick();if(chg||!document.getElementById('blocks').children.length)renderBlocks()}
}

const aud=new Audio();let MUX=false;
// The sound is decoded once and played from memory by the browser's audio clock: it starts and jumps exactly,
// and everything on screen (subtitles, waveform) follows that clock.  The video is only a loose follower.
let ABRATE=1,AB2=null,AB=null,ABPOS=0,ABS=[],ABN=[],ABPLAY=false,ABEND=0,ABLOAD='';
function abNow(){if(!ABPLAY||!AC)return ABPOS;const n=AC.currentTime;for(const s of ABS){if(n<s.c0+s.d/s.r)return s.a0+Math.max(n-s.c0,0)*s.r}const l=ABS[ABS.length-1];return l?l.a0+l.d:ABPOS}
function abStop(keep){if(ABPLAY&&keep)ABPOS=abNow();ABPLAY=false;for(const s of ABN){try{s.onended=null;s.stop()}catch(e){}}ABN=[];ABS=[]}
function abStart(pos){abStop(false);ensureAC();if(!AC||!AB)return false;if(AC.state==='closed'){AC=null;FXN=null;fxSrc=null;ensureAC()}if(AC.state!=='running')AC.resume();
 let list=[];if(stopAt){list=[{a0:pos,a1:Math.min(stopAt,AB.duration)}]}
 else{const sg=segs();if(sg.length){for(const g of sg){if(g.a1>pos+0.02)list.push({a0:Math.max(g.a0,pos),a1:Math.min(g.a1,AB.duration)})}}else list=[{a0:pos,a1:AB.duration}]}
 list=list.filter(x=>x.a1-x.a0>0.01);if(!list.length){ABPOS=pos;return false}
 let c=AC.currentTime+0.04;for(const x of list){const s=AC.createBufferSource();const u2=ABST[ABRATE];s.buffer=u2||AB;s.playbackRate.value=u2?1:ABRATE;if(fxSrc)fxSrc(s);else s.connect(AC.destination);if(u2){s.start(c,x.a0/ABRATE,Math.max((x.a1-x.a0)/ABRATE,0.005))}else s.start(c,x.a0,x.a1-x.a0);ABN.push(s);ABS.push({c0:c,a0:x.a0,d:x.a1-x.a0,r:ABRATE});c+=(x.a1-x.a0)/ABRATE}
 ABEND=c;ABPLAY=true;ABPOS=pos;return true}
const au={get currentTime(){return AB?abNow():aud.currentTime},set currentTime(x){if(AB){ABPOS=x;if(ABPLAY){if(!abStart(x))pauseNow()}}else aud.currentTime=x},
 get paused(){return AB?!ABPLAY:aud.paused},play(){if(AB){if(!ABPLAY)abStart(ABPOS);return Promise.resolve()}return aud.play()},pause(){if(AB)abStop(true);aud.pause()},
 get error(){return aud.error},get readyState(){return AB?4:aud.readyState},set src(v){aud.src=v},set onerror(f){aud.onerror=f},
 addEventListener(n,f){aud.addEventListener(n,f)}};
function setRate(r){if(r===ABRATE)return;const v=vid();if(AB&&ABPLAY){const pos=abNow();ABRATE=r;abStart(pos)}else ABRATE=r;try{aud.playbackRate=r;aud.preservesPitch=true;v.playbackRate=r}catch(e){}}
let LFAST=0;
document.addEventListener('keydown',e=>{if((e.key!=='l'&&e.key!=='L')||e.metaKey||e.ctrlKey||e.altKey||e.isComposing)return;const tg=(e.target&&e.target.tagName)||'';if(tg==='TEXTAREA'||tg==='INPUT'||tg==='SELECT')return;e.preventDefault();if(e.repeat)return;const was=LFAST?LFAST:(au.paused?0:1);const nx=was<2?2:Math.min(was*2,8);LFAST=nx;if(au.paused)playNow();setRate(nx);const m=document.getElementById('tmsg');if(m)m.textContent=nx+'倍速'});
async function stretchR(buf,R){const sr=buf.sampleRate,nc=buf.numberOfChannels,N=buf.length,F=Math.round(sr*0.04)&~7,H=F/2,HI=H*R,D=8,tol=Math.round(sr*0.01);
 const chs=[];for(let c=0;c<nc;c++)chs.push(buf.getChannelData(c));const m=new Float32Array(Math.floor(N/D));
 for(let i=0;i<m.length;i++){let a=0;for(let c=0;c<nc;c++)a+=chs[c][i*D+3];m[i]=a/nc}
 const nf=Math.floor((N-F-tol*2)/HI);if(nf<4)return null;const outLen=nf*H+F;const out=chs.map(()=>new Float32Array(outLen));
 const win=new Float32Array(F);for(let i=0;i<F;i++)win[i]=0.5-0.5*Math.cos(2*Math.PI*(i+0.5)/F);
 const put=(k,pp)=>{const o=k*H;for(let c=0;c<nc;c++){const x=chs[c],y=out[c];for(let i=0;i<F;i++)y[o+i]+=x[pp+i]*win[i]}};
 let p=tol;put(0,p);const T=tol/D|0,W=F/D;
 for(let k=1;k<nf;k++){const q=((tol+k*HI)/D)|0,nat=((p+H)/D)|0;let bl=0,bv=-1e30;
  for(let L=-T;L<=T;L++){const a=q+L;if(a<0||a+W>=m.length)continue;let sum=0;for(let j=0;j<W;j+=2)sum+=m[a+j]*m[nat+j];if(sum>bv){bv=sum;bl=L}}
  let pp=(q+bl)*D;if(pp<0)pp=0;if(pp+F>N)pp=N-F;put(k,pp);p=pp;if(k%150===0)await new Promise(r=>setTimeout(r,0))}
 ensureAC();if(!AC)return null;const ab=AC.createBuffer(nc,outLen,sr);for(let c=0;c<nc;c++)ab.copyToChannel(out[c],c);return ab}
const stretch2=b=>stretchR(b,2);let ABST={};
async function loadAB(k){ABLOAD=k;AB=null;try{ensureAC();if(!AC)return;const r=await fetch('/media/audio?'+Date.now());const sz=+r.headers.get('content-length')||0;if(sz>80e6)return;const buf=await AC.decodeAudioData(await r.arrayBuffer());if(ABLOAD===k){AB=buf;AB2=null;ABST={};(async()=>{for(const R of [2,4,8]){try{const b=await stretchR(buf,R);if(ABLOAD!==k)return;if(b){ABST[R]=b;if(R===2)AB2=b}}catch(e){}}})()}}catch(e){AB=null}}
let stopAt=0,TE=null,TN=['チ','ホ','笑'],TKEY='',OFF=0,VSET='',ACT=null,saveT=null,EDIT=false,STY=null,FR=[1920,1080],UNDO=[];
const vid=()=>document.getElementById('vid');
function tcf(t){const f=Math.round(t*30),ff=f%30,sec=Math.floor(f/30);return Math.floor(sec/60)+':'+String(sec%60).padStart(2,'0')+':'+String(ff).padStart(2,'0')}
function fmt(t){const m=Math.floor(t/60);return m+':'+(t-m*60).toFixed(2).padStart(5,'0')}
let AUSET='';let VSY=0;let MGEN=null;
function audSrc(){if(!S||!S.project||!S.project.audio)return;const k=S.project.name+'|'+S.project.audio;if(AUSET===k)return;AUSET=k;AUFALL=false;au.onerror=()=>{audFallback('音声の形式')};au.src='/media/audio?'+Date.now();loadAB(k)}
function vidSrc(){audSrc();if(!S||!S.project||!S.project.video)return;const v=vid(),m=document.getElementById('vmsg');if(!v)return;const key=(S.project.name||'')+'|'+S.project.video;if(VKEY!==key){VKEY=key;if(MUX){MUX=false;v.muted=true;v.removeAttribute('src');VSET=''}}
 if(false&&S.av==='ready'){if(VSET!==key+'a'){const t0=au.currentTime,wp=!au.paused;aud.pause();MUX=true;VSET=key+'a';v.muted=false;v.playbackRate=1;v.src='/media/av?'+Date.now();m.textContent='';v.addEventListener('loadedmetadata',()=>{try{v.currentTime=Math.max((isFinite(t0)?t0:0)+OFF,0)}catch(e){}if(wp)v.play().catch(()=>{});if(AC&&!FXV){FXV=true;try{fxSrc(v)}catch(e){}}},{once:true})}}
 else if(S.proxy==='ready'&&!MUX){if(VSET!==key+'p'){VSET=key+'p';v.src='/media/video?'+Date.now();m.textContent=''}}
 else if(S.proxy==='making'){m.textContent='軽い動画を作っています…（音声だけは聞けます）'}
 else if(S.proxy==='error'){m.textContent='動画のプレビューを作れませんでした（音声だけ）'}}
let VKEY='',JUMP=false,FXV=false,fxSrc=null;
function seek(t){t=Math.max(t,0);au.currentTime=t;WC=t;const v=vid();try{const w=Math.max(t+OFF,0);if(Math.abs(v.currentTime-w)>0.04)v.currentTime=w}catch(e){}VSY=Date.now();tick(true)}
let AC=null,FXN=null;
function ensureAC(){if(AC)return;try{AC=new (window.AudioContext||window.webkitAudioContext)();const B=(t,f,g,q)=>{const n=AC.createBiquadFilter();n.type=t;n.frequency.value=f;if(g!==undefined)n.gain.value=g;if(q)n.Q.value=q;return n};
 const hp=B('highpass',85),pr=B('peaking',3200,3,0.8),lp=B('lowpass',14000),pre=AC.createGain();
 const comp=AC.createDynamicsCompressor();comp.threshold.value=-30;comp.knee.value=12;comp.ratio.value=5;comp.attack.value=0.004;comp.release.value=0.18;
 const mk=AC.createGain();mk.gain.value=1.6;const lim=AC.createDynamicsCompressor();lim.threshold.value=-3;lim.knee.value=0;lim.ratio.value=20;lim.attack.value=0.001;lim.release.value=0.08;
 const wet=AC.createGain(),dry=AC.createGain();
 hp.connect(pr);pr.connect(lp);lp.connect(pre);pre.connect(comp);comp.connect(mk);mk.connect(lim);lim.connect(wet);wet.connect(AC.destination);dry.connect(AC.destination);fxSrc=n=>{n.connect(hp);n.connect(dry)};
 FXN={wet,dry,pre};AC.onstatechange=()=>cl('AC '+AC.state);fxToggle();fxLevel()}catch(e){AC=null}}
function fxInit(){ensureAC()}
function fxToggle(){const on=document.getElementById('fx').checked;if(FXN){FXN.wet.gain.value=on?1:0;FXN.dry.gain.value=on?0:1}}
function fxLevel(){if(!FXN||!PK)return;const v=PK.peaks.filter(x=>x>0.25).map(x=>x*x).sort((a,b)=>a-b);if(!v.length)return;const med=v[Math.floor(v.length*0.6)];FXN.pre.gain.value=Math.min(Math.max(0.3/med,1),40)}
let AUFALL=false;
async function audFallback(why){if(AUFALL)return false;AUFALL=true;const m=document.getElementById('vmsg');if(m)m.textContent='音声を再生用に変換しています…（'+why+'）';const r=await api('/api/need_audio',{});if(r.error){if(m)m.textContent=r.error;return false}au.src='/media/audio2?'+Date.now();if(m)m.textContent='';return true}
let GAPOK=false;
function playNow(){if(AC&&au.paused&&AB&&Date.now()-IDLE0>3000){cl('停止後の再開: 音の出口を作り直し');resetAC()}if(ACFAIL&&ABKEEP&&AC&&AC.state==='running'&&au.paused){AB=ABKEEP;ACFAIL=false;cl('音声をAC方式に復帰')}if(!stopAt){if(au.paused&&TLMX!==null&&!TLDRAG){const tt=curT();if(Math.abs(tt-au.currentTime)>0.01)seek(tt)}const sg=segs(),t0=au.currentTime;GAPOK=!!sg.length&&!sg.some(g=>t0>=g.a0-0.001&&t0<g.a1-0.05)}fxInit();if(AC&&AC.state!=='running'){try{AC.resume()}catch(e){}if(AC.state==='interrupted'||AC.state==='closed'){resetAC()}}au.play().catch(async e=>{if(e&&(e.name==='NotSupportedError'||(au.error&&au.error.code>=3))&&await audFallback(e.name)){au.play().catch(()=>{});return}const m=document.getElementById('vmsg');if(m)m.textContent='音声を再生できません: '+(e&&e.name)+' / '+(e&&e.message)+' / エラー番号 '+(au.error?au.error.code:'なし')+' / 読み込み状態 '+au.readyState});try{vid().play().catch(()=>{})}catch(e){}document.getElementById('pp').textContent='❚❚ 停止';acCheck()}
let IDLE0=0;function pauseNow(){IDLE0=Date.now();if(typeof LFAST!=='undefined'&&LFAST){LFAST=0;setRate(1)}au.pause();try{vid().pause()}catch(e){}stopAt=0;document.getElementById('pp').textContent='▶ 再生'}
function togglePlay(){(au.paused&&vid().paused)?playNow():pauseNow()}
const fr30=t=>Math.round(t*30)/30,r3=v=>Math.round(v*1000)/1000;
function curT(){try{if(TLMX!==null&&!TLDRAG&&typeof tlT==='function'){const tt=tlT(TLMX,tlLayout());if(tt!==null)return tt}}catch(e){}return au.currentTime}
function playRange(a,b){pauseNow();JUMP=false;seek(Math.max(a-0.05,0));stopAt=b+0.1;playNow()}
function takePlay(a,b){let s=a-0.05;try{const o=tlOnsets().find(x=>x>=a-0.1&&x<b);if(o!==undefined)s=Math.max(o-0.12,a-0.05)}catch(e){}playRange(s+0.05,b)}
function playBlock(bid){const b=S.report.blocks.find(x=>x.id===bid);if(!b||!b.takes.length)return;takePlay(Math.min(...b.takes.map(t=>t.start)),Math.max(...b.takes.map(t=>t.end)))}
function resetAC(){try{const pos=au.currentTime,was=!au.paused;abStop(false);try{AC&&AC.close()}catch(e){}AC=null;FXN=null;fxSrc=null;ensureAC();if(AC&&AC.state==='suspended')AC.resume();if(was){ABPOS=pos;if(AB)abStart(pos)}}catch(e){}}
let AFIX=0,ACFAIL=false,ABKEEP=null,CLN=0;
function cl(m){try{if(CLN++<500)api('/api/cl',{m:m})}catch(e){}}
function toAud(){if(!AB)return false;const pos=au.currentTime,was=!au.paused;abStop(false);ABKEEP=AB;AB=null;ACFAIL=true;cl('音声をプレーヤー方式に切替 pos='+pos.toFixed(2));try{aud.currentTime=pos;aud.playbackRate=ABRATE;if(was)aud.play().catch(()=>{})}catch(e){}return true}
function acCheck(){if(au.paused||!AB||!AC)return;const c0=AC.currentTime;setTimeout(()=>{try{if(au.paused||!AB||!AC)return;if(AC.currentTime-c0>=0.15){AFIX=0;return}AFIX++;cl('音が進まない state='+AC.state+' 回'+AFIX);if(AFIX===1){AC.resume().catch(()=>{});acCheck()}else if(AFIX===2){resetAC();acCheck()}else{AFIX=0;toAud()}}catch(e){}},500)}
document.addEventListener('pointerdown',()=>{try{if(AC&&AC.state!=='running')AC.resume()}catch(e){}},true);document.addEventListener('keydown',()=>{try{if(AC&&AC.state!=='running')AC.resume()}catch(e){}},true);
let LASTT=-1,LASTM=0,STALL=0;
try{if(navigator.mediaDevices)navigator.mediaDevices.addEventListener('devicechange',()=>{cl('出力機器が変わった');if(!au.paused&&AB)resetAC()})}catch(e){}
setInterval(()=>{try{cl('♥ AC='+(AC?AC.state+' '+AC.currentTime.toFixed(1):'なし')+' 再生='+!au.paused+' 方式='+(AB?'AC':'プレーヤー')+' 隠れ='+document.hidden)}catch(e){}},60000);
window.addEventListener('error',e=>cl('エラー '+(e.message||'')+' '+(e.lineno||'')));aud.addEventListener('error',()=>cl('プレーヤー異常 '+(aud.error?aud.error.code:'')));
const acBack=()=>{try{if(!AC)return;AC.resume();setTimeout(()=>{try{if(AC&&AC.state!=='running')resetAC();else if(AC&&!au.paused)acCheck()}catch(e){}},300)}catch(e){}};
document.addEventListener('visibilitychange',()=>{if(!document.hidden)acBack()});window.addEventListener('focus',acBack);window.addEventListener('pageshow',acBack);
setInterval(()=>{try{if(AC&&AC.state==='suspended'&&!au.paused)AC.resume();if(au.paused){LASTT=-1;return}const t=au.currentTime,n=Date.now();if(Math.abs(t-LASTT)>0.001){LASTT=t;LASTM=n;STALL=0;return}if(n-LASTM>900&&!au.seeking){LASTM=n;STALL++;if(STALL>=2&&AB){STALL=0;acCheck();return}if(AC&&AC.state!=='running')AC.resume();au.play().catch(()=>{});try{vid().play().catch(()=>{})}catch(e){}}}catch(e){}},400);
function pend(bid,i){const b=TE[bid],ps=b.pieces;if(ps[i].speaker<0||ps[i].ov)return Math.min(ps[i].end,(b.c1!==undefined?b.c1:b.a1));for(let j=i+1;j<ps.length;j++)if(ps[j].speaker>=0&&!ps[j].ov&&!ps[j].deleted)return ps[j].start;return Math.max((b.c1!==undefined?b.c1:b.a1),ps[i].end||0)}
function isDel(bid,i){return !!TE[bid].pieces[i].deleted}
function findAct(t){if(!TE)return null;let best=null;for(const bid in TE){const ps=TE[bid].pieces;for(let i=0;i<ps.length;i++){if(ps[i].speaker<0||ps[i].ov)continue;if(t>=ps[i].start-0.001&&t<pend(bid,i)){best=[bid,i]}}}return best}
function tick(force){if(JUMP&&!force)return;const t=au.currentTime;if(MUX){const vv=vid();if(vv.playbackRate!==1)vv.playbackRate=1}document.getElementById('tcur').textContent=fmt(t)+(TE?'　完成 '+fmt(cutPos(t))+' / '+fmt(cutTotal()):'');
 if(!au.paused){const v=vid();if(v&&v.readyState>0&&!v.seeking){const dd=(t+OFF)-v.currentTime;if(Math.abs(dd)>0.3){if(Date.now()-VSY>250){VSY=Date.now();v.currentTime=Math.max(t+OFF,0)}}else{const r=Math.abs(dd)<0.06?1:1+Math.max(-0.06,Math.min(0.06,dd*0.4));if(Math.abs(v.playbackRate-r)>0.02)v.playbackRate=r}}if(stopAt&&t>=stopAt)pauseNow();else if(!stopAt){const d=findAct(t);if(d&&isDel(d[0],d[1])){seek(pend(d[0],d[1])+0.01)}}}
 const ac=findAct(t);const key=ac?ac[0]+'_'+ac[1]:'';
 if(force||key!==ACT){ACT=key;document.querySelectorAll('.trow.act').forEach(e=>e.classList.remove('act'));
  const ov=document.getElementById('vov');
  if(ac){const row=document.getElementById('row_'+key);if(row){row.classList.add('act');if(!(document.activeElement&&document.activeElement.tagName==='TEXTAREA')){row.scrollIntoView({block:'center',behavior:'smooth'})}}}
  }chipLight(t,key);overlay(t)}
function cssColor(c,def){const q=(c||'').split(' ').map(Number);return q.length>=3&&!q.some(isNaN)?'rgb('+Math.round(q[0]*255)+','+Math.round(q[1]*255)+','+Math.round(q[2]*255)+')':def}
function ovItem(txt,st,kind,sp){const w=document.getElementById('vov').clientWidth,h=document.getElementById('vov').clientHeight;if(!w||!h)return '';
 const fs=(st?st.size:150)*h/FR[1];
 let x=0,y=-(kind==='note'?450:(kind==='laugh'?-607:-350));
 if(st&&st.pos){const q=st.pos.split(' ').map(Number);x=q[0];y=-q[1]}
 const px=w/2+x*w/FR[0],py=kind==='speaker'?h*0.78:h/2+y*h/FR[1];
 if(kind==='note')return '<div style="position:absolute;left:'+px+'px;top:'+py+'px;transform:translate(-50%,-50%);font-size:'+fs+'px;line-height:1.05;font-weight:900;color:#fff;-webkit-text-stroke:'+(fs*0.14)+'px #000;paint-order:stroke fill;text-align:center;white-space:pre;font-family:\'マティスEB(偽)\',\'Hiragino Sans\'">'+esc(txt)+'</div>';
 return '<div style="position:absolute;left:'+px+'px;top:'+py+'px;transform:translate(-50%,-50%)">'+capSvg(txt,sp,fs/44*0.9)+'</div>'}
function hasAlt(p){return p.alt!==undefined&&p.speaker>=0&&String(p.alt).replace(/\s/g,'')!==String(p.text||'').replace(/\s/g,'')}
let CAPED=false;
function capTarget(){if(!TE)return null;const ac=findAct(au.currentTime);if(ac)return TE[ac[0]].pieces[ac[1]];return tlSel()}
function capEdit(){if(CAPED||!TE)return;const p=capTarget();if(!p||p.speaker<0)return;pauseNow();CAPED=true;const el=document.getElementById('cap');el.innerHTML='';const ta=document.createElement('textarea');ta.id='capte';ta.value=p.text||'';const fit_=()=>{ta.rows=Math.max(2,ta.value.split('\n').length)};fit_();let sn=false;
 ta.oninput=()=>{if(!sn){snap();sn=true}p.text=ta.value;p.m=1;fit_();dirty();tlDraw()};
 ta.onblur=()=>{CAPED=false;el.dataset.h='';renderTelops();caption(au.currentTime)};
 ta.onkeydown=e=>{e.stopPropagation();if(e.key==='Escape'||((e.metaKey||e.ctrlKey)&&e.key==='Enter')){e.preventDefault();ta.blur()}};
 el.appendChild(ta);ta.focus();ta.setSelectionRange(ta.value.length,ta.value.length)}
function swapSrc(p,v){if(!p||p.alt===undefined||(p.src||'s')===v)return;snap();const t=p.text;p.text=p.alt;p.alt=t;p.src=v;p.m=1;dirty()}
async function setKern(v){await setf('kerning',v);const el=document.getElementById('cap');if(el)el.dataset.h='';const o=document.getElementById('vov');if(o)o.dataset.h='';caption(au.currentTime);overlay(au.currentTime)}
function capSrc(v){swapSrc(capTarget(),v);const el=document.getElementById('cap');el.dataset.h='';renderTelops();tlDraw();caption(au.currentTime)}
function rowSrc(bid,i,v){swapSrc(TE[bid].pieces[i],v);renderTelops();tlDraw()}
function srcSel(bid,i,p){return ''}
function srcSel_old(bid,i,p){if(!hasAlt(p))return '';const v=p.src||'s';return '<select class="srcsel" onchange="rowSrc(\''+bid+'\','+i+',this.value)"><option value="s"'+(v==='s'?' selected':'')+'>台本</option><option value="h"'+(v==='h'?' selected':'')+'>聞き取り</option></select>'}
let KCV=null;const KFONT="'CHPOP','LightNovelPopV2','ラノベPOP','Hiragino Maru Gothic ProN'";
function kernMode(){const k=(S&&S.project&&S.project.kerning)||'';return {optical:'オプティカル',mono:'和文等幅'}[k]||(['オプティカル','和文等幅'].includes(k)?k:'メトリクス')}
function kernPos(line,fs,mode){/* x of every letter (px) and the line's width, the way the export sets the kerning */
 if(!KCV)KCV=document.createElement('canvas').getContext('2d');const c=KCV;c.font='400 '+fs+'px '+KFONT;const L=[...line],n=L.length,base=-0.095,adv=[],il=[],ir=[];
 for(const ch of L){const m=c.measureText(ch);adv.push(m.width/fs);il.push(-m.actualBoundingBoxLeft/fs);ir.push(m.actualBoundingBoxRight/fs)}
 const k=new Array(n).fill(base);
 if(mode==='オプティカル'&&n>1){const g=[];for(let q=0;q<n-1;q++)g.push((adv[q]-ir[q])+il[q+1]);const ref=[...g].sort((p,q)=>p-q)[g.length>>1];for(let q=0;q<n-1;q++)k[q]=base+Math.max('、。，．'.includes(L[q])?-0.42:-0.075,Math.min(0.075,(ref-g[q])*0.8))}
 else if(mode==='和文等幅'){for(let q=0;q<n;q++)k[q]=base+Math.max(0,1-adv[q])}
 const xs=[];let x=0;for(let q=0;q<n;q++){xs.push(x);x+=(adv[q]+(q<n-1?k[q]:0))*fs}return {xs,w:x}}
function capSvg(txt,sp,u){const C=sp===1?['#2f6bff','#0b2a8f']:['#ff2d3d','#7d0014'];const L=String(txt).split('\n'),fs=44,lh=fs*1.1,md=kernMode();const KP=L.map(x=>kernPos(x,fs,md));const W=Math.max(40,Math.max(...KP.map(k=>k.w))+24),H=L.length*lh+34;
 const tx=(att,dx,dy)=>'<text x="0" y="0" text-anchor="start" font-family="'+KFONT.replace(/'/g,"&quot;")+'" font-size="'+fs+'" font-weight="400" stroke-linejoin="round" stroke-linecap="round" '+att+'>'+L.map((x,k)=>'<tspan x="'+KP[k].xs.map(v=>(W-KP[k].w)/2+v+dx).join(' ')+'" y="'+(fs+14+k*lh+dy)+'">'+esc(x)+'</tspan>').join('')+'</text>';
 return '<svg viewBox="0 0 '+W+' '+H+'" style="height:'+Math.round(H*u)+'px;width:auto;overflow:visible;display:block">'
  +tx('fill="'+C[0]+'" stroke="'+C[0]+'" stroke-width="8"',3,4)+tx('fill="#fff" stroke="#fff" stroke-width="6"',0,0)+tx('fill="#000" stroke="'+C[1]+'" stroke-width="1.4" paint-order="stroke"',0,0)+'</svg>'}
function capItem(txt,st,kind,sp){if(kind==='note'){return '<div class="capi" style="font-family:\'マティスEB(偽)\',\'Hiragino Sans\';color:#fff;-webkit-text-stroke:6px #000;paint-order:stroke fill">'+esc(txt)+'</div>'}
 return '<div class="capi" style="max-width:100%;display:flex;justify-content:center">'+capSvg(txt,sp,0.62)+'</div>'}
function caption(t){const el=document.getElementById('cap');if(!el||CAPED)return;if(!TE||!STY){el.innerHTML='';return}let h='';
 const ac=findAct(t);if(ac&&!isDel(ac[0],ac[1])){const p=TE[ac[0]].pieces[ac[1]];h+=capItem(p.text,STY.speakers[Math.min(Math.max(p.speaker,0),STY.speakers.length-1)],'speaker',p.speaker)}
 for(const bid in TE){const b=TE[bid];b.pieces.forEach((p,i)=>{if(!p.deleted&&p.speaker<0&&t>=p.start&&t<pend(bid,i))h+=capItem(p.text,STY.laugh,'laugh',-1);if(!p.deleted&&p.ov&&p.speaker>=0&&t>=p.start&&t<pend(bid,i))h+=capItem(p.text,STY.speakers[Math.min(p.speaker,STY.speakers.length-1)],'speaker',p.speaker)});
  (b.notes||[]).forEach(n=>{if(!n.deleted&&t>=n.start&&t<n.end)h+=capItem('※'+n.text,STY.note,'note',0)})}
 const sl=document.getElementById('capsrc');if(sl){const q=capTarget();if(q&&hasAlt(q)){sl.style.display='';if(document.activeElement!==sl)sl.value=q.src||'s'}else sl.style.display='none'}
 if(el.dataset.h!==h){el.dataset.h=h;el.innerHTML=h||'<div class="capi" style="opacity:.25;-webkit-text-stroke:0;color:#aaa">テロップなし</div>'}}
let HASV=false;function overlay(t){caption(t);{const dk=document.getElementById('dock'),vv=vid(),hv=!!(vv&&vv.videoWidth>0);if(dk&&hv!==HASV){HASV=hv;dk.classList.toggle('hasv',hv);FITK=''}}const ov=document.getElementById('vov');const v=vid();const vw=document.getElementById('vwrap');if(vw&&FR&&FR[1])vw.style.setProperty('--ar',(FR[0]/FR[1]).toFixed(4));if(!TE||!STY){ov.innerHTML='';return}let h='';
 const ac=findAct(t);if(ac&&!isDel(ac[0],ac[1])){const p=TE[ac[0]].pieces[ac[1]];h+=ovItem(p.text,STY.speakers[Math.min(Math.max(p.speaker,0),STY.speakers.length-1)],'speaker',p.speaker)}
 for(const bid in TE){const b=TE[bid];b.pieces.forEach((p,i)=>{if(!p.deleted&&p.speaker<0&&t>=p.start&&t<pend(bid,i))h+=ovItem(p.text,STY.laugh,'laugh',-1);if(!p.deleted&&p.ov&&p.speaker>=0&&t>=p.start&&t<pend(bid,i))h+=ovItem(p.text,STY.speakers[Math.min(p.speaker,STY.speakers.length-1)],'speaker',p.speaker)});
  (b.notes||[]).forEach(n=>{if(!n.deleted&&t>=n.start&&t<n.end){const st=STY.note?Object.assign({},STY.note):null;if(st){st.pos=n.with_telop?'0 -450':st.pos}h+=ovItem('※'+n.text,st,'note',0)}})}
 const sig=h+'|'+ov.clientWidth+'x'+ov.clientHeight;if(ov.dataset.h!==sig){ov.dataset.h=sig;ov.innerHTML=h}}
function segs(){if(!TE)return [];const o=[];for(const bid of Object.keys(TE)){if(skipped(bid))continue;const b=TE[bid];if(b.a0===undefined||b.a1===undefined)continue;const ex=b.pieces.filter(p=>!p.deleted&&!p.cut&&p.speaker>=0);o.push({bid,a0:b.c0!==undefined?b.c0:Math.min(b.a0,...ex.map(p=>p.start)),a1:b.c1!==undefined?b.c1:Math.max(b.a1,...ex.map(p=>p.end||0))})}return o.sort((x,y)=>x.a0-y.a0)}
function cutPos(t){let s=0;for(const g of segs()){if(t<g.a0)return s;if(t<g.a1)return s+t-g.a0;s+=g.a1-g.a0}return s}
function cutTotal(){return segs().reduce((a,g)=>a+g.a1-g.a0,0)}
function skipGaps(){if(AB){if(ABPLAY&&AC&&AC.currentTime>=ABEND){const e=ABS.length?ABS[ABS.length-1].a0+ABS[ABS.length-1].d:ABPOS;pauseNow();ABPOS=Math.max(e-0.05,0)}return}
 if(au.paused||stopAt||D2DRAG)return;const t=au.currentTime,sg=segs();if(!sg.length)return;
 const cur=sg.find(g=>t>=g.a0-0.001&&t<g.a1);
 if(cur)GAPOK=false;
 if(!cur&&GAPOK){const nx0=sg.find(g=>g.a0>t);if(!nx0)return}
 else if(!cur){const nx=sg.find(g=>g.a0>t);if(nx)seek(nx.a0);else pauseNow()}
 else if(t>=cur.a1-0.025){const nx=sg.find(g=>g.a0>=cur.a1-0.001);if(nx)seek(nx.a0);else{pauseNow();seek(cur.a1-0.05)}}}
setInterval(skipGaps,15);
setInterval(()=>tick(false),80);
(function raf(){if(!au.paused)tick(false);requestAnimationFrame(raf)})();
let PK=null,PKD=null,WC=0;const WIN=8;
async function loadPeaks(){const r=await api('/api/peaks');PK=r.peaks&&r.peaks.length?r:null;try{const q=await api('/api/peaks?raw=1');PKD=q.peaks&&q.peaks.length?q:null}catch(e){PKD=null}TLVS=null;VMK=null;HC={};TLONS=null;fxLevel();drawWave();if(TE)renderTelops()}
function boundaries(){const o=[];if(!TE)return o;for(const bid in TE)TE[bid].pieces.forEach((p,i)=>{if(p.speaker>=0)o.push({bid,i,t:p.start,text:p.text.replace(/\n/g,'')})});return o}
function drawWave(){const c=document.getElementById('wave');if(!c||!TE)return;const W=c.width,H=c.height,g=c.getContext('2d');g.clearRect(0,0,W,H);
 if(!au.paused)WC=au.currentTime;const t0=WC-WIN/2,px=W/WIN;g.fillStyle='#15161a';g.fillRect(0,0,W,H);
 if(PK){const r=PK.rate;g.fillStyle='#5b6a8c';for(let x=0;x<W;x+=2){const t=t0+x/px,k=Math.floor(t*r);if(k<0||k>=PK.peaks.length)continue;const v=PK.peaks[k]*H*0.44;g.fillRect(x,H*0.55-v,1.5,v*2)}}
 g.font='20px sans-serif';boundaries().forEach(b=>{const x=(b.t-t0)*px;if(x<-300||x>W+10)return;g.strokeStyle='#ff5a4f';g.lineWidth=3;g.beginPath();g.moveTo(x,0);g.lineTo(x,H);g.stroke();g.fillStyle='#ffd7d3';g.fillText(b.text.slice(0,8),x+6,26)});
 g.strokeStyle='#fff';g.lineWidth=2;const cx=(au.currentTime-t0)*px;g.beginPath();g.moveTo(cx,0);g.lineTo(cx,H);g.stroke()}
let DSEL=null,D2A=null,W2=2.4,D2DRAG=null;
function dp(){if(!DSEL||!TE||!TE[DSEL[0]])return null;return TE[DSEL[0]].pieces[DSEL[1]]||null}
function d2html(){return '<div class="d2row" id="d2row"><canvas id="wave2" width="1400" height="300"></canvas><div class="vctl"><button class="nb" onclick="addHere()">＋ここにテロップを追加</button> <button class="nb" onclick="DSEL=null;renderTelops()">✕</button><span id="d2info" class="small"></span></div></div>'}
function drawDetail(){const p=dp();const c=document.getElementById('wave2');if(!c||!p)return;
 {const rc=c.getBoundingClientRect(),dr=window.devicePixelRatio||1;if(rc.width>20&&rc.height>20){const w=Math.round(rc.width*dr),h=Math.round(rc.height*dr);if(c.width!==w||c.height!==h){c.width=w;c.height=h}}}
 const W=c.width,H=c.height,g=c.getContext('2d'),px=W/W2,KK=W/Math.max(c.getBoundingClientRect().width,1);if(D2A===null)D2A=p.start;const t0=D2A-W2*0.3;
 g.fillStyle='#15161a';g.fillRect(0,0,W,H);
 g.strokeStyle='#2d3345';g.lineWidth=1;const f0=Math.ceil((t0+OFF)*30),f1=Math.floor((t0+W2+OFF)*30);for(let f=f0;f<=f1;f++){const x=(f/30-OFF-t0)*px;g.beginPath();g.moveTo(x,0);g.lineTo(x,f%30===0?H:H*0.1);g.stroke()}
 if(PK){const r=PK.rate;g.fillStyle='#6f84b8';for(let x=0;x<W;x+=2){const t=t0+x/px,k=Math.floor(t*r);if(k<0||k>=PK.peaks.length)continue;const v=PK.peaks[k]*H*0.42;g.fillRect(x,H*0.5-v,2,v*2)}}
 g.font=Math.round(14*KK)+'px -apple-system,"Hiragino Sans",sans-serif';const all=[];for(const bid in TE)TE[bid].pieces.forEach((q,i)=>{if(q.speaker>=0&&!q.deleted)all.push({q,bid,i})});
 all.forEach(o=>{const x=(o.q.start-t0)*px;if(x<-500||x>W+10)return;const sel=(o.bid===DSEL[0]&&o.i===DSEL[1]);g.strokeStyle=sel?'#ff5a4f':'#e0a23b';g.lineWidth=(sel?3:1.5)*KK;g.beginPath();g.moveTo(x,0);g.lineTo(x,H);g.stroke();g.fillStyle=sel?'#ffd7d3':'#f3d9a4';g.fillText(o.q.text.replace(/\n/g,'').slice(0,12),x+6*KK,H-8*KK)});
 g.strokeStyle='#fff';g.lineWidth=2;const cx=(au.currentTime-t0)*px;g.beginPath();g.moveTo(cx,0);g.lineTo(cx,H);g.stroke();
 const fr=(p.start+OFF)*30,inf=document.getElementById('d2info');if(inf)inf.textContent='開始 '+fmt(p.start)+'（'+Math.floor(fr/30)+'秒'+String(Math.round(fr%30)).padStart(2,'0')+'）'}
function dnudge(d){const p=dp();if(!p)return;snap();p.start=Math.round((p.start+d)*1000)/1000;p.m=1;dirty();renderTelops();D2A=p.start;if(au.paused)seek(p.start)}
function dplay(){const p=dp();if(p)playRange(p.start,p.start+0.8)}
function bindD2(){const c=document.getElementById('wave2');if(!c)return;
 const tAt=e=>{const r=c.getBoundingClientRect();return (D2A-W2*0.3)+(e.clientX-r.left)/r.width*W2};
 c.onpointerdown=e=>{const p=dp();if(!p)return;c.setPointerCapture(e.pointerId);const t=tAt(e),tol=14/c.getBoundingClientRect().width*W2;
  const cand=[];for(const bid in TE)TE[bid].pieces.forEach((q,i)=>{if(q.speaker>=0&&!q.deleted&&Math.abs(q.start-t)<tol)cand.push({bid,i,d:Math.abs(q.start-t)})});cand.sort((a,b)=>a.d-b.d);
  if(cand.length){snap();DSEL=[cand[0].bid,cand[0].i];D2DRAG=DSEL}else{pauseNow();seek(t)}};
 c.onpointermove=e=>{if(!D2DRAG)return;const q=TE[D2DRAG[0]].pieces[D2DRAG[1]];if(!q)return;q.start=Math.round(tAt(e)*1000)/1000;if(au.paused)au.currentTime=q.start};
 c.onpointerup=()=>{if(D2DRAG){const q=TE[D2DRAG[0]].pieces[D2DRAG[1]];D2DRAG=null;dirty();renderTelops();if(q&&au.paused)seek(q.start)}};
 c.onwheel=e=>{if(e.ctrlKey||e.metaKey){e.preventDefault();W2=Math.min(Math.max(W2*(e.deltaY>0?1.15:0.87),0.5),8)}}}
document.addEventListener('keydown',e=>{if(!dp()||e.target.tagName==='TEXTAREA'||e.target.tagName==='INPUT')return;if(e.key==='ArrowLeft'){e.preventDefault();dnudge(-1/30)}else if(e.key==='ArrowRight'){e.preventDefault();dnudge(1/30)}});
function tlEdits(){const a=[];for(const g of segs()){a.push(g.a0);for(const [p] of tlLive(g.bid))a.push(p.start)}return [...new Set(a)].sort((x,y)=>x-y)}
function fcpKey(e){if(!TE||e.isComposing||e.defaultPrevented)return;const tg=(e.target&&e.target.tagName)||'';if(tg==='TEXTAREA'||tg==='INPUT'||tg==='SELECT')return;const m=e.metaKey||e.ctrlKey,k=e.key,t=au.currentTime;
 if(m&&!e.shiftKey&&!e.altKey&&(k==='='||k==='+'||k==='-')){e.preventDefault();tlZoom(k==='-'?-1:1);return}
 if(m||e.altKey)return;
 if(e.shiftKey&&(k==='Z'||k==='z')){e.preventDefault();TLZ=Math.max(30,Math.min(1200,(document.getElementById('tls').clientWidth-60)/Math.max(1,segs().reduce((a,g)=>a+(g.a1-g.a0),0)+0.5*segs().length)));tlDraw();return}
 if(e.shiftKey&&k==='?'){e.preventDefault();tlAround();return}
 if(k==='k'||k==='K'){e.preventDefault();pauseNow();return}
 if((k==='ArrowUp'||k==='ArrowDown')&&TLSEL&&tlSel()&&tlSel().speaker>=0&&!tlSel().ov&&tlSel().speaker<=1){e.preventDefault();snap();const q=tlSel();q.speaker=q.speaker===0?1:0;q.m=1;dirty();renderTelops();tlDraw();return}
 if(k==='ArrowUp'||k==='ArrowDown'){const ed=tlEdits();const n=k==='ArrowDown'?ed.find(x=>x>t+0.02):[...ed].reverse().find(x=>x<t-0.02);if(n!==undefined){e.preventDefault();seek(n+0.001)}return}
 if((k==='ArrowLeft'||k==='ArrowRight')&&!dp()){e.preventDefault();const d=(k==='ArrowLeft'?-1:1)*(e.shiftKey?10:1)/30;pauseNow();seek(t+d);return}
 if((k==='Delete'||k==='Backspace')&&(CHIPCUR||NOCHIP))return;
 if((k==='Delete'||k==='Backspace')&&TLNS&&TE[TLNS[0]]&&(TE[TLNS[0]].notes||[])[TLNS[1]]){e.preventDefault();snap();TE[TLNS[0]].notes[TLNS[1]].deleted=true;TLNS=null;dirty();renderTelops();tlDraw();return}
 if((k==='Delete'||k==='Backspace')&&!SEL.size&&TLSEL&&tlSel()){e.preventDefault();tlDel(e.shiftKey);return}}
document.addEventListener('keydown',fcpKey);
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&(e.key==='s'||e.key==='S')){e.preventDefault();if(!TE)return;if(!EDIT)EDIT=true;tsave(false)}});
setInterval(()=>{drawWave();drawDetail()},50);
(()=>{const c=document.getElementById('wave');let drag=null;
 const tAt=e=>{const r=c.getBoundingClientRect();return (WC-WIN/2)+(e.clientX-r.left)/r.width*WIN};
 c.onpointerdown=e=>{c.setPointerCapture(e.pointerId);const t=tAt(e),tol=12/c.getBoundingClientRect().width*WIN;
  const b=boundaries().filter(b=>Math.abs(b.t-t)<tol).sort((x,y)=>Math.abs(x.t-t)-Math.abs(y.t-t))[0];
  if(b){snap();drag=b}else{pauseNow();seek(t);WC=t}};
 c.onpointermove=e=>{if(!drag)return;const t=Math.round(tAt(e)*100)/100;TE[drag.bid].pieces[drag.i].start=t;drag.t=t;au.paused&&(au.currentTime=t)};
 c.onpointerup=()=>{if(drag){dirty();renderTelops();seek(drag.t);WC=drag.t;drag=null}}})();
let TGEN='',AUTOAN=false;async function loadTelops(){const r=await api('/api/telops');TGEN=r.gen||'';if(!r.auto){TE=null;document.getElementById('telopCard').style.display='none';return}
 EDIT=false;TN=r.names;STY=r.styles;FR=r.frame||[1920,1080];OFF=r.offset||0;HEARD=r.heard||[];UNDO=[];TE=JSON.parse(JSON.stringify(r.auto));
 for(const k in (r.edited||{}))if(TE[k]&&Math.abs(r.edited[k].a0-TE[k].a0)<0.3)TE[k]=r.edited[k];
 for(const k in TE)tlNorm(k);document.getElementById('telopCard').style.display='block';renderTelops();loadPeaks();if(!TLCL)tlOpen();attachAlt()}
async function attachAlt(only){const TE0=TE;if(!TE0)return;let ch=false;
 for(const bid of (only?[only]:Object.keys(TE0))){const B=TE0[bid];if(!B||skipped(bid))continue;
  const src=B.pieces.map(p=>{const q=Object.assign({},p);delete q.m;return q});let r;try{r=await api('/api/alts',{bid:bid,pieces:src})}catch(e){continue}
  if(TE!==TE0||!r||r.error||!r.alts)continue;const n=x=>String(x).replace(/[\s、。，．,.！？!?…・「」『』（）()]/g,'');
  B.pieces.forEach((p,i)=>{if(p.src==='s'&&p.alt!==undefined&&n(p.alt)===n(p.text)){delete p.alt;delete p.src;ch=true}if(p.src==='s')return;const al=r.alts[i];if(al&&n(al)!==n(p.text)){if(p.alt!==al){p.alt=al;p.src='h';ch=true}}
   else if(al&&n(al)===n(p.text)){/* the telop already has the script's words: what was really heard is the other choice */
    const hs=((pieceChips(bid)[i])||[]).map(c=>c.c).join(''),fa=n(hs),fb=n(p.text);
    if(fa&&fb&&fa!==fb){const A=[...fa],Bc=[...fb],D=Array.from({length:A.length+1},()=>new Array(Bc.length+1).fill(0));for(let x=A.length-1;x>=0;x--)for(let y=Bc.length-1;y>=0;y--)D[x][y]=A[x]===Bc[y]?D[x+1][y+1]+1:Math.max(D[x+1][y],D[x][y+1]);
     if(D[0][0]>=0.6*Math.max(A.length,Bc.length)&&Math.abs(A.length-Bc.length)<=4){let two=fa;if(p.text.includes('\n')){const bs=wsplit(fa).map(o=>o.e).filter(e=>e>0&&e<A.length);let kk=Math.ceil(A.length/2);if(bs.length)kk=bs.reduce((u,v)=>Math.abs(v-A.length/2)<Math.abs(u-A.length/2)?v:u);two=A.slice(0,kk).join('')+'\n'+A.slice(kk).join('')}if(p.alt!==two){p.alt=two;p.src='s';ch=true}}else if(p.alt!==undefined){delete p.alt;delete p.src;ch=true}}
    else if(p.alt!==undefined){delete p.alt;delete p.src;ch=true}}
   else if(p.alt!==undefined){delete p.alt;delete p.src;ch=true}})}
 if(ch&&TE===TE0)renderTelops()}
let ALTT={};function reAlt(bid){clearTimeout(ALTT[bid]);ALTT[bid]=setTimeout(()=>attachAlt(bid),600)}
function snap(){UNDO.push(JSON.stringify(TE));if(UNDO.length>80)UNDO.shift()}
async function exactBlock(bid){snap();const r=await api('/api/exact',{bid:bid,pieces:TE[bid].pieces});if(r.error){alert(r.error);return}TE[bid].pieces=r.pieces;dirty();renderTelops()}
function swapBlock(bid){snap();TE[bid].pieces.forEach(p=>{if(p.speaker===0)p.speaker=1;else if(p.speaker===1)p.speaker=0});dirty();renderTelops()}
function undo(){if(!UNDO.length)return;TE=JSON.parse(UNDO.pop());dirty();renderTelops()}
function dirty(){EDIT=true;document.getElementById('tmsg').textContent='';clearTimeout(saveT);saveT=setTimeout(()=>tsave(true),700)}
let HEARD=[],EDTROW=null;
function moraW(c){if('ゃゅょャュョぁぃぅぇぉ'.includes(c))return .3;if('っッ'.includes(c))return .7;if(/[\u4e00-\u9fff0-9]/.test(c))return 2;if('、。，．,.!！?？…・「」'.includes(c))return .2;return 1}
const WW=['っていう','ということ','という','ところ','から','けど','ので','のに','ない','ます','ました','です','でした','たい','たら','って','ている','ていた','てる','てた','より','まで','ばかり','だけ','みたい','ように','らしい','なんか','ちゃん','じゃ','では','には','とか','でも','もう','まぁ','そう','こと','もの','ため','ほど','たの','んだ','いや','はい'].sort((x,y)=>y.length-x.length);
const CKC={};let CKQ=new Set(),CKT=null;
function wsplit(t){const c=CKC[t];if(c)return c.map(x=>({w:t.slice(x[0],x[1]),i:x[0],e:x[1]}));if(!(t in CKC)&&/\S/.test(t)){CKQ.add(t);clearTimeout(CKT);CKT=setTimeout(ckFlush,30)}return wsplit0(t)}
function ckFlush(){const ts=[...CKQ];CKQ.clear();if(!ts.length)return;ts.forEach(t=>{CKC[t]=false});api('/api/chunks',{texts:ts}).then(j=>{if(!j||!j.chunks)return;j.chunks.forEach((c,k)=>{CKC[ts[k]]=c});HC={};if(TE)renderTelops()}).catch(()=>{})}
function wsplit0(t){const out=[],cls=c=>/[\u4e00-\u9fff々〇]/.test(c)?'k':/[ぁ-ゖ]/.test(c)?'h':/[ァ-ヺー]/.test(c)?'a':/[A-Za-z0-9０-９]/.test(c)?'e':/\s/.test(c)?'s':'p';
 let k=0;const n=t.length;while(k<n){const c=cls(t[k]);if(c==='s'){k++;continue}let e=k+1;
  if(c==='k'||c==='a'||c==='e'){while(e<n&&(cls(t[e])===c||(c==='e'&&/[-'.]/.test(t[e]))||(c==='a'&&t[e]==='ー')))e++;
   if(c==='k'){if(e-k>=3&&e<n&&cls(t[e])==='h'&&!/[はがをにへともの]/.test(t[e])){out.push({w:t.slice(k,e-1),i:k,e:e-1});k=e-1}let m=0;while(e<n&&m<3&&cls(t[e])==='h'&&!/[はがをにへともの]/.test(t[e])){e++;m++}}}
  else if(c==='h'){let hit=null;for(const w of WW)if(t.startsWith(w,k)){hit=w;break}
   if(hit)e=k+hit.length;else{while(e<n&&cls(t[e])==='h'&&!/[はがをにへでともの]/.test(t[e-1])&&!WW.some(w=>t.startsWith(w,e)))e++}}
  out.push({w:t.slice(k,e),i:k,e});k=e}
 const PUN=/^[、。！？!?…・「」~ー]+$/;const m=[];
 for(const o of out){const L=m.length?m[m.length-1]:null;if(L&&PUN.test(o.w)){L.w+=o.w;L.e=o.e}else m.push(o)}
 return m}
function chipsHtml(bid,i,p,e){const t=p.text||'';const cum=[0];let tw=0;for(const c of t){tw+=(c==='\n'?0:moraW(c));cum.push(tw)}tw=tw||1;const T=x=>p.start+(e-p.start)*cum[x]/tw;
 return wsplit(t).map((o,k)=>'<span class="wc'+(t[o.i-1]==='\n'?' nl':'')+'" data-t0="'+T(o.i).toFixed(3)+'" data-t1="'+T(o.e).toFixed(3)+'" data-i0="'+o.i+'" data-i1="'+o.e+'" onclick="chipClick(event,\''+bid+'\','+i+')" ondblclick="chipDbl(\''+bid+'\','+i+')">'+esc(o.w)+'<b class="wx" onclick="chipDel(event,\''+bid+'\','+i+',this)">×</b></span>').join('')}
let HC={};
let VMK=null;
function vmask(){if(VMK||!PK)return VMK;const p=PK.peaks,n=p.length,db=new Float32Array(n);for(let k=0;k<n;k++)db[k]=20*Math.log10(Math.max(p[k]*p[k],1e-7));const top=Array.from(db).sort((x,y)=>x-y)[Math.floor(n*0.99)]||0,th=top-35.6;const M=new Uint8Array(n);for(let k=0;k<n;k++)M[k]=db[k]>th?1:0;let last=-1;for(let k=0;k<n;k++){if(M[k]){if(last>=0&&k-last>1&&k-last<=8)for(let q=last+1;q<k;q++)M[q]=1;last=k}}const CS=new Int32Array(n+1);for(let k=0;k<n;k++)CS[k+1]=CS[k]+M[k];return VMK={M,CS,n,r:PK.rate}}
function voiceMap(s0,s1){const V=vmask();if(!V)return null;const{M,CS,n,r}=V;const snap=t=>{let k=Math.min(Math.max(Math.round(t*r),0),n-1);if(!M[k])return k;for(let d=1;d<=50;d++){if(k-d>=0&&!M[k-d])return k-d;if(k+d<n&&!M[k+d])return k+d}return k};
 const a=snap(s0),b=snap(s1),tot=CS[b]-CS[a];if(tot<10)return null;
 return f=>{const T=Math.min(Math.max(f,0),1)*tot;let lo=a,hi=b;while(lo<hi){const m=(lo+hi)>>1;if(CS[m+1]-CS[a]>=T&&(f>0||M[m]))hi=m;else lo=m+1}if(f<=0){while(lo<b&&!M[lo])lo++;return lo/r}return (lo+1)/r}}
function pieceChips(bid){if(HC[bid])return HC[bid];const b=TE[bid],ps=b.pieces;const skipc=c=>/[、。,.！？!?「」『』（）()\s・…～~"]/.test(c);
 const ok=ps.filter(p=>p.speaker>=0);const lo=ok.length?Math.min(...ok.map(p=>p.start)):0,hi=b.a1;
 const H=[];for(const s of HEARD){const s0=s[0],s1=s[1];if(s1<=lo-0.8||s0>=hi||s1<=s0)continue;const tx=(s[2]||'').replace(/\s+/g,'');const L=tx.length;const W=[];let WT=0;for(let k=0;k<L;k++){const w=skipc(tx[k])?0:moraW(tx[k]);W.push(w);WT+=w}let wc=0;const vm=voiceMap(s0,s1);for(let k=0;k<L;k++){if(skipc(tx[k]))continue;let cs,ce;if(vm){cs=vm(wc/(WT||1));ce=vm((wc+W[k])/(WT||1))}else{cs=s0+k/L*(s1-s0);ce=s0+(k+1)/L*(s1-s0)}wc+=W[k];if(ce<=lo-0.8||cs>=hi)continue;H.push({c:tx[k],cs,ce})}}
 const D=[];ps.forEach((p,i)=>{if(p.speaker<0||(p.text||'').startsWith('※'))return;for(const c of (p.text||'').replace(/\n/g,''))if(!skipc(c))D.push({c,i})});
 const res=ps.map(()=>[]);if(!H.length||!D.length){return HC[bid]=res}
 const n=H.length,m=D.length,W=m+1,T=new Uint16Array((n+1)*W);
 for(let x=n-1;x>=0;x--)for(let y=m-1;y>=0;y--)T[x*W+y]=H[x].c===D[y].c?T[(x+1)*W+y+1]+1:Math.max(T[(x+1)*W+y],T[x*W+y+1]);
 const own=new Array(n).fill(-1);{let x=0,y=0;while(x<n&&y<m){if(H[x].c===D[y].c){own[x]=D[y].i;x++;y++}else if(T[(x+1)*W+y]>=T[x*W+y+1])x++;else y++}}
 let cur=-1;for(let x=0;x<n;x++){const tm=(H[x].cs+H[x].ce)/2;let tp=-1;for(let q=0;q<ps.length;q++)if(ps[q].speaker>=0&&!(ps[q].text||'').startsWith('※')&&ps[q].start<=tm+0.02)tp=q;
 if(own[x]>=0){cur=Math.max(cur,own[x]);own[x]=cur}else{if(tp<0)tp=D[0].i;cur=Math.max(cur,tp);own[x]=cur}}
 H.forEach((h,x)=>res[own[x]].push(h));return HC[bid]=res}
function heardHtml(bid,i,p,e){const cs=(pieceChips(bid)[i])||[];if(!cs.length&&(p.text||'').replace(/\s/g,'').length>0)return chipsHtml(bid,i,p,e);if(!cs.length)return '<span class="hc" style="cursor:pointer" title="クリックでこの位置へ（そのまま再生で聞けます）" onclick="nochipSel(event,\''+bid+'\','+i+')"></span>';
 const sub=cs.map(c=>c.c).join('');return wsplit(sub).map(o=>{const a=cs[o.i],z=cs[Math.min(cs.length-1,o.e-1)];return '<span class="wc" data-t0="'+a.cs.toFixed(3)+'" data-t1="'+z.ce.toFixed(3)+'" onclick="chipClick(event,\''+bid+'\','+i+')" ondblclick="chipDbl(\''+bid+'\','+i+')">'+esc(o.w)+'<b class="wx" onclick="chipDel(event,\''+bid+'\','+i+',this)">×</b></span>'}).join('')}
let CHIPCUR=null;
function chipClick(ev,bid,i){SEL=new Set();NOCHIP=null;document.querySelectorAll('.trow.sel').forEach(x=>x.classList.remove('sel'));const el=ev.currentTarget,r=el.getBoundingClientRect(),left=(ev.clientX-r.left)<r.width/2;seek(left?+el.dataset.t0:+el.dataset.t1);
 document.querySelectorAll('.wc.cl,.wc.cr').forEach(x=>x.classList.remove('cl','cr'));el.classList.add(left?'cl':'cr');CHIPCUR={bid,i,left,t:left?+el.dataset.t0:+el.dataset.t1,idx:[...el.parentNode.querySelectorAll('.wc')].indexOf(el)}}
function cutSnap(t){/* a cut made on a word boundary lands where the next word really begins (the word times run a little early) */
 try{if(!AB)return t;const sr=AB.sampleRate,d=AB.getChannelData(0),hop=Math.max(Math.round(sr*0.005),1),win=Math.round(sr*0.01);const a=Math.max(0,t-0.15),n=Math.floor(0.45/0.005);const E=[];
  for(let k=0;k<n;k++){const i0=Math.round((a+k*0.005)*sr);if(i0+win>=d.length)break;let z=0;for(let j=0;j<win;j++){const v=d[i0+j];z+=v*v}E.push(Math.sqrt(z/win))}
  if(E.length<20)return t;let m=-1,lo=1e9;for(let k=0;k<E.length;k++){const tk=a+k*0.005;if(tk<t-0.12||tk>t+0.2)continue;if(E[k]<lo){lo=E[k];m=k}}
  if(m<0)return t;let hi=0;for(let k=m;k<E.length;k++)hi=Math.max(hi,E[k]);if(hi<lo*2.2||hi<0.004)return t;const thr=lo+0.35*(hi-lo);let k=m;while(k<E.length&&E[k]<=thr)k++;
  return Math.round((a+k*0.005-0.01)*100)/100}catch(e){return t}}
function chipSplit(){const c=CHIPCUR;if(!c)return false;const row=document.getElementById('row_'+c.bid+'_'+c.i);if(!row)return false;const chips=[...row.querySelectorAll('.heardrow .wc')];
 const n=c.left?c.idx:c.idx+1;if(n<=0||n>=chips.length)return false;const P=TE[c.bid].pieces,p=P[c.i];const flat=p.text.replace(/\n/g,'');
 const hs=chips.map(x=>x.firstChild.textContent.replace(/[、。,.！？!?\s]/g,''));const heard=hs.join(''),bd=hs.slice(0,n).join('').length;
 /* line the heard letters up with the telop letters (longest common run), then carry the chip boundary across */
 const fold=s=>[...s.normalize('NFKC').replace(/[ァ-ヶ]/g,c=>String.fromCharCode(c.charCodeAt(0)-96))];
 const A=fold(heard),B=fold(flat),D=Array.from({length:A.length+1},()=>new Array(B.length+1).fill(0));
 for(let x=A.length-1;x>=0;x--)for(let y=B.length-1;y>=0;y--)D[x][y]=A[x]===B[y]?D[x+1][y+1]+1:Math.max(D[x+1][y],D[x][y+1]);
 const mp=new Array(A.length).fill(-1);{let x=0,y=0;while(x<A.length&&y<B.length){if(A[x]===B[y]){mp[x]=y;x++;y++}else if(D[x+1][y]>=D[x][y+1])x++;else y++}}
 /* the boundary sits between heard letters bd-1 and bd: carry it across through the nearest matched letters on both sides */
 let px=-1,py=-1,nx=A.length,ny=B.length;for(let x=bd-1;x>=0;x--)if(mp[x]>=0){px=x;py=mp[x];break}for(let x=bd;x<A.length;x++)if(mp[x]>=0){nx=x;ny=mp[x];break}
 const gl=py+1,gr=ny,sl=px+1,sr=nx;let pos=gl;if(gr>gl){pos=sr>sl?Math.round(gl+(bd-sl)/(sr-sl)*(gr-gl)):gl}
 if(pos<=0||pos>=flat.length)return false;let kk=0,cnt=0;while(kk<p.text.length&&cnt<pos){if(p.text[kk]!=='\n')cnt++;kk++}
 const e=pend(c.bid,c.i),t=fr30(cutSnap(c.t));const ok=doSplit(c.bid,c.i,kk);if(!ok)return false;const q=TE[c.bid].pieces[c.i+1];if(t>p.start+0.05&&t<e-0.05){q.start=r3(t)}CHIPCUR=null;dirty();renderTelops([c.bid,c.i+1,0]);return true}
document.addEventListener('keydown',e=>{if(e.key!=='Enter'||e.isComposing||!CHIPCUR)return;const tg=(e.target&&e.target.tagName)||'';if(tg==='TEXTAREA'||tg==='INPUT'||tg==='SELECT')return;if(chipSplit())e.preventDefault()});
document.addEventListener('keydown',e=>{if((e.key!=='Delete'&&e.key!=='Backspace')||e.isComposing||!CHIPCUR)return;const tg=(e.target&&e.target.tagName)||'';if(tg==='TEXTAREA'||tg==='INPUT'||tg==='SELECT')return;
 const c=CHIPCUR,row=document.getElementById('row_'+c.bid+'_'+c.i);if(!row)return;const chips=row.querySelectorAll('.heardrow .wc'),P=TE[c.bid].pieces;
 if(c.left&&c.idx===0&&c.i>0){e.preventDefault();CHIPCUR=null;const k=tnext(P,c.i,-1);if(k>=0){const L=P[k].text.replace(/\n/g,'').length;tmerge(c.bid,k,[c.bid,k,L],c.i)}}
 else if(!c.left&&c.idx===chips.length-1){const j=tnext(P,c.i,1);if(j>=0){e.preventDefault();CHIPCUR=null;const L=P[c.i].text.replace(/\n/g,'').length;tmerge(c.bid,c.i,[c.bid,c.i,L],j)}}});
document.addEventListener('keydown',e=>{if((e.key!=='b'&&e.key!=='B')||!(e.metaKey||e.ctrlKey)||e.shiftKey||e.altKey||e.isComposing||!TE)return;e.preventDefault();splitHere()});
let NOCHIP=null;
function nochipSel(ev,bid,i){seek(TE[bid].pieces[i].start+0.01);NOCHIP={bid,i};CHIPCUR=null;document.querySelectorAll('.hc.sel,.wc.cl,.wc.cr').forEach(x=>x.classList.remove('sel','cl','cr'));ev.currentTarget.classList.add('sel')}
document.addEventListener('keydown',e=>{if((e.key!=='Delete'&&e.key!=='Backspace')||e.isComposing||!NOCHIP)return;const tg=(e.target&&e.target.tagName)||'';if(tg==='TEXTAREA'||tg==='INPUT'||tg==='SELECT')return;
 const {bid,i}=NOCHIP,P=TE[bid]&&TE[bid].pieces;NOCHIP=null;if(!P||!P[i])return;e.preventDefault();snap();
 if(e.shiftKey){P[i].deleted=true;P[i].cut=true;dirty();renderTelops();return}
 const k=tnext(P,i,-1);if(k>=0){const L=P[k].text.replace(/\n/g,'').length;tmerge(bid,k,[bid,k,L],i)}else{P.splice(i,1);dirty();renderTelops()}});
function chipDbl(bid,i){DSEL=[bid,i];D2A=null;renderTelops()}
function chipDel(ev,bid,i,x){ev.stopPropagation();const ch=x.parentNode,w0=ch.firstChild.textContent;const P=TE[bid].pieces,p=P[i];const e=pend(bid,i);
 let t0=+ch.dataset.t0,t1=+ch.dataset.t1;if(!(t1-t0>0.05))return;snap();
 const flat=p.text.replace(/\n/g,'');let w=w0.replace(/[、。,.！？!?\s]/g,'')||w0;let k=flat.indexOf(w);while(k<0&&w.length>1){w=w.slice(1);k=flat.indexOf(w)}const nlAt=[];let c=0;for(let q=0;q<p.text.length;q++){if(p.text[q]==='\n')nlAt.push(c);else c++}
 if(k<0){k=Math.max(0,Math.min(flat.length,Math.round((t0-p.start)/Math.max(e-p.start,0.01)*flat.length)))}
 const ln=Math.min(w.length,flat.length-k);const cut=(a,z)=>{let r='';for(let q=a;q<z;q++){if(nlAt.includes(q)&&q>a)r+='\n';r+=flat[q]}return r};
 const A=cut(0,k),B=cut(k+ln,flat.length),D=flat.slice(k,k+ln)||w,r2=v=>Math.round(v*100)/100;
 const out=[];const tail=p.end;
 if(A.trim()){out.push(Object.assign({},p,{text:A}))}
 out.push(Object.assign({},p,{start:r2(t0),text:D,deleted:true,cut:true}));
 if(B.trim()){out.push(Object.assign({},p,{start:r2(t1),text:B,deleted:false}))}
 P.splice(i,1,...out);dirty();renderTelops()}
function toggleEdt(bid,i){EDTROW=(EDTROW===bid+'_'+i)?null:bid+'_'+i;renderTelops(EDTROW?[bid,i,0]:undefined)}
function chipLight(t,key){const row=key&&document.getElementById('row_'+key);const old=document.querySelectorAll('.wc.on');if(!row){old.forEach(e=>e.classList.remove('on'));return}const cs=row.querySelectorAll('.heardrow .wc');let hit=null;cs.forEach(c=>{if(t>=+c.dataset.t0-0.001&&t<+c.dataset.t1)hit=c});old.forEach(e=>{if(e!==hit)e.classList.remove('on')});if(hit&&!hit.classList.contains('on'))hit.classList.add('on')}
let LB={},LBN=0,DR=null;
function bandItems(bid){const b=TE[bid],o=[];(b.notes||[]).forEach((n,ni)=>{if(!n.deleted)o.push({kind:'note',obj:n,ni,get s(){return n.start},get e(){return n.end},c:'c'+(ni%4)})});
 b.pieces.forEach(p=>{if(p.speaker<0&&!p.deleted&&!p.cut)o.push({kind:'laugh',obj:p,get s(){return p.start},get e(){return Math.min(p.end,b.a1)},c:'cL'})});return o}
function laneCell(bid,i,p){if(p.cut)return '';const b=TE[bid];const its=bandItems(bid);if(!its.length)return '<div class="ln" title="クリックで※を足す" onclick="laneNew(\''+bid+'\','+i+')"></div>';const vis=b.pieces.filter(q=>!q.cut);let h='';
 its.forEach(it=>{const cov=q=>q.start>=it.s-0.01&&q.start<it.e-0.01||(it.kind==='laugh'&&q===it.obj);if(!cov(p))return;
  const idx=vis.indexOf(p);const first=!vis.slice(0,idx).some(cov),last=!vis.slice(idx+1).some(cov);const k=++LBN;LB[k]={bid,it};
  h+='<div class="lb '+it.c+(first?' f':'')+(last?' l':'')+'">'+(first?'<div class="bhd top" data-k="'+k+'" data-e="top"></div>':'')
   +(first?(it.kind==='note'?'<input class="nin" value="'+esc(it.obj.text)+'" title="※の中身（直接書き換えられます）" oninput="LB['+k+'].it.obj.text=this.value;dirty()" onfocus="snapOnce()" onmousedown="event.stopPropagation()"><b class="nx" title="この※を消す" onclick="noteDel(event,'+k+')">✕</b>':'<span class="nl">笑 '+esc(it.obj.text.slice(0,4))+'</span>'):'')
   +(last?'<div class="bhd bot" data-k="'+k+'" data-e="bot"></div>':'')+'</div>'});
 return '<div class="ln" title="空いている所をクリックで※を足す" onclick="if(event.target===this)laneNew(\''+bid+'\','+i+')">'+h+'</div>'}
function laneNew(bid,i){noteAdd(bid,i)}
function noteAdd(bid,ii){const b=TE[bid];snap();const t=au.currentTime;let i=0;if(ii!==undefined)i=ii;else b.pieces.forEach((q,j)=>{if(!q.cut&&q.speaker>=0&&q.start<=t+0.01)i=j});const q=b.pieces[i];(b.notes=b.notes||[]).push({start:Math.round(q.start*100)/100,end:Math.round(pend(bid,i)*100)/100,text:'',with_telop:true});dirty();renderTelops();setTimeout(()=>{const ins=document.querySelectorAll('#row_'+bid+'_'+i+' .nin');const el=ins[ins.length-1];if(el){el.focus();el.placeholder='※の中身'}},50)}
function noteDel(ev,k){ev.stopPropagation();const r=LB[k];snap();r.it.obj.deleted=true;dirty();renderTelops()}
document.addEventListener('mousedown',e=>{const h=e.target.closest&&e.target.closest('.bhd');if(!h)return;e.preventDefault();e.stopPropagation();const r=LB[h.dataset.k];if(!r)return;snap();DR={bid:r.bid,it:r.it,edge:h.dataset.e,last:null};document.body.style.cursor='ns-resize'},true);
document.addEventListener('mousemove',e=>{if(!DR)return;const b=TE[DR.bid],rows=[...document.querySelectorAll('[id^="row_'+DR.bid+'_"]')].filter(x=>/^row_.+_\d+$/.test(x.id));if(!rows.length)return;
 let r=null;for(const x of rows){const q=x.getBoundingClientRect();if(e.clientY>=q.top&&e.clientY<q.bottom){r=[x,q];break}}
 const it=DR.it,o=it.obj;let v;
 if(!r){const f=rows[0].getBoundingClientRect(),l=rows[rows.length-1].getBoundingClientRect();if(e.clientY<f.top)v=DR.edge==='top'?b.pieces[+rows[0].id.split('_').pop()].start:null;else if(e.clientY>=l.bottom)v=DR.edge==='bot'?b.a1:null;if(v===null)return}
 else{const i=+r[0].id.split('_').pop(),pc=b.pieces[i],up=(e.clientY-r[1].top)<r[1].height/2;v=DR.edge==='top'?(up?pc.start:pend(DR.bid,i)):(up?pc.start:pend(DR.bid,i))}
 if(it.kind==='laugh'&&DR.edge==='bot'){const oi=b.pieces.indexOf(o);const hi=r?+r[0].id.split('_').pop():null;if(hi!==null&&hi<=oi)v=o.start+0.3;v=Math.max(v,o.start+0.3)}
 v=Math.round(v*100)/100;const cs=o.start,ce=it.kind==='note'?o.end:o.end;
 if(DR.edge==='top'){if(v>=(it.kind==='note'?o.end:o.end)-0.2||v===o.start)return;o.start=v}else{if(v<=o.start+(it.kind==='laugh'?0.25:0.2)||v===o.end)return;o.end=v}
 if(DR.last===o.start+'_'+o.end)return;DR.last=o.start+'_'+o.end;renderTelops()},true);
document.addEventListener('mouseup',()=>{if(DR){DR=null;document.body.style.cursor='';dirty()}},true);
function fastBadge(p,e){return ''}
function ovInfo(bid,i,p){const ps=TE[bid].pieces;const note=(p.text||'').replace(/\n/g,'').startsWith('※');if(p.speaker>=0&&!note&&!p.ov)return '';
 let hit=null;for(let j=0;j<ps.length;j++){if(j===i||ps[j].speaker<0||(ps[j].text||'').startsWith('※'))continue;const e=pend(bid,j);if(p.start>=ps[j].start-0.001&&p.start<e){hit=ps[j];break}}
 const kind=note?'※':(p.ov?'重ね':'www');return hit?('「'+hit.text.replace(/\n/g,'').slice(0,12)+'」('+fmt(hit.start)+')と重なり'):'単独（'+kind+'）'}
function renderTelops(focus){if(!TE)return;HC={};LB={};LBN=0;const y=window.scrollY;
 {const _t=tlSel(),_tb=TLSEL&&TLSEL[0],_d=dp(),_db=DSEL&&DSEL[0],_s=[...SEL].map(k=>{const m=k.lastIndexOf('_'),b=k.slice(0,m);return [b,TE[b]&&TE[b].pieces[parseInt(k.slice(m+1))]]});
  for(const k in TE)TE[k].pieces.sort((x,y)=>x.start-y.start);
  if(_t)TLSEL=[_tb,TE[_tb].pieces.indexOf(_t)];if(_d)DSEL=[_db,TE[_db].pieces.indexOf(_d)];SEL=new Set(_s.filter(([b,p])=>p).map(([b,p])=>b+'_'+TE[b].pieces.indexOf(p)))}
const unh={};((S&&S.report)?S.report.blocks:[]).forEach(b=>unh[b.id]=b.unheard||[]);const cue={};((S&&S.report)?S.report.blocks:[]).forEach(b=>cue[b.id]=b.cue);
 let h='';for(const bid of Object.keys(TE).sort()){const b=TE[bid];b.pieces.sort((x,y)=>x.start-y.start);
  if(skipped(bid)){h+='<div class="tb"><h4>'+esc(bname(bid))+'　<span class="small">不使用ブロック</span> <button class="ib" onclick="toggleSkip(\''+bid+'\')">復元</button></h4></div>';continue}
  h+='<div class="tb"><h4>'+esc(bname(bid))+'　<span class="small">'+esc(cue[bid]||'')+'</span> <button class="ib" title="このブロックのチ・ホを入れ替える" onclick="swapBlock(\''+bid+'\')">⇄ 話者入替</button> <button class="ib" title="このブロックの文字を台本どおりに置き換える" onclick="exactBlock(\''+bid+'\')">台本どおり</button></h4>';
  (PK?tlGhosts(bid):(unh[bid]||[])).forEach(u=>{h+='<div class="unh">（？） '+fmt(u[0])+'〜'+fmt(u[1])+' <button class="ib" onclick="playRange('+u[0]+','+u[1]+')">▶</button> <button class="ib" onclick="tghost(\''+bid+'\','+u[0]+','+u[1]+')">テロップ化</button></div>'});
  b.pieces.forEach((p,i)=>{if(p.cut)return;const e=pend(bid,i);
   const ov=ovInfo(bid,i,p);
   h+='<div class="trow sp'+p.speaker+(ov?' lane2':'')+(EDTROW===bid+'_'+i?' edt':'')+(p.deleted?' del':'')+(SEL.has(bid+'_'+i)?' sel':'')+'" id="row_'+bid+'_'+i+'"><div class="ttime" onclick="selRow(event,\''+bid+'\','+i+')"><b>'+fmt(p.start)+'</b>'+(e-p.start).toFixed(1)+'秒'+fastBadge(p,e)+(p.deleted?'<button class="ib" onclick="event.stopPropagation();tdel(\''+bid+'\','+i+')">復元</button>':'')+'</div>'+(ov?'<div class="l2b">2列目 ↳ '+esc(ov)+'</div>':'')
    +'<div class="vc"><div class="heardrow"><span class="hl">聞き取り</span>'+heardHtml(bid,i,p,e)+'</div><textarea rows="1" id="ta_'+bid+'_'+i+'" oninput="TE[\''+bid+'\'].pieces['+i+'].text=this.value;TE[\''+bid+'\'].pieces['+i+'].m=1;dirty();fit(this)" onkeydown="tkey(event,\''+bid+'\','+i+')" onfocus="seekRow(\''+bid+'\','+i+')" onbeforeinput="snapOnce()">'+esc(p.text)+'</textarea>'+srcSel(bid,i,p)+'</div>'
    +'<select onchange="snap();TE[\''+bid+'\'].pieces['+i+'].speaker=parseInt(this.value);TE[\''+bid+'\'].pieces['+i+'].m=1;dirty();renderTelops()">'+TN.map((n,k)=>{const v=(k===TN.length-1)?-1:k;return '<option value="'+v+'"'+(p.speaker===v?' selected':'')+'>'+esc(n)+'</option>'}).join('')+'</select>'
    +laneCell(bid,i,p)+'</div>'+((DSEL&&DSEL[0]===bid&&DSEL[1]===i)?d2html():'')});
  h+='</div>'}
 document.getElementById('telops').innerHTML=h;bindD2();window.scrollTo(0,y);document.querySelectorAll('#telops textarea').forEach(fit);ACT=null;tick(true);
 if(focus){const el=document.getElementById('ta_'+focus[0]+'_'+focus[1]);if(el){el.focus({preventScroll:true});if(focus[2]!==undefined)el.setSelectionRange(focus[2],focus[2])}}}
let SEL=new Set(),LASTSEL=null;
function selRow(e,bid,i){const k=bid+'_'+i;if(e.shiftKey&&LASTSEL&&LASTSEL[0]===bid){const a=Math.min(LASTSEL[1],i),b=Math.max(LASTSEL[1],i);SEL=new Set();for(let x=a;x<=b;x++)SEL.add(bid+'_'+x)}else if(e.metaKey||e.ctrlKey){SEL.has(k)?SEL.delete(k):SEL.add(k)}else{SEL=new Set([k])}LASTSEL=[bid,i];DSEL=[bid,i];D2A=null;seek(TE[bid].pieces[i].start+0.01);renderTelops()}
function delSel(cut){if(!SEL.size)return;if(!cut){snap();const ks=[...SEL].map(k=>{const m=k.lastIndexOf('_');return [k.slice(0,m),parseInt(k.slice(m+1))]}).sort((a,b)=>b[1]-a[1]);ks.forEach(([b,i])=>{const p=TE[b].pieces[i];if(p.deleted)p.deleted=false;else TE[b].pieces.splice(i,1)});SEL=new Set();dirty();renderTelops();return}snap();const ks=[...SEL].map(k=>{const m=k.lastIndexOf('_');return [k.slice(0,m),parseInt(k.slice(m+1))]});const all=ks.every(([b,i])=>TE[b].pieces[i].deleted);ks.forEach(([b,i])=>{TE[b].pieces[i].deleted=!all});dirty();renderTelops()}
let SNAPPED=false;function snapOnce(){if(!SNAPPED){snap();SNAPPED=true;setTimeout(()=>SNAPPED=false,1500)}}
function fit(t){t.style.height='auto';t.style.height=t.scrollHeight+'px'}
function seekPlay(bid,i){seek(TE[bid].pieces[i].start+0.01);playNow()}
function seekRow(bid,i){if(au.paused)seek(TE[bid].pieces[i].start+0.01)}
function tcur(bid,i){snap();TE[bid].pieces[i].start=Math.round(au.currentTime*100)/100;dirty();renderTelops()}
function tplay(bid,i){playRange(TE[bid].pieces[i].start,pend(bid,i))}
function tset(bid,i,v){snap();TE[bid].pieces[i].start=parseFloat(v)||0;dirty();renderTelops()}
function tnudge(bid,i,d){snap();const p=TE[bid].pieces[i];p.start=Math.round((p.start+d)*100)/100;dirty();renderTelops();tplay(bid,i)}
function doSplit(bid,i,k){snap();const p=TE[bid].pieces[i],t=p.text;
 if(!(k>0&&k<t.length)){return false}
 const e=pend(bid,i),mw=x=>[...x.replace(/\n/g,'')].reduce((a,c)=>a+(typeof moraW==='function'?moraW(c):1),0),frac=mw(t.slice(0,k))/Math.max(mw(t),1);
 let ts=p.start+(e-p.start)*frac;   /* where the second half starts: by the sounds said, then to the nearest place where the voice starts again */
 if(PK){let best=null;for(const o of tlOnsets()){if(o<ts-0.35)continue;if(o>ts+0.35)break;if(o<=p.start+0.12||o>=e-0.12)continue;if(best===null||Math.abs(o-ts)<Math.abs(best-ts))best=o}if(best!==null)ts=best-0.01}ts=fr30(ts);if(ts<=p.start+0.03||ts>=e-0.03)ts=Math.min(Math.max(ts,p.start+0.04),e-0.04);
 const q={start:r3(ts),end:p.end,text:t.slice(k).replace(/^\n/,''),speaker:p.speaker};
 p.text=t.slice(0,k).replace(/\n$/,'');TE[bid].pieces.splice(i+1,0,q);reAlt(bid);dirty();renderTelops([bid,i+1,0]);return true}
function tsplit(bid,i){const ta=document.getElementById('ta_'+bid+'_'+i);if(!doSplit(bid,i,ta.selectionStart))alert('分けたい文字の間をクリックしてから「分割」を押してください')}
function tkey(e,bid,i){const ta=e.target;
 if(e.key==='Enter'&&!e.altKey&&!e.isComposing){/* a plain Enter is a line break inside the telop (no split): the browser inserts it */}
 else if((e.key==='Backspace'||e.key==='Delete')&&ta.value===''&&!e.isComposing){e.preventDefault();tdel(bid,i)}
 else if(e.key==='Backspace'&&ta.selectionStart===0&&ta.selectionEnd===0&&i>0&&!e.isComposing){const k=tnext(TE[bid].pieces,i,-1);if(k>=0){e.preventDefault();const L=TE[bid].pieces[k].text.replace(/\n/g,'').length;tmerge(bid,k,[bid,k,L],i)}}
 else if(e.key==='Delete'&&ta.selectionStart===ta.value.length&&ta.selectionEnd===ta.value.length&&!e.isComposing&&tnext(TE[bid].pieces,i,1)>=0){e.preventDefault();const L=ta.value.replace(/\n/g,'').length;tmerge(bid,i,[bid,i,L])}
 else if(e.key==='ArrowUp'&&ta.selectionStart===0&&!e.isComposing){const n=document.getElementById('ta_'+bid+'_'+(i-1));if(n){e.preventDefault();n.focus();n.setSelectionRange(n.value.length,n.value.length)}}
 else if(e.key==='ArrowDown'&&ta.selectionStart===ta.value.length&&!e.isComposing){const n=document.getElementById('ta_'+bid+'_'+(i+1));if(n){e.preventDefault();n.focus();n.setSelectionRange(0,0)}}
 else if(e.key==='Enter'&&e.altKey){e.preventDefault();tcur(bid,i)}
 else if((e.metaKey||e.ctrlKey)&&e.key==='Backspace'){e.preventDefault();tdel(bid,i)}
 else if(e.key===' '&&e.ctrlKey){e.preventDefault();togglePlay()}
 else if((e.metaKey||e.ctrlKey)&&e.key==='z'){e.preventDefault();undo()}}
function addHere(){const t=r3(fr30(curT()));if(!TE)return;let bid=null,bd=1e9;for(const k in TE){const b=TE[k],a0=(b.a0!==undefined?b.a0:(b.pieces[0]?b.pieces[0].start:0));const d=t<a0?a0-t:(t>b.a1?t-b.a1:0);if(d<bd){bd=d;bid=k}}
 if(bid===null)return;snap();const ps=TE[bid].pieces;let at=0;for(let i=0;i<ps.length;i++){if(ps[i].start<=t)at=i+1}
 let sp=0;for(let i=at-1;i>=0;i--){if(ps[i].speaker>=0){sp=ps[i].speaker;break}}
 let nx=TE[bid].a1;for(let i=at;i<ps.length;i++){if(ps[i].speaker>=0){nx=ps[i].start;break}}
 ps.splice(at,0,{start:t,end:Math.max(Math.min(nx,t+1.2),t+0.3),text:'',speaker:sp});dirty();renderTelops([bid,at,0])}
function charTimes(bid,i){/* the time of every letter boundary of a telop: from what was heard where it matches, the rest spread by the sounds */
 const P=TE[bid].pieces,p=P[i],e=pend(bid,i),flat=[...(p.text||'').replace(/\n/g,'')],n=flat.length,T=new Array(n+1).fill(null);T[0]=p.start;T[n]=e;
 const cs=(pieceChips(bid)[i])||[];
 if(cs.length&&n){const fold=x=>[...x.normalize('NFKC').replace(/[ァ-ヶ]/g,c=>String.fromCharCode(c.charCodeAt(0)-96))];const A=cs.map(c=>fold(c.c)[0]||''),B=fold(flat.join('')),D=Array.from({length:A.length+1},()=>new Array(B.length+1).fill(0));
  for(let x=A.length-1;x>=0;x--)for(let y=B.length-1;y>=0;y--)D[x][y]=A[x]===B[y]?D[x+1][y+1]+1:Math.max(D[x+1][y],D[x][y+1]);
  let x=0,y=0;while(x<A.length&&y<B.length){if(A[x]===B[y]){if(T[y]===null||y>0)T[y]=(y===0?T[0]:cs[x].cs);T[y+1]=(y+1===n?T[n]:cs[x].ce);x++;y++}else if(D[x+1][y]>=D[x][y+1])x++;else y++}
  T[0]=p.start;T[n]=e}
 let k=0;while(k<n){if(T[k]!==null){k++;continue}let j=k;while(j<=n&&T[j]===null)j++;const a=T[k-1],z=T[j],mw=c=>typeof moraW==='function'?moraW(c):1;let tot=0;for(let q=k-1;q<j;q++)tot+=mw(flat[q]||'あ');let acc=0;for(let q=k;q<j;q++){acc+=mw(flat[q-1]||'あ');T[q]=a+(z-a)*acc/Math.max(tot,1e-6)}k=j+1}
 for(let q=1;q<=n;q++)if(T[q]<T[q-1])T[q]=T[q-1];
 return T}
function splitHere(){const ta=document.activeElement;
 if(ta&&ta.tagName==='TEXTAREA'&&/^ta_/.test(ta.id||'')){const m=ta.id.match(/^ta_(.+)_(\d+)$/);if(m){const bid=m[1],i=+m[2];if(TE[bid]&&TE[bid].pieces[i]){const k=ta.selectionStart;if(k>0&&k<ta.value.length){const P=TE[bid].pieces[i];P.text=ta.value;if(doSplit(bid,i,k))return true}}}}
 const t=fr30(curT());let hit=null;
 for(const bid in TE){const ps=TE[bid].pieces;for(let i=0;i<ps.length;i++){const p=ps[i];if(p.deleted||p.cut||p.speaker<0||p.ov||(p.text||'').startsWith('※'))continue;const e=pend(bid,i);if(t>=p.start-0.001&&t<e+0.001){if(!hit||p.start>hit[2])hit=[bid,i,p.start,e]}}}
 if(!hit){document.getElementById('tmsg').textContent='ここにはテロップがありません';return false}
 snap();const [bid,i]=hit,p=TE[bid].pieces[i],e=hit[3];const tt=Math.min(Math.max(t,p.start+0.04),e-0.04);if(tt<=p.start||tt>=e){document.getElementById('tmsg').textContent='この位置では切れません';return false}
 const flat=(p.text||'').replace(/\n/g,''),L=[...flat];let k=0;
 if(L.length>=2){const T=charTimes(bid,i);const ch=wsplit(flat);const bs=[];for(const o of ch){if(o.i>0&&o.i<L.length)bs.push(o.i)}
  let best=null;for(const b of bs){const d=Math.abs(T[b]-tt);if(best===null||d<best[0])best=[d,b]}
  /* only where the sounds really say so: the nearest word boundary within a quarter second */
  if(best&&best[0]<=0.25)k=best[1]}
 const nt=r3(fr30(tt)>p.start?fr30(tt):tt);const q={start:nt,end:p.end,text:k?L.slice(k).join(''):'',speaker:p.speaker,m:1};if(k)p.text=L.slice(0,k).join('');
 TE[bid].pieces.splice(i+1,0,q);reAlt(bid);dirty();renderTelops([bid,k?i:i+1,k||0]);if(!k)document.getElementById('tmsg').textContent='切れ目が分からないため、文字は左に残しました';return true}
function tnext(ps,i,d){for(let j=i+d;j>=0&&j<ps.length;j+=d){const q=ps[j];if(q.speaker>=0&&!q.ov&&!q.deleted&&!q.cut&&!(q.text||'').startsWith('※'))return j}return -1}
function tmerge(bid,i,focus,jj){const ps=TE[bid].pieces,j=jj!==undefined?jj:tnext(ps,i,1);if(j<0)return;snap();const a=ps[i],b=ps[j];a.text=a.text.replace(/\n/g,'')+b.text.replace(/\n/g,'');a.end=Math.max(a.end||0,b.end||0);a.m=1;delete a.alt;delete a.src;ps.splice(j,1);reAlt(bid);dirty();renderTelops(focus)}
function tdel(bid,i){snap();const p=TE[bid].pieces[i];if(p.deleted){p.deleted=false}else{TE[bid].pieces.splice(i,1)}dirty();renderTelops()}
async function fillGap(bid,at){const ps=TE[bid].pieces,p=ps[at];if(!p||(p.text||'')!=='（？）')return;try{const src=ps.map(q=>{const o=Object.assign({},q);delete o.m;return o});const r=await api('/api/gap',{bid:bid,k:at,pieces:src});if(r&&r.text&&ps[at]===p&&p.text==='（？）'){snap();p.text=r.text;p.m=1;reAlt(bid);dirty();renderTelops([bid,at,0]);tlDraw()}}catch(e){}}
function tghost(bid,a,b){snap();const ps=TE[bid].pieces;let at=0;for(let i=0;i<ps.length;i++)if(ps[i].start<=a)at=i+1;ps.splice(at,0,{start:r3(fr30(a)),end:r3(fr30(b)),text:'（？）',speaker:0,m:1});dirty();renderTelops([bid,at,0]);fillGap(bid,at)}
function tadd(bid,t){snap();TE[bid].pieces.push({start:Math.round(t*100)/100,end:t+1,text:'',speaker:0});dirty();renderTelops()}
async function tsave(quiet){clearTimeout(saveT);if(!TE||!EDIT)return;const sr=await api('/api/telops',{data:TE,gen:TGEN});if(sr&&sr.stale){EDIT=false;await loadTelops();return}document.getElementById('tmsg').textContent=quiet?'保存済み':'保存しました'}
let RARM=0;async function treset(){const bt=document.getElementById('rbtn');if(!RARM){RARM=1;if(bt)bt.textContent='もう一度タップでリセット';setTimeout(()=>{RARM=0;if(bt)bt.textContent='リセット'},3000);return}RARM=0;if(bt)bt.textContent='リセット';await api('/api/telops',{reset:true});run('analyze');setTimeout(loadTelops,500)}
document.addEventListener('keydown',e=>{const tg=e.target.tagName;if((e.key==='Delete'||e.key==='Backspace')&&tg!=='TEXTAREA'&&tg!=='INPUT'&&tg!=='SELECT'&&SEL.size&&!CHIPCUR&&!NOCHIP){e.preventDefault();delSel(e.shiftKey);return}if((e.metaKey||e.ctrlKey)&&e.key==='z'&&e.target.tagName!=='TEXTAREA'){e.preventDefault();undo();return}const t=e.target.tagName;if(e.code==='Space'&&t!=='TEXTAREA'&&t!=='INPUT'&&t!=='SELECT'&&TE){e.preventDefault();togglePlay()}});
au.addEventListener('ended',pauseNow);
refresh();setInterval(()=>refresh(),1200);
let TLUH=0;try{TLUH=parseInt(localStorage.getItem('tluh')||'0')||0}catch(e){}
let TLMX=null,TLSK=0,TLZ=90,TLSEL=null,TLDRAG=null,TLON=false,TLRAF=0,TLHOV=null,TLONS=null,TLH={ruler:22,wave:52,lane:40,lane2:22,gap:4};try{const q=parseInt(localStorage.getItem('tllane')||'0');if(q>=26&&q<=76){TLH.lane=q;TLH.lane2=Math.round(q*0.55)}}catch(e){}
let FITK='';function fitDock(){const dk=document.getElementById('dock'),vw=document.getElementById('vwrap');if(!dk||!vw||dk.style.display==='none')return;const tlh=parseInt(getComputedStyle(document.documentElement).getPropertyValue('--tlh'))||0,k=window.innerHeight+'_'+tlh;if(k===FITK)return;FITK=k;const av=Math.max(200,window.innerHeight-100-tlh);const rest=Math.max(dk.scrollHeight-vw.offsetHeight,120);const h=Math.max(70,Math.min(640,av-rest-14));document.documentElement.style.setProperty('--vwh',h+'px')}
window.addEventListener('resize',()=>fitDock());
function tlLH(ln){return ln===2?TLH.lane2:TLH.lane}
function tlWaveTop(){return TLH.ruler+8+2*(TLH.lane+TLH.gap)+TLH.lane2+TLH.gap+8}
function tlOpen(){if(!TE||TLON)return;TLCL=false;TLON=true;document.getElementById('tl').classList.add('on');tlBuild();tlLoop()}
let TLCL=false;function tlClose(){TLCL=true;TLON=false;document.getElementById('tl').classList.remove('on');document.body.style.paddingBottom='';document.documentElement.style.removeProperty('--tlh');document.body.classList.remove('tlon');cancelAnimationFrame(TLRAF)}
function tlToggle(){TLON?tlClose():tlOpen()}
function tlLayout(){const L=[];let x=12;for(const g of segs()){const w=(g.a1-g.a0)*TLZ;L.push({bid:g.bid,a0:g.a0,a1:g.a1,x0:x,w});x+=w+22}return {L,W:x+60}}
function tlX(t,Lo){for(const s of Lo.L){if(t>=s.a0-1e-6&&t<=s.a1+1e-6)return s.x0+(t-s.a0)*TLZ}let b=Lo.L[0];for(const s of Lo.L){if(t>=s.a0)b=s}return b?b.x0+Math.max(0,Math.min(t-b.a0,b.a1-b.a0))*TLZ:0}
function tlT(x,Lo){for(const s of Lo.L){if(x>=s.x0-11&&x<=s.x0+s.w+11)return s.a0+Math.max(0,Math.min((x-s.x0)/TLZ,s.a1-s.a0))}return null}
function tlOnsets(){if(TLONS||!PK)return TLONS||[];const p=PK.peaks,r=PK.rate,o=[],n=p.length,db=new Float32Array(n);for(let k=0;k<n;k++)db[k]=20*Math.log10(Math.max(p[k]*p[k],1e-7));const top=Array.from(db).sort((a,b)=>a-b)[Math.floor(n*0.99)]||0,sil=top+4.4-42,lo=top+4.4-40;for(let k=8;k<n-3;k++){const cur=Math.max(db[k],db[k+1],db[k+2]);if(cur<lo)continue;let base=0;base=1e9;for(let j=1;j<=8;j++)if(db[k-j]<base)base=db[k-j];if(cur-base>=10&&db[k-1]<cur-6){o.push(k/r);k+=8}}let q=0;for(let k=6;k<n;k++){if(db[k]>sil){if(q>=6)o.push(k/r);q=0}else q++}o.sort((a,b)=>a-b);const u=[];for(const v of o)if(!u.length||v-u[u.length-1]>0.012)u.push(v);TLONS=u;return u}
let TLVS=null;
function tlVoiced(){if(TLVS||!PK)return TLVS||[];const p=PK.peaks,r=PK.rate,n=p.length,db=new Float32Array(n);for(let k=0;k<n;k++)db[k]=20*Math.log10(Math.max(p[k]*p[k],1e-7));const top=Array.from(db).sort((x,y)=>x-y)[Math.floor(n*0.99)]||0,th=top+4.4-40;const o=[];let s=-1,l=-1;
 for(let k=0;k<n;k++){if(db[k]>th){if(s<0)s=k;l=k}else if(s>=0&&k-l>12){if((l-s)/r>=0.2)o.push([s/r,(l+1)/r]);s=-1}}if(s>=0&&(l-s)/r>=0.2)o.push([s/r,(l+1)/r]);return TLVS=o}
function tlGhosts(bid){const b=TE[bid];if(!b||!PK)return [];const lo=b.c0!==undefined?b.c0:b.a0,hi=b.c1!==undefined?b.c1:b.a1,cov=[];
 b.pieces.forEach((p,i)=>{if(p.deleted||p.cut)return;const e=pend(bid,i),ch=(p.text||'').replace(/\s/g,'').length;cov.push([p.start-0.1,Math.min(e,p.start+0.3+ch*0.2)+0.05])});
 const out=[];for(const [s,e] of tlVoiced()){if(e<=lo||s>=hi)continue;let cur=Math.max(s,lo);const z=Math.min(e,hi);const cs=cov.filter(c=>c[1]>cur&&c[0]<z).sort((x,y)=>x[0]-y[0]);for(const c of cs){if(c[0]-cur>=0.2)out.push([cur,c[0]]);cur=Math.max(cur,c[1])}if(z-cur>=0.2)out.push([cur,z])}return out}
async function tlWaveFit(){if(!TE)return;let bias=0;try{const r=await api('/api/wavebias');if(typeof r.bias==='number')bias=r.bias}catch(e){}if(!PK){document.getElementById('tmsg').textContent='波形なし';return}TLONS=null;const on=tlOnsets();if(!on.length)return;snap();let cnt=0;
 for(const bid of Object.keys(TE)){const b=TE[bid],ps=b.pieces,lo=b.c0!==undefined?b.c0:b.a0;
  for(let i=0;i<ps.length;i++){const p=ps[i];if(p.deleted||p.cut||p.m||p.ov||p.speaker<0||(p.text||'').startsWith('※'))continue;
   let best=null;for(const o of on){if(o<p.start-0.25)continue;if(o>p.start+0.15)break;const d=Math.abs(o-p.start);if(best===null||d<best[0])best=[d,o]}
   if(!best)continue;const t=Math.max(lo,Math.round((best[1]+bias)*100)/100);if(Math.abs(t-p.start)<0.02)continue;
   let prev=null;for(let j=i-1;j>=0;j--)if(ps[j].speaker>=0&&!ps[j].ov&&!ps[j].deleted&&!ps[j].cut){prev=ps[j];break}
   if(prev&&t<prev.start+0.12)continue;if(t>pend(bid,i)-0.12)continue;p.start=t;cnt++}}
 if(cnt){dirty();renderTelops();if(TLON)tlDraw()}document.getElementById('tmsg').textContent='波形調整 '+cnt+'件'}
function tlBuild(){const el=document.getElementById('tl');
 el.innerHTML=''
  +'<div id="tlgrip"><input type="range" id="tlth" min="26" max="76" step="2"></div>'
  +'<div class="tlscroll" id="tls"><div id="tlspc"></div><canvas id="tlc"></canvas></div>'
  +'';
 const th=document.getElementById('tlth');th.value=TLH.lane;th.oninput=()=>{TLH.lane=parseInt(th.value);TLH.lane2=Math.round(TLH.lane*0.55);try{localStorage.setItem('tllane',String(TLH.lane))}catch(er){}tlDraw()};th.onpointerdown=e=>e.stopPropagation();const gp=document.getElementById('tlgrip');gp.onpointerdown=e=>{gp.setPointerCapture(e.pointerId);gp._d=1};gp.onpointermove=e=>{if(!gp._d)return;TLUH=Math.round(Math.max(150,Math.min(window.innerHeight-220,window.innerHeight-e.clientY)));tlDraw()};gp.onpointerup=gp.onpointercancel=()=>{gp._d=0;try{localStorage.setItem('tluh',String(TLUH))}catch(e){}};gp.ondblclick=()=>{TLUH=0;try{localStorage.removeItem('tluh')}catch(e){}tlDraw()};
 const c=document.getElementById('tlc');c.ondblclick=e=>{const h=tlHit(e);if(h.bid&&h.i!==undefined&&!h.note)chipDbl(h.bid,h.i)};c.onpointerdown=tlDown;c.onpointermove=tlMove;c.onpointerup=tlUp;c.onpointercancel=tlUp;c.onpointerleave=()=>{if(!TLDRAG){TLHOV=null;TLMX=null;if(au.paused){try{vid().currentTime=Math.max(au.currentTime+OFF,0)}catch(er){}}}};
 const sc=document.getElementById('tls');sc.onscroll=()=>tlDraw();sc.onwheel=e=>{if(e.ctrlKey||e.metaKey){e.preventDefault();tlZoomAt(Math.exp(-e.deltaY*0.012),e.clientX)}};let z0=0;el.addEventListener('gesturestart',e=>{e.preventDefault();z0=TLZ});el.addEventListener('gesturechange',e=>{e.preventDefault();tlZoomTo(z0*e.scale,window.innerWidth/2)});tlDraw()}
function tlZoomTo(z,cx){const sc=document.getElementById('tls'),Lo=tlLayout(),r=sc.getBoundingClientRect(),t=tlT(sc.scrollLeft+cx-r.left,Lo);TLZ=Math.max(30,Math.min(1200,z));tlDraw();if(t!==null)sc.scrollLeft=Math.max(0,tlX(t,tlLayout())-(cx-r.left));tlDraw()}
function tlZoomAt(f,cx){tlZoomTo(TLZ*f,cx)}
function tlZoom(d){const sc=document.getElementById('tls'),t=tlT(sc.scrollLeft+sc.clientWidth/2,tlLayout());TLZ=Math.max(30,Math.min(1200,Math.round(TLZ*(d>0?1.35:1/1.35))));tlDraw();if(t!==null)sc.scrollLeft=Math.max(0,tlX(t,tlLayout())-sc.clientWidth/2);tlDraw()}
function tlLoop(){if(!TLON)return;tlDraw(true);TLRAF=requestAnimationFrame(tlLoop)}
function tlSel(){if(!TLSEL||!TE[TLSEL[0]])return null;return TE[TLSEL[0]].pieces[TLSEL[1]]||null}
function tlLane(p){return p.speaker<0||p.ov||(p.text||'').startsWith('※')?2:(p.speaker>=1?1:0)}
function tlLive(bid){const ps=TE[bid].pieces;return ps.map((p,i)=>[p,i]).filter(([p])=>p.speaker>=0&&!p.ov&&!p.deleted&&!p.cut&&!(p.text||'').startsWith('※')).sort((a,b)=>a[0].start-b[0].start)}
function snpF(t){return Math.round((t+OFF)*30)/30-OFF}
function tlDraw(follow){const c=document.getElementById('tlc'),sc=document.getElementById('tls');if(!c||!sc||!TE)return;const Lo=tlLayout(),dpr=window.devicePixelRatio||1;
 TLH.wave=52;const H0=tlWaveTop()+52+8,ex=TLUH?TLUH-18-H0:0;if(ex>0)TLH.wave=52+ex;const H=tlWaveTop()+TLH.wave+8,cw=sc.clientWidth;document.getElementById('tlspc').style.cssText='width:'+Lo.W+'px;height:1px';const dock=document.getElementById('tl');const dh=TLUH?Math.max(TLUH,120):H+18;dock.style.height=dh+'px';document.body.style.paddingBottom=(dh+4)+'px';document.documentElement.style.setProperty('--tlh',(dh+4)+'px');fitDock();document.body.classList.add('tlon');
 const t=au.currentTime,cx=tlX(t,Lo);
 if(follow&&!au.paused&&!TLDRAG){if(cx<sc.scrollLeft+40||cx>sc.scrollLeft+cw-100)sc.scrollLeft=Math.max(0,cx-cw*0.3)}
 const sx=sc.scrollLeft;
 if(c.width!==Math.round(cw*dpr)||c.height!==Math.round(H*dpr)){c.width=Math.round(cw*dpr);c.height=Math.round(H*dpr);c.style.width=cw+'px';c.style.height=H+'px'}
 c.style.position='sticky';c.style.left='0';c.style.top='0';c.style.display='block';
 const g=c.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);g.fillStyle='#101014';g.fillRect(0,0,cw,H);g.translate(-sx,0);
 const laneY=k=>TLH.ruler+8+Math.min(k,2)*(TLH.lane+TLH.gap);
 for(const s of Lo.L){if(s.x0+s.w<sx-20||s.x0>sx+cw+20)continue;
  g.fillStyle='#1a1b21';g.fillRect(s.x0,TLH.ruler,s.w,H-TLH.ruler);g.font='600 12px -apple-system,sans-serif';g.fillStyle='#9aa0ae';g.fillText(bname(s.bid),s.x0+8,13);g.fillStyle='rgba(255,214,10,.85)';g.fillRect(s.x0-1,0,4,TLH.ruler);g.fillRect(s.x0+s.w-3,0,4,TLH.ruler);g.fillStyle='#9aa0ae';
  {const stp=Math.max(1,Math.ceil(3.2/(TLZ/30))),b0=(s.a0+OFF)*30,f0=Math.max(Math.ceil(b0),Math.floor((sx-s.x0-20)/TLZ*30+b0)),f1=Math.min(Math.floor((s.a1+OFF)*30),Math.ceil((sx+cw-s.x0+20)/TLZ*30+b0));for(let f=Math.ceil(f0/stp)*stp;f<=f1;f+=stp){const x=Math.round(s.x0+(f/30-OFF-s.a0)*TLZ)+.5,m5=f%(stp*5)===0;g.fillStyle=m5?'#9097a8':'#646879';g.fillRect(x,TLH.ruler-(m5?9:5),1,m5?9:5)}}
  const sf=TLZ>=500?5:(TLZ>=200?15:(TLZ>=60?30:150));for(let f=Math.ceil((s.a0+OFF)*30/sf)*sf;f<=(s.a1+OFF)*30;f+=sf){const u=f/30,x=Math.round(s.x0+(u-OFF-s.a0)*TLZ)+.5;g.fillStyle='#9097a8';g.fillRect(x,TLH.ruler-12,1,12);if((TLZ>=60||Math.abs(u%10)<1e-6)&&x-s.x0>46){g.fillStyle='#8b91a0';g.font='10px ui-monospace,Menlo,monospace';g.fillText(TLZ>=200?tcf(u):fmt(u).replace(/\.00$/,''),x+3,TLH.ruler-14)}}
  if(PK){const r=PK.rate,wt=tlWaveTop(),mid=wt+TLH.wave-2,hh=TLH.wave-6,x0=Math.max(0,sx-s.x0-2),x1=Math.min(s.w,sx+cw-s.x0+2),sw=1/dpr;
   g.fillStyle='#1f3157';g.fillRect(s.x0,wt,s.w,TLH.wave);
   const xs=Math.floor(x0*dpr),xe=Math.ceil(x1*dpr),N=Math.max(0,xe-xs),A=new Float32Array(N),R=new Uint8Array(N);
   for(let i=0;i<N;i++){const xx=(xs+i)/dpr,ta=s.a0+xx/TLZ,tb=s.a0+(xx+sw)/TLZ;let m=0;for(let k=Math.floor(ta*r);k<=Math.floor(tb*r);k++){const v=(PKD||PK).peaks[k];if(v>m)m=v}
    const lin=m*m;A[i]=Math.max(0,Math.min(1,(20*Math.log10(Math.max(lin,1e-6))+60)/60));R[i]=lin>=0.96?1:0}
   const B=new Float32Array(N),K=[1,3,5,3,1];for(let i=0;i<N;i++){let t=0,w=0;for(let j=-2;j<=2;j++){const q=i+j;if(q>=0&&q<N){t+=A[q]*K[j+2];w+=K[j+2]}}B[i]=Math.max(t/w,A[i]*0.92)}
   const path=(mask)=>{g.beginPath();let on=false,x0_=0;const flush=(i)=>{if(on){g.lineTo(s.x0+(xs+i)/dpr,mid);g.lineTo(s.x0+x0_/dpr,mid);g.closePath();on=false}};
    for(let i=0;i<N;i++){const ok=!mask||R[i]||(i>0&&R[i-1])||(i<N-1&&R[i+1]);if(!ok){flush(i);continue}if(!on){on=true;x0_=xs+i;g.moveTo(s.x0+(xs+i)/dpr,mid)}g.lineTo(s.x0+(xs+i)/dpr,mid-B[i]*hh)}flush(N)};
   g.fillStyle='#8ea3d4';path(false);g.fill();
   g.fillStyle='#e0483c';path(true);g.fill();
   }
  const lv=tlLive(s.bid);
  (TE[s.bid].notes||[]).forEach((n,ni)=>{if(n.deleted||!(n.text||'').trim())return;const x1=tlX(n.start,Lo),x2=Math.max(tlX(n.end,Lo),x1+3);if(x2<sx-5||x1>sx+cw+5)return;const y=laneY(2),lh=tlLH(2);
   g.fillStyle='#8a6fc0';g.fillRect(x1,y,x2-x1-1,lh);g.save();g.beginPath();g.rect(x1,y,x2-x1-1,lh);g.clip();g.fillStyle='#fff';g.font='600 13px -apple-system,"Hiragino Sans",sans-serif';g.fillText('※'+n.text,x1+6,y+lh/2+5);g.restore();if(TLNS&&TLNS[0]===s.bid&&TLNS[1]===ni){g.strokeStyle='#fff';g.lineWidth=2;g.strokeRect(x1+1,y+1,x2-x1-3,lh-2)}});
  TE[s.bid].pieces.forEach((p,i)=>{if(p.deleted||p.cut)return;const e=pend(s.bid,i);const x1=tlX(snpF(p.start),Lo),x2=Math.max(tlX(snpF(e),Lo),x1+3);if(x2<sx-5||x1>sx+cw+5)return;const ln=tlLane(p),y=laneY(ln);
   const col=ln===2&&!p.ov?'#a08e4c':(p.speaker===1?'#3f7fd0':'#d85f86');const lh=tlLH(ln);g.fillStyle=col;g.fillRect(x1,y,x2-x1-1,lh);
   const sel=TLSEL&&TLSEL[0]===s.bid&&TLSEL[1]===i;if(sel){g.strokeStyle='#fff';g.lineWidth=2;g.strokeRect(x1+1,y+1,x2-x1-3,lh-2)}
   g.save();g.beginPath();g.rect(x1,y,x2-x1-1,lh);g.clip();g.fillStyle='#fff';g.font='600 13px -apple-system,"Hiragino Sans",sans-serif';const tx=(p.text||'').split('\n');{const two=!!tx[1]&&ln!==2&&lh>=36;g.fillText(two?tx[0]:tx.join(''),x1+6,two?y+lh/2-3:y+lh/2+5);if(two)g.fillText(tx[1],x1+6,y+lh/2+14)}g.restore()});
  {const y=laneY(2),lh=tlLH(2);g.save();g.setLineDash([4,3]);g.strokeStyle='#d8c46a';g.lineWidth=1;g.font='600 11px -apple-system,"Hiragino Sans",sans-serif';for(const [ga,gb] of tlGhosts(s.bid)){const x1=tlX(ga,Lo),x2=Math.max(tlX(gb,Lo),x1+4);if(x2<sx-5||x1>sx+cw+5)continue;g.fillStyle='rgba(216,196,106,.16)';g.fillRect(x1,y,x2-x1-1,lh);g.strokeRect(x1+.5,y+.5,x2-x1-2,lh-1);g.fillStyle='#d8c46a';if(x2-x1>26)g.fillText('（？）',x1+4,y+lh/2+4)}g.restore()}
  lv.forEach(([p,i],k)=>{const x=tlX(snpF(p.start),Lo),hot=(TLHOV&&TLHOV[0]===s.bid&&TLHOV[1]===i)||(TLDRAG&&TLDRAG.bid===s.bid&&TLDRAG.i===i)||(TLSEL&&TLSEL[0]===s.bid&&TLSEL[1]===i);
   g.fillStyle=hot?'#ffd60a':'rgba(255,255,255,.55)';g.fillRect(Math.round(x),TLH.ruler,hot?3:1,H-TLH.ruler);
   if(hot&&k>0){const pr=lv[k-1][0],yp=laneY(tlLane(pr)),yc=laneY(tlLane(p));g.fillStyle='#ffd60a';g.fillRect(Math.round(x)-9,yp,9,3);g.fillRect(Math.round(x)-9,yp+TLH.lane-3,9,3);g.fillRect(Math.round(x),yc,9,3);g.fillRect(Math.round(x),yc+TLH.lane-3,9,3)}})}
 if(TLMX!==null&&!TLDRAG){g.fillStyle='rgba(255,255,255,.5)';g.fillRect(Math.round(TLMX),0,1,H)}
 g.strokeStyle='#ffd60a';g.lineWidth=2;g.beginPath();g.moveTo(cx,0);g.lineTo(cx,H);g.stroke();
 if(TLDRAG&&TLDRAG.blk&&TLDRAG.pend!==null){g.strokeStyle='#ffd60a';g.lineWidth=2;g.beginPath();g.moveTo(TLDRAG.px,0);g.lineTo(TLDRAG.px,H);g.stroke();}
 if(TLDRAG&&TLDRAG.bid&&TLDRAG.i!==undefined){const p=TE[TLDRAG.bid].pieces[TLDRAG.i],x=tlX(p.start,Lo),df=Math.round(((TLDRAG.kind==='e'?p.end:p.start)-TLDRAG.t0)*30);}
 }
function tlHit(e){const c=document.getElementById('tlc'),sc=document.getElementById('tls'),r=c.getBoundingClientRect(),x=e.clientX-r.left+sc.scrollLeft,y=e.clientY-r.top,Lo=tlLayout();
 {const y2=TLH.ruler+8+2*(TLH.lane+TLH.gap);if(y>=y2&&y<=y2+TLH.lane2){for(const s of Lo.L)for(const [ga,gb] of tlGhosts(s.bid)){const x1=tlX(ga,Lo),x2=Math.max(tlX(gb,Lo),x1+4);if(x>=x1&&x<=x2){let free=true;for(const q of TE[s.bid].pieces){if(q.deleted||q.cut||tlLane(q)!==2)continue;const q1=tlX(q.start,Lo),q2=tlX(pend(s.bid,TE[s.bid].pieces.indexOf(q)),Lo);if(Math.abs(x-q1)<=7||Math.abs(x-q2)<=7)free=false}if(free)return {ghost:true,bid:s.bid,a:ga,b:gb,x,Lo}}}}}
 if(y<TLH.ruler){for(const s of Lo.L){if(Math.abs(x-s.x0)<=8)return {blk:s.bid,side:'l',x,Lo};if(Math.abs(x-(s.x0+s.w))<=8)return {blk:s.bid,side:'r',x,Lo}}}
 if(y>=TLH.ruler){const y2=TLH.ruler+8+2*(TLH.lane+TLH.gap);if(y>=y2&&y<=y2+TLH.lane2){for(const s of Lo.L){const ps=TE[s.bid].pieces;for(let i=0;i<ps.length;i++){const p=ps[i];if(p.deleted||p.cut||tlLane(p)!==2)continue;const x1=tlX(p.start,Lo),x2=tlX(pend(s.bid,i),Lo);if(Math.abs(x-x1)<=7)return {bid:s.bid,i,edge:true,kind:'s',x,Lo};if(Math.abs(x-x2)<=7)return {bid:s.bid,i,edge:true,kind:'e',x,Lo}}}
  for(const s of Lo.L){const ns=TE[s.bid].notes||[];for(let ni=0;ni<ns.length;ni++){const n=ns[ni];if(n.deleted)continue;const x1=tlX(n.start,Lo),x2=tlX(n.end,Lo);if(Math.abs(x-x1)<=7)return {note:1,bid:s.bid,ni,part:'s',x,Lo};if(Math.abs(x-x2)<=7)return {note:1,bid:s.bid,ni,part:'e',x,Lo};if(x>x1&&x<x2)return {note:1,bid:s.bid,ni,part:'b',x,Lo}}}}
 for(const s of Lo.L){const lv=tlLive(s.bid);
  for(let k=0;k<lv.length;k++){const [p,i]=lv[k];const xs=tlX(p.start,Lo);if(Math.abs(x-xs)<=7)return {bid:s.bid,i,edge:true,x,Lo}}
  const ps=TE[s.bid].pieces;for(let i=0;i<ps.length;i++){const p=ps[i];if(p.deleted||p.cut)continue;const ln=tlLane(p),y0=TLH.ruler+8+Math.min(ln,2)*(TLH.lane+TLH.gap);if(y<y0||y>y0+tlLH(ln))continue;const x1=tlX(p.start,Lo),x2=Math.max(tlX(pend(s.bid,i),Lo),x1+3);if(x>=x1&&x<=x2)return {bid:s.bid,i,edge:false,x,Lo}}}}
 return {x,Lo,y}}
let TLNS=null;
function tlDown(e){const h=tlHit(e);if(h.ghost){snap();const ps=TE[h.bid].pieces;let at=0;for(let i=0;i<ps.length;i++)if(ps[i].start<=h.a)at=i+1;const np={start:r3(fr30(h.a)),end:r3(fr30(h.b)),text:'（？）',speaker:0,m:1};ps.splice(at,0,np);TLSEL=[h.bid,at];dirty();renderTelops([h.bid,at,0]);tlDraw();fillGap(h.bid,at);return}if(!h.note)TLNS=null;if(h.note){const n=TE[h.bid].notes[h.ni];TLSEL=null;TLNS=[h.bid,h.ni];snap();if(au.paused&&h.part==='b')seek(n.start+0.01);TLDRAG={note:true,bid:h.bid,ni:h.ni,part:h.part,x0:h.x,s0:n.start,e0:n.end}}else if(h.blk){const b=TE[h.blk];TLDRAG={blk:h.blk,side:h.side,Lo:h.Lo,seg:h.Lo.L.find(q=>q.bid===h.blk),pend:null}}else if(h.bid){const was=!!(TLSEL&&TLSEL[0]===h.bid&&TLSEL[1]===h.i);TLSEL=[h.bid,h.i];const p=TE[h.bid].pieces[h.i];if(h.edge){snap();TLDRAG={bid:h.bid,i:h.i,Lo:h.Lo,t0:h.kind==='e'?pend(h.bid,h.i):p.start,kind:h.kind,was:was,p0:p.start,e0:p.end}}else{if(au.paused)seek(p.start+0.01);if(p.speaker>=0&&!(p.text||'').startsWith('※'))TLDRAG={lane:true,bid:h.bid,i:h.i,Lo:h.Lo,y0:e.clientY,moved:false,was:was,p0:p.start,sp0:p.speaker,x0:h.x,s0:p.start,e0:p.end};else{snap();TLDRAG={move:true,bid:h.bid,i:h.i,Lo:h.Lo,x0:h.x,s0:p.start,e0:pend(h.bid,h.i)}}}}
 else{const t=tlT(h.x,h.Lo);if(t!==null){seek(t);TLDRAG={scrub:true,Lo:h.Lo}}}e.target.setPointerCapture(e.pointerId);tlDraw()}
function tlMove(e){const c=document.getElementById('tlc'),sc=document.getElementById('tls'),r=c.getBoundingClientRect(),x=e.clientX-r.left+sc.scrollLeft;
 if(!TLDRAG){TLMX=x;if(au.paused){const tt=tlT(x,tlLayout());if(tt!==null&&Date.now()-TLSK>70){TLSK=Date.now();try{vid().currentTime=Math.max(tt+OFF,0)}catch(er){}}}const h=tlHit(e);TLHOV=h.bid&&h.edge&&!h.kind?[h.bid,h.i]:null;c.style.cursor=(h.bid&&h.edge)||h.blk||(h.note&&h.part!=='b')?'col-resize':'default';return}
 if(TLDRAG.note){const bid=TLDRAG.bid,n=TE[bid].notes[TLDRAG.ni],d=(x-TLDRAG.x0)/TLZ,lv=tlLive(bid),B=lv.map(([q])=>q.start);if(lv.length){const lp=lv[lv.length-1][1];B.push(pend(bid,lp))}
 const near=t=>{let k=0,bd=1e9;B.forEach((v,j)=>{const dd=Math.abs(v-t);if(dd<bd){bd=dd;k=j}});return k};
 if(B.length>1){if(TLDRAG.part==='s'){const k=Math.min(near(TLDRAG.s0+d),near(n.end)-1);n.start=B[Math.max(0,k)]}else if(TLDRAG.part==='e'){const k=Math.max(near(TLDRAG.e0+d),near(n.start)+1);n.end=B[Math.min(B.length-1,k)]}
  else{if(TLDRAG.i0===undefined){TLDRAG.i0=near(TLDRAG.s0);TLDRAG.i1=near(TLDRAG.e0)}const sh=near(TLDRAG.s0+d)-TLDRAG.i0,lo=Math.max(0,TLDRAG.i0+sh),hi=lo+(TLDRAG.i1-TLDRAG.i0);if(hi<=B.length-1){n.start=B[lo];n.end=B[hi]}}}
 n.m=1;tlDraw();return}
 if(TLDRAG.move){const q=TE[TLDRAG.bid].pieces[TLDRAG.i],dd=Math.round((x-TLDRAG.x0)/TLZ*30)/30;q.start=Math.round((TLDRAG.s0+dd)*1000)/1000;q.end=Math.round((TLDRAG.e0+dd)*1000)/1000;q.m=1;tlDraw();return}
 if(TLDRAG.lane){const q=TE[TLDRAG.bid].pieces[TLDRAG.i];if(!TLDRAG.hmode){const dx_=x-TLDRAG.x0,dy_=e.clientY-TLDRAG.y0;if(Math.abs(dx_)>6&&Math.abs(dx_)>Math.abs(dy_))TLDRAG.hmode='h';else if(Math.abs(dy_)>6)TLDRAG.hmode='v'}
  if(TLDRAG.hmode==='h'){if(q.ov){if(!TLDRAG.hs){snap();TLDRAG.hs=1}const dd=Math.round((x-TLDRAG.x0)/TLZ*30)/30;q.start=Math.round((TLDRAG.s0+dd)*1000)/1000;q.end=Math.round((TLDRAG.e0+dd)*1000)/1000;q.m=1;TLDRAG.moved=true;tlDraw()}return}
  const yy=e.clientY-r.top,ln=yy<TLH.ruler+8+TLH.lane+TLH.gap/2?0:(yy<TLH.ruler+8+2*(TLH.lane+TLH.gap)-TLH.gap/2?1:2);if(!TLDRAG.moved&&Math.abs(e.clientY-TLDRAG.y0)<6)return;const cur=q.ov?2:q.speaker;if(cur!==ln){if(!TLDRAG.moved)snap();TLDRAG.moved=true;if(ln===2){if(!q.ov){q.speaker=TLDRAG.sp0;q.ov=1;q.end=Math.round(pend(TLDRAG.bid,TLDRAG.i)*1000)/1000}else q.speaker=TLDRAG.sp0}else{delete q.ov;q.speaker=ln}q.m=1;tlDraw()}return}
 if(TLDRAG.blk){const s=TLDRAG.seg,b=TE[TLDRAG.blk],lv=tlLive(TLDRAG.blk);let t=s.a0+(x-s.x0)/TLZ;t=Math.round(t*30)/30;t=Math.max(0,t);if(TLDRAG.side==='l')t=Math.min(t,s.a1-0.1);else t=Math.max(t,s.a0+0.1);TLDRAG.pend=t;TLDRAG.px=x;tlDraw();return}
 if(TLDRAG.scrub){const t=tlT(x,TLDRAG.Lo);if(t!==null)seek(t);return}
 const bid=TLDRAG.bid,p=TE[bid].pieces[TLDRAG.i];let t=tlT(x,TLDRAG.Lo);if(t===null&&TLDRAG.kind)t=TLDRAG.Lo.L.find(q=>q.bid===bid).a0+(x-TLDRAG.Lo.L.find(q=>q.bid===bid).x0)/TLZ;if(t===null)return;
 const on=tlOnsets();let best=null;for(const o of on){const d=Math.abs(o-t)*TLZ;if(d<=6&&(best===null||d<best[0]))best=[d,o]}
 t=best?best[1]:Math.round(t*30)/30;
 p.m=1;if(TLDRAG.kind==='s'){p.start=Math.round(Math.min(t,(p.end||t+1)-0.1)*1000)/1000;TLDRAG.t0=TLDRAG.t0;tlDraw();return}
 if(TLDRAG.kind==='e'){p.end=Math.round(Math.max(t,p.start+0.1)*1000)/1000;tlDraw();return}
 const lv=tlLive(bid),pos=lv.findIndex(([q])=>q===p);const lo=pos>0?lv[pos-1][0].start+0.05:TE[bid].a0-0.5,hi=pos>=0&&pos<lv.length-1?lv[pos+1][0].start-0.05:TE[bid].a1;p.start=Math.round(Math.max(lo,Math.min(hi,t))*1000)/1000;tlDraw()}
function tlUp(e){const d=TLDRAG;TLDRAG=null;if(!d)return;if(d.note||d.move){dirty();tlDraw();return}if(d.kind==='e'){const b=TE[d.bid],p=b.pieces[d.i],cur=b.c1!==undefined?b.c1:b.a1;if(p.end>cur)b.c1=p.end;dirty();tlDraw();return}if(d.scrub){if(au.paused)playNow();return}if(d.bid&&!d.kind&&d.was&&!d.moved&&Math.abs(TE[d.bid].pieces[d.i].start-d.p0)<1e-6){const q=TE[d.bid].pieces[d.i];seek(q.start+0.01);playNow();return}if(d.blk){if(d.pend!==null){snap();tlCut(d.blk,d.side,d.pend);dirty()}tlDraw();return}dirty()}
function tlCut(bid,side,t){const b=TE[bid];if(side==='l')b.c0=t;else b.c1=t;tlNorm(bid)}
function tlNorm(bid){const b=TE[bid];if(b.c0===undefined&&b.c1===undefined)return;const lo=b.c0!==undefined?b.c0:b.a0,hi=b.c1!==undefined?b.c1:b.a1;
 for(const p of b.pieces){if(p.cb){delete p.cut;delete p.cb}}
 for(const p of b.pieces){if(p.speaker<0||p.deleted||p.ov)continue;const e=pend(bid,b.pieces.indexOf(p));
  if(e<=lo+1e-6||p.start>=hi-1e-6){if(!p.cut){p.cut=1;p.cb=1}}}
 const lv=tlLive(bid);if(lv.length){if(lv[0][0].start<lo)lv[0][0].start=Math.round(lo*1000)/1000;const q=lv[lv.length-1][0];if(q.end>hi||pend(bid,b.pieces.indexOf(q))>hi)q.end=Math.round(hi*1000)/1000}
 for(const p of b.pieces){if(p.speaker<0&&!p.deleted){if(p.end<=lo||p.start>=hi){if(!p.cut){p.cut=1;p.cb=1}}else{if(p.start<lo)p.start=Math.round(lo*1000)/1000;if(p.end>hi)p.end=Math.round(hi*1000)/1000}}}}
function tlAround(){const p=tlSel();if(!p)return;playRange(p.start-1.2,p.start+1.2)}
function tlText(v){const p=tlSel();if(!p)return;p.text=v.replace(/↵/g,'\n');dirty();tlDraw()}
function tlSpk(v){const p=tlSel();if(!p)return;snap();p.speaker=parseInt(v);dirty();tlDraw()}
function tlDel(cut){const p=tlSel();if(!p)return;const bid=TLSEL[0],i=TLSEL[1],ps=TE[bid].pieces;if(cut){snap();p.deleted=true;TLSEL=null;dirty();renderTelops();tlDraw();return}
 if(p.speaker>=0&&!p.ov&&!(p.text||'').startsWith('※')){const k=tnext(ps,i,-1);if(k>=0){tmerge(bid,k,undefined,i);TLSEL=[bid,k];tlDraw();return}}
 snap();ps.splice(i,1);TLSEL=null;dirty();renderTelops();tlDraw()}

</script><div class="btov" id="bt"></div><div class="btov" id="wd"></div><div class="tlov" id="tl"></div>
<script>
let BT=null;

let WD=[];
async function wdOpen(){const r=await api('/api/words');WD=(r.groups||[]).map(g=>({to:g.to,from:g.from.join('、')}));if(!WD.length)WD.push({to:'',from:''});wdRender();document.getElementById('wd').classList.add('on')}
function wdRender(){const el=document.getElementById('wd');let h='<h2>表記統一</h2><div class="wdgrid"><div class="wdh">統一後</div><div class="wdh">表記ゆれ</div><div></div>';
 WD.forEach((g,i)=>{h+='<input value="'+esc(g.to)+'" onchange="WD['+i+'].to=this.value;wdSave()"><input value="'+esc(g.from)+'" onchange="WD['+i+'].from=this.value;wdSave()"><button onclick="WD.splice('+i+',1);wdSave();wdRender()">削除</button>'});
 h+='</div><div class="wdbt"><button onclick="WD.push({to:\'\',from:\'\'});wdRender()">追加</button><button onclick="wdApply()">適用</button><button onclick="document.getElementById(\'wd\').classList.remove(\'on\')">閉じる</button></div>';el.innerHTML=h}
function wdGroups(){return WD.map(g=>({to:g.to,from:String(g.from).split(/[,、，\s]+/).filter(Boolean)}))}
async function wdSave(){await api('/api/words',{groups:wdGroups()})}
async function wdApply(){await wdSave();if(!TE){document.getElementById('wd').classList.remove('on');return}const rules=[];for(const g of wdGroups())if(g.to)for(const x of g.from)if(x!==g.to)rules.push([x,g.to]);rules.sort((a,b)=>b[0].length-a[0].length);let n=0;snap();
 for(const bid of Object.keys(TE))for(const p of TE[bid].pieces){if(p.deleted||!p.text)continue;let t=p.text;for(const [a,b] of rules)t=t.split(a).join(b);if(t!==p.text){p.text=t;n++}}
 document.getElementById('wd').classList.remove('on');if(n){dirty();renderTelops();if(TLON)tlDraw()}document.getElementById('tmsg').textContent='表記統一 '+n+'件'}
async function btOpen(){const r=await api('/api/breaktest');BT={items:r.samples||[],k:0,done:r.done||0,saved:0};btLoad();document.getElementById('bt').classList.add('on')}
function btClose(){document.getElementById('bt').classList.remove('on');BT=null}
function btLoad(){if(!BT)return;const it=BT.items[BT.k];if(!it){btRender();return}BT.text=it.text;const ch=[...it.text];BT.chars=ch;BT.marks=new Array(Math.max(ch.length-1,0)).fill(0);
 let pos=0;it.telops.forEach((t,ti)=>{t.forEach((l,li)=>{pos+=[...l].length;if(pos-1<BT.marks.length&&pos>0){BT.marks[pos-1]=(li<t.length-1)?1:2}})});btRender()}
function btTog(i){BT.marks[i]=(BT.marks[i]+1)%3;btRender()}
function btTelops(){const out=[[]];let line='';BT.chars.forEach((c,i)=>{line+=c;const m=BT.marks[i];if(m===1){out[out.length-1].push(line);line=''}else if(m===2||i===BT.chars.length-1){out[out.length-1].push(line);line='';if(i<BT.chars.length-1)out.push([])}});return out.filter(t=>t.length)}
function btRender(){const el=document.getElementById('bt');if(!BT)return;const n=BT.items.length;
 if(BT.k>=n){el.innerHTML='<h2>終了</h2><div class="btbar"><button class="p" onclick="btClose()">✕</button></div>';return}
 let src='';BT.chars.forEach((c,i)=>{src+='<span class="btc">'+esc(c)+'</span>';if(i<BT.chars.length-1){const m=BT.marks[i];src+='<span class="btg g'+m+'" onclick="btTog('+i+')">'+(m===1?'↵':(m===2?'┃':''))+'</span>'}});
 const prev=btTelops().map(t=>'<div class="btcard">'+t.map(esc).join('<br>')+'</div>').join('');
 el.innerHTML='<h2>改行テスト　'+(BT.k+1)+' / '+n+'</h2><div class="btsrc">'+src+'</div><div class="btprev">'+prev+'</div><div class="btbar"><button class="p" onclick="btNext(true)">決定</button><button onclick="btNext(false)">スキップ</button><button onclick="btClose()">✕</button></div>'}
async function btNext(save){if(!BT)return;if(save){await api('/api/breaktest',{telops:btTelops()});BT.saved++}BT.k++;btLoad()}
document.addEventListener('keydown',e=>{if(!BT)return;if(e.key==='Escape'){btClose()}else if(e.key==='Enter'&&!e.isComposing){e.preventDefault();if(BT.k<BT.items.length)btNext(true);else btClose()}},true);
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
