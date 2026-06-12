// Row components for the split-view rail, ported from rail.js's row-builder
// functions (paneRow / reviewRow / newTabRow / worktreeRow / repoRow +
// worktreeMetaLine). One component per row kind; all CSS class names and glyphs
// are preserved verbatim (rail-row, child-row, wt-row, repo-row, rail-chev,
// rail-icon, rail-label, rail-count, rail-close, rail-dim, newtab-row,
// last-in-worktree, review-empty, dot/dot-*, wt-meta*, drop-target).
//
// Two cross-cutting conventions:
//   - PR/Linear anchors call e.stopPropagation() in a real onClick (#4) so
//     clicking the link opens the PR, not the detail pane.
//   - Drag identity travels on the dragstart payload + handler args — NEVER a
//     previousElementSibling DOM walk (#6); the reorder math lives in Rail.jsx
//     against the merged tree, not the DOM.
//
// Rename is inline (double-click the label): the label becomes an uncontrolled
// <input>; Enter/blur commit, Escape cancels. A `settled` guard reproduces the
// vanilla Enter-then-blur double-submit protection.
import { useState, useRef, useEffect } from "preact/hooks";
import { prUrl } from "../util.js";

// Narrator status dims after 15 min: the work moved on (or the pane went
// quiet) and the one-liner no longer reflects "now".
const STATUS_STALE_S = 900;

export function statusDotClass(s) {
  if (s === "needs-input") return "dot dot-alert dot-pulse";
  if (s === "working") return "dot dot-green";
  if (s === "done") return "dot dot-blue dot-pulse-done";
  if (s === "idle") return "dot dot-grey";
  return "dot dot-none";
}

function ciClass(ci) {
  if (ci === "✓") return "ok";
  if (ci === "✗") return "bad";
  if (ci === "⟳") return "running";
  return "neutral";
}

// Inline-rename label. `kind` is "pane" or "worktree" (only those rename).
// `onCommit(next)` does the network write; `label` is the bold/plain display
// node. Non-renameable rows just render the label.
function RailLabel({ label, kind, renameable, onCommit }) {
  const [editing, setEditing] = useState(false);
  const inputRef = useRef(null);
  const settled = useRef(false);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  function begin(e) {
    if (!renameable) return;
    e.preventDefault();
    e.stopPropagation();
    settled.current = false;
    setEditing(true);
  }

  function commit(value) {
    if (settled.current) return;
    settled.current = true;
    setEditing(false);
    const next = (value || "").trim();
    if (next && next !== label) onCommit(next);
  }
  function cancel() {
    if (settled.current) return;
    settled.current = true;
    setEditing(false);
  }

  if (editing) {
    return (
      <span class="rail-label">
        <input
          ref={inputRef}
          type="text"
          class="rail-rename-input"
          // Uncontrolled (defaultValue): the rail re-renders every poll
          // (~3s) and a `value=` binding would overwrite the user's
          // in-flight keystrokes each tick. Same convention as the
          // Inspector's NotesEditor — see CLAUDE.md "Inspector inputs are
          // UNCONTROLLED".
          defaultValue={label}
          spellcheck={false}
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Enter") { e.preventDefault(); commit(e.currentTarget.value); }
            else if (e.key === "Escape") { e.preventDefault(); cancel(); }
          }}
          onBlur={(e) => commit(e.currentTarget.value)}
        />
      </span>
    );
  }
  // worktree/repo labels render bold; pane labels plain.
  return (
    <span class="rail-label" onDblClick={renameable ? begin : undefined}>
      {kind === "pane" ? label : <b>{label}</b>}
    </span>
  );
}

export function PaneRow({ w, chip, selectedKey, onSelect, onClose, onRename, dim, dragProps, dropPos, pinned, onTogglePin }) {
  const k = `pane:${w.pid}`;
  const sel = k === selectedKey ? " selected" : "";
  const dimCls = dim ? "" : " rail-dim";
  const drop = dropPos ? " drop-target" : "";
  const label = w.name || (w.is_claude ? "claude" : "shell");
  const statusStale =
    w.status_at && Math.floor(Date.now() / 1000) - w.status_at > STATUS_STALE_S;
  return (
    <div
      class={`rail-row child-row pane-row${sel}${dimCls}${drop}`}
      data-drop-pos={dropPos || undefined}
      draggable
      onClick={() => onSelect(k)}
      {...dragProps}
    >
      <div class="pane-row-main">
        {w.is_claude
          ? <span class="rail-icon icon-claude">✻</span>
          : <span class="rail-icon icon-shell">$</span>}
        <RailLabel label={label} kind="pane" renameable onCommit={onRename} />
        {chip && <span class="rail-chip" title={w.cwd}>⧉ {chip}</span>}
        {w.burn_hot && (
          <span
            class="rail-burn"
            title={`eating the session quota — ~${w.burn_wtpm || "?"} weighted tok/min over the last 30m`}
          >🔥</span>
        )}
        <button
          class={`rail-pin${pinned ? " pinned" : ""}`}
          title={pinned ? "unpin" : "pin"}
          onClick={(e) => { e.stopPropagation(); onTogglePin && onTogglePin(); }}
        >{pinned ? "★" : "☆"}</button>
        <span class={statusDotClass(w.state)}></span>
        <button
          class="rail-close"
          title="kill this tab"
          onClick={(e) => { e.stopPropagation(); onClose(); }}
        >×</button>
      </div>
      {w.status_line && (
        <span
          class={`rail-status${statusStale ? " stale" : ""}`}
          title={w.status_line}
        >{w.status_rail || w.status_line}</span>
      )}
    </div>
  );
}

export function ReviewRow({ worktreeKey, lgtmLive, selectedKey, onSelect, dragProps, dropPos }) {
  const k = `review:${worktreeKey}`;
  const sel = k === selectedKey ? " selected" : "";
  const empty = lgtmLive ? "" : " review-empty";
  const drop = dropPos ? " drop-target" : "";
  return (
    <div
      class={`rail-row child-row${sel}${empty}${drop}`}
      data-drop-pos={dropPos || undefined}
      draggable
      onClick={() => onSelect(k)}
      {...dragProps}
    >
      {lgtmLive
        ? <span class="rail-icon icon-review">◉</span>
        : <span class="rail-icon icon-review-empty">○</span>}
      <span class="rail-label">review{lgtmLive ? "" : <> <em>start →</em></>}</span>
    </div>
  );
}

export function NewTabRow({ worktreeKey, onOpen }) {
  return (
    <div
      class="rail-row child-row newtab-row last-in-worktree"
      onClick={() => onOpen(worktreeKey)}
    >
      <span class="rail-icon">+</span>
      <span class="rail-label">New tab</span>
    </div>
  );
}

// Compact per-worktree metadata strip: PR badge + CI glyph, Linear chip, git
// dirty indicator. Drawn from the worktree's first window (all panes share
// branch/repo). PR/Linear anchors stop propagation (#4).
export function WorktreeMeta({ wtWindows }) {
  const w = wtWindows[0];
  if (!w) return null;
  const parts = [];

  if (w.pr) {
    const href = prUrl(w.repo_slug, w.pr);
    const ciGlyph = w.ci
      ? <span class={`wt-meta-ci wt-meta-ci-${ciClass(w.ci)}`}>{w.ci}</span>
      : null;
    const inner = <>#{w.pr}{ciGlyph ? <> {ciGlyph}</> : null}</>;
    parts.push(
      href
        ? <a class="wt-meta-chip wt-meta-pr" href={href} target="_blank" rel="noopener" onClick={(e) => e.stopPropagation()} title={`PR #${w.pr}`}>{inner}</a>
        : <span class="wt-meta-chip wt-meta-pr" title={`PR #${w.pr}`}>{inner}</span>
    );
  }

  if (w.linked_linear) {
    const lid = w.linked_linear;
    const ltitle = w.linked_linear_title ? `: ${w.linked_linear_title}` : "";
    const lstatus = w.linked_linear_status ? ` [${w.linked_linear_status}]` : "";
    parts.push(
      <a class="wt-meta-chip wt-meta-linear" href={`https://linear.app/issue/${lid}`} target="_blank" rel="noopener" onClick={(e) => e.stopPropagation()} title={`Linear ${lid}${ltitle}${lstatus}`}>{lid}</a>
    );
  }

  if (w.git && w.git !== "clean") {
    parts.push(<span class="wt-meta-chip wt-meta-git" title="git status">{w.git}</span>);
  }

  if (!parts.length) return null;
  return <div class="wt-meta">{parts.map((p) => <>{p}</>)}</div>;
}

// Worktree header row (a project's session under its repo group). Dev has no
// worktree rows, so there's no catch-all variant. `collapsed` hides the
// children (rendered by the caller). Rename POSTs /api/session/rename.
export function WorktreeRow({
  worktreeKey, label, collapsed, childCount, rolledUp, dim,
  onToggle, onClose, onRename, dragProps, dropPos,
}) {
  const chev = collapsed ? "▸" : "▾";
  const dimCls = dim ? "" : " rail-dim";
  const drop = dropPos ? " drop-target" : "";
  return (
    <div
      class={`rail-row wt-row${dimCls}${drop}`}
      data-drop-pos={dropPos || undefined}
      draggable
      onClick={(e) => { if (e.target.closest("button") || e.target.closest("input")) return; onToggle(); }}
      {...dragProps}
    >
      <span class="rail-chev">{chev}</span>
      <span class="rail-icon icon-worktree">⎇</span>
      <RailLabel label={label} kind="worktree" renameable onCommit={onRename} />
      {collapsed && childCount > 0 ? <span class="rail-count">{childCount}</span> : null}
      <span class={statusDotClass(rolledUp)}></span>
      <button
        class="rail-close"
        title="kill this session"
        onClick={(e) => { e.stopPropagation(); onClose(); }}
      >×</button>
    </div>
  );
}

export function RepoRow({ repoKey, label, collapsed, rolledUp, dim, isDev, onToggle, dragProps, dropPos }) {
  const chev = collapsed ? "▸" : "▾";
  const dimCls = dim ? "" : " rail-dim";
  const drop = dropPos ? " drop-target" : "";
  // "dev" is pinned to the bottom — never draggable; omit the drag props.
  return (
    <div
      class={`rail-row repo-row${dimCls}${drop}`}
      data-drop-pos={dropPos || undefined}
      draggable={!isDev}
      onClick={onToggle}
      {...(isDev ? {} : dragProps)}
    >
      <span class="rail-chev">{chev}</span>
      {isDev
        ? <span class="rail-icon icon-other">◇</span>
        : <span class="rail-icon icon-repo">◆</span>}
      <span class="rail-label"><b>{label}</b></span>
      <span class={statusDotClass(rolledUp)}></span>
    </div>
  );
}
