import { describe, expect, it } from "vitest";
import { h } from "preact";
import render from "preact-render-to-string";
import { passesFilter } from "../filter.js";
import { Inspector } from "../inspector/Inspector.jsx";
import { computeMode, PaneHeader } from "../split/Detail.jsx";
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
      mem: { rss_gb: 4, age_s: 86400 },
    };
    expect(computeMode(w)).toBe("terminal");
    const html = render(h(PaneHeader, { w, mode: "terminal", onMode: () => {} }));
    expect(html).not.toContain("Transcript");
    expect(html).not.toContain("header-rename");
    expect(html).not.toContain("header-mem");
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
