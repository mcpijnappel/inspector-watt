// Theme: follows the system unless the user picked dark or light (remembered per browser).

export type ThemeMode = 'system' | 'dark' | 'light';

const media = typeof window !== 'undefined' ? window.matchMedia('(prefers-color-scheme: light)') : null;

function load(): ThemeMode {
  try {
    const v = localStorage.getItem('theme');
    if (v === 'dark' || v === 'light' || v === 'system') return v;
  } catch {
    /* storage blocked */
  }
  return 'system';
}

class Theme {
  mode = $state<ThemeMode>(load());
  systemLight = $state(media?.matches ?? false);
  resolved = $derived<'dark' | 'light'>(this.mode === 'system' ? (this.systemLight ? 'light' : 'dark') : this.mode);

  constructor() {
    media?.addEventListener('change', (e) => {
      this.systemLight = e.matches;
      this.apply();
    });
  }

  cycle() {
    this.mode = this.mode === 'system' ? 'dark' : this.mode === 'dark' ? 'light' : 'system';
    try {
      localStorage.setItem('theme', this.mode);
    } catch {
      /* storage blocked */
    }
    this.apply();
  }

  /** Sets data-theme on <html>. Called right away on every change (not from an effect), so charts that rebuild
   *  on a theme change read the new colours: on first render child effects run before the parent's. */
  apply() {
    document.documentElement.dataset.theme = this.resolved;
  }
}

export const theme = new Theme();
