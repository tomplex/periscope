// The pane menu, one per rail: a pane card's ⋯ button or a right-click on the
// card opens it. It holds what the card's hover leaves out (the tab colour,
// moving the session to another account, freezing the name, opening the
// worktree in the editor) and pin and kill again, for the keyboard. Keys
// follow paneMenuKey (paneMenuItems.js). While a swatch is under the pointer
// or the cursor, the card already shows that colour (shownTabColour), so you
// see it before you pick. The menu holds focus while open, so its keys never
// reach a terminal: it closes when focus leaves it, and gives focus back to
// whatever had it when it opened.
import { signal } from "@preact/signals";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { moveAccountTarget } from "../accounts.js";
import { useEscape } from "../hooks/useEscape.js";
import * as prefs from "../prefs.js";
import { editor, windows } from "../store.js";
import { TAB_COLOUR_NAME_MAX, TAB_COLOURS } from "../tabColours.js";
import { paneLabel } from "../util.js";
import { canOpenInEditor, openInEditorTitle } from "./openInEditor.js";
import { paneMenuItems, paneMenuKey, stopPreview, SWATCHES } from "./paneMenuItems.js";

// { pid, w, rect, actions, returnFocus } while open. `rect` is what it opens
// from: the ⋯ button's box, or the right-click's point. `actions` are the
// card's handlers: onTogglePin, onMoveAccount, onToggleNamePin,
// onOpenInEditor and onClose.
const menu = signal(null);
// { pid, colour } while a swatch is under the pointer or the cursor; a null
// colour previews "No colour".
const preview = signal(null);

export function openPaneMenu(w, rect, actions) {
  menu.value = { pid: w.pid, w, rect, actions, returnFocus: document.activeElement };
  preview.value = null;
}

// `refocus` is false when focus already went somewhere else on its own.
export function closePaneMenu({ refocus = true } = {}) {
  const m = menu.value;
  menu.value = null;
  preview.value = null;
  if (refocus) m?.returnFocus?.focus?.();
}

export function isPaneMenuOpen(pid) {
  return menu.value?.pid === pid;
}

// The colour a pane's card shows: the menu's preview while it is on that
// pane, otherwise the saved one.
export function shownTabColour(pid) {
  const p = preview.value;
  return p && p.pid === pid ? p.colour : prefs.getTabColour(pid);
}

// The classes that tint a card in colour `id` (styles.css); none for no colour.
export function tabColourClass(id) {
  return id ? ` tab-colour tab-colour-${id}` : "";
}

export function PaneMenu() {
  const m = menu.value;
  return m ? <Menu key={m.pid} m={m} /> : null;
}

// Below what it opened from, or above it when it doesn't fit there; when
// neither side fits, the roomier one. Never above the rail's top: the sticky
// header sits over everything there.
function placeMenu(rect, box) {
  const minTop = (document.getElementById("rail")?.getBoundingClientRect().top ?? 0) + 4;
  const maxTop = innerHeight - 8 - box.height;
  const below = rect.bottom + 4;
  const above = rect.top - box.height - 4;
  let top = below;
  if (below > maxTop) top = above >= minTop || rect.top - minTop > innerHeight - rect.bottom ? above : below;
  return {
    left: Math.max(8, Math.min(rect.left, innerWidth - box.width - 8)),
    top: Math.max(minTop, Math.min(top, maxTop)),
  };
}

const tabs = (n) => `${n} ${n === 1 ? "tab" : "tabs"}`;

function Menu({ m }) {
  const ref = useRef(null);
  const [cursor, setCursor] = useState(-1);
  const [renaming, setRenaming] = useState(false);
  const [pos, setPos] = useState(null);
  // The live row, so the labels follow a poll that lands while it is open.
  const w = (windows.value || []).find((x) => x.pid === m.pid) || m.w;
  const label = paneLabel(w);
  const current = prefs.getTabColour(m.pid) || null;
  const names = prefs.getTabColourNames();
  const counts = {};
  for (const x of windows.value || []) {
    const id = prefs.getTabColour(x.pid);
    if (id) counts[id] = (counts[id] || 0) + 1;
  }
  const move = moveAccountTarget(w);
  const editorName = canOpenInEditor(w, editor.value) ? editor.value : null;
  const items = paneMenuItems({
    pinned: prefs.getPinnedPids().includes(m.pid),
    namePinned: w.name_pinned,
    move: move?.label,
    editor: editorName,
  });
  // Move names the destination, never the current account. Freeze name is
  // mostly the release: a rename freezes the name on its own. The editor
  // opens the worktree's root (the server resolves the git toplevel), not the
  // cwd, so a pane in a subdirectory still opens the whole project.
  const titles = {
    move: move && `re-open this session on Claude account ${move.label}: spawns a resumed pane there and leaves this one running`,
    name: w.name_pinned ? "name frozen: choose it to let periscope rename this tab again" : "freeze this name, so periscope won't rename this tab",
    editor: editorName && openInEditorTitle(w, editorName),
    kill: "kill this tab's tmux window (asks first)",
  };

  // Measured, so the first frame is unseen; placed, then focused (a
  // visibility:hidden element can't take focus).
  useLayoutEffect(() => { setPos(placeMenu(m.rect, ref.current.getBoundingClientRect())); }, [m]);
  useEffect(() => { if (pos) ref.current.focus(); }, [pos]);

  // A click elsewhere, a wheel over the rail or a resize closes it: its place
  // would be stale. The wheel, not `scroll`: the browser's scroll anchoring
  // moves the rail on its own when a poll reshapes the rows above.
  useEffect(() => {
    const rail = document.getElementById("rail");
    const outside = (e) => { if (!ref.current?.contains(e.target)) finish(); };
    document.addEventListener("pointerdown", outside, true);
    rail?.addEventListener("wheel", outside, { passive: true });
    window.addEventListener("resize", finish);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      rail?.removeEventListener("wheel", outside);
      window.removeEventListener("resize", finish);
    };
  });

  // Escape leaves a rename without saving it, as the rail's pane rename does.
  useEscape(() => (renaming ? stopRenaming() : closePaneMenu()), true);

  function pick(id) {
    prefs.setTabColour(m.pid, id);
    closePaneMenu();
  }

  // Focus goes back before the action runs: Kill's confirm takes it next.
  function run(id) {
    if (id === "rename") { startRenaming(); return; }
    const a = m.actions;
    closePaneMenu();
    if (id === "pin") a.onTogglePin?.();
    else if (id === "move") a.onMoveAccount?.(move.id);
    else if (id === "name") a.onToggleNamePin?.();
    else if (id === "editor") a.onOpenInEditor?.();
    else if (id === "kill") a.onClose?.();
  }

  // The server cleans the names (trims, drops empties and hue names).
  function saveNames() {
    const typed = {};
    for (const input of ref.current?.querySelectorAll("input[data-colour]") || []) typed[input.dataset.colour] = input.value;
    prefs.setTabColourNames(typed);
  }

  function stopRenaming() {
    setRenaming(false);
    ref.current.focus();
  }

  // Leaving mid-rename keeps what was typed, as a blur keeps a pane rename.
  // Once: closing moves focus out, which calls this again.
  function finish({ refocus = true } = {}) {
    if (menu.value?.pid !== m.pid) return;
    if (renaming) saveNames();
    closePaneMenu({ refocus });
  }

  function moveTo(stop) {
    setCursor(stop);
    const colour = stopPreview(stop);
    preview.value = colour === undefined ? null : { pid: m.pid, colour };
    ref.current.querySelector(`[data-stop="${stop}"]`)?.focus();
  }

  function onKeyDown(e) {
    if (renaming) {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); saveNames(); stopRenaming(); }
      return;
    }
    const act = paneMenuKey(e, cursor, items, current);
    if (!act) return;
    // Its keys are the menu's alone, never a terminal's or a global shortcut's.
    e.stopPropagation();
    e.preventDefault();
    if (act.close) closePaneMenu();
    else if (act.cursor !== undefined) moveTo(act.cursor);
    else if (act.run) run(act.run);
    else pick(act.pick);
  }

  // Focus that leaves the menu (Tab past the last name, a click into a
  // terminal) closes it where it went.
  function onFocusOut(e) {
    if (!ref.current?.contains(e.relatedTarget)) finish({ refocus: false });
  }

  function startRenaming() {
    preview.value = null;
    setRenaming(true);
  }
  useEffect(() => { if (renaming) ref.current.querySelector("input[data-colour]")?.focus(); }, [renaming]);

  const hover = (colour) => () => { preview.value = { pid: m.pid, colour }; };
  const root = {
    ref,
    class: `tb-dd-menu pane-menu${renaming ? " is-renaming" : ""}`,
    tabIndex: -1,
    style: pos ? { left: `${pos.left}px`, top: `${pos.top}px` } : { visibility: "hidden" },
    onKeyDown,
    onFocusOut,
  };

  // Renaming, it holds text fields, not menu items: a group, not a menu.
  if (renaming) {
    return (
      <div {...root} role="group" aria-label="rename colours">
        <div class="pane-menu-head">Rename colours<span class="pane-menu-hint">↵ save · esc cancel</span></div>
        {TAB_COLOURS.map((c) => (
          <label key={c.id} class="tb-dd-item tab-colour-item">
            <span class={`tab-colour-swatch tab-colour-${c.id}`} aria-hidden="true"></span>
            <input
              data-colour={c.id}
              class="tab-colour-input"
              // Uncontrolled: the rail re-renders every poll and a value= binding
              // would overwrite what is being typed.
              defaultValue={names[c.id] || ""}
              placeholder={c.name}
              maxLength={TAB_COLOUR_NAME_MAX}
              spellcheck={false}
              aria-label={`name for ${c.name}`}
            />
          </label>
        ))}
      </div>
    );
  }

  // The readout names the swatch under the pointer or the cursor, or else
  // the pane's own colour: the strip has no room for names.
  const shown = preview.value?.pid === m.pid ? preview.value.colour : current;
  const swatchName = (id) => (id ? prefs.getTabColourName(id) : "No colour");
  const hue = (id) => TAB_COLOURS.find((c) => c.id === id)?.name;

  return (
    <div {...root} role="menu" aria-label={`actions for ${label}`} onMouseLeave={() => { preview.value = null; }}>
      <div class="pane-menu-head">Actions for <b>{label}</b></div>
      <div class="pane-menu-swatches" role="group" aria-label="tab colour">
        {[...TAB_COLOURS.map((c) => c.id), null].map((id, i) => (
          <button
            key={id || "none"}
            data-stop={i}
            role="menuitemradio"
            aria-checked={current === id}
            aria-label={swatchName(id)}
            title={id ? `${swatchName(id)}${names[id] ? ` (${hue(id)})` : ""} · ${tabs(counts[id] || 0)} · ${i + 1}` : "No colour · 0"}
            tabIndex={-1}
            class={`pane-menu-swatch${current === id ? " active" : ""}${cursor === i ? " is-cursor" : ""}`}
            onMouseEnter={hover(id)}
            onClick={() => pick(id)}
          >
            <span class={`tab-colour-swatch${id ? ` tab-colour-${id}` : " is-empty"}`} aria-hidden="true"></span>
          </button>
        ))}
      </div>
      <div class="pane-menu-readout" aria-hidden="true">
        <b>{swatchName(shown)}</b>
        {shown && names[shown] && <span>{hue(shown)}</span>}
        {shown && <span>· {tabs(counts[shown] || 0)}</span>}
        <kbd class="ps-kbd">1–6</kbd>
      </div>
      {items.map((it, i) => (
        <MenuItem
          key={it.id}
          it={it}
          stop={SWATCHES + i}
          title={titles[it.id]}
          isCursor={cursor === SWATCHES + i}
          onEnter={() => { preview.value = null; }}
          onRun={() => run(it.id)}
        />
      ))}
    </div>
  );
}

// One item under the strip; Freeze name is the one checkbox.
function MenuItem({ it, stop, title, isCursor, onEnter, onRun }) {
  const props = {
    "data-stop": stop,
    tabIndex: -1,
    title,
    class: `tb-dd-item pane-menu-item${it.id === "kill" ? " is-danger" : ""}${isCursor ? " is-cursor" : ""}`,
    onMouseEnter: onEnter,
    onClick: onRun,
  };
  const body = (
    <>
      <span class="pane-menu-label">{it.label}</span>
      {it.checked && <span class="pane-menu-check" aria-hidden="true">✓</span>}
      {it.key && <kbd class="ps-kbd">{it.key.toUpperCase()}</kbd>}
    </>
  );
  return (
    <>
      {it.sep && <hr class="pane-menu-sep" />}
      {it.checked === undefined
        ? <button {...props} role="menuitem">{body}</button>
        : <button {...props} role="menuitemcheckbox" aria-checked={it.checked}>{body}</button>}
    </>
  );
}
