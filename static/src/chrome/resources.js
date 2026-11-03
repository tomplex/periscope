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

// The popover's last line: what one dashboard refresh costs (/api/state
// `refresh`, forks.py). gap_s is the server's interval between pushes, null
// until it has pushed twice.
export function refreshLine(r) {
  const every = r.gap_s == null ? "" : ` · every ${r.gap_s.toFixed(1)} s`;
  return `refresh ${r.build_s.toFixed(1)} s${every} · ${r.forks_per_min} forks/min`;
}

// Its tooltip: the slowest refresh in the last minute, then the commands that
// forked most.
export function refreshTip(r) {
  return [
    `slowest ${r.build_max_s.toFixed(1)} s in the last minute`,
    ...r.top_forks.map(([cmd, n]) => `${cmd} ×${n}`),
  ].join("\n");
}
