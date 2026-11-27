# スマホ一斉ライト制御サーバー（Python版 / 標準ライブラリのみ / Python 3.7+）
# server.js と同じ動きをします。Node.js がない環境用。
import json, os, socket, ssl, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", "8443"))
OPERATOR_KEY = os.environ.get("OPERATOR_KEY", "")
USE_HTTP = os.environ.get("HTTP") == "1"
BASE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE, "public")
DATA = os.environ.get("LO_DATA") or BASE  # 記録・状態ファイルの置き場所

lock = threading.Lock()
state = {"on": False, "torch": True, "v": 0, "end": 0,
         "mode": "white", "strobe": False, "hz": 8, "fade": 0.5, "target": "torch", "held": []}
OPT_KEYS = ("on", "torch", "mode", "strobe", "hz", "fade", "target", "held")


NUM_FILE = os.path.join(DATA, "numbers.json")
try:
    numbers = json.load(open(NUM_FILE))   # 端末ID → 番号
except Exception:
    numbers = {}


def assign_number(cid):
    """前回の番号が空いていればそれを、なければ空いている一番小さい番号を割り当てる"""
    used = {numbers.get(k) for k in clients if k != cid}
    n = numbers.get(cid)
    if not n or n in used:
        n = 1
        while n in used:
            n += 1
        numbers[cid] = n
        try:
            json.dump(numbers, open(NUM_FILE, "w"))
        except Exception:
            pass
    return n


def public_state():
    return {k: state[k] for k in OPT_KEYS}
cond = threading.Condition(lock)  # ロングポーリング待ちを起こす
clients = {}      # id -> {"q": Sink, "name", "torch", "ack"}
operators = set() # Sink


class Sink:
    """1本の SSE 接続。書き込み失敗で dead になる。"""
    def __init__(self, wfile):
        self.wfile, self.dead, self.wl = wfile, False, threading.Lock()

    def send(self, data=None, raw=None):
        if self.dead:
            return
        try:
            with self.wl:
                self.wfile.write((raw if raw is not None else "data: %s\n\n" % json.dumps(data, ensure_ascii=False)).encode())
                self.wfile.flush()
        except Exception:
            self.dead = True


def status_msg():
    return {"type": "status", **public_state(),
            "clients": [{"id": k, "num": c.get("num"), "name": c["name"], "torch": c["torch"], "ack": c["ack"], "ver": c.get("ver", ""), "ready": c.get("ready", False),
                         "lost": (c.get("q") is None and time.time() - c.get("seen", time.time()) > 10)} for k, c in clients.items()]}


def broadcast_operators():
    m = status_msg()
    for s in list(operators):
        s.send(m)


def bump():
    state["v"] += 1
    cond.notify_all()


def broadcast_state():
    bump()
    for c in list(clients.values()):
        if c.get("q"):
            c["q"].send({"type": "state", **public_state()})
    broadcast_operators()


def set_light(on):
    with lock:
        if state["on"] == on:
            return
        state["on"] = on
        print("[%s] LIGHT %s (%d台)" % (time.strftime("%H:%M:%S"), "ON " if on else "OFF", len(clients)), flush=True)
        broadcast_state()


# ---------- Alien Lab（Mac の画面をスマホで見る・操作する） ----------
al_cond = threading.Condition()
AL = {"list": None, "lv": 0, "live": None, "v": 0, "frame": None, "fv": 0, "cmds": [], "seq": 0, "phones": {}, "mac": 0.0}
_al_host = []


def al_host():
    """Bonjour の名前（例 ny-mac.local）"""
    if not _al_host:
        n = ""
        try:
            n = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3).stdout.strip()
        except Exception:
            pass
        if not n:
            n = socket.gethostname()
            if n.lower().endswith(".local"):
                n = n[:-6]
        _al_host.append(n + ".local" if n else "")
    return _al_host[0]


def al_url():
    h = al_host()
    return "http://%s:%d/alien" % (h, PORT) if h else ""


def al_own_ips():
    ips = set(lan_ips())
    try:
        for fam in (socket.AF_INET, socket.AF_INET6):
            for i in socket.getaddrinfo(socket.gethostname(), None, fam):
                ips.add(i[4][0].split("%")[0])
    except Exception:
        pass
    return ips


AL_CMDS = ("go", "stop", "panic", "pause", "prev", "next", "ph", "fire", "bo")


class AlienMixin:
    def _al_addr(self):
        a = self.client_address[0]
        return a[7:] if a.startswith("::ffff:") else a

    def _al_fwd(self):
        return bool(self.headers.get("cf-connecting-ip") or self.headers.get("x-forwarded-for"))

    def _al_local(self):
        a = self._al_addr()
        return not self._al_fwd() and (a.startswith("127.") or a == "::1")

    def _al_self(self):
        # この Mac 自身から（localhost 以外のアドレスで）開いた
        if self._al_fwd():
            return False
        a = self._al_addr().split("%")[0]
        return a in al_own_ips()

    def _al_lan(self):
        # 同じ Wi-Fi の端末（トンネル経由は除く）
        if self._al_fwd():
            return False
        a = self._al_addr().lower()
        if a.startswith(("10.", "192.168.", "169.254.", "127.", "fc", "fd", "fe80")) or a == "::1":
            return True
        if a.startswith("172."):
            try:
                return 16 <= int(a.split(".")[1]) <= 31
            except Exception:
                return False
        return False

    def _al_raw(self, mx):
        n = int(self.headers.get("Content-Length") or 0)
        if n > mx:
            return None
        return self.rfile.read(n) if n else b""

    def _al_send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)
        return True

    def _al_json(self, o):
        return self._al_send(200, json.dumps(o, ensure_ascii=False).encode())

    def alien_get(self, p, q, ok_key):
        local = self._al_local()
        ok_key = ok_key or self._al_lan()
        if p == "/alien/api/srv":
            if not local:
                return self._al_send(403)
            try:
                cur = open(os.path.join(DATA, "current_url.txt")).read().strip()
            except Exception:
                cur = ""
            return self._al_json({"key": OPERATOR_KEY, "port": PORT, "url": al_url(), "lan": ["http://%s:%d" % (ip, PORT) for ip in lan_ips()], "tunnel": cur})
        if p == "/alien/api/frame":
            if not ok_key and not local:
                return self._al_send(403)
            with al_cond:
                f = AL["frame"]
            return self._al_send(200, f, "image/jpeg") if f else self._al_send(204)
        if p == "/alien/api/poll":
            if not ok_key and not local:
                return self._al_send(403)
            pid = (q.get("id") or "x")[:40]
            with al_cond:
                AL["phones"][pid] = time.time()
                al_cond.wait_for(lambda: str(AL["v"]) != q.get("v") or str(AL["lv"]) != q.get("lv"), timeout=20)
                AL["phones"][pid] = time.time()
                o = {"v": AL["v"], "lv": AL["lv"], "fv": AL["fv"], "mac": time.time() - AL["mac"] < 6, "live": AL["live"]}
                if str(AL["lv"]) != q.get("lv"):
                    o["list"] = AL["list"]
            return self._al_json(o)
        if p == "/alien/api/cmds":
            if not local:
                return self._al_send(403)
            try:
                since = int(q.get("since", "-1"))
            except Exception:
                since = -1
            with al_cond:
                AL["mac"] = time.time()
                if since >= 0:
                    al_cond.wait_for(lambda: AL["seq"] > since, timeout=20)
                now = time.time()
                cmds = [] if since < 0 else [c for c in AL["cmds"] if c["seq"] > since and now - c["at"] < 5]
                o = {"seq": AL["seq"], "cmds": [{k: v for k, v in c.items() if k != "at"} for c in cmds]}
            return self._al_json(o)
        if p == "/alien/remote.webmanifest":
            k = "?key=" + q["key"] if ok_key and q.get("key") else ""
            m = {"id": "/alien", "name": "Alien Lab", "short_name": "Alien Lab", "start_url": "/alien" + k, "scope": "/alien/",
                 "display": "standalone", "background_color": "#0e0f11", "theme_color": "#0e0f11",
                 "icons": [{"src": "/alien/icon-192.png", "sizes": "192x192", "type": "image/png"}, {"src": "/alien/icon-512.png", "sizes": "512x512", "type": "image/png"}]}
            return self._al_send(200, json.dumps(m).encode(), "application/manifest+json")
        if p in ("/alien", "/alien/", "/alien/index.html", "/alien/remote", "/alien/remote.html"):
            # 同じアドレスで、Mac は操作卓・ほかの端末はスマホ画面
            f = "remote.html"
            if local:
                f = "remote.html" if "remote" in p else "index.html"
            elif self._al_self() and "remote" not in p:
                self.send_response(302)
                self.send_header("Location", "http://localhost:%d/alien" % PORT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            try:
                html = open(os.path.join(PUBLIC, "alien", f), encoding="utf-8").read()
            except Exception:
                return self._al_send(404)
            if f == "index.html" and local:
                try:
                    cur = open(os.path.join(DATA, "current_url.txt")).read().strip()
                except Exception:
                    cur = ""
                srv = {"key": OPERATOR_KEY, "port": PORT, "url": al_url(), "lan": ["http://%s:%d" % (ip, PORT) for ip in lan_ips()], "tunnel": cur}
                html = html.replace("/*__AL_SRV__*/", "window.__AL_SRV=%s;" % json.dumps(srv))
            return self._al_send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        return False

    def alien_post(self, p, q, ok_key):
        local = self._al_local()
        ok_key = ok_key or self._al_lan()
        if p == "/alien/api/state":
            if not local:
                return self._al_send(403)
            b = self._al_raw(4000000)
            try:
                j = json.loads(b)
            except Exception:
                return self._al_send(400)
            with al_cond:
                AL["mac"] = time.time()
                if j.get("list"):
                    AL["list"] = j["list"]; AL["lv"] += 1
                if j.get("live"):
                    AL["live"] = j["live"]; AL["v"] += 1
                now = time.time()
                for k in [k for k, t in AL["phones"].items() if now - t > 15]:
                    del AL["phones"][k]
                n = len(AL["phones"])
                al_cond.notify_all()
            return self._al_json({"phones": n})
        if p == "/alien/api/frame":
            if not local:
                return self._al_send(403)
            b = self._al_raw(3000000)
            if b:
                with al_cond:
                    AL["frame"] = b; AL["fv"] += 1
            return self._al_send(204)
        if p == "/alien/api/cmd":
            if not ok_key and not local:
                return self._al_send(403)
            try:
                j = json.loads(self._al_raw(10000) or b"{}")
            except Exception:
                j = {}
            if j.get("c") in AL_CMDS:
                with al_cond:
                    AL["seq"] += 1
                    AL["cmds"].append({"seq": AL["seq"], "c": j["c"], "id": str(j.get("id") or "")[:40], "from": str(j.get("from") or "")[:30], "at": time.time()})
                    del AL["cmds"][:-50]
                    al_cond.notify_all()
            return self._al_send(204)
        return False


MIME = {".png": "image/png", ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json"}


class H(AlienMixin, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def end_headers(self):
        if self.path.startswith("/api/") or self.path.startswith("/client.html") or (self.path.startswith("/alien/api/") and self.command == "OPTIONS"):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self._empty(204)

    def _empty(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(min(n, 10000)) or b"{}")
        except Exception:
            return {}

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        s = Sink(self.wfile)
        s.send(raw="retry: 1000\n\n")
        return s

    def _hold(self, s):
        # 接続を維持（15秒ごとに ping）。切れたら戻る
        while not s.dead:
            time.sleep(5)
            s.send(raw=": ping\n\n")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ok_key = not OPERATOR_KEY or q.get("key") == OPERATOR_KEY
        p = u.path
        if p.startswith("/alien") and self.alien_get(p, q, ok_key) is not False:
            return

        if p == "/events/client":
            cid = q.get("id") or str(uuid.uuid4())
            s = self._sse_open()
            with lock:
                prev = clients.get(cid, {})
                clients[cid] = {"q": s, "name": (q.get("name") or "")[:30], "torch": prev.get("torch"), "ack": prev.get("ack")}
                s.send({"type": "state", **public_state()})
                broadcast_operators()
            self._hold(s)
            with lock:
                if clients.get(cid, {}).get("q") is s:
                    del clients[cid]
                    broadcast_operators()
            self.close_connection = True
            return

        if p == "/events/operator":
            if not ok_key:
                return self._empty(403)
            s = self._sse_open()
            with lock:
                operators.add(s)
                broadcast_operators()
            self._hold(s)
            with lock:
                operators.discard(s)
                empty = not operators
            if empty:
                set_light(False)  # 安全策: オペレーターがいなくなったら消灯
            self.close_connection = True
            return

        if p == "/operator.webmanifest":
            # キューリストの画面を「Dock に追加」したときの設定（スマホ用とは別のアプリ）
            k = "?key=" + OPERATOR_KEY if OPERATOR_KEY else ""
            body = json.dumps({
                "id": "/operator", "name": "Light Operator", "short_name": "Light Operator",
                "start_url": "/operator" + k, "scope": "/operator", "display": "standalone",
                "background_color": "#101114", "theme_color": "#17191e",
                "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if p == "/api/info":
            if q.get("op") != "1":
                print("[%s] 端末がサーバーを確認しに来ました（%s）" % (time.strftime("%H:%M:%S"), self.headers.get("Origin") or "同じアドレス"), flush=True)
            # ホーム画面アプリが「今のサーバー」を探すための情報（固定の入口の名前）
            try:
                topic = open(os.path.join(BASE, "topic.txt")).read().strip()
            except Exception:
                topic = ""
            try:
                cur = open(os.path.join(DATA, "current_url.txt")).read().strip()
            except Exception:
                cur = ""
            body = json.dumps({"app": "phone-light", "topic": topic, "url": cur}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if p == "/api/poll":
            # ロングポーリング：状態が変わるか20秒たつまで待って返す（トンネル経由でも確実に届く）
            cid = q.get("id") or ""
            try:
                v = int(q.get("v", "-1"))
            except ValueError:
                v = -1
            with lock:
                c = clients.get(cid)
                if c is None:
                    clients[cid] = c = {"q": None, "name": (q.get("name") or "")[:30], "torch": None, "ack": None}
                    c["num"] = assign_number(cid)
                    print("[%s] 端末が接続しました（計%d台）" % (time.strftime("%H:%M:%S"), len(clients)), flush=True)
                    bump()  # 台数が変わったので各端末に順番を配り直す
                    broadcast_operators()
                c["seen"] = time.time()
                cond.wait_for(lambda: state["v"] != v, timeout=8)
                c["seen"] = time.time()
                ids = list(clients.keys())
                slot = ids.index(cid) if cid in ids else 0
                body = json.dumps({"v": state["v"], "end": state["end"], "slot": slot, "total": len(ids), "num": c.get("num"), **public_state()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        f = "/client.html" if p == "/" else "/operator.html" if p == "/operator" else p
        path = os.path.normpath(os.path.join(PUBLIC, f.lstrip("/")))
        if not path.startswith(PUBLIC) or not os.path.isfile(path):
            return self._empty(404)
        data = open(path, "rb").read()
        if path.endswith("client.html"):
            try:
                topic = open(os.path.join(BASE, "topic.txt")).read().strip()
            except Exception:
                topic = ""
            data = data.replace(b"__TOPIC__", topic.encode())
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(path)[1], "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ok_key = not OPERATOR_KEY or q.get("key") == OPERATOR_KEY
        if u.path.startswith("/alien") and self.alien_post(u.path, q, ok_key) is not False:
            return
        b = self._body()

        if u.path == "/api/report":
            with lock:
                c = clients.get(b.get("id"))
                if c:
                    for k in ("torch", "ack"):
                        if isinstance(b.get(k), bool):
                            c[k] = b[k]
                    if isinstance(b.get("name"), str):
                        c["name"] = b["name"][:30]
                    if isinstance(b.get("ready"), bool):
                        c["ready"] = b["ready"]
                    if isinstance(b.get("ver"), str):
                        c["ver"] = b["ver"][:10]
                    broadcast_operators()
            return self._empty(204)

        if not ok_key:
            return self._empty(403)

        if u.path == "/api/set":
            # キュー1つ分の変更をまとめて反映（on / torch / mode / strobe / hz / fade）
            new = {}
            if isinstance(b.get("on"), bool): new["on"] = b["on"]
            if isinstance(b.get("torch"), bool): new["torch"] = b["torch"]
            if b.get("mode") in ("white", "rainbow"): new["mode"] = b["mode"]
            if isinstance(b.get("strobe"), bool): new["strobe"] = b["strobe"]
            if isinstance(b.get("hz"), (int, float)): new["hz"] = max(1, min(30, b["hz"]))
            if b.get("target") in ("torch", "screen", "both"): new["target"] = b["target"]
            if isinstance(b.get("held"), list): new["held"] = sorted({int(x) for x in b["held"] if isinstance(x, (int, float)) and 0 < x < 100})
            if isinstance(b.get("fade"), (int, float)): new["fade"] = max(0, min(10, b["fade"]))
            with lock:
                diff = {k: v for k, v in new.items() if state[k] != v}
                if diff or "fade" in new:
                    state.update(new)
                    label = b.get("label") or ""
                    print("[%s] %s %s (%d台)" % (time.strftime("%H:%M:%S"), label, json.dumps(diff, ensure_ascii=False), len(clients)), flush=True)
                    broadcast_state()
            return self._empty(204)

        if u.path == "/api/number":
            # オペレーターが端末の番号を指定（使われている番号なら入れ替え）
            cid, n = b.get("id"), b.get("num")
            if cid in clients and isinstance(n, int) and 0 < n < 100:
                with lock:
                    old = clients[cid].get("num")
                    for k, c in clients.items():
                        if k != cid and c.get("num") == n:
                            c["num"] = old; numbers[k] = old
                    clients[cid]["num"] = n; numbers[cid] = n
                    try:
                        json.dump(numbers, open(NUM_FILE, "w"))
                    except Exception:
                        pass
                    print("[%s] 番号を変更: %s → %d" % (time.strftime("%H:%M:%S"), clients[cid]["name"] or cid[:6], n), flush=True)
                    bump()
                    broadcast_operators()
            return self._empty(204)

        if u.path == "/api/shutdown":
            # オペレーター画面の「サーバーを止める」：全台を消灯してから終了
            with lock:
                state["on"] = False
                state["end"] += 1
                bump()
            print("[%s] サーバーを停止します" % time.strftime("%H:%M:%S"), flush=True)
            try:
                open(os.path.join(DATA, "stopped.flag"), "w").write("stopped")
            except Exception:
                pass
            self._empty(204)
            threading.Timer(1.5, lambda: os._exit(0)).start()
            return

        if u.path == "/api/end":
            with lock:
                state["on"] = False
                print("[%s] 終了 → 全端末を元に戻します (%d台)" % (time.strftime("%H:%M:%S"), len(clients)), flush=True)
                state["end"] += 1
                bump()
                for c in list(clients.values()):
                    if c.get("q"):
                        c["q"].send({"type": "end"})
                broadcast_operators()
            return self._empty(204)

        self._empty(404)


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # 実際には送信しない（オフラインでも可）
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


def load_cert():
    d = os.path.join(BASE, "cert")
    key, crt = os.path.join(d, "key.pem"), os.path.join(d, "cert.pem")
    if not (os.path.exists(key) and os.path.exists(crt)):
        os.makedirs(d, exist_ok=True)
        san = ",".join(["DNS:localhost", "IP:127.0.0.1"] + ["IP:" + ip for ip in lan_ips()])
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825",
                            "-keyout", key, "-out", crt, "-subj", "/CN=phone-light",
                            "-addext", "subjectAltName=" + san],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("自己署名証明書を生成しました: cert/")
        except Exception:
            print("\n[!] 証明書を作れなかったため HTTPS なしで起動します（画面は光ります。Androidのライトは使えません）\n")
            return None
    return key, crt


def reaper():
    # ポーリングが30秒途切れた端末は一覧から外す
    while True:
        time.sleep(5)
        with lock:
            now = time.time()
            gone = [k for k, c in clients.items() if c.get("q") is None and now - c.get("seen", now) > 60]
            for k in gone:
                del clients[k]
            if gone:
                bump()
            if clients or gone:
                broadcast_operators()  # 接続切れ（赤丸）を表示に反映


def main():
    threading.Thread(target=reaper, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.daemon_threads = True
    cert = None if USE_HTTP else load_cert()
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert[1], cert[0])
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    proto = "https" if cert else "http"
    k = "?key=" + OPERATOR_KEY if OPERATOR_KEY else ""
    print("\n=== Light Operator 起動 ===")
    print("  Alien Lab     : %s" % (al_url() or "http://localhost:%d/alien" % PORT))
    for ip in ["localhost"] + lan_ips():
        print("  スマホ用      : %s://%s:%d/" % (proto, ip, PORT))
        print("  オペレーター用: %s://%s:%d/operator%s\n" % (proto, ip, PORT, k))
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
