import { describe, expect, it } from "vitest";
import { accountQuery, sendsAccount } from "../LauncherModal.jsx";

// The launcher's account → query-param mapping. The default account must
// produce NO `account=` param at all: the server fails OPEN on an unknown id
// (store.account_config_dir), so sending a string is the risky direction and
// omitting it keeps the default path byte-identical to the pre-accounts URL.
describe("accountQuery", () => {
  it("omits the param for the default account", () => {
    expect(accountQuery("default")).toBe(null);
    expect(accountQuery(null)).toBe(null);
    expect(accountQuery(undefined)).toBe(null);
  });
  it("passes a non-default account through", () => {
    expect(accountQuery("b")).toBe("b");
  });
});

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
