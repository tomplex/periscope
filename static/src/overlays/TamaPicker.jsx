// Settings → Tamagotchi: which creature lives in the header corner
// (chrome/critters.js). Saves on change, like a toggle — the modal's Save and
// Cancel are for its other fields, and Cancel doesn't undo a pick.
import { Critter } from "../chrome/Critter.jsx";
import { CRITTERS } from "../chrome/critters.js";
import { ClawdSprite } from "../chrome/Tamagotchi.jsx";
import { getTamaCritter, setTamaCritter } from "../prefs.js";

// 40px, calm and awake. Clawd's sprite sits in scene units (17 wide, rows
// 9–19), so its box is shifted to frame it like the 20×20 critters.
function Preview({ critter }) {
  return (
    <svg class="critter-preview" viewBox={critter.sprite ? "0 0 20 20" : "-1.5 1 20 20"} width="40" height="40"
         shape-rendering="crispEdges" aria-hidden="true">
      {critter.sprite ? <Critter sprite={critter.sprite} /> : <ClawdSprite />}
    </svg>
  );
}

export function TamaPicker() {
  const current = getTamaCritter();
  return (
    <fieldset class="tama-picker">
      <legend>Tamagotchi</legend>
      <div class="tama-picker-options">
        {CRITTERS.map((c) => (
          <label key={c.id} class="settings-tile tama-picker-option">
            <input type="radio" name="tama-critter" value={c.id} checked={c.id === current}
                   onChange={() => setTamaCritter(c.id)} />
            <Preview critter={c} />
            <span>{c.name}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
