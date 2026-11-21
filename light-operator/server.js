// スマホ一斉ライト制御サーバー（依存パッケージなし / Node.js 18+）
// 使い方: node server.js   → 表示されたURLをスマホ・PCで開く
const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const { execSync } = require('child_process');

const PORT = Number(process.env.PORT || 8443);
const OPERATOR_KEY = process.env.OPERATOR_KEY || ''; // 設定するとオペレーター画面にキーが必要
const USE_HTTP = process.env.HTTP === '1';           // トンネル(cloudflared等)経由で使う場合は HTTP=1
const PUBLIC = path.join(__dirname, 'public');
const DATA = process.env.LO_DATA || __dirname; // 記録・状態ファイルの置き場所

// ---------- 状態 ----------
let lightOn = false;
let ver = 0, endSeq = 0; const waiters = new Set();
function bump() { ver++; for (const w of waiters) w(); waiters.clear(); }
let torchEnabled = true; // false にするとスマホのライトは使わず画面だけ光らせる
const fx = { mode: 'white', strobe: false, hz: 8, fade: 0.5, target: 'torch', held: [] }; // 演出設定
const pub = () => ({ on: lightOn, torch: torchEnabled, ...fx });
const clients = new Map();   // id -> { res, name, torch, screen, lastSeen }
const operators = new Set(); // SSE res

function send(res, data) { res.write(`data: ${JSON.stringify(data)}\n\n`); }

function broadcastState() {
  bump();
  for (const c of clients.values()) if (c.res) send(c.res, { type: 'state', ...pub() });
  broadcastOperators();
}
function broadcastOperators() {
  const list = [...clients.entries()].map(([id, c]) => ({
    id, num: c.num, name: c.name, torch: c.torch, ack: c.ack, ver: c.ver || '', ready: !!c.ready, lost: !c.res && Date.now() - (c.seen || 0) > 10000,
  }));
  for (const res of operators) send(res, { type: 'status', ...pub(), clients: list });
}
function setLight(on) {
  if (lightOn === on) return;
  lightOn = on;
  console.log(`[${new Date().toLocaleTimeString()}] LIGHT ${on ? 'ON ' : 'OFF'}  (${clients.size}台)`);
  broadcastState();
}

// 端末の番号（ずっと同じ番号を使う）
const NUM_FILE = path.join(DATA, 'numbers.json');
let numbers = {}; try { numbers = JSON.parse(fs.readFileSync(NUM_FILE, 'utf8')); } catch {}
function assignNumber(id) {
  const used = new Set([...clients.keys()].filter(k => k !== id).map(k => numbers[k]));
  let n = numbers[id];
  if (!n || used.has(n)) { n = 1; while (used.has(n)) n++; numbers[id] = n; try { fs.writeFileSync(NUM_FILE, JSON.stringify(numbers)); } catch {} }
  return n;
}

// ---------- HTTP ハンドラ ----------
function readBody(req) {
  return new Promise((resolve) => {
    let b = ''; req.on('data', (d) => { b += d; if (b.length > 1e4) req.destroy(); });
    req.on('end', () => { try { resolve(JSON.parse(b || '{}')); } catch { resolve({}); } });
  });
}
function sse(res) {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive', 'X-Accel-Buffering': 'no',
  });
  res.write('retry: 1000\n\n');
}
const MIME = { '.png': 'image/png', '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json' };

async function handler(req, res) {
  const url = new URL(req.url, 'http://x');
  const p = url.pathname;
  // CORS: ホーム画面アプリ（古いアドレスのページ）から今のサーバーに接続できるように
  if (p.startsWith('/api/') || p.startsWith('/client.html')) {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    if (req.method === 'OPTIONS') { res.writeHead(204).end(); return; }
  }
  if (p === '/operator.webmanifest') {
    // キューリストの画面を「Dock に追加」したときの設定（スマホ用とは別のアプリ）
    res.writeHead(200, { 'Content-Type': 'application/manifest+json', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify({ id: '/operator', name: 'Light Operator', short_name: 'Light Operator',
      start_url: '/operator' + (OPERATOR_KEY ? '?key=' + OPERATOR_KEY : ''), scope: '/operator', display: 'standalone',
      background_color: '#101114', theme_color: '#17191e',
      icons: [{ src: '/icon-192.png', sizes: '192x192', type: 'image/png' }, { src: '/icon-512.png', sizes: '512x512', type: 'image/png' }] }));
    return;
  }
  if (p === '/api/info') {
    let topic = '', cur = '';
    try { topic = fs.readFileSync(path.join(__dirname, 'topic.txt'), 'utf8').trim(); } catch {}
    try { cur = fs.readFileSync(path.join(DATA, 'current_url.txt'), 'utf8').trim(); } catch {}
    res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify({ app: 'phone-light', topic, url: cur }));
    return;
  }
  const okKey = !OPERATOR_KEY || url.searchParams.get('key') === OPERATOR_KEY;

  // スマホ: イベント受信
  if (p === '/events/client') {
    const id = url.searchParams.get('id') || crypto.randomUUID();
    sse(res);
    const prev = clients.get(id);
    clients.set(id, { res, name: (url.searchParams.get('name') || '').slice(0, 30), torch: prev?.torch ?? null, ack: prev?.ack ?? null });
    send(res, { type: 'state', ...pub() });
    broadcastOperators();
    req.on('close', () => { if (clients.get(id)?.res === res) { clients.delete(id); broadcastOperators(); } });
    return;
  }
  // スマホ: ロングポーリング（トンネル経由でも確実に届く）
  if (p === '/api/poll') {
    const id = url.searchParams.get('id') || '';
    const v = Number(url.searchParams.get('v') ?? -1);
    let c = clients.get(id);
    if (!c) { c = { res: null, name: (url.searchParams.get('name') || '').slice(0, 30), torch: null, ack: null }; clients.set(id, c); c.num = assignNumber(id); bump(); broadcastOperators(); }
    c.seen = Date.now();
    const reply = () => {
      clearTimeout(t); waiters.delete(reply); c.seen = Date.now();
      if (res.writableEnded) return;
      res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      const ids = [...clients.keys()];
      res.end(JSON.stringify({ v: ver, end: endSeq, slot: Math.max(0, ids.indexOf(id)), total: ids.length, num: c.num, ...pub() }));
    };
    const t = setTimeout(reply, 8000);
    if (v !== ver) reply(); else { waiters.add(reply); req.on('close', () => { clearTimeout(t); waiters.delete(reply); }); }
    return;
  }
  // スマホ: 自分の状態を報告（ライト対応可否・点灯反映済みか）
  if (p === '/api/report' && req.method === 'POST') {
    const b = await readBody(req);
    const c = clients.get(b.id);
    if (c) {
      if (typeof b.torch === 'boolean') c.torch = b.torch;
      if (typeof b.ack === 'boolean') c.ack = b.ack;
      if (typeof b.name === 'string') c.name = b.name.slice(0, 30);
      if (typeof b.ver === 'string') c.ver = b.ver.slice(0, 10);
      if (typeof b.ready === 'boolean') c.ready = b.ready;
      broadcastOperators();
    }
    res.writeHead(204).end(); return;
  }
  // オペレーター: イベント受信
  if (p === '/events/operator') {
    if (!okKey) { res.writeHead(403).end(); return; }
    sse(res); operators.add(res); broadcastOperators();
    req.on('close', () => {
      operators.delete(res);
      // 安全策: オペレーターが全員いなくなったら消灯
      if (operators.size === 0) setLight(false);
    });
    return;
  }
  // オペレーター: 点灯/消灯
  if (p === '/api/set' && req.method === 'POST') {
    if (!okKey) { res.writeHead(403).end(); return; }
    const b = await readBody(req);
    // キュー1つ分の変更をまとめて反映
    let changed = false;
    if (typeof b.torch === 'boolean' && b.torch !== torchEnabled) { torchEnabled = b.torch; changed = true; }
    if (b.mode === 'white' || b.mode === 'rainbow') { if (fx.mode !== b.mode) changed = true; fx.mode = b.mode; }
    if (typeof b.strobe === 'boolean') { if (fx.strobe !== b.strobe) changed = true; fx.strobe = b.strobe; }
    if (typeof b.hz === 'number') { const h = Math.max(1, Math.min(30, b.hz)); if (fx.hz !== h) changed = true; fx.hz = h; }
    if (Array.isArray(b.held)) { fx.held = [...new Set(b.held.filter(x => typeof x === 'number' && x > 0 && x < 100).map(Math.floor))].sort((a, b) => a - b); changed = true; }
    if (['torch', 'screen', 'both'].includes(b.target)) { if (fx.target !== b.target) changed = true; fx.target = b.target; }
    if (typeof b.fade === 'number') { fx.fade = Math.max(0, Math.min(10, b.fade)); changed = true; }
    if (typeof b.on === 'boolean' && b.on !== lightOn) { lightOn = b.on; changed = true; }
    if (changed) {
      console.log(`[${new Date().toLocaleTimeString()}] ${b.label || ''} on=${lightOn} mode=${fx.mode} strobe=${fx.strobe} fade=${fx.fade} (${clients.size}台)`);
      broadcastState();
    }
    res.writeHead(204).end(); return;
  }

  // オペレーター: 端末の番号を指定（使われている番号なら入れ替え）
  if (p === '/api/number' && req.method === 'POST') {
    if (!okKey) { res.writeHead(403).end(); return; }
    const b = await readBody(req);
    const c = clients.get(b.id);
    if (c && Number.isInteger(b.num) && b.num > 0 && b.num < 100) {
      const old = c.num;
      for (const [k, o] of clients) if (k !== b.id && o.num === b.num) { o.num = old; numbers[k] = old; }
      c.num = b.num; numbers[b.id] = b.num;
      try { fs.writeFileSync(NUM_FILE, JSON.stringify(numbers)); } catch {}
      bump(); broadcastOperators();
    }
    res.writeHead(204).end(); return;
  }
  // オペレーター: サーバーを止める
  if (p === '/api/shutdown' && req.method === 'POST') {
    if (!okKey) { res.writeHead(403).end(); return; }
    lightOn = false; endSeq++; bump();
    console.log('サーバーを停止します');
    try { fs.writeFileSync(path.join(DATA, 'stopped.flag'), 'stopped'); } catch {}
    res.writeHead(204).end();
    setTimeout(() => process.exit(0), 1500);
    return;
  }
  // オペレーター: 終了（全スマホを消灯して元の状態に戻す）
  if (p === '/api/end' && req.method === 'POST') {
    if (!okKey) { res.writeHead(403).end(); return; }
    lightOn = false;
    console.log(`[${new Date().toLocaleTimeString()}] 終了 → 全端末を元に戻します (${clients.size}台)`);
    endSeq++; bump();
    for (const c of clients.values()) if (c.res) send(c.res, { type: 'end' });
    broadcastOperators();
    res.writeHead(204).end(); return;
  }

  // 静的ファイル
  let file = p === '/' ? '/client.html' : p === '/operator' ? '/operator.html' : p;
  file = path.normalize(path.join(PUBLIC, file));
  if (!file.startsWith(PUBLIC)) { res.writeHead(403).end(); return; }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404).end('not found'); return; }
    if (file.endsWith('client.html')) {
      let topic = '';
      try { topic = fs.readFileSync(path.join(__dirname, 'topic.txt'), 'utf8').trim(); } catch {}
      data = Buffer.from(data.toString('utf8').split('__TOPIC__').join(topic));
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    res.end(data);
  });
}

// ポーリングが30秒途切れた端末は一覧から外す
setInterval(() => {
  let gone = false;
  for (const [k, c] of clients) if (!c.res && Date.now() - (c.seen || 0) > 60000) { clients.delete(k); gone = true; }
  if (gone) bump();
  if (gone || clients.size) broadcastOperators(); // 接続切れ（赤丸）を表示に反映
}, 5000);

// SSE の接続維持（プロキシやスマホのスリープ対策）
setInterval(() => {
  for (const c of clients.values()) if (c.res) c.res.write(': ping\n\n');
  for (const r of operators) r.write(': ping\n\n');
}, 15000);

// ---------- 起動 ----------
// スマホでカメラ(=ライト)を使うには HTTPS が必須。証明書がなければ自己署名を自動生成。
function loadCert() {
  const dir = path.join(__dirname, 'cert');
  const key = path.join(dir, 'key.pem'), cert = path.join(dir, 'cert.pem');
  if (!fs.existsSync(key) || !fs.existsSync(cert)) {
    fs.mkdirSync(dir, { recursive: true });
    const ips = lanIPs();
    const san = ['DNS:localhost', 'IP:127.0.0.1', ...ips.map((ip) => `IP:${ip}`)].join(',');
    try {
      execSync(`openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout "${key}" -out "${cert}" -subj "/CN=phone-light" -addext "subjectAltName=${san}"`, { stdio: 'ignore' });
      console.log('自己署名証明書を生成しました: cert/');
    } catch {
      console.warn('\n[!] openssl が見つからないため HTTPS なしで起動します（画面は光ります。Androidのライトは使えません）\n');
      return null;
    }
  }
  return { key: fs.readFileSync(key), cert: fs.readFileSync(cert) };
}
function lanIPs() {
  return Object.values(os.networkInterfaces()).flat()
    .filter((i) => i && i.family === 'IPv4' && !i.internal).map((i) => i.address);
}

const cert = USE_HTTP ? null : loadCert();
const server = cert ? https.createServer(cert, handler) : http.createServer(handler);
server.listen(PORT, '0.0.0.0', () => {
  const proto = cert ? 'https' : 'http';
  const k = OPERATOR_KEY ? `?key=${OPERATOR_KEY}` : '';
  console.log('\n=== Light Operator 起動 ===');
  for (const ip of ['localhost', ...lanIPs()]) {
    console.log(`  スマホ用      : ${proto}://${ip}:${PORT}/`);
    console.log(`  オペレーター用: ${proto}://${ip}:${PORT}/operator${k}\n`);
  }
});
