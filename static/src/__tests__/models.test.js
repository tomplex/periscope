import { describe, expect, it } from "vitest";
import { MODELS } from "../models.js";

describe("MODELS", () => {
  it("offers default first so both pickers' first chip is the safe one", () => {
    expect(MODELS[0].id).toBe("default");
  });

  it("uses aliases, never versioned ids, so the list doesn't rot", () => {
    for (const m of MODELS.slice(1)) expect(m.id).not.toMatch(/\d/);
  });
});
