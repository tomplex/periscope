// Right-pane of the split view (#detail). Four states, ported from detail.js:
//
//   - pane          — <Terminal> + side metadata (pending input / recap / last line)
//   - review-live   — idempotent LGTM iframe
//   - review-empty  — "Start review →" CTA
//   - empty         — nothing selected
//
// The selection is driven by the `railSelection` signal (string highlight-key:
// "pane:<pid>" | "review:<worktree>" | null) — the same value <Rail> writes and
// compares rows against. The live window data comes from the `windows` signal,
// so the header + side panel refresh on every poll without remounting the
// terminal (single-xterm reuse: <Terminal> is keyed on pid, so re-selecting the
// same pane preserves the instance — reconnect, not remount; coupling #5 /
// detail.js sameMount). `activeTarget` is set BEFORE any short-circuit so the
// shared paste handler always targets the current selection.
//
// CSS contract preserved verbatim: detail-pane/-header/-body, detail-xterm,
// detail-side, detail-review/-header/-iframe, detail-empty, review-start-card,
// hsep, header-pr/-linear/-ci/-git/-api-error, side-section/-label/-pending/
// -prompt/-mono. No class renamed.
import { useRef, useEffect, useState } from "preact/hooks";
import { windows, activeTarget, railSelection } from "../store.js";
import { apiCall, rewriteLgtmHost, prUrl, targetQuery } from "../util.js";
import { Terminal } from "../terminal/Terminal.jsx";
import { writeTerminalLine } from "../terminal/terminalCore.js";
import { Sidebar } from "../sidebar/Sidebar.jsx";

// Match the modal's /api/pane cadence so the two views feel identical.
const DETAIL_POLL_MS = 1500;

function lookupWindow(pid) {
  return (windows.value || []).find((w) => w.pid === pid) || null;
}

function lgtmSessionForWorktree(worktreeKey) {
  const w = (windows.value || []).find((x) => x.session === worktreeKey);
  return w?.lgtm?.slug ? w.lgtm : null;
}

// Shared image-paste handler (capture-phase via Terminal's onPaste). Mirrors
// the modal's onPaste exactly: write the pasted image to a temp path on the
// pane via /api/paste-image, keyed by the shared activeTarget signal.
async function handleDetailPaste(e) {
  if (!activeTarget.value) return;
  const items = e.clipboardData?.items || [];
  for (const item of items) {
    if (item.kind !== "file" || !item.type.startsWith("image/")) continue;
    const blob = item.getAsFile();
    if (!blob) continue;
    e.preventDefault();
    e.stopPropagation();
    try {
      const res = await fetch(`/api/paste-image?${targetQuery(activeTarget.value)}`, {
        method: "POST",
        headers: { "Content-Type": blob.type || "image/png" },
        body: blob,
      });
      const d = await res.json();
      if (!d.ok) writeTerminalLine(`\r\n\x1b[31m[periscope: image paste failed: ${d.error}]\x1b[0m`);
    } catch (err) {
      writeTerminalLine(`\r\n\x1b[31m[periscope: image paste error: ${err.message}]\x1b[0m`);
    }
    return;
  }
}

// Header for the pane detail — branch · PR · CI · Linear · git · model · ctx% ·
// API error. PR/Linear anchors are real links; they sit inside a non-clickable
// header so no stopPropagation is needed here (unlike the rail rows).
function PaneHeader({ w }) {
  const parts = [<span><b>{w.session || ""}</b></span>];
  if (w.branch) {
    parts.push(<><span class="hsep">·</span><span>{w.branch}</span></>);
  }
  if (w.pr) {
    const href = prUrl(w.repo_slug, w.pr);
    const ciCls = w.ci === "✓" ? "ci-ok" : w.ci === "✗" ? "ci-bad" : w.ci === "⟳" ? "ci-running" : "";
    const ciSpan = w.ci ? <> <span class={`header-ci ${ciCls}`}>{w.ci}</span></> : null;
    const inner = <>#{String(w.pr)}{ciSpan}</>;
    const prLink = href
      ? <a class="header-pr" href={href} target="_blank" rel="noopener">{inner}</a>
      : <span class="header-pr">{inner}</span>;
    parts.push(<><span class="hsep">·</span>{prLink}</>);
  }
  if (w.linked_linear) {
    const lid = w.linked_linear;
    const ltitle = w.linked_linear_title ? `: ${w.linked_linear_title}` : "";
    const lstatus = w.linked_linear_status ? ` [${w.linked_linear_status}]` : "";
    parts.push(
      <>
        <span class="hsep">·</span>
        <a class="header-linear" href={`https://linear.app/issue/${lid}`} target="_blank" rel="noopener" title={`Linear ${lid}${ltitle}${lstatus}`}>{lid}</a>
      </>
    );
  }
  if (w.git && w.git !== "clean") {
    parts.push(<><span class="hsep">·</span><span class="header-git">{w.git}</span></>);
  }
  if (w.is_claude && w.model) {
    parts.push(<><span class="hsep">·</span><span>{w.model.replace(/\s*\(.*\)/, "")}</span></>);
  }
  if (w.is_claude && w.context_pct != null) {
    parts.push(<><span class="hsep">·</span><span>{w.context_pct}%</span></>);
  }
  if (w.api_error) {
    parts.push(<><span class="hsep">·</span><span class="header-api-error" title="last tool result was an API error">⚠ API error</span></>);
  }
  return <header id="detail-pane-header" class="detail-pane-header">{parts.map((p, i) => <>{p}</>)}</header>;
}

// Rich per-pane sidebar — Linked / Notes / Activity, fed by a 1.5s poll of
// /api/pane (the same endpoint the modal sidebar uses). The poll restarts on
// `target` change and tears down when the pane is deselected or the user
// switches to a review row (Detail unmounts <PaneDetail>, killing this).
//
// Pitfall: the inner <Sidebar> mounts <NotesEditor> uncontrolled + keyed on
// pid, and the activity-stream scrollTop is preserved across renders. Both
// behaviors are coupling-#5 lessons from the modal port; the shared module
// preserves them so we don't have to re-derive here.
function SidePanel({ target }) {
  const [paneData, setPaneData] = useState(null);

  useEffect(() => {
    if (!target) return;
    let alive = true;
    async function tick() {
      try {
        const res = await fetch(`/api/pane?${targetQuery(target)}&lines=80`);
        if (!res.ok) return;
        const d = await res.json();
        if (alive) setPaneData(d);
      } catch (_) {}
    }
    tick();
    const h = setInterval(tick, DETAIL_POLL_MS);
    return () => { alive = false; clearInterval(h); };
  }, [target]);

  // While the first /api/pane response is in flight, render the bare aside so
  // the grid track keeps its 240px column (no width-jump when data lands).
  if (!paneData) return <aside id="detail-side" class="detail-side" />;

  return (
    <Sidebar
      data={paneData}
      onRefresh={() => {}}
      containerId="detail-side"
      containerClass="detail-side"
      idPrefix="detail"
    />
  );
}

// Pane state. The <Terminal> is keyed on pid: re-selecting the same pid keeps
// the instance (reconnect-not-remount), a different pid unmounts+remounts.
// activeTarget is set on every render so the shared paste handler tracks the
// live target even when the same pane stays selected.
function PaneDetail({ w }) {
  // Set the shared paste/active-target before anything else (coupling #5).
  useEffect(() => {
    activeTarget.value = w.target;
  }, [w.target]);

  return (
    <div id="detail-pane" class="detail-pane">
      <PaneHeader w={w} />
      <div class="detail-pane-body">
        <Terminal
          key={w.pid}
          id="detail-xterm"
          class="detail-xterm"
          target={w.target}
          onPaste={handleDetailPaste}
        />
        {/* Keyed on target so switching panes wipes paneData rather than
            showing the previous pane's PR/notes for ~1.5s until the new tick
            lands. */}
        <SidePanel key={w.target} target={w.target} />
      </div>
    </div>
  );
}

// Review state — live LGTM iframe. Idempotent: the iframe element persists and
// its src is reassigned ONLY when the (worktree → url) key changes; never per
// poll (that kills the SSE). Keyed on worktreeKey at the call site so switching
// worktrees tears down + remounts. activeTarget is cleared (review owns the
// iframe, no pane target).
function ReviewDetail({ worktreeKey }) {
  // The LGTM iframe is created imperatively ONCE and parked inside a host div
  // Preact owns; Preact never reconciles the iframe node, so the 1.5s/3s poll
  // re-render can't move/recreate it (which reloads an iframe in a browser and
  // drops the user's in-iframe file selection). Mirrors vanilla's static
  // iframe. `display:contents` keeps it laid out as a flex child of #detail-review.
  const hostRef = useRef(null);
  const iframeRef = useRef(null);
  const mountedSrc = useRef(null);
  const everHadSession = useRef(false);
  const session = lgtmSessionForWorktree(worktreeKey);
  const hasSession = !!(session && session.url);
  if (hasSession) everHadSession.current = true;
  // Use LGTM's EMBEDDING contract (?embedded=1&host=periscope), same as the
  // modal review — NOT the bare standalone URL. The bare URL boots LGTM's full
  // standalone app whose /events SSE fails when iframed, making LGTM reload
  // itself in a loop. Embedded mode is the iframe-designed path and is stable
  // (the modal review proves it).
  const url = hasSession
    ? `${rewriteLgtmHost(session.url)}?embedded=1&host=periscope`
    : null;

  useEffect(() => {
    activeTarget.value = null;
  }, [worktreeKey]);

  useEffect(() => {
    if (!hostRef.current || iframeRef.current) return;
    const f = document.createElement("iframe");
    f.id = "detail-review-iframe";
    f.className = "detail-review-iframe";
    hostRef.current.appendChild(f);
    iframeRef.current = f;
  });

  useEffect(() => {
    if (url && iframeRef.current && mountedSrc.current !== url) {
      iframeRef.current.src = url;
      mountedSrc.current = url;
    }
  }, [url]);

  // No session yet for this worktree → Start-review CTA. (Once a session has
  // existed, everHadSession keeps us on the iframe even across a transient gap.)
  if (!everHadSession.current) {
    return <ReviewStart worktreeKey={worktreeKey} />;
  }

  return (
    <div id="detail-review" class="detail-review">
      <header id="detail-review-header" class="detail-review-header">
        <span><b>review</b></span>
        <span class="hsep">·</span>
        <span>{worktreeKey}</span>
      </header>
      <div ref={hostRef} class="detail-review-iframe-host" style="display:contents" />
    </div>
  );
}

// Review-empty CTA. "Start review →" POSTs /api/lgtm/start with the worktree
// cwd; after start, the next /api/state poll surfaces lgtm.slug and this row
// transitions to ReviewDetail (the iframe mounts on the CTA→live transition).
function ReviewStart({ worktreeKey }) {
  async function start() {
    const w = (windows.value || []).find((x) => x.session === worktreeKey);
    if (!w) return;
    await apiCall("start review", "/api/lgtm/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cwd: w.cwd }),
    });
    // No explicit selectReview re-call: the next poll flips lgtm.slug on, and
    // the signal-driven render swaps in <ReviewDetail> automatically.
  }

  return (
    <div id="detail-review-start" class="detail-review-start">
      <div class="review-start-card">
        <div class="review-start-title">No LGTM session for this worktree</div>
        <button class="review-start-btn" onClick={start}>Start review →</button>
      </div>
    </div>
  );
}

function EmptyDetail() {
  // Markup matches vanilla detail.js:showEmpty verbatim, incl. the
  // #detail-empty-add button. It opens the + open picker via the same window
  // bridge the rail's "+ open" uses (registered by OpenPickerModal when the
  // overlays surface is mounted; a no-op if it isn't).
  return (
    <div id="detail-empty" class="detail-empty">
      <div class="detail-empty-card">
        <p>
          Select a tab on the left, or{" "}
          <button id="detail-empty-add" onClick={() => window.__periscopeOpenPicker?.()}>
            + open
          </button>{" "}
          to add one.
        </p>
      </div>
    </div>
  );
}

export function Detail() {
  const sel = railSelection.value;       // string highlight-key, or null
  const ws = windows.value || [];        // read so Detail re-renders each poll

  const isReview = !!sel && sel.startsWith("review:");
  const isPane = !!sel && sel.startsWith("pane:");
  const activeReviewWt = isReview ? sel.slice("review:".length) : null;

  // Keep EVERY opened review's iframe mounted (CSS-hidden when not active), so
  // switching among reviews/panes never reloads a review — each loads exactly
  // once. Display toggling never reloads an iframe (it stays in the DOM).
  // Mirrors how the modal keeps its review iframe alive across tab switches.
  // The opened set is pruned to worktrees still live in /api/state, so a
  // closed worktree's iframe is torn down rather than leaking.
  const opened = useRef(new Set());
  if (activeReviewWt) opened.current.add(activeReviewWt);
  const liveSessions = new Set(ws.map((w) => w.session));
  for (const wt of [...opened.current]) {
    if (wt !== activeReviewWt && !liveSessions.has(wt)) opened.current.delete(wt);
  }
  const reviewWts = [...opened.current];

  const paneW = isPane ? lookupWindow(sel.slice("pane:".length)) : null;

  return (
    <section id="detail">
      {!sel && <EmptyDetail />}
      {isPane && (paneW ? <PaneDetail w={paneW} /> : <EmptyDetail />)}
      {reviewWts.map((wt) => (
        <div key={wt} style={activeReviewWt === wt ? "display:contents" : "display:none"}>
          <ReviewDetail key={wt} worktreeKey={wt} />
        </div>
      ))}
    </section>
  );
}
