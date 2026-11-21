// ページ本体を端末に保存しておき、サーバーのアドレスが変わっても開けるようにする
const CACHE = 'phone-light-v1';  // 保存先（変えると保存版が消えるので変えない）
const SW_VER = 2;
const PAGES = ['/', '/manifest.json'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PAGES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(self.clients.claim());
});

function withTimeout(p, ms) {
  return Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms))]);
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  // キューリストの画面：サーバーが生きていれば最新、止まっていれば保存版（停止中の案内が出る）
  if (url.pathname === '/operator') {
    e.respondWith((async () => {
      try {
        const res = await withTimeout(fetch(url.href, { cache: 'no-store', credentials: 'same-origin' }), 3000);
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put('/operator', copy)); return res; }
        throw new Error('bad');
      } catch (err) {
        const cached = await caches.match('/operator');
        if (cached) return cached;
        throw err;
      }
    })());
    return;
  }
  const isPage = (e.request.mode === 'navigate' && url.pathname === '/') || url.pathname === '/' || url.pathname === '/client.html';
  const isManifest = url.pathname === '/manifest.json';
  if (!isPage && !isManifest) return; // /api/ などはそのまま通す
  const keyPath = isPage ? '/' : '/manifest.json';
  // 保存版があれば即表示（開いた瞬間に真っ黒）。最新版は裏で取りに行き、次回から使う
  e.respondWith((async () => {
    const cached = await caches.match(keyPath);
    const update = withTimeout(fetch(url.href, { cache: 'no-store', credentials: 'same-origin' }), 8000).then((res) => {
      if (res.ok && (res.headers.get('content-type') || '').match(/html|json/)) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(keyPath, copy));
        return res;
      }
      throw new Error('bad ' + res.status);
    });
    if (cached) { e.waitUntil(update.catch(() => {})); return cached; }
    return update;
  })());
});
