// The single source of the card/row filter predicate. Ported from
// grid.js:passesFilter; the app.js:233-243 inline duplicate (the bulk-send
// path) is reconciled to import from here — there is no second copy.
//
// Unlike the vanilla version it takes the active filter as an argument
// instead of reading state.currentFilter, so callers can pass the
// `currentFilter` signal's value. Branches are identical: all, needs-input,
// working, done, idle, agents, shell, ci-bad.
export function passesFilter(w, filter) {
  if (filter === "all") return true;
  if (filter === "needs-input") return w.state === "needs-input";
  if (filter === "working") return w.state === "working";
  if (filter === "done") return w.state === "done";
  if (filter === "idle") return w.state === "idle";
  if (filter === "agents") return !!w.agent;
  if (filter === "shell") return w.state === "shell";
  if (filter === "ci-bad") return w.ci === "✗";
  return true;
}
