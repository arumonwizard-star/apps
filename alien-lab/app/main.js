// Alien Lab（Mac アプリ）
//  画面は ui/index.html（Light Operator と同じもの）。新しい版は GitHub から自動で取り込む。
//  プロジェクトは .alienlab ファイル。ダブルクリックでそのプロジェクトのオペ画面が開く。
const { app, BrowserWindow, Menu, dialog, ipcMain, protocol, session, shell, net } = require('electron');
const fs = require('fs');
const fsp = fs.promises;
const path = require('path');
const { Readable } = require('stream');
const { spawn } = require('child_process');
const Remote = require('./remote');

const SMOKE = process.argv.includes('--smoke'); // 動作確認用（CI）
const REPO_RAW = 'https://raw.githubusercontent.com/arumonwizard-star/apps/main/light-operator/public/alien/';
const RELEASE = 'https://github.com/arumonwizard-star/apps/releases/download/alien-lab/';
const UI_FILES = ['index.html', 'remote.html', 'icon-180.png', 'icon-192.png', 'icon-512.png'];
const EXT = 'alienlab';

protocol.registerSchemesAsPrivileged([{ scheme: 'alien', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true, corsEnabled: true, codeCache: true } }]);
app.setName('Alien Lab');

const USER = () => app.getPath('userData');
const LOGF = () => path.join(USER(), 'alienlab.log');
function log(...a) { const line = `[${new Date().toISOString()}] ${a.join(' ')}\n`; try { fs.appendFileSync(LOGF(), line); } catch {} if (SMOKE) process.stdout.write(line); }

// ---------- 画面（UI）の場所と自動更新 ----------
const verOf = (html) => { const m = /const VERSION = '([\d.]+)'/.exec(html || ''); return m ? m[1] : '0'; };
const newer = (a, b) => { const x = String(a).split('.').map(Number), y = String(b).split('.').map(Number); for (let i = 0; i < Math.max(x.length, y.length); i++) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0); } return false; };
const BUNDLED = path.join(__dirname, 'ui');
const CACHE = () => path.join(USER(), 'ui');
let uiDir = BUNDLED;
function chooseUI() {
  try {
    const b = verOf(fs.readFileSync(path.join(BUNDLED, 'index.html'), 'utf8'));
    const c = verOf(fs.readFileSync(path.join(CACHE(), 'index.html'), 'utf8'));
    if (newer(c, b) && UI_FILES.every((f) => fs.existsSync(path.join(CACHE(), f)))) uiDir = CACHE();
  } catch {}
  log('UI', uiDir === BUNDLED ? 'bundled' : 'cache', verOf(fs.readFileSync(path.join(uiDir, 'index.html'), 'utf8')));
}
async function updateUI() {
  try {
    const r = await net.fetch(REPO_RAW + 'index.html', { cache: 'no-store' }); if (!r.ok) return;
    const html = await r.text(); const v = verOf(html);
    const cur = verOf(fs.readFileSync(path.join(uiDir, 'index.html'), 'utf8'));
    if (!html.includes('</html>') || !newer(v, cur)) return;
    const tmp = path.join(USER(), 'ui.tmp'); await fsp.rm(tmp, { recursive: true, force: true }); await fsp.mkdir(tmp, { recursive: true });
    await fsp.writeFile(path.join(tmp, 'index.html'), html);
    for (const f of UI_FILES.slice(1)) { const x = await net.fetch(REPO_RAW + encodeURIComponent(f), { cache: 'no-store' }); if (!x.ok) return; await fsp.writeFile(path.join(tmp, f), Buffer.from(await x.arrayBuffer())); }
    await fsp.rm(CACHE(), { recursive: true, force: true }); await fsp.rename(tmp, CACHE());
    uiDir = CACHE(); log('UI を更新', v); // 次に開くウィンドウから新しい版
  } catch (e) { log('UI 更新できず', e.message); }
}

// ---------- アプリ本体の自動更新（終了したときに入れ替える） ----------
let pendingApp = null;
function bundlePath() { return path.resolve(app.getPath('exe'), '../../..'); }
async function updateShell() {
  try {
    const bp = bundlePath();
    if (!app.isPackaged || bp.includes('/AppTranslocation/') || bp.startsWith('/Volumes/')) return;
    try { fs.accessSync(path.dirname(bp), fs.constants.W_OK); } catch { return; }
    const r = await net.fetch(RELEASE + 'latest.json', { cache: 'no-store' }); if (!r.ok) return;
    const j = await r.json(); if (!j.version || !newer(j.version, app.getVersion())) return;
    if (pendingApp && pendingApp.version === j.version) return;
    const dir = path.join(USER(), 'update'); await fsp.rm(dir, { recursive: true, force: true }); await fsp.mkdir(dir, { recursive: true });
    const z = await net.fetch(RELEASE + (j.file || 'Alien-Lab.zip')); if (!z.ok) return;
    const zip = path.join(dir, 'app.zip'); await fsp.writeFile(zip, Buffer.from(await z.arrayBuffer()));
    await new Promise((res, rej) => { const p = spawn('/usr/bin/ditto', ['-x', '-k', zip, path.join(dir, 'new')]); p.on('exit', (c) => c === 0 ? res() : rej(new Error('ditto ' + c))); });
    const nb = path.join(dir, 'new', 'Alien Lab.app');
    if (!fs.existsSync(path.join(nb, 'Contents/MacOS'))) return;
    pendingApp = { version: j.version, path: nb }; log('新しいアプリを用意', j.version);
  } catch (e) { log('アプリ更新を確認できず', e.message); }
}
function applyShellUpdate() {
  if (!pendingApp) return;
  const bp = bundlePath(), nb = pendingApp.path, pid = process.pid;
  const sh = `while kill -0 ${pid} 2>/dev/null; do sleep 0.5; done
B=${JSON.stringify(bp)}; N=${JSON.stringify(nb)}
rm -rf "$B.old" && mv "$B" "$B.old" && mv "$N" "$B" && rm -rf "$B.old" || { [ -d "$B" ] || mv "$B.old" "$B"; }
xattr -dr com.apple.quarantine "$B" 2>/dev/null
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$B" 2>/dev/null; true`;
  spawn('/bin/bash', ['-c', sh], { detached: true, stdio: 'ignore' }).unref();
  log('終了後にアプリを入れ替え', pendingApp.version);
}

// ---------- 素材・画面の配信（alien://app/…） ----------
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml', '.bmp': 'image/bmp',
  '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.aif': 'audio/aiff', '.aiff': 'audio/aiff', '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.flac': 'audio/flac', '.ogg': 'audio/ogg', '.opus': 'audio/ogg', '.caf': 'audio/x-caf',
  '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm', '.mkv': 'video/x-matroska' };
async function serveFile(file, req) {
  let st; try { st = await fsp.stat(file); } catch { return new Response('not found', { status: 404 }); }
  const type = MIME[path.extname(file).toLowerCase()] || 'application/octet-stream';
  const range = req.headers.get('range'); const size = st.size;
  if (range) {
    const m = /bytes=(\d*)-(\d*)/.exec(range); let start = m && m[1] ? +m[1] : 0, end = m && m[2] ? +m[2] : size - 1;
    if (!m[1] && m[2]) { start = Math.max(0, size - +m[2]); end = size - 1; }
    end = Math.min(end, size - 1); if (start > end || start >= size) return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${size}` } });
    return new Response(Readable.toWeb(fs.createReadStream(file, { start, end })), { status: 206, headers: { 'Content-Type': type, 'Content-Length': String(end - start + 1), 'Content-Range': `bytes ${start}-${end}/${size}`, 'Accept-Ranges': 'bytes' } });
  }
  return new Response(Readable.toWeb(fs.createReadStream(file)), { status: 200, headers: { 'Content-Type': type, 'Content-Length': String(size), 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store' } });
}
function registerProtocol() {
  protocol.handle('alien', (req) => {
    const u = new URL(req.url);
    if (u.pathname.startsWith('/media/')) return serveFile(decodeURIComponent(u.pathname.slice(7)), req);
    const f = path.join(uiDir, path.normalize(decodeURIComponent(u.pathname)).replace(/^(\.\.[/\\])+/, ''));
    if (!f.startsWith(uiDir)) return new Response('forbidden', { status: 403 });
    return serveFile(f, req);
  });
}

// ---------- プロジェクト（ウィンドウ1つ = プロジェクト1つ） ----------
const docs = new Map(); // webContents.id -> {win, path, dirty, initial, id, kids}
let lastActive = null, docSeq = 0;
const STATE = () => path.join(USER(), 'state.json');
const readState = () => { try { return JSON.parse(fs.readFileSync(STATE(), 'utf8')); } catch { return {}; } };
const writeState = (o) => { try { fs.writeFileSync(STATE(), JSON.stringify({ ...readState(), ...o })); } catch {} };
const nameOf = (p) => path.basename(p).replace(/\.alienlab(\.json)?$/i, '').replace(/\.json$/i, '');
const RECOVER = () => path.join(USER(), 'recovery');

async function findMedia(dir, names, depth = 3) {
  const found = {}; const want = new Set(names);
  async function walk(d, k) { if (!want.size || k > depth) return; let es; try { es = await fsp.readdir(d, { withFileTypes: true }); } catch { return; }
    for (const e of es) { if (e.name.startsWith('.')) continue; const p = path.join(d, e.name);
      if (e.isFile() && want.has(e.name)) { found[e.name] = p; want.delete(e.name); } else if (e.isDirectory() && !e.name.endsWith('.app')) await walk(p, k + 1); } }
  await walk(dir, 0); return found;
}
async function resolveMedia(file, data) {
  const dir = path.dirname(file), out = {}, missing = [];
  for (const [name, m] of Object.entries((data && data.media) || {})) {
    const cands = [m && m.rel && path.resolve(dir, m.rel), m && m.path, path.join(dir, name), path.join(dir, 'media', name)].filter(Boolean);
    const hit = cands.find((p) => { try { return fs.statSync(p).isFile(); } catch { return false; } });
    if (hit) out[name] = hit; else missing.push(name);
  }
  if (missing.length) Object.assign(out, await findMedia(dir, missing));
  return out;
}
async function openPath(file) {
  file = path.resolve(file);
  for (const d of docs.values()) if (d.path === file) { d.win.show(); d.win.focus(); return; }
  let text; try { text = await fsp.readFile(file, 'utf8'); } catch (e) { log('開けない', file, e.message); return; }
  let data = null; try { data = JSON.parse(text); } catch {}
  const media = await resolveMedia(file, data);
  createWindow({ path: file, text, media, name: nameOf(file) });
}
function createWindow(initial) {
  const id = ++docSeq;
  const win = new BrowserWindow({
    width: 1440, height: 900, minWidth: 980, minHeight: 620, show: false, backgroundColor: '#1e1e1e',
    titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 12, y: 8 },
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true, backgroundThrottling: false, spellcheck: false },
  });
  const d = { win, id, path: initial.path || null, dirty: false, initial, kids: new Set() };
  const wcId = win.webContents.id;
  docs.set(wcId, d); lastActive = d;
  if (d.path) { win.setRepresentedFilename(d.path); app.addRecentDocument(d.path); writeState({ last: d.path }); }
  win.webContents.setWindowOpenHandler(() => ({ action: 'allow', overrideBrowserWindowOptions: { backgroundColor: '#000000', autoHideMenuBar: true } }));
  win.webContents.on('did-create-window', (child) => { d.kids.add(child); child.on('closed', () => d.kids.delete(child)); });
  win.on('focus', () => { lastActive = d; });
  win.once('ready-to-show', () => { if (!SMOKE) win.show(); });
  win.on('close', (e) => {
    if (d.dirty && !d.force) {
      e.preventDefault();
      const r = dialog.showMessageBoxSync(win, { type: 'warning', buttons: ['保存', '保存しない', 'キャンセル'], defaultId: 0, cancelId: 2, message: d.path ? nameOf(d.path) : 'Untitled' });
      if (r === 0) win.webContents.send('al:menu', 'saveThenClose');
      else if (r === 1) { d.force = true; win.close(); }
      else quitting = false;
    }
  });
  win.on('closed', () => { for (const k of d.kids) { try { k.close(); } catch {} } docs.delete(wcId); clearRecovery(d); if (lastActive === d) lastActive = [...docs.values()].filter((x) => !x.win.isDestroyed()).pop() || null; });
  win.webContents.on('render-process-gone', (e, det) => log('画面が落ちた', det.reason));
  win.webContents.on('console-message', (e) => { const m = e.message || ''; if (SMOKE || /error/i.test(m)) log('console', m); });
  win.loadURL('alien://app/index.html');
  return win;
}
const docOf = (e) => docs.get(e.sender.id);
function clearRecovery(d) { try { fs.rmSync(path.join(RECOVER(), d.id + '.json'), { force: true }); } catch {} }

function withRelative(json, file) {
  const data = JSON.parse(json); const dir = path.dirname(file);
  for (const m of Object.values(data.media || {})) if (m && m.path) m.rel = path.relative(dir, m.path).split(path.sep).join('/');
  return data;
}
async function writeAtomic(file, text) { const tmp = file + '.saving'; await fsp.writeFile(tmp, text); await fsp.rename(tmp, file); }
async function doSave(d, json, saveAs, name) {
  let file = d.path;
  if (!file || saveAs) {
    const r = await dialog.showSaveDialog(d.win, { defaultPath: path.join(file ? path.dirname(file) : app.getPath('documents'), `${(name || 'Untitled').replace(/[\\/:*?"<>|]/g, '_')}.${EXT}`), filters: [{ name: 'Alien Lab', extensions: [EXT] }] });
    if (r.canceled || !r.filePath) return { ok: false };
    file = r.filePath.endsWith('.' + EXT) ? r.filePath : r.filePath + '.' + EXT;
  }
  const data = withRelative(json, file); data.name = nameOf(file);
  await writeAtomic(file, JSON.stringify(data, null, 1));
  d.path = file; d.dirty = false; d.win.setDocumentEdited(false); d.win.setRepresentedFilename(file); app.addRecentDocument(file); writeState({ last: file }); clearRecovery(d);
  log('保存', file);
  return { ok: true, path: file, name: nameOf(file) };
}

ipcMain.on('al:initial', (e) => { const d = docOf(e); e.returnValue = d ? d.initial : null; });
ipcMain.on('al:version', (e) => { e.returnValue = app.getVersion(); });
ipcMain.handle('al:save', async (e, json, saveAs, name) => { const d = docOf(e); if (!d) return { ok: false }; try { return await doSave(d, json, saveAs, name); } catch (err) { log('保存失敗', err.message); return { ok: false, error: err.message }; } });
ipcMain.handle('al:open', async (e) => {
  const d = docOf(e); const r = await dialog.showOpenDialog(d ? d.win : undefined, { properties: ['openFile', 'multiSelections'], filters: [{ name: 'Alien Lab', extensions: [EXT, 'json'] }] });
  if (!r.canceled) for (const f of r.filePaths) await openPath(f);
});
ipcMain.handle('al:new', () => { createWindow({}); });
ipcMain.handle('al:backup', async (e, json, name) => {
  const d = docOf(e); if (!d) return { ok: false };
  try {
    if (!d.path) { const s = await doSave(d, json, false, name); if (!s.ok) return s; }
    const stamp = new Date(); const pad = (n) => String(n).padStart(2, '0');
    const nm = nameOf(d.path), folder = path.join(path.dirname(d.path), 'Backup', `${nm} ${stamp.getFullYear()}-${pad(stamp.getMonth() + 1)}-${pad(stamp.getDate())} ${pad(stamp.getHours())}${pad(stamp.getMinutes())}${pad(stamp.getSeconds())}`);
    await fsp.mkdir(path.join(folder, 'media'), { recursive: true });
    const data = JSON.parse(json); let n = 0;
    for (const [mname, m] of Object.entries(data.media || {})) {
      if (!m || !m.path) continue;
      try { await fsp.copyFile(m.path, path.join(folder, 'media', mname)); m.rel = 'media/' + mname; n++; } catch (err) { log('バックアップ: コピーできない', m.path, err.message); }
    }
    data.name = nm; await writeAtomic(path.join(folder, `${nm}.${EXT}`), JSON.stringify(data, null, 1));
    // 本体も保存しておく
    await doSave(d, json, false, name);
    log('バックアップ', folder, `素材 ${n}`);
    return { ok: true, path: folder, name: nm };
  } catch (err) { log('バックアップ失敗', err.message); return { ok: false, error: err.message }; }
});
ipcMain.handle('al:folder', async (e) => {
  const d = docOf(e); const r = await dialog.showOpenDialog(d ? d.win : undefined, { properties: ['openDirectory'] });
  if (r.canceled || !r.filePaths[0]) return null;
  const out = {}; let count = 0;
  async function walk(dir, k) { if (k > 5 || count > 20000) return; let es; try { es = await fsp.readdir(dir, { withFileTypes: true }); } catch { return; }
    for (const x of es) { if (x.name.startsWith('.')) continue; const p = path.join(dir, x.name); if (x.isFile()) { count++; if (!out[x.name]) out[x.name] = p; } else if (x.isDirectory() && !x.name.endsWith('.app')) await walk(p, k + 1); } }
  await walk(r.filePaths[0], 0); return out;
});
ipcMain.on('al:dirty', (e, v) => { const d = docOf(e); if (d) { d.dirty = !!v; d.win.setDocumentEdited(!!v); } });
ipcMain.on('al:recovery', (e, json) => {
  const d = docOf(e); if (!d) return; const f = path.join(RECOVER(), d.id + '.json');
  try { if (!json) { fs.rmSync(f, { force: true }); return; } fs.mkdirSync(RECOVER(), { recursive: true }); fs.writeFileSync(f, JSON.stringify({ path: d.path, json })); } catch {}
});
ipcMain.on('al:close', (e) => { const d = docOf(e); if (d) { d.force = true; d.win.close(); } });

// ---------- スマホ連携 ----------
const remote = new Remote({ uiDir: () => uiDir, log, onCmd: (m) => { const d = lastActive; if (d && !d.win.isDestroyed()) d.win.webContents.send('al:rcmd', m); } });
ipcMain.handle('al:rinfo', async () => { await remote.ready; return remote.srv; });
ipcMain.handle('al:rstate', async (e, body) => { const d = docOf(e); if (d !== lastActive) return { phones: remote.phones() }; return remote.state(body); });
ipcMain.on('al:rframe', (e, buf) => { const d = docOf(e); if (d === lastActive) remote.frame(Buffer.from(buf)); });

// ---------- メニュー ----------
const send = (cmd) => { const w = BrowserWindow.getFocusedWindow(); const d = (w && !w.isDestroyed() && docs.get(w.webContents.id)) || lastActive; if (d && !d.win.isDestroyed()) d.win.webContents.send('al:menu', cmd); };
function buildMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: 'Alien Lab', submenu: [{ role: 'about', label: 'Alien Lab について' }, { type: 'separator' }, { label: '設定…', accelerator: 'Cmd+,', click: () => send('settings') }, { type: 'separator' }, { role: 'hide', label: 'Alien Lab を隠す' }, { role: 'hideOthers', label: 'ほかを隠す' }, { role: 'unhide', label: 'すべてを表示' }, { type: 'separator' }, { role: 'quit', label: 'Alien Lab を終了' }] },
    { label: 'ファイル', submenu: [
      { label: '新規', accelerator: 'Cmd+N', click: () => createWindow({}) },
      { label: '開く…', accelerator: 'Cmd+O', click: async () => { const r = await dialog.showOpenDialog({ properties: ['openFile', 'multiSelections'], filters: [{ name: 'Alien Lab', extensions: [EXT, 'json'] }] }); if (!r.canceled) for (const f of r.filePaths) await openPath(f); } },
      { role: 'recentDocuments', label: '最近使った項目', submenu: [{ role: 'clearRecentDocuments', label: 'メニューを消去' }] },
      { type: 'separator' },
      { role: 'close', label: '閉じる' },
      { label: '保存', accelerator: 'Cmd+S', click: () => send('save') },
      { label: '別名で保存…', accelerator: 'Shift+Cmd+S', click: () => send('saveAs') },
      { label: 'バックアップを作成', accelerator: 'Alt+Cmd+B', click: () => send('backup') },
    ] },
    { label: '編集', submenu: [{ role: 'undo', label: '取り消す' }, { role: 'redo', label: 'やり直す' }, { type: 'separator' }, { role: 'cut', label: 'カット' }, { role: 'copy', label: 'コピー' }, { role: 'paste', label: 'ペースト' }, { role: 'selectAll', label: 'すべてを選択' }] },
    { label: '表示', submenu: [{ role: 'togglefullscreen', label: 'フルスクリーン' }, { type: 'separator' }, { role: 'toggleDevTools', label: '開発者ツール' }] },
    { role: 'windowMenu', label: 'ウインドウ' },
  ]));
}

// ---------- 起動 ----------
const pendingOpen = [];
let ready = false, quitting = false;
app.on('open-file', (e, file) => { e.preventDefault(); if (ready) openPath(file); else pendingOpen.push(file); });
app.on('activate', () => { if (!docs.size && ready) startDocs(); });
app.on('before-quit', () => { quitting = true; });
app.on('window-all-closed', () => { if (SMOKE) app.quit(); });
app.on('will-quit', () => { applyShellUpdate(); remote.close(); });

async function startDocs() {
  if (pendingOpen.length) { for (const f of pendingOpen.splice(0)) await openPath(f); return; }
  // 落ちたときの続き
  let rec = []; try { rec = fs.readdirSync(RECOVER()).filter((f) => f.endsWith('.json')); } catch {}
  for (const f of rec) {
    try { const r = JSON.parse(fs.readFileSync(path.join(RECOVER(), f), 'utf8')); fs.rmSync(path.join(RECOVER(), f), { force: true });
      let media = {}; try { media = r.path ? await resolveMedia(r.path, JSON.parse(r.json)) : {}; } catch {}
      const w = createWindow({ path: r.path, text: r.json, media, name: r.path ? nameOf(r.path) : 'Untitled', dirty: true }); log('復旧', r.path || 'Untitled');
    } catch {}
  }
  if (docs.size) return;
  const st = readState();
  if (st.last && fs.existsSync(st.last)) { await openPath(st.last); return; }
  createWindow({ sample: !st.launched }); writeState({ launched: true });
}

app.whenReady().then(async () => {
  log('起動', app.getVersion(), process.argv.slice(1).join(' '));
  chooseUI(); registerProtocol(); buildMenu();
  const ok = new Set(['media', 'midi', 'midiSysex', 'fullscreen', 'local-fonts', 'clipboard-read', 'clipboard-sanitized-write', 'window-management', 'speaker-selection', 'pointerLock', 'keyboardLock']);
  session.defaultSession.setPermissionRequestHandler((wc, perm, cb) => cb(ok.has(perm)));
  session.defaultSession.setPermissionCheckHandler((wc, perm) => ok.has(perm));
  if (process.platform === 'darwin') { try { session.defaultSession.setDevicePermissionHandler(() => true); } catch {} }
  remote.start();
  ready = true;
  const argFiles = process.argv.slice(1).filter((a) => /\.(alienlab|json)$/i.test(a) && fs.existsSync(a)); pendingOpen.push(...argFiles);
  await startDocs();
  if (SMOKE) return smokeTest();
  setTimeout(updateUI, 8000); setInterval(updateUI, 30 * 60 * 1000);
  setTimeout(updateShell, 20000); setInterval(updateShell, 30 * 60 * 1000);
});

// ---------- CI の動作確認 ----------
function makeWav() { const sr = 44100, n = sr * 3, b = Buffer.alloc(44 + n * 4); b.write('RIFF', 0); b.writeUInt32LE(36 + n * 4, 4); b.write('WAVEfmt ', 8); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(2, 22); b.writeUInt32LE(sr, 24); b.writeUInt32LE(sr * 4, 28); b.writeUInt16LE(4, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(n * 4, 40);
  for (let i = 0; i < n; i++) { const v = Math.round(Math.sin(i / sr * 2 * Math.PI * 440) * 12000); b.writeInt16LE(v, 44 + i * 4); b.writeInt16LE(v, 46 + i * 4); } return b; }
async function smokeTest() {
  const out = (k, v) => process.stdout.write(`SMOKE ${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}\n`);
  const d = [...docs.values()][0];
  const wc = d.win.webContents;
  await new Promise((r) => wc.once('did-finish-load', r)).catch(() => {});
  await new Promise((r) => setTimeout(r, 3000));
  try {
    const res = await wc.executeJavaScript(`({ver: VERSION, app: !!window.alienApp, rows: document.querySelectorAll('.row').length, cues: allCount(), title: document.title})`);
    out('page', res);
    const tmp = path.join(app.getPath('temp'), 'smoke-' + Date.now() + '.' + EXT);
    d.path = tmp;
    const s = await wc.executeJavaScript('saveProject(false)');
    out('save', { ok: s, exists: fs.existsSync(tmp), bytes: fs.existsSync(tmp) ? fs.statSync(tmp).size : 0 });
    const media = await wc.executeJavaScript(`(async()=>{ const r=await fetch(APP.mediaURL(${JSON.stringify(tmp)}),{headers:{Range:'bytes=0-9'}}); return {status:r.status, len:(await r.arrayBuffer()).byteLength}; })()`);
    out('media-range', media);
    out('remote', remote.srv);
    // 素材ファイルの音が Web Audio に流れるか（alien:// が同じ出どころとして扱われるか）
    const wav = path.join(app.getPath('temp'), 'smoke-tone.wav'); fs.writeFileSync(wav, makeWav());
    const audio = await wc.executeJavaScript(`(async()=>{ ac(); registerMedia('smoke-tone.wav', APP.mediaURL(${JSON.stringify(wav)}), 'audio', ${JSON.stringify(wav)});
      const c=mk({type:'audio', number:'99', name:'smoke', file:'smoke-tone.wav'}); curList().cues.push(c); render(); fire(c,false);
      await new Promise(r=>setTimeout(r,1500)); const r=runsOf(c)[0]; const an=r&&r.player&&r.player.ax&&r.player.ax.anL; let peak=0;
      if(an){ const b=new Float32Array(an.fftSize); an.getFloatTimeDomainData(b); for(const x of b) peak=Math.max(peak,Math.abs(x)); }
      const cv=document.createElement('canvas'); cv.width=64; cv.height=36; let blob=null; try{ paintPgm(cv,64,36,1,false); blob=await new Promise(r=>cv.toBlob(r,'image/jpeg')); }catch(e){}
      stopAll(); return {phase:r&&r.phase, t:r&&r.player&&r.player.el&&+r.player.el.currentTime.toFixed(2), peak:+peak.toFixed(3), frame:!!blob}; })()`);
    out('audio', audio);
    // バックアップ → バックアップを開く
    await wc.executeJavaScript('doBackup()'); await new Promise((r) => setTimeout(r, 500));
    const bdir = path.join(path.dirname(tmp), 'Backup'); let bk = { ok: false };
    try { const f = fs.readdirSync(bdir).filter((x) => x.startsWith(nameOf(tmp))).sort().pop(); const folder = path.join(bdir, f);
      const hasMedia = fs.existsSync(path.join(folder, 'media', 'smoke-tone.wav'));
      const pj = path.join(folder, nameOf(tmp) + '.' + EXT); const data = JSON.parse(fs.readFileSync(pj, 'utf8'));
      const resolved = await resolveMedia(pj, data);
      bk = { ok: hasMedia && resolved['smoke-tone.wav'] === path.join(folder, 'media', 'smoke-tone.wav'), rel: data.media['smoke-tone.wav'] && data.media['smoke-tone.wav'].rel };
    } catch (e) { bk.error = e.message; }
    out('backup', bk);
    // 出力ウィンドウ（プロジェクター用）
    await wc.executeJavaScript('openOutputWindow(0)', true); await new Promise((r) => setTimeout(r, 800));
    const outWin = { windows: BrowserWindow.getAllWindows().length, kids: d.kids.size, ok: await wc.executeJavaScript('!!(OUTS[0].win && !OUTS[0].win.closed && OUTS[0].win.document.querySelector(".stagecanvas"))'),
      info: await wc.executeJavaScript('JSON.stringify({w:!!OUTS[0].win, closed:!!(OUTS[0].win&&OUTS[0].win.closed), log:LOG.slice(-3)})') };
    out('output', outWin);
    // スマホ側から操作できるか
    let phone = {};
    if (remote.mode === 'embedded') {
      await wc.executeJavaScript('rmPump()');
      const pr = await new Promise((res) => require('http').get(`http://127.0.0.1:${remote.srv.port}/alien/api/poll?v=-1&lv=-1&id=smoke`, (r) => { let b = ''; r.on('data', (d) => b += d); r.on('end', () => res(JSON.parse(b))); }).on('error', () => res({})));
      const before = await wc.executeJavaScript('playhead');
      await new Promise((res) => { const q = require('http').request(`http://127.0.0.1:${remote.srv.port}/alien/api/cmd`, { method: 'POST' }, (r) => { r.resume(); r.on('end', res); }); q.end(JSON.stringify({ c: 'next' })); });
      await new Promise((r) => setTimeout(r, 400));
      const after = await wc.executeJavaScript('playhead');
      const page = await new Promise((res) => require('http').get(`http://127.0.0.1:${remote.srv.port}/alien`, (r) => { let b = ''; r.on('data', (d) => b += d); r.on('end', () => res(b.includes('class="ctl"'))); }).on('error', () => res(false)));
      phone = { rows: pr.list && pr.list.rows && pr.list.rows.length, moved: before !== after, page };
      out('phone', phone);
    }
    out('result', res.app && res.rows > 0 && s && media.status === 206 && audio.frame && outWin.ok && bk.ok && (remote.mode !== 'embedded' || (phone.rows > 0 && phone.moved && phone.page)) ? 'OK' : 'NG');
  } catch (e) { out('error', e.message); out('result', 'NG'); }
  setTimeout(() => app.exit(0), 500);
}
