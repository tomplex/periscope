// What opening a pane that asks or finished does — shared by the rail's Next
// cards and the header's Clawd (split/alertFeed.js), so the two can't drift on
// dismissals.

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../prefs.js", () => ({ setLastSelected: vi.fn() }));

const { alerts, dismissedAlertIds, dismissedNeedsPids, dismissedReadyPids, railSelection } = await import("../../store.js");
const { dismissEvents, markReadySeen, openNeedsPane, openReadyPane, selectReview } = await import("../alertFeed.js");
const prefs = await import("../../prefs.js");

afterEach(() => {
  alerts.value = [];
  dismissedAlertIds.value = new Set();
  dismissedNeedsPids.value = new Set();
  dismissedReadyPids.value = new Set();
  railSelection.value = null;
  prefs.setLastSelected.mockClear();
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
