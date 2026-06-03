import { describe, it, expect } from "vitest";
import {
  buildNeedsYou, isAcked, needsYouCount, resolvePinned, buildActivity,
  isSoftQuestion, prunedNeedsDismissals,
} from "../attention.js";
import { shortestUniqueSuffix } from "../../util.js";

const win = (over = {}) => ({
  pid: "p1", target: "tc/x:0", state: "idle",
  needs_input: false, asked_question: false,
  focused_at: 0, acted_at: 0, ...over,
});
const evt = (over = {}) => ({
  id: "a1", kind: "need_human", target: "tc/x:0", ts: 100,
  session: "tc/x", name: "win", message: "help", ...over,
});

describe("buildNeedsYou", () => {
  it("includes live needs-input panes, carrying the window for label/waiting_for", () => {
    const live = win({ state: "needs-input", waiting_for: "approve askuserquestion" });
    const rows = buildNeedsYou([live], [], new Set());
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("live");
    expect(rows[0].w.waiting_for).toBe("approve askuserquestion");
  });

  it("excludes panes not in needs-input state", () => {
    const idle = win({ state: "idle" });
    expect(buildNeedsYou([idle], [], new Set())).toHaveLength(0);
  });

  it("includes unacked need_human events after live rows", () => {
    const live = win({ pid: "p1", target: "a:0", state: "needs-input", needs_input: true });
    const rows = buildNeedsYou([live], [evt({ target: "b:0" })], new Set());
    expect(rows.map((r) => r.kind)).toEqual(["live", "event"]);
  });

  it("drops dismissed event ids", () => {
    const rows = buildNeedsYou([], [evt({ id: "a1" })], new Set(["a1"]));
    expect(rows).toHaveLength(0);
  });

  it("drops acked events (max(focused,acted) > ts)", () => {
    const w = win({ target: "tc/x:0", focused_at: 200 });
    const rows = buildNeedsYou([w], [evt({ ts: 100, target: "tc/x:0" })], new Set());
    expect(rows).toHaveLength(0);
  });

  it("keeps events when stamps are at or below ts (boundary)", () => {
    const w = win({ target: "tc/x:0", focused_at: 100, acted_at: 100 });
    const rows = buildNeedsYou([w], [evt({ ts: 100, target: "tc/x:0" })], new Set());
    expect(rows).toHaveLength(1); // 100 > 100 is false → not acked
  });

  it("sorts events newest-first", () => {
    const rows = buildNeedsYou([], [evt({ id: "old", ts: 50 }), evt({ id: "new", ts: 90 })], new Set());
    expect(rows.map((r) => r.id)).toEqual(["new", "old"]);
  });

  it("non-need_human alerts never enter the zone", () => {
    expect(buildNeedsYou([], [evt({ kind: "done" })], new Set())).toHaveLength(0);
  });
});

describe("resolvePinned", () => {
  it("returns live windows in pin order, drops dead ids", () => {
    const a = win({ pid: "a" }), b = win({ pid: "b" });
    const out = resolvePinned(["b", "gone", "a"], [a, b]);
    expect(out.map((w) => w.pid)).toEqual(["b", "a"]);
  });
});

describe("buildActivity", () => {
  it("keeps done/info/milestone, drops need_human", () => {
    const items = [evt({ kind: "done" }), evt({ kind: "need_human" }), evt({ kind: "info" })];
    expect(buildActivity(items).map((r) => r.kind)).toEqual(["done", "info"]);
  });
});

describe("needsYouCount", () => {
  it("counts rows", () => {
    expect(needsYouCount([{ kind: "live" }, { kind: "event" }])).toBe(2);
  });
});

describe("isSoftQuestion", () => {
  it("true for asked_question with no dialog", () => {
    expect(isSoftQuestion(win({ asked_question: true, waiting_for: null }))).toBe(true);
  });
  it("false when a real dialog is open", () => {
    expect(isSoftQuestion(win({ asked_question: true, waiting_for: "permission prompt" }))).toBe(false);
  });
  it("false when not a question at all", () => {
    expect(isSoftQuestion(win({ asked_question: false }))).toBe(false);
  });
});

describe("buildNeedsYou dismissedNeedsPids", () => {
  it("hides a dismissed live pid", () => {
    const live = win({ pid: "p1", state: "needs-input" });
    expect(buildNeedsYou([live], [], new Set())).toHaveLength(1);
    expect(buildNeedsYou([live], [], new Set(), new Set(["p1"]))).toHaveLength(0);
  });
});

describe("prunedNeedsDismissals", () => {
  it("keeps pids still in needs-input, drops the rest", () => {
    const live = [win({ pid: "p1", state: "needs-input" }), win({ pid: "p2", state: "idle" })];
    const next = prunedNeedsDismissals(new Set(["p1", "p2"]), live);
    expect([...next]).toEqual(["p1"]);
  });
});

describe("shortestUniqueSuffix", () => {
  const all = [
    "tc/model-train/feature-store-validity-window",
    "tc/model-train/anthology-shared-lookup-cache",
    "tc/data-catalog-anthology-update",
  ];
  it("collapses to the leaf when unique", () => {
    expect(shortestUniqueSuffix("tc/model-train/feature-store-validity-window", all))
      .toBe("feature-store-validity-window");
  });
  it("grows a segment when leaves collide", () => {
    const colliding = ["tc/model-train/feature-store", "tc/data-catalog/feature-store"];
    expect(shortestUniqueSuffix("tc/model-train/feature-store", colliding))
      .toBe("model-train/feature-store");
  });
  it("disambiguates a leaf that is a tail of a longer path", () => {
    const names = ["a/b", "x/a/b"];
    expect(shortestUniqueSuffix("a/b", names)).toBe("a/b");   // 2 segs, can't grow further
    expect(shortestUniqueSuffix("x/a/b", names)).toBe("x/a/b"); // grows past the collision
  });
  it("returns single-segment names unchanged", () => {
    expect(shortestUniqueSuffix("periscope", ["periscope", "lgtm"])).toBe("periscope");
  });
});
