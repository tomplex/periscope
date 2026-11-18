// The tab colour menu, one per rail: a pane card's colour button or a
// right-click on the card opens it. Keys follow colourMenuKey (tabColours.js).
// While a row is under the pointer or the keyboard cursor, the card already
// shows that colour (shownTabColour), so you see it before you pick. The menu
// holds focus while open, so its keys never reach a terminal, and gives focus
// back to whatever had it on close.
import { signal } from "@preact/signals";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import * as prefs from "../prefs.js";
import { windows } from "../store.js";
import { colourMenuKey, NO_COLOUR_ROW, RENAME_ROW, TAB_COLOURS, TAB_COLOUR_NAME_MAX } from "../tabColours.js";
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

function closeMenu() {
  const m = menu.value;
  menu.value = null;
  preview.value = null;
  m?.returnFocus?.focus?.();
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

function Menu({ m }) {
  const ref = useRef(null);
  const [cursor, setCursor] = useState(-1);
  const [renaming, setRenaming] = useState(false);
  const [pos, setPos] = useState(null);
  prefs.prefsSignal.value;
  const current = prefs.getTabColour(m.pid);
  const names = prefs.getTabColourNames();
  const counts = {};
  for (const w of windows.value || []) {
    const id = prefs.getTabColour(w.pid);
    if (id) counts[id] = (counts[id] || 0) + 1;
  }

  // Below what it opened from, or above it when it would run off the bottom;
  // inside the window either way. Measured, so it waits a frame unseen.
  useLayoutEffect(() => {
    const box = ref.current.getBoundingClientRect();
    const { rect } = m;
    const below = rect.bottom + 4;
    const top = below + box.height > innerHeight - 8 ? Math.max(8, rect.top - box.height - 4) : below;
    setPos({ left: Math.max(8, Math.min(rect.left, innerWidth - box.width - 8)), top });
  }, [m]);
  // Focus once placed: a visibility:hidden element can't take focus, and
  // without it the keys go nowhere.
  useEffect(() => { if (pos) ref.current.focus(); }, [pos]);

  // A click elsewhere, a wheel over the rail or a resize closes it: its place
  // would be stale. The wheel, not `scroll`: the browser's scroll anchoring
  // moves the rail on its own when a poll reshapes the rows above.
  useEffect(() => {
    const rail = document.getElementById("rail");
    const onDown = (e) => { if (!ref.current?.contains(e.target)) finish(); };
    const onWheel = (e) => { if (!ref.current?.contains(e.target)) finish(); };
    document.addEventListener("pointerdown", onDown, true);
    rail?.addEventListener("wheel", onWheel, { passive: true });
    window.addEventListener("resize", finish);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      rail?.removeEventListener("wheel", onWheel);
      window.removeEventListener("resize", finish);
    };
  });

  useEscape(() => (renaming ? saveNames() : closeMenu()), true);

  function pick(id) {
    prefs.setTabColour(m.pid, id);
    closeMenu();
  }

  function saveNames() {
    const typed = {};
    for (const input of ref.current.querySelectorAll("input[data-colour]")) typed[input.dataset.colour] = input.value;
    prefs.setTabColourNames(typed);
    setRenaming(false);
    ref.current.focus();
  }

  // Leaving mid-rename keeps what was typed.
  function finish() {
    if (renaming) saveNames();
    closeMenu();
  }

  function moveTo(row) {
    setCursor(row);
    preview.value = row < NO_COLOUR_ROW ? { pid: m.pid, colour: TAB_COLOURS[row].id }
      : row === NO_COLOUR_ROW ? { pid: m.pid, colour: null } : null;
    ref.current.querySelectorAll("[data-row]")[row]?.focus();
  }

  function onKeyDown(e) {
    // Its keys are the menu's alone, never a terminal's or a global shortcut's.
    e.stopPropagation();
    if (renaming) {
      if (e.key === "Enter") { e.preventDefault(); saveNames(); }
      return;
    }
    const act = colourMenuKey(e.key, cursor);
    if (!act) return;
    e.preventDefault();
    if (act.cursor !== undefined) moveTo(act.cursor);
    else if (act.rename) startRename();
    else pick(act.pick);
  }

  function startRename() {
    preview.value = null;
    setRenaming(true);
  }
  useEffect(() => { if (renaming) ref.current.querySelector("input[data-colour]")?.focus(); }, [renaming]);

  const hover = (colour) => () => { preview.value = { pid: m.pid, colour }; };
  return (
    <div
      ref={ref}
      class={`tab-colour-menu${renaming ? " is-renaming" : ""}`}
      role="menu"
      aria-label={`colour for ${m.name}`}
      tabIndex={-1}
      style={pos ? { left: `${pos.left}px`, top: `${pos.top}px` } : { visibility: "hidden" }}
      onKeyDown={onKeyDown}
      onMouseLeave={() => { if (!renaming) preview.value = null; }}
    >
      <div class="tab-colour-menu-head">
        {renaming
          ? <>Rename colours<span class="tab-colour-menu-hint">↵ save</span></>
          : <>Colour for <b>{m.name}</b></>}
      </div>
      {TAB_COLOURS.map((c, i) => renaming ? (
        <label key={c.id} class="tab-colour-item">
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
      ) : (
        <button
          key={c.id}
          data-row={i}
          role="menuitemradio"
          aria-checked={current === c.id}
          tabIndex={-1}
          class={`tab-colour-item${current === c.id ? " is-current" : ""}${cursor === i ? " is-cursor" : ""}`}
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
      {!renaming && (
        <>
          <hr class="tab-colour-sep" />
          <button
            data-row={NO_COLOUR_ROW}
            role="menuitemradio"
            aria-checked={!current}
            tabIndex={-1}
            class={`tab-colour-item${cursor === NO_COLOUR_ROW ? " is-cursor" : ""}`}
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
            class={`tab-colour-item${cursor === RENAME_ROW ? " is-cursor" : ""}`}
            onMouseEnter={() => { preview.value = null; }}
            onClick={startRename}
          >
            <span class="tab-colour-swatch is-rename" aria-hidden="true">✎</span>
            <span class="tab-colour-name">Rename colours…</span>
            <kbd class="ps-kbd">R</kbd>
          </button>
        </>
      )}
    </div>
  );
}
