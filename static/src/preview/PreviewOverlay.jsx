// File preview overlay — CodeMirror 6 read-only.
//
// Entry points (all set previewPath.value = {path, line | null}):
//   1. Terminal Cmd+click on a path  (terminalCore link router → file handler)
//   2. Transcript tool-call file_path chip click
//   3. Sidebar Files row click
//
// Re-opening on a new path while the overlay is showing re-initializes
// CodeMirror in place (simple replace, no animated transition).
//
// Esc dismiss is via the shared useEscape LIFO. CodeMirror read-only does
// NOT autograb focus, so focus moves to the close button on open — keeps
// keystrokes from reaching xterm and gives Esc a target that bubbles to
// useEscape correctly.
//
// Visible-while-mounted: the overlay floats over .detail-pane-body (over
// terminal OR transcript content). The underlying terminal NEVER resizes
// (invariant: no tmux reflow on preview open).
//
// Bundle-weight discipline:
// - The eager wrapper (this file) holds NO CodeMirror imports. When
//   previewPath becomes non-null, we dynamically import a single inner
//   chunk that bundles the whole CM core + language packs. First open
//   pays one network round trip for the chunk; subsequent opens are
//   instant (cached). This keeps the main app.js out of the +267KB
//   CodeMirror weight that triggered the spec's lazy-load fallback.
import { useEffect, useState } from "preact/hooks";
import { previewPath } from "../store.js";

// Cache the imported module across re-mounts.
let _innerMod = null;
let _innerPromise = null;

function loadInner() {
  if (_innerMod) return Promise.resolve(_innerMod);
  if (!_innerPromise) {
    _innerPromise = import("./PreviewOverlayInner.jsx").then((m) => {
      _innerMod = m;
      return m;
    });
  }
  return _innerPromise;
}

export function PreviewOverlay() {
  const cur = previewPath.value;
  const [Inner, setInner] = useState(() => _innerMod?.PreviewOverlayInner || null);

  useEffect(() => {
    if (!cur || Inner) return;
    let alive = true;
    loadInner().then((m) => { if (alive) setInner(() => m.PreviewOverlayInner); });
    return () => { alive = false; };
  }, [cur, Inner]);

  if (!cur) return null;
  if (!Inner) {
    // First-open path: chunk still loading. Show the chrome immediately so
    // the user sees feedback; the CM-mounted body renders once Inner lands.
    return (
      <div class="preview-overlay" role="dialog" aria-label="File preview">
        <header class="preview-header">
          <span class="preview-path">{cur.path}{cur.line ? `:${cur.line}` : ""}</span>
          <button class="preview-btn" title="Close (Esc)" onClick={() => (previewPath.value = null)}>✕</button>
        </header>
        <div class="preview-body">
          <div class="preview-loading">loading…</div>
        </div>
      </div>
    );
  }
  return <Inner key={cur.path} entry={cur} />;
}
