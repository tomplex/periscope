// Heavy inner of the in-pane file preview tab. Imported dynamically by
// PreviewTab.jsx so the CodeMirror dependency stays out of the eager
// app.js bundle (see fallback note in PreviewTab.jsx).
//
// All CodeMirror packages (state, view, language) and every lang pack are
// statically imported here — they end up in one rolled-up chunk that's
// fetched on the first preview-tab open and then cached.

import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { bracketMatching, HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { Compartment, EditorState, Prec } from "@codemirror/state";
import { drawSelection, EditorView, highlightActiveLineGutter, keymap, lineNumbers } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";
import { useEffect, useRef, useState } from "preact/hooks";
import { activeTarget, docRefreshNonce } from "../store.js";

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
// `v` is an auto-refresh cache-buster (the file's mtime). It must live in the
// QUERY, not the path: relative sub-resource refs inside a rendered page strip
// the query, so they still resolve to real sibling files rather than inheriting
// a version segment that isn't part of any path.
function renderUrl(target, absPath, v = null) {
  const encoded = absPath.split("/").map(encodeURIComponent).join("/");
  // absPath starts with '/'; the split produces a leading "" segment, so
  // the joined string already begins with '/' — no double-slash here.
  const base = `/api/fs/render/${paneToken(target)}${encoded}`;
  return v ? `${base}?v=${encodeURIComponent(v)}` : base;
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

import { css } from "@codemirror/lang-css";
import { html } from "@codemirror/lang-html";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
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

// How often a VISIBLE preview tab asks whether its file moved. Only the active
// tab polls, so this is one request per visible document, not per open tab.
const STAT_POLL_MS = 2000;

// What the editor reconfigures INTO when not editing. A compartment swap
// rather than a remount: rebuilding the EditorView to flip one flag would
// throw away undo history and cursor position on every ✎ press.
const READ_ONLY = [EditorState.readOnly.of(true), EditorView.editable.of(false)];

export function PreviewTabInner({ entry, active = true }) {
  const hostRef = useRef(null);
  const editorRef = useRef(null);
  // Per-mount compartment holding READ_ONLY (or nothing, while editing).
  const editableComp = useRef(null);
  // Scroll position carried ACROSS a content-driven CodeMirror remount. Without
  // it every auto-refresh yanks you to the top of a document you are reading —
  // strictly worse than the manual close-and-reopen this replaces.
  const scrollMemo = useRef(null);
  // Last mtime the poller saw. A ref, not state: the first probe merely
  // ESTABLISHES the baseline, and if that were state it would count as a
  // change and re-fetch a file we just loaded on every tab open.
  const seenMtime = useRef(null);
  // Incremented only when the file actually moves. Drives the re-fetch and
  // busts the render-URL cache for iframe/image views; 0 is falsy, so a
  // never-refreshed tab carries no ?v= at all.
  const [refreshTick, setRefreshTick] = useState(0);
  const [copied, setCopied] = useState(false);
  const [state, setState] = useState({ loading: true, error: null, content: null, lang: null, resolved: null });
  // --- editing ---
  // `editing` gates BOTH the editor's writability and every refetch path
  // (poller, ⌘R, tab re-activation). Suspending refresh is the whole reason
  // edit mode is an explicit toggle rather than always-on: a document that
  // silently stopped tracking Claude's writes, with no visible cause, is
  // worse than one you have to click into.
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  // Set when the file moves under an open editor — either spotted by the
  // poller or reported by a 409 from the save itself.
  const [conflict, setConflict] = useState(false);
  // The mtime the loaded buffer corresponds to; the save precondition.
  const loadedMtime = useRef(null);
  // Read inside the poll closure and the CodeMirror keymap, both of which
  // outlive the render that created them.
  const editingRef = useRef(false);
  editingRef.current = editing;
  const saveRef = useRef(null);
  // Renderable langs (HTML, Markdown) default to "rendered"; everything
  // else goes to source. A `:NN` line jump always wins → source, since
  // line numbers don't translate to the rendered view.
  const [view, setView] = useState(entry.line ? "source" : "rendered");
  // Image click-to-zoom: false = fit-to-pane, true = actual (natural) size
  // with the host scrolling. `zoomable` gates the toggle to images that
  // are actually larger than the fitted display — no point zooming a
  // thumbnail. Set on <img> load by comparing natural vs. rendered size.
  const [zoomed, setZoomed] = useState(false);
  const [zoomable, setZoomable] = useState(false);

  // Target the file's pane: caller-provided (every entry carries the
  // target captured at openFileTab() time, so the fetch hits the pane
  // the tab was opened against — not whichever pane is currently active).
  // Falls back to activeTarget defensively for old call sites.
  const target = entry.target ?? activeTarget.value;

  // Fetch the file. Re-runs when the tab is (re-)shown so a file regenerated
  // while you were in another tab refreshes on return — the tab stays mounted,
  // so without the `active` dep it would freeze at open-time content forever.
  // load() only setStates after the fetch resolves, so a re-fetch never flashes
  // "loading…"; identical content is Object.is-equal and doesn't churn the
  // CodeMirror mount, preserving scroll position when nothing changed.
  useEffect(() => {
    // Editing suspends every refetch path — the buffer is the user's now.
    // Leaving edit mode re-runs this effect, which is what makes "Done" and
    // the conflict banner's Reload pull fresh content from disk.
    if (!active || editing) return undefined;
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
        loadedMtime.current = data.mtime ?? null;
        setDirty(false);          // the buffer now matches disk by definition
        setConflict(false);
        setSaveError(null);
        setState({ loading: false, error: null, content: data.content, lang: data.language, resolved: data.path });
      } catch (e) {
        if (alive) setState({ loading: false, error: String(e), content: null, lang: null, resolved: null });
      }
    }
    load();
    return () => { alive = false; };
    // docRefreshNonce is ⌘R's explicit "now" pull; refreshTick is the mtime
    // poller's. Both land here so there is one re-read path, not two.
  }, [entry.path, active, editing, refreshTick, docRefreshNonce.value]);

  // Auto-refresh: while this tab is visible, watch the file's mtime and let a
  // change drive the fetch effect above. Replaces the close-and-reopen cycle
  // that a Claude-edited document used to need.
  //
  // Polls /api/fs/stat rather than re-reading the body: the probe runs every
  // couple of seconds forever, and re-downloading a file to discover it is
  // unchanged is the expensive shape. A failed probe (deleted mid-rewrite,
  // server blip) is SWALLOWED — holding last-good content beats blanking a
  // document you are reading over a transient miss.
  useEffect(() => {
    if (!active || !target) return undefined;
    let alive = true;
    let timer = null;
    seenMtime.current = null;   // re-baseline when the path or pane changes
    const [session, indexStr] = target.split(":");
    const params = new URLSearchParams({ session, index: indexStr, path: entry.path });

    async function probe() {
      try {
        const res = await fetch(`/api/fs/stat?${params.toString()}`);
        if (!alive || !res.ok) return;
        const data = await res.json();
        if (!alive || typeof data.mtime !== "number") return;
        if (seenMtime.current === null) {
          seenMtime.current = data.mtime;      // baseline only — no refetch
        } else if (data.mtime > seenMtime.current) {
          seenMtime.current = data.mtime;
          // While editing, a disk change can't drive a refetch without
          // destroying the buffer — surface it as a conflict and let the
          // user choose. The save would 409 on it anyway; spotting it here
          // just means finding out before typing another paragraph.
          if (editingRef.current) setConflict(true);
          else setRefreshTick((t) => t + 1);
        }
      } catch (_) { /* transient — keep last-good content and retry next tick */ }
      finally {
        if (alive) timer = setTimeout(probe, STAT_POLL_MS);
      }
    }
    probe();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [entry.path, active, target]);

  // Effective view: non-renderable langs always collapse to source.
  const effectiveView = RENDERABLE.has(state.lang) ? view : "source";

  // Mount CodeMirror once content lands AND we're in source view.
  useEffect(() => {
    if (state.loading || state.error || effectiveView !== "source" || !hostRef.current || state.content == null) return;
    const langExt = languageExt(state.lang);
    const comp = new Compartment();
    editableComp.current = comp;
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
          history(),
          keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
          // Prec.highest so ⌘S beats anything else claiming the chord, and
          // returning true is what stops the browser's Save Page dialog.
          // Goes through a ref because this keymap is built once at mount
          // and the save closure changes every render.
          Prec.highest(keymap.of([
            { key: "Mod-s", run: () => { saveRef.current?.(); return true; } },
          ])),
          PREVIEW_THEME,
          syntaxHighlighting(PREVIEW_HIGHLIGHT, { fallback: true }),
          comp.of(editingRef.current ? [] : READ_ONLY),
          EditorView.updateListener.of((u) => { if (u.docChanged) setDirty(true); }),
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
    } else if (scrollMemo.current) {
      // Restore where the reader was. rAF because CodeMirror has not measured
      // content height yet on this frame, and a scrollTop past the current
      // (still zero-ish) scrollHeight clamps to 0 — silently losing the
      // position we are trying to keep. An explicit line jump outranks this.
      const want = scrollMemo.current;
      requestAnimationFrame(() => {
        if (editorRef.current === editor) editor.scrollDOM.scrollTop = want;
      });
    }
    editorRef.current = editor;
    return () => {
      // Runs before the next mount's body, so the memo is in place by the time
      // the replacement editor wants it.
      scrollMemo.current = editor.scrollDOM.scrollTop;
      editorRef.current = null;
      editableComp.current = null;
      editor.destroy();
    };
  }, [state.loading, state.error, state.content, state.lang, effectiveView]);

  // Flip writability in place. state.content is a dep so a remount (refetch,
  // Source/Rendered switch) re-applies the current mode to the new editor.
  useEffect(() => {
    const editor = editorRef.current;
    if (!editor || !editableComp.current) return;
    editor.dispatch({ effects: editableComp.current.reconfigure(editing ? [] : READ_ONLY) });
    if (editing) editor.focus();
  }, [editing, state.content, effectiveView]);

  // Line jumps AFTER mount — clicking another hunk for a file that's already
  // open. Kept out of the mount effect so changing the target line re-scrolls
  // instead of tearing down and rebuilding the whole editor.
  useEffect(() => {
    const editor = editorRef.current;
    const lineNo = Number(entry.line);
    if (!editor || !Number.isFinite(lineNo) || lineNo <= 0) return;
    const line = editor.state.doc.line(Math.min(lineNo, editor.state.doc.lines));
    editor.dispatch({
      selection: { anchor: line.from, head: line.from },
      effects: EditorView.scrollIntoView(line.from, { y: "center" }),
    });
  }, [entry.line]);

  // Write the buffer back. `force` (the conflict banner's Overwrite) sends a
  // null precondition, the one way past the server's mtime check.
  async function save({ force = false } = {}) {
    const editor = editorRef.current;
    if (!editor || !editingRef.current || saving || !target) return;
    setSaving(true);
    setSaveError(null);
    const [session, indexStr] = target.split(":");
    try {
      const res = await fetch("/api/fs/write", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session, index: Number(indexStr), path: entry.path,
          content: editor.state.doc.toString(),
          base_mtime: force ? null : loadedMtime.current,
        }),
      });
      if (res.status === 409) { setConflict(true); return; }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setSaveError(body.detail || `HTTP ${res.status}`);
        return;
      }
      const data = await res.json();
      // Advance BOTH baselines. seenMtime is what the poller compares
      // against, so skipping it makes our own write look like someone
      // else's the next time it ticks — a conflict banner on a file only
      // we touched.
      loadedMtime.current = data.mtime;
      seenMtime.current = data.mtime;
      setDirty(false);
      setConflict(false);
    } catch (e) {
      setSaveError(String(e));
    } finally {
      setSaving(false);
    }
  }
  saveRef.current = save;

  function startEditing() {
    setView("source");   // line numbers and a caret, not a rendered page
    setEditing(true);
  }

  // Leaving edit mode always re-reads from disk (the fetch effect), so
  // unsaved work is genuinely gone — confirm before discarding it.
  function stopEditing() {
    if (dirty && !window.confirm("Discard unsaved changes?")) return;
    setEditing(false);
    setConflict(false);
    setSaveError(null);
  }

  // Copy the RESOLVED absolute path, not entry.path: what you opened may have
  // been relative to the pane's cwd, and a relative path is useless once
  // pasted into another pane, a commit message, or a message to Claude.
  // Falls back to entry.path only if resolution never landed (error state).
  async function copyPath() {
    const text = state.resolved || entry.path;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch (_) { /* clipboard denied — the path is on screen either way */ }
  }

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
    ? renderUrl(target, state.resolved, refreshTick)
    : null;

  const imageSrc = state.lang === "image" && state.resolved && target
    ? renderUrl(target, state.resolved, refreshTick)
    : null;

  // Text we successfully read is editable; images (no content) and error
  // states are not.
  const editable = !state.loading && !state.error && state.content != null && state.lang !== "image";

  return (
    <div class="preview-tab-content" role="region" aria-label="File preview">
      <header class="preview-header">
        <span class="preview-path">
          {state.resolved || entry.path}{entry.line ? `:${entry.line}` : ""}
          {dirty && <span class="preview-dirty" title="Unsaved changes">●</span>}
        </span>
        {/* Hidden while editing: switching to the rendered view unmounts the
            editor, which would throw the buffer away. */}
        {!editing && RENDERABLE.has(state.lang) && (
          <button
            class="preview-btn preview-btn-text"
            title={view === "rendered" ? "Show source" : "Show rendered"}
            onClick={() => setView(view === "rendered" ? "source" : "rendered")}
          >
            {view === "rendered" ? "Source" : "Rendered"}
          </button>
        )}
        {editing ? (
          <>
            <button
              class="preview-btn preview-btn-text preview-btn-save"
              disabled={!dirty || saving}
              title="Save (⌘S)"
              onClick={() => save()}
            >{saving ? "Saving…" : "Save"}</button>
            <button
              class="preview-btn preview-btn-text"
              title="Stop editing and reload from disk"
              onClick={stopEditing}
            >Done</button>
          </>
        ) : editable && (
          <button class="preview-btn" title="Edit this file" onClick={startEditing}>✎</button>
        )}
        <button
          class="preview-btn"
          title={copied ? "Copied" : "Copy path"}
          onClick={copyPath}
        >{copied ? "✓" : "⧉"}</button>
        <button class="preview-btn" title="Reveal in Finder" onClick={reveal}>⌖</button>
      </header>
      {/* Sibling of .preview-body, not a child: every body child is
          position:absolute inset:0, so a banner inside would render
          underneath the editor rather than above it. */}
      {conflict && (
        <div class="preview-conflict" role="alert">
          <span>This file changed on disk since you opened it.</span>
          <button
            class="preview-btn preview-btn-text"
            title="Discard your edits and reload the file from disk"
            onClick={stopEditing}
          >Reload</button>
          <button
            class="preview-btn preview-btn-text"
            title="Write your version over the one on disk"
            onClick={() => save({ force: true })}
          >Overwrite</button>
        </div>
      )}
      {saveError && <div class="preview-conflict" role="alert"><span>Save failed: {saveError}</span></div>}
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
        {!state.loading && !state.error && imageSrc && (
          <div class="preview-image-host">
            <img
              class={`preview-image${zoomed ? " zoomed" : ""}${zoomable ? " zoomable" : ""}`}
              src={imageSrc}
              alt={state.resolved || entry.path}
              onLoad={(e) => {
                const img = e.currentTarget;
                setZoomable(img.naturalWidth > img.clientWidth || img.naturalHeight > img.clientHeight);
              }}
              onClick={() => zoomable && setZoomed((z) => !z)}
            />
          </div>
        )}
        {!state.loading && !state.error && !imageSrc && effectiveView === "source" && (
          <div ref={hostRef} class="preview-cm-host" />
        )}
      </div>
    </div>
  );
}
