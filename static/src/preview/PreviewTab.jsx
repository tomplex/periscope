// Eager wrapper for an in-pane file preview tab. The heavy inner
// (PreviewTabInner.jsx) statically imports CodeMirror — a ~600KB chunk —
// so we only load it on first preview-tab mount.
//
// Each open file in a pane's tab strip mounts its own <PreviewTab>. The
// active tab is visible; inactive tabs are CSS-hidden (display:none) but
// stay mounted, so switching is instant and per-tab state (CM scroll
// position, Source/Rendered toggle) persists.
//
// Bundle-weight discipline:
// - This file holds NO CodeMirror imports. We dynamically import a
//   single inner chunk on first mount. First open pays one network
//   round trip for the chunk; subsequent opens are instant (cached).
import { useEffect, useState } from "preact/hooks";

// Cache the imported module across re-mounts.
let _innerMod = null;
let _innerPromise = null;

function loadInner() {
  if (_innerMod) return Promise.resolve(_innerMod);
  if (!_innerPromise) {
    _innerPromise = import("./PreviewTabInner.jsx").then((m) => {
      _innerMod = m;
      return m;
    }).catch((err) => {
      // Reset so a future open can retry from scratch (e.g., transient
      // network failure). Surface the error to the chrome via thrown rejection.
      _innerPromise = null;
      throw err;
    });
  }
  return _innerPromise;
}

export function PreviewTab({ entry }) {
  const [Inner, setInner] = useState(() => _innerMod?.PreviewTabInner || null);
  const [loadError, setLoadError] = useState(null);

  useEffect(() => {
    if (Inner) return;
    let alive = true;
    setLoadError(null);
    loadInner()
      .then((m) => { if (alive) setInner(() => m.PreviewTabInner); })
      .catch((err) => {
        if (!alive) return;
        setLoadError(err?.message || String(err));
      });
    return () => { alive = false; };
  }, [Inner]);

  if (loadError) {
    return (
      <div class="preview-tab-content" role="region" aria-label="File preview">
        <header class="preview-header">
          <span class="preview-path">{entry.path}</span>
        </header>
        <div class="preview-body">
          <div class="preview-error">
            <div>Failed to load preview module:</div>
            <div class="preview-error-code">{loadError}</div>
          </div>
        </div>
      </div>
    );
  }
  if (!Inner) {
    return (
      <div class="preview-tab-content" role="region" aria-label="File preview">
        <header class="preview-header">
          <span class="preview-path">{entry.path}{entry.line ? `:${entry.line}` : ""}</span>
        </header>
        <div class="preview-body">
          <div class="preview-loading">loading…</div>
        </div>
      </div>
    );
  }
  return <Inner entry={entry} />;
}
