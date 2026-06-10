// Heavy inner of the in-pane file preview tab. Imported dynamically by
// PreviewTab.jsx so the CodeMirror dependency stays out of the eager
// app.js bundle (see fallback note in PreviewTab.jsx).
//
// All CodeMirror packages (state, view, language) and every lang pack are
// statically imported here — they end up in one rolled-up chunk that's
// fetched on the first preview-tab open and then cached.
import { useEffect, useRef, useState } from "preact/hooks";
import { activeTarget } from "../store.js";

import { EditorState } from "@codemirror/state";
import { EditorView, lineNumbers, highlightActiveLineGutter, drawSelection } from "@codemirror/view";
import { syntaxHighlighting, HighlightStyle, bracketMatching } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";

// Encode "session:index" as base64url for the /api/fs/render path token.
// btoa() is bytes-from-string, so the input must be ASCII-safe; tmux
// targets are.
function paneToken(target) {
  return btoa(target).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// Build the iframe URL for an absolute file path. The path is encoded
// segment-by-segment so '#' / '?' / spaces don't break URL parsing, but
// '/' stays intact (the browser needs the real path structure for
// sibling-asset resolution to work).
function renderUrl(target, absPath) {
  const encoded = absPath.split("/").map(encodeURIComponent).join("/");
  // absPath starts with '/'; the split produces a leading "" segment, so
  // the joined string already begins with '/' — no double-slash here.
  return `/api/fs/render/${paneToken(target)}${encoded}`;
}

// Resolve doc-relative URLs (image src, link href) against the directory of
// the rendered file, served through /api/fs/render. ./ and ../ are
// normalized client-side for clean URLs; the server's safe_resolve still
// gates traversal, so this is cosmetic, not security.
function makeResolveUrl(target, resolvedPath) {
  const dir = resolvedPath.slice(0, resolvedPath.lastIndexOf("/"));
  return (raw) => {
    const out = [];
    for (const p of `${dir}/${raw}`.split("/")) {
      if (p === "." || (p === "" && out.length)) continue;
      if (p === "..") {
        if (out.length > 1) out.pop();
        continue;
      }
      out.push(p);
    }
    return renderUrl(target, out.join("/"));
  };
}

import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { markdown } from "@codemirror/lang-markdown";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { json } from "@codemirror/lang-json";
import { rust } from "@codemirror/lang-rust";

import { renderMarkdown } from "../split/markdown.jsx";
import { highlightCode } from "./highlightCode.jsx";

// Languages with a "rendered" view (toggleable to source). Everything
// else collapses to source-only — no toggle button.
const RENDERABLE = new Set(["html", "markdown"]);

// One-Dark-style palette tuned to match periscope's terminal theme
// (terminal/theme.js #282c34 bg, #e6edf3 fg). Picks a darker gutter so
// line numbers read as ambient context, not text. Selection/active-line
// kept subtle so they don't fight the syntax highlight.
const PREVIEW_THEME = EditorView.theme({
  "&": {
    backgroundColor: "#282c34",
    color: "#e6edf3",
    height: "100%",
  },
  ".cm-content": { caretColor: "#7aa2f7", padding: "8px 0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "#7aa2f7" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection": {
    backgroundColor: "rgba(88,166,255,0.22)",
  },
  ".cm-activeLine": { backgroundColor: "rgba(255,255,255,0.03)" },
  ".cm-activeLineGutter": { backgroundColor: "rgba(255,255,255,0.04)", color: "#abb2bf" },
  ".cm-gutters": {
    backgroundColor: "#21252b",
    color: "#5c6370",
    border: "none",
  },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 12px 0 8px" },
  ".cm-foldPlaceholder": { backgroundColor: "transparent", border: "none", color: "#5c6370" },
}, { dark: true });

const PREVIEW_HIGHLIGHT = HighlightStyle.define([
  { tag: t.keyword,                       color: "#c678dd" },
  { tag: [t.atom, t.bool, t.null, t.special(t.brace)], color: "#d19a66" },
  { tag: [t.number, t.literal],           color: "#d19a66" },
  { tag: t.definition(t.variableName),    color: "#e6edf3" },
  { tag: t.variableName,                  color: "#e06c75" },
  { tag: [t.function(t.variableName), t.function(t.propertyName), t.labelName], color: "#61afef" },
  { tag: t.propertyName,                  color: "#e6edf3" },
  { tag: [t.string, t.special(t.string)], color: "#98c379" },
  { tag: [t.regexp, t.escape, t.special(t.brace)], color: "#56b6c2" },
  { tag: t.comment,                       color: "#7f848e", fontStyle: "italic" },
  { tag: [t.typeName, t.className],       color: "#e5c07b" },
  { tag: t.operator,                      color: "#56b6c2" },
  { tag: [t.tagName, t.heading],          color: "#e06c75" },
  { tag: t.attributeName,                 color: "#d19a66" },
  { tag: t.link,                          color: "#61afef", textDecoration: "underline" },
  { tag: t.emphasis,                      fontStyle: "italic" },
  { tag: t.strong,                        fontWeight: "bold" },
]);

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

export function PreviewTabInner({ entry }) {
  const hostRef = useRef(null);
  const [state, setState] = useState({ loading: true, error: null, content: null, lang: null, resolved: null });
  // Renderable langs (HTML, Markdown) default to "rendered"; everything
  // else goes to source. A `:NN` line jump always wins → source, since
  // line numbers don't translate to the rendered view.
  const [view, setView] = useState(entry.line ? "source" : "rendered");

  // Target the file's pane: caller-provided (every entry carries the
  // target captured at openFileTab() time, so the fetch hits the pane
  // the tab was opened against — not whichever pane is currently active).
  // Falls back to activeTarget defensively for old call sites.
  const target = entry.target ?? activeTarget.value;

  // Fetch the file.
  useEffect(() => {
    let alive = true;
    async function load() {
      if (!target) {
        setState({ loading: false, error: "no active pane", content: null, lang: null, resolved: null });
        return;
      }
      const [session, indexStr] = target.split(":");
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

  // Effective view: non-renderable langs always collapse to source.
  const effectiveView = RENDERABLE.has(state.lang) ? view : "source";

  // Mount CodeMirror once content lands AND we're in source view.
  useEffect(() => {
    if (state.loading || state.error || effectiveView !== "source" || !hostRef.current || state.content == null) return;
    const langExt = languageExt(state.lang);
    // Local name is `editor` (not `view`) so it doesn't shadow the
    // component-level `view` toggle state declared above.
    const editor = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: state.content,
        extensions: [
          lineNumbers(),
          highlightActiveLineGutter(),
          drawSelection(),
          bracketMatching(),
          PREVIEW_THEME,
          syntaxHighlighting(PREVIEW_HIGHLIGHT, { fallback: true }),
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
        const line = editor.state.doc.line(Math.min(lineNo, editor.state.doc.lines));
        editor.dispatch({
          selection: { anchor: line.from, head: line.from },
          effects: EditorView.scrollIntoView(line.from, { y: "center" }),
        });
      }
    }
    return () => editor.destroy();
  }, [state.loading, state.error, state.content, state.lang, effectiveView]);

  async function reveal() {
    if (!target) return;
    const [session, indexStr] = target.split(":");
    const params = new URLSearchParams({
      session, index: indexStr, path: entry.path, action: "reveal",
    });
    try { await fetch(`/api/fs/open?${params.toString()}`, { method: "POST" }); }
    catch (_) {}
  }

  const iframeSrc = effectiveView === "rendered" && state.lang === "html" && state.resolved && target
    ? renderUrl(target, state.resolved)
    : null;

  return (
    <div class="preview-tab-content" role="region" aria-label="File preview">
      <header class="preview-header">
        <span class="preview-path">{state.resolved || entry.path}{entry.line ? `:${entry.line}` : ""}</span>
        {RENDERABLE.has(state.lang) && (
          <button
            class="preview-btn preview-btn-text"
            title={view === "rendered" ? "Show source" : "Show rendered"}
            onClick={() => setView(view === "rendered" ? "source" : "rendered")}
          >
            {view === "rendered" ? "Source" : "Rendered"}
          </button>
        )}
        <button class="preview-btn" title="Reveal in Finder" onClick={reveal}>⌖</button>
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
        {!state.loading && !state.error && effectiveView === "rendered" && state.lang === "html" && iframeSrc && (
          // allow-same-origin so the page's own scripts can fetch sibling
          // JSON/data files (common for self-contained dashboards). Periscope
          // is local-only, so granting the rendered page the same trust as
          // any other locally-opened HTML is acceptable.
          <iframe
            class="preview-iframe"
            src={iframeSrc}
            sandbox="allow-scripts allow-same-origin allow-popups"
            title="Rendered HTML preview"
          />
        )}
        {!state.loading && !state.error && effectiveView === "rendered" && state.lang === "markdown" && (
          // Document mode: real heading scale (no demote), CommonMark soft
          // breaks (hard-wrapped READMEs reflow), lezer-highlighted fences,
          // and doc-relative image/link URLs served via /api/fs/render.
          // .md-doc is a full parallel skin to the transcript's .turn-prose.
          <div class="preview-md-host">
            <div class="md-doc">
              {renderMarkdown(state.content || "", {
                demote: false,
                softBreaks: "space",
                highlight: highlightCode,
                resolveUrl: state.resolved ? makeResolveUrl(target, state.resolved) : null,
              })}
            </div>
          </div>
        )}
        {!state.loading && !state.error && effectiveView === "source" && (
          <div ref={hostRef} class="preview-cm-host" />
        )}
      </div>
    </div>
  );
}
