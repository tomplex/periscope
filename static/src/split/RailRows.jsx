// Row components for the split-view rail, ported from rail.js's row-builder
// functions (paneRow / worktreeRow / repoRow). One component per row kind
// (rail-row, child-row, wt-row, repo-row, rail-chev, rail-icon, rail-label,
// rail-count, rail-close, rail-dim, drop-target).
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
import { accountLabel, moveAccountTarget, multiAccount } from "../accounts.js";
import { blipReason } from "../notify/copy.js";
import { profileLabel } from "../profiles.js";
import { AGENT_META, memHint, paneLabel, prStateMeta, prUrl, relTime } from "../util.js";
import { waitTier } from "./attention.js";
import { ctxClass } from "./ctxBand.js";
import { linearTicket } from "./linearTicket.js";
import { canOpenInEditor, openInEditorTitle } from "./openInEditor.js";

// Narrator status dims after 15 min: the work moved on (or the pane went
// quiet) and the one-liner no longer reflects "now".
const STATUS_STALE_S = 900;

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

function fmtCtxTokens(n) {
  return `${Math.round(n / 1000)}k ctx`;
}

// The chip shows the percentage when Claude's status line parsed, and the
// context's size otherwise — so a pane whose status line is unreadable still
// paints.
export function ctxChipText(w) {
  if (w.context_pct != null) return `${w.context_pct}%`;
  if (w.ctx_tokens != null) return fmtCtxTokens(w.ctx_tokens);
  return null;
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

// When a row's age counts from: how long it has waited on you, or since its
// last turn finished. A working pane is "now"; a shell has none.
function rowAge(w, blip, awaitingSince) {
  if (blip === "shell") return null;
  if (blip === "working") return { text: "now", cls: "", title: "working now" };
  if (blip === "asks") {
    const since = Math.min(...[awaitingSince, w.needs_since].filter((t) => t > 0));
    if (!Number.isFinite(since)) return null;
    return {
      text: relTime(since), cls: ` wait-${waitTier(since, Math.floor(Date.now() / 1000))}`,
      title: "how long it has waited for you",
    };
  }
  return w.completed_at ? { text: relTime(w.completed_at), cls: "", title: "since its last turn finished" } : null;
}

// What the narrator line leads with when the blip alone doesn't say why.
function blipWhy(w, blip) {
  return blip === "broken" || blip === "done" || blip === "done-old" ? blipReason(w, blip) : null;
}

// Adaptive pane row. Default state is a COMPACT row: name, PR and age on line
// 1, then the narrator summary, with Linear/account/profile chips on the
// bottom line. The selected pane LIFTS into an expanded card — gaining a
// model badge and a branch/PR/Linear/account/ctx footer. `blip`
// (attention.paneBlip) rides the left stripe at both sizes; only an asking
// pane's pulses. Lineage (↳ spawned-by) and git-dirty are deliberately NOT
// here — both live in the detail header. `showBranch` is false inside a
// branch cluster, whose header already names it.
export function PaneRow({
  w, chip, blip, selectedKey, onSelect, onClose, onRename, dim, dragProps, dropPos,
  pinned, onTogglePin, awaitingSince, showBranch,
  onMoveAccount, onToggleNamePin, editor, onOpenInEditor,
}) {
  const k = `pane:${w.pid}`;
  const sel = k === selectedKey;
  const expanded = sel;
  const dimCls = dim ? "" : " rail-dim";
  const drop = dropPos ? " drop-target" : "";
  const label = paneLabel(w);
  const agentMeta = AGENT_META[w.agent];
  const statusText = w.status_rail || w.status_line;
  const statusStale =
    w.status_at && Math.floor(Date.now() / 1000) - w.status_at > STATUS_STALE_S;
  const model = shortModel(w.model);
  const prHref = w.pr ? prUrl(w.repo_slug, w.pr) : null;
  // ↻ only when memory tripped it. Age alone tripped it on most panes and said
  // nothing new; the detail header still shows either.
  const mh = w.mem?.trigger === "mem" ? memHint(w.mem) : null;
  const age = rowAge(w, blip, awaitingSince);
  const why = blipWhy(w, blip);
  // A compact row shows context only once it's in a warning band.
  const ctxCls = ctxClass(w);
  // Capacity pooling: one subscription runs out before the others, and the
  // work stuck on the exhausted one has to keep going. null on shell panes and
  // on an unrecognised config dir (see moveAccountTarget).
  const moveAcct = moveAccountTarget(w);

  // PR + Linear as clickable chips: PR on line 1 (compact) or the footer
  // (expanded), Linear on the bottom line or the footer. stop-prop so
  // clicking the chip opens the link without also selecting the pane (#4).
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
  const lin = linearTicket(w);
  const linChip = lin && (
    <a class="pane-pill pane-pill-linear" href={lin.href} target="_blank" rel="noopener" onClick={(e) => e.stopPropagation()} title={lin.tip}>{lin.id}</a>
  );
  // Account + wrapper-profile chips ride the bottom line (compact meta strip /
  // expanded footer), not the name line — they're orientation, not attention.
  // Only for non-default values: a chip on every pane would say nothing.
  // Hidden with a single registered account — there is nothing to tell apart —
  // EXCEPT "unknown": a CLAUDE_CONFIG_DIR no registered account claims is a
  // pane demonstrably not on the one account, which is worth saying either way.
  const acctChip = w.account && w.account !== "default" &&
    (multiAccount() || w.account === "unknown") && (
    <span
      class="pane-pill pane-pill-acct"
      title={`running on Claude account ${accountLabel(w.account)} — its usage bills that subscription`}
    >@{accountLabel(w.account)}</span>
  );
  const profChip = w.profile && w.profile !== "default" && (
    <span
      class="rail-profile"
      title={`running the ${profileLabel(w.profile)} profile — a different plugin set and system prompt than a normal pane`}
    >{profileLabel(w.profile)}</span>
  );
  const branchRef = showBranch && w.branch && (
    <span class="pane-branch" title={`branch ${w.branch}`}><span class="icon-worktree">⎇</span> {w.branch}</span>
  );

  return (
    <div
      class={`rail-row child-row pane-row pane-blip-${blip}${expanded ? " pane-expanded" : ""}${sel ? " selected" : ""}${dimCls}${drop}`}
      data-drop-pos={dropPos || undefined}
      draggable
      onClick={() => onSelect(k)}
      {...dragProps}
    >
      <span class="pane-stripe" aria-hidden="true"></span>
      <div class="pane-row-main">
        {agentMeta
          ? <span class={`rail-icon ${agentMeta.className}`} title={agentMeta.label}>{agentMeta.glyph}</span>
          : <span class="rail-icon icon-shell">$</span>}
        <RailLabel label={label} kind="pane" renameable onCommit={onRename} />
        {/* At-rest pinned flag — the hover actions take no width, so this keeps
            the "this tab is pinned" signal visible without one. Hidden on hover
            (the real toggle is in the action overlay). */}
        {pinned && <span class="pane-pin-flag" title="pinned">★</span>}
        {expanded && model && <span class="pane-model" title={w.model}>{model}</span>}
        {mh && <span class={`rail-mem ${mh.cls}`} title={mh.title}>↻{mh.label}</span>}
        {!expanded && ctxCls && ctxChipText(w) != null && (
          <span
            class={`pane-mini-ctx${ctxCls}`}
            title={w.ctx_hint || "context window used"}
          >{ctxChipText(w)}</span>
        )}
        {!expanded && prChip}
        {age && (
          <span class={`rail-age${age.cls}`} title={age.title}>{age.text}</span>
        )}
      </div>
      {(chip || statusText || why || (!expanded && (linChip || acctChip || profChip))) && (
        <div class={`rail-meta${statusStale && !why ? " stale" : ""}`}>
          {chip && <span class="rail-chip" title={w.cwd}>⧉ {chip}</span>}
          {(why || statusText) && (
            <span class="rail-status" title={w.status_line}>
              {why && <span class={`rail-why why-${blip}`}>{why}</span>}
              {why && statusText ? " · " : null}
              {statusText}
            </span>
          )}
          {!expanded && (linChip || acctChip || profChip) && (
            <span class="rail-chips">{linChip}{acctChip}{profChip}</span>
          )}
        </div>
      )}
      {expanded && (branchRef || w.pr || lin || ctxChipText(w) != null || acctChip || profChip) && (
        <div class="pane-foot">
          {branchRef}
          {prChip}
          {linChip}
          <span class="pane-foot-spacer"></span>
          {acctChip}
          {profChip}
          {ctxChipText(w) != null && (
            <span
              class={`pane-pill pane-pill-ctx${ctxCls}`}
              title={w.ctx_hint || "context window used"}
            >{ctxChipText(w)}</span>
          )}
        </div>
      )}
      {/* Hover-only actions, zero footprint at rest (absolute + faded). */}
      <div class="pane-actions">
        {/* Re-open this pane's session on another subscription. Labeled with
            the destination, never the current account: "→ B" is unambiguous
            where a bare "move" is not. Hover-only because it's a rare,
            deliberate act — unlike the account chip, which is orientation. */}
        {moveAcct && (
          <button
            class="rail-move-acct"
            title={`re-open this session on Claude account ${moveAcct.label} — spawns a resumed pane there and leaves this one running`}
            onClick={(e) => { e.stopPropagation(); onMoveAccount?.(moveAcct.id); }}
          >→ {moveAcct.label}</button>
        )}
        <button
          class={`rail-pin${pinned ? " pinned" : ""}`}
          title={pinned ? "unpin" : "pin"}
          onClick={(e) => { e.stopPropagation(); onTogglePin?.(); }}
        >{pinned ? "★" : "☆"}</button>
        {/* Distinct from the ★ above: that pins the tab into the PINNED
            section, this freezes its NAME against the narrator's auto-rename.
            Renaming pins on its own, so the useful direction here is the
            release — plus keeping a generated name you happen to like. */}
        <button
          class={`rail-name-pin${w.name_pinned ? " pinned" : ""}`}
          title={w.name_pinned
            ? "name pinned — click to let periscope rename this tab again"
            : "freeze this name — periscope won't rename this tab"}
          onClick={(e) => { e.stopPropagation(); onToggleNamePin?.(); }}
        >{w.name_pinned ? "🔒" : "🔓"}</button>
        {/* Opens the pane's WORKTREE ROOT (the server resolves the git
            toplevel), not its cwd — an editor treats the opened folder as the
            project, so a pane sitting in a subdirectory would get a fragment
            of the tree. Hidden entirely for panes outside a repo. */}
        {canOpenInEditor(w, editor) && (
          <button
            class="rail-editor"
            title={openInEditorTitle(w, editor)}
            onClick={(e) => { e.stopPropagation(); onOpenInEditor?.(); }}
          >↗</button>
        )}
        <button
          class="rail-close"
          title="kill this tab"
          onClick={(e) => { e.stopPropagation(); onClose(); }}
        >×</button>
      </div>
    </div>
  );
}

// Branch sub-cluster header (the DERIVED mid-tier): one per branch that two
// or more of a track's tabs share; the label IS the branch name, in mono like
// anything you'd paste. There is NO close button — a branch sub-cluster is
// derived from live `w.branch`, not an entity you can kill. Tear down the
// whole TRACK, or move/close individual tabs. `collapsed` hides the children
// (rendered by the caller). No rename (the branch name is the git branch),
// and no drag: there is nowhere to drop a branch.
export function BranchRow({ label, collapsed, childCount, dim, onToggle }) {
  const chev = collapsed ? "▸" : "▾";
  const dimCls = dim ? "" : " rail-dim";
  return (
    <div class={`rail-row wt-row${dimCls}`} onClick={onToggle}>
      <span class="rail-chev">{chev}</span>
      <span class="rail-icon icon-worktree">⎇</span>
      <span class="rail-label">{label}</span>
      {collapsed && childCount > 0 ? <span class="rail-count">{childCount}</span> : null}
    </div>
  );
}

export function ReviewRow({ row, selectedKey, onSelect }) {
  const k = `review:${row.repo}/${row.slug}`;
  const sel = k === selectedKey ? " selected" : "";
  const stale = row.walkthrough?.stale_by > 0;
  return (
    <div class={`rail-row child-row rail-review${sel}`} onClick={() => onSelect(k)}>
      <span class="rail-icon icon-review">◉</span>
      <span class="rail-label">review · {row.walkthrough.stops} stop{row.walkthrough.stops === 1 ? "" : "s"}{stale ? " · stale" : ""}</span>
      {row.unresolved ? <span class="rail-review-badge">{row.unresolved}</span> : null}
    </div>
  );
}

// Top-level track row (the SOLE grouping primitive — replaces RepoRow). `kind`
// is "loose" | "repo" | "goal" (server-derived, see tracks.track_kind): a goal
// track renders ⧉ and carries an action menu (⋯ toggles a small popover) —
// dissolve [safe default — POSTs /dissolve, tabs survive] and tear down
// [destructive — the caller confirms against the kill list from /teardown].
// The catchalls render 🗀 and carry no menu. Rename POSTs PATCH /api/tracks?track_id=.
// + opens the launcher for a new tab in this track. The sonar strip carries
// the track's state, so the row has no roll-up dot.
export function TrackRow({
  kind, label, collapsed, dim, onToggle, onRename, onNewTab, onDissolve, onTeardown, dragProps, dropPos,
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
      <button
        class="rail-track-add"
        title={`new tab in ${label}`}
        aria-label={`new tab in ${label}`}
        onClick={(e) => { e.stopPropagation(); onNewTab(); }}
      >+</button>
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
