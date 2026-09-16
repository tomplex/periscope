// The move-account decision: given a pane, the registry, and the server's
// move_targets, where does "move this to another subscription" send it? Pure,
// so the row component only has to render the answer.
import { describe, expect, it } from "vitest";
import { accountLabel, moveAccountTarget, multiAccount } from "../accounts.js";

const claude = (over) => ({ agent: "claude", ...over });
const A = { id: "default", label: "A" };
const B = { id: "b", label: "B" };
const C = { id: "c", label: "C" };
const REG = [A, B, C];

describe("moveAccountTarget", () => {
  it("names exactly the server's pick, not the next account in order", () => {
    expect(moveAccountTarget(claude({ account: "default" }), REG, { default: "c" })).toEqual(C);
  });

  it("treats a pane with no account field as the default account", () => {
    // /api/state stamps `account` on every window, but a rolling reload can
    // serve rail rows from a pre-account server.
    expect(moveAccountTarget(claude({}), REG, { default: "b" })).toEqual(B);
  });

  it("offers nothing for a shell pane", () => {
    expect(moveAccountTarget({ agent: null, account: "default" }, REG, { default: "b" })).toBeNull();
  });

  it("offers nothing for a pane on an unregistered config dir", () => {
    // "unknown" = a CLAUDE_CONFIG_DIR no account claims. We can't say which
    // subscription it is on — guessing could move it onto the exhausted one.
    expect(moveAccountTarget(claude({ account: "unknown" }), REG, { unknown: "b" })).toBeNull();
  });

  it("offers nothing when the server sent no target (single account, or an older poll)", () => {
    expect(moveAccountTarget(claude({ account: "default" }), [A], {})).toBeNull();
  });

  it("never offers the pane's own account or an unregistered target", () => {
    expect(moveAccountTarget(claude({ account: "b" }), REG, { b: "b" })).toBeNull();
    expect(moveAccountTarget(claude({ account: "b" }), REG, { b: "zz" })).toBeNull();
  });

  it("honours the legacy is_claude flag during a rolling reload", () => {
    expect(moveAccountTarget({ is_claude: true, account: "default" }, REG, { default: "b" })).toEqual(B);
  });
});

describe("multiAccount / accountLabel", () => {
  it("is false for one account and true for two", () => {
    expect(multiAccount([A])).toBe(false);
    expect(multiAccount([A, B])).toBe(true);
  });

  it("labels a registered id with its letter and an unknown id with itself", () => {
    expect(accountLabel("c", REG)).toBe("C");
    expect(accountLabel("zz", REG)).toBe("zz");
  });
});
