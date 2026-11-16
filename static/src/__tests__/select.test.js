import { describe, expect, it } from "vitest";
import { SELECT_FRESH_MS, selectToApply } from "../select.js";

describe("selectToApply", () => {
  const now = 1_000_000;
  const wins = [{ pid: "a" }, { pid: "b" }];

  it("applies a fresh request this tab has not applied", () => {
    expect(selectToApply({ pid: "a", at: now - 500 }, null, now, wins)).toBe(wins[0]);
  });

  it("applies a newer request after an older one", () => {
    expect(selectToApply({ pid: "b", at: now - 100 }, now - 500, now, wins)).toBe(wins[1]);
  });

  it("ignores the request it already applied", () => {
    expect(selectToApply({ pid: "a", at: now - 500 }, now - 500, now, wins)).toBeNull();
  });

  it("ignores a request older than the fresh window", () => {
    expect(selectToApply({ pid: "a", at: now - SELECT_FRESH_MS - 1 }, null, now, wins)).toBeNull();
  });

  it("ignores a request for a pane that is not live", () => {
    expect(selectToApply({ pid: "z", at: now - 500 }, null, now, wins)).toBeNull();
  });

  it("ignores a null or missing request", () => {
    expect(selectToApply(null, null, now, wins)).toBeNull();
    expect(selectToApply(undefined, null, now, wins)).toBeNull();
  });
});
