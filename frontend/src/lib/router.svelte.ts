// Minimal hash router: #/live, #/sessions, #/sessions/12, #/compare?a=1&b=2, #/settings/storage

export type Route = { name: 'live' | 'sessions' | 'session' | 'compare' | 'settings'; id?: number; section?: string; query: URLSearchParams };

function parse(hash: string): Route {
  const [path, qs = ''] = hash.replace(/^#\/?/, '').split('?');
  const query = new URLSearchParams(qs);
  const parts = path.split('/').filter(Boolean);
  if (parts[0] === 'sessions' && parts[1] && /^\d+$/.test(parts[1])) return { name: 'session', id: Number(parts[1]), query };
  if (parts[0] === 'sessions') return { name: 'sessions', query };
  if (parts[0] === 'compare') return { name: 'compare', query };
  if (parts[0] === 'settings') return { name: 'settings', section: parts[1], query };
  if (parts[0] === 'devices') return { name: 'settings', section: 'devices', query }; // the old Devices page
  return { name: 'live', query };
}

class Router {
  route = $state<Route>(parse(location.hash));

  constructor() {
    window.addEventListener('hashchange', () => (this.route = parse(location.hash)));
  }
}

export const router = new Router();
