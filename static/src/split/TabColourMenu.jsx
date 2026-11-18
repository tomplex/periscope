// The tab colour menu, one per rail: a pane card's colour button or a
// right-click on the card opens it. Keys follow colourMenuKey (tabColours.js).
// While a row is under the pointer or the keyboard cursor, the card already
// shows that colour (shownTabColour), so you see it before you pick. The menu
// holds focus while open, so its keys never reach a terminal: it closes when
// focus leaves it, and gives focus back to whatever had it when it opened.
import { signal } from "@preact/signals";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import * as prefs from "../prefs.js";
import { windows } from "../store.js";
import { colourMenuKey, NO_COLOUR_ROW, RENAME_ROW, rowPreview, TAB_COLOUR_NAME_MAX, TAB_COLOURS } from "../tabColours.js";
import { paneLabel } from "../util.js";

// { pid, name, rect, returnFocus } while open. `rect` is what it opens from:
// the colour button's box, or the right-click's point.
const menu = signal(null);
// { pid, colour } while a row is under the pointer or the cursor; a null
// colour previews "No colour".
const preview = signal(null);

export function openTabColourMenu(w, rect) {
  menu.value = { pid: w.pid, name: paneLabel(w), rect, returnFocus: document.activeElement };
  preview.value = null;
}

// `refocus` is false when focus already went somewhere else on its own.
export function closeTabColourMenu({ refocus = true } = {}) {
  const m = menu.value;
  menu.value = null;
  preview.value = null;
  if (refocus) m?.returnFocus?.focus?.();
}

export function isTabColourMenuOpen(pid) {
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

export function TabColourMenu() {
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

function Menu({ m }) {
  const ref = useRef(null);
  const [cursor, setCursor] = useState(-1);
  const [renaming, setRenaming] = useState(false);
  const [pos, setPos] = useState(null);
  const current = prefs.getTabColour(m.pid);
  const names = prefs.getTabColourNames();
  const counts = {};
  for (const w of windows.value || []) {
    const id = prefs.getTabColour(w.pid);
    if (id) counts[id] = (counts[id] || 0) + 1;
  }

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
  useEscape(() => (renaming ? stopRenaming() : closeTabColourMenu()), true);

  function pick(id) {
    prefs.setTabColour(m.pid, id);
    closeTabColourMenu();
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
    closeTabColourMenu({ refocus });
  }

  function moveTo(row) {
    setCursor(row);
    const colour = rowPreview(row);
    preview.value = colour === undefined ? null : { pid: m.pid, colour };
    ref.current.querySelectorAll("[data-row]")[row]?.focus();
  }

  function onKeyDown(e) {
    if (renaming) {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); saveNames(); stopRenaming(); }
      return;
    }
    const act = colourMenuKey(e, cursor);
    if (!act) return;
    // Its keys are the menu's alone, never a terminal's or a global shortcut's.
    e.stopPropagation();
    e.preventDefault();
    if (act.close) closeTabColourMenu();
    else if (act.cursor !== undefined) moveTo(act.cursor);
    else if (act.rename) startRenaming();
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
    class: `tb-dd-menu tab-colour-menu${renaming ? " is-renaming" : ""}`,
    tabIndex: -1,
    style: pos ? { left: `${pos.left}px`, top: `${pos.top}px` } : { visibility: "hidden" },
    onKeyDown,
    onFocusOut,
  };

  // Renaming, it holds text fields, not menu items: a group, not a menu.
  if (renaming) {
    return (
      <div {...root} role="group" aria-label="rename colours">
        <div class="tab-colour-menu-head">Rename colours<span class="tab-colour-menu-hint">↵ save · esc cancel</span></div>
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

  return (
    <div {...root} role="menu" aria-label={`colour for ${m.name}`} onMouseLeave={() => { preview.value = null; }}>
      <div class="tab-colour-menu-head">Colour for <b>{m.name}</b></div>
      {TAB_COLOURS.map((c, i) => (
        <button
          key={c.id}
          data-row={i}
          role="menuitemradio"
          aria-checked={current === c.id}
          tabIndex={-1}
          class={`tb-dd-item tab-colour-item${current === c.id ? " active" : ""}${cursor === i ? " is-cursor" : ""}`}
          onMouseEnter={hover(c.id)}
          onClick={() => pick(c.id)}
        >
          <span class={`tab-colour-swatch tab-colour-${c.id}`} aria-hidden="true"></span>
          <span class="tab-colour-name">
            {prefs.getTabColourName(c.id)}
            {names[c.id] && <span class="tab-colour-hue">{c.name}</span>}
          </span>
          <span class="tab-colour-count" title={`${counts[c.id] || 0} tabs in this colour`}>{counts[c.id] || ""}</span>
          <kbd class="ps-kbd">{i + 1}</kbd>
        </button>
      ))}
      <hr class="tab-colour-sep" />
      <button
        data-row={NO_COLOUR_ROW}
        role="menuitemradio"
        aria-checked={!current}
        tabIndex={-1}
        class={`tb-dd-item tab-colour-item${cursor === NO_COLOUR_ROW ? " is-cursor" : ""}`}
        onMouseEnter={hover(null)}
        onClick={() => pick(null)}
      >
        <span class="tab-colour-swatch is-empty" aria-hidden="true"></span>
        <span class="tab-colour-name">No colour</span>
        <kbd class="ps-kbd">0</kbd>
      </button>
      <button
        data-row={RENAME_ROW}
        role="menuitem"
        tabIndex={-1}
        class={`tb-dd-item tab-colour-item${cursor === RENAME_ROW ? " is-cursor" : ""}`}
        onMouseEnter={() => { preview.value = null; }}
        onClick={startRenaming}
      >
        <span class="tab-colour-swatch is-rename" aria-hidden="true">✎</span>
        <span class="tab-colour-name">Rename colours…</span>
        <kbd class="ps-kbd">R</kbd>
      </button>
    </div>
  );
}
