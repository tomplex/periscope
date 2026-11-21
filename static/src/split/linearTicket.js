// The Linear ticket a pane shows: the one Claude linked (link_linear), else the
// one its PR title or branch names, derived server-side (THI-441). The tooltip
// says where a derived one came from, so it doesn't read as confirmed.
// static/util.js holds a copy for the /history page; the test runs both.
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
