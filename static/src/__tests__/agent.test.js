import { h } from "preact";
import render from "preact-render-to-string";
import { describe, expect, it } from "vitest";
import { passesFilter } from "../filter.js";
import { Inspector } from "../inspector/Inspector.jsx";
import { computeMode, ModeToggle, PaneHeader, TabStrip } from "../split/Detail.jsx";
import { AGENT_META, paneLabel } from "../util.js";

describe("agent presentation", () => {
  it("labels null and shell panes safely", () => {
    expect(paneLabel(null)).toBe("shell");
    expect(paneLabel({})).toBe("shell");
  });

  it("prefers pane names and falls back to the provider", () => {
    expect(paneLabel({ agent: "codex" })).toBe("codex");
    expect(paneLabel({ name: "review", agent: "codex" })).toBe("review");
  });

  it("includes every provider in the agents filter", () => {
    expect(passesFilter({ agent: "claude" }, "agents")).toBe(true);
    expect(passesFilter({ agent: "codex" }, "agents")).toBe(true);
    expect(passesFilter({ agent: null }, "agents")).toBe(false);
  });

  it("defines presentation metadata for Codex", () => {
    expect(AGENT_META.codex.label).toBe("Codex");
    expect(AGENT_META.codex.className).toBe("icon-codex");
  });

  it("keeps Codex in terminal mode without Claude-only header controls", () => {
    const w = {
      agent: "codex", pid: "codex-1", target: "dev:1", state: "idle",
      mem: { tier: "bad", trigger: "mem", gb: 4, age_s: 86400 },
    };
    expect(computeMode(w)).toBe("terminal");
    const html = render(h(PaneHeader, { w }));
    expect(html).not.toContain("header-rename");
    expect(html).not.toContain("header-mem");
    expect(html).toContain('data-agent="codex"');
  });

  it("keeps the tab list to tabs and the toggle beside it, out of the header", () => {
    const w = { agent: "claude", pid: "claude-1", target: "dev:2", state: "idle" };
    const strip = render(h(TabStrip, { pid: w.pid, paneLabel: "dev" }));
    expect(strip).toContain('role="tablist"');
    expect(strip).not.toContain("Transcript");
    const toggle = render(h(ModeToggle, { mode: "terminal", onMode: () => {} }));
    expect(toggle).toMatch(/class="is-active"[^>]*>Terminal</);
    // The header names the agent (its CSS keeps the toggle's old line height) and holds no toggle.
    const header = render(h(PaneHeader, { w }));
    expect(header).toContain('data-agent="claude"');
    expect(header).not.toContain("Transcript");
  });

  it("does not show Claude channel-link prompts for Codex", () => {
    const html = render(h(Inspector, {
      data: { agent: "codex", pid: "codex-1", pane_id: "%1", activity: [] },
      onRefresh: () => {},
      containerId: "test-inspector",
      containerClass: "test-inspector",
      idPrefix: "test",
    }));
    expect(html).not.toContain("+ link pull request");
    expect(html).not.toContain("+ link Linear ticket");
    expect(html).not.toContain("Respawn Claude");
  });
});
