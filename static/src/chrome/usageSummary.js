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

/** The account with the most headroom, or null when no account has usable data.
 *
 * "Most headroom" is the LOWEST headline percent — the headline is whichever
 * limit binds first, so an account at 8% weekly but 95% session has 95% of
 * headroom gone, not 8%. Accounts with no meters are skipped rather than
 * treated as empty: no data must never look like infinite room.
 *
 * Ties are broken randomly (injectable for tests) so identical accounts share
 * load instead of every new tab piling onto whichever sorts first.
 */
export function bestAccount(plan, nowSec, rand = Math.random) {
  const usable = summarizeAccounts(plan, nowSec).filter((a) => a.available && a.headline);
  if (!usable.length) return null;
  const low = Math.min(...usable.map((a) => a.headline.m.percent || 0));
  const tied = usable.filter((a) => (a.headline.m.percent || 0) === low);
  return tied[Math.floor(rand() * tied.length)].id;
}
