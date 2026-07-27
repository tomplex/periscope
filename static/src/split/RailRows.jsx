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
import { useEffect, useRef, useState } from "preact/hooks";
import { memHint, prStateMeta, prUrl, relTime } from "../util.js";
import { waitTier } from "./attention.js";

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

// Status-line model field carries a parenthetical context window ("Opus 4.8
// (1M context)"); the rail badge only wants the model name.
function shortModel(m) {
  if (!m) return null;
  return m.replace(/\s*\(.*$/, "").trim();
}

// Context-window fill drives a quiet→warn→hot color ramp: a pane near its
// context limit is the one about to compact / lose history.
function ctxClass(p) {
  if (p == null) return "";
  if (p >= 80) return " ctx-hot";
  if (p >= 60) return " ctx-warn";
  return "";
}

// A worktree's git field is "clean" / "clean *" (the `*` means unpushed
// commits, not untracked files) when there's nothing to surface; anything
// else ("+149 -118", "?3", "+12 -3 ?1") is a real dirty count.
function isDirty(git) {
  return git && git !== "clean" && git !== "clean *";
}

// Inline-rename label. `kind` is "pane" or "track" (pane renders plain, track
// renders bold; both rename when `renameable`).
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

// Adaptive pane row. Default state is a COMPACT two-line row (stripe + name +
// one key metric, plus the always-visible narrator summary). A pane LIFTS into
// an expanded card — gaining a model badge and a PR/Linear/git/ctx/time footer
// — only when it's selected or needs input. State rides the left stripe at both
// sizes, so collapsing never loses what a pane is or how it's doing.
export function PaneRow({
  w, chip, selectedKey, onSelect, onClose, onRename, dim, dragProps, dropPos,
  pinned, onTogglePin, awaitingSince,
}) {
  const k = `pane:${w.pid}`;
  const sel = k === selectedKey;
  const expanded = sel || w.state === "needs-input";
  const stateCls = w.is_claude ? (w.state || "idle") : "shell";
  const dimCls = dim ? "" : " rail-dim";
  const drop = dropPos ? " drop-target" : "";
  const label = w.name || (w.is_claude ? "claude" : "shell");
  const statusText = w.status_rail || w.status_line;
  const statusStale =
    w.status_at && Math.floor(Date.now() / 1000) - w.status_at > STATUS_STALE_S;
  const model = shortModel(w.model);
  const prHref = w.pr ? prUrl(w.repo_slug, w.pr) : null;
  const mh = memHint(w.mem);  // cycle-hint: server sends w.mem only past thresholds
  // An unanswered question, aged. The tier drives colour so a pane parked for
  // an hour stops looking like one that just asked.
  const waitTierCls = awaitingSince
    ? waitTier(awaitingSince, Math.floor(Date.now() / 1000)) : null;

  // PR + Linear as clickable chips, shared between the compact line-2 strip and
  // the expanded footer (the link just relocates as the card grows). stop-prop
  // so clicking the chip opens the link without also selecting the pane (#4).
  // CI glyph is moot on a merged/closed PR — suppress it so a stale ✓/✗ can't
  // ride a landed PR.
  const prMeta = prStateMeta(w.pr_state);
  const prTitle = `PR #${w.pr}${prMeta.suffix}`;
  const prInner = <>#{w.pr}{w.ci && !prMeta.cls ? <span class={`pane-ci pane-ci-${ciClass(w.ci)}`}> {w.ci}</span> : null}</>;
  const prChip = w.pr && (
    prHref
      ? <a class={`pane-pill pane-pill-pr ${prMeta.cls}`} href={prHref} target="_blank" rel="noopener" onClick={(e) => e.stopPropagation()} title={prTitle}>{prInner}</a>
      : <span class={`pane-pill pane-pill-pr ${prMeta.cls}`} title={prTitle}>{prInner}</span>
  );
  const linChip = w.linked_linear && (
    <a class="pane-pill pane-pill-linear" href={`https://linear.app/issue/${w.linked_linear}`} target="_blank" rel="noopener" onClick={(e) => e.stopPropagation()} title={`Linear ${w.linked_linear}${w.linked_linear_status ? ` [${w.linked_linear_status}]` : ""}`}>{w.linked_linear}</a>
  );

  return (
    <div
      class={`rail-row child-row pane-row pane-state-${stateCls}${expanded ? " pane-expanded" : ""}${sel ? " selected" : ""}${dimCls}${drop}`}
      data-drop-pos={dropPos || undefined}
      draggable
      onClick={() => onSelect(k)}
      {...dragProps}
    >
      <span class="pane-stripe" aria-hidden="true"></span>
      <div class="pane-row-main">
        {w.is_claude
          ? <span class="rail-icon icon-claude">✻</span>
          : <span class="rail-icon icon-shell">$</span>}
        <RailLabel label={label} kind="pane" renameable onCommit={onRename} />
        {/* Line 1 carries only the name + one quiet metric: model when
            expanded, context% when compact. PR/Linear ride line 2 (compact) or
            the footer (expanded) so they never crowd the name. */}
        {expanded
          ? (model && <span class="pane-model" title={w.model}>{model}</span>)
          : (w.context_pct != null && (
              <span class={`pane-mini-ctx${ctxClass(w.context_pct)}`}>{w.context_pct}%</span>
            ))}
        {w.burn_hot && (
          <span
            class="rail-burn"
            title={`eating the session quota — ~${w.burn_wtpm || "?"} weighted tok/min over the last 30m`}
          >🔥</span>
        )}
        {mh && <span class={`rail-mem ${mh.cls}`} title={mh.title}>↻{mh.label}</span>}
        {waitTierCls && (
          <span
            class={`rail-await wait-${waitTierCls}`}
            title={`asked you something ${relTime(awaitingSince)} ago and has had no reply since`}
          >⚠{relTime(awaitingSince)}</span>
        )}
        {/* At-rest pinned flag — the hover actions take no width, so this keeps
            the "this tab is pinned" signal visible without one. Hidden on hover
            (the real toggle is in the action overlay). */}
        {pinned && <span class="pane-pin-flag" title="pinned">★</span>}
      </div>
      {(chip || statusText || (!expanded && (prChip || linChip))) && (
        <div class={`rail-meta${statusStale ? " stale" : ""}`}>
          {chip && <span class="rail-chip" title={w.cwd}>⧉ {chip}</span>}
          {statusText && (
            <span class="rail-status" title={w.status_line}>{statusText}</span>
          )}
          {!expanded && (prChip || linChip) && (
            <span class="rail-chips">{prChip}{linChip}</span>
          )}
        </div>
      )}
      {expanded && (w.pr || w.linked_linear || isDirty(w.git) || w.context_pct != null) && (
        <div class="pane-foot">
          {prChip}
          {linChip}
          {isDirty(w.git) && <span class="pane-pill pane-pill-git" title="git status">{w.git}</span>}
          <span class="pane-foot-spacer"></span>
          {w.context_pct != null && (
            <span class={`pane-pill pane-pill-ctx${ctxClass(w.context_pct)}`} title="context window used">{w.context_pct}%</span>
          )}
          {w.status_at ? <span class="pane-time">{relTime(w.status_at)}</span> : null}
        </div>
      )}
      {/* Hover-only actions, zero footprint at rest (absolute + faded). */}
      <div class="pane-actions">
        <button
          class={`rail-pin${pinned ? " pinned" : ""}`}
          title={pinned ? "unpin" : "pin"}
          onClick={(e) => { e.stopPropagation(); onTogglePin?.(); }}
        >{pinned ? "★" : "☆"}</button>
        <button
          class="rail-close"
          title="kill this tab"
          onClick={(e) => { e.stopPropagation(); onClose(); }}
        >×</button>
      </div>
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
    const meta = prStateMeta(w.pr_state);
    const ciGlyph = w.ci && !meta.cls
      ? <span class={`wt-meta-ci wt-meta-ci-${ciClass(w.ci)}`}>{w.ci}</span>
      : null;
    const inner = <>#{w.pr}{ciGlyph ? <> {ciGlyph}</> : null}</>;
    parts.push(
      href
        ? <a class={`wt-meta-chip wt-meta-pr ${meta.cls}`} href={href} target="_blank" rel="noopener" onClick={(e) => e.stopPropagation()} title={`PR #${w.pr}${meta.suffix}`}>{inner}</a>
        : <span class={`wt-meta-chip wt-meta-pr ${meta.cls}`} title={`PR #${w.pr}${meta.suffix}`}>{inner}</span>
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

// Branch sub-cluster header (the DERIVED mid-tier). A track that spans ≥2
// distinct branches renders one BranchRow per branch; the label IS the branch
// name. There is NO close button — a branch sub-cluster is derived from live
// `w.branch`, not an entity you can kill. Tear down the whole TRACK, or
// move/close individual tabs. `collapsed` hides the children (rendered by the
// caller). No rename (the branch name is the git branch).
export function BranchRow({
  label, collapsed, childCount, rolledUp, dim, onToggle, dragProps, dropPos,
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
      <span class="rail-label"><b>{label}</b></span>
      {collapsed && childCount > 0 ? <span class="rail-count">{childCount}</span> : null}
      <span class={statusDotClass(rolledUp)}></span>
    </div>
  );
}

// Top-level track row (the SOLE grouping primitive — replaces RepoRow). `kind`
// is "loose" | "repo" | "goal" (server-derived, see tracks.track_kind): a goal
// track renders ⧉ and carries an action menu (⋯ toggles a small popover) —
// dissolve [safe default — POSTs /dissolve, tabs survive] and tear down
// [destructive — the caller confirms against the kill list from /teardown].
// The catchalls render 🗀 and carry no menu. Rename POSTs PATCH /api/tracks?track_id=.
export function TrackRow({
  kind, label, collapsed, rolledUp, dim, onToggle, onRename, onDissolve, onTeardown, dragProps, dropPos,
}) {
  const chev = collapsed ? "▸" : "▾";
  const dimCls = dim ? "" : " rail-dim";
  const drop = dropPos ? " drop-target" : "";
  const [menuOpen, setMenuOpen] = useState(false);
  // BOTH catchalls (loose + repo-default) refuse dissolve AND teardown
  // server-side, so neither gets an action menu. A repo-default row used to
  // offer both: Tear down 409'd, Dissolve archived a row that
  // repo_default_track resolves into anyway — a silent no-op.
  const isCatchall = kind === "loose" || kind === "repo";
  // A repo-default's label is basename(repo), which is exactly what a goal
  // track for that repo tends to be named — the icon is the only distinction.
  const icon = kind === "goal" ? "⧉" : "🗀";

  // Close the menu on any outside click / Escape while it's open.
  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e) => { if (!e.target.closest(".track-menu")) setMenuOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setMenuOpen(false); };
    document.addEventListener("click", onDoc, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", onDoc, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  return (
    <div
      class={`rail-row repo-row${dimCls}${drop}`}
      data-drop-pos={dropPos || undefined}
      draggable
      onClick={(e) => { if (e.target.closest("button") || e.target.closest("input")) return; onToggle(); }}
      {...dragProps}
    >
      <span class="rail-chev">{chev}</span>
      <span class="rail-icon icon-effort" title={kind === "goal" ? "track" : "project"}>{icon}</span>
      <RailLabel label={label} kind="track" renameable={kind !== "loose"} onCommit={onRename} />
      <span class="repo-rule" aria-hidden="true"></span>
      <span class={statusDotClass(rolledUp)}></span>
      {!isCatchall && (
        <span class="track-menu">
          <button
            class="rail-track-menu-btn"
            title="track actions"
            onClick={(e) => { e.stopPropagation(); setMenuOpen((o) => !o); }}
          >⋯</button>
          {menuOpen && (
            <div class="track-menu-pop" onClick={(e) => e.stopPropagation()}>
              <button class="track-menu-item" onClick={() => { setMenuOpen(false); onDissolve(); }}>
                Dissolve <span class="track-menu-hint">tabs survive</span>
              </button>
              <button class="track-menu-item track-menu-danger" onClick={() => { setMenuOpen(false); onTeardown(); }}>
                Tear down <span class="track-menu-hint">kills tabs</span>
              </button>
            </div>
          )}
        </span>
      )}
    </div>
  );
}
