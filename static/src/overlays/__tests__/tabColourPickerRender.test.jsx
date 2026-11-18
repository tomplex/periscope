// Settings → Tab colours must show the saved tint as the checked radio, and
// each tile must preview its own strength, or picking one is a guess.

import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { prefsSignal } from "../../prefs.js";
import { TabColourPicker } from "../TabColourPicker.jsx";

const withUI = (ui) => { prefsSignal.value = { ...prefsSignal.value, loaded: true, ui }; };
const checked = (html) => [...html.matchAll(/<input[^>]*value="(\w+)"[^>]*checked/g)].map((m) => m[1]);

describe("<TabColourPicker>", () => {
  afterEach(() => withUI({}));

  it("offers Quiet, Default and Strong as real radios, in that order", () => {
    const html = render(<TabColourPicker />);
    const values = [...html.matchAll(/<input type="radio" name="tab-tint"[^>]*value="(\w+)"/g)].map((m) => m[1]);
    expect(values).toEqual(["quiet", "default", "strong"]);
  });

  it("previews each tile at its own strength", () => {
    const html = render(<TabColourPicker />);
    expect([...html.matchAll(/--tab-wash:(\d+)%/g)].map((m) => m[1])).toEqual(["9", "13", "18"]);
  });

  it("checks Default when nothing is picked", () => {
    expect(checked(render(<TabColourPicker />))).toEqual(["default"]);
  });

  it("checks the saved tint", () => {
    withUI({ tab_tint: "strong" });
    expect(checked(render(<TabColourPicker />))).toEqual(["strong"]);
  });
});
