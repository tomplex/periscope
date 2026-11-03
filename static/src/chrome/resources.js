// Formatting and ordering behind <ResourcePill> and the detail header.
// /api/state's `resources` groups and each row's `res` are
// { mem_mb, cpu_pct }: memory footprint, and the share of the whole
// machine's CPU (100 = every core busy) — null until the server has two
// samples to diff.

export function fmtMem(mb) {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)}G` : `${mb}M`;
}

export function fmtCpu(pct) {
  if (pct == null) return "–";
  // A pane is often a fraction of a percent of the machine; whole numbers
  // would round it to 0.
  return pct < 10 ? `${pct.toFixed(1)}%` : `${Math.round(pct)}%`;
}

// The pill's figure: what periscope and its panes hold. Elsewhere is the rest
// of the machine, shown in the popover, not owned here.
export function ownedMb(r) {
  return r.server.mem_mb + (r.webview?.mem_mb || 0) + r.panes.mem_mb;
}

// Popover pane lines, heaviest first. A row has no `res` when it has no
// pane_pid, or when the tick's process snapshot failed — the backend
// recomputes when the pane set changes.
export function paneLines(windows) {
  return windows
    .filter((w) => w.res)
    .map((w) => ({ w, ...w.res }))
    .sort((a, b) => b.mem_mb - a.mem_mb);
}

export function machineLine(m) {
  return `${fmtMem(m.ram_mb)} RAM · swap ${fmtMem(m.swap_used_mb)}/${fmtMem(m.swap_total_mb)} · ${m.mem_free_pct}% free · load ${m.load1}`;
}

// The popover's poll-cost block: what one dashboard poll costs (/api/state
// `build_stats`, forks.py). gap_s is the server's interval between pushes,
// null until it has pushed twice. forks_per_min/top_forks count every fork
// server-wide over the last minute, not just this poll's — hence "server".
export function buildStatsLine(r) {
  const every = r.gap_s == null ? "" : ` · every ${r.gap_s.toFixed(1)} s`;
  return `poll ${r.build_s.toFixed(1)} s${every} · server ${r.forks_per_min} forks/min`;
}

// The slowest poll in the last minute, as its own row; absent when unknown.
export function slowestLine(r) {
  return r.build_max_s == null ? null : `slowest ${r.build_max_s.toFixed(1)} s`;
}

// The commands that forked most server-wide in the last minute, as their own
// row; absent when there were none.
export function topForksLine(r) {
  if (!r.top_forks?.length) return null;
  return r.top_forks.map(([cmd, n]) => `${cmd} ${n}`).join(" · ");
}
