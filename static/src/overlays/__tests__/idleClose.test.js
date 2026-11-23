import { describe, expect, it } from "vitest";
import { idleCloseLine } from "../idleClose.js";

const row = (name, verdict = "closes") => ({ name, verdict });

describe("idleCloseLine", () => {
  it("counts and names the panes that close", () => {
    expect(idleCloseLine([row("notify-buttons"), row("pinned-one", "pinned"), row("bq-cost-guard")])).toBe(
      "2 panes close at 04:00 tonight: notify-buttons, bq-cost-guard",
    );
    expect(idleCloseLine([row("notify-buttons")])).toBe("1 pane closes at 04:00 tonight: notify-buttons");
  });

  it("says so when no pane closes", () => {
    expect(idleCloseLine([row("a", "weekend")])).toBe("No pane closes at 04:00 tonight");
  });
});
