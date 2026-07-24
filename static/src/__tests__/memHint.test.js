// memHint — the cycle-hint chip content for a bloated/aged Claude process.
import { describe, expect, it } from "vitest";
import { memHint } from "../util.js";

describe("memHint", () => {
  it("null/undefined mem renders nothing", () => {
    expect(memHint(null)).toBeNull();
    expect(memHint(undefined)).toBeNull();
  });

  it("rss-tripped tier leads with gigabytes", () => {
    const h = memHint({ tier: "bad", rss_gb: 4.2, age_s: 2 * 86400 + 5 * 3600 });
    expect(h.label).toBe("4.2G");
    expect(h.cls).toBe("mem-bad");
    expect(h.title).toContain("4.2GB");
    expect(h.title).toContain("2d 5h");
  });

  it("age-only trip leads with uptime days", () => {
    const h = memHint({ tier: "warn", rss_gb: 0.5, age_s: 3 * 86400 });
    expect(h.label).toBe("3d");
    expect(h.cls).toBe("mem-warn");
  });
});
