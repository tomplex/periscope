// Render-path smoke test for <UsagePill> — the reduction logic is covered in
// usageSummary.test.js; this catches wiring errors (bad prop names, a meter
// list that never reaches MeterBar) that a build alone can't. Same rationale
// and technique as railRender.test.jsx: this repo browser-verifies instead of
// mounting components elsewhere.

import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { usage } from "../../store.js";
import { UsagePill } from "../UsagePill.jsx";

const NOW = () => Math.floor(Date.now() / 1000);

function meters(pairs) {
  const m = {};
  for (const [k, percent] of Object.entries(pairs)) m[k] = { label: k, percent };
  return m;
}

afterEach(() => {
  usage.value = null;
});

describe("<UsagePill>", () => {
  it("collapses to one binding meter per account, A then B", () => {
    usage.value = {
      plan: {
        b: { available: true, fetched_at: NOW(), meters: meters({ session: 19, week_all: 2 }) },
        default: {
          available: true,
          fetched_at: NOW(),
          meters: meters({ session: 0, week_all: 100, week_fable: 5 }),
        },
      },
      fallback: null,
    };
    const html = render(<UsagePill />);
    expect(html.match(/usage-acct-label/g)).toHaveLength(2);
    // One bar per account — the binding one, not all five meters.
    expect(html.match(/usage-item-bar/g)).toHaveLength(2);
    expect(html.indexOf(">A<")).toBeLessThan(html.indexOf(">B<"));
    // A is pinned at its weekly wall (danger tone), B has room.
    expect(html).toContain("usage-item-fill danger");
    expect(html).toContain("usage-item-fill ok");
    expect(html).toContain("<b>100%</b>");
    expect(html).toContain("<b>19%</b>"); // B's session, its highest meter
  });

  it("shows a credential-less account rather than dropping it", () => {
    usage.value = {
      plan: {
        default: { available: true, fetched_at: NOW(), meters: meters({ week_all: 100 }) },
        b: { available: false },
      },
      fallback: null,
    };
    const html = render(<UsagePill />);
    expect(html).toContain("usage-acct-off");
    expect(html).toContain("no data");
  });

  it("greys only the stale account", () => {
    usage.value = {
      plan: {
        default: { available: true, fetched_at: NOW() - 7200, meters: meters({ week_all: 100 }) },
        b: { available: true, fetched_at: NOW(), meters: meters({ week_all: 2 }) },
      },
      fallback: null,
    };
    const html = render(<UsagePill />);
    expect(html.match(/usage-acct is-stale/g)).toHaveLength(1);
  });

  it("falls back to the JSONL estimate only when no account has meters", () => {
    usage.value = {
      plan: { default: { available: false }, b: { available: false } },
      fallback: { available: true, messages: 3, input_tokens: 2000, reset_at: NOW() + 600 },
    };
    expect(render(<UsagePill />)).toContain("usage-fallback");
  });

  it("keeps one live account off the fallback path", () => {
    usage.value = {
      plan: { default: { available: false }, b: { available: true, fetched_at: NOW(), meters: meters({ week_all: 2 }) } },
      fallback: { available: true, messages: 3, input_tokens: 2000, reset_at: NOW() + 600 },
    };
    expect(render(<UsagePill />)).not.toContain("usage-fallback");
  });

  it("names the morning poke and the reset it anchored in the account tooltip", () => {
    const at = NOW() - 3600;
    usage.value = {
      plan: { b: { available: true, fetched_at: NOW(), meters: meters({ session: 9 }) } },
      fallback: null,
      poke: { b: { date: "2026-09-08", at, resets_at: at + 5 * 3600, verified: true } },
    };
    expect(render(<UsagePill />)).toMatch(/poked [^→]+ → resets /);
    usage.value = {
      ...usage.value,
      poke: { b: { date: "2026-09-08", at, resets_at: null, verified: false } },
    };
    expect(render(<UsagePill />)).toContain("→ not anchored");
  });

  it("marks an account 💤 when its Fable budget is on pace to go unused", () => {
    const soon = NOW() + 30 * 3600;
    usage.value = {
      plan: {
        default: {
          available: true, fetched_at: NOW(),
          meters: { week_all: { label: "w", percent: 4, projected_percent: 13, resets_at: NOW() + 100 * 3600 },
                    week_fable: { label: "f", percent: 4, projected_percent: 13, resets_at: NOW() + 100 * 3600 } },
        },
        b: {
          available: true, fetched_at: NOW(),
          meters: { week_all: { label: "w", percent: 9, projected_percent: 11, resets_at: soon },
                    week_fable: { label: "f", percent: 14, projected_percent: 18, resets_at: soon } },
        },
      },
      fallback: null,
    };
    const html = render(<UsagePill />);
    expect(html.match(/usage-acct-waste/g)).toHaveLength(1);
    expect(html).toContain("on pace for 18% at reset");
  });
});
