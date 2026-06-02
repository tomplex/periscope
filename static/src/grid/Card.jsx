// One pane card. Ported from grid.js:renderCard / renderCardMeta /
// renderCardActivity / renderCardFooter. JSX auto-escapes, so escapeHtml
// drops; the inline `onclick="event.stopPropagation()"` on PR/Linear anchors
// becomes a real onClick calling e.stopPropagation() (convention #4 —
// without it, clicking a PR link opens the modal).
//
// CSS contract preserved verbatim: state-${w.state} card class, card-channel-
// ${kind} tint, card-needs-attention pulse, card-pr-fail bundle, dot classes,
// and every card-* / channel-dot / session-* class name. No renames.
//
// Must-not-drop:
//  - status-label priority: needs-input beats spinner verb beats raw state
//    (convention #6).
//  - unread-alert override + card-channel-${kind} tint.
//  - needs-attention pulse only for need_human.
//  - promote gating: project_pinned_dir === "__main__" AND aff.kind && != no-repo.
//  - CI ✗ bundled into card-pr-fail.
//  - footer model parenthetical strip + relTime(focused_at).
import { useRef, useEffect } from "preact/hooks";
import { signal } from "@preact/signals";
import { relTime, prUrl, targetQuery, apiCall } from "../util.js";
import * as prefs from "../prefs.js";
import { editingTarget } from "../store.js";
import { poll, openModal } from "./poll.js";
import { confirmDialog } from "../overlays/Dialog.jsx";
import { CARD_MIME } from "./dnd.js";

function CiSpan({ ci }) {
  if (!ci) return null;
  const cls = ci === "✓" ? "card-ci-ok" : ci === "✗" ? "card-ci-bad" : "card-ci-pending";
  return <span class={cls}>{ci}</span>;
}

// `git` from server is "clean" or "+N -M [*]". Split for separate styling.
function GitMeta({ git }) {
  if (!git) return null;
  if (git === "clean") return <span class="card-clean">clean</span>;
  return <span class="card-dirty">{git}</span>;
}

// Meta row: branch · clean/dirty · #PR ci · linear · worktree-chip · lgtm.
// Parts are interleaved with the `·` card-dot separator — once between
// adjacent parts only, no leading/trailing (joinWithDots semantics).
function CardMeta({ w, aff, onLgtm }) {
  const parts = [];

  if (w.branch) parts.push(<span class="card-branch">{w.branch}</span>);
  if (w.git) parts.push(<GitMeta git={w.git} />);

  if (w.pr) {
    const linkedTitle = w.pr_linked ? " (linked by claude)" : "";
    const linkedClass = w.pr_linked ? " card-pr-linked" : "";
    const prHref = prUrl(w.repo_slug, w.pr);
    const prLink = prHref ? (
      <a
        class={`card-pr${linkedClass}`}
        href={prHref}
        target="_blank"
        rel="noopener"
        onClick={(e) => e.stopPropagation()}
        title={`PR #${w.pr}${linkedTitle}`}
      >
        #{w.pr}
      </a>
    ) : (
      <span class={`card-pr${linkedClass}`} title={`PR #${w.pr}${linkedTitle}`}>
        #{w.pr}
      </span>
    );
    if (w.ci === "✗") {
      // Bundle #PR + ✗ into a single red badge so the failure mode is
      // immediately legible without claiming the card's state accent.
      parts.push(
        <span class="card-pr-fail">
          {prLink}
          <span class="card-ci-bad">✗</span>
        </span>
      );
    } else {
      parts.push(
        <>
          {prLink}
          {w.ci ? <> <CiSpan ci={w.ci} /></> : null}
        </>
      );
    }
  }

  if (w.linked_linear) {
    const lid = w.linked_linear;
    const ltitle = w.linked_linear_title ? `: ${w.linked_linear_title}` : "";
    const lstatus = w.linked_linear_status ? ` [${w.linked_linear_status}]` : "";
    parts.push(
      <a
        class="card-linear"
        href={`https://linear.app/issue/${lid}`}
        target="_blank"
        rel="noopener"
        onClick={(e) => e.stopPropagation()}
        title={`Linear ${lid}${ltitle}${lstatus} (linked by claude)`}
      >
        {lid}
      </a>
    );
  }

  if (aff.kind === "sibling") {
    parts.push(
      <span
        class="card-worktree-chip card-worktree-chip-sibling"
        title="this tab is in a sibling worktree of the project's repo"
      >
        ↪ {aff.label || ""}
      </span>
    );
  } else if (aff.kind === "off-repo") {
    parts.push(
      <span
        class="card-worktree-chip card-worktree-chip-off-repo"
        title="this tab's cwd is outside the project's repo"
      >
        ⚠ {aff.label || ""}
      </span>
    );
  }

  if (w.lgtm) {
    const total = (w.lgtm.claude_comments || 0) + (w.lgtm.user_comments || 0);
    const tip = total > 0
      ? `LGTM review · ${total} comment${total === 1 ? "" : "s"}`
      : "LGTM review (no comments)";
    parts.push(
      <button
        type="button"
        class="card-lgtm"
        data-lgtm-badge
        data-target={w.target}
        title={tip}
        onClick={(e) => {
          e.stopPropagation();
          onLgtm();
        }}
      >
        👁 review{total > 0 ? <> <span class="card-lgtm-n">{total}</span></> : null}
      </button>
    );
  }

  if (!parts.length) return null;
  return (
    <div class="card-meta">
      {parts.map((p, i) => (
        <>
          {i > 0 ? <> <span class="card-dot">·</span> </> : null}
          {p}
        </>
      ))}
    </div>
  );
}

// Activity row. Priority: unread Claude alert > pending_input > recap >
// last_line. is-shell when it's a bare shell pane with nothing claude-shaped.
function CardActivity({ w, unreadAlert, channelKind }) {
  if (unreadAlert) {
    return (
      <div class={`card-activity is-channel is-channel-${channelKind}`}>
        <span class="card-channel-prefix">claude</span>
        {unreadAlert.message}
      </div>
    );
  }
  if (w.pending_input) {
    return (
      <div class="card-activity is-pending">
        <span class="prompt">›</span>
        {w.pending_input}
      </div>
    );
  }
  if (w.recap) {
    return <div class="card-activity is-output">{w.recap}</div>;
  }
  if (w.last_line) {
    const cls = w.is_claude ? "is-output" : "is-shell";
    return <div class={`card-activity ${cls}`}>{w.last_line}</div>;
  }
  return null;
}

// Footer: progress bar + ctx% + model + viewed-age. Progress bar only when a
// context % exists; model strips the trailing parenthetical (e.g. "(1M
// context)").
function CardFooter({ w }) {
  const parts = [];
  if (w.context_pct != null) {
    parts.push(
      <div class="card-progress">
        <i style={`width:${w.context_pct}%`} />
      </div>
    );
    parts.push(<span class="card-pct">{w.context_pct}%</span>);
  }
  if (w.model) {
    parts.push(<span class="card-model">{w.model.replace(/\s*\(.*\)/, "")}</span>);
  }
  const recent = relTime(w.focused_at);
  if (recent) parts.push(<span class="card-viewed">viewed {recent}</span>);
  if (!parts.length) return null;
  return (
    <div class="card-foot">
      {parts.map((p, i) => (
        <>
          {i > 0 ? " " : null}
          {p}
        </>
      ))}
    </div>
  );
}

export function Card({ w }) {
  const titleRef = useRef(null);
  const clickTimer = useRef(null);
  // Per-card "is the title being renamed" flag, held as a signal in a ref so
  // the click closures read the live value (a captured useState const would
  // be stale). Toggling it re-renders the card.
  const renamingRef = useRef(null);
  if (!renamingRef.current) renamingRef.current = signal(false);
  const renaming = renamingRef.current;

  const stateClass = `state-${w.state}`;
  const ciBadCls = w.ci === "✗" ? " ci-bad" : "";
  const apiErrCls = w.api_error ? " api-error" : "";
  const kind = w.is_claude ? "claude" : "shell";

  // Claude alert override: while unread, the most recent alert takes over the
  // activity line and tints the card background.
  const hasUnread = (w.channel_unread || 0) > 0 && (w.channel_alerts || []).length > 0;
  const unreadAlert = hasUnread ? w.channel_alerts[w.channel_alerts.length - 1] : null;
  const channelKind = unreadAlert ? unreadAlert.kind || "info" : null;
  const channelClass = channelKind ? ` card-channel card-channel-${channelKind}` : "";

  // Needs-attention pulse: only for need_human with unread replies.
  const needsAttention = channelKind === "need_human";
  const cardClass = `card${needsAttention ? " card-needs-attention" : ""}${channelClass}`;

  const aff = w.worktree_affiliation || { kind: "no-repo" };

  // Status label. needs-input wins over the spinner verb (convention #6 —
  // a stale "envisioning…" in scrollback shouldn't drown out the prompt).
  const statusText =
    w.state === "needs-input"
      ? "needs input"
      : w.spinner
        ? `${w.spinner.toLowerCase()}…`
        : w.state;

  // Promote-to-project: only on __main__ tabs whose cwd is inside a git repo.
  const canPromote =
    w.project_pinned_dir === "__main__" && aff.kind && aff.kind !== "no-repo";

  // --- card / title click + dblclick-defer + inline rename ---------------
  function openOnce() {
    openModal(w.target);
  }

  function onCardClick(e) {
    // PR/Linear anchors + the lgtm badge stop their own propagation; this is
    // the card-body fallthrough. A click on the title defers so a dblclick
    // can win and start a rename instead.
    if (renaming.value) return;
    const onName = titleRef.current && titleRef.current.contains(e.target);
    if (!onName) {
      openOnce();
      return;
    }
    if (clickTimer.current) return; // a defer is already pending
    clickTimer.current = setTimeout(() => {
      clickTimer.current = null;
      openOnce();
    }, 220);
  }

  function onTitleDblClick(e) {
    e.stopPropagation();
    if (clickTimer.current) {
      clearTimeout(clickTimer.current);
      clickTimer.current = null;
    }
    startRename();
  }

  function startRename() {
    if (editingTarget.value) return;
    editingTarget.value = w.target;
    renaming.value = true;
  }

  function commitRename(save, value) {
    // one-shot: Enter commits synchronously and unmounts the input, then the
    // browser fires blur on the detached node — guard so the second call
    // no-ops (matches the vanilla `done` flag).
    if (!renaming.value) return;
    renaming.value = false;
    editingTarget.value = null;
    const newName = (value || "").trim();
    if (save && newName && newName !== w.name) {
      fetch(`/api/rename?${targetQuery(w.target)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      }).catch(() => {
        /* poll() resyncs from tmux either way */
      });
    }
    poll();
  }

  async function onLgtm() {
    openModal(w.target, { tab: "review" });
  }

  async function onKill(e) {
    e.stopPropagation();
    if (
      !(await confirmDialog(`Kill window '${w.name}' (${w.target})?`, {
        okLabel: "Kill",
        danger: true,
      }))
    )
      return;
    await apiCall("kill window", `/api/window?${targetQuery(w.target)}`, {
      method: "DELETE",
    });
    poll();
  }

  async function onPromote(e) {
    e.stopPropagation();
    const btn = e.currentTarget;
    btn.disabled = true;
    await apiCall("promote", "/api/projects/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: w.session, index: w.index }),
    });
    btn.disabled = false;
  }

  return (
    <article
      class={`${cardClass} ${stateClass}${ciBadCls}${apiErrCls}`}
      data-target={w.target}
      data-kind={kind}
      draggable
      onDragStart={(e) => {
        // Card move: carry the source target on CARD_MIME ONLY (omit
        // text/plain) so the session-reorder branch ignores it (coupling #6).
        // Identity rides the payload — no DOM-sibling walks.
        e.stopPropagation();
        e.dataTransfer.setData(CARD_MIME, w.target);
        e.dataTransfer.effectAllowed = "move";
        e.currentTarget.classList.add("dragging");
      }}
      onClick={onCardClick}
    >
      <header class="card-head">
        {renaming.value ? (
          <RenameInput
            initial={w.name}
            onCommit={(save, value) => commitRename(save, value)}
          />
        ) : (
          <span
            class="card-title"
            ref={titleRef}
            onDblClick={onTitleDblClick}
          >
            {w.name}
          </span>
        )}
        {w.channel_attached && (
          <span class="channel-dot" title="channel attached"></span>
        )}
        {w.api_error && (
          <span
            class="card-api-error"
            title="last tool result was an API error — pane is waiting for a nudge (e.g. 'keep going')"
          >
            ⚠ API error
          </span>
        )}
        <span class="card-status">{statusText}</span>
        {prefs.hasAnnotation(w.pid) && (
          <span class="card-anno" title="has notes">📝</span>
        )}
        {canPromote && (
          <button
            class="card-promote"
            title="promote this tab to its own project"
            onClick={onPromote}
          >
            ↗ promote
          </button>
        )}
        <button class="card-kill" title="kill this window" onClick={onKill}>
          ✕
        </button>
      </header>
      <CardMeta w={w} aff={aff} onLgtm={onLgtm} />
      <CardActivity w={w} unreadAlert={unreadAlert} channelKind={channelKind} />
      <CardFooter w={w} />
    </article>
  );
}

// Inline rename <input>. Mirrors the vanilla rename-input: focus+select on
// mount, Enter commits, Escape cancels, blur commits. keydown stops
// propagation so the chrome Tab/Escape keybindings don't fire while typing.
function RenameInput({ initial, onCommit }) {
  const ref = useRef(null);
  useEffect(() => {
    const inp = ref.current;
    if (inp) {
      inp.focus();
      inp.select();
    }
  }, []);
  return (
    <input
      ref={ref}
      type="text"
      class="rename-input"
      value={initial}
      onClick={(e) => e.stopPropagation()}
      onDblClick={(e) => e.stopPropagation()}
      onBlur={(e) => onCommit(true, e.currentTarget.value)}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Enter") {
          e.preventDefault();
          onCommit(true, e.currentTarget.value);
        } else if (e.key === "Escape") {
          e.preventDefault();
          onCommit(false, e.currentTarget.value);
        }
      }}
    />
  );
}
