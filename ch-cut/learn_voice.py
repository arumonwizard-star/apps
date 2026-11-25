"""Who is talking, by the sound of the voice alone (one microphone / a mono recording).
A linear model of the voice (spectral shape + pitch of each 0.3 s of speech) is trained on the stretches whose speaker the person
has seen and corrected in every project (チ = 0, ホ = 1); it starts from voice_base.npz that comes with the app and grows with
each project that is edited (presets/voice_local.npz).  The model is stored in presets/voice_model.json."""
import json
import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BASE = HERE / "voice_base.npz"
LOCAL = HERE / "presets" / "voice_local.npz"
MODEL = HERE / "presets" / "voice_model.json"
_CACHE = {}


def _feat(F, f0):
    f0 = np.asarray(f0, dtype=float)
    med = np.nanmedian(f0) if np.isfinite(f0).any() else 5.0
    f = np.where(np.isnan(f0), med, f0)[:, None]
    return np.hstack([F, f * 3.0])


def train(X, y):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    m0, m1 = Z[y == 0].mean(0), Z[y == 1].mean(0)
    C = np.cov(np.vstack([Z[y == 0] - m0, Z[y == 1] - m1]).T)
    C = C + np.eye(len(C)) * np.trace(C) / len(C) * 0.1
    w = np.linalg.solve(C, m1 - m0)
    b = -float(w @ (m0 + m1) / 2)
    return {"mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist(), "b": b, "n": int(len(y))}


def _load_sets():
    Xs, ys = [], []
    for f in (BASE, LOCAL):
        try:
            d = np.load(f, allow_pickle=True)
            Xs.append(d["X"]); ys.append(d["y"])
        except Exception:   # noqa: BLE001
            pass
    return (np.vstack(Xs), np.concatenate(ys)) if Xs else (None, None)


def model():
    try:
        mt = MODEL.stat().st_mtime if MODEL.exists() else 0
        if _CACHE.get("t") == mt and _CACHE.get("m"):
            return _CACHE["m"]
        m = json.loads(MODEL.read_text(encoding="utf-8")) if MODEL.exists() else None
        if m is None:
            X, y = _load_sets()
            if X is None or len(set(y.tolist())) < 2:
                return None
            m = train(X, y)
        _CACHE.update(t=mt, m=m)
        return m
    except Exception:   # noqa: BLE001
        return None


def labels(F, f0, idx, win=4):
    """Speaker (0/1) of each analysed frame, or None when there is no model."""
    m = model()
    if m is None or len(F) == 0:
        return None
    X = _feat(F, f0)
    Z = (X - np.array(m["mu"])) / np.array(m["sd"])
    s = Z @ np.array(m["w"]) + m["b"]
    idx = np.asarray(idx)
    out = np.empty_like(s)
    for k in range(len(s)):
        a, b = np.searchsorted(idx, idx[k] - win), np.searchsorted(idx, idx[k] + win, side="right")
        out[k] = np.median(s[a:b])
    return (out > 0).astype(int)


def _speech_frames(st, SR):
    hop = int(0.1 * SR)
    m = len(st) // hop
    fr = st[: m * hop].reshape(m, hop, 2)
    e = (fr ** 2).mean(1) + 1e-12
    tot = 10 * np.log10(e.sum(1))
    return m, tot > np.percentile(tot, 10) + 18


def collect(pdir):
    """(X, y) of one project: the frames inside the telops of the blocks the person has edited, with their speakers."""
    import subtitles as sb
    pdir = Path(pdir)
    ed = json.loads((pdir / "telops_edited.json").read_text(encoding="utf-8"))
    pj = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    src = pdir / "audio_local.m4a"
    if not src.exists():
        src = Path(pj.get("audio") or "")
    if not src.exists():
        return None, None
    SR = sb.SR
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    st = np.frombuffer(r.stdout, dtype=np.float32)
    st = st[: len(st) // 2 * 2].reshape(-1, 2)
    m, speech = _speech_frames(st, SR)
    idx, F, f0 = sb._voice_feats(st.mean(axis=1), speech)
    lab = np.full(m, -1)
    for b, v in ed.items():
        for p in v.get("pieces", []):
            if p.get("deleted") or p.get("speaker", 0) < 0 or p.get("ov") or p.get("text") == "www":
                continue
            lab[int(p["start"] * 10): int(p["end"] * 10)] = p["speaker"]
    L = lab[idx]
    ok = L >= 0
    if ok.sum() < 40:
        return None, None
    return _feat(F[ok], f0[ok]), L[ok]


def update(projects_root):
    """Learn from every project that has hand-edited telops (only when its edits changed since the last time)."""
    root = Path(projects_root)
    meta_f = HERE / "presets" / "voice_meta.json"
    meta_f.parent.mkdir(exist_ok=True)
    try:
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
    except Exception:   # noqa: BLE001
        meta = {}
    try:
        d = np.load(LOCAL, allow_pickle=True)
        parts = {str(k): (a, b) for k, a, b in zip(d["names"], d["Xs"], d["ys"])}
    except Exception:   # noqa: BLE001
        parts = {}
    changed = False
    for f in root.glob("*/telops_edited.json"):
        name = f.parent.name
        sig = "%d-%d" % (f.stat().st_mtime, f.stat().st_size)
        if meta.get(name) == sig:
            continue
        try:
            X, y = collect(f.parent)
        except Exception:   # noqa: BLE001
            continue
        meta[name] = sig
        if X is not None:
            parts[name] = (X, y)
            changed = True
    if not changed:
        return False
    names = list(parts)
    obj = lambda arr: np.array(arr, dtype=object)
    Xs, ys = obj([None] * len(names)), obj([None] * len(names))
    for i, n in enumerate(names):
        Xs[i], ys[i] = parts[n][0], parts[n][1]
    LOCAL.parent.mkdir(exist_ok=True)
    np.savez(LOCAL, X=np.vstack([parts[n][0] for n in names]), y=np.concatenate([parts[n][1] for n in names]),
             names=np.array(names), Xs=Xs, ys=ys)
    X, y = _load_sets()
    if X is not None and len(set(y.tolist())) == 2:
        MODEL.write_text(json.dumps(train(X, y)), encoding="utf-8")
    meta_f.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    _CACHE.clear()
    return True
