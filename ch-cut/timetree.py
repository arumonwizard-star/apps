"""Pick a script (the calendar memo) from TimeTree, from inside the CH Cut window.

TimeTree has no public API, so the app opens TimeTree in a second window of its own (the sign-in is kept
between runs), reads the events around today from the month view and the memo of each, and hands the
list to the page.  Needs the app window (pywebview); in a normal browser it says so."""
import datetime
import json
import threading
import time
from pathlib import Path

URL = "https://timetreeapp.com/calendars"
JS = (Path(__file__).parent / "timetree_scrape.js").read_text(encoding="utf-8")
S = {"state": "idle", "msg": "", "items": [], "n": 0, "total": 0, "checked": []}
_win = None
_lock = threading.Lock()


def status():
    return dict(S)


def start(webview, span=7):
    with _lock:
        if S["state"] in ("loading", "login", "run"):
            return
        if webview is None:
            S.update(state="error", msg="アプリの画面で開いたときだけ使えます（ブラウザでは使えません）", items=[])
            return
        S.update(state="loading", msg="タイムツリーを開いています…", items=[], n=0, total=0)
    threading.Thread(target=_work, args=(webview, span), daemon=True).start()


def preload(webview):
    """Open TimeTree hidden right after the app starts, so a click on the button does not wait for the page."""
    global _win
    try:
        if _win is None:
            _win = webview.create_window("タイムツリー", URL, width=800, height=1000, hidden=True)
    except Exception:   # noqa: BLE001
        pass


def stop():
    try:
        if _win is not None and S["state"] == "run":
            _win.evaluate_js("window.__ttCancel=true")
    except Exception:   # noqa: BLE001
        pass


def _url():
    try:
        return _win.get_current_url() or ""
    except Exception:   # noqa: BLE001
        return ""


def _work(webview, span):
    global _win
    try:
        if _win is None:
            _win = webview.create_window("タイムツリー", URL, width=800, height=1000, hidden=True)
        elif "timetreeapp.com" not in _url():
            _win.load_url(URL)
        t0, shown = time.time(), False
        base = None
        while time.time() - t0 < 600:                     # up to 10 minutes to sign in
            u = _url()
            if "timetreeapp.com/calendars/" in u and "/events/" not in u:
                base = u.split("?")[0].split("/daily")[0].split("/monthly")[0].rstrip("/")
                break
            if "/signin" in u or "/login" in u:
                if not shown:
                    S.update(state="login", msg="タイムツリーのウインドウでログインしてください")
                    _win.show()                          # only for signing in
                    shown = True
            time.sleep(1)
        if shown:
            _win.hide()
        if not base:
            S.update(state="error", msg="タイムツリーを開けませんでした")
            _win.hide()
            return
        S.update(state="run", msg="予定を読み込んでいます…")
        js = JS.replace("__TODAY__", datetime.date.today().isoformat()).replace("__N__", str(int(span)))
        _win.evaluate_js(js)
        t1, retried = time.time(), False
        while time.time() - t1 < 420:
            time.sleep(0.25)
            try:
                r = json.loads(_win.evaluate_js("JSON.stringify(window.__tt||null)") or "null")
            except Exception:   # noqa: BLE001
                r = None
            if not r:
                continue
            S.update(items=r.get("items", []), n=r.get("opened", 0), total=len(r.get("items", [])), checked=r.get("checked", []))
            if r.get("state") == "done":
                print("タイムツリー: 確認", r.get("opened"), "件 / 台本", len(r.get("items", [])), "件", r.get("checked"), flush=True)
                S.update(state="done", msg="")
                break
            if r.get("state") == "error":
                if not retried and "読み取れ" in (r.get("msg") or ""):
                    retried = True                       # a hidden window may not lay the page out: try once with it shown
                    _win.show()
                    _win.load_url(base)
                    time.sleep(3)
                    _win.evaluate_js(js)
                    continue
                S.update(state="error", msg=r.get("msg") or "読み取れませんでした")
                break
        else:
            S.update(state="error", msg="時間がかかりすぎたので止めました")
        _win.hide()
    except Exception as e:   # noqa: BLE001
        S.update(state="error", msg="タイムツリーを読み取れませんでした: %s" % e)
        try:
            _win.hide()
        except Exception:   # noqa: BLE001
            pass
