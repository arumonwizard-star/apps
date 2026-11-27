// スマホで見る・操作する
//  Light Operator が入っている Mac：Light Operator のサーバー（8787）を通す
//  入っていない Mac：アプリが自分で 8787 番で受け付ける（同じ Wi-Fi の端末だけ）
const http = require('http');
const os = require('os');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const LO_DIR = path.join(os.homedir(), 'Applications', '.lightoperator');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function req(method, url, body, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const r = http.request(url, { method, timeout, headers: body ? { 'Content-Type': body instanceof Buffer ? 'image/jpeg' : 'text/plain', 'Content-Length': Buffer.byteLength(body) } : {} }, (res) => {
      const ch = []; res.on('data', (d) => ch.push(d)); res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(ch).toString() }));
    });
    r.on('timeout', () => r.destroy(new Error('timeout'))); r.on('error', reject);
    if (body) r.write(body); r.end();
  });
}
function lanIPs() { const out = []; for (const list of Object.values(os.networkInterfaces())) for (const i of list || []) if (i.family === 'IPv4' && !i.internal) out.push(i.address); return out; }
function hostName() { let n = ''; try { n = execSync('scutil --get LocalHostName', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim(); } catch {} if (!n) n = os.hostname().replace(/\.local$/i, ''); return n ? n + '.local' : ''; }

class Remote {
  constructor(o) {
    this.o = o; this.mode = null; this.srv = null; this.server = null; this.closed = false;
    this.ready = new Promise((r) => { this._ready = r; });
    this.S = { list: null, lv: 0, live: null, v: 0, frame: null, fv: 0, phones: new Map() };
    this.waitP = new Set(); this.relayPhones = 0;
  }
  phones() { if (this.mode === 'relay') return this.relayPhones; const now = Date.now(); for (const [k, t] of this.S.phones) if (now - t > 15000) this.S.phones.delete(k); return this.S.phones.size; }
  async start() {
    for (let i = 0; !this.closed; i++) {
      const s = await this.tryRelay();
      if (s) { this.mode = 'relay'; this.srv = s; this.o.log('スマホ連携: Light Operator 経由', s.url || ''); this._ready(); this.cmdLoop(); return; }
      if (!fs.existsSync(LO_DIR)) {
        const port = await this.listen([8787, 8788, 8789]);
        if (port) { const h = hostName(); this.mode = 'embedded'; this.srv = { key: '', port, url: h ? `http://${h}:${port}/alien` : '', lan: lanIPs().map((ip) => `http://${ip}:${port}`), tunnel: '' }; this.o.log('スマホ連携: アプリで受付', port); }
        this._ready(); return;
      }
      if (i === 0) this._ready();
      await sleep(5000);
    }
  }
  close() { this.closed = true; try { this.server && this.server.close(); } catch {} }
  async tryRelay() { try { const r = await req('GET', 'http://127.0.0.1:8787/alien/api/srv', null, 1500); if (r.status === 200) return JSON.parse(r.body); } catch {} return null; }

  // 画面からの状態
  async state(body) {
    if (this.mode === 'relay') {
      try { const r = await req('POST', 'http://127.0.0.1:8787/alien/api/state', JSON.stringify(body)); const j = JSON.parse(r.body); this.relayPhones = j.phones | 0; return j; } catch { return { phones: 0 }; }
    }
    if (this.mode === 'embedded') {
      if (body.list) { this.S.list = body.list; this.S.lv++; }
      if (body.live) { this.S.live = body.live; this.S.v++; }
      for (const w of [...this.waitP]) w(); this.waitP.clear();
      return { phones: this.phones() };
    }
    return { phones: 0 };
  }
  frame(buf) {
    if (this.mode === 'relay') { req('POST', 'http://127.0.0.1:8787/alien/api/frame', buf).catch(() => {}); return; }
    if (this.mode === 'embedded') { this.S.frame = buf; this.S.fv++; }
  }
  async cmdLoop() {
    let since = -1;
    while (!this.closed) {
      try {
        const r = await req('GET', `http://127.0.0.1:8787/alien/api/cmds?since=${since}`, null, 26000); const j = JSON.parse(r.body);
        if (since < 0) { since = j.seq; continue; }
        for (const m of j.cmds || []) if (m.seq > since) { since = m.seq; this.o.onCmd(m); }
        if (j.seq < since) since = j.seq;
      } catch { await sleep(2000); }
    }
  }

  // Light Operator がない Mac 用のサーバー
  listen(ports) {
    return new Promise((resolve) => {
      const tryPort = (i) => {
        if (i >= ports.length) return resolve(0);
        const srv = http.createServer((q, s) => this.handle(q, s).catch(() => { try { s.writeHead(500).end(); } catch {} }));
        srv.once('error', () => tryPort(i + 1));
        srv.listen(ports[i], '0.0.0.0', () => { this.server = srv; resolve(ports[i]); });
      };
      tryPort(0);
    });
  }
  async handle(q, s) {
    const u = new URL(q.url, 'http://x'), p = u.pathname;
    s.setHeader('Access-Control-Allow-Origin', '*'); s.setHeader('Cache-Control', 'no-store');
    if (q.method === 'OPTIONS') { s.setHeader('Access-Control-Allow-Headers', 'Content-Type'); s.writeHead(204).end(); return; }
    const json = (o) => { s.writeHead(200, { 'Content-Type': 'application/json' }); s.end(JSON.stringify(o)); };
    const S = this.S;
    if (p === '/alien/api/poll') {
      const id = (u.searchParams.get('id') || 'x').slice(0, 40); S.phones.set(id, Date.now());
      const reply = () => { S.phones.set(id, Date.now()); const o = { v: S.v, lv: S.lv, fv: S.fv, mac: true, live: S.live }; if (String(S.lv) !== u.searchParams.get('lv')) o.list = S.list; json(o); };
      if (String(S.v) !== u.searchParams.get('v') || String(S.lv) !== u.searchParams.get('lv')) return reply();
      let done = false; const go = () => { if (done) return; done = true; clearTimeout(t); this.waitP.delete(go); reply(); };
      const t = setTimeout(go, 20000); this.waitP.add(go); q.on('close', () => { done = true; clearTimeout(t); this.waitP.delete(go); }); return;
    }
    if (p === '/alien/api/frame') { if (!S.frame) { s.writeHead(204).end(); return; } s.writeHead(200, { 'Content-Type': 'image/jpeg' }); s.end(S.frame); return; }
    if (p === '/alien/api/cmd' && q.method === 'POST') {
      const ch = []; let n = 0; for await (const d of q) { n += d.length; if (n > 1e4) break; ch.push(d); }
      let j = {}; try { j = JSON.parse(Buffer.concat(ch).toString()); } catch {}
      if (['go', 'stop', 'panic', 'pause', 'prev', 'next', 'ph', 'fire', 'bo'].includes(j.c)) this.o.onCmd({ c: j.c, id: typeof j.id === 'string' ? j.id.slice(0, 40) : '', from: String(j.from || '').slice(0, 30) });
      s.writeHead(204).end(); return;
    }
    if (p === '/alien/remote.webmanifest') {
      s.writeHead(200, { 'Content-Type': 'application/manifest+json' });
      s.end(JSON.stringify({ id: '/alien', name: 'Alien Lab', short_name: 'Alien Lab', start_url: '/alien', scope: '/alien/', display: 'standalone', background_color: '#0e0f11', theme_color: '#0e0f11',
        icons: [{ src: '/alien/icon-192.png', sizes: '192x192', type: 'image/png' }, { src: '/alien/icon-512.png', sizes: '512x512', type: 'image/png' }] })); return;
    }
    let file = null;
    if (['/', '/alien', '/alien/', '/alien/remote', '/alien/remote.html', '/alien/index.html'].includes(p)) file = 'remote.html';
    else if (/^\/alien\/icon-(180|192|512)\.png$/.test(p)) file = p.slice(7);
    if (!file) { s.writeHead(404).end(); return; }
    try { const b = fs.readFileSync(path.join(this.o.uiDir(), file)); s.writeHead(200, { 'Content-Type': file.endsWith('.png') ? 'image/png' : 'text/html; charset=utf-8' }); s.end(b); }
    catch { s.writeHead(404).end(); }
  }
}
module.exports = Remote;
