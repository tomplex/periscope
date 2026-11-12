import { describe, expect, it } from "vitest";
import { DUTY_ORDER, deskView, dutyItems, moreCount, nextMergeWatch, PARTY_S, wallLabel, wallPhrase } from "../clawdDuty.js";

const NOW = 1_800_000_000;
const pane = (pid, extra = {}) => ({ pid, target: `s:${pid}`, name: `pane-${pid}`, agent: "claude", state: "idle", ...extra });
const kinds = (items) => items.map((i) => i.kind);

describe("dutyItems", () => {
  it("is empty when nothing needs anyone", () => {
    expect(dutyItems({ windows: [pane("a"), pane("b", { state: "working" })], nowSec: NOW })).toEqual([]);
  });

  it("orders every kind by DUTY_ORDER", () => {
    const items = dutyItems({
      windows: [
        pane("a", { state: "needs-input", waiting_for: "permission" }),
        pane("b", { api_error: true }),
        pane("c", { ci: "✗", pr: 7, repo_slug: "o/r" }),
        pane("d", { state: "done" }),
        pane("e", { context_pct: 91 }),
      ],
      parties: [{ key: "f#9", pid: "f", pr: 9, at: NOW - 5 }],
      update: { behind: 3, running: false }, nowSec: NOW,
    });
    expect(kinds(items)).toEqual(DUTY_ORDER);
    expect(DUTY_ORDER).toEqual(["bell", "api", "done", "merged", "ctx", "ci", "update"]);
    expect(moreCount(items)).toBe(1 + 1 + 1 + 1 + 1 + 1);
  });

  it("skips the pane you're looking at for every kind but its merge party", () => {
    const windows = [
      pane("a", { state: "needs-input", waiting_for: "permission" }), pane("b", { api_error: true }),
      pane("c", { state: "done" }), pane("d", { context_pct: 95 }),
    ];
    for (const [pid, kind] of [["a", "bell"], ["b", "api"], ["c", "done"], ["d", "ctx"]]) {
      expect(kinds(dutyItems({ windows, selectedPid: pid, nowSec: NOW })), pid).not.toContain(kind);
    }
    const parties = [{ key: "a#9", pid: "a", pr: 9, at: NOW }];
    expect(kinds(dutyItems({ windows: [pane("a")], parties, selectedPid: "a", nowSec: NOW }))).toEqual(["merged"]);
  });

  it("rings for need_human alerts and goes red after ten minutes", () => {
    const windows = [pane("a")];
    const alerts = [{ id: 1, kind: "need_human", target: "s:a", ts: NOW - 700 }];
    const [bell] = dutyItems({ windows, alerts, nowSec: NOW });
    // An event row reveals its pane, as the rail's event row does.
    expect(bell).toMatchObject({ kind: "bell", label: "1", urgent: true, target: { action: "reveal", target: "s:a" } });
    expect(bell.phrase).toBe("pane-a needs you (11 min)");
    expect(dutyItems({ windows, alerts: [{ ...alerts[0], ts: NOW - 60 }], nowSec: NOW })[0].urgent).toBe(false);
  });

  it("ages a live question from the server's needs-input stamp, oldest first", () => {
    const windows = [pane("a", { state: "needs-input", waiting_for: "permission", needs_since: NOW - 30 }),
      pane("b", { state: "needs-input", asked_question: true, needs_since: NOW - 900 })];
    expect(dutyItems({ windows, nowSec: NOW })[0]).toMatchObject({ count: 2, urgent: true, target: { action: "needs", pid: "b" } });
    // A pane without a stamp (a server from before needs_since) reads as fresh.
    expect(dutyItems({ windows: [pane("c", { state: "needs-input", waiting_for: "permission" })], nowSec: NOW })[0].urgent).toBe(false);
  });

  it("counts a pane once when it has both a live row and a need_human event", () => {
    // Claude notifies while its pane already sits in needs-input: one pane, one ring.
    const windows = [pane("a", { state: "needs-input", waiting_for: "permission", needs_since: NOW - 30 })];
    const alerts = [{ id: 1, kind: "need_human", target: "s:a", ts: NOW - 20 }];
    const [bell] = dutyItems({ windows, alerts, nowSec: NOW });
    expect(bell).toMatchObject({ count: 1, label: "1", target: { action: "needs", pid: "a" } });
    expect(bell.phrase).toBe("pane-a needs you");
  });

  it("ages a pane from its oldest unanswered event, as the rail's urgency tier does", () => {
    // After a reload needs_since can be fresh while the pane has been asking for
    // an hour; the rail marks it wait-urgent from the event, and so does Clawd.
    const windows = [pane("a", { state: "needs-input", waiting_for: "permission", needs_since: NOW - 30 })];
    const alerts = [{ id: 1, kind: "need_human", target: "s:a", ts: NOW - 700 }];
    expect(dutyItems({ windows, alerts, nowSec: NOW })[0]).toMatchObject({ urgent: true, phrase: "pane-a needs you (11 min)" });
  });

  it("opens live rows the rail's way and event rows by revealing them", () => {
    // The dismissal itself lives in alertFeed's openNeedsPane / openReadyPane,
    // shared with the rail (alertFeed.test.js); here, which path a click takes.
    expect(dutyItems({ windows: [pane("d", { state: "done" })], nowSec: NOW })[0].target)
      .toEqual({ action: "ready", pid: "d", says: "open pane-d" });
    const event = dutyItems({ windows: [pane("d")], alerts: [{ id: 2, kind: "done", target: "s:d", ts: NOW - 5 }], nowSec: NOW });
    expect(event[0].target).toMatchObject({ action: "reveal", target: "s:d" });
  });

  it("respects the rail's dismissals", () => {
    const windows = [pane("a", { state: "needs-input", asked_question: true }), pane("b", { state: "done" })];
    expect(dutyItems({ windows, dismissedNeedsPids: new Set(["a"]), dismissedReadyPids: new Set(["b"]), nowSec: NOW })).toEqual([]);
  });

  it("counts a failing PR once however many panes share it, and only while open", () => {
    const windows = [pane("a", { ci: "✗", pr: 9000, repo_slug: "o/r" }), pane("b", { ci: "✗", pr: 9000, repo_slug: "o/r" }),
      pane("c", { ci: "✗", pr: 12, repo_slug: "o/r", pr_state: "merged" }), pane("d", { ci: "✓", pr: 13 })];
    expect(dutyItems({ windows, nowSec: NOW })[0])
      .toMatchObject({ kind: "ci", count: 1, phrase: "PR #9000 is failing CI", target: { action: "select", pid: "a" } });
    expect(kinds(dutyItems({ windows, selectedPid: "b", nowSec: NOW }))).not.toContain("ci");
  });

  it("aims a failing PR at the pane its Next card opens, not the first in window order", () => {
    const windows = [pane("a", { ci: "✗", pr: 9000, repo_slug: "o/r" }),
      pane("b", { ci: "✗", pr: 9000, repo_slug: "o/r", pr_linked: true })];
    expect(dutyItems({ windows, nowSec: NOW })[0].target).toMatchObject({ action: "select", pid: "b" });
  });

  it("is on duty for exactly what the rail's Next lists, so the two never disagree", () => {
    const counts = (windows, alerts = []) =>
      Object.fromEntries(dutyItems({ windows, alerts, nowSec: NOW }).map((i) => [i.kind, i.count]));
    const red = { pr: 12, repo_slug: "o/r", ci: "✗", pr_state: "open" };
    // An agent already working on its API error or its red CI doesn't need you
    // yet, and a shell window showing a red PR is nobody to ask.
    expect(counts([pane("a", { state: "working", api_error: true })])).toEqual({});
    expect(counts([pane("b", { state: "working", ...red })])).toEqual({});
    expect(counts([pane("c", { agent: null, state: "shell", ...red })])).toEqual({});
    // One pane, one duty: broken outranks done, and asking outranks both.
    expect(counts([pane("d", { state: "done", completed_at: NOW - 60, ...red })])).toEqual({ ci: 1 });
    expect(counts([pane("e", { state: "done", completed_at: NOW - 60, api_error: true })])).toEqual({ api: 1 });
    expect(counts([pane("f", { state: "done", completed_at: NOW - 60 })],
      [{ id: 1, kind: "need_human", target: "s:f", ts: NOW - 120 }])).toEqual({ bell: 1 });
  });

  it("names a nearly-full window by its percent, and labels the bubble with the fullest", () => {
    const windows = [pane("a", { context_pct: 70 }), pane("b", { context_pct: 84 })];
    expect(dutyItems({ windows, nowSec: NOW })[0])
      .toMatchObject({ kind: "ctx", count: 1, label: "84%", pct: 84, cost: false, phrase: "pane-b is running out of context (84%)" });
  });

  it("names a cost-hot pane by its cost, not as running out of context", () => {
    const hint = "each call re-reads 480k of context; clearing pays for itself in ~12 min at this pace";
    expect(dutyItems({ windows: [pane("c", { ctx_class: "hot", context_pct: 40, ctx_hint: hint })], nowSec: NOW })[0])
      .toMatchObject({ label: "1", pct: null, cost: true, phrase: `pane-c is costly to keep carrying: ${hint}` });
    expect(dutyItems({ windows: [pane("c", { ctx_class: "hot", context_pct: 40 }), pane("d", { context_pct: 90 })], nowSec: NOW })[0])
      .toMatchObject({ count: 2, label: "90%", phrase: "2 panes worth compacting", target: { pid: "d" } });
  });

  it("ends a merge party after ten minutes", () => {
    const parties = [{ key: "a#9", pid: "a", pr: 9, at: NOW - PARTY_S }];
    expect(dutyItems({ parties, nowSec: NOW })).toEqual([]);
    expect(dutyItems({ windows: [pane("a")], parties, nowSec: NOW - 1 })[0]).toMatchObject({ kind: "merged", phrase: "PR #9 merged 🎉" });
  });

  it("aims a party's click at its pane by pid, or just ends it once the pane is gone", () => {
    const parties = [{ key: "a#9", pid: "a", pr: 9, at: NOW }];
    expect(dutyItems({ windows: [pane("a")], parties, nowSec: NOW })[0].target)
      .toEqual({ action: "party", key: "a#9", pid: "a", says: "open pane-a" });
    expect(dutyItems({ windows: [], parties, nowSec: NOW })[0].target)
      .toEqual({ action: "party", key: "a#9", pid: null, says: "end the party" });
  });

  it("offers the update only when behind and not already running", () => {
    expect(kinds(dutyItems({ update: { behind: 2, running: false }, nowSec: NOW }))).toEqual(["update"]);
    expect(dutyItems({ update: { behind: 2, running: true }, nowSec: NOW })).toEqual([]);
    expect(dutyItems({ update: { behind: 0 }, nowSec: NOW })).toEqual([]);
  });
});

describe("wallLabel", () => {
  it("formats the reset, naming the day only for a far-off one", () => {
    const at = new Date(2026, 9, 21, 15, 0).getTime() / 1000;
    expect(wallLabel(at, at - 3600)).toEqual({ day: null, time: "3pm" });
    expect(wallLabel(at + 40 * 60, at)).toEqual({ day: null, time: "3:40pm" });
    expect(wallLabel(at + 2 * 86400, at)).toEqual({ day: "Fri", time: "3pm" });
    expect(wallPhrase({ day: "Fri", time: "3pm" })).toBe("every account is at its limit until Fri 3pm");
  });

  it("says 'later' when no walling meter reported a reset", () => {
    expect(wallLabel(null, NOW)).toEqual({ day: null, time: "later" });
  });
});

describe("deskView", () => {
  it("shows the mood only with nothing on duty, and mirrors agents unless the screen is taken", () => {
    expect(deskView("code", null)).toEqual({ atDesk: true, moodShown: true, showsPanes: true });
    expect(deskView("code", "bell")).toMatchObject({ moodShown: false, showsPanes: true });
    expect(deskView("code", "update")).toMatchObject({ showsPanes: true });
    for (const kind of ["done", "merged", "ci", "api", "ctx"]) expect(deskView("code", kind).showsPanes, kind).toBe(false);
    expect(deskView("eat", null)).toMatchObject({ atDesk: false, moodShown: false, showsPanes: false });
  });

  it("leaves the walled nap asleep at the desk: no mood, no agents on the monitor", () => {
    expect(deskView("nap", null)).toEqual({ atDesk: false, moodShown: false, showsPanes: false });
  });
});

describe("merge watch", () => {
  it("throws a party only on open → merged for the same PR", () => {
    const open = [pane("a", { pr: 9, pr_state: "open" })];
    const done = [pane("a", { pr: 9, pr_state: "merged" })];
    let m = nextMergeWatch({ states: {}, parties: [] }, open, NOW);
    expect(m.parties).toEqual([]);
    m = nextMergeWatch(m, [pane("a", { pr: 9 })], NOW + 3); // a cold cache drops pr_state for a poll
    m = nextMergeWatch(m, done, NOW + 6);
    expect(m.parties).toEqual([{ key: "a#9", pid: "a", pr: 9, at: NOW + 6 }]);
    expect(nextMergeWatch(m, done, NOW + 9).parties).toHaveLength(1);
  });

  it("never parties on a first sighting, a close, or a different PR", () => {
    expect(nextMergeWatch({ states: {}, parties: [] }, [pane("a", { pr: 9, pr_state: "merged" })], NOW).parties).toEqual([]);
    const states = { a: { pr: 9, state: "open" } };
    expect(nextMergeWatch({ states, parties: [] }, [pane("a", { pr: 9, pr_state: "closed" })], NOW).parties).toEqual([]);
    expect(nextMergeWatch({ states, parties: [] }, [pane("a", { pr: 10, pr_state: "merged" })], NOW).parties).toEqual([]);
  });
});
