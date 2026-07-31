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
});
