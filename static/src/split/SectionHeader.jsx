// Shared section-header primitive for the left rail: a collapse chevron, an
// uppercase label, and an optional right-aligned count badge. Presentational
// only — collapse state and count are owned by the caller (prefs / computed),
// never here. Instanced for PINNED / TRACKS / ACTIVITY.
export function SectionHeader({ icon, label, count, collapsed, onToggle }) {
  return (
    <div class="rail-row section-header" onClick={onToggle}>
      <span class="rail-chev">{collapsed ? "▸" : "▾"}</span>
      {icon ? <span class="section-header-icon">{icon}</span> : null}
      <span class="section-header-label">{label}</span>
      {count != null && count > 0
        ? <span class="section-header-count">{count}</span>
        : null}
    </div>
  );
}
