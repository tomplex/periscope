// Shared mounting helper for the live xterm. Wraps terminal.js so callers
// (modal.js, detail.js) don't have to repeat the
// setTerminalContainer + setTerminalLinkCallback + paste-handler dance.
//
// One xterm instance lives in the app at a time (terminal.js's invariant).
// mount() retargets it onto a new container; unmount() tears it down.

import {
  setTerminalContainer,
  setTerminalLinkCallback,
  startLiveTerminal,
  stopLiveTerminal,
  refitTerminal,
} from './terminal.js';

let activePasteHandler = null;
let activeContainer = null;

/**
 * Mount the live terminal for `target` into `container`.
 * @param {HTMLElement} container
 * @param {string} target — tmux target spec (e.g. "session:0.0")
 * @param {Object} opts
 * @param {(rawPath: string) => void} [opts.onMdLink]
 * @param {(event: ClipboardEvent) => void} [opts.onPaste] — capture-phase paste hook
 */
export function mountTerminal(container, target, opts = {}) {
  unmountTerminal();  // tear down any previous mount
  setTerminalContainer(container);
  setTerminalLinkCallback(opts.onMdLink || null);
  if (opts.onPaste) {
    activePasteHandler = opts.onPaste;
    container.addEventListener("paste", activePasteHandler, true);
  }
  activeContainer = container;
  startLiveTerminal(target);
  // Defensive refit on the next animation frame: when a view-switch just
  // un-hid the container, layout queries during startLiveTerminal can
  // race the browser's layout pass and produce stale cols/rows. Asking
  // the browser to refit on the next frame catches that case. Two rAFs
  // because the first one fires before the post-paint relayout settles
  // when transitioning from `display: none` → visible.
  requestAnimationFrame(() => requestAnimationFrame(refitTerminal));
}

export function unmountTerminal() {
  stopLiveTerminal();
  if (activeContainer && activePasteHandler) {
    activeContainer.removeEventListener("paste", activePasteHandler, true);
  }
  activePasteHandler = null;
  activeContainer = null;
  setTerminalContainer(null);
  setTerminalLinkCallback(null);
}
