import { describe, expect, it } from "vitest";
import { mermaidTheme } from "../mermaidTheme.js";

describe("mermaidTheme", () => {
  const hex = {
    "--bg-0": "#000000", "--bg-1": "#111111", "--bg-1-hi": "#1a1a1a", "--bg-2": "#222222",
    "--fg-0": "#f0f0f0", "--fg-1": "#d0d0d0", "--fg-2": "#a0a0a0",
    "--accent": "#ff0011", "--s-done": "#22cc33", "--s-working": "#3366ff", "--s-needs": "#ffaa00",
    "--effort": "#aa33ff", "--worktree": "#00bbcc", "--s-shell": "#888888", "--s-danger": "#ee2244",
  };
  const t = mermaidTheme({ color: (n) => hex[n], font: "Hanken Grotesk, sans-serif" });

  it("uses mermaid's configurable base theme in dark mode", () => {
    expect(t.theme).toBe("base");
    expect(t.themeVariables.darkMode).toBe(true);
  });
  it("maps periscope's tokens onto nodes, lines, labels and groups", () => {
    expect(t.themeVariables).toMatchObject({
      primaryColor: "#222222",
      primaryTextColor: "#f0f0f0",
      nodeTextColor: "#f0f0f0",
      titleColor: "#f0f0f0",
      primaryBorderColor: "#a0a0a0",
      lineColor: "#a0a0a0",
      clusterBorder: "#a0a0a0",
      edgeLabelBackground: "#111111",
      background: "#111111",
      clusterBkg: "#1a1a1a",
      secondaryColor: "#1a1a1a",
      tertiaryColor: "#111111",
      textColor: "#d0d0d0",
      fontFamily: "Hanken Grotesk, sans-serif",
      fontSize: "14px",
    });
  });
  it("cycles the dashboard's category colors for pie slices and git branches, not primaryColor's hue", () => {
    const cats = ["#ff0011", "#22cc33", "#3366ff", "#ffaa00", "#aa33ff", "#00bbcc", "#888888", "#ee2244"];
    for (let i = 0; i < cats.length; i++) {
      expect(t.themeVariables[`pie${i + 1}`]).toBe(cats[i]);
      expect(t.themeVariables[`git${i}`]).toBe(cats[i]);
    }
  });
  it("labels category fills in dark text, for the mid-light fills", () => {
    expect(t.themeVariables.cScaleLabel0).toBe("#000000");
    expect(t.themeVariables.gitBranchLabel0).toBe("#000000");
  });
  it("reads only opaque tokens, which the canvas converter can paint", () => {
    const asked = new Set();
    mermaidTheme({ color: (n) => { asked.add(n); return "#000000"; }, font: "x" });
    expect([...asked].sort()).toEqual([
      "--accent", "--bg-0", "--bg-1", "--bg-1-hi", "--bg-2", "--effort", "--fg-0", "--fg-1", "--fg-2",
      "--s-danger", "--s-done", "--s-needs", "--s-shell", "--s-working", "--worktree",
    ]);
  });
  it("drops a key whose token is missing, instead of passing mermaid an explicit undefined", () => {
    const missingFg1 = { ...hex, "--fg-1": undefined };
    const t2 = mermaidTheme({ color: (n) => missingFg1[n], font: "x" });
    expect("textColor" in t2.themeVariables).toBe(false);
  });
});
