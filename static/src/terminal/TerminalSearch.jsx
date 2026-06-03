// Cmd+F search bar overlay for the terminal. Lives at the <Detail> level
// (NOT inside <Terminal>) so it doesn't introduce extra layout layers
// around the xterm host — that broke FitAddon's measurement and caused
// scrollback reflow at different widths after mount.
//
// The bar is positioned absolute against #detail (which is already
// position: relative); it floats top-right of the terminal area when
// open. Esc closes via shared useEscape (LIFO). Only meaningful for
// terminal-mode Claude panes; the caller gates rendering.
import { useEffect, useRef, useState, useCallback } from "preact/hooks";
import { searchNext, searchPrev, clearSearch } from "./terminalCore.js";
import { useEscape } from "../hooks/useEscape.js";

export function TerminalSearch() {
  const inputRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  useEffect(() => {
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key === "f") {
        e.preventDefault();
        setOpen(true);
        requestAnimationFrame(() => inputRef.current?.focus());
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
    clearSearch();
  }, []);

  useEscape(close, open);

  function onSubmit(e) {
    e.preventDefault();
    if (!query) return;
    if (e.shiftKey) searchPrev(query); else searchNext(query);
  }

  if (!open) return null;
  return (
    <form class="term-search" onSubmit={onSubmit}>
      <input
        ref={inputRef}
        class="term-search-input"
        value={query}
        placeholder="find in terminal"
        onInput={(e) => { setQuery(e.currentTarget.value); }}
      />
      <button type="button" class="term-search-btn"
              title="Previous (Shift+Enter)"
              onClick={() => query && searchPrev(query)}>‹</button>
      <button type="button" class="term-search-btn"
              title="Next (Enter)"
              onClick={() => query && searchNext(query)}>›</button>
      <button type="button" class="term-search-btn"
              title="Close (Esc)"
              onClick={close}>✕</button>
    </form>
  );
}
