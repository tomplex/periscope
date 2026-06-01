// The "+ new" tile at the tail of each session's card row. Reads commands
// from prefs; the first is the primary (top, larger hit area). Each command
// renders as a pair: a main button (plain tab) + a ⌥ button (worktree
// variant — opens an inline branch-name input) when the session is a
// worktree-eligible project.
//
// Ported from grid.js:renderNewTile + handleNewWindow + handleWorktreeVariant.
// CSS classes (.card.card-new, .new-window-pair, .new-window, .is-primary,
// .new-window-worktree, .new-window-stack, .new-window-worktree-form/-label/
// -input/-actions/-cancel/-submit) are preserved verbatim. The worktree
// inline form is local component state rather than the vanilla innerHTML swap.
import { useState, useRef, useEffect } from "preact/hooks";
import * as prefs from "../prefs.js";
import { apiCall } from "../util.js";
import { showToast } from "../overlays/Toast.jsx";
import { poll } from "./poll.js";

// One command's button pair. `worktreeEligible` gates the ⌥ variant button.
function Pair({ session, cmd, primary, worktreeEligible, onWorktree }) {
  const [busy, setBusy] = useState(false);

  async function newWindow(e) {
    e.stopPropagation();
    if (busy) return;
    setBusy(true);
    try {
      await apiCall(
        "new window",
        `/api/window/new?session=${encodeURIComponent(session)}&exec=${encodeURIComponent(cmd.exec || "")}`,
        { method: "POST" }
      );
    } finally {
      setBusy(false);
    }
    poll();
  }

  const cls = primary ? " is-primary" : "";
  return (
    <span class="new-window-pair">
      <button class={`new-window${cls}`} disabled={busy} onClick={newWindow}>
        + {cmd.label}
      </button>
      {worktreeEligible && (
        <button
          class={`new-window-worktree${cls}`}
          disabled={busy}
          title={`new worktree tab + ${cmd.label}`}
          onClick={(e) => {
            e.stopPropagation();
            onWorktree(cmd);
          }}
        >
          ⌥
        </button>
      )}
    </span>
  );
}

// Inline branch-name form shown in place of the tile when a ⌥ variant is
// clicked. Cancel / Escape / a successful create all dismiss it.
function WorktreeForm({ session, cmd, onClose }) {
  const inputRef = useRef(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  async function submit() {
    const branch = inputRef.current?.value.trim() || "";
    if (!branch) {
      inputRef.current?.focus();
      return;
    }
    const params = new URLSearchParams({ session, branch });
    if (cmd.exec) params.set("exec", cmd.exec);
    try {
      const res = await fetch(`/api/window/new-worktree?${params}`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showToast(`new worktree tab failed: ${err.detail || res.status}`, "bad", 6000);
        onClose();
        return;
      }
      const body = await res.json();
      if (body.warning) console.warn("new-worktree warning:", body.warning);
      onClose();
    } catch (e) {
      showToast(`request failed: ${e.message}`, "bad", 6000);
      onClose();
    }
  }

  return (
    <div class="new-window-worktree-form" onClick={(e) => e.stopPropagation()}>
      <div class="new-window-worktree-label">+ {cmd.label} (worktree)</div>
      <input
        ref={inputRef}
        type="text"
        class="new-window-worktree-input"
        placeholder="branch name (e.g. tc/sub-feat)"
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            onClose();
          }
        }}
      />
      <div class="new-window-worktree-actions">
        <button class="new-window-worktree-cancel" type="button" onClick={onClose}>
          cancel
        </button>
        <button class="new-window-worktree-submit" type="button" onClick={submit}>
          create
        </button>
      </div>
    </div>
  );
}

export function NewTile({ session, project }) {
  const commands = prefs.getCommands();
  const [worktreeCmd, setWorktreeCmd] = useState(null);

  if (!commands.length) {
    return <div class="card card-new" data-session={session}></div>;
  }

  // Worktree tab requires a project with a resolved repo; for unmanaged
  // sessions the ⌥ button is hidden.
  const worktreeEligible =
    !!project &&
    project.pinned_dir !== "__main__" &&
    !project.archived_at &&
    !!project.repo;

  if (worktreeCmd) {
    return (
      <div class="card card-new" data-session={session}>
        <WorktreeForm
          session={session}
          cmd={worktreeCmd}
          onClose={() => setWorktreeCmd(null)}
        />
      </div>
    );
  }

  const [primary, ...rest] = commands;
  return (
    <div class="card card-new" data-session={session}>
      <Pair
        session={session}
        cmd={primary}
        primary
        worktreeEligible={worktreeEligible}
        onWorktree={setWorktreeCmd}
      />
      {rest.length > 0 && (
        <div class="new-window-stack">
          {rest.map((c) => (
            <Pair
              key={c.label}
              session={session}
              cmd={c}
              primary={false}
              worktreeEligible={worktreeEligible}
              onWorktree={setWorktreeCmd}
            />
          ))}
        </div>
      )}
    </div>
  );
}
