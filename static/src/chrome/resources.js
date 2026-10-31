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

// Popover pane lines, heaviest first. A row has no `res` when the server's
// snapshot failed that tick.
export function paneLines(windows) {
  return windows
    .filter((w) => w.res)
    .map((w) => ({ w, ...w.res }))
    .sort((a, b) => b.mem_mb - a.mem_mb);
}

export function machineLine(m) {
  return `${fmtMem(m.ram_mb)} RAM · swap ${fmtMem(m.swap_used_mb)}/${fmtMem(m.swap_total_mb)} · ${m.mem_free_pct}% free · load ${m.load1}`;
}
