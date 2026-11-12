// What opening a pane that asks or finished does — shared by the rail's Next
// cards and the header's Clawd (split/alertFeed.js), so the two can't drift on
// dismissals.

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../prefs.js", () => ({ setLastSelected: vi.fn() }));

const { alerts, dismissedAlertIds, dismissedNeedsPids, dismissedReadyPids, railSelection, windows } = await import("../../store.js");
const {
  dismissalFor, dismissEvents, markDoneCardsSeen, markReadySeen, openNeedsPane, openNext, openNextEntry, openReadyPane, selectedPid, selectReview,
} = await import("../alertFeed.js");
const { freezeReleases } = await import("../layoutFreeze.js");
const prefs = await import("../../prefs.js");

afterEach(() => {
  alerts.value = [];
  windows.value = [];
  dismissedAlertIds.value = new Set();
  dismissedNeedsPids.value = new Set();
  dismissedReadyPids.value = new Set();
  railSelection.value = null;
  prefs.setLastSelected.mockClear();
});

describe("selectedPid", () => {
  it("extracts a pid from a pane highlight-key", () => {
    expect(selectedPid("pane:aa11")).toBe("aa11");
  });

  it("returns null for the review form and for no selection", () => {
    // railSelection is a union of key shapes; only the pane form has a pid.
    expect(selectedPid("review:/dev/x")).toBeNull();
    expect(selectedPid(null)).toBeNull();
    expect(selectedPid(undefined)).toBeNull();
  });
});

describe("opening a Next entry", () => {
  const pane = (pid, over = {}) => ({ pid, target: `s:${pid}`, agent: "claude", state: "idle", focused_at: 0, acted_at: 0, ...over });

  it("selects a broken pane, which has no row behind it", () => {
    openNextEntry({ kind: "broken", w: pane("b", { api_error: true }), row: null });
    expect(railSelection.value).toBe("pane:b");
  });

  it("marks a live done pane seen as it opens it", () => {
    const w = pane("d", { state: "done" });
    openNextEntry({ kind: "done", w, row: { kind: "live", pid: "d", w } });
    expect([...dismissedReadyPids.value]).toEqual(["d"]);
    expect(railSelection.value).toBe("pane:d");
  });

  it("reveals an event's pane, and does nothing once that pane has closed", () => {
    windows.value = [pane("e")];
    openNextEntry({ kind: "asks", w: windows.value[0], row: { kind: "event", target: "s:e" } });
    expect(railSelection.value).toBe("pane:e");
    railSelection.value = null;
    openNextEntry({ kind: "done", w: null, row: { kind: "event", target: "gone:0" } });
    expect(railSelection.value).toBe(null);
  });

  it("lets a held Next queue through, as opening or dismissing a card does", () => {
    const before = freezeReleases.value;
    openNextEntry({ kind: "broken", w: pane("b"), row: null });
    dismissEvents("done", "s:b");
    markReadySeen(pane("c"));
    expect(freezeReleases.value).toBe(before + 3);
  });
});

describe("a Next card's ×", () => {
  const w = { pid: "p", target: "s:p" };

  it("marks a live done pane seen, and its done events with it", () => {
    alerts.value = [{ id: "d1", kind: "done", target: "s:p" }];
    dismissalFor({ kind: "done", w, row: { kind: "live", pid: "p", w } })();
    expect([...dismissedReadyPids.value]).toEqual(["p"]);
    expect([...dismissedAlertIds.value]).toEqual(["d1"]);
  });

  it("dismisses an event card's kind of event, every one the pane raised", () => {
    alerts.value = [
      { id: "q1", kind: "need_human", target: "s:p" }, { id: "q2", kind: "need_human", target: "s:p" },
      { id: "d1", kind: "done", target: "s:p" },
    ];
    dismissalFor({ kind: "asks", w, row: { kind: "event", target: "s:p" } })();
    expect([...dismissedAlertIds.value].sort()).toEqual(["q1", "q2"]);
    dismissalFor({ kind: "done", w: null, row: { kind: "event", target: "s:p" } })();
    expect([...dismissedAlertIds.value].sort()).toEqual(["d1", "q1", "q2"]);
    expect(dismissedReadyPids.value.size).toBe(0);
  });

  it("is absent on a live question and a broken pane, which leave on their own", () => {
    expect(dismissalFor({ kind: "asks", w, row: { kind: "live", pid: "p", w } })).toBe(null);
    expect(dismissalFor({ kind: "broken", w, row: null })).toBe(null);
  });
});

describe("Next's mark all seen", () => {
  it("marks every done card seen, live or event, and leaves the questions", () => {
    const a = { pid: "a", target: "s:a" };
    const q = { pid: "q", target: "s:q" };
    alerts.value = [
      { id: "d1", kind: "done", target: "s:a" }, { id: "d2", kind: "done", target: "s:gone" },
      { id: "q1", kind: "need_human", target: "s:q" },
    ];
    markDoneCardsSeen([
      { kind: "asks", w: q, row: { kind: "event", target: "s:q" } },
      { kind: "done", w: a, row: { kind: "live", pid: "a", w: a } },
      { kind: "done", w: null, row: { kind: "event", target: "s:gone" } },
    ]);
    expect([...dismissedReadyPids.value]).toEqual(["a"]);
    expect([...dismissedAlertIds.value].sort()).toEqual(["d1", "d2"]);
  });
});

describe("⌘J's walk (openNext)", () => {
  const pane = (pid, over = {}) => ({ pid, target: `s:${pid}`, agent: "claude", state: "idle", focused_at: 0, acted_at: 0, ...over });

  it("walks every entry that stays in the queue before starting over", () => {
    // A permission dialog and two API errors stay in Next when opened, so a
    // walk that lost its place would revisit one before reaching the third.
    windows.value = [
      pane("A", { state: "needs-input", waiting_for: "permission prompt", needs_since: 1 }),
      pane("B", { api_error: true }),
      pane("D", { api_error: true }),
    ];
    const seen = [];
    for (let i = 0; i < 6; i++) {
      openNext();
      seen.push(railSelection.value);
    }
    expect(seen).toEqual(["pane:A", "pane:B", "pane:D", "pane:A", "pane:B", "pane:D"]);
  });

  it("skips the pane on screen, and does nothing when it's all that wants you", () => {
    windows.value = [pane("A", { api_error: true })];
    railSelection.value = "pane:A";
    openNext();
    expect(railSelection.value).toBe("pane:A");
    expect(prefs.setLastSelected).not.toHaveBeenCalled();
  });
});

describe("row clicks shared by the rail and Clawd", () => {
  it("dismisses a soft question for this episode and selects its pane", () => {
    openNeedsPane({ pid: "a", asked_question: true, waiting_for: null });
    expect([...dismissedNeedsPids.value]).toEqual(["a"]);
    expect(railSelection.value).toBe("pane:a");
  });

  it("keeps a real dialog sticky", () => {
    openNeedsPane({ pid: "b", asked_question: false, waiting_for: "permission" });
    expect(dismissedNeedsPids.value.size).toBe(0);
    expect(railSelection.value).toBe("pane:b");
  });

  it("always dismisses a done row", () => {
    openReadyPane({ pid: "c" });
    expect([...dismissedReadyPids.value]).toEqual(["c"]);
    expect(railSelection.value).toBe("pane:c");
  });

  it("does nothing for a pane that has gone", () => {
    openNeedsPane(null);
    openReadyPane(null);
    expect(dismissedReadyPids.value.size).toBe(0);
    expect(railSelection.value).toBe(null);
  });
});

describe("what a done banner's buttons do", () => {
  it("Mark seen dismisses the READY row without opening the pane", () => {
    markReadySeen({ pid: "c" });
    expect([...dismissedReadyPids.value]).toEqual(["c"]);
    expect(railSelection.value).toBe(null);
    expect(prefs.setLastSelected).not.toHaveBeenCalled();
    markReadySeen(null);
    expect(dismissedReadyPids.value.size).toBe(1);
  });

  it("Mark seen also dismisses the pane's done events, which would take the row's place", () => {
    alerts.value = [
      { id: "d1", kind: "done", target: "s:1" },
      { id: "q1", kind: "need_human", target: "s:1" },
      { id: "d2", kind: "done", target: "s:2" },
    ];
    markReadySeen({ pid: "c", target: "s:1" });
    expect([...dismissedAlertIds.value]).toEqual(["d1"]);
  });

  it("dismissEvents dismisses every event of one kind a pane raised", () => {
    alerts.value = [
      { id: "q1", kind: "need_human", target: "s:1" },
      { id: "q2", kind: "need_human", target: "s:1" },
      { id: "d1", kind: "done", target: "s:1" },
    ];
    dismissEvents("need_human", "s:1");
    expect([...dismissedAlertIds.value].sort()).toEqual(["q1", "q2"]);
  });

  it("selectReview keys by repo + slug and persists repo + branch", () => {
    selectReview("fdy", "feat/x");
    expect(railSelection.value).toBe("review:fdy/feat--x");
    expect(prefs.setLastSelected).toHaveBeenLastCalledWith({ kind: "review", repo: "fdy", branch: "feat/x" });
  });
});
