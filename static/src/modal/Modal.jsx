// The pane modal — one file (tab strip + sidebar inline, per the structure
// decision; NOT exploded into <TabStrip>/<Sidebar>). Ported from
// static/modal.js. Renders its own `#modal` element inside the Preact root so
// the CSS contract is preserved verbatim: `#modal[data-tab]` keys the visible
// pane, `#modal-terminal-pane` / `#modal-review-pane` / `#modal-xterm` /
// `#modal-side` ids are unchanged, and every `.modal-*` / `.timeline-*` /
// `.pr-*` / `.tag-*` / `.activity-*` class name carries over.
//
// Lifecycle (Task 6 must-not-drop):
//  - openModal(target, opts) sets the shared `activeTarget` signal (also set by
//    <Detail>.selectPane — coupling #5) and a 1.5s /api/pane header poll runs
//    while open. The poll does NOT commit while `modalRenaming` /
//    `modalAutoRenaming` hold.
//  - <Terminal> is keyed per-open (activeTarget) so it mounts once per open and
//    stays mounted across tab switches (CSS hides the terminal pane on the
//    review tab; we must not unmount it or the WS would churn).
//  - Sidebar inputs are UNCONTROLLED (refs); the poll re-renders the PR/Linear/
//    activity cards but a focus-guard skips the wholesale rebuild while focus is
//    inside the sidebar, and the activity scrollTop is restored across updates.
//  - The LGTM iframe is reused; its src is reassigned ONLY when the tab/doc key
//    changes (never per poll — that kills the SSE), and it is keyed so Preact
//    doesn't recreate it. No loading=lazy.
//  - Escape goes through the shared LIFO useEscape stack (dropdown closes first,
//    then the modal).
import { signal } from "@preact/signals";
import { useRef, useEffect, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { activeTarget, modalRenaming, modalAutoRenaming } from "../store.js";
import { targetQuery, apiCall, escapeHtml, relTime, prUrl, rewriteLgtmHost } from "../util.js";
import * as prefs from "../prefs.js";
import { Terminal } from "../terminal/Terminal.jsx";
import { writeTerminalLine } from "../terminal/terminalCore.js";
import { poll } from "../grid/poll.js";

const MODAL_POLL_MS = 1500;
const MOUNTED_DOCS_KEY_PREFIX = "periscope-lgtm-mounted:";

// Open request, mirrors the vanilla openModal(target, {tab}) signature. A
// signal so the singleton <Modal> reacts; the public opener (registered on
// window so Card/Detail can call it) just writes activeTarget + this.
const openOpts = signal({});

// Public opener. Card.jsx and (later) Detail call this via
// window.__periscopeOpenModal (see poll.js openModal bridge). Setting
// activeTarget mounts the modal; opts.tab === "review" auto-switches to the
// first LGTM tab once data arrives.
export function openModal(target, opts = {}) {
  openOpts.value = opts || {};
  activeTarget.value = target;
}

function alertDotColor(kind) {
  if (kind === "need_human") return "var(--s-danger)";
  if (kind === "done") return "var(--s-success)";
  return "var(--fg-3)";
}

function timelineColor(kind, evState) {
  if (kind === "commit") return "var(--s-shell)";
  if (kind === "ci") {
    if (evState === "failed") return "var(--s-danger)";
    if (evState === "running") return "var(--s-working)";
    return "var(--s-success)";
  }
  if (kind === "open") return "var(--fg-3)";
  if (kind === "reset") return "var(--s-working)";
  if (kind === "milestone") return "var(--s-success)";
  return "var(--fg-3)";
}

function timelineLabel(kind, evState) {
  if (kind === "commit") return "commit";
  if (kind === "ci") return evState ? `ci ${evState}` : "ci";
  if (kind === "open") return "opened";
  if (kind === "reset") return evState === "compacted" ? "compacted" : "cleared";
  if (kind === "milestone") return "milestone";
  return kind;
}

function avatarChars(handle) {
  if (!handle) return "?";
  const letters = handle.replace(/[^A-Za-z0-9]/g, "");
  return (letters.slice(0, 2) || handle.slice(0, 1)).toUpperCase();
}

// ── Tab spec (ported from buildTabSpec) ─────────────────────────────────────
function buildTabSpec(data, mountedDocIds) {
  const items = data?.lgtm?.items || [];
  const slug = data?.lgtm?.slug;
  const hasSession = !!slug && items.length > 0;
  const allDocs = hasSession
    ? items.filter((i) => i.id !== "diff").map((d) => ({ id: `lgtm:${d.id}`, label: d.title || d.id }))
    : [];
  const mounted = allDocs.filter((d) => mountedDocIds.has(d.id));
  return {
    slug,
    showDiff: hasSession && items.some((i) => i.id === "diff"),
    showWalkthrough: hasSession && !!data?.lgtm?.walkthrough,
    mounted,
    docs: allDocs,
    showStart: !hasSession && !!data?.cwd_raw,
  };
}

// ── PR / Linear sidebar cards (data-driven JSX; convention #4 stopPropagation) ─
function PrCard({ data, onLinkAsk }) {
  if (!data.pr) {
    const attached = !!data.channel_attached;
    const title = attached
      ? "Ask Claude to link the PR for this pane (via link_pr MCP tool)"
      : "Channel not attached. Respawn Claude via + claude.";
    return (
      <button
        class="modal-side-link-btn"
        type="button"
        disabled={!attached}
        title={title}
        onClick={() => onLinkAsk("pr")}
      >
        + link pull request
      </button>
    );
  }
  const ciState = data.ci === "✓" ? "passing" : data.ci === "✗" ? "failing" : data.ci === "⟳" ? "running" : "—";
  const ciClass = data.ci === "✓" ? "ci-passing" : data.ci === "✗" ? "ci-failing" : data.ci === "⟳" ? "ci-running" : "";
  const url = prUrl(data.repo_slug, data.pr);
  const adds = data.pr_additions || 0;
  const dels = data.pr_deletions || 0;
  return (
    <div class="modal-card-inset">
      <div class="pr-head">
        {url ? (
          <a class="pr-num" href={url} target="_blank" rel="noopener" onClick={(e) => e.stopPropagation()}>
            #{data.pr}
          </a>
        ) : (
          <span class="pr-num">#{data.pr}</span>
        )}
        <span class="pr-title" title={data.pr_title || ""}>{data.pr_title || ""}</span>
      </div>
      <div class="pr-meta">
        {data.pr_draft ? (
          <span class="pr-mini pr-mini-draft">draft</span>
        ) : (
          <span class="pr-mini pr-mini-open">open</span>
        )}
        <span class="pr-diff">
          <span class="diff-plus">+{adds}</span> <span class="diff-minus">−{dels}</span>
        </span>
        <span class={`pr-ci ${ciClass}`}>
          <span class="ci-dot"></span>ci {ciState}
        </span>
        {(data.pr_reviewers || []).length > 0 && (
          <span class="pr-reviewers">
            {(data.pr_reviewers || []).map((r) => (
              <span class="modal-avatar" title={r}>{avatarChars(r)}</span>
            ))}
          </span>
        )}
      </div>
    </div>
  );
}

function LinearCard({ data, onLinkAsk }) {
  if (data.linked_linear) {
    const id = data.linked_linear;
    const url = `https://linear.app/issue/${id}`;
    const title = data.linked_linear_title || "linear ticket";
    return (
      <div class="modal-card-inset">
        <div class="pr-head">
          <a
            class="pr-num"
            href={url}
            target="_blank"
            rel="noopener"
            onClick={(e) => e.stopPropagation()}
            title={`Linear ticket ${id} (linked by claude)`}
          >
            {id}
          </a>
          <span class="pr-title" title={title}>{title}</span>
        </div>
        {data.linked_linear_status && (
          <div class="pr-meta">
            <span class="pr-mini">{data.linked_linear_status}</span>
          </div>
        )}
      </div>
    );
  }
  const attached = !!data.channel_attached;
  const title = attached
    ? "Ask Claude to link a Linear ticket for this pane (via link_linear MCP tool)"
    : "Channel not attached. Respawn Claude via + claude.";
  return (
    <button
      class="modal-side-link-btn"
      type="button"
      disabled={!attached}
      title={title}
      onClick={() => onLinkAsk("linear")}
    >
      + link Linear ticket
    </button>
  );
}

// ── Activity stream ─────────────────────────────────────────────────────────
function ActivityRow({ e }) {
  if (e.src === "alert") {
    return (
      <li class="timeline-row timeline-row-alert" data-kind={e.kind}>
        <span class="timeline-dot" style={`background:${alertDotColor(e.kind)}`}></span>
        <div class="timeline-body">
          <div class="timeline-text">{e.text}</div>
          <div class="timeline-when">claude · {e.kind} · {relTime(e.at)} ago</div>
        </div>
      </li>
    );
  }
  return (
    <li class="timeline-row" data-kind={e.kind}>
      <span class="timeline-dot" style={`background:${timelineColor(e.kind, e.state)}`}></span>
      <div class="timeline-body">
        {e.url ? (
          <a class="timeline-text timeline-link" href={e.url} target="_blank" rel="noopener">{e.text}</a>
        ) : (
          <div class="timeline-text">{e.text}</div>
        )}
        <div class="timeline-when">{timelineLabel(e.kind, e.state)} · {relTime(e.at)} ago</div>
      </div>
    </li>
  );
}

function ActivitySection({ data, streamRef }) {
  const stream = data.activity || [];
  const latestAlert = stream
    .filter((e) => e.src === "alert")
    .reduce((best, a) => (best && best.at >= a.at ? best : a), null);
  const pinned = latestAlert && latestAlert.kind === "need_human" ? latestAlert : null;
  const rest = stream.filter((e) => e !== pinned);
  return (
    <>
      {pinned && (
        <div class="activity-pinned">
          <div class="activity-pinned-label">needs you · {relTime(pinned.at)} ago</div>
          <div class="activity-pinned-text">{pinned.text}</div>
        </div>
      )}
      {rest.length ? (
        <ol class="timeline activity-stream" ref={streamRef}>
          {rest.map((e, i) => (
            <ActivityRow key={i} e={e} />
          ))}
        </ol>
      ) : !pinned ? (
        <div class="timeline-empty">no recent activity</div>
      ) : null}
    </>
  );
}

// ── Notes editor — UNCONTROLLED so the 1.5s poll never clobbers in-flight
// typing. Keyed by pid at the call site so switching panes resets it. ──────────
function NotesEditor({ pid, onRefresh }) {
  const taRef = useRef(null);
  const tiRef = useRef(null);
  const notesTimer = useRef(null);
  const ann = pid ? prefs.getAnnotation(pid) : null;
  const tags = ann?.tags || [];

  function flushNotes() {
    if (!pid) return;
    const cur = prefs.getAnnotation(pid) || { notes: "", tags: [] };
    prefs.setAnnotation(pid, { notes: taRef.current.value, tags: cur.tags });
  }

  function submitTag() {
    const raw = (tiRef.current.value || "").trim();
    if (!raw || !pid) return;
    const cur = prefs.getAnnotation(pid) || { notes: taRef.current.value, tags: [] };
    const parts = raw.split(/[\s,]+/).filter(Boolean);
    prefs.setAnnotation(pid, { notes: taRef.current.value, tags: [...cur.tags, ...parts] });
    tiRef.current.value = "";
    onRefresh();
  }

  function removeTag(i) {
    const cur = prefs.getAnnotation(pid) || { notes: taRef.current.value, tags: [] };
    prefs.setAnnotation(pid, { notes: taRef.current.value, tags: cur.tags.filter((_, idx) => idx !== i) });
    onRefresh();
  }

  return (
    <>
      <textarea
        ref={taRef}
        id="modal-notes"
        class="modal-notes"
        placeholder={pid ? "Notes — saves on blur" : "Notes unavailable (no pid)"}
        disabled={!pid}
        // Uncontrolled: defaultValue (initial) only; poll re-renders never reset it.
        defaultValue={ann?.notes || ""}
        onInput={() => {
          clearTimeout(notesTimer.current);
          notesTimer.current = setTimeout(flushNotes, 600);
        }}
        onBlur={() => {
          clearTimeout(notesTimer.current);
          flushNotes();
        }}
        onKeyDown={(e) => e.stopPropagation()}
      />
      <div class="tag-row">
        <div class="tag-chips" id="modal-tags">
          {tags.map((t, i) => (
            <span class="tag-chip" data-tag-i={i}>
              {t}
              <button class="tag-chip-x" title="remove" onClick={() => removeTag(i)}>×</button>
            </span>
          ))}
        </div>
        <input
          ref={tiRef}
          id="modal-tag-input"
          class="modal-tag-input"
          type="text"
          placeholder="add tag, Enter or comma"
          disabled={!pid}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              submitTag();
            }
          }}
        />
      </div>
    </>
  );
}

// Canned prompts for the "+ link …" buttons (ping Claude's MCP tool).
const LINK_ASK_PROMPTS = {
  pr: "Please link the PR you're working on for this pane using the `link_pr` MCP tool. If there isn't one, ignore this.",
  linear: "Please link the relevant Linear ticket for this pane using the `link_linear` MCP tool. If there isn't one, ignore this.",
};

// ── Sidebar: focus-guarded, uncontrolled-notes wrapper ──────────────────────
// The notes/tags inputs live in <NotesEditor> (uncontrolled + keyed on pid) so
// they never reset on poll. The PR/Linear/activity cards re-render with fresh
// data each poll, but we restore the activity scrollTop and DO NOT touch the
// sidebar at all while focus is inside it (matches the vanilla
// `modalSide.contains(document.activeElement)` skip).
function Sidebar({ data, onRefresh }) {
  const rootRef = useRef(null);
  const streamRef = useRef(null);
  const priorScroll = useRef(0);
  const pid = data.pid || "";

  // Restore the activity-stream scrollTop after each data-driven re-render.
  useEffect(() => {
    if (streamRef.current && priorScroll.current) {
      streamRef.current.scrollTop = priorScroll.current;
    }
  });

  // Clear unread when the modal shows replies (fire-and-forget).
  useEffect(() => {
    if (data.pane_id && (data.channel_unread || 0) > 0) {
      fetch(`/api/channel/clear-unread?pane=${encodeURIComponent(data.pane_id)}`, { method: "POST" });
    }
  }, [data.pane_id, data.channel_unread]);

  function onLinkAsk(kind) {
    const content = LINK_ASK_PROMPTS[kind];
    if (!content || !data.pane_id) return;
    fetch(`/api/channel/push?pane=${encodeURIComponent(data.pane_id)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ content }),
    }).catch(() => {});
  }

  // Stash scrollTop just before render commits (so the post-render effect can
  // restore it). Reading during render is the cheapest equivalent of the
  // vanilla pre-rebuild stash.
  if (streamRef.current) priorScroll.current = streamRef.current.scrollTop;

  return (
    <aside id="modal-side" class="modal-side" ref={rootRef}>
      <section class="modal-side-section">
        <h4>Linked</h4>
        <PrCard data={data} onLinkAsk={onLinkAsk} />
        <LinearCard data={data} onLinkAsk={onLinkAsk} />
      </section>
      <section class="modal-side-section modal-side-notes">
        <h4>Notes</h4>
        <NotesEditor key={pid} pid={pid} onRefresh={onRefresh} />
      </section>
      <section class="modal-side-section modal-side-activity">
        <h4>Activity</h4>
        <ActivitySection data={data} streamRef={streamRef} />
      </section>
    </aside>
  );
}

// ── Tab strip ───────────────────────────────────────────────────────────────
function TabStrip({ spec, activeTab, onSwitch, onMount, onUnmount, onRemove }) {
  const [dropdownOpen, setDropdownOpen] = useState(false);

  // Outside-click + Escape close the dropdown (LIFO: closes before the modal).
  useEscape(() => setDropdownOpen(false), dropdownOpen);
  useEffect(() => {
    if (!dropdownOpen) return;
    function onDoc(e) {
      if (e.target.closest(".modal-tab-dropdown")) return;
      setDropdownOpen(false);
    }
    const t = setTimeout(() => document.addEventListener("click", onDoc), 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener("click", onDoc);
    };
  }, [dropdownOpen]);

  const toggleActive = !!spec.docs.find((d) => d.id === activeTab) && !spec.mounted.find((d) => d.id === activeTab);

  function tabBtn(id, label) {
    const active = id === activeTab;
    return (
      <button
        class={`modal-tab${active ? " is-active" : ""}`}
        type="button"
        role="tab"
        aria-selected={active ? "true" : "false"}
        onClick={(e) => { e.stopPropagation(); onSwitch(id); }}
      >
        {label}
      </button>
    );
  }

  return (
    <nav class="modal-tabs" id="modal-tabs" role="tablist" aria-label="modal view">
      {tabBtn("terminal", "Terminal")}
      {spec.showDiff && tabBtn("lgtm:diff", "Diff")}
      {spec.showWalkthrough && tabBtn("lgtm:walkthrough", "Walkthrough")}
      {spec.mounted.map((m) => {
        const active = m.id === activeTab;
        return (
          <div
            class={`modal-tab modal-tab-mounted${active ? " is-active" : ""}`}
            data-tab={m.id}
            role="tab"
            aria-selected={active ? "true" : "false"}
            onClick={(e) => { e.stopPropagation(); onSwitch(m.id); }}
          >
            <span class="modal-tab-mounted-label">{m.label}</span>
            <button
              type="button"
              class="modal-tab-mounted-unmount"
              title={`Unpin ${m.label} from tabs`}
              aria-label={`Unpin ${m.label}`}
              onClick={(e) => { e.stopPropagation(); onUnmount(m.id); }}
            >
              ×
            </button>
          </div>
        );
      })}
      {spec.docs.length > 0 && (
        <div class="modal-tab-dropdown">
          <button
            type="button"
            class={`modal-tab modal-tab-dropdown-toggle${toggleActive ? " is-active" : ""}`}
            aria-haspopup="menu"
            aria-expanded={dropdownOpen ? "true" : "false"}
            aria-selected={toggleActive ? "true" : "false"}
            onClick={(e) => { e.stopPropagation(); setDropdownOpen((v) => !v); }}
          >
            <span class="modal-tab-dropdown-label">
              {toggleActive ? spec.docs.find((d) => d.id === activeTab).label : "Documents"}
            </span>
            <span class="modal-tab-dropdown-chevron" aria-hidden="true">▾</span>
          </button>
          <div class="modal-tab-dropdown-menu" role="menu" hidden={!dropdownOpen}>
            {spec.docs.map((d) => {
              const rawItemId = d.id.startsWith("lgtm:") ? d.id.slice(5) : d.id;
              return (
                <div
                  class={`modal-tab-dropdown-item${d.id === activeTab ? " is-active" : ""}`}
                  data-tab={d.id}
                  role="menuitem"
                  onClick={(e) => { e.stopPropagation(); onMount(d.id); setDropdownOpen(false); }}
                >
                  <span class="modal-tab-dropdown-item-label">{d.label}</span>
                  <button
                    type="button"
                    class="modal-tab-dropdown-item-remove"
                    title={`Remove from review (${d.label})`}
                    aria-label={`Remove ${d.label}`}
                    onClick={(e) => { e.stopPropagation(); onRemove(rawItemId); }}
                  >
                    ×
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}
      {spec.showStart && tabBtn("lgtm-start", "+ Start review")}
    </nav>
  );
}

// ── Review pane: idempotent LGTM iframe / Start-review / walkthrough ─────────
// The iframe is reused; src reassigned only when the (tabId → url) key changes.
// Keyed by activeTarget at the <Modal> level so a pane switch tears it down.
// Mounted whenever the modal is open (NOT gated on the active tab) so the
// iframe survives terminal↔review toggles — the vanilla path left the iframe
// in #modal-review-content and only CSS-hid it on the Terminal tab, so the SSE
// connection stayed up across tab switches. We mirror that: the iframe element
// persists; its src is reassigned ONLY when the (review) tab/doc key changes.
function ReviewPane({ data, activeTab, onStarted }) {
  const iframeRef = useRef(null);
  const mountedSrc = useRef(null);

  // Compute the iframe URL for the active LGTM tab. When the active tab is the
  // terminal (or lgtm-start), `url` is null — we leave the iframe's last src in
  // place rather than tearing it down.
  let url = null;
  const lgtm = data?.lgtm;
  if (lgtm?.slug && lgtm?.url) {
    const baseUrl = rewriteLgtmHost(lgtm.url);
    if (activeTab === "lgtm:walkthrough") {
      url = `${baseUrl}?embedded=1&view=walkthrough&host=periscope`;
    } else if (activeTab.startsWith("lgtm:")) {
      const itemId = activeTab.slice(5);
      url = `${baseUrl}?embedded=1&item=${encodeURIComponent(itemId)}&host=periscope`;
    }
  }

  // Reassign src ONLY when the review url changes — never per poll, and never
  // back to null on a terminal switch (that would kill the SSE).
  useEffect(() => {
    if (url && iframeRef.current && mountedSrc.current !== url) {
      iframeRef.current.src = url;
      mountedSrc.current = url;
    }
  }, [url]);

  // The Start-review CTA replaces the iframe only when there's no session yet.
  if (activeTab === "lgtm-start") {
    return <StartReview data={data} onStarted={onStarted} />;
  }

  // Render the iframe whenever a session exists (so it persists across tab
  // switches). No loading=lazy: the pane is display:none on the Terminal tab
  // and some browsers refuse to load lazy iframes whose ancestors aren't shown.
  if (lgtm?.slug && lgtm?.url) {
    return <iframe ref={iframeRef} title="LGTM review" referrerpolicy="no-referrer" />;
  }
  return <div class="modal-review-empty"><p>Loading…</p></div>;
}

function StartReview({ data, onStarted }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const cwd = data.cwd_raw || "";

  async function start() {
    setBusy(true);
    setErr("");
    try {
      const res = await fetch("/api/lgtm/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cwd }),
      });
      const payload = await res.json();
      if (!payload.ok) throw new Error(payload.error || "unknown error");
      onStarted();
    } catch (e) {
      setBusy(false);
      setErr(`Could not start review: ${e.message}`);
    }
  }

  return (
    <div class="modal-review-empty">
      <p>No LGTM review session for this pane's repository yet.</p>
      <p style="font-size: 11.5px; color: var(--fg-4);">{cwd || "(no cwd)"}</p>
      <button type="button" disabled={!cwd || busy} onClick={start}>
        {busy ? "Starting…" : "Start review"}
      </button>
      <div class="modal-review-error" hidden={!err}>{err}</div>
    </div>
  );
}

// ── Modal header title (name + ✨ auto-rename + session + cwd) ────────────────
function ModalTitle({ data, target, onRename, onAutoRename }) {
  const [editing, setEditing] = useState(false);
  const inputRef = useRef(null);
  const committed = useRef(false);
  const name = data?.name || data?.target || target;

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  function begin() {
    if (modalRenaming.value) return;
    committed.current = false;
    modalRenaming.value = true;
    setEditing(true);
  }

  async function finish(save, value) {
    if (committed.current) return;
    committed.current = true;
    modalRenaming.value = false;
    setEditing(false);
    const newName = (value || "").trim();
    if (save && newName && newName !== name) {
      await onRename(newName);
    }
    poll();
  }

  if (editing) {
    return (
      <h2 id="modal-title">
        <input
          ref={inputRef}
          type="text"
          class="rename-input modal-rename-input"
          value={name}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Enter") { e.preventDefault(); finish(true, e.currentTarget.value); }
            else if (e.key === "Escape") { e.preventDefault(); finish(false, e.currentTarget.value); }
          }}
          onBlur={(e) => finish(true, e.currentTarget.value)}
        />
      </h2>
    );
  }

  return (
    <h2 id="modal-title">
      <span class="modal-name" onDblClick={(e) => { e.stopPropagation(); begin(); }}>{name}</span>
      <button
        type="button"
        class={`modal-title-rename${modalAutoRenaming.value ? " busy" : ""}`}
        title="ask Claude to rename this window"
        disabled={modalAutoRenaming.value}
        onClick={(e) => { e.stopPropagation(); onAutoRename(); }}
      >
        ✨
      </button>
      {data?.session && <span class="modal-session">{data.session}</span>}
      {data?.cwd && <span class="modal-cwd mono">{data.cwd}</span>}
    </h2>
  );
}

// ── Subtitle (branch · PR · CI · ctx% · model · spinner) ─────────────────────
function ModalSubtitle({ data }) {
  if (!data) return <div id="modal-subtitle" class="modal-subtitle"></div>;
  const parts = [];
  if (data.branch) parts.push(<span class="mono">{data.branch}</span>);
  if (data.pr) {
    const ciCls = data.ci === "✓" ? "ci-ok" : data.ci === "✗" ? "ci-bad" : "ci-pending";
    const href = prUrl(data.repo_slug, data.pr);
    parts.push(
      <>
        {href ? (
          <a class="pr" href={href} target="_blank" rel="noopener">#{data.pr}</a>
        ) : (
          <span class="pr">#{data.pr}</span>
        )}{" "}
        {data.ci ? <span class={ciCls}>{data.ci}</span> : null}
      </>
    );
  }
  if (data.context_pct != null) parts.push(<>{data.context_pct}%</>);
  if (data.model) parts.push(<>{data.model.replace(/\s*\(.*\)/, "")}</>);
  if (data.state === "needs-input") {
    parts.push(<span class="spinner-tag" style="color: var(--s-needs); font-weight: 600;">⚠ needs input</span>);
  } else if (data.spinner) {
    parts.push(<span class="spinner-tag">✻ {data.spinner.toLowerCase()}…</span>);
  } else if (data.pending_input) {
    parts.push(<span class="spinner-tag" style="color: var(--fg-3); font-style: normal;">↗ pending</span>);
  }
  return (
    <div id="modal-subtitle" class="modal-subtitle">
      {parts.map((p, i) => (
        <>
          {i > 0 ? <span class="sep">·</span> : null}
          {i > 0 ? " " : null}
          {p}
        </>
      ))}
    </div>
  );
}

// Last pane_id from the modal's /api/pane poll. The window-level LGTM
// postMessage handler reads it to deliver `lgtm-notify-claude` payloads over
// periscope's channel (the iframe doesn't know the pane id).
let lastModalPaneId = null;

// ── The Modal itself ─────────────────────────────────────────────────────────
function ModalBody({ target }) {
  const [data, setData] = useState(null);
  const [activeTab, setActiveTab] = useState("terminal");
  // mountedDocIds + the slug it's loaded for (localStorage-backed pinning).
  const mountedDocIds = useRef(new Set());
  const mountedDocsSlug = useRef(null);
  // Auto-switch intents (refs so the poll closure reads the live value).
  const pendingReviewOpen = useRef(false);
  const pendingTabIdAfterAdd = useRef(null);
  // Latest /api/pane data, so tab clicks / unmount can recompute without
  // waiting for the next poll.
  const lastData = useRef(null);
  // The interval-driven `refresh` closure is created once (open effect, [target]
  // deps), so it would capture a stale `activeTab`. Mirror the live value into a
  // ref each render so the auto-switch reads the current tab.
  const activeTabRef = useRef("terminal");
  activeTabRef.current = activeTab;

  const isReview = activeTab !== "terminal";

  function loadMountedDocs(slug) {
    mountedDocsSlug.current = slug;
    try {
      const raw = localStorage.getItem(MOUNTED_DOCS_KEY_PREFIX + slug);
      mountedDocIds.current = new Set(raw ? JSON.parse(raw) : []);
    } catch {
      mountedDocIds.current = new Set();
    }
  }
  function saveMountedDocs() {
    if (!mountedDocsSlug.current) return;
    try {
      localStorage.setItem(
        MOUNTED_DOCS_KEY_PREFIX + mountedDocsSlug.current,
        JSON.stringify([...mountedDocIds.current])
      );
    } catch (_) {}
  }

  // Header poll. Paused while a rename / auto-rename is in flight.
  async function refresh() {
    if (modalRenaming.value) return;
    if (modalAutoRenaming.value) return;
    try {
      const res = await fetch(`/api/pane?${targetQuery(target)}&lines=80`);
      if (!res.ok) return;
      const d = await res.json();
      lastData.current = d;
      lastModalPaneId = d.pane_id || null;
      // Lazily load the pinned-docs set once per slug.
      const slug = d?.lgtm?.slug;
      if (slug && mountedDocsSlug.current !== slug) loadMountedDocs(slug);
      // Auto-switch logic (ported from performAutoSwitch); reads the live tab.
      autoSwitch(d, activeTabRef.current);
      setData(d);
    } catch (_) {}
  }

  // Ported from performAutoSwitch. Two triggers: a doc just added via Cmd+click
  // (highest priority), and openModal({tab:"review"}). Plus the fallback when
  // the active tab disappeared (deregister / doc removed) → prefer Diff over
  // Terminal. `activeTab` is read via a ref-free closure over the latest state
  // by reading the current value at call time (refresh runs after setData in a
  // later tick, so `activeTab` here is the value from this render).
  function autoSwitch(d, current) {
    const spec = buildTabSpec(d, mountedDocIds.current);
    const validIds = new Set(["terminal"]);
    if (spec.showDiff) validIds.add("lgtm:diff");
    if (spec.showWalkthrough) validIds.add("lgtm:walkthrough");
    for (const dd of spec.docs) validIds.add(dd.id);
    if (spec.showStart) validIds.add("lgtm-start");

    let next = current;
    if (pendingTabIdAfterAdd.current && validIds.has(pendingTabIdAfterAdd.current)) {
      next = pendingTabIdAfterAdd.current;
      pendingTabIdAfterAdd.current = null;
    } else if (pendingReviewOpen.current) {
      const first = spec.showDiff ? "lgtm:diff" : spec.docs[0]?.id ?? null;
      if (first) {
        next = first;
        pendingReviewOpen.current = false;
      }
    }
    if (!validIds.has(next)) next = spec.showDiff ? "lgtm:diff" : "terminal";
    if (next !== current) setActiveTab(next);
  }

  // Open lifecycle: set the review-open intent, kick the first poll, start the
  // 1.5s interval. Re-runs only when `target` changes (modal keyed per open).
  useEffect(() => {
    pendingReviewOpen.current = openOpts.value.tab === "review";
    refresh();
    const handle = setInterval(refresh, MODAL_POLL_MS);
    return () => clearInterval(handle);
  }, [target]);

  async function onRename(newName) {
    await fetch(`/api/rename?${targetQuery(target)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName }),
    });
    refresh();
  }

  async function onAutoRename() {
    if (modalAutoRenaming.value) return;
    modalAutoRenaming.value = true;
    try {
      const d = await apiCall("auto-rename window", `/api/auto-rename-window?${targetQuery(target)}`, {
        method: "POST",
      });
      if (d) poll();
    } finally {
      modalAutoRenaming.value = false;
      refresh();
    }
  }

  // Cmd+click on a .md path in the terminal → add as an LGTM doc; queue the
  // auto-switch to the new tab.
  async function onMdLink(rawPath) {
    if (!rawPath) return;
    const cwd = lastData.current?.cwd_raw;
    if (!cwd) {
      console.warn("add doc: no cwd for current pane");
      return;
    }
    const path = rawPath.replace(/:\d+$/, "");
    const payload = await apiCall("add doc", "/api/lgtm/add-doc", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cwd, path }),
    });
    if (!payload) return;
    pendingTabIdAfterAdd.current = payload.tab_id;
    refresh();
  }

  async function onPaste(e) {
    if (!activeTarget.value) return;
    const items = e.clipboardData?.items || [];
    for (const item of items) {
      if (item.kind !== "file" || !item.type.startsWith("image/")) continue;
      const blob = item.getAsFile();
      if (!blob) continue;
      e.preventDefault();
      e.stopPropagation();
      try {
        const res = await fetch(`/api/paste-image?${targetQuery(activeTarget.value)}`, {
          method: "POST",
          headers: { "Content-Type": blob.type || "image/png" },
          body: blob,
        });
        const d = await res.json();
        if (!d.ok) writeTerminalLine(`\r\n\x1b[31m[periscope: image paste failed: ${d.error}]\x1b[0m`);
      } catch (err) {
        writeTerminalLine(`\r\n\x1b[31m[periscope: image paste error: ${err.message}]\x1b[0m`);
      }
      return;
    }
  }

  // Tab strip handlers.
  function switchTab(id) {
    setActiveTab(id);
  }
  function mountDoc(id) {
    if (!mountedDocIds.current.has(id)) {
      mountedDocIds.current.add(id);
      saveMountedDocs();
    }
    setActiveTab(id);
  }
  function unmountDoc(id) {
    if (mountedDocIds.current.delete(id)) {
      saveMountedDocs();
      if (lastData.current) setData({ ...lastData.current }); // re-render strip
    }
  }
  async function removeItem(rawItemId) {
    const slug = lastData.current?.lgtm?.slug;
    if (!slug || !rawItemId) return;
    await fetch(`/api/lgtm/items?slug=${encodeURIComponent(slug)}&item=${encodeURIComponent(rawItemId)}`, {
      method: "DELETE",
    });
    refresh();
  }
  function onStarted() {
    pendingReviewOpen.current = true;
    refresh();
  }

  const spec = data ? buildTabSpec(data, mountedDocIds.current) : { docs: [], mounted: [], showDiff: false, showWalkthrough: false, showStart: false };

  return (
    <div
      id="modal"
      data-tab={isReview ? "review" : "terminal"}
      data-tabId={activeTab}
      // Backdrop click (on #modal itself, outside .modal-card) closes — matches
      // the vanilla `if (e.target === modal) closeModal()`.
      onClick={(e) => { if (e.currentTarget === e.target) closeModal(); }}
    >
      <div class="modal-card">
        <div class="modal-head">
          <div class="modal-title-block">
            <ModalTitle data={data} target={target} onRename={onRename} onAutoRename={onAutoRename} />
            <ModalSubtitle data={data} />
          </div>
          <TabStrip
            spec={spec}
            activeTab={activeTab}
            onSwitch={switchTab}
            onMount={mountDoc}
            onUnmount={unmountDoc}
            onRemove={removeItem}
          />
          <div class="modal-actions">
            <button id="modal-close" onClick={closeModal}>×</button>
          </div>
        </div>
        <div class="modal-body">
          {/* Terminal pane stays mounted across tab switches (CSS hides it on
              review); keyed on target so a pane switch reconnects. */}
          <div id="modal-terminal-pane" class="modal-pane">
            <Terminal
              key={target}
              id="modal-xterm"
              class="modal-xterm"
              target={target}
              onMdLink={onMdLink}
              onPaste={onPaste}
            />
            {data && <Sidebar data={data} onRefresh={refresh} />}
          </div>
          <div id="modal-review-pane" class="modal-pane modal-review-pane">
            <div id="modal-review-content">
              {/* Rendered whenever data exists (not gated on the active tab) so
                  the LGTM iframe persists across terminal↔review toggles. CSS
                  (#modal[data-tab]) controls which pane is visible. */}
              {data && (
                <ReviewPane key={target} data={data} activeTab={activeTab} onStarted={onStarted} />
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function closeModal() {
  activeTarget.value = null;
  modalRenaming.value = false;
  modalAutoRenaming.value = false;
}

export function Modal() {
  const target = activeTarget.value;

  // Register the public opener so Card/Detail (via poll.js's openModal bridge)
  // route to the Preact modal. Runs once.
  useEffect(() => {
    window.__periscopeOpenModal = openModal;
    return () => {
      if (window.__periscopeOpenModal === openModal) delete window.__periscopeOpenModal;
    };
  }, []);

  // Escape closes the modal (LIFO; the docs dropdown pushes on top of this).
  useEscape(closeModal, !!target);

  // body.modal-open locks scroll (CSS: `body.modal-open { overflow: hidden }`).
  useEffect(() => {
    if (target) document.body.classList.add("modal-open");
    else document.body.classList.remove("modal-open");
    return () => document.body.classList.remove("modal-open");
  }, [target]);

  // Forwarded Escape + channel push from the embedded LGTM iframe.
  useEffect(() => {
    function onMessage(e) {
      if (e.data?.type === "lgtm-embedded-escape" && activeTarget.value) {
        closeModal();
        return;
      }
      if (e.data?.type === "lgtm-notify-claude") {
        // pane_id rides on the last /api/pane poll (tracked in lastModalPaneId);
        // the iframe can't know it, so we resolve + deliver over our channel.
        const content = e.data.content;
        const pane = lastModalPaneId;
        if (!content || !pane) return;
        fetch(`/api/channel/push?pane=${encodeURIComponent(pane)}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ content }),
        }).catch(() => {});
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  if (!target) return null;

  // Keyed on target so re-opening a different pane fully resets ModalBody
  // state (tabs, mounted docs, poll) — and the <Terminal> inside it reconnects.
  return <ModalBody key={target} target={target} />;
}
