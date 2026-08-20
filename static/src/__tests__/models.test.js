import { describe, expect, it } from "vitest";
import { MODELS, modelQuery } from "../models.js";

describe("modelQuery", () => {
  it("omits the param for the default so the launch URL is unchanged", () => {
    expect(modelQuery("default")).toBeNull();
    expect(modelQuery("")).toBeNull();
    expect(modelQuery(undefined)).toBeNull();
  });

  it("passes any other id through verbatim", () => {
    expect(modelQuery("opus")).toBe("opus");
    expect(modelQuery("claude-fable-5")).toBe("claude-fable-5");
  });

  it("offers default first so the picker's first button is the safe one", () => {
    expect(MODELS[0].id).toBe("default");
  });
});
