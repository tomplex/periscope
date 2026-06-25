// Commands editor modal. Open/close + row state + drag reorder. Persists
// every mutation through prefs.js — no batched save button. Ported from
// static/commands-modal.js: the innerHTML row rendering becomes JSX
// (convention #8); the drag-reorder identity travels on dataTransfer (label)
// rather than DOM walks; Escape closes via the shared LIFO useEscape hook.
//
// CSS contract preserved: #commands-modal / .commands-modal-overlay /
// .commands-modal-card / .commands-modal-head / .commands-modal-sub /
// .commands-row / .commands-grip / .commands-label / .commands-exec /
// .commands-del / .commands-modal-add / .dragging / .drag-over-top /
// .drag-over-bottom, and the body.commands-modal-open class.
//
// The opener is registered on window (#open-commands lives in the Preact
// <Header>) — same imperative-by-id bridge the other secondary modals use.
import { signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import * as prefs from "../prefs.js";
import { track } from "../track.js";

const open = signal(false);

function openCommandsModal() {
  open.value = true;
  track("overlay.open", { which: "commands" });
}
function close() {
  open.value = false;
}

function Row({ c, i, onDragState }) {
  const rootRef = useRef(null);

  async function update(_e) {
    const row = rootRef.current;
    if (!row) return;
    const newLabel = row.querySelector(".commands-label").value.trim();
    const newExec = row.querySelector(".commands-exec").value;
    if (!newLabel) return;
    await prefs.updateCommand(c.label, { label: newLabel, exec: newExec });
  }

  async function del() {
    await prefs.deleteCommand(c.label);
  }

  return (
    <div
      ref={rootRef}
      class="commands-row"
      draggable
      data-label={c.label}
      data-i={i}
      onDragStart={(e) => {
        onDragState(c.label);
        rootRef.current?.classList.add("dragging");
        e.dataTransfer.setData("text/plain", c.label);
        e.dataTransfer.effectAllowed = "move";
      }}
      onDragEnd={() => onDragState(null)}
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        const rect = rootRef.current.getBoundingClientRect();
        const before = e.clientY < rect.top + rect.height / 2;
        rootRef.current.classList.toggle("drag-over-top", before);
        rootRef.current.classList.toggle("drag-over-bottom", !before);
      }}
      onDragLeave={() => {
        rootRef.current?.classList.remove("drag-over-top", "drag-over-bottom");
      }}
      onDrop={(e) => {
        e.preventDefault();
        const rect = rootRef.current.getBoundingClientRect();
        const before = e.clientY < rect.top + rect.height / 2;
        onDragState.drop(c.label, before);
      }}
    >
      <span class="commands-grip" title="drag to reorder">⋮⋮</span>
      <input class="commands-label" value={c.label} placeholder="label" onChange={update} />
      <input
        class="commands-exec"
        value={c.exec || ""}
        placeholder="exec (empty = bare shell)"
        onChange={update}
      />
      <button class="commands-del" title="delete" onClick={del}>×</button>
    </div>
  );
}

export function CommandsModal() {
  useEscape(close, open.value);

  // Register the opener so the Preact <Header>'s #open-commands button drives
  // this modal. Wired by id, the same bridge <Grid> uses for header buttons.
  useEffect(() => {
    const btn = document.getElementById("open-commands");
    if (btn) btn.addEventListener("click", openCommandsModal);
    return () => { if (btn) btn.removeEventListener("click", openCommandsModal); };
  }, []);

  // Drag identity lives in a closure ref, not the DOM.
  const dragLabel = useRef(null);
  const setDrag = (label) => { dragLabel.current = label; };
  // Reorder splice on drop, then persist through prefs.reorderCommands.
  setDrag.drop = (targetLabel, before) => {
    const src = dragLabel.current;
    if (!src || targetLabel === src) return;
    const labels = prefs.getCommands().map((c) => c.label);
    const idxDrag = labels.indexOf(src);
    if (idxDrag < 0) return;
    labels.splice(idxDrag, 1);
    const idxTarget = labels.indexOf(targetLabel);
    const insertAt = before ? idxTarget : idxTarget + 1;
    labels.splice(insertAt, 0, src);
    prefs.reorderCommands(labels);
  };

  async function handleAdd() {
    const base = "command";
    let label = base;
    let n = 1;
    const taken = new Set(prefs.getCommands().map((c) => c.label));
    while (taken.has(label)) label = `${base}-${++n}`;
    await prefs.addCommand({ label, exec: "" });
  }

  if (!open.value) return null;
  const commands = prefs.getCommands();

  return (
    <div
      id="commands-modal"
      class="commands-modal-overlay"
      onClick={(e) => { if (e.target.id === "commands-modal") close(); }}
    >
      <div class="commands-modal-card">
        <header class="commands-modal-head">
          <h2>New-window commands</h2>
          <button id="commands-modal-close" title="close" onClick={close}>×</button>
        </header>
        <p class="commands-modal-sub">
          First row is the primary button. Drag rows to reorder. Empty <code>exec</code> = bare shell.
        </p>
        <div id="commands-modal-list">
          {commands.map((c, i) => (
            <Row key={c.label} c={c} i={i} onDragState={setDrag} />
          ))}
        </div>
        <button id="commands-modal-add" class="commands-modal-add" onClick={handleAdd}>
          + add command
        </button>
      </div>
    </div>
  );
}
