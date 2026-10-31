// resources.js — formatting and ordering behind <ResourcePill> and the
// detail header's mem/cpu part.
import { describe, expect, it } from "vitest";
import { fmtCpu, fmtMem, machineLine, ownedMb, paneLines } from "../resources.js";

describe("fmtMem", () => {
  it("megabytes under a gigabyte, one decimal of gigabytes above", () => {
    expect(fmtMem(512)).toBe("512M");
    expect(fmtMem(1024)).toBe("1.0G");
    expect(fmtMem(6963)).toBe("6.8G");
  });
});

describe("fmtCpu", () => {
  it("rounds per-core percent; unknown is a dash, never 0", () => {
    expect(fmtCpu(57.6)).toBe("58%");
    expect(fmtCpu(0)).toBe("0%");
    expect(fmtCpu(null)).toBe("–");
  });
});

describe("ownedMb", () => {
  const g = (mem_mb) => ({ mem_mb, cpu_pct: null });
  it("adds server, webview and panes — not elsewhere", () => {
    expect(ownedMb({ server: g(100), webview: g(2000), panes: g(10000), elsewhere: g(9999) })).toBe(12100);
  });
  it("counts no webview when there is none", () => {
    expect(ownedMb({ server: g(100), webview: null, panes: g(900), elsewhere: g(1) })).toBe(1000);
  });
});

describe("paneLines", () => {
  it("heaviest first, skipping rows without a measurement", () => {
    const a = { pid: "a", res: { mem_mb: 300, cpu_pct: 1 } };
    const b = { pid: "b", res: { mem_mb: 7000, cpu_pct: 58 } };
    const c = { pid: "c", res: null };
    expect(paneLines([a, b, c]).map((l) => l.w.pid)).toEqual(["b", "a"]);
  });
});

describe("machineLine", () => {
  it("load, swap and free memory", () => {
    expect(machineLine({ load1: 52.3, swap_used_mb: 16896, swap_total_mb: 17818, mem_free_pct: 27 }))
      .toBe("load 52.3 · swap 16.5G/17.4G · 27% free");
  });
});
