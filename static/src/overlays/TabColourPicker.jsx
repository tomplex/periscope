// Settings → Tab colours: how strongly a tab's colour tints its card in the
// rail. Saves on change, like Fonts and the Tamagotchi picker: the modal's
// Save and Cancel are for its other fields. Each tile previews two coloured
// cards and a plain one at its own strength.
import { getTabTint, setTabTint } from "../prefs.js";
import { TAB_TINTS } from "../tabColours.js";

const PREVIEW = [
  { colour: "sky", blip: "working", name: "rail-member-cards" },
  { colour: "clay", blip: "idle", name: "config-migration" },
  { colour: null, blip: "done", name: "tc-collision-demo" },
];

function Preview({ wash }) {
  return (
    <span class="tab-tint-preview" style={{ "--tab-wash": `${wash}%` }} aria-hidden="true">
      {PREVIEW.map((p) => (
        <span key={p.name} class={`tab-tint-card pane-blip-${p.blip}${p.colour ? ` tab-colour tab-colour-${p.colour}` : ""}`}>
          <span class="pane-stripe"></span>{p.name}
        </span>
      ))}
    </span>
  );
}

export function TabColourPicker() {
  const current = getTabTint().id;
  return (
    <fieldset class="tab-tint-picker">
      <legend>Tab colours</legend>
      <div class="tab-tint-options">
        {TAB_TINTS.map((t) => (
          <label key={t.id} class="settings-tile tab-tint-option">
            <input type="radio" name="tab-tint" value={t.id} checked={t.id === current}
                   onChange={() => setTabTint(t.id)} />
            <Preview wash={t.wash} />
            <span class="tab-tint-name">{t.name}<small>{t.wash}%</small></span>
          </label>
        ))}
      </div>
      <p class="settings-overrides-hint">
        How strongly a tab's colour tints its card in the rail. Right-click a card to colour it.
      </p>
    </fieldset>
  );
}
