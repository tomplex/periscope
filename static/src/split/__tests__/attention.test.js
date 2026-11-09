import { describe, expect, it } from "vitest";
import { shortestUniqueSuffix } from "../../util.js";
import {awaitingReplyByPid, buildActivity,
  buildNeedsYou, buildNext, buildReady, buildRunning,
  isCiFailing, isSoftQuestion, nextToOpen, paneBlip, prunedStateDismissals,resolvePinned, waitingPanes, waitTier,
} from "../attention.js";

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
  it("keeps done/info, drops need_human", () => {
    const items = [evt({ kind: "done" }), evt({ kind: "need_human" }), evt({ kind: "info" })];
    expect(buildActivity(items).map((r) => r.kind)).toEqual(["done", "info"]);
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

describe("prunedStateDismissals", () => {
  it("keeps pids still in the given state, drops the rest", () => {
    const live = [win({ pid: "p1", state: "needs-input" }), win({ pid: "p2", state: "idle" })];
    const next = prunedStateDismissals(new Set(["p1", "p2"]), live, "needs-input");
    expect([...next]).toEqual(["p1"]);
  });
  it("scopes to the state argument (done)", () => {
    const live = [win({ pid: "p1", state: "done" }), win({ pid: "p2", state: "idle" })];
    const next = prunedStateDismissals(new Set(["p1", "p2"]), live, "done");
    expect([...next]).toEqual(["p1"]);
  });
});

describe("buildReady", () => {
  it("includes live done panes, carrying the window", () => {
    const live = win({ state: "done", completed_at: 500 });
    const rows = buildReady([live], [], new Set());
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("live");
    expect(rows[0].w.completed_at).toBe(500);
  });

  it("excludes panes not in done state", () => {
    expect(buildReady([win({ state: "idle" })], [], new Set())).toHaveLength(0);
    expect(buildReady([win({ state: "working" })], [], new Set())).toHaveLength(0);
    expect(buildReady([win({ state: "unknown", agent: "codex" })], [], new Set())).toHaveLength(0);
  });

  it("hides a dismissed live pid", () => {
    const live = win({ pid: "p1", state: "done" });
    expect(buildReady([live], [], new Set(), new Set(["p1"]))).toHaveLength(0);
  });

  it("includes unacked done events after live rows", () => {
    const live = win({ pid: "p1", target: "a:0", state: "done" });
    const rows = buildReady([live], [evt({ kind: "done", target: "b:0" })], new Set());
    expect(rows.map((r) => r.kind)).toEqual(["live", "event"]);
  });

  it("dedupes a done event whose target is already a live row", () => {
    const live = win({ pid: "p1", target: "a:0", state: "done" });
    const rows = buildReady([live], [evt({ kind: "done", target: "a:0" })], new Set());
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("live");
  });

  it("drops dismissed and acked events", () => {
    expect(buildReady([], [evt({ kind: "done", id: "a1" })], new Set(["a1"]))).toHaveLength(0);
    const w = win({ target: "tc/x:0", state: "idle", focused_at: 200 });
    expect(buildReady([w], [evt({ kind: "done", ts: 100 })], new Set())).toHaveLength(0);
  });

  it("non-done alerts never enter the zone", () => {
    expect(buildReady([], [evt({ kind: "need_human" })], new Set())).toHaveLength(0);
    expect(buildReady([], [evt({ kind: "info" })], new Set())).toHaveLength(0);
  });

  it("sorts events newest-first", () => {
    const rows = buildReady([], [
      evt({ kind: "done", id: "old", ts: 50 }),
      evt({ kind: "done", id: "new", ts: 90 }),
    ], new Set());
    expect(rows.map((r) => r.id)).toEqual(["new", "old"]);
  });
});

describe("buildRunning", () => {
  it("includes only working panes, carrying the window", () => {
    const live = win({ pid: "p1", state: "working", spinner: "Brewing" });
    const rows = buildRunning([live, win({ pid: "p2", state: "idle" }), win({ pid: "p3", state: "done" })]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ kind: "live", pid: "p1" });
    expect(rows[0].w.spinner).toBe("Brewing");
  });

  it("empty/missing windows → no rows", () => {
    expect(buildRunning([])).toHaveLength(0);
    expect(buildRunning(undefined)).toHaveLength(0);
    expect(buildRunning([win({ state: "unknown", agent: "codex" })])).toHaveLength(0);
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

describe("awaitingReplyByPid", () => {
  it("maps a pane with an unanswered need_human to that question's ts", () => {
    const w = win({ pid: "p1", target: "a:0" });
    const m = awaitingReplyByPid([w], [evt({ target: "a:0", ts: 500 })], new Set());
    expect(m.get("p1")).toBe(500);
  });

  it("keeps the OLDEST ask, so repeating the question doesn't reset the wait", () => {
    const w = win({ pid: "p1", target: "a:0" });
    const asks = [
      evt({ id: "a1", target: "a:0", ts: 500 }),
      evt({ id: "a2", target: "a:0", ts: 900 }),
    ];
    expect(awaitingReplyByPid([w], asks, new Set()).get("p1")).toBe(500);
  });

  it("drops a pane the user has since engaged (same predicate as NEEDS YOU)", () => {
    const w = win({ pid: "p1", target: "a:0", acted_at: 600 });
    const m = awaitingReplyByPid([w], [evt({ target: "a:0", ts: 500 })], new Set());
    expect(m.has("p1")).toBe(false);
  });

  it("drops dismissed asks and ignores non-need_human kinds", () => {
    const w = win({ pid: "p1", target: "a:0" });
    expect(awaitingReplyByPid([w], [evt({ id: "x" })], new Set(["x"])).size).toBe(0);
    expect(awaitingReplyByPid([w], [evt({ kind: "done" })], new Set()).size).toBe(0);
  });

  it("ignores asks whose pane is no longer live", () => {
    expect(awaitingReplyByPid([], [evt({ target: "gone:0" })], new Set()).size).toBe(0);
  });
});

describe("waitTier", () => {
  it("escalates fresh -> stale -> urgent at 10min and 1h", () => {
    expect(waitTier(1000, 1000)).toBe("fresh");
    expect(waitTier(1000, 1000 + 599)).toBe("fresh");
    expect(waitTier(1000, 1000 + 600)).toBe("stale");
    expect(waitTier(1000, 1000 + 3599)).toBe("stale");
    expect(waitTier(1000, 1000 + 3600)).toBe("urgent");
  });
});

describe("waitingPanes", () => {
  it("counts a pane once when it has both a live dialog and an unanswered event", () => {
    const w = win({ state: "needs-input", needs_since: 300 });
    const m = waitingPanes([w], [evt({ ts: 200 })], new Set(), new Set(), 1000);
    expect(m.size).toBe(1);
    expect(m.get("pid:p1").row.kind).toBe("live");
    expect(m.get("pid:p1").since).toBe(200);
  });

  it("keys an event for a pane that has gone by its target", () => {
    const m = waitingPanes([], [evt({ target: "gone:3" })], new Set(), new Set(), 1000);
    expect([...m.keys()]).toEqual(["target:gone:3"]);
  });

  it("leaves out the panes `keep` rejects", () => {
    const a = win({ pid: "a", target: "a:0", state: "needs-input" });
    const b = win({ pid: "b", target: "b:0", state: "needs-input" });
    const m = waitingPanes([a, b], [], new Set(), new Set(), 1000, (w) => w?.pid !== "a");
    expect([...m.keys()]).toEqual(["pid:b"]);
  });
});

describe("isCiFailing", () => {
  it("is true for a red open PR, or one whose state isn't known yet", () => {
    expect(isCiFailing(win({ pr: 7, ci: "✗", pr_state: "open" }))).toBe(true);
    expect(isCiFailing(win({ pr: 7, ci: "✗" }))).toBe(true);
  });

  it("is false once the PR merged or closed, when CI isn't red, or with no PR", () => {
    expect(isCiFailing(win({ pr: 7, ci: "✗", pr_state: "merged" }))).toBe(false);
    expect(isCiFailing(win({ pr: 7, ci: "✗", pr_state: "closed" }))).toBe(false);
    expect(isCiFailing(win({ pr: 7, ci: "✓" }))).toBe(false);
    expect(isCiFailing(win({ ci: "✗" }))).toBe(false);
  });
});

describe("paneBlip", () => {
  const agent = (over) => win({ agent: "claude", ...over });

  it("a shell is a shell whatever its PR says", () => {
    expect(paneBlip(win({ agent: null, pr: 7, ci: "✗" }), 0)).toBe("shell");
  });

  it("reads a pre-agent server's is_claude as an agent", () => {
    expect(paneBlip(win({ agent: undefined, is_claude: true, state: "working" }), 0)).toBe("working");
  });

  it("asks on a dialog, or on an unanswered need_human while idle", () => {
    expect(paneBlip(agent({ state: "needs-input" }), 0)).toBe("asks");
    expect(paneBlip(agent({ state: "idle" }), 0, true)).toBe("asks");
  });

  it("is broken on an API error or failing CI, unless the agent is working on it", () => {
    expect(paneBlip(agent({ api_error: true }), 0)).toBe("broken");
    expect(paneBlip(agent({ pr: 7, ci: "✗" }), 0)).toBe("broken");
    expect(paneBlip(agent({ state: "working", api_error: true }), 0)).toBe("working");
  });

  it("a done pane dims an hour after it finished", () => {
    expect(paneBlip(agent({ state: "done", completed_at: 1000 }), 1000 + 3599)).toBe("done");
    expect(paneBlip(agent({ state: "done", completed_at: 1000 }), 1000 + 3600)).toBe("done-old");
  });

  it("anything else is idle", () => {
    expect(paneBlip(agent({ state: "idle" }), 0)).toBe("idle");
  });
});

describe("buildNext", () => {
  const agent = (over) => win({ agent: "claude", ...over });

  it("orders asks (longest wait first), then broken, then done", () => {
    const done = agent({ pid: "d", target: "d:0", state: "done", completed_at: 900 });
    const broken = agent({ pid: "b", target: "b:0", api_error: true });
    const newer = agent({ pid: "n", target: "n:0", state: "needs-input", needs_since: 800 });
    const older = agent({ pid: "o", target: "o:0", state: "needs-input", needs_since: 500 });
    const next = buildNext([done, broken, newer, older], [], new Set(), new Set(), new Set(), 1000);
    expect(next.map((e) => e.key)).toEqual(["asks:pid:o", "asks:pid:n", "broken:b", "done:d"]);
    expect(next.map((e) => e.since)).toEqual([500, 800, null, 900]);
  });

  it("carries the agent's own words from its unanswered need_human onto the live entry", () => {
    const pane = agent({ pid: "p", target: "p:0", state: "needs-input", needs_since: 900 });
    const next = buildNext([pane], [evt({ target: "p:0", ts: 950, message: "which branch?" })],
      new Set(), new Set(), new Set(), 1000);
    expect(next).toHaveLength(1);
    expect(next[0]).toMatchObject({ kind: "asks", message: "which branch?", since: 900 });
    expect(next[0].row.kind).toBe("live");
  });

  it("shows the pane's newest unanswered question, whichever row it keeps", () => {
    const events = [
      evt({ id: "old", target: "p:0", ts: 100, message: "first ask" }),
      evt({ id: "new", target: "p:0", ts: 300, message: "latest ask" }),
    ];
    const idle = agent({ pid: "p", target: "p:0" });
    const [entry] = buildNext([idle], events, new Set(), new Set(), new Set(), 1000);
    expect(entry).toMatchObject({ kind: "asks", message: "latest ask", since: 100 });
  });

  it("lists a PR failing CI once, however many panes show it", () => {
    const red = (pid) => agent({ pid, target: `${pid}:0`, pr: 42, repo_slug: "o/r", ci: "✗" });
    const other = agent({ pid: "z", target: "z:0", pr: 7, repo_slug: "o/r", ci: "✗" });
    const errored = agent({ pid: "e", target: "e:0", pr: 42, repo_slug: "o/r", api_error: true });
    const next = buildNext([red("x"), red("y"), other, errored], [], new Set(), new Set(), new Set(), 1000);
    expect(next.map((e) => e.key)).toEqual(["broken:x", "broken:z", "broken:e"]);
  });

  it("an event whose pane has closed still asks, with its message", () => {
    const next = buildNext([], [evt({ target: "gone:0", message: "help" })], new Set(), new Set(), new Set(), 1000);
    expect(next).toEqual([expect.objectContaining({ kind: "asks", w: null, message: "help" })]);
  });

  it("lists a pane once: asking beats broken, and broken beats done", () => {
    const both = agent({ pid: "x", target: "x:0", state: "needs-input", api_error: true, needs_since: 1 });
    const brokenDone = agent({ pid: "y", target: "y:0", state: "done", completed_at: 5, pr: 3, ci: "✗" });
    const next = buildNext([both, brokenDone], [], new Set(), new Set(), new Set(), 1000);
    expect(next.map((e) => e.key)).toEqual(["asks:pid:x", "broken:y"]);
  });

  it("drops a done pane once it's been marked seen", () => {
    const done = agent({ pid: "d", target: "d:0", state: "done", completed_at: 900 });
    expect(buildNext([done], [], new Set(), new Set(), new Set(["d"]), 1000)).toEqual([]);
  });
});

describe("nextToOpen", () => {
  const e = (key, pid) => ({ key, w: pid ? { pid } : null });

  it("walks the queue by what this walk has opened, not by position", () => {
    // Opening a done card removes it, so the queue the next press sees has
    // shrunk; a position-based walk fell back to the asking pane every time.
    const asks = e("asks:p", "p");
    const opened = new Set(["asks:p", "done:a"]);
    expect(nextToOpen([asks, e("done:b", "b"), e("done:c", "c")], "a", opened).key).toBe("done:b");
  });

  it("skips the pane already on screen, even for its second entry", () => {
    const entries = [e("done:e1", "c"), e("done:e2", "c"), e("done:d", "d")];
    expect(nextToOpen(entries, "c", new Set()).key).toBe("done:d");
  });

  it("starts over once every entry has been opened", () => {
    const entries = [e("asks:p", "p"), e("broken:q", "q")];
    expect(nextToOpen(entries, "q", new Set(["asks:p", "broken:q"])).key).toBe("asks:p");
  });

  it("is null when nothing but the selected pane wants you", () => {
    expect(nextToOpen([], "a", new Set())).toBe(null);
    expect(nextToOpen([e("done:a", "a")], "a", new Set())).toBe(null);
  });
});
