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
// Auto-rename is no longer a standalone DOM element; it's rendered into the
// title's innerHTML each poll and bound via event delegation on modalTitle.
const modalSide = document.getElementById("modal-side");
const modalTabs = document.getElementById("modal-tabs");
const modalReviewContent = document.getElementById("modal-review-content");

const MODAL_POLL_MS = 1500;
let modalPollHandle = null;
// Tab id currently mounted in the review pane. Lets us skip needless
// iframe.src reassignments and DOM rebuilds on every 1.5s poll.
let mountedTabId = null;
// Last /api/pane response. Cached so a tab switch can render the Review
// pane immediately using already-known data, rather than waiting for the
// next 1.5s poll to land. Cleared on modal close.
let lastPaneData = null;
// Set by openModal({tab: "review"}) — switches to the first LGTM tab as
// soon as data arrives. Cleared after the switch or on modal close.
let pendingReviewOpen = false;
// Set by addLgtmDocFromTerminal — switches to a freshly-added item
// once it appears in the cache. Cleared after the switch or on close.
let pendingTabIdAfterAdd = null;
// Docs pinned as top-level tabs (between Diff and the Documents dropdown).
// Persisted per LGTM slug in localStorage so pinning survives modal close
// and page refresh. Mutated by mountDoc / unmountDoc.
let mountedDocIds = new Set();
let mountedDocsSlug = null;  // slug that mountedDocIds is loaded for
const MOUNTED_DOCS_KEY_PREFIX = "periscope-lgtm-mounted:";

export function openModal(target, opts = {}) {
  state.activeTarget = target;
  pushEscape(closeModal);
  modalTitle.textContent = target;
  modalSubtitle.innerHTML = "";
  // The Review tab is now a class of tabs (one per LGTM item). When
  // opened via an LGTM badge on the card, pick the first non-terminal
  // tab once data arrives; until then, default to terminal.
  setActiveTab("terminal");
  pendingReviewOpen = opts.tab === "review";
  modalReviewContent.innerHTML = "";
  modalTabs.innerHTML = "";
  modalTabs.dataset.signature = "";
  mountedTabId = null;
  // Clear stale data from any previous pane so a poll-in-flight doesn't
  // momentarily render the previous repo's mounted tabs.
  lastPaneData = null;
  mountedDocsSlug = null;
  mountedDocIds = new Set();
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
  // Tear down the dropdown listener before nuking the tab DOM so we
  // don't leave a document-level click handler dangling.
  closeDropdownMenu();
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
  modalTabs.innerHTML = "";
  modalTabs.dataset.signature = "";
  mountedTabId = null;
  lastPaneData = null;
  pendingReviewOpen = false;
  pendingTabIdAfterAdd = null;
  state.modalRenaming = false;
  state.activeTarget = null;
  popEscape(closeModal);
}

// Tab id model:
//   "terminal"            — live pane terminal (always present, first)
//   "lgtm-start"          — bootstrap-a-review button (only when no session)
//   "lgtm:diff"           — the diff tab (gets its own button)
//   "lgtm:walkthrough"    — the walkthrough view (only when a walkthrough
//                           has been set; its own LGTM iframe view, NOT an item)
//   "lgtm:<doc-id>"       — one of N documents (collapsed into a dropdown)
// CSS treats "terminal" specially via `#modal[data-tab="terminal"]`;
// everything else maps to the review pane.
function setActiveTab(name) {
  modal.dataset.tab = name === "terminal" ? "terminal" : "review";
  modal.dataset.tabId = name;
  // Apply is-active immediately for snappiness; renderTabStrip will
  // confirm on the next poll. Only valid if the strip is already built.
  if (lastPaneData) applyActiveState(name, buildTabSpec(lastPaneData));
}

function buildTabSpec(data) {
  const items = data?.lgtm?.items || [];
  const slug = data?.lgtm?.slug;
  const hasSession = !!slug && items.length > 0;
  // Ensure the mounted set is loaded for the current slug. We do this
  // lazily here (rather than on every refresh) so the load runs once
  // per modal open, not per poll.
  if (slug && mountedDocsSlug !== slug) loadMountedDocs(slug);
  const allDocs = hasSession
    ? items
        .filter(i => i.id !== "diff")
        .map(d => ({ id: `lgtm:${d.id}`, label: d.title || d.id }))
    : [];
  // Keep mounted order stable across renders by walking allDocs; drop
  // any pinned ids that no longer exist as items.
  const mounted = allDocs.filter(d => mountedDocIds.has(d.id));
  return {
    slug,
    showDiff: hasSession && items.some(i => i.id === "diff"),
    showWalkthrough: hasSession && !!data?.lgtm?.walkthrough,
    mounted,
    docs: allDocs,
    showStart: !hasSession && !!data?.cwd_raw,
  };
}

function loadMountedDocs(slug) {
  mountedDocsSlug = slug;
  try {
    const raw = localStorage.getItem(MOUNTED_DOCS_KEY_PREFIX + slug);
    mountedDocIds = new Set(raw ? JSON.parse(raw) : []);
  } catch {
    mountedDocIds = new Set();
  }
}

function saveMountedDocs() {
  if (!mountedDocsSlug) return;
  try {
    localStorage.setItem(
      MOUNTED_DOCS_KEY_PREFIX + mountedDocsSlug,
      JSON.stringify([...mountedDocIds]),
    );
  } catch (_) {
    // Quota / disabled storage — pinning falls back to in-memory only.
  }
}

function mountDoc(tabId) {
  if (mountedDocIds.has(tabId)) return false;
  mountedDocIds.add(tabId);
  saveMountedDocs();
  return true;
}

function unmountDoc(tabId) {
  if (!mountedDocIds.has(tabId)) return false;
  mountedDocIds.delete(tabId);
  saveMountedDocs();
  return true;
}

function renderTabStrip(data) {
  if (!modalTabs) return;
  const spec = buildTabSpec(data);
  ensureStripStructure(spec);
  applyActiveState(modal.dataset.tabId || "terminal", spec);
  performAutoSwitch(spec);
}

function ensureStripStructure(spec) {
  // Signature drives rebuild. Includes mounted docs so pinning/unpinning
  // triggers a strip rebuild, but not comment counts or active-tab id —
  // those just toggle is-active flags.
  const signature = JSON.stringify({
    diff: spec.showDiff,
    walkthrough: spec.showWalkthrough,
    mounted: spec.mounted.map(d => [d.id, d.label]),
    docs: spec.docs.map(d => [d.id, d.label]),
    start: spec.showStart,
  });
  if (modalTabs.dataset.signature === signature) return;
  // Rebuild closes any open dropdown menu by destroying its DOM.
  closeDropdownMenu();
  modalTabs.dataset.signature = signature;
  modalTabs.innerHTML = "";

  modalTabs.appendChild(makeTabBtn("terminal", "Terminal"));
  if (spec.showDiff) modalTabs.appendChild(makeTabBtn("lgtm:diff", "Diff"));
  if (spec.showWalkthrough) modalTabs.appendChild(makeTabBtn("lgtm:walkthrough", "Walkthrough"));
  for (const m of spec.mounted) modalTabs.appendChild(makeMountedTab(m.id, m.label));
  if (spec.docs.length > 0) modalTabs.appendChild(makeDocsDropdown(spec.docs));
  if (spec.showStart) modalTabs.appendChild(makeTabBtn("lgtm-start", "+ Start review"));
}

function makeMountedTab(tabId, label) {
  // A mounted tab is a regular .modal-tab (so applyActiveState picks
  // it up via data-tab) with an inline × subbutton for unpinning.
  // Click on the label area switches; click on × unmounts only.
  const wrap = document.createElement("div");
  wrap.className = "modal-tab modal-tab-mounted";
  wrap.dataset.tab = tabId;
  wrap.setAttribute("role", "tab");
  wrap.setAttribute("aria-selected", "false");
  wrap.innerHTML = `
    <span class="modal-tab-mounted-label">${escapeHtml(label)}</span>
    <button type="button" class="modal-tab-mounted-unmount"
            data-unmount-doc="${escapeHtml(tabId)}"
            title="Unpin ${escapeHtml(label)} from tabs"
            aria-label="Unpin ${escapeHtml(label)}">×</button>
  `;
  return wrap;
}

function makeTabBtn(tabId, label) {
  const btn = document.createElement("button");
  btn.className = "modal-tab";
  btn.dataset.tab = tabId;
  btn.type = "button";
  btn.setAttribute("role", "tab");
  btn.setAttribute("aria-selected", "false");
  btn.textContent = label;
  return btn;
}

function makeDocsDropdown(docs) {
  const wrap = document.createElement("div");
  wrap.className = "modal-tab-dropdown";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "modal-tab modal-tab-dropdown-toggle";
  toggle.dataset.docDropdownToggle = "";
  toggle.setAttribute("aria-haspopup", "menu");
  toggle.setAttribute("aria-expanded", "false");
  toggle.innerHTML = `
    <span class="modal-tab-dropdown-label">Documents</span>
    <span class="modal-tab-dropdown-chevron" aria-hidden="true">▾</span>
  `;
  wrap.appendChild(toggle);

  const menu = document.createElement("div");
  menu.className = "modal-tab-dropdown-menu";
  menu.setAttribute("role", "menu");
  menu.hidden = true;
  for (const d of docs) {
    // Row is a div, not a button — buttons can't reliably nest other
    // buttons, and we want both "click to switch" and a separate "× to
    // remove" affordance inside the same row.
    const row = document.createElement("div");
    row.className = "modal-tab-dropdown-item";
    row.dataset.tab = d.id;
    row.setAttribute("role", "menuitem");
    // Strip the "lgtm:" prefix when handing the raw item id to the
    // remove button — the API takes LGTM's id, not periscope's tab id.
    const rawItemId = d.id.startsWith("lgtm:") ? d.id.slice(5) : d.id;
    row.innerHTML = `
      <span class="modal-tab-dropdown-item-label">${escapeHtml(d.label)}</span>
      <button type="button" class="modal-tab-dropdown-item-remove"
              data-remove-item="${escapeHtml(rawItemId)}"
              title="Remove from review (${escapeHtml(d.label)})"
              aria-label="Remove ${escapeHtml(d.label)}">×</button>
    `;
    menu.appendChild(row);
  }
  wrap.appendChild(menu);
  return wrap;
}

function applyActiveState(currentTabId, spec) {
  // Plain tabs (Terminal, Diff, Start) — highlight if data-tab matches.
  for (const btn of modalTabs.querySelectorAll(".modal-tab:not(.modal-tab-dropdown-toggle)")) {
    const active = btn.dataset.tab === currentTabId;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  }
  // Dropdown toggle's active state and label only kick in when the
  // current doc is reachable ONLY through the dropdown. When the active
  // doc is pinned as a mounted tab, that tab carries the active state
  // and the dropdown stays neutral ("Documents").
  const toggle = modalTabs.querySelector(".modal-tab-dropdown-toggle");
  if (toggle) {
    const activeDoc = spec.docs.find(d => d.id === currentTabId);
    const activeIsMounted = !!spec.mounted.find(d => d.id === currentTabId);
    const showActive = !!activeDoc && !activeIsMounted;
    toggle.classList.toggle("is-active", showActive);
    toggle.setAttribute("aria-selected", showActive ? "true" : "false");
    const labelEl = toggle.querySelector(".modal-tab-dropdown-label");
    if (labelEl) labelEl.textContent = showActive ? activeDoc.label : "Documents";
    // Menu still highlights the active row regardless of pinning, so
    // when the user opens the dropdown they can see what's current.
    for (const item of modalTabs.querySelectorAll(".modal-tab-dropdown-item")) {
      item.classList.toggle("is-active", item.dataset.tab === currentTabId);
    }
  }
}

function performAutoSwitch(spec) {
  // Two triggers:
  //   1. openModal({tab: "review"}) — switch to the first LGTM tab as
  //      soon as items arrive. Prefer Diff; fall back to first doc.
  //   2. The active tab disappeared (deregister, doc removed) — fall
  //      back to Terminal.
  const validIds = new Set(["terminal"]);
  if (spec.showDiff) validIds.add("lgtm:diff");
  if (spec.showWalkthrough) validIds.add("lgtm:walkthrough");
  for (const d of spec.docs) validIds.add(d.id);
  if (spec.showStart) validIds.add("lgtm-start");

  const current = modal.dataset.tabId || "terminal";
  let target = current;
  // Highest-priority auto-switch: a doc the user just added via
  // Cmd+click. Lands as soon as it shows up in the items list.
  if (pendingTabIdAfterAdd && validIds.has(pendingTabIdAfterAdd)) {
    target = pendingTabIdAfterAdd;
    pendingTabIdAfterAdd = null;
  } else if (pendingReviewOpen) {
    const first = spec.showDiff ? "lgtm:diff" : (spec.docs[0]?.id ?? null);
    if (first) {
      target = first;
      pendingReviewOpen = false;
    }
  }
  if (!validIds.has(target)) {
    // If the active tab disappeared (e.g. a doc was just removed),
    // prefer Diff over Terminal so the user stays in review context.
    target = spec.showDiff ? "lgtm:diff" : "terminal";
  }
  if (target !== current) setActiveTab(target);
}

// ── Dropdown menu open/close ────────────────────────────────────────
// Tracks open state at module scope. ensureStripStructure calls
// closeDropdownMenu before rebuilding so the listener doesn't leak.

let docsDropdownOpen = false;

function toggleDropdownMenu() {
  if (docsDropdownOpen) closeDropdownMenu();
  else openDropdownMenu();
}

function openDropdownMenu() {
  const menu = modalTabs?.querySelector(".modal-tab-dropdown-menu");
  const toggle = modalTabs?.querySelector(".modal-tab-dropdown-toggle");
  if (!menu || !toggle) return;
  menu.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
  docsDropdownOpen = true;
  // ESC closes the menu first (then a second ESC closes the modal).
  pushEscape(closeDropdownMenu);
  // Outside-click closes the menu. Use a microtask so the click that
  // opened it doesn't immediately count as "outside."
  setTimeout(() => document.addEventListener("click", onOutsideDropdownClick), 0);
}

function closeDropdownMenu() {
  const menu = modalTabs?.querySelector(".modal-tab-dropdown-menu");
  const toggle = modalTabs?.querySelector(".modal-tab-dropdown-toggle");
  if (menu) menu.hidden = true;
  if (toggle) toggle.setAttribute("aria-expanded", "false");
  if (docsDropdownOpen) {
    popEscape(closeDropdownMenu);
    document.removeEventListener("click", onOutsideDropdownClick);
  }
  docsDropdownOpen = false;
}

function onOutsideDropdownClick(e) {
  if (e.target.closest(".modal-tab-dropdown")) return;
  closeDropdownMenu();
}

// Called from terminal.js when the user Cmd+clicks a .md path in the
// xterm view. We POST the path to /api/lgtm/add-doc with the pane's
// cwd; on success we stash the returned tab_id so the next poll's
// auto-switch lands on the new tab.
export async function addLgtmDocFromTerminal(rawPath) {
  if (!rawPath) return;
  const cwd = lastPaneData?.cwd_raw;
  if (!cwd) {
    console.warn("add doc: no cwd for current pane");
    return;
  }
  // Strip a trailing :line suffix that path links sometimes carry —
  // LGTM doesn't anchor docs to line numbers, and the file path alone
  // is what /api/lgtm/add-doc validates.
  const path = rawPath.replace(/:\d+$/, "");
  try {
    const res = await fetch("/api/lgtm/add-doc", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cwd, path }),
    });
    const payload = await res.json();
    if (!payload.ok) {
      console.warn(`add doc: ${payload.error}`);
      writeTerminalLine(`\r\n\x1b[31m[periscope: add doc failed — ${payload.error}]\x1b[0m`);
      return;
    }
    pendingTabIdAfterAdd = payload.tab_id;
    refreshModalHeader();
  } catch (e) {
    console.warn("add doc error:", e);
  }
}

async function refreshModalHeader() {
  // /api/pane is now used only for parsed status fields (branch, PR, recap,
  // spinner). The terminal content itself streams live via WebSocket and
  // doesn't need this poll. lines=80 is enough buffer for the parser to find
  // the status block and most recent recap.
  if (!state.activeTarget) return;
  if (state.modalRenaming) return;  // don't clobber the in-flight rename input
  if (state.modalAutoRenaming) return;  // keep the ✨ button in its busy state
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
  // The auto-rename ✨ button sits immediately right of the name — the
  // operation it triggers (ask Claude to rename) is name-specific, so the
  // affordance lives next to its target. Click is delegated from modalTitle
  // so we don't have to re-bind it after every poll rebuilds this innerHTML.
  const name = data.name || data.target;
  const busy = state.modalAutoRenaming ? " busy" : "";
  const titleParts = [
    `<span class="modal-name">${escapeHtml(name)}</span>`,
    `<button type="button" class="modal-title-rename${busy}" data-action="auto-rename" title="ask Claude to rename this window">✨</button>`,
  ];
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
  renderTabStrip(data);
  // Render the review pane only when actually viewing it; saves us from
  // churning DOM on every poll for users sitting on the Terminal tab.
  if (modal.dataset.tab === "review") renderReviewPane(data);
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
  const tabId = modal.dataset.tabId || "terminal";
  if (tabId === "terminal") return;

  if (tabId === "lgtm-start") {
    renderStartReview(data);
    return;
  }

  if (tabId === "lgtm:walkthrough") {
    renderLgtmWalkthrough(data);
    return;
  }

  if (tabId.startsWith("lgtm:")) {
    renderLgtmItem(data, tabId.slice(5));
    return;
  }
}

function renderLgtmWalkthrough(data) {
  const lgtm = data.lgtm;
  if (!lgtm?.slug || !lgtm?.url) return;
  // Walkthrough is a separate LGTM view, not an item — load with
  // ?view=walkthrough so LGTM's main.tsx boots into walkthrough mode
  // before first paint.
  const baseUrl = rewriteLgtmHost(lgtm.url);
  const url = `${baseUrl}?embedded=1&view=walkthrough`;
  if (mountedTabId === "lgtm:walkthrough") return;

  let iframe = modalReviewContent.querySelector("iframe");
  if (!iframe) {
    modalReviewContent.innerHTML = "";
    iframe = document.createElement("iframe");
    iframe.title = "LGTM walkthrough";
    iframe.referrerPolicy = "no-referrer";
    modalReviewContent.appendChild(iframe);
  }
  iframe.src = url;
  mountedTabId = "lgtm:walkthrough";
}

function renderLgtmItem(data, itemId) {
  const lgtm = data.lgtm;
  if (!lgtm?.slug || !lgtm?.url) {
    // The tab disappeared (session deregistered) but renderTabStrip
    // hasn't run yet for this poll. Bail; the next render will fall
    // back to terminal.
    return;
  }
  // Build the embedded iframe URL. Rewrite the host to match the parent
  // so localhost/127.0.0.1/LAN-IP all converge on a single hostname pair.
  const baseUrl = rewriteLgtmHost(lgtm.url);
  const url = `${baseUrl}?embedded=1&item=${encodeURIComponent(itemId)}`;
  const tabKey = `lgtm:${itemId}`;
  if (mountedTabId === tabKey) return;

  let iframe = modalReviewContent.querySelector("iframe");
  if (!iframe) {
    modalReviewContent.innerHTML = "";
    iframe = document.createElement("iframe");
    iframe.title = "LGTM review";
    // No loading=lazy: the iframe lives inside a tabbed pane that's
    // display:none while on the Terminal tab. Some browsers refuse to
    // load lazy iframes whose ancestors aren't visible.
    iframe.referrerPolicy = "no-referrer";
    modalReviewContent.appendChild(iframe);
  }
  iframe.src = url;
  mountedTabId = tabKey;
}

function renderStartReview(data) {
  if (mountedTabId === "lgtm-start") return;
  mountedTabId = "lgtm-start";
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
      // Queue an auto-switch to the first LGTM tab as soon as items
      // arrive in the next refresh.
      pendingReviewOpen = true;
      mountedTabId = null;
      refreshModalHeader();
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Start review";
      err.textContent = `Could not start review: ${e.message}`;
      err.hidden = false;
    }
  });
}

// ── Sidebar: Linked (PR + Linear) + Activity. ────────────────────────
// Data rides on the existing 1.5s /api/pane poll — no extra request.

function alertDotColor(kind) {
  if (kind === "need_human") return "var(--s-danger)";
  if (kind === "done") return "var(--s-success)";
  return "var(--fg-3)";
}

// One row in the merged Activity stream — either a git/CI event or an
// alert a session raised over the channel. Alerts wrap (messages run
// long) and read brighter; git events stay terse and single-line.
function activityRow(e) {
  if (e.src === "alert") {
    return `
      <li class="timeline-row timeline-row-alert" data-kind="${escapeHtml(e.kind)}">
        <span class="timeline-dot" style="background:${alertDotColor(e.kind)}"></span>
        <div class="timeline-body">
          <div class="timeline-text timeline-text-wrap">${escapeHtml(e.text)}</div>
          <div class="timeline-when">claude · ${escapeHtml(e.kind)} · ${escapeHtml(relTime(e.at))} ago</div>
        </div>
      </li>
    `;
  }
  return `
    <li class="timeline-row" data-kind="${escapeHtml(e.kind)}">
      <span class="timeline-dot" style="background:${timelineColor(e.kind, e.state)}"></span>
      <div class="timeline-body">
        <div class="timeline-text">${escapeHtml(e.text)}</div>
        <div class="timeline-when">${escapeHtml(timelineLabel(e.kind, e.state))} · ${escapeHtml(relTime(e.at))} ago</div>
      </div>
    </li>
  `;
}

// Merged chronological stream: git/CI activity + channel alerts. An
// unresolved need_human alert (the latest alert, when nothing newer has
// superseded it) is pinned above the stream so a blocked pane stays loud
// even after older events scroll it out of view.
function renderActivitySection(data) {
  const alerts = (data.channel_alerts || []).map((r) => ({
    src: "alert", at: r.ts, kind: r.kind || "info", text: r.message || "",
  }));
  const events = (data.activity || []).map((e) => ({
    src: "git", at: e.at, kind: e.kind, state: e.state, text: e.text || "",
  }));

  const latestAlert = alerts.reduce(
    (best, a) => (best && best.at >= a.at ? best : a), null);
  const pinned =
    latestAlert && latestAlert.kind === "need_human" ? latestAlert : null;

  const stream = [...alerts, ...events]
    .filter((e) => e !== pinned)
    .sort((a, b) => (b.at || 0) - (a.at || 0));

  let html = "";
  if (pinned) {
    html += `
      <div class="activity-pinned">
        <div class="activity-pinned-label">needs you · ${escapeHtml(relTime(pinned.at))} ago</div>
        <div class="activity-pinned-text">${escapeHtml(pinned.text)}</div>
      </div>
    `;
  }
  if (stream.length) {
    html += `<ol class="timeline activity-stream">${stream.map(activityRow).join("")}</ol>`;
  } else if (!pinned) {
    html += `<div class="timeline-empty">no recent activity</div>`;
  }
  return html;
}

function renderModalSidebar(data) {
  if (!modalSide) return;
  // The 1.5s header poll re-renders the sidebar wholesale. If focus is inside
  // the sidebar (notes textarea, tag input), rebuilding the innerHTML drops
  // focus and clobbers in-flight typing. Skip this tick — the next poll after
  // blur will catch up.
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
      ${renderActivitySection(data)}
    </section>
  `;
  wireNotesEditor(data);
  wireLinkAskButtons(data);

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
    // No add-from-UI flow; clicking pings Claude (via the MCP channel) to call
    // its `link_pr` tool. Disabled when no Claude is attached on this pane.
    const attached = !!data.channel_attached;
    const title = attached
      ? "Ask Claude to link the PR for this pane (via link_pr MCP tool)"
      : "Channel not attached. Respawn Claude via + claude.";
    return `<button class="modal-side-link-btn" type="button" data-link-ask="pr"${attached ? "" : " disabled"} title="${title}">+ link pull request</button>`;
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
  // unset, fall back to a "+ link" button that pings Claude to call its
  // `link_linear` tool — there's no UI flow for the user to search/add a
  // ticket directly.
  if (data.linked_linear) {
    const id = escapeHtml(data.linked_linear);
    const url = `https://linear.app/issue/${id}`;
    // title/status are Claude-supplied (link_linear MCP tool) and optional.
    // Fall back to a generic label when no title was passed.
    const title = data.linked_linear_title
      ? escapeHtml(data.linked_linear_title)
      : "linear ticket";
    const statusPill = data.linked_linear_status
      ? `<div class="pr-meta"><span class="pr-mini">${escapeHtml(data.linked_linear_status)}</span></div>`
      : "";
    return `
      <div class="modal-card-inset">
        <div class="pr-head">
          <a class="pr-num" href="${url}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Linear ticket ${id} (linked by claude)">${id}</a>
          <span class="pr-title" title="${title}">${title}</span>
        </div>
        ${statusPill}
      </div>
    `;
  }
  const attached = !!data.channel_attached;
  const title = attached
    ? "Ask Claude to link a Linear ticket for this pane (via link_linear MCP tool)"
    : "Channel not attached. Respawn Claude via + claude.";
  return `<button class="modal-side-link-btn" type="button" data-link-ask="linear"${attached ? "" : " disabled"} title="${title}">+ link Linear ticket</button>`;
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

// Canned prompts sent to Claude when the user clicks the sidebar's "+ link …"
// buttons. The pane has no add-from-UI flow for PR or Linear — instead, the
// click pings the attached Claude and asks it to call the relevant MCP tool.
const LINK_ASK_PROMPTS = {
  pr: "Please link the PR you're working on for this pane using the `link_pr` MCP tool. If there isn't one, ignore this.",
  linear: "Please link the relevant Linear ticket for this pane using the `link_linear` MCP tool. If there isn't one, ignore this.",
};

function wireLinkAskButtons(data) {
  if (!modalSide) return;
  const btns = modalSide.querySelectorAll("button[data-link-ask]");
  btns.forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      const kind = btn.dataset.linkAsk;
      const content = LINK_ASK_PROMPTS[kind];
      if (!content || !data.pane_id) return;
      // Visual ack — next poll re-renders the sidebar wholesale, which will
      // either show the new card (Claude linked it) or restore the button.
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = "asked Claude…";
      try {
        await fetch(`/api/channel/push?pane=${encodeURIComponent(data.pane_id)}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ content }),
        });
      } catch {
        btn.disabled = false;
        btn.textContent = orig;
      }
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

async function handleModalAutoRename(btn) {
  if (!state.activeTarget || state.modalAutoRenaming) return;
  state.modalAutoRenaming = true;
  // The header poll is paused while this flag is set (see refreshModalHeader),
  // so the button keeps its busy class until we restore.
  btn.classList.add("busy");
  btn.disabled = true;
  try {
    const data = await apiCall(
      "auto-rename window",
      `/api/auto-rename-window?${targetQuery(state.activeTarget)}`,
      { method: "POST" }
    );
    if (data) poll();  // refresh cards; modal header refreshes in the finally
  } finally {
    state.modalAutoRenaming = false;
    refreshModalHeader();  // rebuilds the title; new button is fresh
  }
}

export function initModal() {
  modalClose.addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeModal();
  });

  // Double-click the window name in the modal header to rename it. The
  // `.modal-name` span is rebuilt every poll by updateModalHeader, so delegate
  // from the persistent <h2> instead of attaching per-render. Same logic
  // applies to the ✨ auto-rename button rendered into the title innerHTML.
  modalTitle.addEventListener("dblclick", (e) => {
    if (!e.target.closest(".modal-name")) return;
    e.stopPropagation();
    startModalRename();
  });
  modalTitle.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action='auto-rename']");
    if (!btn) return;
    e.stopPropagation();
    handleModalAutoRename(btn);
  });

  // Forwarded Escape from the LGTM iframe. When focus is inside the
  // iframe the keystroke never bubbles to us — LGTM's embedded shim
  // postMessages on Escape (excluding text-input focus) so the modal
  // close binding still works while reviewing.
  window.addEventListener("message", (e) => {
    if (e.data?.type === "lgtm-embedded-escape" && !modal.classList.contains("hidden")) {
      closeModal();
    }
  });

  if (modalTabs) {
    modalTabs.addEventListener("click", (e) => {
      // Documents dropdown toggle: open/close menu, don't change tab.
      const toggle = e.target.closest("[data-doc-dropdown-toggle]");
      if (toggle) {
        e.stopPropagation();
        toggleDropdownMenu();
        return;
      }
      // Unpin (×) on a mounted tab — drops the doc from the top-level
      // strip but stays on it (still reachable via dropdown).
      const unmount = e.target.closest("[data-unmount-doc]");
      if (unmount) {
        e.stopPropagation();
        if (unmountDoc(unmount.dataset.unmountDoc) && lastPaneData) {
          renderTabStrip(lastPaneData);
        }
        return;
      }
      // Remove (×) button inside a dropdown row — strip the item from
      // LGTM via the periscope proxy. Stop here so the row's tab-switch
      // doesn't also fire.
      const remove = e.target.closest("[data-remove-item]");
      if (remove) {
        e.stopPropagation();
        const slug = lastPaneData?.lgtm?.slug;
        const itemId = remove.dataset.removeItem;
        if (!slug || !itemId) return;
        remove.disabled = true;
        fetch(
          `/api/lgtm/items?slug=${encodeURIComponent(slug)}&item=${encodeURIComponent(itemId)}`,
          { method: "DELETE" },
        )
          .then(r => r.json())
          .then(p => {
            if (!p.ok) console.warn("lgtm remove failed:", p.error);
            // The next /api/pane poll will pick up the new items list
            // via the SSE-driven cache refresh and rebuild the strip.
            refreshModalHeader();
          })
          .catch(err => {
            console.warn("lgtm remove failed:", err);
            remove.disabled = false;
          });
        return;
      }
      // Documents dropdown menu item: switch to that doc + auto-pin
      // it as a top-level tab for quick re-access, close menu.
      const item = e.target.closest(".modal-tab-dropdown-item");
      if (item) {
        e.stopPropagation();
        mountDoc(item.dataset.tab);
        setActiveTab(item.dataset.tab);
        closeDropdownMenu();
        if (lastPaneData) {
          renderTabStrip(lastPaneData);
          renderReviewPane(lastPaneData);
        }
        refreshModalHeader();
        return;
      }
      // Plain tab click (Terminal, Diff, + Start review).
      const btn = e.target.closest(".modal-tab");
      if (!btn) return;
      e.stopPropagation();
      setActiveTab(btn.dataset.tab);
      if (btn.dataset.tab !== "terminal") {
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
