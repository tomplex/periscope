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
