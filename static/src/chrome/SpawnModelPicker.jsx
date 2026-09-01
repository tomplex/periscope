// Header segmented control pinning the model new Claude panes launch on.
// "default" (unset) leaves it to the account's settings.json; an alias pins
// EVERY unnamed spawn path — launcher New Tab, unified open, MCP spawn_claude
// — because the pin is honored server-side in store.spawn_model_env, the
// choke point they all share. The launcher's per-launch picker seeds from this
// and still wins for one launch (it sends its value explicitly).
//
// A server setting (settings.spawn_model), not a client pref, for the account
// pin's reason: MCP spawns never see client prefs. Rides /api/state as
// `spawn_model`, so this writes optimistically and lets the poll confirm.
// Reuses the account picker's classes — same chrome, same row.
import { MODELS } from "../models.js";
import { spawnModel } from "../store.js";
import { apiCall } from "../util.js";

async function pick(id) {
  const prev = spawnModel.value;
  spawnModel.value = id; // optimistic; the poll carries the persisted value
  const res = await apiCall("spawn model", "/api/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ spawn_model: id }),
  });
  if (!res) spawnModel.value = prev;
}

export function SpawnModelPicker() {
  const cur = spawnModel.value || null;
  return (
    <div
      class="spawn-acct spawn-model"
      title={
        "which model new Claude panes launch on\n" +
        "default — whatever the account's settings.json picks\n" +
        "fable / opus 1m / sonnet — pin every spawn (New Tab, + new, spawned workers); the launcher can still override one launch"
      }
    >
      <span class="spawn-acct-label">model</span>
      {MODELS.map((m) => {
        const id = m.id === "default" ? null : m.id;
        return (
          <button
            type="button"
            key={m.id}
            class={`spawn-acct-btn${cur === id ? " is-active" : ""}`}
            onClick={() => pick(id)}
          >
            {m.label}
          </button>
        );
      })}
    </div>
  );
}
