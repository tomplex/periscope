// memHint — the cycle-hint chip content for a bloated/aged Claude process.
import { describe, expect, it } from "vitest";
import { memHint } from "../util.js";

describe("memHint", () => {
  it("null/undefined mem renders nothing", () => {
    expect(memHint(null)).toBeNull();
    expect(memHint(undefined)).toBeNull();
  });

  it("a footprint trip leads with gigabytes", () => {
    const h = memHint({ tier: "bad", trigger: "mem", gb: 4.2, age_s: 2 * 86400 + 5 * 3600 });
    expect(h.label).toBe("4.2G");
    expect(h.cls).toBe("mem-bad");
    expect(h.title).toContain("4.2GB");
    expect(h.title).toContain("2d 5h");
  });

  it("an age trip leads with uptime days", () => {
    const h = memHint({ tier: "warn", trigger: "age", gb: 0.5, age_s: 3 * 86400 });
    expect(h.label).toBe("3d");
    expect(h.cls).toBe("mem-warn");
  });

  it("an age trip with an unreadable footprint still explains itself", () => {
    const h = memHint({ tier: "warn", trigger: "age", gb: null, age_s: 3 * 86400 });
    expect(h.title).toContain("running 3d 0h");
    expect(h.title).not.toContain("null");
  });
});
