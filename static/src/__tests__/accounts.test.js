// The move-account decision: given a pane and the account registry, where does
// "move this to the other subscription" send it? Pure, so the row component
// only has to render the answer.
import { describe, expect, it } from "vitest";
import { moveAccountTarget } from "../accounts.js";

const claude = (over) => ({ agent: "claude", ...over });

describe("moveAccountTarget", () => {
  it("sends a default-account Claude pane to B", () => {
    expect(moveAccountTarget(claude({ account: "default" }))).toEqual({ id: "b", label: "B" });
  });

  it("sends a B pane back to A", () => {
    expect(moveAccountTarget(claude({ account: "b" }))).toEqual({ id: "default", label: "A" });
  });

  it("treats a pane with no account field as the default account", () => {
    // /api/state stamps `account` on every window, but a rolling reload can
    // serve rail rows from a pre-account server — B is still the right target.
    expect(moveAccountTarget(claude({}))).toEqual({ id: "b", label: "B" });
  });

  it("offers nothing for a shell pane", () => {
    // No Claude session means nothing to resume.
    expect(moveAccountTarget({ agent: null, account: "default" })).toBeNull();
  });

  it("offers nothing for a pane on an unregistered config dir", () => {
    // "unknown" = a CLAUDE_CONFIG_DIR no account claims. We can't say which
    // subscription it is on, so we can't say which one is "the other" — and
    // guessing could move it onto the exhausted one.
    expect(moveAccountTarget(claude({ account: "unknown" }))).toBeNull();
  });

  it("honours the legacy is_claude flag during a rolling reload", () => {
    expect(moveAccountTarget({ is_claude: true, account: "default" }))
      .toEqual({ id: "b", label: "B" });
  });
});
