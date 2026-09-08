// Reduces /api/state's `usage_plan` — a mapping of account id → that
// subscription's plan meters — to the rows the usage pill renders.
//
// The pill answers one question at a glance: WHICH ACCOUNT HAS ROOM. That is
// why each account collapses to a single `headline` meter (its highest-percent
// one, i.e. the limit that actually binds right now) while `meters` keeps the
// full ordered set for the expanded view.
import { ACCOUNTS, accountLabel } from "../accounts.js";

// Stale once the fetch is two refresh intervals old (server refreshes every
// 5 min on success) — beyond that the server is failing to fetch, not just
// between refreshes, and the percentages describe the past. Per ACCOUNT: one
// dead credential must not grey out the other subscription's live numbers.
export const STALE_AFTER_S = 600;

const KNOWN_METERS = ["session", "week_all", "week_opus", "week_sonnet"];
const METER_LABELS = {
  session: "session",
  week_all: "week",
  week_opus: "opus",
  week_sonnet: "sonnet",
};

// Scoped per-model meters (week_fable, ...) are keyed dynamically by the
// server and differ between accounts — an account can be missing a meter the
// other one has. Never assume a fixed key set.
function meterLabel(k) {
  return METER_LABELS[k] || k.replace(/^week_/, "");
}

function orderMeters(meters) {
  const extra = Object.keys(meters).filter((k) => !KNOWN_METERS.includes(k)).sort();
  return [...KNOWN_METERS, ...extra]
    .filter((k) => meters[k])
    .map((k) => ({ key: k, label: meterLabel(k), m: meters[k] }));
}

// Registered accounts first, in their canonical A/B order; anything else
// (a hand-edited registry) sorted after, so an unknown id is visible rather
// than silently dropped.
function orderAccounts(ids) {
  const known = ACCOUNTS.map((a) => a.id).filter((id) => ids.includes(id));
  const rest = ids.filter((id) => !known.includes(id)).sort();
  return [...known, ...rest];
}

/** usage_plan mapping → [{ id, label, available, stale, fetchedAt, meters, headline }]. */
export function summarizeAccounts(plan, nowSec) {
  if (!plan) return [];
  return orderAccounts(Object.keys(plan)).map((id) => {
    const entry = plan[id] || {};
    const meters = entry.available && entry.meters ? orderMeters(entry.meters) : [];
    // `available: true` with an empty meter set says nothing renderable — treat
    // it as unavailable so the row shows "—" instead of a bare letter.
    const available = meters.length > 0;
    // Max percent, first-in-canonical-order on a tie. Ties are common at 0%,
    // where "session" is the more honest headline than an arbitrary week meter.
    const headline = available
      ? meters.reduce((best, x) => ((x.m.percent || 0) > (best.m.percent || 0) ? x : best))
      : null;
    return {
      id,
      label: accountLabel(id),
      available,
      stale: available && !!entry.fetched_at && nowSec - entry.fetched_at > STALE_AFTER_S,
      fetchedAt: entry.fetched_at || null,
      meters,
      headline,
    };
  });
}

// Whether an account's Fable budget is on pace to go unused: its week_fable*
// meter projects under 100% at reset AND the reset is within 48h — past the
// point where the remaining budget is likely to be burned. The inverse of the
// server's 🔥 signal, from the same projected_percent field. Takes the raw
// plan entry (not a summarizeAccounts row) so it composes with either.
//
// The prefix scan mirrors launch_policy.sublimit on the server: sub-limit keys
// are slugified display names, so "Fable 5.1" would arrive as week_fable_5_1.
// Duplicated here in four lines rather than stamped server-side so this
// display rule ships without a Python change; move it if a second rule appears.
export const WASTE_HORIZON_S = 48 * 3600;

export function wasteMark(entry, nowSec) {
  const meters = (entry?.available && entry.meters) || {};
  const key = Object.keys(meters).find((k) => k === "week_fable" || k.startsWith("week_fable_"));
  const m = key && meters[key];
  if (!m || m.projected_percent == null || !m.resets_at) return false;
  return m.projected_percent < 100 && m.resets_at - nowSec < WASTE_HORIZON_S;
}
