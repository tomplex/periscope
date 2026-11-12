// What Clawd is on duty for (Tamagotchi.jsx): the pure part, so the priority
// rule and every trigger unit-test without signals or a DOM.
//
// What asks, what's broken and what finished are the rail's Next queue
// (split/attention.js buildNext) with the same dismissals, a click goes through
// the rail's own row behaviour (split/alertFeed.js openNeedsPane /
// openReadyPane), and the context band is the rail's ctxClass: Clawd and the
// rail can never disagree about a pane.

import { brokenCause, buildNext, prKey, waitTier } from "../split/attention.js";
import { ctxClass } from "../split/ctxBand.js";
import { paneLabel, plural } from "../util.js";

// One slot, highest first: what each one is costing you right now. A blocked
// agent beats a stalled API beats waiting results; the merge party is ten
// minutes long and a context warning can wait that long; red CI is the PR's
// own loop to fix and stays on the PR chip meanwhile; the update is housekeeping.
export const DUTY_ORDER = ["bell", "api", "done", "merged", "ctx", "ci", "update"];
export const PARTY_S = 600;

function waited(sinceSec, nowSec) {
  const min = Math.floor((nowSec - sinceSec) / 60);
  if (min < 1) return "";
  return min < 60 ? ` (${min} min)` : ` (${Math.floor(min / 60)}h ${min % 60}m)`;
}

// Click targets carry ids, not window objects: dutyItems' output is
// JSON-keyed to spare re-renders, and the widget looks the live window up by
// pid at click time, so a pane renumbered since (tmux renumber-windows) can't
// send the click to its neighbour.
const pane = (action, w, extra = {}) => ({ action, pid: w.pid, says: `open ${paneLabel(w)}`, ...extra });

// Everything pending, in DUTY_ORDER. Each item: { kind, label (the bubble's
// text), count (what it adds to the other items' "+N"), phrase (the hover's
// clause), target (what a click does), urgent }.
//
// `selectedPid` is the pane open in the detail view: Clawd never rings about
// the thing already in front of you. The merge party is the exception — it
// asks nothing of you, and you're usually looking at the pane that merged.
export function dutyItems({
  windows = [], alerts = [], dismissedAlertIds = new Set(), dismissedNeedsPids = new Set(),
  dismissedReadyPids = new Set(), selectedPid = null, parties = [], update = null, nowSec,
}) {
  const items = [];
  const notSelected = (w) => !w || selectedPid == null || String(w.pid) !== String(selectedPid);
  const live = windows.filter(notSelected);
  const byPid = new Map(windows.map((w) => [String(w.pid), w]));
  const selected = selectedPid == null ? null : byPid.get(String(selectedPid));
  const next = buildNext(windows, alerts, dismissedAlertIds, dismissedNeedsPids, dismissedReadyPids, nowSec)
    .filter((e) => notSelected(e.w));
  const broken = (cause) => next.filter((e) => e.kind === "broken" && brokenCause(e.w) === cause);

  // Panes, not rows, longest wait first, so Clawd's red bubble and the rail's
  // wait-urgent agree. A live row opens the way the rail's Next card does; an
  // event row reveals its pane, as an event's Next card does.
  const asks = next.filter((e) => e.kind === "asks");
  if (asks.length) {
    const [oldest] = asks;
    const r = oldest.row;
    const name = r.w ? paneLabel(r.w) : r.name || r.session || "a pane";
    items.push({
      kind: "bell", count: asks.length, label: String(asks.length),
      urgent: waitTier(oldest.since, nowSec) !== "fresh",
      phrase: asks.length === 1 ? `${name} needs you${waited(oldest.since, nowSec)}`
        : `needs you in ${asks.length} panes · ${name} has waited longest${waited(oldest.since, nowSec)}`,
      target: r.kind === "live" ? pane("needs", r.w) : { action: "reveal", target: r.target, says: `open ${name}` },
    });
  }

  const api = broken("api");
  if (api.length) {
    items.push({
      kind: "api", count: api.length, label: "API",
      phrase: `the Anthropic API is erroring in ${plural(api.length, "pane")}, not your code`,
      target: pane("select", api[0].w),
    });
  }

  // Next lists a failing PR once, by the pane that speaks for it; the PR drops
  // out entirely when any pane showing it is the one you're looking at.
  const red = broken("ci").filter((e) => !(selected?.pr && prKey(selected) === prKey(e.w)));
  if (red.length) {
    items.push({
      kind: "ci", count: red.length, label: String(red.length),
      phrase: red.length === 1 ? `PR #${red[0].w.pr} is failing CI` : `${red.length} PRs failing CI`,
      target: pane("select", red[0].w),
    });
  }

  const done = next.filter((e) => e.kind === "done");
  if (done.length) {
    const [first] = done;
    const name = first.w ? paneLabel(first.w) : first.row.name || "a pane";
    items.push({
      kind: "done", count: done.length, label: String(done.length),
      phrase: done.length === 1 ? `${name} finished` : `${done.length} panes finished`,
      target: first.row.kind === "live" ? pane("ready", first.w)
        : { action: "reveal", target: first.row.target, says: `open ${name}` },
    });
  }

  // A click opens the merged pane if it's still there and ends the party
  // either way; the hover says which.
  const partying = parties.filter((p) => nowSec - p.at < PARTY_S);
  if (partying.length) {
    const p = partying[partying.length - 1];
    const w = byPid.get(String(p.pid));
    items.push({
      kind: "merged", count: partying.length, label: String(partying.length),
      phrase: partying.length === 1 ? `PR #${p.pr} merged 🎉` : `${partying.length} PRs merged 🎉`,
      target: { action: "party", key: p.key, pid: w ? w.pid : null, says: w ? `open ${paneLabel(w)}` : "end the party" },
    });
  }

  // The rail's red band has two causes, and the hover names the one at hand:
  // a window nearly full (context_pct, the auto-compact hazard) or a context
  // expensive to keep carrying (the server's cost band, `ctx_class: "hot"`).
  const hot = live.filter((w) => ctxClass(w) === " ctx-hot");
  if (hot.length) {
    const full = hot.filter((w) => (w.context_pct ?? 0) >= 80);
    const lead = full.length ? full.reduce((a, b) => (b.context_pct > a.context_pct ? b : a)) : hot[0];
    const pct = full.length ? Math.round(lead.context_pct) : null;
    const phrase = hot.length > 1 ? `${hot.length} panes worth compacting`
      : pct != null ? `${paneLabel(lead)} is running out of context (${pct}%)`
      : `${paneLabel(lead)} is costly to keep carrying${lead.ctx_hint ? `: ${lead.ctx_hint}` : ""}`;
    items.push({
      kind: "ctx", count: hot.length, label: pct != null ? `${pct}%` : String(hot.length), pct, cost: pct == null,
      phrase, target: pane("select", lead),
    });
  }

  if (update?.behind > 0 && !update.running) {
    items.push({
      kind: "update", count: 1, label: "new",
      phrase: `periscope ${update.target} is out`,
      target: { action: "update", says: "see the update" },
    });
  }

  return items.sort((a, b) => DUTY_ORDER.indexOf(a.kind) - DUTY_ORDER.indexOf(b.kind));
}

// The other items' counts, for the bubble's "+N".
export function moreCount(items) {
  return items.slice(1).reduce((n, i) => n + i.count, 0);
}

// The nap's reset time (`/api/state.walls.until`, launch_policy.walls): "3pm",
// "3:40pm"; a reset more than 20 hours out (a weekly wall) also names the day.
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export function wallLabel(until, nowSec) {
  if (!until) return { day: null, time: "later" };
  const d = new Date(until * 1000);
  const h = d.getHours();
  const m = d.getMinutes();
  const time = `${h % 12 || 12}${m ? `:${String(m).padStart(2, "0")}` : ""}${h < 12 ? "am" : "pm"}`;
  return { day: until - nowSec > 20 * 3600 ? DAYS[d.getDay()] : null, time };
}

export function wallPhrase({ day, time }) {
  return `every account is at its limit until ${day ? `${day} ` : ""}${time}`;
}

// The kinds that take over the monitor; with any other on top (or none) the
// monitor keeps mirroring the working agents.
const SCREEN_KINDS = new Set(["done", "merged", "ci", "api", "ctx"]);

// What the desk shows, for the drawing and the hover alike. Only coding sits
// at it awake (the walled nap is the schedule's, tamagotchi.activityAt): the
// mood shows only with nothing on duty, and the monitor mirrors the agents
// unless the top kind needs the screen.
export function deskView(activity, topKind) {
  const atDesk = activity === "code";
  return { atDesk, moodShown: atDesk && !topKind, showsPanes: atDesk && !SCREEN_KINDS.has(topKind) };
}

// ---- watcher transition (clawdWatch.js runs it on every poll) -------------

// A linked PR's pr_state turning open → merged between two polls throws a
// party. The first sighting of a pane only records its state, so a reload
// never re-throws an old party; panes missing from a poll keep their last
// state, so a rolling reload can't erase the "open" a merge needs to follow.
export function nextMergeWatch({ states, parties }, windows, nowSec) {
  const next = { ...states };
  const out = parties.filter((p) => nowSec - p.at < PARTY_S);
  for (const w of windows || []) {
    if (!w.pid || !w.pr || !w.pr_state) continue;
    const was = states[w.pid];
    next[w.pid] = { pr: w.pr, state: w.pr_state };
    const key = `${w.pid}#${w.pr}`;
    if (was?.pr === w.pr && was.state === "open" && w.pr_state === "merged" && !out.some((p) => p.key === key)) {
      out.push({ key, pid: w.pid, pr: w.pr, at: nowSec });
    }
  }
  return { states: next, parties: out };
}
