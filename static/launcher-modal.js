// Per-worktree "+ New tab" launcher. Reads prefs.getCommands() and lets
// the user pick one; POSTs to /api/window/new with the worktree's
// session as the target.
//
// /api/window/new takes URL query parameters (not a JSON body): `session`
// and `exec`. The "label" in prefs is purely UI text; the `exec` field
// is the actual shell command to run.

import * as prefs from './prefs.js';
import { escapeHtml, apiCall } from './util.js';

const $ = (id) => document.getElementById(id);

export function openLauncher(worktreeKey) {
  $("launcher-session-name").textContent = `Add to session: ${worktreeKey}`;
  const commands = prefs.getCommands();
  $("launcher-list").innerHTML = commands.length === 0
    ? `<div class="launcher-empty">No commands configured. Use Commands settings to add some.</div>`
    : commands.map(c => `
        <button class="launcher-row" data-label="${escapeHtml(c.label)}">${escapeHtml(c.label)}</button>
      `).join("");

  $("launcher-list").querySelectorAll(".launcher-row").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      const label = e.currentTarget.dataset.label;
      const cmd = (prefs.getCommands() || []).find(c => c.label === label);
      const exec = cmd?.exec || "";
      const qs = new URLSearchParams({ session: worktreeKey });
      if (exec) qs.set("exec", exec);
      await apiCall("new window", `/api/window/new?${qs.toString()}`, { method: "POST" });
      close();
    });
  });
  $("launcher-modal").classList.remove("hidden");
}

function close() {
  $("launcher-modal").classList.add("hidden");
}

export function initLauncher() {
  $("launcher-close").addEventListener("click", close);
}
