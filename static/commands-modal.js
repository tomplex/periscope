// Commands editor modal. Open/close + row state + drag reorder. Persists
// every mutation through prefs.js — no batched save button.

import * as prefs from './prefs.js';
import { escapeHtml } from './util.js';
import { pushEscape, popEscape } from './overlay.js';

const modal = document.getElementById("commands-modal");
const closeBtn = document.getElementById("commands-modal-close");
const addBtn = document.getElementById("commands-modal-add");
const listEl = document.getElementById("commands-modal-list");

let isOpen = false;

function render() {
  const commands = prefs.getCommands();
  listEl.innerHTML = commands
    .map(
      (c, i) => `
        <div class="commands-row" draggable="true" data-label="${escapeHtml(c.label)}" data-i="${i}">
          <span class="commands-grip" title="drag to reorder">⋮⋮</span>
          <input class="commands-label" value="${escapeHtml(c.label)}" placeholder="label">
          <input class="commands-exec" value="${escapeHtml(c.exec || "")}" placeholder="exec (empty = bare shell)">
          <button class="commands-del" title="delete">×</button>
        </div>`
    )
    .join("");
}

async function handleAdd() {
  const base = "command";
  let label = base;
  let n = 1;
  const taken = new Set(prefs.getCommands().map((c) => c.label));
  while (taken.has(label)) label = `${base}-${++n}`;
  const ok = await prefs.addCommand({ label, exec: "" });
  if (ok) render();
}

async function handleUpdateRow(row) {
  const oldLabel = row.dataset.label;
  const newLabel = row.querySelector(".commands-label").value.trim();
  const newExec = row.querySelector(".commands-exec").value;
  if (!newLabel) return;
  const ok = await prefs.updateCommand(oldLabel, { label: newLabel, exec: newExec });
  if (ok) render();
}

async function handleDeleteRow(row) {
  const label = row.dataset.label;
  const ok = await prefs.deleteCommand(label);
  if (ok) render();
}

async function handleReorder(newOrder) {
  const ok = await prefs.reorderCommands(newOrder);
  if (ok) render();
}

// ── Drag/drop reorder ───────────────────────────────────────────────────

let dragLabel = null;

function bindDragHandlers() {
  listEl.addEventListener("dragstart", (e) => {
    const row = e.target.closest(".commands-row");
    if (!row) return;
    dragLabel = row.dataset.label;
    row.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
  });
  listEl.addEventListener("dragend", () => {
    listEl.querySelectorAll(".commands-row").forEach((r) => r.classList.remove("dragging"));
    listEl.querySelectorAll(".commands-row").forEach((r) =>
      r.classList.remove("drag-over-top", "drag-over-bottom")
    );
    dragLabel = null;
  });
  listEl.addEventListener("dragover", (e) => {
    const row = e.target.closest(".commands-row");
    if (!row) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const rect = row.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    row.classList.toggle("drag-over-top", before);
    row.classList.toggle("drag-over-bottom", !before);
  });
  listEl.addEventListener("dragleave", (e) => {
    const row = e.target.closest(".commands-row");
    if (row) row.classList.remove("drag-over-top", "drag-over-bottom");
  });
  listEl.addEventListener("drop", (e) => {
    const row = e.target.closest(".commands-row");
    if (!row || !dragLabel) return;
    e.preventDefault();
    const targetLabel = row.dataset.label;
    if (targetLabel === dragLabel) return;
    const rect = row.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    const labels = prefs.getCommands().map((c) => c.label);
    const idxDrag = labels.indexOf(dragLabel);
    if (idxDrag < 0) return;
    labels.splice(idxDrag, 1);
    const idxTarget = labels.indexOf(targetLabel);
    const insertAt = before ? idxTarget : idxTarget + 1;
    labels.splice(insertAt, 0, dragLabel);
    handleReorder(labels);
  });
}

// ── Open / close ────────────────────────────────────────────────────────

export function openCommandsModal() {
  if (isOpen) return;
  isOpen = true;
  render();
  modal.classList.remove("hidden");
  document.body.classList.add("commands-modal-open");
  pushEscape(closeCommandsModal);
}

export function closeCommandsModal() {
  if (!isOpen) return;
  isOpen = false;
  modal.classList.add("hidden");
  document.body.classList.remove("commands-modal-open");
  popEscape(closeCommandsModal);
}

export function initCommandsModal() {
  closeBtn.addEventListener("click", closeCommandsModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeCommandsModal();
  });
  addBtn.addEventListener("click", handleAdd);
  listEl.addEventListener("change", (e) => {
    const row = e.target.closest(".commands-row");
    if (!row) return;
    if (e.target.matches(".commands-label, .commands-exec")) handleUpdateRow(row);
  });
  listEl.addEventListener("click", (e) => {
    const delBtn = e.target.closest(".commands-del");
    if (!delBtn) return;
    const row = delBtn.closest(".commands-row");
    handleDeleteRow(row);
  });
  bindDragHandlers();
}
