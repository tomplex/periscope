import { describe, expect, it } from "vitest";
import { mermaidTheme } from "../mermaidTheme.js";

describe("mermaidTheme", () => {
  const hex = {
    "--bg-1": "#111111", "--bg-1-hi": "#1a1a1a", "--bg-2": "#222222",
    "--fg-0": "#f0f0f0", "--fg-1": "#d0d0d0", "--fg-2": "#a0a0a0",
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
      primaryBorderColor: "#a0a0a0",
      lineColor: "#a0a0a0",
      clusterBorder: "#a0a0a0",
      edgeLabelBackground: "#111111",
      background: "#111111",
      clusterBkg: "#1a1a1a",
      secondaryColor: "#1a1a1a",
      textColor: "#d0d0d0",
      fontFamily: "Hanken Grotesk, sans-serif",
      fontSize: "14px",
    });
  });
  it("reads only opaque tokens, which the canvas converter can paint", () => {
    const asked = new Set();
    mermaidTheme({ color: (n) => { asked.add(n); return "#000000"; }, font: "x" });
    expect([...asked].sort()).toEqual(["--bg-1", "--bg-1-hi", "--bg-2", "--fg-0", "--fg-1", "--fg-2"]);
  });
});
