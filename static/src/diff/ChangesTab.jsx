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

const SCOPES = [
  ["session", "This session", "Changes since the current Claude session started"],
  ["branch", "This branch", "Changes vs where this branch forked"],
];

function Line({ line }) {
  return (
    <div class={`dl dl-${line.kind}`}>
      <span class="dl-text">{line.text || " "}</span>
    </div>
  );
}

function FileBlock({ file }) {
  const [open, setOpen] = useState(true);
  const stat = (file.additions || 0) + (file.deletions || 0);
  return (
    <div class={`dfile dfile-${file.status}`}>
      <button type="button" class="dfile-head" onClick={() => setOpen(!open)}>
        <span class="dfile-caret">{open ? "▾" : "▸"}</span>
        <span class="dfile-path">{file.path}</span>
        <span class="dfile-status">{file.status}</span>
        <span class="dfile-stat">
          {file.additions ? <span class="dstat-add">+{file.additions}</span> : null}
          {file.deletions ? <span class="dstat-del">−{file.deletions}</span> : null}
          {!stat && file.status === "binary" ? <span class="dstat-bin">binary</span> : null}
        </span>
      </button>
      {open && file.hunks.map((h, i) => (
        <div class="dhunk" key={i}>
          <div class="dhunk-head">
            <span class="dhunk-range">@@ {h.new_start}</span>
            {h.header ? <span class="dhunk-sym">{h.header}</span> : null}
          </div>
          {h.lines.map((l, j) => <Line line={l} key={j} />)}
        </div>
      ))}
      {open && file.truncated ? (
        <div class="dfile-trunc">diff truncated — open the file to see the rest</div>
      ) : null}
    </div>
  );
}

export function ChangesTab({ target, active }) {
  const [scope, setScope] = useState("session");
  const [state, setState] = useState({ loading: true, error: null, data: null });

  // Refetch when the tab is shown or the scope flips. Like the preview tab,
  // load() only setStates after the fetch resolves, so switching scopes swaps
  // content without a loading flash.
  useEffect(() => {
    if (!active || !target) return undefined;
    let alive = true;
    (async () => {
      try {
        const res = await fetch(`/api/fs/diff?${targetQuery(target)}&scope=${scope}`);
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
  }, [target, scope, active]);

  const files = state.data?.files || [];
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
        {!state.loading && !state.error && (
          <span class="changes-summary">
            {files.length
              ? <>{files.length} file{files.length === 1 ? "" : "s"}
                  {" "}<span class="dstat-add">+{totals.a}</span>
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
        {files.map((f) => <FileBlock file={f} key={f.path} />)}
        {state.data?.truncated ? (
          <div class="changes-msg">
            {state.data.truncated} more file{state.data.truncated === 1 ? "" : "s"} not shown
          </div>
        ) : null}
      </div>
    </div>
  );
}
