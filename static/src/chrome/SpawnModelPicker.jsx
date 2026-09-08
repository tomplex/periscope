// Header segmented control pinning the model new Claude panes launch on.
// "auto" (unset) leaves it to launch_policy — fable, then opus[1m], within
// whichever account the policy picked; "default" leaves it to the account's
// settings.json; an alias pins EVERY unnamed spawn path — launcher New Tab,
// unified open, MCP spawn_claude — because the pin is honored server-side in
// usage.choose_launch, the choke point they all share. The launcher's
// per-launch picker seeds from the resolved answer and still wins for one
// launch (it sends its value explicitly).
//
// A server setting (settings.spawn_model), not a client pref, for the account
// pin's reason: MCP spawns never see client prefs. Rides /api/state as
// `spawn_model` (the raw pin, "auto"/"default"/alias, null = auto) beside
// `launch_default` (what auto resolves to), so this writes optimistically
// and lets the poll confirm. Reuses the account picker's classes — same
// chrome, same row.
import { PIN_MODELS } from "../models.js";
import { launchDefault, spawnModel } from "../store.js";
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
  const cur = spawnModel.value || "auto";
  const auto = launchDefault.value;
  return (
    <div
      class="spawn-acct spawn-model"
      title={
        "which model new Claude panes launch on\n" +
        "auto — fable until its weekly sub-limit walls, then opus 1m (per account)\n" +
        "default — whatever the account's settings.json picks\n" +
        "fable / opus 1m / sonnet — pin every spawn (New Tab, + new, spawned workers); the launcher can still override one launch"
      }
    >
      <span class="spawn-acct-label">model</span>
      {PIN_MODELS.map((m) => (
        <button
          type="button"
          key={m.id}
          class={`spawn-acct-btn${cur === m.id ? " is-active" : ""}`}
          title={m.id === "auto" ? auto?.reason : undefined}
          onClick={() => pick(m.id)}
        >
          {m.id === "auto" && cur === "auto" ? `auto → ${auto?.model ?? "default"}` : m.label}
        </button>
      ))}
    </div>
  );
}
