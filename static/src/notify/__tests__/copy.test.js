import { describe, expect, it, vi } from "vitest";
import { waitKind } from "../../util.js";
import { firstSentence, payloadFor } from "../copy.js";

// actions.js (imported below for the category cross-check) pulls in UI
// modules that reach tauri.js; feed.test.js's mock, unused here otherwise.
vi.mock("../../tauri.js", () => ({
  inTauri: () => false, notify: vi.fn(), onNotificationClick: vi.fn(), onNotificationOpened: vi.fn(),
  onNotificationAction: vi.fn(), openExternal: vi.fn(), setBadgeCount: vi.fn(), setNotifyCategories: vi.fn(async () => {}),
}));
const { categories } = await import("../actions.js");
const { summaryPayload } = await import("../burst.js");

const LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])/;

const w = (over = {}) => ({ pid: "p1", target: "s:1", session: "periscope", name: "dual-moas", branch: "main", agent: "claude", ...over });

describe("payloadFor", () => {
  it("words a needs-input banner by what the pane waits on", () => {
    const p = payloadFor({ kind: "needs_input", w: w({ waiting_for: "permission prompt" }) });
    expect(p).toMatchObject({
      kind: "needs_input", thread: "needs_input", target: "s:1", title: "dual-moas needs you",
      subtitle: "periscope · main", body: "Waiting for your approval.", data: { open: "pane", pid: "p1" },
    });
    expect(payloadFor({ kind: "needs_input", w: w({ waiting_for: "approve AskUserQuestion" }) }).body).toBe("Waiting for your answer.");
    expect(payloadFor({ kind: "needs_input", w: w() }).body).toBe("Waiting for your input.");
  });

  it("clips a long pane name so the verb survives macOS's truncation", () => {
    const p = payloadFor({ kind: "needs_input", w: w({ name: "a-really-long-pane-name-that-goes-on-and-on" }) });
    expect(p.title.length).toBeLessThanOrEqual(40);
    expect(p.title.endsWith("… needs you")).toBe(true);
  });

  it("clips a pane name by code point, never splitting a surrogate pair", () => {
    // 26-char clip cuts after the 24 a's, right where the emoji (a surrogate
    // pair) starts: a code-unit slice would take the a's plus the emoji's
    // lone high surrogate, which is exactly the bug this guards against.
    const p = payloadFor({ kind: "needs_input", w: w({ name: `${"a".repeat(24)}😀bbbb` }) });
    expect(LONE_SURROGATE.test(p.title)).toBe(false);
  });

  it("carries the agent's question, even for a pane that has gone", () => {
    const alert = { target: "gone:2", name: "old", session: "s", message: "Which database should I use?" };
    expect(payloadFor({ kind: "question", w: w(), alert })).toMatchObject({ title: "dual-moas asks", body: "Which database should I use?" });
    expect(payloadFor({ kind: "question", w: null, alert })).toMatchObject({ title: "old asks", subtitle: "s", target: "gone:2" });
  });

  it("names a finished pane's linked PR, and its last sentence", () => {
    const p = payloadFor({ kind: "done", w: w({ pr: 29, pr_linked: true, recap: "Opened the PR. Tests pass." }) });
    expect(p).toMatchObject({ title: "dual-moas is done", subtitle: "periscope · PR #29", body: "Opened the PR." });
    // An auto-detected PR is just the branch's: the subtitle stays session · branch.
    expect(payloadFor({ kind: "done", w: w({ pr: 29 }) }).subtitle).toBe(payloadFor({ kind: "done", w: w() }).subtitle);
    expect(payloadFor({ kind: "done", w: w() }).body).toBe("Finished.");
  });

  it("says a done pane's reply from its transcript over anything read off its screen (THI-416)", () => {
    // last_line is a screen scrape: the turn's `✻ … for 42s` line, a footer
    // statusline when the composer goes unrecognised.
    const status = "📁 periscope 🌿 main ✎6  |  🧠███░░░░░░░ 39% 📨 2 📤 403 | session: 387.5k in / 403 out 💰 $121.06 🤖 opus";
    const body = (over) => payloadFor({ kind: "done", w: w(over) }).body;
    expect(body({ last_reply: "I updated FDY-8473. It now covers the call.", recap: "An older recap.", last_line: status }))
      .toBe("I updated FDY-8473.");
    expect(body({ recap: "Opened the PR. Tests pass.", last_line: status })).toBe("Opened the PR.");
    expect(body({ last_line: "Merged it. Prod restarted." })).toBe("Merged it.");
    expect(body({ last_reply: "", recap: null, last_line: "" })).toBe("Finished.");
  });

  it("lists the failing checks first and links the PR", () => {
    const p = payloadFor({
      kind: "ci_failed",
      w: w({ pr: 7, repo_slug: "o/r", ci_checks: [{ name: "test", state: "failure" }, { name: "lint", state: "success" }] }),
    });
    expect(p).toMatchObject({
      title: "CI failed on #7", subtitle: "periscope · dual-moas", body: "✗ test · ✓ lint",
      data: { open: "url", url: "https://github.com/o/r/pull/7" },
    });
    expect(payloadFor({ kind: "ci_failed", w: w({ pr: 7 }) }).body).toBe("Checks are failing.");
  });

  it("links the PR through util's prUrl, null without a repo_slug", () => {
    expect(payloadFor({ kind: "ci_failed", w: w({ pr: 7 }) }).data.url).toBeNull();
    expect(payloadFor({ kind: "ci_failed", w: w({ pr: 7, repo_slug: "o/r" }) }).data.url).toBe("https://github.com/o/r/pull/7");
  });

  it("shows where a merged PR went", () => {
    const p = payloadFor({ kind: "pr_merged", w: w({ pr: 7, repo_slug: "o/r", branch: "feat", pr_base: "main" }) });
    expect(p).toMatchObject({ title: "PR #7 merged", subtitle: "periscope · feat → main" });
  });

  it("names the account and its working sessions only when there are several accounts", () => {
    const ev = { kind: "usage", account: "b", meter: "week_all", m: { percent: 81.4, projected_percent: 112 }, mood: "grumpy" };
    const windows = [w({ state: "working", account: "b" }), w({ state: "working", account: "default" }), w({ state: "idle", account: "b" })];
    const two = [{ id: "default", label: "A" }, { id: "b", label: "B" }];
    expect(payloadFor(ev, { accounts: two, windows })).toMatchObject({
      title: "Account B is running hot", subtitle: "week usage 81% · →112% at reset", body: "1 session working on B.",
      target: "", data: { open: "usage" },
    });
    expect(payloadFor(ev, { accounts: [{ id: "b", label: "B" }], windows })).toMatchObject({
      title: "Usage is running hot", body: "1 session working.",
    });
    expect(payloadFor({ ...ev, m: { percent: 99, hot: true } }, { accounts: two }).subtitle).toBe("week usage 99%");
  });

  it("clips a long account label in the usage title", () => {
    const ev = { kind: "usage", account: "b", meter: "week_all", m: { percent: 10 } };
    const accounts = [{ id: "b", label: "A Really Long Account Label" }, { id: "default", label: "A" }];
    const p = payloadFor(ev, { accounts, windows: [] });
    expect(p.title).toBe("Account A Really Long A… is running hot");
  });

  it("renders 0% when the meter's percent is undefined", () => {
    const ev = { kind: "usage", account: "default", meter: "week_all", m: {} };
    expect(payloadFor(ev, { accounts: [{ id: "default", label: "A" }] }).subtitle).toBe("week usage 0%");
  });

  it("says 0 sessions working when a multi-account setup has no working sessions", () => {
    const ev = { kind: "usage", account: "b", meter: "week_all", m: { percent: 50 } };
    const two = [{ id: "default", label: "A" }, { id: "b", label: "B" }];
    expect(payloadFor(ev, { accounts: two, windows: [] }).body).toBe("0 sessions working on B.");
  });

  it("falls back to the raw account id when it's missing from ctx.accounts", () => {
    const ev = { kind: "usage", account: "ghost", meter: "week_all", m: { percent: 50 } };
    const two = [{ id: "default", label: "A" }, { id: "b", label: "B" }];
    expect(payloadFor(ev, { accounts: two, windows: [] }).title).toBe("Account ghost is running hot");
  });

  it("gives a done banner done_seen regardless of branch — Review diff waits on THI-433", () => {
    expect(payloadFor({ kind: "done", w: w() })).toMatchObject({ category: "done_seen", data: { pid: "p1", session: "periscope" } });
    expect(payloadFor({ kind: "done", w: w({ branch: "" }) }).category).toBe("done_seen");
  });

  it("gives each other banner the category whose buttons all apply", () => {
    expect(payloadFor({ kind: "pr_merged", w: w({ pr: 7, repo_slug: "o/r", cwd: "/wt/a" }) })).toMatchObject({
      category: "pr_merged", data: { cwd: "/wt/a" },
    });
    expect(payloadFor({ kind: "pr_merged", w: w({ pr: 7, repo_slug: "o/r" }) }).category).toBe("pr_merged_pr");
    expect(payloadFor({ kind: "needs_input", w: w() }).category).toBe("needs_dialog");
  });

  it("offers no PR buttons on pr_merged without a URL", () => {
    expect(payloadFor({ kind: "pr_merged", w: w({ pr: 7, cwd: "/wt/a" }) }).category).toBe("pr_merged_cleanup");
    expect(payloadFor({ kind: "pr_merged", w: w({ pr: 7 }) }).category).toBeUndefined();
  });

  it("asks to allow the request the server named, and only then", () => {
    const perm = { request_id: "a1b2c3d4e5f60718", tool: "Bash", summary: "touch probe.txt" };
    const p = payloadFor({ kind: "needs_input", w: w({ waiting_for: "permission prompt", pending_permission: perm }) });
    expect(p).toMatchObject({
      title: "dual-moas needs you", subtitle: "periscope · main", body: "Allow Bash: touch probe.txt?",
      category: "needs_permission", data: { open: "pane", pid: "p1", request_id: "a1b2c3d4e5f60718" },
    });
    expect(payloadFor({ kind: "needs_input", w: w({ pending_permission: { ...perm, summary: "" } }) }).body).toBe("Allow Bash?");
    // Not named (two requests, or an older pane without the hook): Open only, 2a's words.
    const unnamed = payloadFor({ kind: "needs_input", w: w({ waiting_for: "permission prompt", pending_permission: null }) });
    expect(unnamed).toMatchObject({ body: "Waiting for your approval.", category: "needs_dialog", data: { pid: "p1" } });
    expect(unnamed.data.request_id).toBeUndefined();
    expect(payloadFor({ kind: "needs_input", w: w({ waiting_for: "approve AskUserQuestion" }) }).category).toBe("needs_dialog");
    // An unstamped pane ("" pid) can't be named even with a request_id on hand.
    const noPid = payloadFor({ kind: "needs_input", w: w({ pid: "", waiting_for: "permission prompt", pending_permission: perm }) });
    expect(noPid).toMatchObject({ category: "needs_dialog", body: "Waiting for your approval." });
    expect(noPid.data.request_id).toBeUndefined();
  });

  it("names an MCP tool as name (server), and falls back without one", () => {
    const mcp = payloadFor({
      kind: "needs_input",
      w: w({ pending_permission: { request_id: "r", tool: "mcp__linear-personal__save_issue", summary: "x" } }),
    });
    expect(mcp.body).toBe("Allow save_issue (linear-personal): x?");
    expect(payloadFor({ kind: "needs_input", w: w({ pending_permission: { request_id: "r", tool: "", summary: "" } }) }).body).toBe(
      "Allow this?",
    );
  });

  it("offers Reply on a question only while its pane is a live CLAUDE pane", () => {
    const alert = { target: "gone:2", name: "old", session: "s", message: "Which database?" };
    expect(payloadFor({ kind: "question", w: w(), alert }).category).toBe("question");
    expect(payloadFor({ kind: "question", w: null, alert }).category).toBeUndefined();
    // Not an agent (e.g. the worktree's shell window) and not stamped: no Reply.
    expect(payloadFor({ kind: "question", w: w({ agent: null }), alert }).category).toBeUndefined();
    expect(payloadFor({ kind: "question", w: w({ pid: "" }), alert }).category).toBeUndefined();
    // Codex: the server's /api/pane/reply refuses it (dialog_open can't see
    // a Codex approval prompt), so no Reply even though the pane is live.
    expect(payloadFor({ kind: "question", w: w({ agent: "codex" }), alert }).category).toBeUndefined();
  });

  it("offers Ask agent to fix with a live agent pane, Open PR with a URL", () => {
    const checks = [{ name: "test", state: "failure" }, { name: "lint", state: "success" }, { name: "e2e", state: "failure" }];
    expect(payloadFor({ kind: "ci_failed", w: w({ pr: 7, repo_slug: "o/r", ci_checks: checks }) })).toMatchObject({
      category: "ci_failed", data: { pid: "p1", pr: 7, checks: ["test", "e2e"] },
    });
    expect(payloadFor({ kind: "ci_failed", w: w({ pr: 7 }) })).toMatchObject({ category: "ci_failed_fix", data: { checks: [] } });
    expect(payloadFor({ kind: "ci_failed", w: w({ pr: 7, repo_slug: "o/r", pid: "" }) }).category).toBe("ci_failed_pr");
    expect(payloadFor({ kind: "ci_failed", w: w({ pr: 7, pid: "" }) }).category).toBeUndefined();
  });

  it("finds a CLAUDE pane on the same PR when the notified window is a shell", () => {
    // window_view merges **pr into every window on the PR, so byPr can hand
    // copy.js the worktree's zsh window; Ask agent to fix must still land on
    // the agent's pane, found by prKey among the live windows.
    const shell = w({ pr: 7, repo_slug: "o/r", agent: null });
    const agentPane = w({ pid: "p2", agent: "claude", pr: 7, repo_slug: "o/r" });
    expect(payloadFor({ kind: "ci_failed", w: shell }, { windows: [agentPane] })).toMatchObject({
      category: "ci_failed", data: { pid: "p2" },
    });
    // No agent pane anywhere: Open PR only when there's a URL, else nothing —
    // but a plain click still finds the notified (shell) pane by pid.
    expect(payloadFor({ kind: "ci_failed", w: shell }, { windows: [] }).category).toBe("ci_failed_pr");
    const noSibling = payloadFor({ kind: "ci_failed", w: w({ pr: 7, agent: null }) }, { windows: [] });
    expect(noSibling.category).toBeUndefined();
    expect(noSibling.data).toMatchObject({ pid: "p1" });
  });

  it("skips a Codex sibling on the same PR: the server can't see its approval prompt", () => {
    const shell = w({ pr: 7, repo_slug: "o/r", agent: null });
    const codexPane = w({ pid: "p2", agent: "codex", pr: 7, repo_slug: "o/r" });
    const p = payloadFor({ kind: "ci_failed", w: shell }, { windows: [codexPane] });
    // Falls back to Open PR, not Ask agent to fix, and the notified shell's
    // own pid — never the Codex pane's.
    expect(p.category).toBe("ci_failed_pr");
    expect(p.data).toMatchObject({ pid: "p1" });
    // A Codex pane is itself never a fixer, even for its own PR.
    expect(payloadFor({ kind: "ci_failed", w: w({ pr: 7, repo_slug: "o/r", agent: "codex" }) }, { windows: [] }).category).toBe(
      "ci_failed_pr",
    );
  });

  it("offers Spawn on the account a pane on A would move to, and only then", () => {
    const ev = { kind: "usage", account: "default", meter: "week_all", m: { percent: 90 } };
    const accounts = [{ id: "default", label: "A" }, { id: "b", label: "B" }];
    expect(payloadFor(ev, { accounts, moveTargets: { default: "b" } })).toMatchObject({
      category: "usage_to_b", data: { open: "usage", spawn_to: "b" },
    });
    expect(payloadFor(ev, { accounts, moveTargets: {} })).toMatchObject({ category: "usage", data: { open: "usage" } });
    expect(payloadFor(ev, { accounts }).data.spawn_to).toBeUndefined();
    expect(payloadFor(ev, { accounts, moveTargets: { default: "default" } }).category).toBe("usage");
  });

  it("only names categories that actions.js registers, or none", () => {
    const accounts = [{ id: "default", label: "A" }, { id: "b", label: "B" }];
    const moveTargets = { default: "b", b: "default" };
    const ctx = { accounts, windows: [], moveTargets };
    const ids = new Set(categories({ accounts, moveTargets }).map((c) => c.id));
    const alert = { name: "old", session: "s", message: "m" };
    const evs = [
      { kind: "done", w: w() }, { kind: "done", w: w({ branch: "" }) },
      { kind: "ci_failed", w: w({ pr: 7 }) }, { kind: "ci_failed", w: w({ pr: 7, repo_slug: "o/r" }) },
      { kind: "pr_merged", w: w({ pr: 7, cwd: "/wt" }) }, { kind: "pr_merged", w: w({ pr: 7, repo_slug: "o/r", cwd: "/wt" }) },
      { kind: "pr_merged", w: w({ pr: 7 }) }, { kind: "pr_merged", w: w({ pr: 7, repo_slug: "o/r" }) },
      { kind: "usage", account: "default", meter: "week_all", m: { percent: 90 } },
      { kind: "usage", account: "c", meter: "week_all", m: { percent: 90 } },
      { kind: "needs_input", w: w() },
      { kind: "needs_input", w: w({ pending_permission: { request_id: "r", tool: "Bash", summary: "ls" } }) },
      { kind: "question", w: w(), alert }, { kind: "question", w: null, alert },
      { kind: "ci_failed", w: w({ pr: 7, pid: "" }) }, { kind: "ci_failed", w: w({ pr: 7, repo_slug: "o/r", pid: "" }) },
    ];
    for (const ev of evs) {
      const p = payloadFor(ev, ctx);
      expect(p.category === undefined || ids.has(p.category)).toBe(true);
    }
    // A burst summary carries no category either.
    const summary = summaryPayload([{ kind: "needs_input", w: w() }]);
    expect(summary.category === undefined || ids.has(summary.category)).toBe(true);
  });
});

describe("helpers", () => {
  it("firstSentence takes one sentence and clips it", () => {
    expect(firstSentence("Done. Next I'll refactor.")).toBe("Done.");
    expect(firstSentence("no full stop here")).toBe("no full stop here");
    expect(firstSentence("x".repeat(300)).length).toBe(140);
    expect(firstSentence(null)).toBe("");
  });

  it("firstSentence doesn't cut at an abbreviation's period", () => {
    expect(firstSentence("Fixed the bug in e.g. the parser module. Tests now pass.")).toBe(
      "Fixed the bug in e.g. the parser module.",
    );
  });

  it("firstSentence also cuts at a sentence-ending !", () => {
    expect(firstSentence("Shipped it! Tests pass.")).toBe("Shipped it!");
  });

  it("waitKind sorts the raw waiting_for values", () => {
    expect(waitKind("permission prompt")).toBe("permission");
    expect(waitKind("Approve AskUserQuestion")).toBe("question");
    expect(waitKind("dialog open")).toBe("dialog");
    expect(waitKind(null)).toBeNull();
  });
});
