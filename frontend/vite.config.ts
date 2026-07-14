import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // The dashboard API runs on :8000 in dev (docs/dashboard_ui_spec.md
      // section 6/24) — proxying here means the browser only ever talks
      // to :5173, so cookies stay same-site (no cross-origin cookie
      // complications on top of the CORS config api/main.py already has).
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // GET /api/ws is a WebSocket upgrade, not a plain HTTP request
        // — Vite's proxy does not forward upgrades by default.
        ws: true,
      },
    },
  },
})
