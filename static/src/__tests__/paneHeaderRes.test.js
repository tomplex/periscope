// The detail header's "6.8G mem · 58% cpu" part: the selected pane's whole
// process tree (the row's `res`).
import { h } from "preact";
import render from "preact-render-to-string";
import { describe, expect, it } from "vitest";
import { PaneHeader } from "../split/Detail.jsx";

const base = { pid: "p1", target: "dev:1", state: "shell" };

describe("PaneHeader resources", () => {
  it("labels memory and CPU", () => {
    const html = render(h(PaneHeader, { w: { ...base, res: { mem_mb: 6963, cpu_pct: 57.6 } } }));
    expect(html).toContain("6.8G mem · 58% cpu");
  });

  it("shows a dash before the server has two samples", () => {
    const html = render(h(PaneHeader, { w: { ...base, res: { mem_mb: 300, cpu_pct: null } } }));
    expect(html).toContain("300M mem · – cpu");
  });

  it("shows nothing without a measurement", () => {
    expect(render(h(PaneHeader, { w: { ...base, res: null } }))).not.toContain("header-res");
  });
});
