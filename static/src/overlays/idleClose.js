// The settings row's second line, from the rows of GET /api/idle-close/preview.
export function idleCloseLine(rows) {
  const names = rows.filter((r) => r.verdict === "closes").map((r) => r.name);
  if (!names.length) return "No pane closes at 04:00 tonight";
  return `${names.length} ${names.length === 1 ? "pane closes" : "panes close"} at 04:00 tonight: ${names.join(", ")}`;
}
