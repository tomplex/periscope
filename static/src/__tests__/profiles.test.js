import { describe, expect, it } from "vitest";
import { profileLabel, profileQuery, sendsProfile } from "../profiles.js";

// The launcher's profile → query-param mapping. Same contract as accountQuery:
// the default profile must produce NO `profile=` param, because the server
// fails OPEN on an unknown id (config.profile_env) and omitting keeps the
// default launch byte-identical to the pre-profiles URL.
describe("profileQuery", () => {
  it("omits the param for the default profile", () => {
    expect(profileQuery("default")).toBe(null);
    expect(profileQuery(null)).toBe(null);
    expect(profileQuery(undefined)).toBe(null);
    expect(profileQuery("")).toBe(null);
  });
  it("passes a non-default profile through", () => {
    expect(profileQuery("lab")).toBe("lab");
  });
});

describe("sendsProfile", () => {
  it("binds the profile only for a Claude agent launch", () => {
    expect(sendsProfile({ mode: "agent", agent: "claude" })).toBe(true);
    expect(sendsProfile({ mode: "agent" })).toBe(true);
  });
  it("never binds it for a shell", () => {
    // The env var is set on the WINDOW, so every process in it inherits it. A
    // shell window carrying it would put a hand-typed `claude` on the lab
    // plugin set invisibly — no profile chip renders without a live claude
    // process to read the env off.
    expect(sendsProfile({ mode: "shell", exec: "" })).toBe(false);
    expect(sendsProfile({ mode: "shell", exec: "vim" })).toBe(false);
  });
  it("never binds it for codex, which doesn't go through the claude wrapper", () => {
    expect(sendsProfile({ mode: "agent", agent: "codex" })).toBe(false);
  });
  it("is defensive about a missing target", () => {
    expect(sendsProfile(null)).toBe(false);
  });
});

describe("profileLabel", () => {
  it("names the known profiles", () => {
    expect(profileLabel("lab")).toBe("lab");
    expect(profileLabel("default")).toBe("normal");
  });
  it("falls back to the raw id rather than dropping an unknown one", () => {
    // A pane whose CLAUDE_WRAPPER_PROFILE we don't recognize is demonstrably
    // NOT normal; rendering nothing would assert the opposite of what's true.
    expect(profileLabel("experimental")).toBe("experimental");
  });
});
