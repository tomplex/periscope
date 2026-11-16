import { describe, expect, it } from "vitest";
import { SELECT_FRESH_MS, selectToApply } from "../select.js";

describe("selectToApply", () => {
  const now = 1_000_000;

  it("applies a fresh request this tab has not applied", () => {
    expect(selectToApply({ pid: "a", at: now - 500 }, null, now)).toBe("a");
  });

  it("ignores the request it already applied", () => {
    expect(selectToApply({ pid: "a", at: now - 500 }, now - 500, now)).toBeNull();
  });

  it("ignores a request older than the fresh window", () => {
    expect(selectToApply({ pid: "a", at: now - SELECT_FRESH_MS - 1 }, null, now)).toBeNull();
  });

  it("ignores no request", () => {
    expect(selectToApply(null, null, now)).toBeNull();
    expect(selectToApply(undefined, null, now)).toBeNull();
  });
});
