// Heavy inner of the file preview overlay. Imported dynamically by
// PreviewOverlay.jsx so the CodeMirror dependency stays out of the eager
// app.js bundle (see fallback note in PreviewOverlay.jsx).
//
// All CodeMirror packages (state, view, language) and every lang pack are
// statically imported here — they end up in one rolled-up chunk that's
// fetched on the first preview open and then cached.
import { useEffect, useRef, useState } from "preact/hooks";
import { previewPath, activeTarget } from "../store.js";
import { useEscape } from "../hooks/useEscape.js";

import { EditorState } from "@codemirror/state";
import { EditorView, lineNumbers, highlightActiveLineGutter, drawSelection } from "@codemirror/view";
import { syntaxHighlighting, defaultHighlightStyle, bracketMatching } from "@codemirror/language";

import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { markdown } from "@codemirror/lang-markdown";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { json } from "@codemirror/lang-json";
import { rust } from "@codemirror/lang-rust";

function languageExt(name) {
  switch (name) {
    case "javascript": return javascript();
    case "python": return python();
    case "markdown": return markdown();
    case "html": return html();
    case "css": return css();
    case "json": return json();
    case "rust": return rust();
    default: return null;
  }
}

export function PreviewOverlayInner({ entry }) {
  const hostRef = useRef(null);
  const closeBtnRef = useRef(null);
  const [state, setState] = useState({ loading: true, error: null, content: null, lang: null, resolved: null });

  function close() { previewPath.value = null; }
  useEscape(close, true);

  // Fetch the file.
  useEffect(() => {
    let alive = true;
    async function load() {
      const t = activeTarget.value;
      if (!t) {
        setState({ loading: false, error: "no active pane", content: null, lang: null, resolved: null });
        return;
      }
      const [session, indexStr] = t.split(":");
      const params = new URLSearchParams({
        session, index: indexStr, path: entry.path,
      });
      try {
        const res = await fetch(`/api/fs/read?${params.toString()}`);
        if (!alive) return;
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setState({ loading: false, error: body.detail || `HTTP ${res.status}`, errorCode: res.status, content: null, lang: null, resolved: null });
          return;
        }
        const data = await res.json();
        setState({ loading: false, error: null, content: data.content, lang: data.language, resolved: data.path });
      } catch (e) {
        if (alive) setState({ loading: false, error: String(e), content: null, lang: null, resolved: null });
      }
    }
    load();
    return () => { alive = false; };
  }, [entry.path]);

  // Mount CodeMirror once content lands.
  useEffect(() => {
    if (state.loading || state.error || !hostRef.current || state.content == null) return;
    const langExt = languageExt(state.lang);
    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: state.content,
        extensions: [
          lineNumbers(),
          highlightActiveLineGutter(),
          drawSelection(),
          bracketMatching(),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          ...(langExt ? [langExt] : []),
        ],
      }),
    });
    // :NN line jump — EditorView.scrollIntoView returns a state effect.
    if (entry.line) {
      const lineNo = Number(entry.line);
      if (Number.isFinite(lineNo) && lineNo > 0) {
        const line = view.state.doc.line(Math.min(lineNo, view.state.doc.lines));
        view.dispatch({
          selection: { anchor: line.from, head: line.from },
          effects: EditorView.scrollIntoView(line.from, { y: "center" }),
        });
      }
    }
    return () => view.destroy();
  }, [state.loading, state.error, state.content, state.lang]);

  // Focus the close button on mount so keystrokes don't reach xterm.
  useEffect(() => {
    closeBtnRef.current?.focus();
  }, []);

  async function reveal() {
    const t = activeTarget.value;
    if (!t) return;
    const [session, indexStr] = t.split(":");
    const params = new URLSearchParams({
      session, index: indexStr, path: entry.path, action: "reveal",
    });
    try { await fetch(`/api/fs/open?${params.toString()}`, { method: "POST" }); }
    catch (_) {}
  }

  return (
    <div class="preview-overlay" role="dialog" aria-label="File preview">
      <header class="preview-header">
        <span class="preview-path">{state.resolved || entry.path}{entry.line ? `:${entry.line}` : ""}</span>
        <button class="preview-btn" title="Reveal in Finder" onClick={reveal}>⌖</button>
        <button class="preview-btn" title="Close (Esc)" onClick={close} ref={closeBtnRef}>✕</button>
      </header>
      <div class="preview-body">
        {state.loading && <div class="preview-loading">loading…</div>}
        {state.error && (
          <div class="preview-error">
            <div>{state.error}</div>
            {state.errorCode && <div class="preview-error-code">HTTP {state.errorCode}</div>}
            {state.errorCode === 415 && (
              <button class="preview-btn-large" onClick={reveal}>Open in Finder</button>
            )}
          </div>
        )}
        {!state.loading && !state.error && <div ref={hostRef} class="preview-cm-host" />}
      </div>
    </div>
  );
}
