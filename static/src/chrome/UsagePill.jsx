// Plan-usage pill in the dashboard header. Prefers the plan percentages
// from Anthropic's OAuth usage endpoint (server-fetched, authoritative);
// falls back to the JSONL-derived 5h estimate before the first fetch
// completes. CSS classes (.usage, .usage-fallback, .usage-item,
// .usage-item-label, .usage-item-bar, .usage-item-fill + ok/warn/danger
// tone) are shared with the original usage-pill styling.
//
// Reads the `usage` signal, which the poll loop writes as
// { plan: data.usage_plan, fallback: data.usage } — `plan` being a mapping of
// account id → that subscription's meters.
//
// COLLAPSED IS THE POINT. Two subscriptions exist because one keeps hitting
// its weekly wall, so the pill's job is "which account has room?", answerable
// without a click: one row per account, letter-labelled A/B to match the
// launcher's picker, showing only that account's BINDING meter (highest
// percent) with the existing ok/warn/danger tone. A pinned red bar next to a
// near-empty one reads at a glance. Click expands to every meter per account.
import { useState } from "preact/hooks";
import { usage } from "../store.js";
import { relTime } from "../util.js";
import { summarizeAccounts } from "./usageSummary.js";

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

// Every meter of one account, as the tooltip text for that account's row.
// Collapsing to the binding meter hides the rest from sight, not from reach.
// Carried on the row rather than the pill because each MeterBar's own title
// wins on hover — the row's only reliably-hoverable area is its letter.
function acctTitle(a, expanded) {
  const lines = a.meters.map(({ m }) =>
    [`${m.label}: ${m.percent}% used`, fmtReset(m.resets_at), ...paceLines(m)]
      .filter(Boolean)
      .join("\n  "),
  );
  if (a.stale) lines.push(`⚠ stale — last updated ${relTime(a.fetchedAt)} ago`);
  lines.push(expanded ? "(click to collapse)" : "(click to show every meter)");
  return [`account ${a.label}`, ...lines].join("\n\n");
}

export function UsagePill() {
  // Expansion is view-only and deliberately unpersisted: the collapsed answer
  // is the one worth having on every load.
  const [expanded, setExpanded] = useState(false);
  const u = usage.value || {};
  const fallback = u.fallback;
  const accounts = summarizeAccounts(u.plan, Math.floor(Date.now() / 1000));

  // Prefer the server-fetched plan percentages. Fall back to the JSONL-derived
  // 5h pill only when NO account has meters — before the first fetch completes,
  // or with every OAuth credential dead. One live account still beats the
  // estimate, so a single dead credential must not trigger the fallback.
  if (accounts.some((a) => a.available)) {
    return (
      <div
        id="usage"
        class={`usage${expanded ? " is-expanded" : ""}`}
        onClick={() => setExpanded((v) => !v)}
      >
        {accounts.map((a) => (
          <div
            key={a.id}
            class={`usage-acct${a.stale ? " is-stale" : ""}`}
            title={acctTitle(a, expanded)}
          >
            <span class="usage-acct-label">{a.label}</span>
            {a.available ? (
              (expanded ? a.meters : [a.headline]).map(({ key, label, m }) => (
                <MeterBar
                  key={key}
                  label={label}
                  m={m}
                  resets={fmtReset(m.resets_at)}
                  pace={paceLines(m)}
                />
              ))
            ) : (
              // A blank row would read as "0% used" — the opposite of the truth.
              <span class="usage-acct-off" title="no usable credential for this account">
                no data
              </span>
            )}
            {a.stale && <span class="usage-stale-mark">⚠</span>}
          </div>
        ))}
      </div>
    );
  }

  if (!fallback?.available) {
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
