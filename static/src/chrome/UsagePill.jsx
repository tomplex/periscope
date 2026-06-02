// Plan-usage pill in the dashboard header. Prefers scraped TUI plan
// percentages; falls back to the JSONL-derived 5h estimate before the scrape
// completes. Ported from static/usage-pill.js — the imperative innerHTML
// build is replaced by JSX, but every CSS class (.usage, .usage-fallback,
// .usage-item, .usage-item-label, .usage-item-bar, .usage-item-fill +
// ok/warn/danger tone) and the formatting/tone logic are preserved verbatim.
//
// Reads the `usage` signal, which the poll loop (Task 5) writes as
// { scraped: data.usage_scrape, fallback: data.usage }.
import { usage } from "../store.js";

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
  return `resets in ${h}h ${m}m`;
}

function MeterBar({ label, pct, resets }) {
  const tone = pct >= 90 ? "danger" : pct >= 70 ? "warn" : "ok";
  return (
    <div class="usage-item" title={`${label} — ${pct}% used. Resets ${resets || ""}`}>
      <span class="usage-item-label">{label}</span>
      <span class="usage-item-bar">
        <span class={`usage-item-fill ${tone}`} style={`width:${pct}%`}></span>
      </span>
      <b>{pct}%</b>
    </div>
  );
}

export function UsagePill() {
  const u = usage.value || {};
  const scraped = u.scraped;
  const fallback = u.fallback;

  // Prefer the scraped TUI data (real plan percentages). Fall back to the
  // JSONL-derived 5h pill when the scrape hasn't completed yet (first ~20s
  // after server start) or failed.
  if (scraped && scraped.available && scraped.meters) {
    const m = scraped.meters;
    const order = ["session", "week_all", "week_sonnet"];
    const compactLabels = { session: "session", week_all: "week", week_sonnet: "sonnet" };
    const present = order.filter((k) => m[k]);
    const title = present
      .map((k) => `${m[k].label}: ${m[k].percent}% used\n  Resets ${m[k].resets}`)
      .join("\n\n");
    return (
      <div id="usage" class="usage" title={title}>
        {present.map((k) => (
          <MeterBar key={k} label={compactLabels[k]} pct={m[k].percent} resets={m[k].resets} />
        ))}
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
    `Claude Code plan usage estimate (JSONL-derived; scrape not yet ready)\n` +
    `  ${fallback.messages} assistant messages\n` +
    `  ${fmtTokens(active)} active tokens\n` +
    `  ${fmtTokens(fallback.cache_read_tokens)} cache reads (discounted)`;
  return (
    <div id="usage" class="usage usage-fallback" title={title}>
      {`5h: ${fmtTokens(active)} · ${fmtResetCountdown(fallback.reset_at)}`}
    </div>
  );
}
