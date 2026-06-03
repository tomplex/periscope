// Thin ref+useEffect wrapper over the imperative xterm/WS core
// (terminalCore.js). This is the ONLY Preact-aware part of the terminal —
// the xterm instance, WebSocket, reconnect FSM, fit/resize, and paste
// handlers all live in the core and stay imperative.
//
// Lifecycle: the empty-deps effect mounts the live terminal ONCE per
// component instance and tears it down on unmount. Call sites KEY this
// component on the pane's pid:
//
//   <Terminal key={pid} target={target} onPaste={handlePaste} />
//
// so re-selecting the same pane preserves this instance (reconnect, not
// remount) and selecting a different pane unmounts+remounts → a fresh
// connect.
//
// The Cmd+F search bar lives at the <Detail> level as a sibling overlay
// (TerminalSearch component), NOT inside this wrapper — wrapping the
// terminal host in extra flex/relative layers shifts FitAddon's
// measurements and causes mid-mount tmux reflow (scrollback at one width,
// new output at another). Keep this wrapper a pure pass-through.
import { useRef, useEffect } from "preact/hooks";
import { mountTerminal, unmountTerminal } from "./terminalCore.js";

export function Terminal({ target, onPaste, class: className = "modal-xterm", id }) {
  const ref = useRef(null);
  useEffect(() => {
    mountTerminal(ref.current, target, { onPaste });
    // mountTerminal self-unmounts the prior mount on its next call, so the
    // single cleanup here is enough — don't double-tear-down.
    return unmountTerminal;
  }, []); // empty deps: mount ONCE for this component instance (pid-keyed at call site)
  return <div ref={ref} id={id} class={className} />;
}
