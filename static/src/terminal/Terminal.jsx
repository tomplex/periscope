// Thin ref+useEffect wrapper over the imperative xterm/WS core
// (terminalCore.js). This is the ONLY Preact-aware part of the terminal —
// the xterm instance, WebSocket, reconnect FSM, fit/resize, paste, and
// link handlers all live in the core and stay imperative.
//
// Lifecycle: the empty-deps effect mounts the live terminal ONCE per
// component instance and tears it down on unmount. Call sites KEY this
// component on the pane's pid so re-selecting the same pane preserves
// this instance.
//
// Cmd+F opens a search bar overlay above the terminal. The bar is
// rendered here (Preact) but the actual search work is in terminalCore
// (xterm.js addon-search). Esc closes the bar via useEscape (LIFO).
import { useRef, useEffect, useState, useCallback } from "preact/hooks";
import {
  mountTerminal, unmountTerminal,
  searchNext, searchPrev, clearSearch,
} from "./terminalCore.js";
import { useEscape } from "../hooks/useEscape.js";

export function Terminal({ target, onMdLink, onPaste, class: className = "modal-xterm", id }) {
  const hostRef = useRef(null);
  const inputRef = useRef(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");

  useEffect(() => {
    mountTerminal(hostRef.current, target, { onMdLink, onPaste });
    return unmountTerminal;
  }, []); // empty deps — mount ONCE per component instance (pid-keyed at call site)

  // Cmd+F opens the search bar. Use a window-level listener so the user
  // doesn't have to focus the terminal first.
  useEffect(() => {
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key === "f") {
        e.preventDefault();
        setSearchOpen(true);
        requestAnimationFrame(() => inputRef.current?.focus());
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const close = useCallback(() => {
    setSearchOpen(false);
    setQuery("");
    clearSearch();
  }, []);

  useEscape(close, searchOpen);

  function onSubmit(e) {
    e.preventDefault();
    if (!query) return;
    if (e.shiftKey) searchPrev(query); else searchNext(query);
  }

  return (
    <div class="terminal-wrap">
      {searchOpen && (
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
      )}
      <div ref={hostRef} id={id} class={className} />
    </div>
  );
}
