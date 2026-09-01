import { describe, expect, it } from "vitest";
import { MODELS } from "../models.js";

describe("MODELS", () => {
  it("offers default first so both pickers' first chip is the safe one", () => {
    expect(MODELS[0].id).toBe("default");
  });

  it("uses aliases, never versioned ids, so the list doesn't rot", () => {
    // A version is digits in the family name itself ('claude-opus-5', 'opus-5').
    // The '[1m]' extended-context suffix carries a digit but names a context
    // window, not a release, so it composes with the alias and doesn't rot.
    for (const m of MODELS.slice(1)) {
      expect(m.id.replace(/\[[^\]]*\]$/, "")).not.toMatch(/\d/);
    }
  });
});
