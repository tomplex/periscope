// The pane modal — one file (tab strip + sidebar inline, per the structure
// decision; NOT exploded into <TabStrip>/<Sidebar>). Ported from
// static/modal.js. Renders its own `#modal` element inside the Preact root so
// the CSS contract is preserved verbatim: `#modal[data-tab]` keys the visible
// pane, `#modal-terminal-pane` / `#modal-review-pane` / `#modal-xterm` /
// `#modal-side` ids are unchanged, and every `.modal-*` / `.timeline-*` /
// `.pr-*` / `.tag-*` / `.activity-*` class name carries over.
//
// Lifecycle (Task 6 must-not-drop):
//  - openModal(target, opts) sets the shared `modalTarget` signal (also set by
//    <Detail>.selectPane — coupling #5) and a 1.5s /api/pane header poll runs
//    while open. The poll does NOT commit while `modalRenaming` /
//    `modalAutoRenaming` hold.
//  - <Terminal> is keyed per-open (modalTarget) so it mounts once per open and
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
import { modalTarget, modalRenaming, modalAutoRenaming } from "../store.js";
import { targetQuery, apiCall, prUrl, rewriteLgtmHost } from "../util.js";
import { Terminal } from "../terminal/Terminal.jsx";
import { writeTerminalLine } from "../terminal/terminalCore.js";
import { poll } from "../poll.js";
import { Inspector } from "../inspector/Inspector.jsx";

const MODAL_POLL_MS = 1500;
const MOUNTED_DOCS_KEY_PREFIX = "periscope-lgtm-mounted:";

// Open request, mirrors the vanilla openModal(target, {tab}) signature. A
// signal so the singleton <Modal> reacts; the public opener (registered on
// window so Card/Detail can call it) just writes modalTarget + this.
const openOpts = signal({});

// Public opener. Card.jsx and (later) Detail call this via
// window.__periscopeOpenModal (see poll.js openModal bridge). Setting
// modalTarget mounts the modal; opts.tab === "review" auto-switches to the
// first LGTM tab once data arrives.
export function openModal(target, opts = {}) {
  openOpts.value = opts || {};
  modalTarget.value = target;
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
// Keyed by modalTarget at the <Modal> level so a pane switch tears it down.
// Mounted whenever the modal is open (NOT gated on the active tab) so the
// iframe survives terminal↔review toggles — the vanilla path left the iframe
// in #modal-review-content and only CSS-hid it on the Terminal tab, so the SSE
// connection stayed up across tab switches. We mirror that: the iframe element
// persists; its src is reassigned ONLY when the (review) tab/doc key changes.
function ReviewPane({ data, activeTab, onStarted }) {
  // The LGTM iframe is created imperatively ONCE and parked inside a host div
  // that Preact owns. Preact never reconciles the iframe node itself, so the
  // 1.5s poll re-render can't move/recreate it — moving an iframe in the DOM
  // reloads it in a browser (WKWebView tolerated it, which is why the Tauri
  // app looked fine; Chromium/WebKit-in-browser don't). This mirrors vanilla's
  // static iframe that JS only ever set .src on. `display:contents` on the host
  // keeps the iframe laid out exactly as a direct child of #modal-review-content.
  const hostRef = useRef(null);
  const iframeRef = useRef(null);
  const mountedSrc = useRef(null);
  const everHadSession = useRef(false);

  let url = null;
  const lgtm = data?.lgtm;
  const hasSession = !!(lgtm?.slug && lgtm?.url);
  if (hasSession) everHadSession.current = true;
  if (hasSession) {
    const baseUrl = rewriteLgtmHost(lgtm.url);
    if (activeTab === "lgtm:walkthrough") {
      url = `${baseUrl}?embedded=1&view=walkthrough&host=periscope`;
    } else if (activeTab.startsWith("lgtm:")) {
      const itemId = activeTab.slice(5);
      url = `${baseUrl}?embedded=1&item=${encodeURIComponent(itemId)}&host=periscope`;
    }
  }

  // Create + append the iframe once the host div exists (guarded; runs every
  // commit but only builds the node a single time).
  useEffect(() => {
    if (!hostRef.current || iframeRef.current) return;
    const f = document.createElement("iframe");
    f.title = "LGTM review";
    f.setAttribute("referrerpolicy", "no-referrer");
    hostRef.current.appendChild(f);
    iframeRef.current = f;
  });

  // Reassign src ONLY when the review url changes — never per poll.
  useEffect(() => {
    if (url && iframeRef.current && mountedSrc.current !== url) {
      iframeRef.current.src = url;
      mountedSrc.current = url;
    }
  }, [url]);

  if (!everHadSession.current && activeTab === "lgtm-start") {
    return <StartReview data={data} onStarted={onStarted} />;
  }
  // Once a session has been seen, render the stable host (so the iframe inside
  // is never torn down by a transient poll gap or a tab switch).
  if (everHadSession.current) {
    return <div ref={hostRef} class="modal-review-iframe-host" style="display:contents" />;
  }
  if (activeTab === "lgtm-start") {
    return <StartReview data={data} onStarted={onStarted} />;
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

  async function onPaste(e) {
    if (!modalTarget.value) return;
    const items = e.clipboardData?.items || [];
    for (const item of items) {
      if (item.kind !== "file" || !item.type.startsWith("image/")) continue;
      const blob = item.getAsFile();
      if (!blob) continue;
      e.preventDefault();
      e.stopPropagation();
      try {
        const res = await fetch(`/api/paste-image?${targetQuery(modalTarget.value)}`, {
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
              onPaste={onPaste}
            />
            {data && (
              <Inspector
                data={data}
                onRefresh={refresh}
                containerId="modal-side"
                containerClass="modal-side"
                idPrefix="modal"
              />
            )}
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
  modalTarget.value = null;
  modalRenaming.value = false;
  modalAutoRenaming.value = false;
}

export function Modal() {
  const target = modalTarget.value;

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
      if (e.data?.type === "lgtm-embedded-escape" && modalTarget.value) {
        closeModal();
        return;
      }
      if (e.data?.type === "lgtm-notify-claude") {
        // pane_id rides on the last /api/pane poll (tracked in lastModalPaneId);
        // the iframe can't know it, so we resolve + deliver over our channel.
        const content = e.data.content;
        const pane = lastModalPaneId;
        if (!content || !pane) return;
        fetch(`/api/channel/push?pane_id=${encodeURIComponent(pane)}`, {
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
