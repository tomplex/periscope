// Modal lifecycle: open/close, header refresh, inline rename, auto-rename,
// image-paste forwarding.
//
// Imports `poll` from grid.js to refresh card data after a rename/auto-rename.
// This is a circular import (grid.js imports openModal from here) — it works
// because `poll` is a function declaration (hoisted) and is only called inside
// event handlers, never at module top level.
import { pushEscape, popEscape } from './overlay.js';

import { state } from './state.js';
import { escapeHtml, targetQuery, apiCall, relTime } from './util.js';
import { startLiveTerminal, stopLiveTerminal, writeTerminalLine } from './terminal.js';
import { poll } from './grid.js';
import * as prefs from './prefs.js';

const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modal-title");
const modalXtermEl = document.getElementById("modal-xterm");
const modalClose = document.getElementById("modal-close");
const modalSubtitle = document.getElementById("modal-subtitle");
const modalAutoRename = document.getElementById("modal-auto-rename");
const modalSide = document.getElementById("modal-side");
const modalTabs = document.getElementById("modal-tabs");
const modalReviewContent = document.getElementById("modal-review-content");

const MODAL_POLL_MS = 1500;
let modalPollHandle = null;
// Slug of the LGTM session currently mounted in the iframe. Lets us skip
// remounts when /api/pane reports the same session on subsequent polls,
// which would otherwise reset scroll and any in-flight UI state.
let mountedLgtmSlug = null;
// Last /api/pane response. Cached so a tab switch can render the Review
// pane immediately using already-known data, rather than waiting for the
// next 1.5s poll to land. Cleared on modal close.
let lastPaneData = null;

export function openModal(target, opts = {}) {
  state.activeTarget = target;
  pushEscape(closeModal);
  modalTitle.textContent = target;
  modalSubtitle.innerHTML = "";
  // Reset tab to terminal unless the caller asked otherwise (e.g. clicking
  // an LGTM badge on the grid card). Clear any iframe from a previous pane.
  setActiveTab(opts.tab === "review" ? "review" : "terminal");
  modalReviewContent.innerHTML = "";
  mountedLgtmSlug = null;
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  startLiveTerminal(target);
  // Header poll keeps the subtitle/brief/spinner fresh; the terminal body
  // itself streams live via the WebSocket, no polling needed.
  refreshModalHeader();
  modalPollHandle = setInterval(refreshModalHeader, MODAL_POLL_MS);
}

export function closeModal() {
  stopLiveTerminal();
  modal.classList.add("hidden");
  document.body.classList.remove("modal-open");
  if (modalPollHandle) {
    clearInterval(modalPollHandle);
    modalPollHandle = null;
  }
  if (modalSide) modalSide.innerHTML = "";
  // Drop the iframe so it stops holding the LGTM SSE connection open while
  // the modal is closed. Next open re-mounts.
  modalReviewContent.innerHTML = "";
  mountedLgtmSlug = null;
  lastPaneData = null;
  state.modalRenaming = false;
  state.activeTarget = null;
  popEscape(closeModal);
}

function setActiveTab(name) {
  if (name !== "terminal" && name !== "review") name = "terminal";
  modal.dataset.tab = name;
  if (!modalTabs) return;
  for (const btn of modalTabs.querySelectorAll(".modal-tab")) {
    const active = btn.dataset.tab === name;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  }
}

async function refreshModalHeader() {
  // /api/pane is now used only for parsed status fields (branch, PR, recap,
  // spinner). The terminal content itself streams live via WebSocket and
  // doesn't need this poll. lines=80 is enough buffer for the parser to find
  // the status block and most recent recap.
  if (!state.activeTarget) return;
  if (state.modalRenaming) return;  // don't clobber the in-flight rename input
  try {
    const res = await fetch(`/api/pane?${targetQuery(state.activeTarget)}&lines=80`);
    if (!res.ok) return;
    const data = await res.json();
    updateModalHeader(data);
  } catch (_) {
    // Transient — next tick will retry
  }
}

function updateModalHeader(data) {
  lastPaneData = data;
  // Title: window name (prominent), then session and cwd in dim text.
  // tmux window index is intentionally omitted — not useful for orientation.
  const name = data.name || data.target;
  const titleParts = [`<span class="modal-name">${escapeHtml(name)}</span>`];
  if (data.session) {
    titleParts.push(`<span class="modal-session">${escapeHtml(data.session)}</span>`);
  }
  if (data.cwd) {
    titleParts.push(`<span class="modal-cwd mono">${escapeHtml(data.cwd)}</span>`);
  }
  modalTitle.innerHTML = titleParts.join("");

  // Subtitle: branch · PR · CI · context% · model · spinner
  const parts = [];
  if (data.branch) parts.push(`<span class="mono">${escapeHtml(data.branch)}</span>`);
  if (data.pr) {
    const ciCls = data.ci === "✓" ? "ci-ok" : data.ci === "✗" ? "ci-bad" : "ci-pending";
    const ci = data.ci ? `<span class="${ciCls}">${data.ci}</span>` : "";
    parts.push(
      `<a class="pr" href="https://github.com/faradayio/fdy/pull/${data.pr}" target="_blank" rel="noopener">#${data.pr}</a> ${ci}`
    );
  }
  if (data.context_pct != null) parts.push(`${data.context_pct}%`);
  if (data.model) parts.push(escapeHtml(data.model.replace(/\s*\(.*\)/, "")));
  if (data.state === "needs-input") {
    parts.push(
      `<span class="spinner-tag" style="color: var(--s-needs); font-weight: 600;">⚠ needs input</span>`
    );
  } else if (data.spinner) {
    parts.push(
      `<span class="spinner-tag">✻ ${escapeHtml(data.spinner.toLowerCase())}…</span>`
    );
  } else if (data.pending_input) {
    parts.push(
      `<span class="spinner-tag" style="color: var(--fg-3); font-style: normal;">↗ pending</span>`
    );
  }
  modalSubtitle.innerHTML = parts.join(`<span class="sep">·</span> `);
  renderModalSidebar(data);
  updateReviewTab(data);
}

// ── Review tab: LGTM badge + iframe / Start-review empty state. ───────
// The Review tab is wired off /api/pane.lgtm (or null). When a session
// exists we mount its URL in an iframe; otherwise we render a button
// that POSTs /api/lgtm/start with the pane's cwd.
function updateReviewTab(data) {
  const reviewTabBtn = modalTabs?.querySelector('.modal-tab[data-tab="review"]');
  const lgtm = data.lgtm;
  // Badge on the tab: unresolved comment count (claude + user). 0 → no badge.
  if (reviewTabBtn) {
    const count = lgtm ? (lgtm.claude_comments || 0) + (lgtm.user_comments || 0) : 0;
    const existing = reviewTabBtn.querySelector(".lgtm-tab-badge");
    if (count > 0) {
      if (existing) {
        existing.textContent = String(count);
      } else {
        const b = document.createElement("span");
        b.className = "lgtm-tab-badge";
        b.textContent = String(count);
        reviewTabBtn.appendChild(b);
      }
    } else if (existing) {
      existing.remove();
    }
  }
  // Only touch the review content while the user is actually viewing it,
  // so we don't churn DOM on every poll for users who never open it.
  if (modal.dataset.tab !== "review") return;
  renderReviewPane(data);
}

function rewriteLgtmHost(url) {
  // Replace the URL's hostname with whatever the parent page is on. The
  // server hands out 127.0.0.1 by default, but the user may be on
  // localhost or a LAN IP; matching the parent's host keeps the iframe
  // and parent on the same hostname (port still differs).
  try {
    const u = new URL(url);
    u.hostname = window.location.hostname;
    return u.toString();
  } catch {
    return url;
  }
}

function renderReviewPane(data) {
  const lgtm = data.lgtm;
  if (lgtm && lgtm.slug && lgtm.url) {
    // Mount the iframe once per slug; skip if it's already mounted to
    // preserve scroll position and the iframe's own state.
    if (mountedLgtmSlug === lgtm.slug && modalReviewContent.querySelector("iframe")) {
      return;
    }
    modalReviewContent.innerHTML = "";
    const iframe = document.createElement("iframe");
    // Use the parent page's host (localhost vs 127.0.0.1 vs LAN IP)
    // instead of whatever the server cached. Keeps both origins under
    // the same hostname so subdomain/port differences are the only
    // cross-origin axis, which is the friendlier case.
    iframe.src = rewriteLgtmHost(lgtm.url);
    iframe.title = "LGTM review";
    // No loading=lazy: the iframe lives inside a tabbed pane that's
    // display:none while on the Terminal tab. Some browsers refuse to
    // load lazy iframes whose ancestors aren't visible, which is exactly
    // the failure mode "Review tab is blank" looks like.
    iframe.referrerPolicy = "no-referrer";
    modalReviewContent.appendChild(iframe);
    mountedLgtmSlug = lgtm.slug;
    return;
  }
  // No session yet — render the Start-review affordance. Skip if it's
  // already there (poll runs every 1.5s; we don't want to rebuild the
  // button mid-click).
  if (modalReviewContent.querySelector(".modal-review-empty")) return;
  mountedLgtmSlug = null;
  const cwd = data.cwd_raw || "";
  modalReviewContent.innerHTML = `
    <div class="modal-review-empty">
      <p>No LGTM review session for this pane's repository yet.</p>
      <p style="font-size: 11.5px; color: var(--fg-4);">${escapeHtml(cwd || "(no cwd)")}</p>
      <button type="button" id="lgtm-start-btn" ${cwd ? "" : "disabled"}>Start review</button>
      <div class="modal-review-error" hidden></div>
    </div>
  `;
  const btn = modalReviewContent.querySelector("#lgtm-start-btn");
  const err = modalReviewContent.querySelector(".modal-review-error");
  btn?.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Starting…";
    err.hidden = true;
    try {
      const res = await fetch("/api/lgtm/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cwd }),
      });
      const payload = await res.json();
      if (!payload.ok) throw new Error(payload.error || "unknown error");
      // Don't mount the iframe inline — let the next poll tick pick up
      // the lgtm field from /api/pane and render through renderReviewPane.
      // That keeps a single mount path (avoids two slightly-different
      // render branches drifting apart).
      refreshModalHeader();
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Start review";
      err.textContent = `Could not start review: ${e.message}`;
      err.hidden = false;
    }
  });
}

// ── Sidebar: Linked (PR + Linear placeholder) + Activity timeline. ───
// Data rides on the existing 1.5s /api/pane poll — no extra request.
function renderMessages(data) {
  const replies = data.channel_replies || [];
  if (!replies.length) {
    return `<em class="modal-msg-empty">No messages from Claude yet.</em>`;
  }
  return replies.map(r => {
    const kind = r.kind || "info";
    const severity = r.severity || "info";
    const time = new Date(r.ts * 1000).toLocaleTimeString();
    return `
      <div class="modal-msg modal-msg-${kind} modal-msg-sev-${severity}">
        <div class="modal-msg-meta">${time} · ${escapeHtml(kind)}</div>
        <div class="modal-msg-body">${escapeHtml(r.message)}</div>
      </div>
    `;
  }).join("");
}

function renderMessageComposer(data) {
  if (!data.channel_attached) {
    return `<div class="modal-msg-composer-disabled" title="Channel not attached. Respawn Claude via + claude.">push disabled (no channel)</div>`;
  }
  return `
    <form class="modal-msg-composer" data-pane-id="${data.pane_id}">
      <input type="text" class="modal-msg-composer-input" placeholder="Push to Claude...">
      <button type="submit" class="modal-msg-composer-btn">push</button>
    </form>
  `;
}

function renderModalSidebar(data) {
  if (!modalSide) return;
  // The 1.5s header poll re-renders the sidebar wholesale. If focus is inside
  // the sidebar (notes textarea, tag input, message composer), rebuilding the
  // innerHTML drops focus and clobbers in-flight typing. Skip this tick — the
  // next poll after blur will catch up.
  if (modalSide.contains(document.activeElement)) return;
  modalSide.innerHTML = `
    <section class="modal-side-section">
      <h4>Linked</h4>
      ${renderPRCard(data)}
      ${renderLinearCard(data)}
    </section>
    <section class="modal-side-section modal-side-notes">
      <h4>Notes</h4>
      ${renderNotesEditor(data)}
    </section>
    <section class="modal-side-section modal-side-activity">
      <h4>Activity</h4>
      ${renderActivityTimeline(data.activity)}
    </section>
    <section class="modal-side-section modal-side-messages">
      <h4>Messages</h4>
      ${renderMessages(data)}
      ${renderMessageComposer(data)}
    </section>
  `;
  wireNotesEditor(data);
  wireMessageComposer(data);

  // Clear unread when the modal is showing replies. Fire-and-forget.
  if (data.pane_id && (data.channel_unread || 0) > 0) {
    fetch(`/api/channel/clear-unread?pane=${encodeURIComponent(data.pane_id)}`, {
      method: "POST",
    });
  }
}

function avatarChars(handle) {
  if (!handle) return "?";
  // GitHub usernames carry dashes/underscores; strip and take the first two
  // letters for the avatar bubble (Inter, 2-char max per design).
  const letters = handle.replace(/[^A-Za-z0-9]/g, "");
  return (letters.slice(0, 2) || handle.slice(0, 1)).toUpperCase();
}

function renderPRCard(data) {
  if (!data.pr) {
    return `<button class="modal-side-link-btn" type="button" disabled title="link a PR — coming soon">+ link pull request</button>`;
  }
  const ciState = data.ci === "✓" ? "passing"
    : data.ci === "✗" ? "failing"
    : data.ci === "⟳" ? "running"
    : "—";
  const ciClass = data.ci === "✓" ? "ci-passing"
    : data.ci === "✗" ? "ci-failing"
    : data.ci === "⟳" ? "ci-running"
    : "";
  const draftPill = data.pr_draft
    ? `<span class="pr-mini pr-mini-draft">draft</span>`
    : `<span class="pr-mini pr-mini-open">open</span>`;
  const reviewers = (data.pr_reviewers || [])
    .map((r) => `<span class="modal-avatar" title="${escapeHtml(r)}">${escapeHtml(avatarChars(r))}</span>`)
    .join("");
  const title = escapeHtml(data.pr_title || "");
  const url = `https://github.com/faradayio/fdy/pull/${data.pr}`;
  const adds = data.pr_additions || 0;
  const dels = data.pr_deletions || 0;
  return `
    <div class="modal-card-inset">
      <div class="pr-head">
        <a class="pr-num" href="${url}" target="_blank" rel="noopener" onclick="event.stopPropagation()">#${data.pr}</a>
        <span class="pr-title" title="${title}">${title}</span>
      </div>
      <div class="pr-meta">
        ${draftPill}
        <span class="pr-diff"><span class="diff-plus">+${adds}</span> <span class="diff-minus">−${dels}</span></span>
        <span class="pr-ci ${ciClass}"><span class="ci-dot"></span>ci ${ciState}</span>
        ${reviewers ? `<span class="pr-reviewers">${reviewers}</span>` : ""}
      </div>
    </div>
  `;
}

function renderLinearCard(data) {
  // Linear linking is always Claude-declared (via the link_linear MCP tool);
  // periscope doesn't auto-detect. When set, render the same inset shape as
  // the PR card so the two linked resources read as a visual pair. When
  // unset, fall back to a disabled placeholder — a manual-link affordance is
  // deferred to a future UI pass.
  if (data.linked_linear) {
    const id = escapeHtml(data.linked_linear);
    const url = `https://linear.app/issue/${id}`;
    return `
      <div class="modal-card-inset">
        <div class="pr-head">
          <a class="pr-num" href="${url}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Linear ticket ${id} (linked by claude)">${id}</a>
          <span class="pr-title">linear ticket</span>
        </div>
      </div>
    `;
  }
  return `<button class="modal-side-link-btn" type="button" disabled title="Linear integration coming soon">+ link Linear ticket</button>`;
}

function timelineColor(kind, evState) {
  if (kind === "commit") return "var(--s-shell)";
  if (kind === "ci") {
    if (evState === "failed") return "var(--s-danger)";
    if (evState === "running") return "var(--s-working)";
    return "var(--s-success)";
  }
  if (kind === "open") return "var(--fg-3)";
  return "var(--fg-3)";
}

function timelineLabel(kind, evState) {
  if (kind === "commit") return "commit";
  if (kind === "ci") return evState ? `ci ${evState}` : "ci";
  if (kind === "open") return "opened";
  return kind;
}

function renderActivityTimeline(events) {
  if (!events || events.length === 0) {
    return `<div class="timeline-empty">no recent activity</div>`;
  }
  return `
    <ol class="timeline">
      ${events.map((e) => `
        <li class="timeline-row" data-kind="${escapeHtml(e.kind)}">
          <span class="timeline-dot" style="background:${timelineColor(e.kind, e.state)}"></span>
          <div class="timeline-body">
            <div class="timeline-text">${escapeHtml(e.text || "")}</div>
            <div class="timeline-when">${escapeHtml(timelineLabel(e.kind, e.state))} · ${escapeHtml(relTime(e.at))} ago</div>
          </div>
        </li>
      `).join("")}
    </ol>
  `;
}

function renderNotesEditor(data) {
  const pid = data.pid || "";
  const ann = pid ? prefs.getAnnotation(pid) : null;
  const notes = ann?.notes || "";
  const tags = ann?.tags || [];
  const chips = tags
    .map(
      (t, i) =>
        `<span class="tag-chip" data-tag-i="${i}">${escapeHtml(t)}<button class="tag-chip-x" data-tag-i="${i}" title="remove">×</button></span>`
    )
    .join("");
  return `
    <textarea id="modal-notes" class="modal-notes" placeholder="${
      pid ? "Notes — saves on blur" : "Notes unavailable (no pid)"
    }" ${pid ? "" : "disabled"}>${escapeHtml(notes)}</textarea>
    <div class="tag-row">
      <div class="tag-chips" id="modal-tags">${chips}</div>
      <input id="modal-tag-input" class="modal-tag-input" type="text"
             placeholder="add tag, Enter or comma" ${pid ? "" : "disabled"}>
    </div>
  `;
}

let _notesTimer = null;

function wireNotesEditor(data) {
  const pid = data.pid;
  if (!pid) return;
  const ta = document.getElementById("modal-notes");
  const ti = document.getElementById("modal-tag-input");
  const tagsHost = document.getElementById("modal-tags");
  if (!ta || !ti || !tagsHost) return;

  // Debounce typing 600ms; flush immediately on blur.
  const flushNotes = () => {
    const ann = prefs.getAnnotation(pid) || { notes: "", tags: [] };
    prefs.setAnnotation(pid, { notes: ta.value, tags: ann.tags });
  };
  ta.addEventListener("input", () => {
    clearTimeout(_notesTimer);
    _notesTimer = setTimeout(flushNotes, 600);
  });
  ta.addEventListener("blur", () => {
    clearTimeout(_notesTimer);
    flushNotes();
  });
  // Stop Escape/Enter from bubbling to the modal handler.
  ta.addEventListener("keydown", (e) => e.stopPropagation());

  const submitTag = () => {
    const raw = ti.value.trim();
    if (!raw) return;
    const ann = prefs.getAnnotation(pid) || { notes: ta.value, tags: [] };
    const parts = raw.split(/[\s,]+/).filter(Boolean);
    const nextTags = [...ann.tags, ...parts];
    prefs.setAnnotation(pid, { notes: ta.value, tags: nextTags });
    ti.value = "";
    refreshModalHeader();  // re-render the sidebar with the new chip
  };
  ti.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      submitTag();
    }
  });

  tagsHost.addEventListener("click", (e) => {
    const btn = e.target.closest(".tag-chip-x");
    if (!btn) return;
    const i = Number(btn.dataset.tagI);
    const ann = prefs.getAnnotation(pid) || { notes: ta.value, tags: [] };
    const nextTags = ann.tags.filter((_, idx) => idx !== i);
    prefs.setAnnotation(pid, { notes: ta.value, tags: nextTags });
    refreshModalHeader();
  });
}

function wireMessageComposer(data) {
  const form = modalSide?.querySelector(".modal-msg-composer");
  if (!form) return;
  const input = form.querySelector(".modal-msg-composer-input");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || !data.pane_id) return;
    input.value = "";
    await fetch(`/api/channel/push?pane=${encodeURIComponent(data.pane_id)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ content: text }),
    });
  });
}

function startModalRename() {
  if (!state.activeTarget || state.modalRenaming) return;
  const nameSpan = modalTitle.querySelector(".modal-name");
  if (!nameSpan) return;
  const currentName = nameSpan.textContent;
  state.modalRenaming = true;
  const input = document.createElement("input");
  input.type = "text";
  input.value = currentName;
  input.className = "rename-input modal-rename-input";
  nameSpan.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const newName = input.value.trim();
    state.modalRenaming = false;
    if (save && newName && newName !== currentName && state.activeTarget) {
      await fetch(`/api/rename?${targetQuery(state.activeTarget)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      });
    }
    refreshModalHeader();
    poll();  // also refresh cards on the grid
  };

  // stopPropagation so Esc/Enter don't escape to the document handler
  // (which would close the modal) or the xterm terminal.
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

async function handleModalAutoRename() {
  if (!state.activeTarget || modalAutoRename.dataset.busy) return;
  modalAutoRename.dataset.busy = "1";
  const orig = modalAutoRename.textContent;
  modalAutoRename.textContent = "✨ thinking…";
  modalAutoRename.disabled = true;
  try {
    const data = await apiCall(
      "auto-rename window",
      `/api/auto-rename-window?${targetQuery(state.activeTarget)}`,
      { method: "POST" }
    );
    if (data) {
      refreshModalHeader();
      poll();
    }
  } finally {
    modalAutoRename.textContent = orig;
    modalAutoRename.disabled = false;
    delete modalAutoRename.dataset.busy;
  }
}

export function initModal() {
  modalClose.addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeModal();
  });

  // Double-click the window name in the modal header to rename it. The
  // `.modal-name` span is rebuilt every poll by updateModalHeader, so delegate
  // from the persistent <h2> instead of attaching per-render.
  modalTitle.addEventListener("dblclick", (e) => {
    if (!e.target.closest(".modal-name")) return;
    e.stopPropagation();
    startModalRename();
  });

  modalAutoRename.addEventListener("click", (e) => {
    e.stopPropagation();
    handleModalAutoRename();
  });

  if (modalTabs) {
    modalTabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".modal-tab");
      if (!btn) return;
      e.stopPropagation();
      setActiveTab(btn.dataset.tab);
      if (btn.dataset.tab === "review") {
        // Render immediately from cached pane data so the user sees the
        // iframe (or the Start-review button) without waiting 1.5s for
        // the next poll. If no data has arrived yet, show a placeholder
        // and the next poll will replace it.
        if (lastPaneData) {
          renderReviewPane(lastPaneData);
        } else if (!modalReviewContent.firstChild) {
          modalReviewContent.innerHTML = `<div class="modal-review-empty"><p>Loading…</p></div>`;
        }
        refreshModalHeader();
      }
    });
  }

  // Image paste: when the user pastes a screenshot (or any image) into the
  // modal, upload the bytes to the server, which writes a temp file and
  // bracketed-pastes "@/tmp/foo.png " into the pane so Claude Code reads it as
  // a file reference. Text pastes are ignored here and fall through to xterm's
  // own paste handling. Capture phase so we see the event before xterm's
  // hidden textarea consumes it.
  modalXtermEl.addEventListener("paste", async (e) => {
    if (!state.activeTarget) return;
    const items = e.clipboardData?.items || [];
    for (const item of items) {
      if (item.kind !== "file" || !item.type.startsWith("image/")) continue;
      const blob = item.getAsFile();
      if (!blob) continue;
      e.preventDefault();
      e.stopPropagation();
      try {
        const res = await fetch(`/api/paste-image?${targetQuery(state.activeTarget)}`, {
          method: "POST",
          headers: { "Content-Type": blob.type || "image/png" },
          body: blob,
        });
        const data = await res.json();
        if (!data.ok) {
          writeTerminalLine(`\r\n\x1b[31m[periscope: image paste failed: ${data.error}]\x1b[0m`);
        }
      } catch (err) {
        writeTerminalLine(`\r\n\x1b[31m[periscope: image paste error: ${err.message}]\x1b[0m`);
      }
      return;
    }
  }, true);
}
