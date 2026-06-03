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
import {
  windows, activeTarget, railSelection, paneTranscript,
  paneTabs, paneActiveTab, openFileTab, closeFileTab, setActiveTab,
} from "../store.js";
import { apiCall, rewriteLgtmHost, prUrl, targetQuery } from "../util.js";
import { getDetailMode, setDetailMode } from "../prefs.js";
import { Terminal } from "../terminal/Terminal.jsx";
import { TerminalSearch } from "../terminal/TerminalSearch.jsx";
import { writeTerminalLine, scrollTerminalToBottom, isTerminalAtBottom, setTerminalFileCallback } from "../terminal/terminalCore.js";
import { Sidebar } from "../sidebar/Sidebar.jsx";
import { TranscriptView } from "./Transcript.jsx";
import { PreviewTab } from "../preview/PreviewTab.jsx";

// Match the modal's /api/pane cadence so the two views feel identical.
const DETAIL_POLL_MS = 1500;

// Last segment of a path, for the tab label. Strips trailing slash and
// returns the segment after the last "/". Falls back to the full path
// if no "/" is present (rare — REL_PATH_RE requires at least one).
function tabLabel(path) {
  const p = path.replace(/\/+$/, "");
  const i = p.lastIndexOf("/");
  return i >= 0 ? p.slice(i + 1) : p;
}

// Browser-style tab strip at the top of an open pane. The first tab is
// the pane itself (terminal or transcript, per the header toggle); each
// subsequent tab is an open file preview. Clicking a tab switches the
// body content; the × on a file tab closes it (falls back to the pane
// tab if that tab was active). State is per-pid signal, so tabs persist
// across rail-selection changes and only evaporate on reload.
function TabStrip({ pid, paneLabel }) {
  const tabs = paneTabs.value[pid] || [];
  const active = paneActiveTab.value[pid] || "pane";
  return (
    <div class="detail-tab-strip" role="tablist">
      <button
        type="button"
        role="tab"
        class={`detail-tab${active === "pane" ? " is-active" : ""}`}
        aria-selected={active === "pane"}
        onClick={() => setActiveTab(pid, "pane")}
        title={paneLabel}
      >
        <span class="detail-tab-label">{paneLabel}</span>
      </button>
      {tabs.map((t) => {
        const key = `file:${t.path}`;
        const isActive = active === key;
        return (
          <button
            type="button"
            role="tab"
            key={key}
            class={`detail-tab${isActive ? " is-active" : ""}`}
            aria-selected={isActive}
            onClick={() => setActiveTab(pid, key)}
            title={t.path}
          >
            <span class="detail-tab-label">{tabLabel(t.path)}</span>
            <span
              class="detail-tab-close"
              title="Close tab"
              onClick={(e) => { e.stopPropagation(); closeFileTab(pid, t.path); }}
            >×</span>
          </button>
        );
      })}
    </div>
  );
}

function lookupWindow(pid) {
  return (windows.value || []).find((w) => w.pid === pid) || null;
}

function computeMode(w) {
  if (!w || !w.is_claude) return "terminal";
  // Default to terminal; per-pid explicit choice (the Transcript/Terminal toggle
  // in the header) is persisted in UI prefs (detail_mode_by_pid) so it survives
  // reloads. We deliberately do NOT auto-flip to transcript once data is seen —
  // that surprises users who were happily watching the terminal.
  return getDetailMode(w.pid) || "terminal";
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
// API error · ✨ auto-rename. PR/Linear anchors are real links; they sit
// inside a non-clickable header so no stopPropagation is needed here (unlike
// the rail rows).
function PaneHeader({ w, mode, onMode }) {
  const [renaming, setRenaming] = useState(false);

  async function autoRename() {
    if (renaming) return;
    setRenaming(true);
    try {
      // The endpoint mutates the tmux window's title via Claude; the next
      // /api/state poll surfaces the new name in the rail row. No need to
      // touch local state — refresh is implicit via the poll loop.
      await apiCall("auto-rename window", `/api/auto-rename-window?${targetQuery(w.target)}`, {
        method: "POST",
      });
    } finally {
      setRenaming(false);
    }
  }

  const parts = [
    <button
      type="button"
      class={`header-rename${renaming ? " busy" : ""}`}
      title="ask Claude to rename this window"
      disabled={renaming}
      onClick={autoRename}
    >
      ✨
    </button>,
  ];
  // Session: shown only when it isn't just the branch repeated (the
  // worktree-pane case — session and branch are literally the same string
  // there, which produced a triple-printed line per the screenshot). For
  // non-worktree panes session is meaningful ("personal", "default"), so
  // keep it then.
  if (w.session && w.session !== w.branch) {
    parts.push(<span><b>{w.session}</b></span>);
  }
  if (w.cwd) {
    const segs = w.cwd.split("/").filter(Boolean);
    // Slugified-branch heuristic: when the last cwd segment is just the
    // branch with slashes flipped to hyphens (the standard worktree
    // layout), the segment is fully redundant with the branch chip — show
    // only the parent (the project name, e.g. "fdy") instead.
    const last = segs[segs.length - 1] || "";
    const branchSlug = w.branch ? w.branch.replace(/[\/\\]/g, "-") : null;
    let tail;
    if (branchSlug && last === branchSlug && segs.length >= 2) {
      tail = segs[segs.length - 2];
    } else {
      tail = segs.length >= 2 ? segs.slice(-2).join("/") : (segs[0] || w.cwd);
    }
    // Empty tail (root cwd) — skip the chip entirely.
    if (tail) {
      parts.push(
        <>
          {parts.length > 1 && <span class="hsep">·</span>}
          <span
            class="header-cwd-reveal"
            title={`Reveal ${w.cwd} in Finder`}
            onClick={async () => {
              try {
                await fetch(
                  `/api/fs/open?session=${encodeURIComponent(w.session)}&index=${w.index}&path=.&action=reveal`,
                  { method: "POST" },
                );
              } catch (_) { /* best-effort */ }
            }}
          >{tail}</span>
        </>
      );
    }
  }
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
  if (w.is_claude) {
    parts.push(
      <span class="detail-mode-toggle">
        <button class={mode === "transcript" ? "is-active" : ""}
                onClick={() => onMode("transcript")}>Transcript</button>
        <button class={mode === "terminal" ? "is-active" : ""}
                onClick={() => onMode("terminal")}>Terminal</button>
      </span>
    );
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
  const mode = computeMode(w);
  // Set the shared paste/active-target before anything else (coupling #5).
  useEffect(() => {
    activeTarget.value = w.target;
  }, [w.target]);

  // Wire the terminal's file-link callback. Cmd+click on a path → add
  // (or focus) a preview tab for the active pane.
  useEffect(() => {
    setTerminalFileCallback((rawPath) => {
      // Split off optional :NN line suffix for the line jump.
      let path = rawPath;
      let line = null;
      const m = path.match(/^(.*?):(\d+)$/);
      if (m) { path = m[1]; line = m[2]; }
      openFileTab({ path, line });
    });
    return () => setTerminalFileCallback(null);
  }, [w.target]);

  // Publish the pane header's height as --detail-header-h so the (absolutely
  // positioned, kept-mounted) transcript host can sit exactly below it. The
  // header flex-wraps (PR/branch/model chips), so its height is dynamic — a
  // ResizeObserver keeps the offset correct instead of a magic constant.
  useEffect(() => {
    const el = document.getElementById("detail-pane-header");
    const detail = document.getElementById("detail");
    if (!el || !detail) return;
    const apply = () => detail.style.setProperty("--detail-header-h", el.offsetHeight + "px");
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => ro.disconnect();
  }, [w.target]);

  // Switching to terminal mode un-hides the xterm; pin it to the live bottom
  // (it otherwise shows wherever it was last scrolled — often the top).
  useEffect(() => {
    if (mode !== "terminal") return;
    const r = requestAnimationFrame(() => requestAnimationFrame(scrollTerminalToBottom));
    return () => cancelAnimationFrame(r);
  }, [mode]);

  // Show the scroll-to-bottom button only when scrolled up. Poll the terminal's
  // position while it's the visible mode (cheap buffer read; ~instant feel).
  const [atBottom, setAtBottom] = useState(true);
  useEffect(() => {
    if (mode !== "terminal") return;
    const tick = () => setAtBottom(isTerminalAtBottom());
    tick();
    const h = setInterval(tick, 250);
    return () => clearInterval(h);
  }, [mode, w.pid]);

  // Working → (idle | done) transition pulse for Claude panes. The server
  // refines idle → done when there's an unacknowledged completion stamp
  // (window_view.py:120-121), so the visible-to-client transition is
  // almost always working → done. Watching both is correct.
  const prevState = useRef(w.state);
  useEffect(() => {
    const wasWorking = prevState.current === "working";
    const isFinished = w.state === "idle" || w.state === "done";
    if (wasWorking && isFinished && w.is_claude) {
      const el = document.getElementById("detail-xterm");
      if (el) {
        el.classList.add("bell-pulse");
        setTimeout(() => el.classList.remove("bell-pulse"), 400);
      }
    }
    prevState.current = w.state;
  }, [w.state, w.is_claude]);

  const tabs = paneTabs.value[w.pid] || [];
  const activeTab = paneActiveTab.value[w.pid] || "pane";
  const paneTabActive = activeTab === "pane";
  // Label the pane tab with the window name (auto-rename target). Same
  // fallback the rail rows use (RailRows.jsx).
  const paneLabel = w.name || (w.is_claude ? "claude" : "shell");

  return (
    <div id="detail-pane" class="detail-pane">
      <PaneHeader w={w} mode={mode} onMode={(next) => setDetailMode(w.pid, next)} />
      <TabStrip pid={w.pid} paneLabel={paneLabel} />
      <div class="detail-pane-body">
        <div class="detail-term-host" style={paneTabActive && mode === "terminal" ? "display:contents" : "display:none"}>
          <Terminal
            key={w.pid}
            id="detail-xterm"
            class="detail-xterm"
            target={w.target}
            onPaste={handleDetailPaste}
          />
        </div>
        {/* Each opened file preview tab stays mounted; CSS-hidden when
            inactive so switching is instant and per-tab CM state survives.
            Keyed by path so re-opening the same file finds its existing
            mounted instance. */}
        {tabs.map((t) => {
          const tabKey = `file:${t.path}`;
          const shown = activeTab === tabKey;
          return (
            <div
              key={tabKey}
              class="detail-preview-host"
              style={shown ? "display:contents" : "display:none"}
            >
              <PreviewTab entry={t} />
            </div>
          );
        })}
        {/* Keyed on target so switching panes wipes paneData rather than
            showing the previous pane's PR/notes for ~1.5s until the new tick
            lands. SidePanel takes `target`, NOT `w`. */}
        <SidePanel key={w.target} target={w.target} />
      </div>
      {paneTabActive && mode === "terminal" && !atBottom && (
        <button
          class="term-scroll-bottom"
          title="Scroll to latest"
          onClick={scrollTerminalToBottom}
        >
          ⤓
        </button>
      )}
      {paneTabActive && mode === "terminal" && <TerminalSearch />}
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

  // Keep every opened Claude transcript mounted (CSS-hidden when not active) so
  // scroll position + expanded segments survive pane switches — same discipline
  // as the review iframes. Pruned to pids still live in /api/state.
  const openedTr = useRef(new Set());
  if (isPane && paneW?.is_claude) openedTr.current.add(paneW.pid);
  const livePids = new Set(ws.map((x) => x.pid));
  const selMode = computeMode(paneW);
  for (const pid of [...openedTr.current]) {
    const isSelected = isPane && paneW?.pid === pid;
    if (!isSelected && !livePids.has(pid)) {
      openedTr.current.delete(pid);
      // Evict from the shared transcript store to bound memory across
      // long sessions where many panes have been opened.
      if (paneTranscript.value[pid]) {
        const { [pid]: _drop, ...rest } = paneTranscript.value;
        paneTranscript.value = rest;
      }
    }
  }
  const trPids = [...openedTr.current];

  return (
    <section id="detail">
      {!sel && <EmptyDetail />}
      {isPane && (paneW ? <PaneDetail w={paneW} /> : <EmptyDetail />)}
      {reviewWts.map((wt) => (
        <div key={wt} style={activeReviewWt === wt ? "display:contents" : "display:none"}>
          <ReviewDetail key={wt} worktreeKey={wt} />
        </div>
      ))}
      {trPids.map((pid) => {
        const tw = lookupWindow(pid);
        const isSelected = isPane && paneW?.pid === pid;
        // Transcript host visibility is gated on the "pane" tab being
        // active too — switching to a file preview tab must hide the
        // transcript along with the terminal.
        const paneTabActive = (paneActiveTab.value[pid] || "pane") === "pane";
        const shown = isSelected && paneTabActive && selMode === "transcript";
        return (
          <div key={`tr:${pid}`} class="detail-transcript-host"
               style={shown ? "display:flex" : "display:none"}>
            <TranscriptView target={tw?.target} pid={pid} selected={isSelected}
                            state={tw?.state} waitingFor={tw?.waiting_for}
                            spinner={tw?.spinner} />
          </div>
        );
      })}
    </section>
  );
}
