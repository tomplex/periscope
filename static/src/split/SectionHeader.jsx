// Shared section-header primitive for the left rail: a collapse chevron, an
// uppercase label, and an optional right-aligned count badge. Presentational
// only — collapse state and count are owned by the caller (prefs / computed),
// never here. Instanced 4× in Phase 2 (NEEDS YOU / PINNED / PROJECTS / ACTIVITY).
export function SectionHeader({ icon, label, count, collapsed, onToggle, tone }) {
  const toneCls =
    tone === "alert" ? " section-header-alert"
    : tone === "ready" ? " section-header-ready"
    : tone === "working" ? " section-header-working"
    : "";
  return (
    <div
      class={`rail-row section-header${toneCls}`}
      onClick={onToggle}
    >
      <span class="rail-chev">{collapsed ? "▸" : "▾"}</span>
      {icon ? <span class="section-header-icon">{icon}</span> : null}
      <span class="section-header-label">{label}</span>
      {count != null && count > 0
        ? <span class="section-header-count">{count}</span>
        : null}
    </div>
  );
}
