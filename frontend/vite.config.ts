import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// `npm run dev` talks to the running stack: api on :8000, capture's live WebSocket on :8100.
export default defineConfig({
  plugins: [svelte()],
  server: {
    host: true,
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8100', ws: true, rewrite: (p) => p.replace(/^\/ws/, '') },
    },
  },
});
