// Grid rendering, /api/state polling, drag-reorder, and one-time event
// delegation on the grid root. Handlers walk up from event.target via
// closest() to find the relevant card/header/button and resolve
// target/session from data-attributes — so a render() innerHTML rebuild
// doesn't invalidate any listeners.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml, targetQuery, apiCall, relTime } from './util.js';
import { openModal } from './modal.js';

const POLL_MS = 3000;

const grid = document.getElementById("grid");
const counts = document.getElementById("counts");
const lastUpdate = document.getElementById("last-update");
const usageEl = document.getElementById("usage");
const toggleAllBtn = document.getElementById("toggle-all");
const bannerEl = document.getElementById("connection-banner");

// Consecutive failed /api/state polls. Banner shows at ≥2 (≈6s of
// detection) to avoid flicker on a single transient hiccup.
let consecutivePollFails = 0;

const nameClickTimers = new Map();  // target -> setTimeout handle (single-click defer for dblclick rename)

function passesFilter(w) {
  if (state.currentFilter === "all") return true;
  if (state.currentFilter === "needs-input") return w.state === "needs-input";
  if (state.currentFilter === "working") return w.state === "working";
  if (state.currentFilter === "done") return w.state === "done";
  if (state.currentFilter === "idle") return w.state === "idle";
  if (state.currentFilter === "claude") return w.is_claude;
  if (state.currentFilter === "shell") return w.state === "shell";
  if (state.currentFilter === "ci-bad") return w.ci === "✗";
  return true;
}

function ciSpan(ci) {
  if (!ci) return "";
  const cls = ci === "✓" ? "card-ci-ok" : ci === "✗" ? "card-ci-bad" : "card-ci-pending";
  return `<span class="${cls}">${ci}</span>`;
}

// `git` from server is "clean" or "+N -M [*]". Split into clean/dirty +
// formatted suffix for separate styling. The trailing " *" (unpushed) is
// preserved as part of the dirty text.
function gitMetaSpan(git) {
  if (!git) return "";
  if (git === "clean") return `<span class="card-clean">clean</span>`;
  return `<span class="card-dirty">${escapeHtml(git)}</span>`;
}

// Build a tmux_session → project row lookup from the most recent /api/state
// response. Lives on `state.projectsByTmux` so renderSession can consume it
// without re-walking the array per group.
function indexProjects(projects) {
  const idx = {};
  for (const p of projects || []) {
    if (p.tmux_session) idx[p.tmux_session] = p;
  }
  return idx;
}

function renderCard(w) {
  const stateClass = `state-${w.state}`;
  const ciBadCls = w.ci === "✗" ? " ci-bad" : "";
  const apiErrCls = w.api_error ? " api-error" : "";
  const kind = w.is_claude ? "claude" : "shell";

  // Claude reply override: while there are unread replies, the most recent
  // one takes over the card's activity line and tints the card background.
  // Opening the modal clears unread (T13), and the card drops back to its
  // normal pending/recap/last_line activity on the next poll.
  const hasUnread = (w.channel_unread || 0) > 0 && (w.channel_replies || []).length > 0;
  const unreadReply = hasUnread ? w.channel_replies[w.channel_replies.length - 1] : null;
  const channelKind = unreadReply ? (unreadReply.kind || "info") : null;
  const channelClass = channelKind ? ` card-channel card-channel-${channelKind}` : "";

  // Needs-attention pulse: only when Claude has actively flagged need_human
  // *and* there are unread replies. Opening the modal clears unread (T13),
  // which auto-dismisses the pulse on the next render.
  const needsAttention = channelKind === "need_human";
  const cardClass = `card${needsAttention ? " card-needs-attention" : ""}${channelClass}`;
  const anno = prefs.hasAnnotation(w.pid)
    ? `<span class="card-anno" title="has notes">📝</span>`
    : "";

  // Meta row: branch · clean/dirty · #PR ci.  PR/CI stays on the card so
  // a glance still surfaces CI breakage; matches the existing scan pattern.
  const metaParts = [];
  if (w.branch) metaParts.push(`<span class="card-branch">${escapeHtml(w.branch)}</span>`);
  if (w.git) {
    if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
    metaParts.push(gitMetaSpan(w.git));
  }
  if (w.pr) {
    if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
    // pr_linked: Claude explicitly linked this PR via the link_pr MCP tool
    // (overrides periscope's auto-detection from the title bar).
    const linkedTitle = w.pr_linked ? " (linked by claude)" : "";
    const linkedClass = w.pr_linked ? " card-pr-linked" : "";
    const prLink = `<a class="card-pr${linkedClass}" href="https://github.com/faradayio/fdy/pull/${w.pr}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="PR #${w.pr}${linkedTitle}">#${w.pr}</a>`;
    if (w.ci === "✗") {
      // Bundle #PR + ✗ into a single red badge so the failure mode is
      // immediately legible without claiming the card's state accent.
      metaParts.push(`<span class="card-pr-fail">${prLink}<span class="card-ci-bad">✗</span></span>`);
    } else {
      metaParts.push(prLink);
      if (w.ci) metaParts.push(ciSpan(w.ci));
    }
  }
  if (w.linked_linear) {
    if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
    // Linear linking is always explicit — periscope doesn't auto-detect, so
    // every linked_linear chip is something Claude (or a future UI affordance)
    // declared. URL points at app.linear.app/issue/<id> which redirects to
    // the workspace's canonical URL.
    metaParts.push(
      `<a class="card-linear" href="https://linear.app/issue/${escapeHtml(w.linked_linear)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Linear ticket ${escapeHtml(w.linked_linear)} (linked by claude)">${escapeHtml(w.linked_linear)}</a>`
    );
  }
  const aff = w.worktree_affiliation || { kind: "no-repo" };
  if (aff.kind === "sibling") {
    if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
    metaParts.push(
      `<span class="card-worktree-chip card-worktree-chip-sibling" title="this tab is in a sibling worktree of the project's repo">↪ ${escapeHtml(aff.label || "")}</span>`
    );
  } else if (aff.kind === "off-repo") {
    if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
    metaParts.push(
      `<span class="card-worktree-chip card-worktree-chip-off-repo" title="this tab's cwd is outside the project's repo">⚠ ${escapeHtml(aff.label || "")}</span>`
    );
  }
  if (w.lgtm) {
    if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
    // LGTM session is registered for this pane's repo. Click opens the modal
    // pre-switched to the Review tab; the existing card click handler keys
    // off `data-lgtm-badge`.
    const total = (w.lgtm.claude_comments || 0) + (w.lgtm.user_comments || 0);
    const countLabel = total > 0 ? ` <span class="card-lgtm-n">${total}</span>` : "";
    const tip = total > 0 ? `LGTM review · ${total} comment${total === 1 ? "" : "s"}` : "LGTM review (no comments)";
    metaParts.push(
      `<button type="button" class="card-lgtm" data-lgtm-badge data-target="${w.target}" title="${tip}">👁 review${countLabel}</button>`
    );
  }
  const metaRow = metaParts.length
    ? `<div class="card-meta">${metaParts.join(" ")}</div>`
    : "";

  // Activity row. Priority: unread Claude reply (highest — Claude is actively
  // saying something the user hasn't seen) > pending_input (claude is going
  // to act on whatever you type next) > recap > last_line. is-shell when
  // it's a bare shell pane with nothing claude-shaped to show.
  let activity = "";
  if (unreadReply) {
    activity = `<div class="card-activity is-channel is-channel-${channelKind}"><span class="card-channel-prefix">claude</span>${escapeHtml(unreadReply.message)}</div>`;
  } else if (w.pending_input) {
    activity = `<div class="card-activity is-pending"><span class="prompt">›</span>${escapeHtml(w.pending_input)}</div>`;
  } else if (w.recap) {
    activity = `<div class="card-activity is-output">${escapeHtml(w.recap)}</div>`;
  } else if (w.last_line) {
    const cls = w.is_claude ? "is-output" : "is-shell";
    activity = `<div class="card-activity ${cls}">${escapeHtml(w.last_line)}</div>`;
  }

  // Status label. needs-input wins over the spinner verb (a stale "envisioning…"
  // in scrollback shouldn't drown out the blocking prompt).
  const statusText = w.state === "needs-input"
    ? "needs input"
    : w.spinner
      ? `${w.spinner.toLowerCase()}…`
      : w.state;
  const statusLabel = `<span class="card-status">${escapeHtml(statusText)}</span>`;

  // Channel attached indicator. Unread replies are surfaced via the card
  // background tint + activity-row override (above), so no numeric badge
  // is needed here.
  const channelDot = w.channel_attached
    ? `<span class="channel-dot" title="channel attached"></span>`
    : "";

  // API-error chip: the pane's most recent tool result was an `⎿ API Error:`
  // — typically a rate limit. Claude doesn't auto-retry; the turn aborts and
  // the pane silently waits for the user to nudge it (`keep going`). Loud
  // so this blocked-waiting state is visible from across the dashboard.
  const apiErrChip = w.api_error
    ? `<span class="card-api-error" title="last tool result was an API error — pane is waiting for a nudge (e.g. 'keep going')">⚠ API error</span>`
    : "";

  // Footer: progress bar + ctx% + model + viewed-age. Progress bar only when
  // we have a context % to fill; otherwise the row reads "model · viewed Xm".
  const footParts = [];
  if (w.context_pct != null) {
    footParts.push(`<div class="card-progress"><i style="width:${w.context_pct}%"></i></div>`);
    footParts.push(`<span class="card-pct">${w.context_pct}%</span>`);
  }
  if (w.model) {
    footParts.push(`<span class="card-model">${escapeHtml(w.model.replace(/\s*\(.*\)/, ""))}</span>`);
  }
  const recent = relTime(w.focused_at);
  if (recent) footParts.push(`<span class="card-viewed">viewed ${recent}</span>`);
  const footRow = footParts.length
    ? `<div class="card-foot">${footParts.join(" ")}</div>`
    : "";

  // Promote-to-project: only on tabs in the __main__ project, only
  // when the cwd is inside a git repo (worktree_affiliation tells us).
  // `aff` is already in scope from the worktree-chip block above.
  const isMainTab = w.project_pinned_dir === "__main__";
  const canPromote = isMainTab && aff.kind && aff.kind !== "no-repo";
  const promoteBtn = canPromote
    ? `<button class="card-promote" data-session="${escapeHtml(w.session)}" data-index="${w.index}" title="promote this tab to its own project">↗ promote</button>`
    : "";

  return `
    <article class="${cardClass} ${stateClass}${ciBadCls}${apiErrCls}" data-target="${w.target}" data-kind="${kind}" draggable="true">
      <header class="card-head">
        <span class="card-title">${escapeHtml(w.name)}</span>
        ${channelDot}
        ${apiErrChip}
        ${statusLabel}
        ${anno}
        ${promoteBtn}
        <button class="card-kill" data-target="${w.target}" data-name="${escapeHtml(w.name)}" title="kill this window">✕</button>
      </header>
      ${metaRow}
      ${activity}
      ${footRow}
    </article>
  `;
}

function renderNewTile(session) {
  // Read commands from prefs. First entry is the primary (top, larger
  // hit area). Each command renders as a pair: a main button (plain
  // tab) + a ⌥ button (worktree variant — opens an inline branch-name
  // input).
  const s = escapeHtml(session);
  const commands = prefs.getCommands();
  if (!commands.length) {
    return `<div class="card card-new" data-session="${s}"></div>`;
  }

  // Whether this session has a non-main project (worktree-eligible).
  // Worktree tab requires a project with a repo; for unmanaged sessions
  // the ⌥ button is hidden.
  const project = state.projectsByTmux?.[session];
  const worktreeEligible = project
    && project.pinned_dir !== "__main__"
    && !project.archived_at
    && (project.repo || null);  // require resolved repo

  const [primary, ...rest] = commands;
  const pair = (cmd, cls) => {
    const label = escapeHtml(cmd.label);
    const execAttr = escapeHtml(cmd.exec || "");
    const mainBtn = `<button class="new-window${cls}" data-session="${s}" data-exec="${execAttr}">+ ${label}</button>`;
    const variantBtn = worktreeEligible
      ? `<button class="new-window-worktree${cls}" data-session="${s}" data-exec="${execAttr}" data-label="${label}" title="new worktree tab + ${label}">⌥</button>`
      : "";
    return `<span class="new-window-pair">${mainBtn}${variantBtn}</span>`;
  };
  const stack = rest.length
    ? `<div class="new-window-stack">${rest.map((c) => pair(c, "")).join("")}</div>`
    : "";
  return `
    <div class="card card-new" data-session="${s}">
      ${pair(primary, " is-primary")}
      ${stack}
    </div>
  `;
}

function orderedSessions(allSessions, bySession) {
  // User-pinned (drag-reordered) sessions appear in their saved order.
  // Sessions NOT in the saved order are freshly created — surface them at
  // the TOP, newest first, so a just-made project/session is immediately
  // visible instead of buried alphabetically at the bottom. A drag-reorder
  // saves the full order, so once the user places a session it leaves the
  // "fresh" group and stays put.
  const saved = prefs.getSessionOrder();
  const ordered = saved.filter((s) => allSessions.includes(s));
  const orderedSet = new Set(ordered);
  const recencyOf = (s) =>
    Math.max(0, ...(bySession.get(s) || []).map((w) => w.acted_at || 0));
  const fresh = allSessions
    .filter((s) => !orderedSet.has(s))
    .sort((a, b) => recencyOf(b) - recencyOf(a));
  return [...fresh, ...ordered];
}

// Channel-alert pill rolled up across a session's panes. Visible in both
// expanded and collapsed states so a collapsed group still signals when one
// of its hidden cards has an unread Claude alert.
const _KIND_RANK = { need_human: 3, done: 2, info: 1 };
const _KIND_ICON = { need_human: "⚠", done: "✓", info: "•" };

function sessionChannelAlert(ws) {
  const alerts = ws
    .filter(w => (w.channel_unread || 0) > 0 && (w.channel_replies || []).length > 0)
    .map(w => ({
      kind: (w.channel_replies[w.channel_replies.length - 1].kind || "info"),
      message: w.channel_replies[w.channel_replies.length - 1].message || "",
    }));
  if (!alerts.length) return { html: "", kind: null };
  const topKind = alerts.reduce(
    (a, b) => (_KIND_RANK[a.kind] || 0) >= (_KIND_RANK[b.kind] || 0) ? a : b
  ).kind;
  const count = alerts.length;
  const icon = _KIND_ICON[topKind] || "•";
  // Preview the top alert's message (truncated) so collapsed sessions reveal
  // *what* needs attention, not just *that* something does.
  const top = alerts.find(a => a.kind === topKind);
  const preview = top ? escapeHtml(top.message.slice(0, 60)) : "";
  const more = count > 1 ? ` <span class="session-channel-more">+${count - 1}</span>` : "";
  return {
    html: `<span class="session-channel-pill session-channel-${topKind}" title="${count} unread Claude alert(s)">${icon} ${preview}${more}</span>`,
    kind: topKind,
  };
}

function sessionPill(ws) {
  const needsInput = ws.filter((w) => w.state === "needs-input").length;
  const done = ws.filter((w) => w.state === "done").length;
  const idle = ws.filter((w) => w.state === "idle").length;
  const working = ws.filter((w) => w.state === "working").length;
  const ciBad = ws.filter((w) => w.ci === "✗").length;
  const parts = [];
  if (needsInput) parts.push(`${needsInput} needs input`);
  if (done) parts.push(`${done} done`);
  if (working) parts.push(`${working} working`);
  if (idle) parts.push(`${idle} idle`);
  if (ciBad) parts.push(`${ciBad} ✗`);
  if (!parts.length) parts.push(`${ws.length}`);
  // Pill color hierarchy: needs-input is the loudest signal (a pane is
  // blocked on me) > ci-bad > done > working > idle. Anything quieter loses.
  let cls = "session-pill";
  if (needsInput) cls += " has-needs-input";
  else if (ciBad) cls += " has-ci-bad";
  else if (done) cls += " has-done";
  else if (working) cls += " has-working";
  else if (idle) cls += " has-idle";
  return `<span class="${cls}">${parts.join(" · ")}</span>`;
}

function renderSession(session, ws, totalWindows) {
  const shown = ws.length;
  const meta = shown === totalWindows
    ? `${totalWindows} windows`
    : `${shown}/${totalWindows} windows`;
  const collapsed = state.collapsedSessions.has(session) ? " collapsed" : "";
  const recent = Math.max(0, ...ws.map((w) => w.focused_at || 0));
  const recentLabel = recent ? relTime(recent) : "";
  const s = escapeHtml(session);
  const project = state.projectsByTmux?.[session] || null;
  const pinnedDirLabel = project && project.pinned_dir && project.pinned_dir !== "__main__"
    ? project.pinned_dir.replace(/^\/Users\/[^/]+/, "~")
    : null;
  const adoptBtn = project
    ? ""
    : `<button class="adopt" data-session="${s}" title="register this tmux session as a project">+ adopt</button>`;
  const alert = sessionChannelAlert(ws);
  const alertClass = alert.kind ? ` session-has-channel session-has-channel-${alert.kind}` : "";
  return `
    <section class="session-group${collapsed}${alertClass}" data-session="${s}">
      <div class="session-header" draggable="true" data-session="${s}">
        <span class="chevron">▾</span>
        <h2 class="session-name">${escapeHtml(project?.name || session)}</h2>
        ${pinnedDirLabel ? `<span class="session-pinned-dir">${escapeHtml(pinnedDirLabel)}</span>` : ""}
        <span class="session-meta">${meta}${recentLabel ? ` · ${recentLabel}` : ""}</span>
        ${sessionPill(ws)}
        ${alert.html}
        ${adoptBtn}
        ${project && project.pinned_dir !== "__main__" ? `<button class="project-menu" data-pinned-dir="${escapeHtml(project.pinned_dir)}" title="project actions">⋯</button>` : ""}
        <button class="auto-rename" data-session="${s}" title="ask Claude to auto-rename windows in this session">✨ rename</button>
        <button class="kill-session" data-session="${s}" title="kill this tmux session">✕</button>
      </div>
      <div class="cards">
        ${ws.map(renderCard).join("")}
        ${renderNewTile(session)}
      </div>
    </section>
  `;
}

// The header "collapse/expand all" toggle reflects, and operates on, only the
// sessions currently rendered — filtering out hidden sessions keeps the button
// label honest and avoids surprise mutations to off-screen state.
function updateToggleAll(visibleSessions) {
  if (!toggleAllBtn) return;
  if (visibleSessions.length === 0) {
    toggleAllBtn.hidden = true;
    return;
  }
  toggleAllBtn.hidden = false;
  const allCollapsed = visibleSessions.every((s) => state.collapsedSessions.has(s));
  toggleAllBtn.textContent = allCollapsed ? "▸ expand all" : "▾ collapse all";
}

function handleToggleAll() {
  const visible = [...grid.querySelectorAll(".session-group")].map((g) => g.dataset.session);
  if (visible.length === 0) return;
  const allCollapsed = visible.every((s) => state.collapsedSessions.has(s));
  if (allCollapsed) {
    for (const s of visible) state.collapsedSessions.delete(s);
  } else {
    for (const s of visible) state.collapsedSessions.add(s);
  }
  prefs.setCollapsed(state.collapsedSessions);
  render(state.lastWindows);
}

// Stream view sorts strictly by acted_at desc — most-recently-engaged at
// top, regardless of state. State color/icon still convey urgency; we don't
// also force state-priority into the sort key (doing so kept hours-old
// needs-input rows pinned above tabs you just opened).

function streamIcon(s) {
  if (s === "needs-input") return "!";
  if (s === "working") return `<span class="stream-spin">◐</span>`;
  if (s === "done") return "✓";
  if (s === "idle") return "·";
  return "$";
}

function streamAction(s) {
  if (s === "needs-input") return `<span class="stream-action-respond">respond ↵</span>`;
  if (s === "working") return "watch";
  if (s === "done") return "review";
  if (s === "idle") return "resume";
  return "focus";
}

function renderStreamRow(w) {
  const stateClass = `state-${w.state}`;
  const ciBadCls = w.ci === "✗" ? " ci-bad" : "";
  const apiErrCls = w.api_error ? " api-error" : "";
  const focusedCls = w.target === state.streamFocusedTarget ? " is-focused" : "";
  const needHumanCls = hasUnreadNeedHuman(w) ? " has-need-human" : "";
  const sessionLabel = escapeHtml(w.session);
  const branchPart = w.branch
    ? `${sessionLabel} · ${escapeHtml(w.branch)}`
    : sessionLabel;
  const ctxPart = w.is_claude && w.context_pct != null
    ? ` · ${escapeHtml((w.model || "").replace(/\s*\(.*\)/, ""))} · ${w.context_pct}%`
    : "";

  let msg = "";
  if (w.pending_input) {
    msg = `<span class="stream-prompt">›</span> ${escapeHtml(w.pending_input)}`;
  } else if (w.recap) {
    msg = escapeHtml(w.recap);
  } else if (w.last_line) {
    msg = escapeHtml(w.last_line);
  }

  // acted_at is guaranteed > 0 here (renderStream filtered).
  const when = relTime(w.acted_at) || "now";

  return `
    <div class="stream-row ${stateClass}${ciBadCls}${apiErrCls}${focusedCls}${needHumanCls}" data-target="${w.target}">
      <span class="stream-time">${when}</span>
      <span class="stream-icon">${streamIcon(w.state)}</span>
      <div class="stream-body">
        <div class="stream-title">
          <b>${escapeHtml(w.name)}</b>
          <em>${branchPart}</em>
          ${prefs.hasAnnotation(w.pid) ? `<span class="stream-anno" title="has notes">📝</span>` : ""}
          ${w.api_error ? `<span class="stream-api-error" title="last tool result was an API error — pane is waiting for a nudge">⚠ API error</span>` : ""}
          <span class="stream-extra">${ctxPart}</span>
        </div>
        <div class="stream-msg">${msg}</div>
      </div>
      <div class="stream-action">${streamAction(w.state)}</div>
    </div>
  `;
}

function passesStreamQuery(w, q) {
  if (!q) return true;
  const needle = q.toLowerCase();
  return (
    (w.name || "").toLowerCase().includes(needle) ||
    (w.session || "").toLowerCase().includes(needle)
  );
}

// Channel `need_human` notification with unread = pane is paging the user.
// Same gate as the dashboard-wide attention fade in render(); kept in sync
// so what gets pinned at the top of the stream matches what's lit up
// elsewhere.
function hasUnreadNeedHuman(w) {
  if (!(w.channel_unread > 0)) return false;
  return (w.channel_replies || []).some((r) => r.kind === "need_human");
}

const STREAM_QUERY_KEY = "periscope-stream-query";

function loadStreamQuery() {
  try {
    return localStorage.getItem(STREAM_QUERY_KEY) || "";
  } catch {
    return "";
  }
}

function saveStreamQuery(q) {
  try {
    if (q) localStorage.setItem(STREAM_QUERY_KEY, q);
    else localStorage.removeItem(STREAM_QUERY_KEY);
  } catch {
    // Quota or disabled storage — query falls back to in-memory only.
  }
}

function ensureStreamScaffold() {
  // Stream toolbar (filter + new-tab) is built once and re-used across
  // polls. Rebuilding the input every 1.5s would yank focus and clobber
  // the user's typing mid-keystroke; we only refresh the dynamic parts
  // (banner text, row list, new-tab session label).
  if (document.getElementById("stream-toolbar")) return;
  // Hydrate the query from localStorage on first build. State already
  // holds it across re-renders within a single page load; this picks up
  // a value from the previous load.
  if (!state.streamQuery) state.streamQuery = loadStreamQuery();
  grid.innerHTML = `
    <div class="stream-toolbar" id="stream-toolbar">
      <input id="stream-filter" class="stream-filter" type="text"
             placeholder="filter by name or session… (press / to focus)" autocomplete="off"
             value="${escapeHtml(state.streamQuery || "")}">
      <button id="stream-new-tab" class="stream-new-tab" type="button" hidden></button>
    </div>
    <div class="stream-banner" id="stream-banner"></div>
    <div class="stream" id="stream-list"></div>
  `;
  const input = document.getElementById("stream-filter");
  input.addEventListener("input", () => {
    state.streamQuery = input.value;
    saveStreamQuery(input.value);
    renderStream(state.lastWindows);
  });
  // Esc clears the query and re-renders. Doesn't blur — Esc is more useful
  // as "abort current filter" than "leave the search," especially since
  // there's no other Esc handler bound to the stream view.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && input.value) {
      e.stopPropagation();
      input.value = "";
      state.streamQuery = "";
      saveStreamQuery("");
      renderStream(state.lastWindows);
    }
  });
  document.getElementById("stream-new-tab").addEventListener(
    "click", handleStreamNewTab,
  );
}

async function handleStreamNewTab(e) {
  const btn = e.currentTarget;
  const session = btn.dataset.session;
  const exec = btn.dataset.exec || "";
  if (!session) return;
  btn.disabled = true;
  try {
    await apiCall(
      "new window",
      `/api/window/new?session=${encodeURIComponent(session)}&exec=${encodeURIComponent(exec)}`,
      { method: "POST" },
    );
  } finally {
    btn.disabled = false;
  }
  poll();
}

function updateStreamNewTab(topRow) {
  const btn = document.getElementById("stream-new-tab");
  if (!btn) return;
  const commands = prefs.getCommands();
  const primary = commands[0];
  // Need both: a session to spawn into (from the topmost row) AND a
  // primary command (so we know what to launch). Without either, hide
  // the button — header buttons handle the "from-scratch" cases.
  if (!topRow || !primary) {
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  btn.dataset.session = topRow.session;
  btn.dataset.exec = primary.exec || "";
  btn.textContent = `+ ${primary.label} in ${topRow.session}`;
  btn.title = `spawn \`${primary.exec || "shell"}\` as a new window in tmux session '${topRow.session}'`;
}

function renderStream(windows) {
  // Stream considers *only* windows Tom has actually engaged with in
  // periscope (acted_at > 0). Sessions Tom has switched to in tmux but
  // never opened in the dashboard don't show here.
  ensureStreamScaffold();
  const banner = document.getElementById("stream-banner");
  const list = document.getElementById("stream-list");

  const opened = windows.filter((w) => w.acted_at > 0);
  // Two-key sort:
  //   1. needs-human-and-unread group first (Claude is paging the user via
  //      the channel — this outranks anything else, including a tab opened
  //      30s ago, because the alert IS the reason to look at the stream).
  //   2. acted_at desc within each group.
  const visible = opened
    .filter(passesFilter)
    .filter((w) => passesStreamQuery(w, state.streamQuery))
    .sort((a, b) => {
      const ah = hasUnreadNeedHuman(a) ? 0 : 1;
      const bh = hasUnreadNeedHuman(b) ? 0 : 1;
      if (ah !== bh) return ah - bh;
      return (b.acted_at || 0) - (a.acted_at || 0);
    });

  // Track the rendered order so ↑/↓ key handlers can step through it
  // without recomputing the sort.
  state.streamVisible = visible.map((w) => w.target);

  // Reconcile focused target with what's actually visible. If the focused
  // row got filtered out (or it's still null on first paint), snap to the
  // top of the list.
  if (
    !state.streamFocusedTarget ||
    !state.streamVisible.includes(state.streamFocusedTarget)
  ) {
    state.streamFocusedTarget = state.streamVisible[0] || null;
  }

  // Topmost row's session powers the "+ new tab" button — keep this
  // before the empty-state early returns so the button updates even when
  // the filtered list is empty (it stays usable while you're searching).
  updateStreamNewTab(visible[0] || opened.sort((a, b) => b.acted_at - a.acted_at)[0]);

  if (!opened.length) {
    banner.textContent = "";
    list.innerHTML = `<div class="empty-state">no tabs opened yet — click a card in grid view to start tracking activity</div>`;
    updateToggleAll([]);
    return;
  }
  if (!visible.length) {
    const reason = state.streamQuery
      ? `no opened tabs match "${state.streamQuery}"`
      : "no opened tabs match the current filter";
    banner.textContent = "";
    list.innerHTML = `<div class="empty-state">${escapeHtml(reason)}</div>`;
    updateToggleAll([]);
    return;
  }

  const attention = visible.filter(
    (w) => w.state === "needs-input" || w.state === "working"
  ).length;
  banner.textContent = `Now · ${attention} ${attention === 1 ? "needs" : "need"} attention`;
  list.innerHTML = visible.map(renderStreamRow).join("");
  updateToggleAll([]);  // toggle-all is grid-only; hide while in stream
}

function renderGrid(windows) {
  const filtered = windows.filter(passesFilter);
  const bySession = new Map();
  for (const w of filtered) {
    if (!bySession.has(w.session)) bySession.set(w.session, []);
    bySession.get(w.session).push(w);
  }

  if (filtered.length === 0) {
    grid.innerHTML = `<div class="empty-state">no windows match the current filter</div>`;
    updateToggleAll([]);
    return;
  }

  const sessionOrder = orderedSessions([...bySession.keys()], bySession);
  const totals = new Map();
  for (const w of windows) totals.set(w.session, (totals.get(w.session) || 0) + 1);

  grid.innerHTML = sessionOrder
    .map((s) => {
      // Hard rule: the leftmost card in a session is the one most recently
      // opened in periscope (acted_at). Nothing else influences order — tmux
      // active-window changes don't move cards. Cards never opened from the
      // dashboard fall back to tmux index for a stable position.
      const ws = bySession.get(s).slice().sort((a, b) => {
        const da = (b.acted_at || 0) - (a.acted_at || 0);
        if (da !== 0) return da;
        return a.index - b.index;
      });
      return renderSession(s, ws, totals.get(s));
    })
    .join("");

  updateToggleAll(sessionOrder);
}

export function render(windows) {
  // Dispatch on the view attribute the user toggled via the view-switch.
  // Defaults to grid when unset (first paint, or no localStorage entry).
  state.projectsByTmux = indexProjects(state.lastProjects || []);
  const view = document.body.dataset.view === "stream" ? "stream" : "grid";
  if (view === "stream") renderStream(windows);
  else renderGrid(windows);

  // "send to N" bulk-broadcast button. Scoped to whatever the current filter
  // already shows — composes with the existing filter UX instead of inventing
  // a new "pick which panes" modal. Hidden when there's nothing to broadcast
  // to (zero or one matching pane); the singular case has plenty of single-
  // pane affordances already.
  const sendBulkBtn = document.getElementById("send-bulk");
  if (sendBulkBtn) {
    const visible = windows.filter(passesFilter);
    if (visible.length > 1) {
      sendBulkBtn.hidden = false;
      sendBulkBtn.textContent = `→ send to ${visible.length}`;
    } else {
      sendBulkBtn.hidden = true;
    }
  }

  // Fade non-alerting cards when something needs the user's eyes. Same gate
  // as the per-card needsAttention so the two stay in sync.
  const anyAttention = windows.some((w) => {
    return (w.channel_replies || []).some((r) => r.kind === "need_human")
        && (w.channel_unread || 0) > 0;
  });
  grid.classList.toggle("grid-has-attention", anyAttention);

  // Counts in header — same in both views. Lead with needs-input — that's
  // the only count that means "drop what you're doing"; renders only when
  // nonzero. Each count is its own classed span so CSS colors them by
  // status.
  const total = windows.length;
  const needsInput = windows.filter((w) => w.state === "needs-input").length;
  const working = windows.filter((w) => w.state === "working").length;
  const done = windows.filter((w) => w.state === "done").length;
  const idle = windows.filter((w) => w.state === "idle").length;
  const sep = `<span class="count-sep">·</span>`;
  const segments = [`<span><b>${total}</b> windows</span>`];
  if (needsInput) segments.push(`<span class="count-needs">${needsInput} needs input</span>`);
  segments.push(`<span class="count-working">${working} working</span>`);
  segments.push(`<span class="count-done">${done} done</span>`);
  segments.push(`<span class="count-idle">${idle} idle</span>`);
  counts.innerHTML = segments.join(` ${sep} `);
}

function startRename(nameEl, target, currentName) {
  if (state.editingTarget) return;
  state.editingTarget = target;
  const input = document.createElement("input");
  input.type = "text";
  input.value = currentName;
  input.className = "rename-input";
  nameEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const newName = input.value.trim();
    state.editingTarget = null;
    if (save && newName && newName !== currentName) {
      try {
        await fetch(`/api/rename?${targetQuery(target)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: newName }),
        });
      } catch (e) {
        // poll() below will resync from tmux either way
      }
    }
    poll();  // immediate refresh so the new name appears
  };

  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("dblclick", (e) => e.stopPropagation());
}

async function handlePromote(btn) {
  const session = btn.dataset.session;
  const index = parseInt(btn.dataset.index, 10);
  if (!session || !Number.isFinite(index)) return;
  btn.disabled = true;
  try {
    const res = await fetch("/api/projects/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, index }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`promote failed: ${err.detail || res.status}`);
    }
  } finally {
    btn.disabled = false;
  }
}

async function handleAdopt(btn) {
  const session = btn.dataset.session;
  if (!session) return;
  btn.disabled = true;
  try {
    const res = await fetch("/api/projects/adopt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tmux_session: session }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`adopt failed: ${err.detail || res.status}`);
    }
  } finally {
    btn.disabled = false;
  }
}

let openProjectMenu = null;  // module-level: {pinnedDir, panelEl, anchorEl}

function closeProjectMenu() {
  if (!openProjectMenu) return;
  openProjectMenu.panelEl.remove();
  document.removeEventListener("click", onDocumentClickForMenu);
  document.removeEventListener("keydown", onKeydownForMenu);
  openProjectMenu = null;
}

function onDocumentClickForMenu(e) {
  if (!openProjectMenu) return;
  if (openProjectMenu.panelEl.contains(e.target)) return;
  if (openProjectMenu.anchorEl.contains(e.target)) return;
  closeProjectMenu();
}

function onKeydownForMenu(e) {
  if (e.key === "Escape") closeProjectMenu();
}

function handleProjectMenu(btn) {
  // If another menu is open, close it first.
  if (openProjectMenu && openProjectMenu.anchorEl !== btn) {
    closeProjectMenu();
  } else if (openProjectMenu) {
    closeProjectMenu();
    return;
  }

  const pinnedDir = btn.dataset.pinnedDir;
  if (!pinnedDir) return;

  // Build the panel. Position absolutely under the anchor.
  const panel = document.createElement("div");
  panel.className = "project-menu-panel";
  panel.innerHTML = `
    <button class="project-menu-item" data-action="rename">Rename</button>
    <button class="project-menu-item" data-action="archive">Archive</button>
  `;

  // Anchor the panel under the ⋯ button. Positioned via fixed + getBoundingClientRect
  // so it works even when the parent is overflow-hidden.
  const rect = btn.getBoundingClientRect();
  panel.style.position = "fixed";
  panel.style.top = `${rect.bottom + 2}px`;
  panel.style.right = `${window.innerWidth - rect.right}px`;
  document.body.appendChild(panel);

  openProjectMenu = { pinnedDir, panelEl: panel, anchorEl: btn };
  // Defer document listeners by one tick so the originating click doesn't
  // immediately fire close.
  setTimeout(() => {
    document.addEventListener("click", onDocumentClickForMenu);
    document.addEventListener("keydown", onKeydownForMenu);
  }, 0);

  panel.addEventListener("click", (e) => {
    const item = e.target.closest(".project-menu-item");
    if (!item) return;
    const action = item.dataset.action;
    if (action === "rename") {
      closeProjectMenu();
      startProjectRename(pinnedDir);
    } else if (action === "archive") {
      // Inline two-click confirm: first click changes the button label.
      if (item.dataset.confirming) {
        closeProjectMenu();
        archiveProject(pinnedDir);
      } else {
        item.dataset.confirming = "1";
        item.textContent = "Click again to confirm";
        item.classList.add("project-menu-item-confirming");
      }
    }
  });
}

function startProjectRename(pinnedDir) {
  // Find the project's session header by walking projectsByTmux back to
  // the data-session attribute. Then make the .session-name h2 editable
  // in place.
  const project = (state.lastProjects || []).find((p) => p.pinned_dir === pinnedDir);
  if (!project) return;
  const session = project.tmux_session;
  const header = grid.querySelector(`.session-header[data-session="${session}"]`);
  if (!header) return;
  const nameEl = header.querySelector(".session-name");
  if (!nameEl) return;

  const currentName = project.name || session;
  const input = document.createElement("input");
  input.type = "text";
  input.value = currentName;
  input.className = "session-name-input";
  nameEl.replaceWith(input);
  input.focus();
  input.select();

  const commit = async () => {
    // Guard against double-fire: `Enter` calls commit synchronously, which
    // removes the input from the DOM. Browser then fires `blur` on the
    // detached input, which our listener would re-invoke. Idempotency flag
    // makes the second invocation a no-op.
    if (input.dataset.committed) return;
    input.dataset.committed = "1";
    const newName = input.value.trim();
    // Restore the heading regardless (re-rendered on poll if rename succeeded).
    const restored = document.createElement("h2");
    restored.className = "session-name";
    restored.textContent = currentName;
    input.replaceWith(restored);
    if (!newName || newName === currentName) return;
    try {
      const res = await fetch("/api/projects/patch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          pinned_dir: pinnedDir,
          name: newName,
          tmux_session: newName,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`rename failed: ${err.detail || res.status}`);
      }
    } catch (e) {
      alert(`rename request failed: ${e.message}`);
    }
  };
  const cancel = () => {
    const restored = document.createElement("h2");
    restored.className = "session-name";
    restored.textContent = currentName;
    input.replaceWith(restored);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancel();
    }
  });
  input.addEventListener("blur", commit);
}

async function archiveProject(pinnedDir) {
  try {
    const res = await fetch("/api/projects/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned_dir: pinnedDir }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`archive failed: ${err.detail || res.status}`);
    }
  } catch (e) {
    alert(`archive request failed: ${e.message}`);
  }
}

async function handleAutoRename(autoBtn) {
  if (autoBtn.dataset.busy) return;
  const session = autoBtn.dataset.session;
  autoBtn.dataset.busy = "1";
  const orig = autoBtn.innerHTML;
  autoBtn.innerHTML = "✨ thinking…";
  autoBtn.disabled = true;
  try {
    const res = await fetch(
      `/api/auto-rename-session?session=${encodeURIComponent(session)}`,
      { method: "POST" }
    );
    const data = await res.json();
    if (!data.ok) {
      autoBtn.innerHTML = `✗ ${escapeHtml(data.error || "failed").slice(0, 40)}`;
      setTimeout(() => { autoBtn.innerHTML = orig; }, 4000);
    } else {
      const n = (data.applied || []).length;
      autoBtn.innerHTML = n ? `✓ renamed ${n}` : "✓ all good";
      setTimeout(() => { autoBtn.innerHTML = orig; }, 2500);
      poll();
    }
  } catch (err) {
    autoBtn.innerHTML = `✗ ${err.message}`.slice(0, 40);
    setTimeout(() => { autoBtn.innerHTML = orig; }, 4000);
  } finally {
    autoBtn.disabled = false;
    delete autoBtn.dataset.busy;
  }
}

async function handleKillSession(btn) {
  const session = btn.dataset.session;
  const n = state.lastWindows.filter((w) => w.session === session).length;
  const msg = `Kill session '${session}'?\n\nCloses ${n} window${n === 1 ? "" : "s"} and detaches any attached client.`;
  if (!confirm(msg)) return;
  await apiCall("kill session", `/api/session?session=${encodeURIComponent(session)}`, {
    method: "DELETE",
  });
  poll();
}

async function handleKillWindow(btn) {
  const target = btn.dataset.target;
  const name = btn.dataset.name;
  if (!confirm(`Kill window '${name}' (${target})?`)) return;
  await apiCall("kill window", `/api/window?${targetQuery(target)}`, {
    method: "DELETE",
  });
  poll();
}

async function handleNewWindow(btn) {
  const session = btn.dataset.session;
  const exec = btn.dataset.exec || "";
  const tile = btn.closest(".card-new");
  // Disable all buttons in the tile while the request is in flight so a
  // double-click can't spawn two windows.
  tile.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    await apiCall(
      "new window",
      `/api/window/new?session=${encodeURIComponent(session)}&exec=${encodeURIComponent(exec)}`,
      { method: "POST" }
    );
  } finally {
    tile.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
  poll();
}

async function handleWorktreeVariant(btn) {
  const session = btn.dataset.session;
  const exec = btn.dataset.exec || "";
  const label = btn.dataset.label || "command";
  if (!session) return;

  // Swap the new-tile's contents for an inline branch-name form.
  // Closing/cancelling restores the tile via the next /api/state poll's
  // re-render (3s max). Storing a flag on the tile so other handlers
  // don't fight us mid-flow.
  const tile = btn.closest(".card-new");
  if (!tile) return;
  if (tile.dataset.worktreeForm === "1") return;  // already open
  tile.dataset.worktreeForm = "1";
  const prevHtml = tile.innerHTML;

  tile.innerHTML = `
    <div class="new-window-worktree-form">
      <div class="new-window-worktree-label">+ ${escapeHtml(label)} (worktree)</div>
      <input type="text" class="new-window-worktree-input" placeholder="branch name (e.g. tc/sub-feat)" autofocus>
      <div class="new-window-worktree-actions">
        <button class="new-window-worktree-cancel" type="button">cancel</button>
        <button class="new-window-worktree-submit" type="button">create</button>
      </div>
    </div>
  `;
  const input = tile.querySelector(".new-window-worktree-input");
  input.focus();

  const restore = () => {
    tile.removeAttribute("data-worktree-form");
    tile.innerHTML = prevHtml;
  };

  const submit = async () => {
    const branch = input.value.trim();
    if (!branch) {
      input.focus();
      return;
    }
    const params = new URLSearchParams({ session, branch });
    if (exec) params.set("exec", exec);
    try {
      const res = await fetch(`/api/window/new-worktree?${params}`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`new worktree tab failed: ${err.detail || res.status}`);
        restore();
        return;
      }
      const body = await res.json();
      if (body.warning) console.warn("new-worktree warning:", body.warning);
      restore();
    } catch (e) {
      alert(`request failed: ${e.message}`);
      restore();
    }
  };

  tile.querySelector(".new-window-worktree-cancel").addEventListener("click", restore);
  tile.querySelector(".new-window-worktree-submit").addEventListener("click", submit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      restore();
    }
  });
}

function reorderSessions(src, dst, before) {
  // Build the order from current DOM (so we capture auto-sorted positions of new sessions too)
  const all = [...grid.querySelectorAll(".session-group")].map(
    (g) => g.dataset.session
  );
  const without = all.filter((s) => s !== src);
  const dstIdx = without.indexOf(dst);
  const insertAt = before ? dstIdx : dstIdx + 1;
  without.splice(insertAt, 0, src);
  prefs.setSessionOrder(without);
  render(state.lastWindows);
}

function wireGrid() {
  if (toggleAllBtn) toggleAllBtn.addEventListener("click", handleToggleAll);

  grid.addEventListener("click", (e) => {
    // Mutation buttons inside the grid take priority over the more general
    // header-toggle / card-open handlers below.
    const adoptBtn = e.target.closest(".adopt");
    if (adoptBtn) {
      e.stopPropagation();
      handleAdopt(adoptBtn);
      return;
    }
    const autoBtn = e.target.closest(".auto-rename");
    if (autoBtn) {
      e.stopPropagation();
      handleAutoRename(autoBtn);
      return;
    }
    const projectMenuBtn = e.target.closest(".project-menu");
    if (projectMenuBtn) {
      e.stopPropagation();
      handleProjectMenu(projectMenuBtn);
      return;
    }
    const killSessionBtn = e.target.closest(".kill-session");
    if (killSessionBtn) {
      e.stopPropagation();
      handleKillSession(killSessionBtn);
      return;
    }
    const promoteBtn = e.target.closest(".card-promote");
    if (promoteBtn) {
      e.stopPropagation();
      handlePromote(promoteBtn);
      return;
    }
    const killWindowBtn = e.target.closest(".card-kill");
    if (killWindowBtn) {
      e.stopPropagation();
      handleKillWindow(killWindowBtn);
      return;
    }
    const newWorktreeBtn = e.target.closest(".new-window-worktree");
    if (newWorktreeBtn) {
      e.stopPropagation();
      handleWorktreeVariant(newWorktreeBtn);
      return;
    }
    const newWindowBtn = e.target.closest(".new-window");
    if (newWindowBtn) {
      e.stopPropagation();
      handleNewWindow(newWindowBtn);
      return;
    }
    // Header click toggles collapse (unless the header is mid-drag).
    const header = e.target.closest(".session-header");
    if (header && !header.classList.contains("dragging")) {
      const session = header.dataset.session;
      if (state.collapsedSessions.has(session)) state.collapsedSessions.delete(session);
      else state.collapsedSessions.add(session);
      prefs.setCollapsed(state.collapsedSessions);
      header.closest(".session-group").classList.toggle("collapsed");
      return;
    }
    // Stream-row click: open modal. Stream rows don't carry a renameable
    // title surface, so no dblclick-defer needed — checked before .card so
    // the next branch's renameable-title logic doesn't apply. Also moves
    // the keyboard-focus marker so ↑/↓ picks up from the clicked row.
    const streamRow = e.target.closest(".stream-row");
    if (streamRow) {
      state.streamFocusedTarget = streamRow.dataset.target;
      openModal(streamRow.dataset.target);
      return;
    }
    // Card click: open modal, but defer if the click is on the name (so a
    // dblclick can win and start a rename instead).
    const card = e.target.closest(".card");
    if (!card) return;
    const target = card.dataset.target;
    // LGTM badge: open the modal directly on the Review tab. Check before
    // the .card-title branch so it takes precedence.
    if (e.target.closest("[data-lgtm-badge]")) {
      e.stopPropagation();
      openModal(target, { tab: "review" });
      return;
    }
    const onName = !!e.target.closest(".card-title");
    if (!onName) {
      openModal(target);
      return;
    }
    if (nameClickTimers.has(target)) return;
    const timer = setTimeout(() => {
      nameClickTimers.delete(target);
      openModal(target);
    }, 220);
    nameClickTimers.set(target, timer);
  });

  grid.addEventListener("dblclick", (e) => {
    const nameEl = e.target.closest(".card-title");
    if (!nameEl) return;
    const card = nameEl.closest(".card");
    const target = card.dataset.target;
    e.stopPropagation();
    const timer = nameClickTimers.get(target);
    if (timer) {
      clearTimeout(timer);
      nameClickTimers.delete(target);
    }
    startRename(nameEl, target, nameEl.textContent);
  });

  // Drag-and-drop — delegated on grid. Two drags coexist via MIME type:
  //   text/plain                    → session header reorder
  //   application/periscope-card    → card move into another session
  // Card drags don't also set text/plain so the reorder branch ignores them.
  const CARD_MIME = "application/periscope-card";

  grid.addEventListener("dragstart", (e) => {
    const header = e.target.closest(".session-header");
    if (header) {
      header.classList.add("dragging");
      // Pause polling: the 3s poll rebuilds grid.innerHTML, which would
      // destroy the element being dragged and silently abort the drag.
      state.dragging = true;
      e.dataTransfer.setData("text/plain", header.dataset.session);
      e.dataTransfer.effectAllowed = "move";
      return;
    }
    const card = e.target.closest(".card");
    // Skip the "+ new" tile (no data-target). Cards have data-target = "sess:idx".
    if (card && card.dataset.target) {
      card.classList.add("dragging");
      state.dragging = true;
      e.dataTransfer.setData(CARD_MIME, card.dataset.target);
      e.dataTransfer.effectAllowed = "move";
    }
  });

  grid.addEventListener("dragend", () => {
    state.dragging = false;
    grid.querySelectorAll(".dragging").forEach((el) => el.classList.remove("dragging"));
    grid.querySelectorAll(".drag-over-top, .drag-over-bottom, .card-drop-target")
      .forEach((el) => el.classList.remove("drag-over-top", "drag-over-bottom", "card-drop-target"));
  });

  grid.addEventListener("dragover", (e) => {
    if (e.dataTransfer.types.includes(CARD_MIME)) {
      const group = e.target.closest(".session-group");
      if (!group) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      // Single active drop target at a time so the highlight follows the cursor
      // instead of accumulating as the pointer crosses groups.
      grid.querySelectorAll(".card-drop-target").forEach((g) => {
        if (g !== group) g.classList.remove("card-drop-target");
      });
      group.classList.add("card-drop-target");
      return;
    }
    const header = e.target.closest(".session-header");
    if (!header) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const rect = header.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    header.classList.toggle("drag-over-top", before);
    header.classList.toggle("drag-over-bottom", !before);
  });

  grid.addEventListener("dragleave", (e) => {
    const header = e.target.closest(".session-header");
    if (header) header.classList.remove("drag-over-top", "drag-over-bottom");
    // Group highlight is cleaned up in dragend; per-leave clearing flickers
    // because moving between a group's children fires leave/enter rapidly.
  });

  grid.addEventListener("drop", (e) => {
    // Clear the drag-pause flag here too: the drop handlers below call
    // render(), which detaches the dragged element — a subsequent dragend
    // on a detached node isn't guaranteed, so don't rely on it alone.
    state.dragging = false;
    if (e.dataTransfer.types.includes(CARD_MIME)) {
      const group = e.target.closest(".session-group");
      if (!group) return;
      e.preventDefault();
      const src = e.dataTransfer.getData(CARD_MIME);
      const dest = group.dataset.session;
      moveCard(src, dest);
      return;
    }
    const header = e.target.closest(".session-header");
    if (!header) return;
    e.preventDefault();
    const src = e.dataTransfer.getData("text/plain");
    const dst = header.dataset.session;
    if (src === dst) return;
    const rect = header.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    reorderSessions(src, dst, before);
  });
}

async function moveCard(target, dest) {
  // target = "session:index"; split on the *last* colon so session names
  // containing ":" still parse (matches util.targetQuery's convention).
  const i = target.lastIndexOf(":");
  if (i < 0) return;
  const session = target.slice(0, i);
  const index = target.slice(i + 1);
  if (session === dest) return;  // no-op drop on source session
  const params = new URLSearchParams({ session, index, dest });
  const data = await apiCall("move window", `/api/window/move?${params.toString()}`, {
    method: "POST",
  });
  if (data) poll();
}

export async function poll() {
  if (state.editingTarget) return;  // user is mid-rename; don't blow away their input
  if (state.dragging) return;  // mid-drag; a render() would destroy the drag source
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.lastWindows = data.windows;
    state.lastProjects = data.projects || [];
    render(state.lastWindows);
    updateUsagePill(data.usage_scrape, data.usage);
    lastUpdate.textContent = `updated ${new Date().toLocaleTimeString()}`;
    if (consecutivePollFails > 0) {
      consecutivePollFails = 0;
      if (bannerEl) bannerEl.hidden = true;
      document.body.classList.remove("disconnected");
    }
  } catch (e) {
    consecutivePollFails += 1;
    // Threshold of 2 (≈6s detection) avoids false-positive flicker on
    // a single transient hiccup (laptop sleep, background-tab throttling).
    if (consecutivePollFails >= 2) {
      if (bannerEl) bannerEl.hidden = false;
      document.body.classList.add("disconnected");
    }
    lastUpdate.textContent = `poll failed: ${e.message}`;
  }
}

function fmtTokens(n) {
  if (!n) return "0";
  if (n < 1000) return `${n}`;
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  return `${(n / 1_000_000_000).toFixed(2)}B`;
}

function fmtResetCountdown(epochSec) {
  if (!epochSec) return "5h window open";
  const diff = epochSec - Math.floor(Date.now() / 1000);
  if (diff <= 0) return "resets now";
  if (diff < 60) return `resets in ${diff}s`;
  if (diff < 3600) return `resets in ${Math.floor(diff / 60)}m`;
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  return `resets in ${h}h ${m}m`;
}

function meterBar(label, pct, resets) {
  const tone = pct >= 90 ? "danger" : pct >= 70 ? "warn" : "ok";
  return `
    <div class="usage-item" title="${escapeHtml(label)} — ${pct}% used. Resets ${escapeHtml(resets || "")}">
      <span class="usage-item-label">${escapeHtml(label)}</span>
      <span class="usage-item-bar"><span class="usage-item-fill ${tone}" style="width:${pct}%"></span></span>
      <b>${pct}%</b>
    </div>
  `;
}

function updateUsagePill(scraped, fallback) {
  if (!usageEl) return;
  // Prefer the scraped TUI data (real plan percentages). Fall back to the
  // JSONL-derived 5h pill when the scrape hasn't completed yet (first ~20s
  // after server start) or failed.
  if (scraped && scraped.available && scraped.meters) {
    const m = scraped.meters;
    const order = ["session", "week_all", "week_sonnet"];
    const compactLabels = { session: "session", week_all: "week", week_sonnet: "sonnet" };
    usageEl.classList.remove("usage-fallback");
    usageEl.innerHTML = order
      .filter((k) => m[k])
      .map((k) => meterBar(compactLabels[k], m[k].percent, m[k].resets))
      .join("");
    usageEl.title = order
      .filter((k) => m[k])
      .map((k) => `${m[k].label}: ${m[k].percent}% used\n  Resets ${m[k].resets}`)
      .join("\n\n");
    return;
  }
  if (!fallback || !fallback.available) {
    usageEl.classList.remove("usage-fallback");
    usageEl.textContent = "";
    usageEl.title = "";
    return;
  }
  const active = (fallback.input_tokens || 0) + (fallback.cache_creation_tokens || 0) + (fallback.output_tokens || 0);
  usageEl.classList.add("usage-fallback");
  usageEl.textContent = `5h: ${fmtTokens(active)} · ${fmtResetCountdown(fallback.reset_at)}`;
  usageEl.title = `Claude Code plan usage estimate (JSONL-derived; scrape not yet ready)\n` +
    `  ${fallback.messages} assistant messages\n` +
    `  ${fmtTokens(active)} active tokens\n` +
    `  ${fmtTokens(fallback.cache_read_tokens)} cache reads (discounted)`;
}

export function initGrid() {
  wireGrid();
  poll();
  setInterval(poll, POLL_MS);
}
