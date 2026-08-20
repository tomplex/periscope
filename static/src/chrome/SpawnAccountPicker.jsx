// Header segmented control pinning which account new Claude panes land on.
// "auto" (the default) keeps best-headroom routing; A/B pins EVERY unnamed
// spawn path — launcher New Tab, unified open, MCP spawn_claude — because the
// pin is honored server-side inside usage.best_account(), the choke point
// they all share. An explicitly chosen account (the launcher's per-launch
// picker, a tool's account arg) still wins over the pin.
//
// The value is a server setting (settings.spawn_account), not a client pref:
// MCP spawns never see client prefs. It rides /api/state as `spawn_account`,
// so this control writes optimistically and lets the poll confirm.
import { ACCOUNTS } from "../accounts.js";
import { spawnAccount } from "../store.js";
import { apiCall } from "../util.js";

const CHOICES = [{ id: null, label: "auto" }, ...ACCOUNTS];

async function pick(id) {
  const prev = spawnAccount.value;
  spawnAccount.value = id; // optimistic; the poll carries the persisted value
  const res = await apiCall("spawn account", "/api/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ spawn_account: id }),
  });
  if (!res) spawnAccount.value = prev;
}

export function SpawnAccountPicker() {
  const cur = spawnAccount.value || null;
  return (
    <div
      class="spawn-acct"
      title={
        "which account new Claude panes launch on\n" +
        "auto — the account with the most headroom right now\n" +
        "A / B — pin every spawn (New Tab, + new, spawned workers) to one subscription"
      }
    >
      <span class="spawn-acct-label">spawn</span>
      {CHOICES.map((c) => (
        <button
          type="button"
          key={c.id ?? "auto"}
          class={`spawn-acct-btn${cur === c.id ? " is-active" : ""}`}
          onClick={() => pick(c.id)}
        >
          {c.label}
        </button>
      ))}
    </div>
  );
}
