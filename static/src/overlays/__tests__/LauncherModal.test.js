import { describe, expect, it } from "vitest";
import { sendsAccount } from "../LauncherModal.jsx";

describe("sendsAccount", () => {
  it("binds the account only for a Claude agent launch", () => {
    expect(sendsAccount({ mode: "agent", agent: "claude" })).toBe(true);
  });
  it("never binds it for a shell", () => {
    // A shell window carrying the account ran a hand-typed `claude` on the
    // wrong subscription, invisibly — no account chip renders without a live
    // claude process to read the env off.
    expect(sendsAccount({ mode: "shell", exec: "" })).toBe(false);
    expect(sendsAccount({ mode: "shell", exec: "vim" })).toBe(false);
  });
  it("never binds it for codex, which has no Claude subscription", () => {
    expect(sendsAccount({ mode: "agent", agent: "codex" })).toBe(false);
  });
  it("is defensive about a missing target", () => {
    expect(sendsAccount(null)).toBe(false);
  });
});
