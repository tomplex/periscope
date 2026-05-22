// Plan-usage pill in the dashboard header. Prefers scraped TUI plan
// percentages; falls back to the JSONL-derived 5h estimate before the
// scrape completes. `updateUsagePill` is called each poll from grid.js.

import { escapeHtml } from './util.js';

const usageEl = document.getElementById("usage");

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

function meterBar(label, pct, resets) {
  const tone = pct >= 90 ? "danger" : pct >= 70 ? "warn" : "ok";
  return `
    <div class="usage-item" title="${escapeHtml(label)} — ${pct}% used. Resets ${escapeHtml(resets || "")}">
      <span class="usage-item-label">${escapeHtml(label)}</span>
      <span class="usage-item-bar"><span class="usage-item-fill ${tone}" style="width:${pct}%"></span></span>
      <b>${pct}%</b>
    </div>
  `;
}

export function updateUsagePill(scraped, fallback) {
  if (!usageEl) return;
  // Prefer the scraped TUI data (real plan percentages). Fall back to the
  // JSONL-derived 5h pill when the scrape hasn't completed yet (first ~20s
  // after server start) or failed.
  if (scraped && scraped.available && scraped.meters) {
    const m = scraped.meters;
    const order = ["session", "week_all", "week_sonnet"];
    const compactLabels = { session: "session", week_all: "week", week_sonnet: "sonnet" };
    usageEl.classList.remove("usage-fallback");
    usageEl.innerHTML = order
      .filter((k) => m[k])
      .map((k) => meterBar(compactLabels[k], m[k].percent, m[k].resets))
      .join("");
    usageEl.title = order
      .filter((k) => m[k])
      .map((k) => `${m[k].label}: ${m[k].percent}% used\n  Resets ${m[k].resets}`)
      .join("\n\n");
    return;
  }
  if (!fallback || !fallback.available) {
    usageEl.classList.remove("usage-fallback");
    usageEl.textContent = "";
    usageEl.title = "";
    return;
  }
  const active = (fallback.input_tokens || 0) + (fallback.cache_creation_tokens || 0) + (fallback.output_tokens || 0);
  usageEl.classList.add("usage-fallback");
  usageEl.textContent = `5h: ${fmtTokens(active)} · ${fmtResetCountdown(fallback.reset_at)}`;
  usageEl.title = `Claude Code plan usage estimate (JSONL-derived; scrape not yet ready)\n` +
    `  ${fallback.messages} assistant messages\n` +
    `  ${fmtTokens(active)} active tokens\n` +
    `  ${fmtTokens(fallback.cache_read_tokens)} cache reads (discounted)`;
}
