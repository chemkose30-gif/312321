// TeamHub 서비스 워커: 알림(Web Push) 받기 + 앱 설치
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = {body: e.data && e.data.text()}; }
  e.waitUntil(self.registration.showNotification(d.title || 'TeamHub', {
    body: d.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: d.tag || 'teamhub',
    renotify: true,
    data: {url: d.url || '/'},
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL(e.notification.data && e.notification.data.url || '/', self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const w of wins) {
      if (new URL(w.url).origin === self.location.origin) {
        await w.focus();
        w.postMessage({type: 'open', url});
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
