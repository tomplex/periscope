// What each notification says (notify/detect.js finds them, notify/feed.js
// sends them): the pure part. macOS truncates titles around 40 characters, so
// pane names are clipped before they push the verb off the end.

import { meterLabel } from "../chrome/usageRules.js";
import { paneLabel, plural, prUrl, waitKind } from "../util.js";
import { prKey } from "./detect.js";

// Cut by code point, not UTF-16 code unit — a pane name ending in an emoji
// or other surrogate-pair character would otherwise leave a lone surrogate
// (an unpaired high surrogate renders as a replacement glyph in the banner).
const clip = (s, n) => {
  const t = String(s || "").trim();
  const chars = Array.from(t);
  return chars.length > n ? `${chars.slice(0, n - 1).join("")}…` : t;
};
const pane = (w) => clip(paneLabel(w), 26);
const where = (w) => [w?.session, w?.branch].filter(Boolean).join(" · ");

// Reply / Ask agent to fix type into the pane: it must be a live CLAUDE pane.
// byPr can hand us the worktree's shell window, which shows the same PR. Not
// Codex: the server's /api/pane/reply refuses it too, since dialog_open can't
// see a Codex approval prompt and text there could approve something.
const agent = (x) => !!(x?.pid && x.agent === "claude");

// An MCP tool's wire name (mcp__server__tool) reads as `tool (server)`,
// display-only — the server's screen check still compares the raw stored name.
const toolLabel = (t) => {
  const m = /^mcp__(.+?)__(.+)$/.exec(t || "");
  return m ? `${m[2]} (${m[1]})` : t || "this";
};

// The first sentence of a pane's last words, for a done banner. A sentence
// ends at `.`/`!`/`?` only when followed by whitespace-then-capital/digit (or
// the end of the text) — plain "e.g."/"i.e." no longer read as sentence
// ends. "Mr. Smith approved." still cuts at "Mr." since "Smith" is
// capitalized; there's no cheap way to special-case every title abbreviation.
export function firstSentence(text) {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  const m = t.match(/^.*?[.!?](?=\s[A-Z0-9]|$)/);
  return clip(m ? m[0] : t, 140);
}

const NEEDS_BODY = {
  permission: "Waiting for your approval.",
  question: "Waiting for your answer.",
  dialog: "Waiting for your input.",
};
const CHECK_MARK = { failure: "✗", pending: "⟳", success: "✓" };

// A usage banner's category when it can offer Spawn on {B}: one per B,
// because a category's buttons, labels included, are fixed (notify/actions.js).
export const usageCategory = (to) => `usage_to_${to}`;

// Each event's banner: { kind, title, subtitle, body, target, thread, category, data }.
// `target` is the pane, so an older shell's plain click can still reveal it;
// `data` says what a click opens. ctx: { accounts, windows, moveTargets }.
// `category` picks the banner's buttons (notify/actions.js); a kind without
// one shows none.
export function payloadFor(ev, ctx = {}) {
  const w = ev.w;
  const base = { kind: ev.kind, thread: ev.kind, target: w?.target || "", data: {} };
  switch (ev.kind) {
    case "needs_input": {
      // Allow / Deny only when the server could name the request the dialog
      // asks (w.pending_permission, from the PermissionRequest hook); it
      // re-checks that on screen before a key goes in. Otherwise Open only.
      const perm = w.pending_permission;
      if (perm?.request_id && w.pid) {
        return {
          ...base, title: `${pane(w)} needs you`, subtitle: where(w),
          body: perm.summary ? `Allow ${toolLabel(perm.tool)}: ${perm.summary}?` : `Allow ${toolLabel(perm.tool)}?`,
          category: "needs_permission", data: { open: "pane", pid: w.pid, request_id: perm.request_id },
        };
      }
      return {
        ...base, title: `${pane(w)} needs you`, subtitle: where(w),
        body: NEEDS_BODY[waitKind(w.waiting_for)] || NEEDS_BODY.dialog,
        category: "needs_dialog", data: { open: "pane", pid: w.pid },
      };
    }
    case "question": {
      const a = ev.alert;
      return {
        ...base, title: `${clip(w ? paneLabel(w) : a.name || a.session || "A session", 26)} asks`,
        subtitle: w ? where(w) : a.session || "", body: clip(a.message, 180), target: a.target || base.target,
        // Reply types into an agent's live pane; one that has gone, or isn't
        // one (the worktree's shell), has nowhere to send it.
        ...(agent(w) ? { category: "question" } : {}),
        data: { open: "pane", pid: w?.pid ?? null },
      };
    }
    case "done":
      return {
        ...base, title: `${pane(w)} is done`,
        // The linked PR only: an auto-detected one is just the branch's, often
        // days old, and would ride every finished turn.
        subtitle: w.pr && w.pr_linked ? `${w.session} · PR #${w.pr}` : where(w),
        // The reply from the transcript first: recap and last_line are read off
        // the screen, where the footer's statusline passed for the reply (THI-416).
        body: firstSentence(w.last_reply || w.recap || w.last_line) || "Finished.",
        category: "done_seen", data: { open: "pane", pid: w.pid, session: w.session },
      };
    case "ci_failed": {
      const checks = (w.ci_checks || []).map((c) => `${CHECK_MARK[c.state] || "·"} ${c.name}`).join(" · ");
      const url = prUrl(w.repo_slug, w.pr);
      // Ask agent to fix types into an agent's pane: w itself, or — since
      // window_view merges **pr into every window on the PR, including the
      // worktree's shell — another live window on the same PR that is one.
      const fixer = agent(w) ? w : (ctx.windows || []).find((x) => agent(x) && prKey(x) === prKey(w));
      let category;
      if (fixer?.pid && url) category = "ci_failed";
      else if (fixer?.pid) category = "ci_failed_fix";
      else if (url) category = "ci_failed_pr";
      return {
        ...base, title: `CI failed on #${w.pr}`, subtitle: [w.session, paneLabel(w)].filter(Boolean).join(" · "),
        body: checks || "Checks are failing.",
        ...(category ? { category } : {}),
        // `checks`: the failing ones' names, for Ask agent to fix's message.
        // `pid`: the fixer's when Ask agent to fix is offered, else w's own —
        // a plain click / Open still finds the notified pane by pid even with
        // no agent around to fix it (target can name a neighbour by now).
        data: {
          open: "url", url, pid: fixer?.pid || w.pid, pr: w.pr,
          checks: (w.ci_checks || []).filter((c) => c.state === "failure").map((c) => c.name),
        },
      };
    }
    case "pr_merged": {
      const url = prUrl(w.repo_slug, w.pr);
      let category;
      if (w.cwd && url) category = "pr_merged";
      else if (w.cwd) category = "pr_merged_cleanup";
      else if (url) category = "pr_merged_pr";
      return {
        ...base, title: `PR #${w.pr} merged`,
        subtitle: w.pr_base ? `${w.session} · ${w.branch} → ${w.pr_base}` : where(w),
        body: "The worktree and its session can be cleaned up.",
        ...(category ? { category } : {}),
        data: { open: "url", url, pid: w.pid, cwd: w.cwd },
      };
    }
    case "usage": {
      const registry = ctx.accounts || [];
      const name = clip(registry.find((a) => a.id === ev.account)?.label || ev.account, 16);
      const multi = registry.length > 1;
      const proj = Math.max(...[ev.m.projected_percent, ev.m.projected_recent].filter((v) => v != null), -1);
      const working = (ctx.windows || []).filter((x) => x.state === "working" && x.account === ev.account).length;
      // Spawn on the account a pane on A would move to (launch_policy.choose_move,
      // published as move_targets): not launch_default, which can be A itself.
      const to = ctx.moveTargets?.[ev.account];
      const spawn = to && to !== ev.account;
      return {
        ...base, target: "",
        title: multi ? `Account ${name} is running hot` : "Usage is running hot",
        subtitle: `${meterLabel(ev.meter)} usage ${Math.round(ev.m.percent ?? 0)}%`
          + (proj >= 0 ? ` · →${Math.round(proj)}% at reset` : ""),
        body: multi ? `${plural(working, "session")} working on ${name}.` : `${plural(working, "session")} working.`,
        category: spawn ? usageCategory(to) : "usage",
        data: spawn ? { open: "usage", spawn_to: to } : { open: "usage" },
      };
    }
    default:
      return null;
  }
}
