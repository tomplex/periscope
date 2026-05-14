import { defineConfig } from "vite";

// Dev-only convenience: serves static/ with HMR and proxies API/WS to the
// FastAPI server on :8765. There's no build step — index.html still loads
// vendored xterm as plain <script> tags and app.js as a module, which works
// natively in both `npm run dev` (Vite) and `uv run server.py` (FastAPI).
export default defineConfig({
  root: "static",
  server: {
    host: "127.0.0.1",
    port: 5174,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
      // Vite's extensionless URL resolution picks up history.js for /history
      // (returns the JS source with Content-Type: text/html). Hand /history
      // off to FastAPI's explicit route instead, which serves history.html.
      "/history": "http://127.0.0.1:8765",
    },
  },
});
