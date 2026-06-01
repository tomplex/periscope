// The dashboard header: brand + usage pill + counts, then the filters nav
// (history link, state filter, + new, conditional send-bulk/collapse-all, ⋯
// overflow, alerts toggle, view switch). Ported from the <header> markup in
// index.html + the wiring in app.js. CSS classes (.periscope-header, .topbar,
// .brand, .meta, .filters, .filter-btn, .is-action, .filters-sep,
// .filters-spacer, count-* spans, .tb-dd*) are preserved verbatim.
//
// Scope note (Task 4 = chrome): this owns the filter, view switch, usage
// pill, counts, history link, and the global ⌘/ + Tab keybindings. The +new
// items, ⋯ overflow actions, send-bulk, collapse-all, and alerts toggle are
// rendered structurally so the layout is complete, but their click handlers
// land with their owning surfaces (Grid → Task 5; secondary modals + alerts →
// Task 8). They are inert placeholders until then; the vanilla path still owns
// those surfaces while only chrome is Preact-mounted.
import { useEffect, useState, useRef, useCallback } from "preact/hooks";
import { windows, view } from "../store.js";
import { useEscape } from "../hooks/useEscape.js";
import * as prefs from "../prefs.js";
import { UsagePill } from "./UsagePill.jsx";
import { FilterBar } from "./FilterBar.jsx";
import { ViewSwitch, nextView } from "./ViewSwitch.jsx";

// One dropdown open at a time among the toggle-style menus (+ new, ⋯). The
// state-filter dropdown manages itself in <FilterBar>.
function Dropdown({ id, toggleClass, toggleLabel, title, children }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const close = useCallback(() => setOpen(false), []);
  useEscape(close, open);
  useEffect(() => {
    if (!open) return;
    function onDocClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);
  return (
    <div class="tb-dd" ref={ref}>
      <button
        type="button"
        class={toggleClass}
        id={id}
        aria-haspopup="menu"
        aria-expanded={open ? "true" : "false"}
        title={title}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        {toggleLabel}
      </button>
      <div class="tb-dd-menu" role="menu" hidden={!open} onClick={() => setOpen(false)}>
        {children}
      </div>
    </div>
  );
}

function Counts() {
  const ws = windows.value;
  const total = ws.length;
  const needsInput = ws.filter((w) => w.state === "needs-input").length;
  const working = ws.filter((w) => w.state === "working").length;
  const done = ws.filter((w) => w.state === "done").length;
  const idle = ws.filter((w) => w.state === "idle").length;
  const Sep = () => <span class="count-sep">·</span>;
  // Lead with needs-input — the only count that means "drop what you're
  // doing"; renders only when nonzero. Each count is its own classed span so
  // CSS colors them by status.
  return (
    <span id="counts">
      <span>
        <b>{total}</b> windows
      </span>
      {needsInput > 0 && (
        <>
          {" "}
          <Sep /> <span class="count-needs">{needsInput} needs input</span>
        </>
      )}{" "}
      <Sep /> <span class="count-working">{working} working</span> <Sep />{" "}
      <span class="count-done">{done} done</span> <Sep />{" "}
      <span class="count-idle">{idle} idle</span>
    </span>
  );
}

export function Header() {
  // Global keybindings owned by the chrome surface:
  //   ⌘/ (or ⌃/)  → /history
  //   Tab          → cycle grid ↔ split (2-way now; stream cut)
  // Suppressed while an input/textarea/contenteditable is focused (so Tab
  // through form fields keeps working) and while the modal is open (its own
  // keybindings own that surface). One global listener — not per-component.
  useEffect(() => {
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key === "/") {
        e.preventDefault();
        window.location.href = "/history";
        return;
      }
      const tag = (document.activeElement?.tagName || "").toLowerCase();
      const editable =
        tag === "input" || tag === "textarea" || document.activeElement?.isContentEditable;
      const modalEl = document.getElementById("modal");
      const modalOpen = modalEl && !modalEl.classList.contains("hidden");
      if (
        e.key === "Tab" &&
        !e.metaKey &&
        !e.ctrlKey &&
        !e.altKey &&
        !editable &&
        !modalOpen
      ) {
        e.preventDefault();
        const next = nextView(view.value);
        view.value = next;
        // ViewSwitch's effect mirrors the change onto body[data-view].
        prefs.setView(next);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Keep --header-h synced to THIS (visible, Preact) header's height. The
  // vanilla header is display:none in Preact mode, so querySelector(".periscope-
  // header") in Alerts would measure 0 and collapse #split-view to top:0
  // (overlapping the header). Owning it here from a ref is authoritative.
  const headerRef = useRef(null);
  useEffect(() => {
    const el = headerRef.current;
    if (!el) return;
    const apply = () =>
      document.documentElement.style.setProperty("--header-h", el.offsetHeight + "px");
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <header class="periscope-header" ref={headerRef}>
      <div class="topbar">
        <span class="brand">
          periscope<i class="brand-cursor"></i>
        </span>
        <UsagePill />
        <div class="meta">
          <Counts />
          <span id="last-update"></span>
        </div>
      </div>
      <nav class="filters" id="filters">
        <a
          href="/history"
          class="filter-btn is-action"
          id="history-link"
          title="search conversation history (⌘/)"
        >
          🔍 history <span class="ps-kbd" style="margin-left:4px">⌘</span>
          <span class="ps-kbd">/</span>
        </a>
        <span class="filters-sep"></span>

        <FilterBar />

        <span class="filters-sep"></span>

        {/* + new dropdown. Item handlers (new session/project/review PR) land
            with the secondary-modal port in Task 8; inert here. */}
        <Dropdown
          id="new-dd-toggle"
          toggleClass="filter-btn is-action tb-dd-toggle"
          toggleLabel={
            <>
              + new <span class="tb-dd-chev" aria-hidden="true">▾</span>
            </>
          }
          title="create new"
        >
          <button id="new-session" class="tb-dd-item" role="menuitem" title="create a new tmux session">
            + session
          </button>
          <button
            id="new-project-btn"
            class="tb-dd-item"
            role="menuitem"
            title="create a new project (new worktree + tmux session)"
          >
            + project
          </button>
          <button
            id="review-pr-btn"
            class="tb-dd-item"
            role="menuitem"
            title="review a PR by number — creates a worktree at pr-<N> + claude session"
          >
            📋 review PR
          </button>
        </Dropdown>

        {/* Conditional buttons (send-bulk, collapse-all) are grid-owned —
            their visibility + handlers land in Task 5. Rendered hidden so the
            layout slot exists; the vanilla grid still drives them while only
            chrome is Preact-mounted. */}
        <button
          id="send-bulk"
          class="filter-btn is-action"
          hidden
          title="paste text + Enter into every visible pane (filter first to scope)"
        ></button>
        <button id="toggle-all" class="filter-btn is-action" title="collapse or expand all sessions"></button>

        {/* ⋯ overflow: low-frequency actions. Handlers (cleanup/settings/
            commands) land in Task 8; inert here. */}
        <Dropdown
          id="more-dd-toggle"
          toggleClass="filter-btn is-action tb-dd-toggle"
          toggleLabel="⋯"
          title="more actions"
        >
          <button
            id="cleanup-btn"
            class="tb-dd-item"
            role="menuitem"
            title="archive worktrees with merged PRs, deleted branches, or idle activity"
          >
            🧹 cleanup
          </button>
          <button id="settings-btn" class="tb-dd-item" role="menuitem" title="settings">
            🛠 settings
          </button>
          <button
            id="open-commands"
            class="tb-dd-item"
            role="menuitem"
            title="edit + claude / + shell / + ... commands"
          >
            ⚙ commands
          </button>
        </Dropdown>

        <span class="filters-spacer"></span>

        {/* Alerts toggle — feed + badge land with Alerts in Task 8; inert
            here. */}
        <button
          id="alerts-toggle"
          class="filter-btn is-action"
          aria-pressed="false"
          title="toggle notifications panel — cross-pane feed"
        >
          <span class="alerts-toggle-label">🔔 notifications</span>
          <span class="alerts-badge" id="alerts-badge" hidden>
            0
          </span>
        </button>

        <ViewSwitch />
      </nav>
    </header>
  );
}
