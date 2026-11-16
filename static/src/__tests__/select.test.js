import { describe, expect, it } from "vitest";
import { SELECT_FRESH_MS, selectToApply } from "../select.js";

describe("selectToApply", () => {
  const now = 1_000_000;
  const wins = [{ pid: "a" }, { pid: "b" }];
  const none = (lastAt) => ({ window: null, lastAt });

  it("applies a fresh request this tab has not applied", () => {
    expect(selectToApply({ pid: "a", at: now - 500 }, null, now, wins)).toEqual({ window: wins[0], lastAt: now - 500 });
  });

  it("applies a newer request after an older one", () => {
    expect(selectToApply({ pid: "b", at: now - 100 }, now - 500, now, wins)).toEqual({ window: wins[1], lastAt: now - 100 });
  });

  it("ignores the request it already applied", () => {
    expect(selectToApply({ pid: "a", at: now - 500 }, now - 500, now, wins)).toEqual(none(now - 500));
  });

  it("ignores a request older than the fresh window", () => {
    expect(selectToApply({ pid: "a", at: now - SELECT_FRESH_MS - 1 }, null, now, wins)).toEqual(none(null));
  });

  it("ignores a request for a pane that is not live", () => {
    expect(selectToApply({ pid: "z", at: now - 500 }, null, now, wins)).toEqual(none(null));
  });

  it("ignores a null or missing request", () => {
    expect(selectToApply(null, null, now, wins)).toEqual(none(null));
    expect(selectToApply(undefined, null, now, wins)).toEqual(none(null));
  });

  it("applies a request once, on the first state that shows its pane", () => {
    // applyState stores lastAt between states: a pane that shows one state late keeps its request.
    const select = { pid: "a", at: now - 500 };
    const before = selectToApply(select, null, now, []);
    expect(before).toEqual(none(null));
    const applied = selectToApply(select, before.lastAt, now + 1000, wins);
    expect(applied).toEqual({ window: wins[0], lastAt: now - 500 });
    expect(selectToApply(select, applied.lastAt, now + 2000, wins)).toEqual(none(now - 500));
  });
});
