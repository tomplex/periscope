// The pane's Changes tab: a git-backed diff of the worktree, in two scopes.
//
//   branch  — everything this branch has done vs its fork point
//   session — everything since the current Claude session started
//
// Git-backed rather than transcript-derived on purpose: it catches edits made
// by Bash, by you in another editor, and by any tool — the JSONL only ever knew
// about Edit/Write calls. Git also does the line matching, so there's no
// diffing algorithm here; we render the hunks the server parsed.
import { useEffect, useState } from "preact/hooks";
import { targetQuery } from "../util.js";
import { withIntraline } from "./intraline.js";
import {
  diffReview,
  fileState,
  setCollapsed,
  setViewed,
  viewedCount,
} from "./reviewState.js";

const SCOPES = [
  ["session", "This session", "Changes since the current Claude session started"],
  ["branch", "This branch", "Changes vs where this branch forked"],
];

// path -> the language key highlightCode understands.
const LANG_BY_EXT = {
  js: "js", mjs: "js", cjs: "js", jsx: "jsx", ts: "ts", tsx: "tsx",
  py: "py", rs: "rs", json: "json", css: "css",
  html: "html", htm: "html", md: "md", markdown: "md",
};

function langFor(path) {
  const leaf = (path || "").split("/").pop() || "";
  const dot = leaf.lastIndexOf(".");
  return dot > 0 ? LANG_BY_EXT[leaf.slice(dot + 1).toLowerCase()] || "" : "";
}

// Syntax colour is foreground, the intraline mark is background, so the two
// compose instead of fighting (the row tint already says add/delete).
// Segments always break on token boundaries, so highlighting each separately
// only mis-tokenizes in the rare case where a change lands inside a string
// literal — worth it to keep the changed span exact.
function Text({ text, lang, hl }) {
  if (!hl || !lang) return text || " ";
  try {
    return hl(text, lang);
  } catch {
    return text || " ";
  }
}

function Line({ line, lang, hl }) {
  return (
    <div class={`dl dl-${line.kind}`}>
      <span class="dl-text">
        {line.segs
          ? line.segs.map((s, i) => (
              <span class={s.changed ? "dl-intra" : ""} key={i}>
                <Text text={s.text} lang={lang} hl={hl} />
              </span>
            ))
          : <Text text={line.text} lang={lang} hl={hl} />}
      </span>
    </div>
  );
}

function FileBlock({ file, repo, review, hl }) {
  const { collapsed, viewed } = fileState(review, repo, file.path, file.sig);
  const open = !collapsed;
  const lang = langFor(file.path);
  const stat = (file.additions || 0) + (file.deletions || 0);
  return (
    <div class={`dfile dfile-${file.status}${viewed ? " is-viewed" : ""}`}>
      <div class="dfile-head">
        <button
          type="button"
          class="dfile-toggle"
          onClick={() => setCollapsed(repo, file.path, open)}
        >
          <span class="dfile-caret">{open ? "▾" : "▸"}</span>
          <span class="dfile-path">{file.path}</span>
          <span class="dfile-status">{file.status}</span>
          <span class="dfile-stat">
            {file.additions ? <span class="dstat-add">+{file.additions}</span> : null}
            {file.deletions ? <span class="dstat-del">−{file.deletions}</span> : null}
            {!stat && file.status === "binary" ? <span class="dstat-bin">binary</span> : null}
          </span>
        </button>
        <button
          type="button"
          class={`dfile-viewed${viewed ? " is-on" : ""}`}
          title={viewed
            ? "Marked viewed — re-surfaces automatically if this file changes again"
            : "Mark viewed (folds it away until it changes again)"}
          onClick={() => setViewed(repo, file.path, file.sig, !viewed)}
        >{viewed ? "✓ viewed" : "viewed"}</button>
      </div>
      {open && file.hunks.map((h, i) => (
        <div class="dhunk" key={i}>
          <div class="dhunk-head">
            <span class="dhunk-range">@@ {h.new_start}</span>
            {h.header ? <span class="dhunk-sym">{h.header}</span> : null}
          </div>
          {withIntraline(h.lines).map((l, j) => (
            <Line line={l} lang={lang} hl={hl} key={j} />
          ))}
        </div>
      ))}
      {open && file.truncated ? (
        <div class="dfile-trunc">diff truncated — open the file to see the rest</div>
      ) : null}
    </div>
  );
}

const CONTEXTS = [3, 10, 25];

export function ChangesTab({ target, active, gitSig }) {
  const [scope, setScope] = useState("session");
  const [context, setContext] = useState(3);
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [hl, setHl] = useState(null);

  // The lezer parsers live in the lazy `preview` chunk (vite manualChunks), so
  // pull them in only once the tab is actually opened. Until they land — or if
  // the chunk fails — lines render as plain text rather than not at all.
  useEffect(() => {
    if (!active || hl) return undefined;
    let alive = true;
    import("../preview/highlightCode.jsx")
      .then((m) => { if (alive) setHl(() => m.highlightCode); })
      .catch(() => {});
    return () => { alive = false; };
  }, [active, hl]);

  // Refetch when the tab is shown, the scope flips, or the worktree's git
  // summary moves. `gitSig` is the pane's `git` field from /api/state ("+14 -6
  // ?2") — it changes exactly when the working tree does, so it makes the tab
  // live off the state push instead of a poll of its own. Only setStates after
  // the fetch resolves, so a refresh never flashes a loading state.
  useEffect(() => {
    if (!active || !target) return undefined;
    let alive = true;
    (async () => {
      try {
        const res = await fetch(
          `/api/fs/diff?${targetQuery(target)}&scope=${scope}&context=${context}`);
        if (!alive) return;
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setState({ loading: false, error: body.detail || `HTTP ${res.status}`, data: null });
          return;
        }
        setState({ loading: false, error: null, data: await res.json() });
      } catch (e) {
        if (alive) setState({ loading: false, error: String(e), data: null });
      }
    })();
    return () => { alive = false; };
  }, [target, scope, active, gitSig, context]);

  const files = state.data?.files || [];
  const repo = state.data?.repo || "";
  const review = diffReview.value;
  const nViewed = viewedCount(review, repo, files);
  const totals = files.reduce(
    (acc, f) => ({ a: acc.a + (f.additions || 0), d: acc.d + (f.deletions || 0) }),
    { a: 0, d: 0 },
  );

  return (
    <div class="changes-tab">
      <header class="changes-head">
        <span class="changes-scope">
          {SCOPES.map(([key, label, tip]) => (
            <button
              type="button"
              key={key}
              class={scope === key ? "is-active" : ""}
              title={tip}
              onClick={() => setScope(key)}
            >{label}</button>
          ))}
        </span>
        <span class="changes-ctx" title="Lines of context around each change">
          {CONTEXTS.map((n) => (
            <button
              type="button"
              key={n}
              class={context === n ? "is-active" : ""}
              onClick={() => setContext(n)}
            >{n}</button>
          ))}
        </span>
        {!state.loading && !state.error && (
          <span class="changes-summary">
            {files.length
              ? <>{nViewed ? `${nViewed}/${files.length} viewed · ` : `${files.length} file${files.length === 1 ? "" : "s"} · `}
                  <span class="dstat-add">+{totals.a}</span>
                  {" "}<span class="dstat-del">−{totals.d}</span></>
              : "no changes"}
          </span>
        )}
      </header>
      <div class="changes-body">
        {state.loading ? <div class="changes-msg">loading…</div> : null}
        {state.error ? <div class="changes-msg changes-err">{state.error}</div> : null}
        {!state.loading && !state.error && !files.length ? (
          <div class="changes-msg">
            {scope === "session"
              ? "Nothing changed on disk since this session started."
              : "This branch matches its fork point."}
          </div>
        ) : null}
        {files.map((f) => (
          <FileBlock file={f} repo={repo} review={review} hl={hl} key={f.path} />
        ))}
        {state.data?.truncated ? (
          <div class="changes-msg">
            {state.data.truncated} more file{state.data.truncated === 1 ? "" : "s"} not shown
          </div>
        ) : null}
      </div>
    </div>
  );
}
