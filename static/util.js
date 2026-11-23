// Pure helpers for the vanilla /history page. The Preact dashboard has its
// own copies under src/ — this module must stay self-contained (no sibling
// imports): the Preact cutover deleted vanilla toast.js while this file still
// imported it, which killed the whole /history module graph and blanked the page.

export function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function relTime(epochSec) {
  if (!epochSec) return "";
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - epochSec);
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

// A copy of src/split/linearTicket.js. The Linear ticket a row shows: the one
// Claude linked (link_linear), else the one its PR title or branch names. The
// tip says where a derived one came from, so it doesn't read as confirmed.
const SOURCES = { pr_title: "from the PR title", branch: "from the branch name" };

export function linearTicket(w) {
  if (w?.linked_linear) {
    const id = w.linked_linear;
    const title = w.linked_linear_title ? `: ${w.linked_linear_title}` : "";
    const status = w.linked_linear_status ? ` [${w.linked_linear_status}]` : "";
    return { id, href: `https://linear.app/issue/${id}`, tip: `Linear ${id}${title}${status}`, note: null, derived: false };
  }
  if (w?.linear_ticket) {
    const id = w.linear_ticket;
    const note = SOURCES[w.linear_ticket_source] || "derived";
    return { id, href: `https://linear.app/issue/${id}`, tip: `Linear ${id} (${note})`, note, derived: true };
  }
  return null;
}
