import { describe, expect, it } from "vitest";
import { prKey } from "../../split/attention.js";
import { detect, GRACE_PASSES, PR_PASSES, questionKey } from "../detect.js";

const NOW = 1_000_000;
const win = (over = {}) => ({
  pid: "p1", target: "s:1", session: "s", name: "pane", branch: "b", state: "idle",
  focused_at: 0, acted_at: 0, ...over,
});
const ask = (over = {}) => ({ id: "a1", kind: "need_human", pane_id: "%1", target: "s:1", ts: NOW - 5, message: "which db?", ...over });
const meter = (over = {}) => ({ percent: 40, resets_at: NOW + 3600, hot: false, ...over });
const plan = (meters, over = {}) => ({ default: { available: true, fetched_at: NOW - 60, meters, ...over } });

// Seed with `before`, then return the events `after` announces.
function step(before, after) {
  const seeded = detect(null, before, NOW).state;
  return detect(seeded, after, NOW);
}
const kinds = (r) => r.events.map((e) => e.kind);
// `n` more passes of `snap`, 3 s apart; returns the last state.
function passes(state, snap, n, t0 = NOW) {
  for (let i = 1; i <= n; i++) state = detect(state, snap, t0 + 3 * i).state;
  return state;
}

describe("detect", () => {
  it("announces nothing on the first snapshot, whatever is already there", () => {
    const r = detect(null, { windows: [win({ state: "needs-input" }), win({ pid: "p2", state: "done" })], alerts: [ask()] }, NOW);
    expect(r.events).toEqual([]);
  });

  it("announces each needs-input episode once", () => {
    const waiting = { windows: [win({ state: "needs-input", needs_since: 100 })] };
    const r = step({ windows: [win()] }, waiting);
    expect(kinds(r)).toEqual(["needs_input"]);
    expect(detect(r.state, waiting, NOW).events).toEqual([]);
    // A new episode of the same pane is new news.
    expect(kinds(detect(r.state, { windows: [win({ state: "needs-input", needs_since: 200 })] }, NOW))).toEqual(["needs_input"]);
  });

  it("announces an agent's question, but not alongside the same pane's dialog", () => {
    expect(kinds(step({ windows: [win()], alerts: [] }, { windows: [win()], alerts: [ask()] }))).toEqual(["question"]);
    const both = { windows: [win({ state: "needs-input" })], alerts: [ask()] };
    expect(kinds(step({ windows: [win()], alerts: [] }, both))).toEqual(["needs_input"]);
  });

  it("skips dismissed and acked questions", () => {
    const before = { windows: [win()], alerts: [] };
    expect(step(before, { windows: [win()], alerts: [ask()], dismissedAlertIds: new Set(["a1"]) }).events).toEqual([]);
    expect(step(before, { windows: [win({ focused_at: NOW })], alerts: [ask()] }).events).toEqual([]);
  });

  it("announces each finished turn once, unless its READY row was dismissed", () => {
    const done = { windows: [win({ state: "done", completed_at: 500 })] };
    expect(kinds(step({ windows: [win()] }, done))).toEqual(["done"]);
    expect(step({ windows: [win()] }, { ...done, dismissedReadyPids: new Set(["p1"]) }).events).toEqual([]);
  });

  it("announces CI turning red on an open PR, once per transition", () => {
    const pr = (ci, over = {}) => ({ windows: [win({ pr: 7, repo_slug: "o/r", ci, ...over })] });
    const r = step(pr("⟳"), pr("✗"));
    expect(kinds(r)).toEqual(["ci_failed"]);
    expect(detect(r.state, pr("✗"), NOW).events).toEqual([]);
    // Re-run, red again: a new failure.
    const green = detect(r.state, pr("⟳"), NOW).state;
    expect(kinds(detect(green, pr("✗"), NOW))).toEqual(["ci_failed"]);
    // A PR first seen red, or a closed one, says nothing.
    expect(step({ windows: [] }, pr("✗")).events).toEqual([]);
    expect(step(pr("⟳", { pr_state: "closed" }), pr("✗", { pr_state: "closed" })).events).toEqual([]);
  });

  it("doesn't let a cold linked PR reset its CI state", () => {
    const red = { windows: [win({ pr: 7, repo_slug: "o/r", pr_linked: true, pr_state: "open", ci: "✗" })] };
    const cold = { windows: [win({ pr: 7, repo_slug: "o/r", pr_linked: true })] };
    const between = step(red, cold);
    expect(between.events).toEqual([]);
    expect(detect(between.state, red, NOW).events).toEqual([]);
    // A PR first linked while red, its cache still cold at first: only recorded.
    const linked = detect(detect(null, { windows: [win()] }, NOW).state, cold, NOW).state;
    expect(detect(linked, red, NOW).events).toEqual([]);
  });

  it("counts a PR once however many panes show it", () => {
    const panes = (ci) => ({ windows: [win({ pr: 7, repo_slug: "o/r", ci }), win({ pid: "p2", target: "s:2", pr: 7, repo_slug: "o/r", ci })] });
    expect(kinds(step(panes("⟳"), panes("✗")))).toEqual(["ci_failed"]);
  });

  it("announces a linked PR merging, and keeps a PR missing from a poll", () => {
    const linked = (pr_state) => ({ windows: [win({ pr: 7, repo_slug: "o/r", pr_state })] });
    expect(kinds(step(linked("open"), linked("merged")))).toEqual(["pr_merged"]);
    const gone = detect(detect(null, linked("open"), NOW).state, { windows: [] }, NOW).state;
    expect(kinds(detect(gone, linked("merged"), NOW))).toEqual(["pr_merged"]);
    expect(step({ windows: [] }, linked("merged")).events).toEqual([]);
  });

  it("forgets a PR no pane has shown for PR_PASSES passes", () => {
    const open = { windows: [win({ pr: 7, repo_slug: "o/r", pr_state: "open", ci: "⟳" })] };
    const kept = passes(detect(null, open, NOW).state, { windows: [win()] }, PR_PASSES);
    expect(Object.keys(kept.prs)).toEqual(["o/r#7"]);
    const gone = passes(kept, { windows: [win()] }, 1);
    expect(gone.prs).toEqual({});
    expect(gone.ci).toEqual({});
    expect(gone.prAt).toEqual({});
    // Seen again after that: recorded, not announced.
    expect(detect(gone, { windows: [win({ pr: 7, repo_slug: "o/r", pr_state: "merged" })] }, NOW).events).toEqual([]);
  });

  it("records a stale account's meters when seeding, so its first fresh fetch isn't news", () => {
    const hot = meter({ hot: true });
    const seeded = detect(null, { plan: plan({ s: hot }, { fetched_at: NOW - 9999 }) }, NOW).state;
    expect(detect(seeded, { plan: plan({ s: hot }) }, NOW).events).toEqual([]);
  });

  it("announces a meter getting worse once per mood step per reset window", () => {
    const at = (m) => ({ plan: plan({ week_all: m }) });
    const grumpy = meter({ projected_percent: 120 });
    const r = step(at(meter()), at(grumpy));
    expect(r.events.map((e) => e.mood)).toEqual(["grumpy"]);
    // Flapping back to calm and up again doesn't re-arm.
    const calm = detect(r.state, at(meter()), NOW).state;
    expect(detect(calm, at(grumpy), NOW).events).toEqual([]);
    // Furious is the next step.
    expect(detect(calm, at(meter({ hot: true })), NOW).events.map((e) => e.mood)).toEqual(["furious"]);
    // A new reset window starts over.
    const next = meter({ projected_percent: 120, resets_at: NOW + 99999 });
    expect(detect(calm, at(next), NOW).events.map((e) => e.mood)).toEqual(["grumpy"]);
  });

  it("doesn't re-announce when resets_at drifts by a second across fetches", () => {
    const at = (m) => ({ plan: plan({ week_all: m }) });
    const r = step(at(meter({ resets_at: NOW + 3600 })), at(meter({ resets_at: NOW + 3599, projected_percent: 120 })));
    expect(r.events.map((e) => e.mood)).toEqual(["grumpy"]);
    let st = r.state;
    for (const resets_at of [NOW + 3600, NOW + 3599, NOW + 3600]) {
      const p = detect(st, at(meter({ resets_at, projected_percent: 120 })), NOW);
      expect(p.events).toEqual([]);
      st = p.state;
    }
  });

  it("doesn't re-announce after a stale or unavailable fetch in between", () => {
    const grumpy = plan({ week_all: meter({ projected_percent: 120 }) });
    const r = step({ plan: plan({ week_all: meter() }) }, { plan: grumpy });
    expect(kinds(r)).toEqual(["usage"]);
    for (const gap of [{ fetched_at: NOW - 9999 }, { available: false }]) {
      const between = detect(r.state, { plan: plan({ week_all: meter({ projected_percent: 120 }) }, gap) }, NOW).state;
      expect(detect(between, { plan: grumpy }, NOW).events).toEqual([]);
    }
  });

  it("ignores a meter with no resets_at", () => {
    const r = step({ plan: plan({ s: meter({ resets_at: null }) }) }, { plan: plan({ s: meter({ resets_at: null, hot: true }) }) });
    expect(r.events).toEqual([]);
    expect(r.state.meters).toEqual({});
  });

  it("prunes a reset window once it has passed", () => {
    const seeded = detect(null, { plan: plan({ s: meter({ hot: true }) }) }, NOW).state;
    expect(Object.keys(seeded.meters)).toHaveLength(1);
    expect(Object.keys(detect(seeded, {}, NOW + 60).state.meters)).toHaveLength(1);
    // Keyed by the rounded minute, so allow the half-minute either side.
    expect(detect(seeded, {}, NOW + 3600 + 60).state.meters).toEqual({});
  });

  it("sends at most one usage banner per account per pass, the worst", () => {
    const calm = { plan: plan({ session: meter(), week_all: meter() }) };
    const two = step(calm, { plan: plan({ session: meter({ projected_percent: 120 }), week_all: meter({ projected_percent: 120 }) }) });
    expect(two.events.map((e) => [e.meter, e.mood])).toEqual([["session", "grumpy"]]);
    // Both meters are recorded: neither announces again.
    expect(Object.values(two.state.meters)).toEqual([1, 1]);
    const mixed = step(calm, { plan: plan({ session: meter({ projected_percent: 120 }), week_all: meter({ hot: true }) }) });
    expect(mixed.events.map((e) => [e.meter, e.mood])).toEqual([["week_all", "furious"]]);
    // Another account still gets its own.
    const both = { ...plan({ s: meter({ hot: true }) }), other: { available: true, fetched_at: NOW - 60, meters: { s: meter({ hot: true }) } } };
    expect(step({ plan: { ...plan({ s: meter() }), other: { available: true, fetched_at: NOW - 60, meters: { s: meter() } } } }, { plan: both }).events.map((e) => e.account)).toEqual(["default", "other"]);
  });

  it("leaves stale and unavailable accounts out of usage", () => {
    const hot = meter({ hot: true });
    expect(step({ plan: plan({ s: meter() }) }, { plan: plan({ s: hot }, { fetched_at: NOW - 9999 }) }).events).toEqual([]);
    expect(step({ plan: plan({ s: meter() }) }, { plan: plan({ s: hot }, { available: false }) }).events).toEqual([]);
  });

  it("resumes from a saved state: news that arrived meanwhile is announced once", () => {
    const saved = detect(null, { windows: [win()] }, NOW).state;
    const after = { windows: [win({ state: "needs-input", needs_since: 9 })] };
    const r = detect(JSON.parse(JSON.stringify(saved)), after, NOW);
    expect(kinds(r)).toEqual(["needs_input"]);
    expect(detect(r.state, after, NOW).events).toEqual([]);
  });

  it("keeps a missing key for GRACE_PASSES passes, then prunes it", () => {
    const r = step({ windows: [win()] }, { windows: [win({ state: "needs-input", needs_since: 1 })] });
    const missing = passes(r.state, { windows: [win()] }, GRACE_PASSES);
    expect(Object.keys(missing.seen)).toEqual(["needs:p1@1"]);
    expect(Object.keys(passes(missing, { windows: [win()] }, 1).seen)).toEqual([]);
  });

  it("doesn't re-announce after empty polls within the grace, does after it", () => {
    const waiting = { windows: [win({ state: "needs-input", needs_since: 9 }), win({ pid: "p2", target: "s:2", state: "done", completed_at: 5 })] };
    const r = step({ windows: [] }, waiting);
    expect(kinds(r)).toEqual(["needs_input", "done"]);
    for (const n of [1, GRACE_PASSES]) {
      expect(detect(passes(r.state, { windows: [] }, n), waiting, NOW + 999).events).toEqual([]);
    }
    // Absent for longer than the grace: it's news again.
    expect(kinds(detect(passes(r.state, { windows: [] }, GRACE_PASSES + 1), waiting, NOW + 999))).toEqual(["needs_input", "done"]);
  });

  it("counts a long sleep or a relaunch as no passes: nothing replays", () => {
    const all = { windows: [win({ state: "needs-input", needs_since: 9 }), win({ pid: "p2", target: "s:2", state: "done", completed_at: 5 }), win({ pid: "p3", target: "s:3" })], alerts: [ask({ target: "s:3" })] };
    const r = step({ windows: [] }, all);
    expect(kinds(r)).toEqual(["needs_input", "question", "done"]);
    expect(detect(r.state, all, NOW + 3600).events).toEqual([]);
    expect(detect(JSON.parse(JSON.stringify(r.state)), all, NOW + 600).events).toEqual([]);
  });

  it("treats an old-format saved state (seen: { key: true }) as expired", () => {
    const waiting = { windows: [win({ state: "needs-input", needs_since: 9 })] };
    const r = detect({ seen: { "needs:p1@9": true }, ci: {}, prs: {}, meters: {} }, waiting, NOW);
    expect(kinds(r)).toEqual(["needs_input"]);
    expect(r.state.seen["needs:p1@9"]).toBe(r.state.pass);
    // So is one that held seconds, which would otherwise never age out.
    const secs = detect({ seen: { "needs:p1@9": NOW }, ci: {}, prs: {}, meters: {} }, waiting, NOW);
    expect(kinds(secs)).toEqual(["needs_input"]);
  });

  it("keeps dismissed items seen, so a resume with empty dismissals doesn't replay them", () => {
    const roundTrip = (st) => JSON.parse(JSON.stringify(st));
    // A done row dismissed, then the webview recycles.
    const done = { windows: [win({ state: "done", completed_at: 500 })] };
    const d = step({ windows: [win()] }, { ...done, dismissedReadyPids: new Set(["p1"]) });
    expect(d.events).toEqual([]);
    expect(detect(roundTrip(d.state), { ...done, dismissedReadyPids: new Set() }, NOW + 5).events).toEqual([]);
    // However long the app was gone (persist resumes up to 15 min).
    expect(detect(roundTrip(d.state), { ...done, dismissedReadyPids: new Set() }, NOW + 600).events).toEqual([]);
    // A dismissed question, the same.
    const q = step({ windows: [win()], alerts: [] }, { windows: [win()], alerts: [ask()], dismissedAlertIds: new Set(["a1"]) });
    expect(q.events).toEqual([]);
    expect(detect(roundTrip(q.state), { windows: [win()], alerts: [ask()] }, NOW + 5).events).toEqual([]);
    // A dismissed soft needs-input row, the same.
    const soft = { windows: [win({ state: "needs-input", needs_since: 9, asked_question: true })] };
    const n = step({ windows: [win()] }, { ...soft, dismissedNeedsPids: new Set(["p1"]) });
    expect(n.events).toEqual([]);
    expect(detect(roundTrip(n.state), soft, NOW + 5).events).toEqual([]);
  });

  it("announces a question its pane's dialog hid, once the dialog closes", () => {
    const hidden = { windows: [win({ state: "needs-input", needs_since: 9 })], alerts: [ask()] };
    const r = step({ windows: [win()], alerts: [] }, hidden);
    expect(kinds(r)).toEqual(["needs_input"]);
    const closed = detect(r.state, { windows: [win()], alerts: [ask()] }, NOW + 3);
    expect(kinds(closed)).toEqual(["question"]);
    expect(detect(closed.state, { windows: [win()], alerts: [ask()] }, NOW + 6).events).toEqual([]);
    // Engaged through periscope after the question: acked, so it stays quiet.
    expect(detect(r.state, { windows: [win({ acted_at: NOW })], alerts: [ask()] }, NOW + 3).events).toEqual([]);
    // Announced before a dialog opened, then hidden past the grace: still seen.
    const asked = step({ windows: [win()], alerts: [] }, { windows: [win()], alerts: [ask()] });
    expect(kinds(asked)).toEqual(["question"]);
    let st = asked.state;
    st = passes(st, hidden, GRACE_PASSES + 5);
    expect(detect(st, { windows: [win()], alerts: [ask()] }, NOW + 999).events).toEqual([]);
  });

  it("doesn't replay a question when a server restart regenerates its id", () => {
    const asked = step({ windows: [win()], alerts: [] }, { windows: [win()], alerts: [ask()] });
    expect(kinds(asked)).toEqual(["question"]);
    expect(detect(asked.state, { windows: [win()], alerts: [ask({ id: "rehydrated" })] }, NOW + 3).events).toEqual([]);
    // A rename moves the target, not the pane id.
    expect(detect(asked.state, { windows: [win({ target: "t:1" })], alerts: [ask({ target: "t:1" })] }, NOW + 3).events).toEqual([]);
    // Dismissed, then restarted: still seen.
    const q = step({ windows: [win()], alerts: [] }, { windows: [win()], alerts: [ask()], dismissedAlertIds: new Set(["a1"]) });
    expect(detect(q.state, { windows: [win()], alerts: [ask({ id: "rehydrated" })] }, NOW + 3).events).toEqual([]);
  });

  it("keys a question by pane, time and message", () => {
    expect(questionKey(ask())).toBe(questionKey(ask({ id: "other", target: "x:9" })));
    expect(questionKey(ask())).not.toBe(questionKey(ask({ message: "which table?" })));
    expect(questionKey(ask())).not.toBe(questionKey(ask({ pane_id: "%2" })));
    expect(questionKey(ask())).not.toBe(questionKey(ask({ ts: NOW })));
  });

  it("keys a PR by repo and number", () => {
    expect(prKey({ repo_slug: "o/r", pr: 7 })).toBe("o/r#7");
    expect(prKey({ repo_key: "/x", pr: 7 })).toBe("/x#7");
  });
});
