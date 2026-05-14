// /history route — single-page conversation history search.
//
// Layout: route header / search row / filter chips / body (results | detail) /
// footer. Single mutable state object; render() does a full innerHTML rebuild
// of #history-root and restores input focus + caret. Event delegation: one
// click handler on the root, one keydown on the document.

import { escapeHtml, relTime, apiCall } from './util.js';

const RESUME_SESSION = "resumes";  // tmux session host for resume windows
const STORAGE_KEY = "periscope:history:filter";
const DEBOUNCE_MS = 200;

const SINCE_LABELS = {
  today: "today",
  week: "this week",
  month: "this month",
  all: "all time",
};

// Module state — mutated in place; render() reads it.
const S = {
  query: "",
  filter: loadFilter(),
  results: [],
  selectedId: null,
  detail: null,
  detailCache: new Map(),    // session_id → detail payload
  stats: null,
  loading: false,
  searchToken: 0,            // race-protect overlapping fetches
};

const root = document.getElementById("history-root");

// ── Persistence ─────────────────────────────────────────

function loadFilter() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
    return {
      project: typeof raw.project === "string" ? raw.project : "all",
      since: ["today", "week", "month", "all"].includes(raw.since) ? raw.since : "all",
      includeTrivial: !!raw.includeTrivial,
      rerank: !!raw.rerank,
    };
  } catch {
    return { project: "all", since: "all", includeTrivial: false, rerank: false };
  }
}

function saveFilter() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(S.filter));
}

// ── Formatters ──────────────────────────────────────────

function fmtDuration(s) {
  if (s == null) return "";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${h}h ${mm}m` : `${h}h`;
}

function fmtBytes(n) {
  if (!n) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function fmtDateTime(epochSec) {
  if (!epochSec) return "";
  const d = new Date(epochSec * 1000);
  // Match the design: "May 13, 9:14 AM"
  const opts = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
  return d.toLocaleString(undefined, opts);
}

function fmtTime(epochMs) {
  if (!epochMs) return "";
  const d = new Date(epochMs);
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function shortenPath(p) {
  if (!p) return "";
  // Compact `~/dev/<name>` to `<name>`; keep other paths whole.
  return p.replace(/^~?\/(?:Users\/[^/]+\/)?dev\//, "");
}

function tagClass(t) {
  if (t === "shipped") return "is-shipped";
  if (t === "bug") return "is-bug";
  if (t === "design") return "is-design";
  if (t === "unfinished") return "is-unfinished";
  return "";
}

// Wrap query-term matches in <mark>. Case-insensitive, word-boundary-free
// so partial matches highlight. Returns escaped HTML.
function highlight(text) {
  const escaped = escapeHtml(text || "");
  const q = S.query.trim();
  if (!q) return escaped;
  const terms = q.split(/\s+/)
    .filter(Boolean)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return escaped;
  const re = new RegExp(`(${terms.join("|")})`, "gi");
  return escaped.replace(re, "<mark>$1</mark>");
}

// ── Render helpers ──────────────────────────────────────

function renderHead() {
  const st = S.stats;
  const indexed = st ? `<b>${st.total.toLocaleString()}</b> sessions indexed` : `loading…`;
  const synced = st && st.last_scan_at
    ? `synced ${relTime(st.last_scan_at)} ago`
    : (st ? "no full scan yet" : "");
  const projects = st ? `${st.projects} project${st.projects === 1 ? "" : "s"}` : "";
  const parts = [
    `<span class="ph-row"><span class="ph-idx-pulse"></span><span>${indexed}</span></span>`,
  ];
  if (synced) parts.push(`<span style="color:var(--fg-4)">·</span>`, `<span>${escapeHtml(synced)}</span>`);
  if (projects) parts.push(`<span style="color:var(--fg-4)">·</span>`, `<span>${escapeHtml(projects)}</span>`);
  return `
    <div class="ph-head">
      <button class="ph-back" data-action="back">← dashboard</button>
      <div class="ph-route-title"><span class="ph-route-slash">/</span>history</div>
      <div class="ph-head-meta">${parts.join(" ")}</div>
    </div>
  `;
}

function renderSearchRow() {
  const count = S.results.length;
  const countLabel = count === 1 ? "1 result" : `${count} results`;
  return `
    <div class="ph-search-row">
      <div class="ph-search-input">
        <span class="glyph">🔍</span>
        <input id="ph-search-input" type="text" placeholder="search summaries, prompts, files, commands…"
               autocomplete="off" spellcheck="false"
               value="${escapeHtml(S.query)}">
        <span class="ph-result-count">${countLabel}</span>
        <span class="ph-kbd-hint"><span class="ps-kbd">⌘</span><span class="ps-kbd">/</span></span>
      </div>
    </div>
  `;
}

function renderFilters() {
  // Project options: the static "all" + every project_path we've seen in
  // results. Cheap and useful; on first paint (no results yet) only "all"
  // shows. Once Tom searches the chips populate dynamically.
  const seenProjects = new Set();
  for (const r of S.results) {
    if (r.project_path) seenProjects.add(r.project_path);
  }
  const projectChips = ["all", ...[...seenProjects].sort()];
  const projectHtml = projectChips.map((p) => {
    const label = p === "all" ? "all projects" : escapeHtml(shortenPath(p));
    const active = S.filter.project === p ? " is-active" : "";
    return `<button class="ph-fchip${active}" data-action="set-project" data-value="${escapeHtml(p)}">${label}</button>`;
  }).join("");

  const sinceHtml = ["today", "week", "month", "all"].map((v) => {
    const active = S.filter.since === v ? " is-active" : "";
    return `<button class="ph-fchip${active}" data-action="set-since" data-value="${v}">${SINCE_LABELS[v]}</button>`;
  }).join("");

  const trivialActive = S.filter.includeTrivial ? " is-active" : "";
  const rerankActive = S.filter.rerank ? " is-active" : "";

  return `
    <div class="ph-filters">
      ${projectHtml}
      <div class="ph-fchip-sep"></div>
      ${sinceHtml}
      <div class="ph-fchip-sep"></div>
      <button class="ph-fchip${trivialActive}" data-action="toggle-trivial">
        <span class="ph-fchip-glyph">${S.filter.includeTrivial ? "☑" : "☐"}</span> trivial
      </button>
      <button class="ph-fchip${rerankActive}" data-action="toggle-rerank">
        <span class="ph-fchip-glyph">✦</span> rerank
        <span class="ph-rerank-cost">~$0.001</span>
      </button>
    </div>
  `;
}

function renderSectionHead() {
  if (!S.query.trim() && S.results.length) {
    return `<div class="ph-section-head">Recent</div>`;
  }
  if (S.filter.rerank) {
    return `<div class="ph-section-head">✦ Reranked by relevance</div>`;
  }
  return `<div class="ph-section-head">Sorted by BM25 rank</div>`;
}

function renderResultRow(r) {
  const selected = r.session_id === S.selectedId ? " is-selected" : "";
  const trivial = r.trivial ? " is-trivial" : "";
  const summary = r.summary || r.first_user_msg || "(no summary)";
  // BM25 isn't meaningful as an absolute number to the user, but the
  // monotonic-decreasing bar gives a visual rank cue. We render rank
  // (1..N) since BM25's raw score isn't on the API surface.
  const barPct = Math.max(8, 100 - (r.rank - 1) * 9);
  const projLabel = escapeHtml(shortenPath(r.project_path || ""));
  const meta = [
    `<span class="ph-meta-proj">${projLabel}</span>`,
    `<span class="ph-meta-dot">·</span>`,
    `<span class="ph-meta-branch">${escapeHtml(r.branch || "—")}</span>`,
    `<span class="ph-meta-dot">·</span>`,
    `<span>${r.user_msg_count} msgs</span>`,
    `<span class="ph-meta-dot">·</span>`,
    `<span>${fmtDuration(r.duration_s)}</span>`,
    `<span class="ph-meta-dot">·</span>`,
    `<span>${escapeHtml(relTime(r.started_at) || "")} ago</span>`,
  ];
  if (r.trivial) meta.push(`<span class="ph-trivial-pill" title="heuristic summary only">trivial</span>`);
  if (r.was_interrupted) meta.push(`<span class="ph-interrupted-pill">interrupted</span>`);

  const tags = (r.tags || []).map((t) =>
    `<span class="ph-tag ${tagClass(t)}">${escapeHtml(t)}</span>`
  ).join("");
  const tagsRow = tags ? `<div class="ph-result-tags">${tags}</div>` : "";

  const rerankRow = (S.filter.rerank && r.rerank_reason)
    ? `<div class="ph-result-rerank"><span class="ph-rerank-mark">✦</span><span>${escapeHtml(r.rerank_reason)}</span></div>`
    : "";

  return `
    <div class="ph-result${selected}${trivial}" data-action="select" data-id="${escapeHtml(r.session_id)}">
      <div class="ph-result-head">
        <div class="ph-result-summary">${highlight(summary)}</div>
        <div class="ph-result-rank">
          <span class="ph-bm25">
            <span class="ph-bm25-bar"><i style="width:${barPct}%"></i></span>
            #${r.rank}
          </span>
        </div>
      </div>
      <div class="ph-result-meta">${meta.join(" ")}</div>
      ${tagsRow}
      ${rerankRow}
    </div>
  `;
}

function renderResults() {
  if (S.loading && !S.results.length) {
    return `<div class="ph-empty"><div class="ph-empty-sub">searching…</div></div>`;
  }
  if (!S.results.length) {
    if (S.query.trim()) {
      return `
        <div class="ph-empty">
          <div class="ph-empty-title">No matches</div>
          <div class="ph-empty-sub">Try a broader query, toggle <b>trivial</b>, or expand the time range.</div>
        </div>
      `;
    }
    return `
      <div class="ph-empty">
        <div class="ph-empty-title">No indexed sessions yet</div>
        <div class="ph-empty-sub">Run <code>python -m history backfill</code> to build the index.</div>
      </div>
    `;
  }
  return renderSectionHead() + S.results.map(renderResultRow).join("");
}

function renderDetailEmpty(msg) {
  return `
    <div class="ph-empty">
      <div class="ph-empty-title">${escapeHtml(msg)}</div>
    </div>
  `;
}

function renderResumeButton(r) {
  const blocked = r.is_live || r.is_resuming;
  const title = r.is_live
    ? "Session is currently live (last modified < 60s). Wait or focus the running window."
    : r.is_resuming
      ? "Another resume in progress for this session."
      : `claude --resume in a new tmux window (session: ${RESUME_SESSION})`;
  const cls = blocked ? "ph-resume is-disabled" : "ph-resume";
  const dis = blocked ? " disabled aria-disabled=\"true\"" : "";
  return `<button class="${cls}" data-action="resume" data-id="${escapeHtml(r.session_id)}" title="${escapeHtml(title)}"${dis}>↻ resume</button>`;
}

function renderDetailHead(r) {
  const proj = escapeHtml(shortenPath(r.project_path || ""));
  const startedDate = r.started_at ? fmtDateTime(r.started_at) : "";
  const dur = fmtDuration(r.duration_s);
  const idShort = r.session_id ? `${r.session_id.slice(0, 10)}…` : "";

  const files = (r.files_touched || []).slice(0, 8).map((f) => {
    const idx = f.lastIndexOf("/");
    const dir = idx >= 0 ? f.slice(0, idx + 1) : "";
    const name = idx >= 0 ? f.slice(idx + 1) : f;
    return `<span class="ph-file"><span class="ph-file-dir">${escapeHtml(dir)}</span>${escapeHtml(name)}</span>`;
  }).join("");
  const filesOverflow = (r.files_touched || []).length > 8
    ? `<span class="ph-file" style="color:var(--fg-4)">+${r.files_touched.length - 8} more</span>`
    : "";
  const filesRow = files ? `<div class="ph-detail-files">${files}${filesOverflow}</div>` : "";

  const cmds = (r.notable_cmds || []).map((c) =>
    `<span class="ph-cmd">${escapeHtml(c)}</span>`
  ).join("");
  const cmdsRow = cmds ? `<div class="ph-detail-cmds">${cmds}</div>` : "";

  const interruptedPill = r.was_interrupted
    ? `<span class="dm-pill is-interrupted">interrupted</span>` : "";
  const livePill = r.is_live
    ? `<span class="dm-pill is-resuming">live · cannot resume</span>` : "";

  return `
    <div class="ph-detail-head">
      <div class="ph-detail-title-row">
        <div class="ph-detail-title">${highlight(r.summary || r.first_user_msg || "(no summary)")}</div>
        <div class="ph-detail-actions">
          ${renderResumeButton(r)}
          <button class="modal-actions-btn ph-copy" data-action="copy-id" data-id="${escapeHtml(r.session_id)}">copy id</button>
        </div>
      </div>
      <div class="ph-detail-meta">
        <span class="dm-pill" title="${escapeHtml(r.session_id || "")}">${escapeHtml(idShort)}</span>
        <span><b>${proj}</b></span>
        <span style="color:var(--fg-4)">·</span>
        <span>${escapeHtml(r.branch || "—")}</span>
        <span style="color:var(--fg-4)">·</span>
        <span>${escapeHtml(startedDate)} · ${escapeHtml(dur)}</span>
        <span style="color:var(--fg-4)">·</span>
        <span><b>${r.user_msg_count || 0}</b> user · <b>${r.asst_msg_count || 0}</b> assistant · <b>${r.tool_use_count || 0}</b> tools</span>
        ${interruptedPill}
        ${livePill}
      </div>
      ${filesRow}
      ${cmdsRow}
    </div>
  `;
}

function renderMsg(m) {
  const at = m.ts_ms ? fmtTime(m.ts_ms) : "";
  if (m.role === "tool" || (m.role === "assistant" && Array.isArray(m.tool_uses) && m.tool_uses.length)) {
    // Assistant turn that includes tool_use blocks: render the tool calls
    // inline below the text. (The API returns tool calls attached to the
    // assistant message, not as separate "tool" role rows.)
    const toolsHtml = (m.tool_uses || []).map((t) => `
      <div class="ph-msg role-tool">
        <div class="ph-msg-gutter"><span class="ph-role-label">tool</span></div>
        <div class="ph-msg-body">
          <span class="ph-tool-head">${escapeHtml(t.name || "tool")} <span class="ph-tool-sub">${escapeHtml((t.input && JSON.stringify(t.input).slice(0, 80)) || "")}</span></span>
        </div>
      </div>
    `).join("");
    const text = m.text
      ? `<div class="ph-msg role-assistant">
           <div class="ph-msg-gutter"><span class="ph-role-label">claude</span>${at}</div>
           <div class="ph-msg-body">${highlight(m.text)}</div>
         </div>`
      : "";
    return text + toolsHtml;
  }
  const roleLabel = m.role === "user" ? "you" : m.role === "assistant" ? "claude" : escapeHtml(m.role);
  return `
    <div class="ph-msg role-${escapeHtml(m.role)}">
      <div class="ph-msg-gutter">
        <span class="ph-role-label">${roleLabel}</span>${at}
      </div>
      <div class="ph-msg-body">${highlight(m.text || "")}</div>
    </div>
  `;
}

function renderDetail() {
  const sel = S.results.find((r) => r.session_id === S.selectedId);
  if (!sel) return renderDetailEmpty("Select a result");
  // Detail body lazy-loads; fall back to the search row's metadata while
  // waiting. The head doesn't need detail data — search results already
  // carry everything it shows.
  const detail = S.detail && S.detail.session_id === sel.session_id ? S.detail : sel;
  const messagesHtml = detail.messages && detail.messages.length
    ? detail.messages.map(renderMsg).join("")
    : (S.detail
        ? `<div class="ph-empty"><div class="ph-empty-sub">(no parsed messages)</div></div>`
        : `<div class="ph-empty"><div class="ph-empty-sub">loading messages…</div></div>`);
  return `
    ${renderDetailHead(detail)}
    <div class="ph-detail-body">${messagesHtml}</div>
  `;
}

function renderFoot() {
  const meta = S.stats
    ? `last indexer run · ${S.stats.last_scan_at ? relTime(S.stats.last_scan_at) + " ago" : "never"} · ${S.stats.total.toLocaleString()} sessions · ${fmtBytes(S.stats.db_bytes)} index`
    : "";
  return `
    <div class="ph-foot">
      <span><span class="ps-kbd">↑</span><span class="ps-kbd">↓</span> navigate</span>
      <span><span class="ps-kbd">↵</span> focus detail</span>
      <span><span class="ps-kbd">⌘</span><span class="ps-kbd">↵</span> resume</span>
      <span><span class="ps-kbd">esc</span> back</span>
      <span class="ph-foot-meta">${escapeHtml(meta)}</span>
    </div>
  `;
}

function render() {
  // Preserve search-input focus + caret across the full innerHTML rebuild.
  // The input is the only stateful focus surface; everything else is purely
  // derived from S.
  const active = document.activeElement;
  const wasInSearch = active && active.id === "ph-search-input";
  const caret = wasInSearch
    ? { start: active.selectionStart, end: active.selectionEnd }
    : null;

  root.innerHTML = `
    ${renderHead()}
    ${renderSearchRow()}
    ${renderFilters()}
    <div class="ph-body">
      <div class="ph-results">${renderResults()}</div>
      <div class="ph-detail">${renderDetail()}</div>
    </div>
    ${renderFoot()}
  `;

  if (wasInSearch) {
    const input = document.getElementById("ph-search-input");
    if (input) {
      input.focus();
      try { input.setSelectionRange(caret.start, caret.end); } catch {}
    }
  }
}

// ── Fetch ───────────────────────────────────────────────

function sinceToEpoch(since) {
  if (since === "all") return null;
  const now = Math.floor(Date.now() / 1000);
  if (since === "today") return now - 86400;
  if (since === "week") return now - 7 * 86400;
  if (since === "month") return now - 30 * 86400;
  return null;
}

async function fetchSearch() {
  const token = ++S.searchToken;
  S.loading = true;
  const params = new URLSearchParams();
  params.set("q", S.query);
  if (S.filter.project && S.filter.project !== "all") params.set("project", S.filter.project);
  const since = sinceToEpoch(S.filter.since);
  if (since != null) params.set("since", String(since));
  if (S.filter.includeTrivial) params.set("include_trivial", "true");
  if (S.filter.rerank) params.set("rerank", "true");
  try {
    const res = await fetch(`/api/history/search?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (token !== S.searchToken) return;  // a newer query has superseded us
    S.results = data.results || [];
    // Preserve selection by session_id if still present, else first row.
    if (S.selectedId && !S.results.find((r) => r.session_id === S.selectedId)) {
      S.selectedId = S.results[0]?.session_id || null;
    } else if (!S.selectedId) {
      S.selectedId = S.results[0]?.session_id || null;
    }
    // Selection changed → fetch detail (cached if known).
    if (S.selectedId) ensureDetail(S.selectedId);
  } catch (e) {
    if (token !== S.searchToken) return;
    S.results = [];
    console.error("history search failed:", e);
  } finally {
    if (token === S.searchToken) {
      S.loading = false;
      render();
    }
  }
}

async function ensureDetail(sessionId) {
  if (!sessionId) {
    S.detail = null;
    return;
  }
  if (S.detailCache.has(sessionId)) {
    S.detail = S.detailCache.get(sessionId);
    return;
  }
  // Don't block render on the detail fetch; render with placeholder, then
  // re-render when it lands.
  S.detail = null;
  try {
    const res = await fetch(`/api/history/session/${encodeURIComponent(sessionId)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    S.detailCache.set(sessionId, data);
    // Only commit if the user is still on this row.
    if (S.selectedId === sessionId) {
      S.detail = data;
      render();
    }
  } catch (e) {
    console.error("history detail fetch failed:", e);
  }
}

async function fetchStats() {
  try {
    const res = await fetch("/api/history/stats");
    if (!res.ok) return;
    S.stats = await res.json();
    render();
  } catch (e) {
    console.error("history stats failed:", e);
  }
}

async function doResume(sessionId) {
  const params = new URLSearchParams();
  params.set("session", RESUME_SESSION);
  params.set("mode", "resume");
  params.set("resume_id", sessionId);
  const data = await apiCall("resume", `/api/window/new?${params.toString()}`, {
    method: "POST",
  });
  if (data) {
    // Optimistic: flip the row's is_resuming locally so the UI reflects the
    // guard immediately. Next search will re-fetch authoritative state.
    const row = S.results.find((r) => r.session_id === sessionId);
    if (row) row.is_resuming = true;
    render();
  }
}

// ── Event wiring ────────────────────────────────────────

let searchTimer = null;

function onRootClick(e) {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  const value = btn.dataset.value;
  const id = btn.dataset.id;

  if (action === "back") {
    window.location.href = "/";
    return;
  }
  if (action === "select") {
    if (S.selectedId === id) return;
    S.selectedId = id;
    S.detail = S.detailCache.get(id) || null;
    render();
    if (!S.detail) ensureDetail(id);
    return;
  }
  if (action === "set-project") {
    S.filter.project = value;
    saveFilter();
    fetchSearch();
    return;
  }
  if (action === "set-since") {
    S.filter.since = value;
    saveFilter();
    fetchSearch();
    return;
  }
  if (action === "toggle-trivial") {
    S.filter.includeTrivial = !S.filter.includeTrivial;
    saveFilter();
    fetchSearch();
    return;
  }
  if (action === "toggle-rerank") {
    S.filter.rerank = !S.filter.rerank;
    saveFilter();
    fetchSearch();
    return;
  }
  if (action === "resume") {
    doResume(id);
    return;
  }
  if (action === "copy-id") {
    navigator.clipboard?.writeText(id).then(() => {
      btn.textContent = "copied ✓";
      setTimeout(() => { btn.textContent = "copy id"; }, 1500);
    });
    return;
  }
}

function onRootInput(e) {
  if (e.target.id !== "ph-search-input") return;
  S.query = e.target.value;
  // Update the URL so the query is shareable / bookmarkable.
  const url = new URL(window.location.href);
  if (S.query) url.searchParams.set("q", S.query); else url.searchParams.delete("q");
  history.replaceState(null, "", url.toString());
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchTimer = null;
    fetchSearch();
  }, DEBOUNCE_MS);
}

function onDocKeydown(e) {
  // ⌘/ — focus search from anywhere.
  if ((e.metaKey || e.ctrlKey) && e.key === "/") {
    e.preventDefault();
    document.getElementById("ph-search-input")?.focus();
    return;
  }
  const inSearch = document.activeElement?.id === "ph-search-input";
  // Esc: clear search if non-empty, else back to dashboard.
  if (e.key === "Escape") {
    if (inSearch && S.query) {
      const input = document.getElementById("ph-search-input");
      input.value = "";
      S.query = "";
      // Drop the URL query immediately.
      const url = new URL(window.location.href);
      url.searchParams.delete("q");
      history.replaceState(null, "", url.toString());
      fetchSearch();
      return;
    }
    e.preventDefault();
    window.location.href = "/";
    return;
  }
  // Arrow keys move selection. Skip when the input is focused — text-cursor
  // movement inside the input shouldn't get hijacked.
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    if (inSearch) return;
    if (!S.results.length) return;
    e.preventDefault();
    const idx = S.results.findIndex((r) => r.session_id === S.selectedId);
    const next = e.key === "ArrowDown"
      ? Math.min(S.results.length - 1, (idx < 0 ? 0 : idx + 1))
      : Math.max(0, (idx < 0 ? 0 : idx - 1));
    const id = S.results[next]?.session_id;
    if (id && id !== S.selectedId) {
      S.selectedId = id;
      S.detail = S.detailCache.get(id) || null;
      render();
      if (!S.detail) ensureDetail(id);
    }
    return;
  }
  // ⌘↵ — resume selected row.
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    if (S.selectedId) {
      const row = S.results.find((r) => r.session_id === S.selectedId);
      if (row && !row.is_live && !row.is_resuming) {
        doResume(S.selectedId);
      }
    }
  }
}

// ── Boot ────────────────────────────────────────────────

function boot() {
  // Hydrate query from URL so deep-links work.
  const params = new URLSearchParams(window.location.search);
  const initQ = params.get("q") || "";
  S.query = initQ;

  root.addEventListener("click", onRootClick);
  root.addEventListener("input", onRootInput);
  document.addEventListener("keydown", onDocKeydown);

  render();
  fetchStats();
  fetchSearch();
}

boot();
