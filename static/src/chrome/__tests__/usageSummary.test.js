import { describe, expect, it } from "vitest";
import { STALE_AFTER_S, summarizeAccounts, WASTE_HORIZON_S, wasteMark } from "../usageSummary.js";

const NOW = 1_800_000_000;

// A minimal plan entry: `meters` keyed by meter name, each with a percent.
function acct(meters, fetchedAt = NOW) {
  const m = {};
  for (const [k, percent] of Object.entries(meters)) m[k] = { label: k, percent };
  return { available: true, meters: m, fetched_at: fetchedAt };
}

describe("summarizeAccounts", () => {
  it("returns one row per account, in A/B order, whatever the key order", () => {
    const rows = summarizeAccounts({ b: acct({ week_all: 2 }), default: acct({ week_all: 100 }) }, NOW);
    expect(rows.map((r) => [r.id, r.label])).toEqual([
      ["default", "A"],
      ["b", "B"],
    ]);
  });

  it("headlines the highest-percent meter — the account's binding limit", () => {
    const [a] = summarizeAccounts({ default: acct({ session: 12, week_all: 100, week_fable: 40 }) }, NOW);
    expect(a.headline.key).toBe("week_all");
    expect(a.headline.label).toBe("week");
    expect(a.headline.m.percent).toBe(100);
  });

  it("breaks a headline tie toward the earlier meter in canonical order", () => {
    const [a] = summarizeAccounts({ default: acct({ week_fable: 50, session: 50 }) }, NOW);
    expect(a.headline.key).toBe("session");
  });

  it("orders meters known-first, then unknown keys alphabetically", () => {
    const [a] = summarizeAccounts(
      { default: acct({ week_zeta: 1, week_fable: 2, week_all: 3, session: 4 }) },
      NOW,
    );
    expect(a.meters.map((x) => x.key)).toEqual(["session", "week_all", "week_fable", "week_zeta"]);
    // week_ prefix stripped for the compact label; known keys keep their word.
    expect(a.meters.map((x) => x.label)).toEqual(["session", "week", "fable", "zeta"]);
  });

  it("keeps a credential-less account as an unavailable row, not a missing one", () => {
    const rows = summarizeAccounts({ default: acct({ week_all: 100 }), b: { available: false } }, NOW);
    expect(rows[1]).toMatchObject({ id: "b", label: "B", available: false, headline: null });
    expect(rows[1].meters).toEqual([]);
  });

  it("treats available-but-meterless as unavailable", () => {
    const [a] = summarizeAccounts({ default: { available: true, meters: {} } }, NOW);
    expect(a.available).toBe(false);
  });

  it("flags staleness per account, not for the whole pill", () => {
    const rows = summarizeAccounts(
      {
        default: acct({ week_all: 100 }, NOW - STALE_AFTER_S - 1),
        b: acct({ week_all: 2 }, NOW - STALE_AFTER_S + 1),
      },
      NOW,
    );
    expect(rows.map((r) => r.stale)).toEqual([true, false]);
  });

  it("never marks an unavailable account stale — it has no fetch to age", () => {
    const [a] = summarizeAccounts({ default: { available: false } }, NOW);
    expect(a.stale).toBe(false);
  });

  it("returns [] for a missing plan so the pill falls back to the JSONL estimate", () => {
    expect(summarizeAccounts(null, NOW)).toEqual([]);
    expect(summarizeAccounts({}, NOW)).toEqual([]);
  });

  it("labels an unregistered account id with the id itself, sorted after the known two", () => {
    const rows = summarizeAccounts({ zz: acct({ week_all: 5 }), b: acct({ week_all: 2 }) }, NOW);
    expect(rows.map((r) => r.label)).toEqual(["B", "zz"]);
  });
});

describe("wasteMark", () => {
  const H = 3600;
  const fable = (projected, resetsIn, key = "week_fable") => ({
    available: true,
    meters: {
      week_all: { percent: 9, projected_percent: 11, resets_at: NOW + resetsIn },
      [key]: { percent: 14, projected_percent: projected, resets_at: NOW + resetsIn },
    },
  });

  it("marks a Fable budget on pace to go unused within 48h of its reset", () => {
    expect(wasteMark(fable(18, 30 * H), NOW)).toBe(true);
  });

  it("matches the sub-limit by prefix, like the server's sublimit()", () => {
    expect(wasteMark(fable(18, 30 * H, "week_fable_5_1"), NOW)).toBe(true);
  });

  it("stays quiet while there is more than 48h to burn it", () => {
    expect(wasteMark(fable(18, WASTE_HORIZON_S + H), NOW)).toBe(false);
    expect(wasteMark(fable(18, WASTE_HORIZON_S - H), NOW)).toBe(true);
  });

  it("stays quiet on a reset already in the past — stale data, not an expiring budget", () => {
    expect(wasteMark(fable(18, -H), NOW)).toBe(false);
  });

  it("stays quiet when the pace reaches 100%", () => {
    expect(wasteMark(fable(100, 30 * H), NOW)).toBe(false);
    expect(wasteMark(fable(140, 30 * H), NOW)).toBe(false);
  });

  it("needs a projection and a Fable meter", () => {
    expect(wasteMark(fable(null, 30 * H), NOW)).toBe(false);
    expect(wasteMark({ available: true, meters: { week_all: { percent: 9, projected_percent: 11, resets_at: NOW + 30 * H } } }, NOW)).toBe(false);
    expect(wasteMark({ available: false }, NOW)).toBe(false);
  });
});
