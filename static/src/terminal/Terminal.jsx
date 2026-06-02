// Thin ref+useEffect wrapper over the imperative xterm/WS core
// (terminalCore.js). This is the ONLY Preact-aware part of the terminal — the
// xterm instance, WebSocket, reconnect FSM, fit/resize, and paste/link
// handlers all live in the core and stay imperative (CLAUDE.md invariants
// #3/#4). Do not move that logic here.
//
// Lifecycle: the empty-deps effect mounts the live terminal ONCE per component
// instance and tears it down on unmount. Call sites KEY this component on the
// pane's pid:
//
//   <Terminal key={pid} target={target} onPaste={handlePaste} />
//
// so re-selecting the same pane preserves this instance (reproduces
// detail.js's `sameMount` pid-keyed skip — reconnect, not remount) and
// selecting a different pane unmounts+remounts → a fresh connect. The modal,
// which is keyed per-open, mounts once per open via the same empty-deps effect.
//
// The default class is `modal-xterm` (matches the modal's styles.css contract);
// the split-view <Detail> passes `class="detail-xterm"`. Neither class is
// renamed — both already exist in styles.css.
import { useRef, useEffect } from "preact/hooks";
import { mountTerminal, unmountTerminal } from "./terminalCore.js";

export function Terminal({ target, onMdLink, onPaste, class: className = "modal-xterm", id }) {
  const ref = useRef(null);
  useEffect(() => {
    mountTerminal(ref.current, target, { onMdLink, onPaste });
    // mountTerminal self-unmounts the prior mount on its next call, so the
    // single cleanup here is enough — don't double-tear-down.
    return unmountTerminal;
  }, []); // empty deps: mount ONCE for this component instance (pid-keyed at call site)
  // `id` lets the modal carry `#modal-xterm` (styles.css keys flex/padding/
  // background off that id); the split-view <Detail> can omit it.
  return <div ref={ref} id={id} class={className} />;
}
