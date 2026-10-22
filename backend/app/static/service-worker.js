self.addEventListener('push', event => {
  let message = {};
  try { message = event.data ? event.data.json() : {}; } catch { /* Empty push is valid. */ }
  const title = message.title || 'New job match';
  const options = {
    body: message.body || 'A new opportunity matched your search profile.',
    icon: '/assets/jobharbor-icon.svg',
    badge: '/assets/jobharbor-icon.svg',
    tag: message.tag || 'jobharbor-job-alert',
    data: { url: message.url || '/notifications' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  let destination = new URL('/notifications', self.location.origin);
  try {
    const candidate = new URL(event.notification.data?.url || '/notifications', self.location.origin);
    if (candidate.origin === self.location.origin || candidate.protocol === 'https:') destination = candidate;
  } catch { /* Keep the local alerts page as a safe fallback. */ }
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
    if (destination.origin !== self.location.origin) return self.clients.openWindow(destination.href);
    const existing = clients.find(client => new URL(client.url).origin === self.location.origin);
    if (existing) {
      existing.navigate(destination.href);
      return existing.focus();
    }
    return self.clients.openWindow(destination.href);
  }));
});
