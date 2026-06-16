import { defineConfig } from "vite";
import preact from "@preact/preset-vite";
import { fileURLToPath } from "node:url";

// Dev: serves static/ with HMR and proxies API/WS to FastAPI on :8766 —
// the dev backend port (dev.sh sets PERISCOPE_PORT=8766), kept off the
// :8765 prod instance so HMR development never reclaims prod or spends Haiku.
// Build: bundles the Preact app under static/src/ into a committed
// static/dist/app.js (fixed name, no content hash) so index.html references a
// stable path and `bin/periscope restart` needs no build step. Production
// still loads styles.css + vendored xterm as plain <script> tags; only the
// app JS goes through the build now.
export default defineConfig({
  root: "static",
  plugins: [preact()],
  build: {
    outDir: "dist",
    // Don't wipe dist/ — the committed bundle is the only build artifact and
    // we never want the build to nuke it before the new one lands.
    emptyOutDir: false,
    rollupOptions: {
      input: fileURLToPath(new URL("./static/src/main.jsx", import.meta.url)),
      output: {
        // Stable filenames → index.html references fixed paths, no churn.
        entryFileNames: "app.js",
        assetFileNames: "[name][extname]",
        // Dynamic-import chunks land at static/dist/chunks/<name>.js with
        // stable hash-free names so they can be committed alongside app.js.
        // PreviewTabInner (CodeMirror) is the only such chunk today.
        chunkFileNames: "chunks/[name].js",
        // Roll every CodeMirror/lezer dep + the lazy preview modules into a
        // single chunk. Without this Vite splits each lang pack out
        // separately and PreviewTabInner ends up importing from a sibling
        // CodeMirror chunk — fragile, and broke once already during the
        // overlay→tab rename. Matched by name, NOT by directory:
        // preview/PreviewTab.jsx is the EAGER wrapper that dynamic-imports
        // the rest; routing it here would make app.js import the chunk
        // statically and defeat the lazy split.
        manualChunks(id) {
          if (
            id.includes("/@codemirror/") ||
            id.includes("/@lezer/") ||
            id.includes("/preview/PreviewTabInner") ||
            id.includes("/preview/highlightCode")
          ) {
            return "preview";
          }
          return "app";
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5174,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8766",
      "/ws": { target: "ws://127.0.0.1:8766", ws: true },
      // Vite's extensionless URL resolution picks up history.js for /history
      // (returns the JS source with Content-Type: text/html). Hand /history
      // off to FastAPI's explicit route instead, which serves history.html.
      "/history": "http://127.0.0.1:8766",
    },
  },
});
