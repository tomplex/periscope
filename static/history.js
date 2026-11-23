// /history — find a past Claude session, read it, resume it.
//
// boot() builds the page once. render() rewrites only the filter panel, the
// list and the detail, so the search input keeps its caret and the list and
// the transcript keep their scroll. Events delegate from the root; one keydown
// handler serves the page.

import { SINCE_LABELS, activeFilterLabels, baseName, emptyList, filtersHideSessions, listHead } from "./historyList.js";
import { resumeAnswer, resumeOffer } from "./resume.js";
import { escapeHtml, linearTicket, relTime } from "./util.js";

const RESUME_SESSION = "resumes"; // tmux session host for resume windows
const STORAGE_KEY = "periscope:history:filter";
const DEBOUNCE_MS = 200;
// One page of results. The server answers with the cap that applied (a rerank
// returns fewer); a full page means the index may hold more.
const LIMIT = 50;

// Module state — mutated in place; render() reads it.
const S = {
  query: "",
  filter: loadFilter(),
  filtersOpen: false,
  results: [],
  answered: null, // { query, rerankUsed, limit } of the results on screen
  searchError: null,
  loading: false,
  searchToken: 0, // a newer search supersedes an older one still in flight
  selectedId: null,
  detailCache: new Map(), // session_id → detail payload
  detailError: null, // { id, message }
  stats: null,
  resumes: new Map(), // session_id → { state: "pending" | "done" | "failed", target, detail }
};

// Scroll intents for the next render.
let listToTop = false;
let selectedIntoView = false;

const root = document.getElementById("history-root");
let el = {}; // the regions boot() builds: input, filters, listHead, list, empty, detail, total, announce

// ── Persistence ─────────────────────────────────────────

function loadFilter() {
  const none = { project: "", since: "all", includeTrivial: false, rerank: false };
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
    return {
      // "all" is the old stored value for "every project".
      project: typeof raw.project === "string" && raw.project !== "all" ? raw.project : "",
      since: Object.hasOwn(SINCE_LABELS, raw.since) ? raw.since : "all",
      includeTrivial: !!raw.includeTrivial,
      rerank: !!raw.rerank,
    };
  } catch {
    return none;
  }
}

function saveFilter() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(S.filter));
  } catch {}
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

function fmtDate(epochSec) {
  if (!epochSec) return "";
  return new Date(epochSec * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function fmtTime(epochMs) {
  if (!epochMs) return "";
  return new Date(epochMs).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

const homePath = (p) => (p || "").replace(/^\/(?:Users|home)\/[^/]+/, "~");
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const optId = (sid) => `ph-opt-${sid}`;

// A file path relative to the session's project when it sits inside it.
function relPath(path, project) {
  return project && path.startsWith(`${project}/`) ? path.slice(project.length + 1) : homePath(path);
}

// The one line that says what a tool call did: the input a person would
// recognise (the command, the file, the pattern), else its first string.
const TOOL_SUMMARY_KEYS = {
  Bash: "command",
  Read: "file_path",
  Edit: "file_path",
  Write: "file_path",
  NotebookEdit: "notebook_path",
  Agent: "description",
  Task: "description",
  Grep: "pattern",
  Glob: "pattern",
  WebFetch: "url",
  WebSearch: "query",
};

function toolSummary(t) {
  const input = t.input || {};
  const v = input[TOOL_SUMMARY_KEYS[t.name]];
  const s = typeof v === "string" ? v : Object.values(input).find((x) => typeof x === "string") || "";
  return s.split("\n")[0].slice(0, 160);
}

// Wrap query-term matches in <mark>. Case-insensitive, word-boundary-free
// so partial matches highlight. Returns escaped HTML.
function highlight(text) {
  const escaped = escapeHtml(text || "");
  const terms = S.query
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map((t) => escapeHtml(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return escaped;
  return escaped.replace(new RegExp(`(${terms.join("|")})`, "gi"), "<mark>$1</mark>");
}

// ── Regions ─────────────────────────────────────────────

// Replace a region's markup and give focus back to the control that had it:
// a click on a filter control re-renders the panel it sits in. When that
// control is gone or disabled, focus stays in the region, else goes to the
// search, so a keyboard user never lands on <body>.
function swap(region, html) {
  const active = document.activeElement;
  if (!region.contains(active)) {
    region.innerHTML = html;
    return;
  }
  const key = active.dataset.focus;
  region.innerHTML = html;
  const enabled = (sel) => region.querySelector(`${sel}:not(:disabled)`);
  const next = (key && enabled(`[data-focus="${key}"]`)) || enabled("[data-focus]") || el.input;
  next.focus({ preventScroll: true });
}

// Scroll `child` into its scroller by offsets. scrollIntoView would also
// scroll the fixed-height page around it. The scroller is position: relative.
function scrollWithin(scroller, child, mode) {
  if (!child) return;
  const top = child.offsetTop;
  const bottom = top + child.offsetHeight;
  if (mode === "third") scroller.scrollTop = top - scroller.clientHeight / 3;
  else if (top < scroller.scrollTop) scroller.scrollTop = top;
  else if (bottom > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = bottom - scroller.clientHeight;
}

function renderFilters() {
  const f = S.filter;
  const paths = S.stats?.project_paths || [];
  const options = (f.project && !paths.includes(f.project) ? [f.project, ...paths] : paths)
    .map((p) => `<option value="${escapeHtml(p)}"${p === f.project ? " selected" : ""}>${escapeHtml(homePath(p))}</option>`)
    .join("");
  const pills = Object.entries(SINCE_LABELS)
    .map(
      ([v, label]) =>
        `<button type="button" class="ph-pill" data-action="set-since" data-value="${v}" data-focus="since-${v}" aria-pressed="${f.since === v}">${label}</button>`,
    )
    .join("");
  // Haiku did not summarize them: too short to be worth it, no API key, or a failed call.
  const unsummarized = S.stats ? ` (${S.stats.heuristic})` : "";
  swap(
    el.filters,
    `
    <label class="ph-field">
      <span class="ph-field-label">Project</span>
      <select class="ph-select" data-action="set-project" data-focus="project">
        <option value="">all projects</option>${options}
      </select>
    </label>
    <div class="ph-field" role="group" aria-labelledby="ph-since-label">
      <span class="ph-field-label" id="ph-since-label">Last turn</span>
      <span class="ph-pills">${pills}</span>
    </div>
    <label class="ph-check">
      <input type="checkbox" data-action="toggle-trivial" data-focus="trivial"${f.includeTrivial ? " checked" : ""}>
      unsummarized sessions${unsummarized}
    </label>
    <label class="ph-check">
      <input type="checkbox" data-action="toggle-rerank" data-focus="rerank"${f.rerank ? " checked" : ""}>
      rerank matches with Haiku, about $0.001 a search
    </label>
    <button type="button" class="ph-text-btn" data-action="clear-filters" data-focus="clear"${filtersHideSessions(f) || f.includeTrivial ? "" : " disabled"}>clear filters</button>
  `,
  );
  el.filters.hidden = !S.filtersOpen;
  const n = activeFilterLabels(f).length;
  el.filterToggle.setAttribute("aria-expanded", String(S.filtersOpen));
  el.filterToggle.innerHTML = `Filters${n ? ` <span class="ph-count">${n}</span>` : ""}`;
}

function refsHtml(r) {
  const out = [];
  const project = baseName(r.project_path);
  // A worktree's folder is often named after its branch: say it once.
  if (project !== r.branch) {
    out.push(`<span class="ph-proj" title="${escapeHtml(r.project_path || "")}">${escapeHtml(project)}</span>`);
  }
  if (r.branch && r.branch !== "HEAD") {
    out.push(`<span class="ph-ref ph-branch"><span class="ph-glyph" aria-hidden="true">⎇</span>${escapeHtml(r.branch)}</span>`);
  }
  if (r.linked_pr) out.push(`<span class="ph-ref" title="pull request">#${escapeHtml(r.linked_pr)}</span>`);
  const ticket = linearTicket(r);
  if (ticket) out.push(`<span class="ph-ref ph-ticket" title="${escapeHtml(ticket.tip)}">${escapeHtml(ticket.id)}</span>`);
  return out.join("");
}

function marksOf(r) {
  const out = [];
  if (r.resume_refusal?.status === 404) out.push("no transcript");
  if (r.was_interrupted) out.push("interrupted");
  if (r.trivial) out.push("unsummarized");
  return out;
}

function renderRow(r) {
  const selected = r.session_id === S.selectedId;
  const summary = r.summary || r.first_user_msg || "(no summary)";
  const marks = marksOf(r)
    .map((m) => `<span class="ph-mark">${m}</span>`)
    .join("");
  return `
    <div class="ph-row${selected ? " is-selected" : ""}" role="option" id="${optId(r.session_id)}"
         aria-selected="${selected}" data-action="select" data-id="${escapeHtml(r.session_id)}">
      <div class="ph-row-line">
        <span class="ph-row-title">${highlight(r.pane_name || summary)}</span>
        <span class="ph-row-age" title="last turn ${escapeHtml(fmtDate(r.ended_at))}">${relTime(r.ended_at)} ago</span>
      </div>
      ${r.pane_name ? `<div class="ph-row-summary">${highlight(summary)}</div>` : ""}
      <div class="ph-row-meta">${refsHtml(r)}${marks}</div>
    </div>`;
}

function emptyHtml(title, sub = "", action = "") {
  return `<div class="ph-empty"><p class="ph-empty-title">${title}</p>${sub ? `<p class="ph-empty-sub">${sub}</p>` : ""}${action}</div>`;
}

const clearFiltersBtn = `<button type="button" class="ph-btn" data-action="clear-filters" data-focus="clear">Clear filters</button>`;

// The head is a live region: writing the same text again would announce it again.
function renderListHead() {
  const head = listHead(S);
  if (el.listHead.textContent !== head) el.listHead.textContent = head;
}

function emptyListHtml(empty) {
  const q = escapeHtml(S.answered?.query.trim() || "");
  const noMatch = q ? `No session matches “${q}”` : "No session to show";
  if (empty.kind === "error") {
    return emptyHtml(
      "Search failed",
      escapeHtml(S.searchError),
      `<button type="button" class="ph-btn" data-action="retry" data-focus="retry">Try again</button>`,
    );
  }
  if (empty.kind === "no-index") {
    return emptyHtml("History has no sessions yet", "Run <code>python -m history backfill</code> to index them.");
  }
  if (empty.kind === "filtered") {
    return emptyHtml(
      q ? `No session matches “${q}” here` : "No session matches these filters",
      `Filters: ${escapeHtml(activeFilterLabels(S.filter).join(", "))}.`,
      clearFiltersBtn,
    );
  }
  if (empty.kind === "hidden") {
    return emptyHtml(
      noMatch,
      `${plural(empty.hidden, "unsummarized session")} hidden.`,
      `<button type="button" class="ph-btn" data-action="include-trivial" data-focus="include-trivial">Include unsummarized sessions</button>`,
    );
  }
  return emptyHtml(noMatch, q ? "Try fewer or shorter words." : "");
}

function renderList() {
  renderListHead();
  const empty = emptyList(S);
  // A listbox owns only options, so the states with buttons sit beside it.
  // Swap before hiding: a hidden region drops the focus it holds.
  swap(el.list, S.results.map(renderRow).join(""));
  swap(el.empty, empty ? emptyListHtml(empty) : "");
  el.list.hidden = !S.results.length;
  el.empty.hidden = !empty;
  el.input.setAttribute("aria-expanded", String(S.results.length > 0));
  if (listToTop) el.list.scrollTop = 0;
  if (S.selectedId && S.results.length) {
    el.input.setAttribute("aria-activedescendant", optId(S.selectedId));
  } else {
    el.input.removeAttribute("aria-activedescendant");
  }
  if (selectedIntoView) scrollWithin(el.list, document.getElementById(optId(S.selectedId)));
}

function resumeView(r) {
  const offer = resumeOffer(r.resume_refusal, S.resumes.get(r.session_id));
  const control =
    offer.button === "pending"
      ? `<button type="button" class="ph-btn is-primary" data-focus="resume" aria-disabled="true">Resuming…</button>`
      : offer.button === "ready"
        ? `<button type="button" class="ph-btn is-primary" data-action="resume" data-id="${escapeHtml(r.session_id)}" data-focus="resume"
                title="claude --resume in a new window of tmux session ${RESUME_SESSION}">Resume <span class="ps-kbd">⌘↵</span></button>`
        : "";
  // With no button, the link is where Resume's focus goes.
  const dashboard = offer.dashboard
    ? ` <a class="ph-link" href="/" data-focus="${control ? "dashboard" : "resume"}">Open the dashboard</a>`
    : "";
  return { control, status: offer.status ? `${escapeHtml(offer.status)}${dashboard}` : "" };
}

function renderMore(r) {
  const files = r.files_touched || [];
  const cmds = r.notable_cmds || [];
  if (!files.length && !cmds.length) return "";
  const label = [files.length ? plural(files.length, "file") : "", cmds.length ? plural(cmds.length, "command") : ""]
    .filter(Boolean)
    .join(" · ");
  const fileItems = files.map((f) => `<li class="ph-path">${escapeHtml(relPath(f, r.project_path))}</li>`).join("");
  const cmdItems = cmds.map((c) => `<li class="ph-cmd">${escapeHtml(c)}</li>`).join("");
  return `
    <details class="ph-more">
      <summary data-focus="more">${label}</summary>
      ${fileItems ? `<ul class="ph-more-list">${fileItems}</ul>` : ""}
      ${cmdItems ? `<ul class="ph-more-list">${cmdItems}</ul>` : ""}
    </details>`;
}

function renderMsg(m) {
  if (m.role === "system") {
    return m.kind === "compact" ? `<div class="ph-divider"><span>context compacted ${fmtTime(m.ts_ms)}</span></div>` : "";
  }
  const at = m.ts_ms ? `<time>${fmtTime(m.ts_ms)}</time>` : "";
  const text = m.text
    ? `<div class="ph-msg role-${m.role === "user" ? "user" : "assistant"}">
         <div class="ph-msg-gutter"><span class="ph-role">${m.role === "user" ? "you" : "claude"}</span>${at}</div>
         <div class="ph-msg-body">${highlight(m.text)}</div>
       </div>`
    : "";
  const tools = (m.tool_uses || [])
    .map(
      (t) =>
        `<li class="ph-tool"><span class="ph-tool-name">${escapeHtml(t.name || "tool")}</span><span class="ph-tool-sub">${highlight(toolSummary(t))}</span></li>`,
    )
    .join("");
  return text + (tools ? `<ul class="ph-tools">${tools}</ul>` : "");
}

function renderDetail() {
  const row = S.results.find((r) => r.session_id === S.selectedId);
  if (!row) {
    swap(el.detail, S.results.length ? emptyHtml("Select a session to read it") : "");
    return;
  }
  const loaded = S.detailCache.get(row.session_id);
  // The row wins: it carries the newest resume answer.
  const r = loaded ? { ...loaded, ...row } : row;
  const summary = r.summary || r.first_user_msg || "(no summary)";
  const { control, status } = resumeView(r);
  const tags = (r.tags || []).length
    ? `<ul class="ph-tags" aria-label="tags">${r.tags.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>`
    : "";
  const facts = [
    `last turn ${escapeHtml(fmtDate(r.ended_at))}`,
    `ran ${fmtDuration(r.duration_s)}`,
    plural(r.user_msg_count || 0, "prompt"),
    plural(r.tool_use_count || 0, "tool call"),
  ];
  if (r.was_interrupted) facts.push("interrupted");

  let body;
  if (S.detailError?.id === row.session_id) {
    // Its focus key is the transcript's, so focus lands there and not on Resume.
    body = emptyHtml(
      "Could not load this session",
      escapeHtml(S.detailError.message),
      `<button type="button" class="ph-btn" data-action="retry-detail" data-id="${escapeHtml(row.session_id)}" data-focus="transcript">Try again</button>`,
    );
  } else if (!loaded) {
    body = emptyHtml("Loading the transcript…");
  } else if (loaded.jsonl_missing) {
    body = emptyHtml("History lost its copy of this transcript", "Only the summary above remains.");
  } else if (!loaded.messages.length) {
    body = emptyHtml("This transcript has no messages");
  } else {
    body = loaded.messages.map(renderMsg).join("");
  }

  // Same session on screen: keep the transcript's scroll and the open list
  // of files. A transcript that just loaded opens at the first match.
  const sameSession = el.detail.dataset.id === row.session_id;
  const keepTop = sameSession && el.detail.dataset.loaded === String(!!loaded);
  const top = keepTop ? el.detail.querySelector(".ph-detail-body")?.scrollTop || 0 : 0;
  const moreOpen = sameSession && !!el.detail.querySelector(".ph-more")?.open;
  swap(
    el.detail,
    `
    <div class="ph-detail-head">
      <div class="ph-detail-titlerow">
        <h2 class="ph-detail-title${r.pane_name ? "" : " is-summary"}">${highlight(r.pane_name || summary)}</h2>
        <div class="ph-detail-actions">
          ${control}
          <button type="button" class="ph-btn" data-action="copy-id" data-id="${escapeHtml(r.session_id)}" data-focus="copy">Copy session id</button>
        </div>
      </div>
      ${r.pane_name ? `<p class="ph-detail-summary">${highlight(summary)}</p>` : ""}
      <div class="ph-detail-refs">${refsHtml(r)}<span class="ph-ref" title="${escapeHtml(r.session_id)}">${escapeHtml(r.session_id.slice(0, 8))}</span></div>
      <p class="ph-detail-facts">${facts.join(" · ")}</p>
      ${status ? `<p class="ph-resume-status">${status}</p>` : ""}
      ${tags}
      ${renderMore(r)}
    </div>
    <div class="ph-detail-body" tabindex="0" role="region" aria-label="Transcript" data-focus="transcript">${body}</div>
  `,
  );
  el.detail.dataset.id = row.session_id;
  el.detail.dataset.loaded = String(!!loaded);
  const next = el.detail.querySelector(".ph-detail-body");
  if (keepTop) next.scrollTop = top;
  else if (loaded) scrollWithin(next, next.querySelector("mark"), "third");
  if (moreOpen) el.detail.querySelector(".ph-more").open = true;
}

function render() {
  renderList();
  renderDetail();
  const st = S.stats;
  // A stale "last indexed" says the session-end hook stopped feeding the index.
  const indexed = st?.last_indexed_at && `last indexed ${relTime(st.last_indexed_at)} ago`;
  el.total.textContent = st ? [`${st.total.toLocaleString()} sessions in history`, indexed].filter(Boolean).join(" · ") : "";
  el.total.title = st?.last_indexed_at ? `last indexed ${fmtDate(st.last_indexed_at)}` : "";
  listToTop = false;
  selectedIntoView = false;
}

function announce(msg) {
  el.announce.textContent = msg;
}

// ── Fetch ───────────────────────────────────────────────

// The error a failed response carries: FastAPI's `detail`, else the status.
async function responseError(res) {
  const body = await res.json().catch(() => ({}));
  return new Error(body.detail ? `${body.detail} (HTTP ${res.status})` : `periscope answered HTTP ${res.status}`);
}

// Calendar boundaries in local time: "today" starts at midnight, the week on Monday.
function sinceToEpoch(since) {
  if (since === "all") return null;
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  if (since === "week") d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  if (since === "month") d.setDate(1);
  return Math.floor(d.getTime() / 1000);
}

async function fetchSearch() {
  const token = ++S.searchToken;
  const query = S.query;
  S.loading = true;
  renderListHead();
  const params = new URLSearchParams({ q: query, limit: String(LIMIT) });
  if (S.filter.project) params.set("project", S.filter.project);
  const since = sinceToEpoch(S.filter.since);
  if (since != null) params.set("since", String(since));
  if (S.filter.includeTrivial) params.set("include_trivial", "true");
  if (S.filter.rerank) params.set("rerank", "true");
  try {
    const res = await fetch(`/api/history/search?${params}`);
    if (!res.ok) throw await responseError(res);
    const data = await res.json();
    if (token !== S.searchToken) return;
    const newQuery = S.answered?.query !== query;
    S.results = data.results || [];
    S.answered = { query, rerankUsed: !!data.rerank_used, limit: data.limit, unsummarizedHidden: data.unsummarized_hidden };
    S.searchError = null;
    // The new rows say "already resumed in …" while the resumed window runs, but
    // only for 30 minutes (panes.RESUME_EXPIRY_S); then they offer Resume again.
    for (const [sid, run] of S.resumes) if (run.state === "done") S.resumes.delete(sid);
    // A new query starts at its best match; a filter change keeps the row
    // you were reading while it is still in the list.
    if (newQuery || !S.results.some((r) => r.session_id === S.selectedId)) {
      S.selectedId = S.results[0]?.session_id ?? null;
      listToTop = true;
    }
    if (S.selectedId) ensureDetail(S.selectedId);
  } catch (e) {
    if (token !== S.searchToken) return;
    S.results = [];
    S.selectedId = null;
    S.searchError = e instanceof TypeError ? "periscope did not answer. Is the server running?" : e.message;
  } finally {
    if (token === S.searchToken) {
      S.loading = false;
      render();
    }
  }
}

async function ensureDetail(sessionId) {
  if (S.detailCache.has(sessionId)) return;
  try {
    const res = await fetch(`/api/history/session/${encodeURIComponent(sessionId)}`);
    if (!res.ok) throw await responseError(res);
    S.detailCache.set(sessionId, await res.json());
    if (S.detailError?.id === sessionId) S.detailError = null;
  } catch (e) {
    S.detailError = { id: sessionId, message: e instanceof TypeError ? "periscope did not answer." : e.message };
  }
  if (S.selectedId === sessionId) renderDetail();
}

async function fetchStats() {
  try {
    const res = await fetch("/api/history/stats");
    if (!res.ok) return;
    S.stats = await res.json();
    renderFilters();
    render();
  } catch {
    // The page works without counts and the project list; the search reports the outage.
  }
}

async function doResume(sessionId) {
  const row = S.results.find((r) => r.session_id === sessionId);
  if (!row) return;
  const offer = resumeOffer(row.resume_refusal, S.resumes.get(sessionId));
  if (offer.button !== "ready") {
    if (offer.status) announce(offer.status);
    return;
  }
  S.resumes.set(sessionId, { state: "pending" });
  renderDetail();
  const params = new URLSearchParams({ session: RESUME_SESSION, mode: "resume", resume_id: sessionId });
  let status = 0;
  let body = {};
  try {
    const res = await fetch(`/api/window/new?${params}`, { method: "POST" });
    status = res.status;
    body = await res.json().catch(() => ({}));
  } catch {
    // status 0: periscope did not answer.
  }
  const next = resumeAnswer(status, body, row.resume_refusal);
  if (next.run) S.resumes.set(sessionId, next.run);
  else S.resumes.delete(sessionId);
  row.resume_refusal = next.refusal;
  announce(next.announce);
  renderList();
  renderDetail();
}

// ── Events ──────────────────────────────────────────────

let searchTimer = null;

function select(id, { scroll = false } = {}) {
  if (!id || id === S.selectedId) return;
  S.selectedId = id;
  selectedIntoView = scroll;
  renderList();
  renderDetail();
  selectedIntoView = false;
  ensureDetail(id);
}

function setQuery(q) {
  S.query = q;
  const url = new URL(window.location.href);
  if (q) url.searchParams.set("q", q);
  else url.searchParams.delete("q");
  history.replaceState(null, "", url.toString());
  clearTimeout(searchTimer);
  searchTimer = setTimeout(fetchSearch, DEBOUNCE_MS);
}

function setFilter(patch) {
  Object.assign(S.filter, patch);
  saveFilter();
  renderFilters();
  fetchSearch();
}

function onRootClick(e) {
  const btn = e.target.closest("[data-action]");
  if (!btn || btn.disabled) return;
  const { action, value, id } = btn.dataset;
  if (action === "select") select(id);
  else if (action === "toggle-filters") {
    S.filtersOpen = !S.filtersOpen;
    renderFilters();
  } else if (action === "set-since") setFilter({ since: value });
  else if (action === "clear-filters") setFilter({ project: "", since: "all", includeTrivial: false });
  else if (action === "retry") fetchSearch();
  else if (action === "include-trivial") setFilter({ includeTrivial: true });
  else if (action === "retry-detail") {
    S.detailError = null;
    renderDetail();
    ensureDetail(id);
  }
  else if (action === "resume") doResume(id);
  else if (action === "copy-id") {
    const done = (text) => {
      btn.textContent = text;
      setTimeout(() => {
        btn.textContent = "Copy session id";
      }, 1500);
    };
    navigator.clipboard?.writeText(id).then(
      () => done("Copied"),
      () => done("Copy failed"),
    );
  }
}

function onRootChange(e) {
  const action = e.target.dataset.action;
  if (action === "set-project") setFilter({ project: e.target.value });
  else if (action === "toggle-trivial") setFilter({ includeTrivial: e.target.checked });
  else if (action === "toggle-rerank") setFilter({ rerank: e.target.checked });
}

function onRootInput(e) {
  if (e.target === el.input) setQuery(e.target.value);
}

function onDocKeydown(e) {
  if ((e.metaKey || e.ctrlKey) && e.key === "/") {
    e.preventDefault();
    el.input.focus();
    el.input.select();
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    if (S.selectedId) doResume(S.selectedId);
    return;
  }
  // Esc clears the search, then closes the filters. It never leaves the page:
  // "← dashboard" does that.
  if (e.key === "Escape") {
    if (S.query) {
      e.preventDefault();
      el.input.value = "";
      setQuery("");
    } else if (S.filtersOpen) {
      S.filtersOpen = false;
      renderFilters();
      el.filterToggle.focus();
    }
    return;
  }
  // ↑↓ move the selection from the search field and from the list's
  // surroundings; a select, a checkbox or the transcript keeps its own keys.
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const t = e.target;
    const ours = t === el.input || t === document.body || el.list.contains(t);
    if (!ours || !S.results.length) return;
    e.preventDefault();
    const idx = S.results.findIndex((r) => r.session_id === S.selectedId);
    const next = e.key === "ArrowDown" ? Math.min(S.results.length - 1, idx + 1) : Math.max(0, idx - 1);
    select(S.results[next]?.session_id, { scroll: true });
  }
}

// ── Boot ────────────────────────────────────────────────

function boot() {
  S.query = new URLSearchParams(window.location.search).get("q") || "";
  root.innerHTML = `
    <header class="ph-bar">
      <a class="ph-back" href="/">← dashboard</a>
      <h1 class="ph-title">History</h1>
      <div class="ph-search">
        <input id="ph-search-input" type="text" role="combobox" aria-expanded="false" aria-controls="ph-results"
               aria-autocomplete="list" aria-label="Search sessions"
               placeholder="search summaries, prompts, files and commands" autocomplete="off" spellcheck="false">
        <span class="ps-kbd" aria-hidden="true">⌘/</span>
      </div>
      <button type="button" class="ph-btn ph-filter-toggle" data-action="toggle-filters" aria-controls="ph-filters" aria-expanded="false">Filters</button>
    </header>
    <div class="ph-filters" id="ph-filters" hidden></div>
    <main class="ph-body">
      <section class="ph-list" aria-label="Sessions">
        <p class="ph-list-head" aria-live="polite"></p>
        <div class="ph-results" id="ph-results" role="listbox" aria-label="Sessions" hidden></div>
        <div class="ph-list-empty" hidden></div>
      </section>
      <section class="ph-detail" aria-label="Session"></section>
    </main>
    <footer class="ph-foot">
      <span><span class="ps-kbd">↑</span><span class="ps-kbd">↓</span> select</span>
      <span><span class="ps-kbd">⌘↵</span> resume</span>
      <span><span class="ps-kbd">esc</span> clear the search</span>
      <span class="ph-foot-total"></span>
    </footer>
    <p class="ph-sr-only" aria-live="polite"></p>
  `;
  el = {
    input: root.querySelector("#ph-search-input"),
    filterToggle: root.querySelector(".ph-filter-toggle"),
    filters: root.querySelector("#ph-filters"),
    listHead: root.querySelector(".ph-list-head"),
    list: root.querySelector("#ph-results"),
    empty: root.querySelector(".ph-list-empty"),
    detail: root.querySelector(".ph-detail"),
    total: root.querySelector(".ph-foot-total"),
    announce: root.querySelector(".ph-sr-only"),
  };
  el.input.value = S.query;

  root.addEventListener("click", onRootClick);
  root.addEventListener("change", onRootChange);
  root.addEventListener("input", onRootInput);
  document.addEventListener("keydown", onDocKeydown);
  // A tab left open keeps its first stats; "last indexed" would then blame the hook.
  document.addEventListener("visibilitychange", () => document.hidden || fetchStats());

  renderFilters();
  render();
  el.input.focus();
  fetchStats();
  fetchSearch();
}

boot();
