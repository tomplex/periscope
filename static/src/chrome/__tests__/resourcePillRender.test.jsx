// Render-path test for <ResourcePill>. Same rationale/technique as
// usagePillRender.test.jsx: catches wiring errors a build alone can't.

import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { resources, windows } from "../../store.js";
import { ResourcePill } from "../ResourcePill.jsx";

const g = (mem_mb, cpu_pct) => ({ mem_mb, cpu_pct });
const R = {
  server: g(176, 7),
  webview: g(2900, 40),
  panes: g(12800, 112),
  elsewhere: g(30000, 300),
  machine: { load1: 52.3, ram_mb: 16384, swap_used_mb: 16896, swap_total_mb: 17818, mem_free_pct: 27 },
};

afterEach(() => {
  resources.value = null;
  windows.value = [];
});

describe("<ResourcePill>", () => {
  it("renders nothing without a measurement", () => {
    expect(render(<ResourcePill />)).toBe("");
  });

  it("shows what periscope and its panes hold", () => {
    resources.value = R;
    const html = render(<ResourcePill />);
    expect(html).toContain("▣ 15.5G");  // 176 + 2900 + 12800 MB: elsewhere is not periscope's
  });

  it("lists the owners, panes heaviest first, then elsewhere and the machine", () => {
    resources.value = R;
    windows.value = [
      { pid: "a", name: "light", res: g(300, 1) },
      { pid: "b", name: "heavy", res: g(6963, 58) },
    ];
    const html = render(<ResourcePill />);
    const order = [">periscope server<", ">webview<", ">heavy<", ">light<", ">elsewhere<", "16.0G RAM"];
    const at = order.map((s) => html.indexOf(s));
    expect(at.every((i) => i >= 0)).toBe(true);
    expect([...at].sort((x, y) => x - y)).toEqual(at);
    expect(html).toContain("6.8G");
    expect(html).toContain("58%");
  });

  it("leaves the webview line out when there is no app", () => {
    resources.value = { ...R, webview: null };
    expect(render(<ResourcePill />)).not.toContain(">webview<");
  });
});
