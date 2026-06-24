// State-filter dropdown. Writes the `currentFilter` signal (the single read
// model the grid/rail filter off). Ported from the #filters dropdown in
// index.html + the filter-button wiring in app.js:135-146. The label tracks
// the active filter; clicking the toggle reveals the eight options.
//
// CSS classes preserved verbatim (.tb-dd, .tb-dd-toggle, .tb-dd-chev,
// .tb-dd-menu, .tb-dd-item, .filter-btn) so styles.css is unchanged. The
// dropdown's open/close + outside-click + Escape mechanics mirror app.js's
// openDropdown/closeDropdown, but Escape goes through the LIFO useEscape hook
// instead of the old pushEscape/popEscape registry.
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { currentFilter } from "../store.js";
import { track } from "../track.js";

const FILTERS = [
  { key: "all", label: "all" },
  { key: "needs-input", label: "needs input" },
  { key: "working", label: "working" },
  { key: "done", label: "done" },
  { key: "idle", label: "idle" },
  { key: "claude", label: "claude" },
  { key: "shell", label: "shells" },
  { key: "ci-bad", label: "CI ✗" },
];

export function FilterBar() {
  const [open, setOpen] = useState(false);
  const ddRef = useRef(null);
  const active = currentFilter.value;
  const activeLabel = (FILTERS.find((f) => f.key === active) || FILTERS[0]).label;

  const close = useCallback(() => setOpen(false), []);
  useEscape(close, open);

  // Outside-click closes the menu. Deferred-attach isn't needed here because
  // the toggle's onClick stops propagation, so the opening click never
  // reaches this document listener.
  useEffect(() => {
    if (!open) return;
    function onDocClick(e) {
      if (ddRef.current && !ddRef.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  function pick(key) {
    currentFilter.value = key;
    track("filter.use");
    setOpen(false);
  }

  return (
    <div class="tb-dd" data-dd="filter" ref={ddRef}>
      <button
        type="button"
        class="filter-btn tb-dd-toggle"
        id="filter-dd-toggle"
        aria-haspopup="menu"
        aria-expanded={open ? "true" : "false"}
        title="filter by state"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        <span id="filter-dd-label">{activeLabel}</span>
        <span class="tb-dd-chev" aria-hidden="true">▾</span>
      </button>
      <div class="tb-dd-menu" id="filter-dd-menu" role="menu" hidden={!open}>
        {FILTERS.map((f) => (
          <button
            key={f.key}
            data-filter={f.key}
            class={`tb-dd-item${f.key === active ? " active" : ""}`}
            role="menuitem"
            onClick={() => pick(f.key)}
          >
            {f.label}
          </button>
        ))}
      </div>
    </div>
  );
}
