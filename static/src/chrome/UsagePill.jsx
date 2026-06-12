// Plan-usage pill in the dashboard header. Prefers the plan percentages
// from Anthropic's OAuth usage endpoint (server-fetched, authoritative);
// falls back to the JSONL-derived 5h estimate before the first fetch
// completes. CSS classes (.usage, .usage-fallback, .usage-item,
// .usage-item-label, .usage-item-bar, .usage-item-fill + ok/warn/danger
// tone) are shared with the original usage-pill styling.
//
// Reads the `usage` signal, which the poll loop writes as
// { plan: data.usage_plan, fallback: data.usage }.
import { usage } from "../store.js";
import { relTime } from "../util.js";

// Stale once the fetch is two refresh intervals old (server refreshes every
// 5 min on success) — beyond that the server is failing to fetch, not just
// between refreshes, and the percentages describe the past.
const STALE_AFTER_S = 600;

function fmtTokens(n) {
  if (!n) return "0";
  if (n < 1000) return `${n}`;
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  return `${(n / 1_000_000_000).toFixed(2)}B`;
}

function fmtResetCountdown(epochSec) {
  if (!epochSec) return "5h window open";
  const diff = epochSec - Math.floor(Date.now() / 1000);
  if (diff <= 0) return "resets now";
  if (diff < 60) return `resets in ${diff}s`;
  if (diff < 3600) return `resets in ${Math.floor(diff / 60)}m`;
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  if (h < 48) return `resets in ${h}h ${m}m`;
  return `resets in ${Math.floor(h / 24)}d ${h % 24}h`;
}

// "6:39 PM", weekday-prefixed when not today.
function fmtClock(epochSec) {
  const d = new Date(epochSec * 1000);
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? time : `${d.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

// "resets in 2h 15m (6:39 PM)" — countdown plus the wall-clock reset time.
function fmtReset(epochSec) {
  if (!epochSec) return "";
  return `${fmtResetCountdown(epochSec)} (${fmtClock(epochSec)})`;
}

// Pace lines for a meter's tooltip — the "on track to blow the limit"
// heuristics computed server-side (usage.py attach_projections).
function paceLines(m) {
  const lines = [];
  if (m.projected_percent != null)
    lines.push(`window average → ${m.projected_percent}% at reset`);
  if (m.projected_recent != null)
    lines.push(`current burn → ${m.projected_recent}% at reset`);
  if (m.limit_at) {
    const diff = m.limit_at - Math.floor(Date.now() / 1000);
    const dur = diff < 3600
      ? `${Math.max(1, Math.floor(diff / 60))}m`
      : `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
    lines.push(`at current burn, limit in ~${dur} (${fmtClock(m.limit_at)})`);
  }
  if (m.hot) lines.push("🔥 burning ≥2× the even-burn pace for this window");
  return lines;
}

// "→102%" when the two heuristics agree (within 10 points), "→83–160%" when
// bursty usage makes them disagree. Red = on track to blow by either one.
function projText(m) {
  const cands = [m.projected_percent, m.projected_recent].filter((v) => v != null);
  if (!cands.length) return null;
  const lo = Math.min(...cands);
  const hi = Math.max(...cands);
  if (hi <= m.percent) return null; // adds nothing over the current bar
  return { text: hi - lo > 10 ? `→${lo}–${hi}%` : `→${hi}%`, blow: hi >= 100 };
}

function MeterBar({ label, m, resets, pace }) {
  const pct = m.percent;
  const tone = pct >= 90 ? "danger" : pct >= 70 ? "warn" : "ok";
  const title = [`${label} — ${pct}% used. ${resets || ""}`, ...pace].join("\n");
  const proj = projText(m);
  return (
    <div class="usage-item" title={title}>
      <span class="usage-item-label">{label}</span>
      <span class="usage-item-bar">
        <span class={`usage-item-fill ${tone}`} style={`width:${pct}%`}></span>
      </span>
      <b>{pct}%</b>
      {proj && (
        <span class={`usage-item-proj${proj.blow ? " blow" : ""}`}>{proj.text}</span>
      )}
      {m.hot && <span class="usage-item-hot">🔥</span>}
    </div>
  );
}

export function UsagePill() {
  const u = usage.value || {};
  const plan = u.plan;
  const fallback = u.fallback;

  // Prefer the server-fetched plan percentages. Fall back to the
  // JSONL-derived 5h pill when the first fetch hasn't completed yet or the
  // OAuth endpoint is unreachable.
  if (plan && plan.available && plan.meters) {
    const m = plan.meters;
    const order = ["session", "week_all", "week_opus", "week_sonnet"];
    const compactLabels = {
      session: "session",
      week_all: "week",
      week_opus: "opus",
      week_sonnet: "sonnet",
    };
    const present = order.filter((k) => m[k]);
    const stale =
      plan.fetched_at &&
      Math.floor(Date.now() / 1000) - plan.fetched_at > STALE_AFTER_S;
    const staleLine = stale
      ? `⚠ stale — last updated ${relTime(plan.fetched_at)} ago`
      : null;
    const title = present
      .map((k) =>
        [`${m[k].label}: ${m[k].percent}% used`, fmtReset(m[k].resets_at), ...paceLines(m[k])]
          .filter(Boolean)
          .join("\n  "),
      )
      .concat(staleLine ? [staleLine] : [])
      .join("\n\n");
    return (
      <div id="usage" class={`usage${stale ? " usage-stale" : ""}`} title={title}>
        {present.map((k) => (
          <MeterBar
            key={k}
            label={compactLabels[k]}
            m={m[k]}
            resets={fmtReset(m[k].resets_at)}
            pace={paceLines(m[k])}
          />
        ))}
        {stale && <span class="usage-stale-mark">⚠</span>}
      </div>
    );
  }

  if (!fallback || !fallback.available) {
    return <div id="usage" class="usage"></div>;
  }

  const active =
    (fallback.input_tokens || 0) +
    (fallback.cache_creation_tokens || 0) +
    (fallback.output_tokens || 0);
  const title =
    `Claude Code plan usage estimate (JSONL-derived; plan fetch not yet ready)\n` +
    `  ${fallback.messages} assistant messages\n` +
    `  ${fmtTokens(active)} active tokens\n` +
    `  ${fmtTokens(fallback.cache_read_tokens)} cache reads (discounted)`;
  return (
    <div id="usage" class="usage usage-fallback" title={title}>
      {`5h: ${fmtTokens(active)} · ${fmtResetCountdown(fallback.reset_at)}`}
    </div>
  );
}
