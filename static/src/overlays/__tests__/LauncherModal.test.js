import { describe, expect, it } from "vitest";
import { accountQuery } from "../LauncherModal.jsx";

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
