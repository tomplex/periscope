// The native review walkthrough (THI-443): a fixed header over one scrolling
// body. Each stop is a card: its title row, its diagrams across the full
// width, then its prose beside its files and code changes. Reads
// /api/reviews/walkthrough; the file is the artifact, this is a lens on it
// (2026-10-26 spec D2). No pane target here, so hunk heads are inert. The
// lightbox host lives in Overlays.jsx, at the app root.
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { Hunk, langFor } from "../diff/ChangesTab.jsx";
import { refreshReviews } from "../poll.js";
import { Mermaid } from "../split/Mermaid.jsx";
import { renderMarkdown } from "../split/markdown.jsx";
import { prStateMeta } from "../util.js";
import { evidenceFor, evidenceIndex, HIDDEN_WHY, hiddenSummary, isReviewed, parseWalkthrough, stopSig, withMark } from "./walkthrough.js";

async function postJson(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

// The keys the comment box listens for, named the way this keyboard labels them.
export function sendHint(platform) {
  return /Mac|iPhone|iPad/.test(platform || "") ? "⌘↩ to send" : "Ctrl+↩ to send";
}
const SEND_HINT = sendHint(typeof navigator === "undefined" ? "" : navigator.platform);

// What a comment POST's result does to the box: a delivered or saved comment
// closes it; anything else keeps the text so nothing typed is lost.
export function commentOutcome(r) {
  if (r.ok) return { close: true, note: "sent to the owning pane" };
  if (r.status === 409) return { close: true, note: `saved, ${r.data?.detail || "not delivered"}` };
  return { close: false, note: r.data?.detail || `HTTP ${r.status}` };
}

// Smooth scrolling unless the viewer asked for less motion.
const motion = () => (typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");

const kindOf = (stop) => (stop.knownKind ? stop.kind : "other");

function Comments({ list }) {
  if (!list.length) return null;
  return (
    <ul class="wt-comments">
      {list.map((c, i) => (
        <li key={i} class={c.resolved ? "is-resolved" : ""}>
          <span class="wt-comment-ts">{c.ts}</span>
          <span class="wt-comment-state">{c.resolved ? "resolved" : "open"}</span>
          <div class="wt-comment-body">{renderMarkdown(c.body, { demote: false, softBreaks: "space" })}</div>
        </li>
      ))}
    </ul>
  );
}

// The open box, pure, so a render test can see it.
export function CommentForm({ stop, text, busy, note, textRef, onInput, onSend, onCancel }) {
  return (
    <div class="wt-comment-box is-open">
      <textarea ref={textRef} rows={3} value={text} placeholder="comment on this stop…" aria-label={`comment on ${stop}`}
        onInput={(e) => onInput(e.currentTarget.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onSend(); }} />
      <div class="wt-comment-actions">
        <button type="button" class="wt-btn" disabled={busy || !text.trim()} onClick={onSend}>send</button>
        <button type="button" class="wt-link" onClick={onCancel}>cancel</button>
        <span class="wt-hint">{SEND_HINT}</span>
        {note ? <span class="wt-comment-note" role="status">{note}</span> : null}
      </div>
    </div>
  );
}

// Half-typed comments, by stop. Marking a stop reviewed folds it, which unmounts
// its box (hiding it instead would break mermaid, which can't measure text in a
// display:none subtree), so a draft must outlive the component.
const drafts = new Map();

// A "comment" link until used: thirteen always-open boxes buried the stops.
function CommentBox({ repo, branch, stop, onPosted }) {
  const draftKey = `${repo}\n${branch}\n${stop}`;
  const [open, setOpen] = useState(() => drafts.has(draftKey));
  const [text, setTextState] = useState(() => drafts.get(draftKey) || "");
  const setText = (v) => {
    if (v) drafts.set(draftKey, v);
    else drafts.delete(draftKey);
    setTextState(v);
  };
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const textRef = useRef(null);
  useEffect(() => { if (open) textRef.current?.focus(); }, [open]);
  async function send() {
    if (busy || !text.trim()) return;
    setBusy(true);
    try {
      const out = commentOutcome(await postJson("/api/reviews/comment", { repo, branch, stop, body: text }));
      setNote(out.note);
      if (out.close) { setText(""); setOpen(false); }
      onPosted();
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  }
  if (!open) {
    return (
      <div class="wt-comment-box">
        <button type="button" class="wt-link" onClick={() => { setNote(""); setOpen(true); }}>comment</button>
        {note ? <span class="wt-comment-note" role="status">{note}</span> : null}
      </div>
    );
  }
  return <CommentForm stop={stop} text={text} busy={busy} note={note} textRef={textRef}
    onInput={setText} onSend={send} onCancel={() => setOpen(false)} />;
}

// One accessible name in both states: aria-pressed carries the state, and the ✓
// is decoration (a changing label plus "pressed" would be announced twice).
export function ReviewedToggle({ on, onToggle, error }) {
  return (
    <span class="wt-reviewed">
      {error ? <span class="wt-reviewed-err" role="alert">{error}</span> : null}
      <button type="button" class={`wt-reviewed-btn${on ? " is-on" : ""}`} aria-pressed={on ? "true" : "false"} onClick={onToggle}>
        {on ? <span aria-hidden="true">✓ </span> : null}reviewed
      </button>
    </span>
  );
}

// The right column (THI-443 D5): the stop's files, one summary line for the
// ones with nothing to show, then the hunks expanded.
function Evidence({ ev, hl, colRef, onGo }) {
  const summary = hiddenSummary(ev.hidden);
  if (!ev.shown.length && !summary) return <div class="wt-evidence is-empty" />;
  return (
    <aside class="wt-evidence" ref={colRef} aria-label="files and code changes">
      <ul class="wt-files">
        {ev.shown.map((e, i) => {
          const label = (
            <>
              <span class="wt-file-name">{e.name}</span>
              {e.ref.start != null ? <span class="wt-file-range">{`:${e.ref.start}${e.ref.end !== e.ref.start ? `-${e.ref.end}` : ""}`}</span> : null}
              {ev.noDiff ? null : <span class="wt-file-count">{`${e.hunks.length} change${e.hunks.length === 1 ? "" : "s"}`}</span>}
            </>
          );
          return (
            <li key={i}>
              {ev.noDiff
                ? <span class="wt-file" title={e.ref.path}>{label}</span>
                : <button type="button" class="wt-file" title={e.ref.path} onClick={() => onGo(i)}>{label}</button>}
            </li>
          );
        })}
      </ul>
      {summary ? (
        <details class="wt-hidden">
          <summary>{summary}</summary>
          <ul>
            {Object.entries(ev.hidden).flatMap(([why, refs]) => refs.map((r, i) => (
              <li key={`${why}${i}`}><span class="wt-hidden-path">{r.raw}</span>{` · ${HIDDEN_WHY[why]}`}</li>
            )))}
          </ul>
        </details>
      ) : null}
      {ev.noDiff ? <p class="wt-nodiff">no worktree, so code changes are unavailable</p> : ev.shown.map((e, i) => (
        <section class="wt-ev-file" data-ev={i} key={i}>
          <div class="wt-ev-path">{e.ref.raw}</div>
          {e.hunks.map((h, j) => <Hunk key={j} hunk={h} lang={langFor(e.ref.path)} hl={hl} onOpen={null} />)}
        </section>
      ))}
    </aside>
  );
}

function Stop({ stop, n, diffFiles, comments, repo, branch, hl, reviewed, toggleError, onToggleReviewed, onPosted }) {
  const [unfolded, setUnfolded] = useState(false);
  const col = useRef(null);
  const done = isReviewed(stop, reviewed);
  const folded = done && !unfolded;
  const ev = evidenceFor(stop.refs, diffFiles);
  // Wide, the column scrolls on its own, so move it rather than the page.
  // Otherwise bring the file into view only if it isn't already ("nearest"), so
  // a column that fits on screen never jumps the page (D5).
  function goTo(i) {
    const c = col.current;
    const el = c?.querySelector(`[data-ev="${i}"]`);
    if (!el) return;
    if (c.scrollHeight > c.clientHeight) c.scrollTo({ top: el.offsetTop - 8, behavior: motion() });
    else el.scrollIntoView({ block: "nearest", behavior: motion() });
    // Restart the flash even on a second click of the same file: the reflow
    // between remove and add makes the browser replay the animation.
    el.classList.remove("is-flash");
    el.getBoundingClientRect();
    el.classList.add("is-flash");
  }
  const cite = (c) => {
    const i = ev.noDiff ? -1 : evidenceIndex(ev.shown, c);
    return (
      <button type="button" class={`md-cite${i >= 0 ? "" : " is-inert"}`}
        title={i >= 0 ? "show in the code column" : "not among this stop's changes"}
        onClick={() => { if (i >= 0) goTo(i); }}>{c.raw}</button>
    );
  };
  const title = <><span class="wt-stop-n">{`${n}.`}</span> {stop.title}</>;
  return (
    <section class={`wt-stop wt-kind-${kindOf(stop)}${folded ? " is-folded" : ""}`} id={`stop-${stop.id}`}>
      <div class="wt-stop-head">
        <h3 class="wt-stop-title">
          {done
            ? <button type="button" class="wt-fold" aria-expanded={folded ? "false" : "true"} title={folded ? "show this stop" : "fold this stop"}
                onClick={() => setUnfolded(!unfolded)}>{title}</button>
            : title}
        </h3>
        {stop.kind ? <span class="wt-kind-badge">{stop.kind}</span> : null}
        <ReviewedToggle on={done} error={toggleError} onToggle={() => { setUnfolded(false); onToggleReviewed(stop, !done); }} />
      </div>
      {folded ? null : (
        <>
          {stop.diagrams.length ? (
            <div class="wt-diagrams">
              {/* label: a stop can have 2–3 diagrams, and their ⤢ buttons need telling apart. */}
              {stop.diagrams.map((code, i) => <Mermaid code={code} zoom label={`${stop.title}, diagram ${i + 1}`} key={i} />)}
            </div>
          ) : null}
          <div class="wt-cols">
            <div class="wt-prose">
              <div class="md-doc wt-stop-body">
                {renderMarkdown(stop.bodyMarkdown, { demote: false, softBreaks: "space", mermaid: true, meta: true, cite, zoom: true })}
              </div>
              <Comments list={comments} />
              <CommentBox repo={repo} branch={branch} stop={stop.title} onPosted={onPosted} />
            </div>
            <Evidence ev={ev} hl={hl} colRef={col} onGo={goTo} />
          </div>
        </>
      )}
    </section>
  );
}

function Contents({ stops, reviewed }) {
  if (!stops.length) return null;
  return (
    <nav class="wt-toc" aria-label="stops">
      <ol>
        {stops.map((s, i) => (
          <li key={s.id}>
            {/* A button, not a #link: the URL and periscope's routing stay put. */}
            <button type="button" class={`wt-toc-item wt-kind-${kindOf(s)}`}
              onClick={() => document.getElementById(`stop-${s.id}`)?.scrollIntoView({ block: "start", behavior: motion() })}>
              <span class="wt-toc-n">{`${i + 1}.`}</span>
              <span class="wt-toc-dot" aria-hidden="true" />
              <span class="wt-toc-title">{s.title}</span>
              {isReviewed(s, reviewed) ? <span class="wt-toc-done" title="reviewed">✓</span> : null}
            </button>
          </li>
        ))}
      </ol>
    </nav>
  );
}

// stale_by is null both with no worktree (no diff either) and when the
// worktree no longer has the recorded head (rebased away); the queue calls the
// second "stale: unknown", and so does this.
function Staleness({ staleBy, header, diff }) {
  if (staleBy > 0) return <span class="wt-badge is-warn">{`stale: ${staleBy} commit${staleBy === 1 ? "" : "s"}`}</span>;
  if (staleBy == null && header && !diff) return <span class="wt-badge">no worktree</span>;
  if (staleBy == null && header && diff) return <span class="wt-badge is-warn">stale: unknown</span>;
  return null;
}

// The page itself, pure, so render-to-string tests can see every state.
export function WalkthroughView({ data, repo, branch, pr, hl, reviewed, toggleErrors, error, onToggleReviewed, onPosted }) {
  const { text, comments, diff, stale_by } = data;
  // Every mark and refetch re-renders the page; parse only when the text changes.
  const w = useMemo(() => parseWalkthrough(text), [text]);
  const byStop = new Map();
  for (const c of comments) (byStop.get(c.stop) || byStop.set(c.stop, []).get(c.stop)).push(c);
  const attached = new Set(w.stops.map((s) => s.title));
  const unattached = comments.filter((c) => !attached.has(c.stop));
  const done = w.stops.filter((s) => isReviewed(s, reviewed)).length;
  return (
    <div class="walkthrough">
      <header class="wt-head">
        <h2 class="wt-title">{w.title || branch}</h2>
        <div class="wt-sub">
          <span>{`${repo} · ${branch}`}</span>
          {/* Styled like the queue's PR link (THI-442): unclassed, it was the UA's default blue. */}
          {w.header?.pr ? (pr?.url && pr.number === w.header.pr
            ? <a class={`rq-pr-link ${prStateMeta(pr.state).cls}`} href={pr.url} target="_blank" rel="noopener">{`PR #${w.header.pr}`}</a>
            : <span>{`PR #${w.header.pr}`}</span>) : null}
          <Staleness staleBy={stale_by} header={w.header} diff={diff} />
          {w.stops.length ? <span class="wt-progress">{`${done} / ${w.stops.length} reviewed`}</span> : null}
        </div>
      </header>
      <div class="wt-scroll">
        {error ? <div class="wt-error" role="alert">{`refresh failed: ${error}`}</div> : null}
        <div class="wt-cols wt-intro">
          <div class="md-doc wt-problem">{renderMarkdown(w.problemMarkdown, { demote: false, softBreaks: "space" })}</div>
          <Contents stops={w.stops} reviewed={reviewed} />
        </div>
        {w.stops.map((s, i) => (
          <Stop key={s.id} stop={s} n={i + 1} diffFiles={diff?.files || null} comments={byStop.get(s.title) || []}
            repo={repo} branch={branch} hl={hl} reviewed={reviewed} toggleError={toggleErrors[s.title] || null}
            onToggleReviewed={onToggleReviewed} onPosted={onPosted} />
        ))}
        {unattached.length ? (
          <section class="wt-stop wt-kind-other">
            <div class="wt-stop-head"><h3 class="wt-stop-title">unattached comments</h3></div>
            <Comments list={unattached} />
          </section>
        ) : null}
      </div>
    </div>
  );
}

// `sig` changes when the row's head / staleness / unresolved count does (from
// the 10 s reviews poll), so a reply, a resolve or a re-prepare refetches the
// open view instead of leaving it stale until re-selected.
export function Walkthrough({ repo, branch, pr, sig }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [hl, setHl] = useState(null);
  const [nonce, setNonce] = useState(0);
  const [reviewed, setReviewed] = useState({});
  const [toggleErrors, setToggleErrors] = useState({});
  // Marks in flight, and marks settled: a refetch must not apply a `reviewed`
  // it read before a mark landed, whether it arrives mid-POST or just after
  // ("send a comment, then mark the stop" starts exactly that refetch).
  const pending = useRef(0);
  const settled = useRef(0);

  useEffect(() => {
    let alive = true;
    import("../preview/highlightCode.jsx").then((m) => { if (alive) setHl(() => m.highlightCode); }).catch(() => {});
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    let alive = true;
    const marksAtStart = settled.current;
    (async () => {
      try {
        const res = await fetch(`/api/reviews/walkthrough?repo=${encodeURIComponent(repo)}&branch=${encodeURIComponent(branch)}`);
        const body = await res.json().catch(() => ({}));
        if (!alive) return;
        // Keep the last good data on a failed refetch: unmounting every Stop
        // would drop the open hunks and any half-typed comment.
        if (!res.ok) { setState((s) => ({ loading: false, error: body.detail || `HTTP ${res.status}`, data: s.data })); return; }
        setState({ loading: false, error: null, data: body });
        if (!pending.current && settled.current === marksAtStart) setReviewed(body.reviewed || {});
      } catch (e) {
        if (alive) setState((s) => ({ loading: false, error: String(e), data: s.data }));
      }
    })();
    return () => { alive = false; };
  }, [repo, branch, nonce, sig]);

  const onPosted = () => { setNonce((n) => n + 1); refreshReviews(); };

  async function onToggleReviewed(stop, on) {
    const s = stopSig(stop);
    const before = reviewed[stop.title] ?? null;
    setReviewed((r) => withMark(r, stop.title, on ? s : null));
    setToggleErrors((e) => ({ ...e, [stop.title]: null }));
    pending.current++;
    try {
      const r = await postJson("/api/reviews/reviewed", { repo, branch, stop: stop.title, sig: s, reviewed: on });
      if (!r.ok) throw new Error(r.data.detail || `HTTP ${r.status}`);
    } catch (e) {
      setReviewed((r) => withMark(r, stop.title, before));
      setToggleErrors((x) => ({ ...x, [stop.title]: `not saved: ${e.message || e}` }));
    } finally {
      pending.current--;
      settled.current++;
    }
  }

  if (state.loading) return <div class="wt-empty">loading…</div>;
  if (!state.data) return <div class="wt-empty">{state.error}</div>;
  return (
    <WalkthroughView data={state.data} repo={repo} branch={branch} pr={pr} hl={hl} reviewed={reviewed}
      toggleErrors={toggleErrors} error={state.error} onToggleReviewed={onToggleReviewed} onPosted={onPosted} />
  );
}
