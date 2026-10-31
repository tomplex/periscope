// The dashboard header: brand + usage pill + counts, then the filters nav
// (history link, state filter, + new, conditional send-bulk/collapse-all, ⋯
// overflow, alerts toggle, view switch). Ported from the <header> markup in
// index.html + the wiring in app.js. CSS classes (.periscope-header, .topbar,
// .brand, .meta, .filters, .filter-btn, .is-action, .filters-sep,
// count-* spans, .tb-dd*) are preserved verbatim.
//
// Scope note (Task 4 = chrome): this owns the filter, view switch, usage
// pill, counts, history link, and the global ⌘/ + Tab keybindings. The +new
// items, ⋯ overflow actions, send-bulk, collapse-all, and alerts toggle are
// rendered structurally so the layout is complete, but their click handlers
// land with their owning surfaces (Grid → Task 5; secondary modals + alerts →
// Task 8). They are inert placeholders until then; the vanilla path still owns
// those surfaces while only chrome is Preact-mounted.
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { multiAccount } from "../accounts.js";
import { useEscape } from "../hooks/useEscape.js";
import { unresolvedTotal } from "../review/walkthrough.js";
import { selectReviewsQueue } from "../split/alertFeed.js";
import { reviews, windows } from "../store.js";
import { track } from "../track.js";
import { FilterBar } from "./FilterBar.jsx";
import { PanelToggles } from "./PanelToggles.jsx";
import { ResourcePill } from "./ResourcePill.jsx";
import { SpawnAccountPicker } from "./SpawnAccountPicker.jsx";
import { SpawnEffortPicker } from "./SpawnEffortPicker.jsx";
import { SpawnModelPicker } from "./SpawnModelPicker.jsx";
import { Tamagotchi } from "./Tamagotchi.jsx";
import { UpdatePill } from "./UpdatePill.jsx";
import { UsagePill } from "./UsagePill.jsx";

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
  // ⌘/ (or ⌃/) → /history. The Tab keybinding (grid ↔ split cycle)
  // was retired with the grid view.
  useEffect(() => {
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key === "/") {
        track("key.shortcut", { key: "cmd+/" });
        e.preventDefault();
        window.location.href = "/history";
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
      document.documentElement.style.setProperty("--header-h", `${el.offsetHeight}px`);
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    // data-pin-rows: the spawn pins stack three rows when the account row
    // shows (SpawnAccountPicker), which makes the header tall enough for the
    // tamagotchi at 5x (styles.css, .tama).
    <header class="periscope-header" ref={headerRef} data-pin-rows={multiAccount() ? 3 : 2}>
      <div class="topbar">
        <span class="brand">
          periscope<i class="brand-cursor"></i>
        </span>
        <UsagePill />
        <ResourcePill />
        {/* The spawn pins stack as one block to the right of the usage
            meters (.usage flexes, so this block sits at the meters' far edge):
            account, model, effort, labels aligned so the chips read as a
            3-row table. */}
        <div class="spawn-pins">
          <SpawnAccountPicker />
          <SpawnModelPicker />
          <SpawnEffortPicker />
        </div>
        <UpdatePill />
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

        {/* + new — opens the unified open omnibox. */}
        <button type="button" class="filter-btn is-action" id="new-open-btn"
                title="open a repo, worktree, or PR"
                onClick={() => window.__periscopeOpenOmnibox?.()}>
          + new
        </button>

        {/* Reviews queue — every branch waiting on review, across tracks. */}
        <button type="button" class="filter-btn is-action" id="reviews-btn"
                title="open branches, walkthroughs, review comments"
                onClick={selectReviewsQueue}>
          ◉ reviews{unresolvedTotal(reviews.value) ? <span class="rail-review-badge">{unresolvedTotal(reviews.value)}</span> : null}
        </button>

        {/* ⋯ overflow: low-frequency actions. */}
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

        <span class="filters-sep"></span>
        <PanelToggles />

        {/* Counts live at the row's far end (.meta is margin-left:auto): the
            top bar's right side is the tamagotchi's, and this row has the room. */}
        <div class="meta">
          <Counts />
        </div>
      </nav>
      {/* Spans both header rows in the top-right corner (styles.css), so the
          4x scene adds no height to either. */}
      <Tamagotchi />
    </header>
  );
}
