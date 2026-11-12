// A banner's buttons: which ones each banner category carries (categories)
// and what pressing one does (runAction). notify/feed.js registers the set
// through tauri.js and routes presses here; notifications.rs maps the set
// onto macOS categories and reports a press on notification-action.
//
// A category is a fixed set of buttons, labels included, so copy.js gives
// each banner the variant whose buttons all apply: no Clean up without a
// worktree, no Open PR without a URL, no Allow / Deny without a request the
// server could name, and Spawn on {B} gets one category per B.
// Allow / Deny / Reply / Ask agent to fix make an agent act: they POST to
// routes/pane_actions.py, which re-checks the pane when pressed
// (docs/superpowers/specs/2026-10-26-native-notifications-2b-design.md).
import { accountLabel } from "../accounts.js";
import { pickSpawnAccount } from "../chrome/SpawnAccountPicker.jsx";
import { openUsageDetails } from "../chrome/UsagePill.jsx";
import { openCleanupModal } from "../overlays/CleanupModal.jsx";
import { showToast } from "../overlays/Toast.jsx";
import { markReadySeen, revealPane, selectPane } from "../split/alertFeed.js";
import { windows } from "../store.js";
import { notify, openExternal } from "../tauri.js";
import { postJson } from "../util.js";
import { usageCategory } from "./copy.js";

const act = (id, title, opts = {}) => Object.freeze({ id, title, ...opts });
// `foreground` brings Periscope forward: only for buttons that show
// something in it. Open PR's browser comes forward on its own.
const OPEN_PR = act("open_pr", "Open PR");
const MARK_SEEN = act("mark_seen", "Mark seen");
const CLEANUP = act("cleanup", "Clean up worktree", { foreground: true });
const DETAILS = act("details", "Details", { foreground: true });
const OPEN = act("open", "Open", { foreground: true });
// `auth`: Touch ID or the password first when the screen is locked — every
// button that lets an agent proceed or types into it; Deny only stops one.
const FIX = act("fix", "Ask agent to fix", { auth: true });

// The whole set: macOS replaces it on each registration. Move targets are
// deduped and sorted so the feed's change check doesn't resend an identical
// set because move_targets came back in another order.
//
// ONE button per category: macOS (Big Sur on) folds two or more actions into
// an "Options" menu, so the main action only shows directly when it's alone.
// The banner's click (openFromBanner) already opens the pane, the PR or the
// usage details, so Open / Open PR / Details buttons beside a main action were
// redundant anyway. Deny lost its button for the same reason: the click opens
// the pane, where the dialog's own No is one key away. runAction still handles
// deny / open, so a banner posted before this change still works.
export function categories({ accounts = [], moveTargets = {} } = {}) {
  const set = [
    // No "done" category: every done banner gets done_seen only.
    { id: "done_seen", actions: [MARK_SEEN] },
    { id: "ci_failed_pr", actions: [OPEN_PR] },
    { id: "pr_merged", actions: [CLEANUP] },
    { id: "pr_merged_cleanup", actions: [CLEANUP] },
    { id: "pr_merged_pr", actions: [OPEN_PR] },
    { id: "usage", actions: [DETAILS] },
    { id: "needs_permission", actions: [act("allow", "Allow", { auth: true })] },
    { id: "needs_dialog", actions: [OPEN] },
    // A category's buttons are static, so the placeholder can't name the pane.
    { id: "question", actions: [act("send", "Reply", { auth: true, reply: { button: "Send", placeholder: "Reply…" } })] },
    { id: "ci_failed", actions: [FIX] },
    { id: "ci_failed_fix", actions: [FIX] },
  ];
  for (const to of [...new Set(Object.values(moveTargets || {}))].sort()) {
    // accounts may come back null from a caller that hasn't loaded the registry yet.
    set.push({ id: usageCategory(to), actions: [act("spawn_on", `Spawn on ${accountLabel(to, accounts || [])}`)] });
  }
  return set;
}

export const OPENERS = Object.freeze({
  openExternal, openUsageDetails, pickSpawnAccount, openCleanupModal, markReadySeen, selectPane, revealPane,
  post: postJson, toast: showToast, notify,
});

// What a failed agent-acting press says in its toast.
const ACT_LABEL = { allow: "Allow", deny: "Deny", send: "Reply", fix: "Ask agent to fix" };

// What a failed agent-acting press titles its follow-up notice (a full
// sentence fragment, not ACT_LABEL's button name: "Couldn't ask the agent to
// fix" reads better than "Ask agent to fix failed").
const NOTICE_TITLE = {
  allow: "Couldn't allow", deny: "Couldn't deny", send: "Couldn't send your reply", fix: "Couldn't ask the agent to fix",
};

// allow/deny have nothing to answer without the dialog's own request_id
// (same reasoning as the missing-pid guard below): a stale banner's id would
// answer a dialog that moved on.
const NEEDS_REQUEST_ID = new Set(["allow", "deny"]);

// A route's canned 409 reason, used only when the server's own detail is
// just postJson's generic HTTP fallback (i.e. it said nothing route-specific).
const ROUTE_409 = {
  "/api/pane/permission": "The dialog changed — open the pane to answer it.",
  "/api/pane/reply": "The pane has a dialog open, or isn't running an agent — open it to see why.",
};

// The follow-up notice's body, by status: 404/0 get a plain human reason; a
// 409 gets the route's canned one unless the server already said something
// more specific; anything else is the server's own detail.
function noticeBody(path, r) {
  if (r?.status === 404) return "The pane is gone.";
  if (r?.status === 0) return "Periscope isn't answering.";
  if (r?.status === 409) {
    return typeof r.detail === "string" && r.detail !== `HTTP ${r.status}` ? r.detail : ROUTE_409[path];
  }
  return r?.detail || "No answer";
}

// A check name is written by whatever has checks write access, and an
// ESC[201~ in it would end the reply's bracketed paste early, the rest typed
// as keys. The same set routes/pane_actions.py strips (all C0 and C1 controls
// but \t and \n), so the message built here is the one typed.
const isControl = (c) => {
  const n = c.codePointAt(0);
  return n !== 9 && n !== 10 && (n < 32 || (n >= 127 && n <= 159));
};
const withoutControls = (s) => [...String(s)].filter((c) => !isControl(c)).join("").trim();

// The message Ask agent to fix types into the pane: the PR and the names of
// its failing checks (copy.js puts them in data.checks).
export function fixMessage(pr, checks) {
  const names = (checks || []).map(withoutControls).filter(Boolean);
  const what = names.length ? `${names.join(" · ")}.` : "Checks are failing.";
  return `CI failed on #${pr}: ${what} Please investigate and fix.`;
}

// A press that makes the agent act. The server re-checks the pane and
// answers 409 when the banner no longer matches it (another request, the
// dialog gone, a dialog just opened): that only needs the pane shown as it is
// now. Any other failure shows it too, with the error. Allow / Deny / Send /
// Fix are background presses — the app isn't brought forward for them — so
// show() and the toast land in a hidden window where nobody sees them; a
// plain follow-up notice (kind "notice", no category, data.open: "pane" —
// openFromBanner already opens that by pid, `target` as its fallback, same as
// every copy.js banner) is what actually reaches the user, on every failure,
// posted before the toast since the toast is the one that's invisible here.
// Success needs nothing: the pane's next state shows it working. Never rejects.
async function agentAct({ target, show }, action, path, body, o) {
  // Guarded by `sent`: show()/toast() can themselves throw (an opener with a
  // bug), which falls into the catch below — that must not re-post a second
  // notice with an unrelated internal-error message in place of the real,
  // already-computed route reason. First reason wins.
  let sent = false;
  const notice = (reason) => {
    if (sent) return;
    sent = true;
    // Deferred a tick so a throwing/rejecting o.notify (a dead IPC call, say)
    // can't take the rest of this function down with it.
    Promise.resolve()
      .then(() => o.notify({
        kind: "notice", thread: "notice", title: NOTICE_TITLE[action], body: reason, target,
        data: { open: "pane", pid: body.pid }, quiet_when_focused: true,
      }))
      .catch((e) => console.error("[notify] follow-up failed", e));
  };
  try {
    // A banner with nothing to answer (its pane is gone, or — allow/deny
    // only — its dialog's own request_id is missing) still tells the user,
    // rather than failing silently in a window nobody's looking at.
    if (!body.pid || (NEEDS_REQUEST_ID.has(action) && !body.request_id)) {
      notice("Open the pane to answer it.");
      show();
      return;
    }
    const r = await o.post(path, body);
    if (r?.ok) return;
    // Computed before show(): if show() throws, the catch below must still
    // report this reason, not its own exception.
    const reason = noticeBody(path, r);
    notice(reason);
    show();
    if (r?.status !== 409) o.toast(`${ACT_LABEL[action]} failed: ${r?.detail || "no answer"}`, "bad", 6000);
  } catch (e) {
    console.error("[notify] action failed", action, e);
    notice(e?.message || "Periscope isn't answering.");
    try {
      show();
    } catch (e2) {
      console.error("[notify] reveal failed", e2);
    }
  }
}

// A pressed button, as the shell reports it. The pane is found by pid first:
// tmux renumbers windows, so target can name a neighbour by now — `show`
// selects the live pane found by pid, falling back to revealPane(target)
// (the banner's own, possibly-stale, pane) only once that pane is gone too.
// Never throws: a failed press still shows you something. The agent-acting
// presses return agentAct's promise, which never rejects either.
export function runAction({ target, action, text, data } = {}, o = OPENERS) {
  // A default covers only a missing data; the shell's Info defaults it to null.
  data ||= {};
  let w;
  const show = () => (w ? o.selectPane(w) : target && o.revealPane(target));
  try {
    // "" is an unstamped pane's pid — must not match every unstamped window.
    w = data.pid ? (windows.peek() || []).find((x) => String(x.pid) === String(data.pid)) : null;
    switch (action) {
      case "open_pr":
        if (data.url) o.openExternal(data.url);
        else show();
        return;
      case "details":
        o.openUsageDetails();
        return;
      case "spawn_on":
        // pickSpawnAccount is async but can't reject today (apiCall swallows
        // its errors), so there's nothing here for the catch below to see.
        if (data.spawn_to) o.pickSpawnAccount(data.spawn_to);
        return;
      case "mark_seen":
        // A gone pane has no done card left to mark seen.
        if (w) o.markReadySeen(w);
        return;
      case "cleanup": {
        // The banner names the worktree whose PR merged; the live pane may
        // since have moved to another one.
        const cwd = data.cwd || w?.cwd;
        if (cwd) o.openCleanupModal({ paths: [cwd] });
        else show();
        return;
      }
      case "allow":
      case "deny":
        return agentAct({ target, show }, action, "/api/pane/permission", { pid: data.pid, request_id: data.request_id, choice: action }, o);
      case "send": {
        // An empty reply says nothing; there's nothing to send.
        const reply = String(text || "").trim();
        if (!reply) return;
        return agentAct({ target, show }, action, "/api/pane/reply", { pid: data.pid, text: reply }, o);
      }
      case "fix":
        return agentAct({ target, show }, action, "/api/pane/reply", { pid: data.pid, text: fixMessage(data.pr, data.checks) }, o);
      case "open":
        show();
        return;
      default:
        console.warn("[notify] unknown action", action);
    }
  } catch (e) {
    console.error("[notify] action failed", action, e);
    try {
      show();
    } catch (e2) {
      console.error("[notify] reveal failed", e2);
    }
  }
}
