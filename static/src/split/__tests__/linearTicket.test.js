import { describe, expect, it } from "vitest";
import { linearTicket as historyLinearTicket } from "../../../util.js";
import { linearTicket as dashboardLinearTicket } from "../linearTicket.js";

// static/util.js keeps a copy for the /history page: both copies give one answer.
describe.each([
  ["dashboard", dashboardLinearTicket],
  ["history page", historyLinearTicket],
])("linearTicket on the %s", (_page, linearTicket) => {
  it("is the ticket Claude linked, with its title and status", () => {
    const t = linearTicket({ linked_linear: "FAR-456", linked_linear_title: "Fix the thing", linked_linear_status: "In Progress" });
    expect(t).toEqual({
      id: "FAR-456",
      href: "https://linear.app/issue/FAR-456",
      tip: "Linear FAR-456: Fix the thing [In Progress]",
      note: null,
      derived: false,
    });
  });

  it("falls back to the ticket the branch or PR title names, saying where it came from", () => {
    expect(linearTicket({ linear_ticket: "FDY-8562", linear_ticket_source: "branch" })).toEqual({
      id: "FDY-8562",
      href: "https://linear.app/issue/FDY-8562",
      tip: "Linear FDY-8562 (from the branch name)",
      note: "from the branch name",
      derived: true,
    });
    expect(linearTicket({ linear_ticket: "THI-441", linear_ticket_source: "pr_title" }).tip).toBe(
      "Linear THI-441 (from the PR title)",
    );
  });

  it("prefers the linked ticket over a derived one", () => {
    expect(linearTicket({ linked_linear: "FAR-456", linear_ticket: "FDY-8562" }).id).toBe("FAR-456");
  });

  it("is null with neither", () => {
    expect(linearTicket({})).toBeNull();
    expect(linearTicket(null)).toBeNull();
  });
});
