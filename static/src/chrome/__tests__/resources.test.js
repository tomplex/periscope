// resources.js — formatting and ordering behind <ResourcePill> and the
// detail header's mem/cpu part.
import { describe, expect, it } from "vitest";
import { fmtCpu, fmtMem, machineLine, ownedMb, paneLines, refreshLine, refreshTip } from "../resources.js";

describe("fmtMem", () => {
  it("megabytes under a gigabyte, one decimal of gigabytes above", () => {
    expect(fmtMem(512)).toBe("512M");
    expect(fmtMem(1024)).toBe("1.0G");
    expect(fmtMem(6963)).toBe("6.8G");
  });
});

describe("fmtCpu", () => {
  it("one decimal under 10%, whole above; unknown is a dash, never 0", () => {
    expect(fmtCpu(57.6)).toBe("58%");
    expect(fmtCpu(4.66)).toBe("4.7%");
    expect(fmtCpu(0)).toBe("0.0%");
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
  it("RAM first, so owners adding up past it read as overcommitment", () => {
    expect(machineLine({ load1: 52.3, ram_mb: 16384, swap_used_mb: 16896, swap_total_mb: 17818, mem_free_pct: 27 }))
      .toBe("16.0G RAM · swap 16.5G/17.4G · 27% free · load 52.3");
  });
});

describe("refreshLine", () => {
  it("build time, the push interval and forks per minute", () => {
    expect(refreshLine({ build_s: 0.43, build_max_s: 1.2, gap_s: 1.07, forks_per_min: 140, top_forks: [] }))
      .toBe("refresh 0.4 s · every 1.1 s · 140 forks/min");
  });
  it("leaves the interval out until the server has pushed twice", () => {
    expect(refreshLine({ build_s: 0.4, build_max_s: 0.4, gap_s: null, forks_per_min: 9, top_forks: [] }))
      .toBe("refresh 0.4 s · 9 forks/min");
  });
});

describe("refreshTip", () => {
  it("the slowest build in the last minute, then the top commands", () => {
    expect(refreshTip({ build_s: 0.4, build_max_s: 2.31, gap_s: 1, forks_per_min: 52, top_forks: [["git status", 40], ["ps", 12]] }))
      .toBe("slowest 2.3 s in the last minute\ngit status ×40\nps ×12");
  });
});
