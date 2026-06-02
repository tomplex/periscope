// Rich per-pane sidebar — Linked (PR + Linear cards), Notes (editable), and
// Activity (merged timeline). Shared by the modal sidebar (#modal-side) and
// the split-view detail sidebar (#detail-side); both feed it a /api/pane
// payload and pass a container id/class plus an `idPrefix` so the notes/tag
// inputs get unique DOM ids (modal-notes/modal-tags/modal-tag-input vs
// detail-notes/detail-tags/detail-tag-input).
//
// Conventions preserved verbatim from the modal port:
//  - PR/Linear "+ link …" buttons POST /api/channel/push with a canned prompt
//    that asks Claude to call its link_pr / link_linear MCP tool.
//  - NotesEditor inputs are UNCONTROLLED (defaultValue + ref) so a 1.5s poll
//    re-render never clobbers the user's in-flight keystrokes. The editor is
//    keyed on `pid` at the call site so switching panes resets it.
//  - The activity-stream scrollTop is restored across data-driven re-renders.
//  - When data.channel_unread > 0 and we're rendering data.pane_id, fire a
//    /api/channel/clear-unread POST (the badge clears as soon as the user
//    looks at the pane in either modal or split-view).
import { useRef, useEffect } from "preact/hooks";
import { relTime, prUrl } from "../util.js";
import * as prefs from "../prefs.js";
import { paneTranscript, transcriptSeen, previewPath } from "../store.js";
import { filesTouched } from "../split/filesTouched.js";

function alertDotColor(kind) {
  if (kind === "need_human") return "var(--s-danger)";
  if (kind === "done") return "var(--s-success)";
  return "var(--fg-3)";
}

function timelineColor(kind, evState) {
  if (kind === "commit") return "var(--s-shell)";
  if (kind === "ci") {
    if (evState === "failed") return "var(--s-danger)";
    if (evState === "running") return "var(--s-working)";
    return "var(--s-success)";
  }
  if (kind === "open") return "var(--fg-3)";
  if (kind === "reset") return "var(--s-working)";
  if (kind === "milestone") return "var(--s-success)";
  return "var(--fg-3)";
}

function timelineLabel(kind, evState) {
  if (kind === "commit") return "commit";
  if (kind === "ci") return evState ? `ci ${evState}` : "ci";
  if (kind === "open") return "opened";
  if (kind === "reset") return evState === "compacted" ? "compacted" : "cleared";
  if (kind === "milestone") return "milestone";
  return kind;
}

function avatarChars(handle) {
  if (!handle) return "?";
  const letters = handle.replace(/[^A-Za-z0-9]/g, "");
  return (letters.slice(0, 2) || handle.slice(0, 1)).toUpperCase();
}

// Canned prompts for the "+ link …" buttons (ping Claude's MCP tool).
const LINK_ASK_PROMPTS = {
  pr: "Please link the PR you're working on for this pane using the `link_pr` MCP tool. If there isn't one, ignore this.",
  linear: "Please link the relevant Linear ticket for this pane using the `link_linear` MCP tool. If there isn't one, ignore this.",
};

function PrCard({ data, onLinkAsk }) {
  if (!data.pr) {
    const attached = !!data.channel_attached;
    const title = attached
      ? "Ask Claude to link the PR for this pane (via link_pr MCP tool)"
      : "Channel not attached. Respawn Claude via + claude.";
    return (
      <button
        class="modal-side-link-btn"
        type="button"
        disabled={!attached}
        title={title}
        onClick={() => onLinkAsk("pr")}
      >
        + link pull request
      </button>
    );
  }
  const ciState = data.ci === "✓" ? "passing" : data.ci === "✗" ? "failing" : data.ci === "⟳" ? "running" : "—";
  const ciClass = data.ci === "✓" ? "ci-passing" : data.ci === "✗" ? "ci-failing" : data.ci === "⟳" ? "ci-running" : "";
  const url = prUrl(data.repo_slug, data.pr);
  const adds = data.pr_additions || 0;
  const dels = data.pr_deletions || 0;
  return (
    <div class="modal-card-inset">
      <div class="pr-head">
        {url ? (
          <a class="pr-num" href={url} target="_blank" rel="noopener" onClick={(e) => e.stopPropagation()}>
            #{data.pr}
          </a>
        ) : (
          <span class="pr-num">#{data.pr}</span>
        )}
        <span class="pr-title" title={data.pr_title || ""}>{data.pr_title || ""}</span>
      </div>
      <div class="pr-meta">
        {data.pr_draft ? (
          <span class="pr-mini pr-mini-draft">draft</span>
        ) : (
          <span class="pr-mini pr-mini-open">open</span>
        )}
        <span class="pr-diff">
          <span class="diff-plus">+{adds}</span> <span class="diff-minus">−{dels}</span>
        </span>
        <span class={`pr-ci ${ciClass}`}>
          <span class="ci-dot"></span>ci {ciState}
        </span>
        {(data.pr_reviewers || []).length > 0 && (
          <span class="pr-reviewers">
            {(data.pr_reviewers || []).map((r) => (
              <span class="modal-avatar" title={r}>{avatarChars(r)}</span>
            ))}
          </span>
        )}
      </div>
    </div>
  );
}

function LinearCard({ data, onLinkAsk }) {
  if (data.linked_linear) {
    const id = data.linked_linear;
    const url = `https://linear.app/issue/${id}`;
    const title = data.linked_linear_title || "linear ticket";
    return (
      <div class="modal-card-inset">
        <div class="pr-head">
          <a
            class="pr-num"
            href={url}
            target="_blank"
            rel="noopener"
            onClick={(e) => e.stopPropagation()}
            title={`Linear ticket ${id} (linked by claude)`}
          >
            {id}
          </a>
          <span class="pr-title" title={title}>{title}</span>
        </div>
        {data.linked_linear_status && (
          <div class="pr-meta">
            <span class="pr-mini">{data.linked_linear_status}</span>
          </div>
        )}
      </div>
    );
  }
  const attached = !!data.channel_attached;
  const title = attached
    ? "Ask Claude to link a Linear ticket for this pane (via link_linear MCP tool)"
    : "Channel not attached. Respawn Claude via + claude.";
  return (
    <button
      class="modal-side-link-btn"
      type="button"
      disabled={!attached}
      title={title}
      onClick={() => onLinkAsk("linear")}
    >
      + link Linear ticket
    </button>
  );
}

function ActivityRow({ e }) {
  if (e.src === "alert") {
    return (
      <li class="timeline-row timeline-row-alert" data-kind={e.kind}>
        <span class="timeline-dot" style={`background:${alertDotColor(e.kind)}`}></span>
        <div class="timeline-body">
          <div class="timeline-text">{e.text}</div>
          <div class="timeline-when">claude · {e.kind} · {relTime(e.at)} ago</div>
        </div>
      </li>
    );
  }
  return (
    <li class="timeline-row" data-kind={e.kind}>
      <span class="timeline-dot" style={`background:${timelineColor(e.kind, e.state)}`}></span>
      <div class="timeline-body">
        {e.url ? (
          <a class="timeline-text timeline-link" href={e.url} target="_blank" rel="noopener">{e.text}</a>
        ) : (
          <div class="timeline-text">{e.text}</div>
        )}
        <div class="timeline-when">{timelineLabel(e.kind, e.state)} · {relTime(e.at)} ago</div>
      </div>
    </li>
  );
}

function ActivitySection({ data, streamRef }) {
  const stream = data.activity || [];
  const latestAlert = stream
    .filter((e) => e.src === "alert")
    .reduce((best, a) => (best && best.at >= a.at ? best : a), null);
  const pinned = latestAlert && latestAlert.kind === "need_human" ? latestAlert : null;
  const rest = stream.filter((e) => e !== pinned);
  return (
    <>
      {pinned && (
        <div class="activity-pinned">
          <div class="activity-pinned-label">needs you · {relTime(pinned.at)} ago</div>
          <div class="activity-pinned-text">{pinned.text}</div>
        </div>
      )}
      {rest.length ? (
        <ol class="timeline activity-stream" ref={streamRef}>
          {rest.map((e, i) => (
            <ActivityRow key={i} e={e} />
          ))}
        </ol>
      ) : !pinned ? (
        <div class="timeline-empty">no recent activity</div>
      ) : null}
    </>
  );
}

// Uncontrolled (defaultValue + ref) so the 1.5s poll's re-render never resets
// in-flight typing. Keyed by pid at the call site → switching panes remounts.
// `idPrefix` produces unique DOM ids ("modal" or "detail"); the CSS keys on
// class names, not ids, so styling works in either container.
function NotesEditor({ pid, onRefresh, idPrefix }) {
  const taRef = useRef(null);
  const tiRef = useRef(null);
  const notesTimer = useRef(null);
  const ann = pid ? prefs.getAnnotation(pid) : null;
  const tags = ann?.tags || [];

  function flushNotes() {
    if (!pid) return;
    const cur = prefs.getAnnotation(pid) || { notes: "", tags: [] };
    prefs.setAnnotation(pid, { notes: taRef.current.value, tags: cur.tags });
  }

  function submitTag() {
    const raw = (tiRef.current.value || "").trim();
    if (!raw || !pid) return;
    const cur = prefs.getAnnotation(pid) || { notes: taRef.current.value, tags: [] };
    const parts = raw.split(/[\s,]+/).filter(Boolean);
    prefs.setAnnotation(pid, { notes: taRef.current.value, tags: [...cur.tags, ...parts] });
    tiRef.current.value = "";
    onRefresh?.();
  }

  function removeTag(i) {
    const cur = prefs.getAnnotation(pid) || { notes: taRef.current.value, tags: [] };
    prefs.setAnnotation(pid, { notes: taRef.current.value, tags: cur.tags.filter((_, idx) => idx !== i) });
    onRefresh?.();
  }

  return (
    <>
      <textarea
        ref={taRef}
        id={`${idPrefix}-notes`}
        class="modal-notes"
        placeholder={pid ? "Notes — saves on blur" : "Notes unavailable (no pid)"}
        disabled={!pid}
        defaultValue={ann?.notes || ""}
        onInput={() => {
          clearTimeout(notesTimer.current);
          notesTimer.current = setTimeout(flushNotes, 600);
        }}
        onBlur={() => {
          clearTimeout(notesTimer.current);
          flushNotes();
        }}
        onKeyDown={(e) => e.stopPropagation()}
      />
      <div class="tag-row">
        <div class="tag-chips" id={`${idPrefix}-tags`}>
          {tags.map((t, i) => (
            <span class="tag-chip" data-tag-i={i}>
              {t}
              <button class="tag-chip-x" title="remove" onClick={() => removeTag(i)}>×</button>
            </span>
          ))}
        </div>
        <input
          ref={tiRef}
          id={`${idPrefix}-tag-input`}
          class="modal-tag-input"
          type="text"
          placeholder="add tag, Enter or comma"
          disabled={!pid}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              submitTag();
            }
          }}
        />
      </div>
    </>
  );
}

function FilesSection({ pid }) {
  if (!pid || !transcriptSeen.value[pid]) return null;
  const entry = paneTranscript.value[pid];
  if (!entry || !entry.messages) return null;
  const items = filesTouched(entry.messages);
  if (!items.length) return null;
  return (
    <section class="modal-side-section modal-side-files">
      <h4>Files</h4>
      <ul class="files-list">
        {items.map((it) => (
          <li
            key={it.path}
            class="files-row"
            onClick={() => { previewPath.value = { path: it.path, line: null }; }}
            title={`Open ${it.path} in preview overlay`}
          >
            <span class="files-op">{opGlyph(it.op)}</span>
            <span class="files-path">{it.path}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function opGlyph(op) {
  switch (op) {
    case "Read": return "👁";
    case "Write": return "+";
    case "Edit":
    case "MultiEdit":
    case "NotebookEdit": return "✎";
    default: return "·";
  }
}

// Top-level wrapper. `containerId` / `containerClass` pick the host element so
// the modal can render `#modal-side.modal-side` and the detail pane can render
// `#detail-side.detail-side`. The activity-stream scrollTop is captured during
// render and restored after each commit so the timeline doesn't snap to top
// every 1.5s.
export function Sidebar({ data, onRefresh, containerId, containerClass, idPrefix }) {
  const streamRef = useRef(null);
  const priorScroll = useRef(0);
  const pid = data?.pid || "";

  useEffect(() => {
    if (streamRef.current && priorScroll.current) {
      streamRef.current.scrollTop = priorScroll.current;
    }
  });

  // Clear unread when the sidebar shows replies (fire-and-forget).
  useEffect(() => {
    if (data?.pane_id && (data.channel_unread || 0) > 0) {
      fetch(`/api/channel/clear-unread?pane=${encodeURIComponent(data.pane_id)}`, { method: "POST" });
    }
  }, [data?.pane_id, data?.channel_unread]);

  function onLinkAsk(kind) {
    const content = LINK_ASK_PROMPTS[kind];
    if (!content || !data?.pane_id) return;
    fetch(`/api/channel/push?pane=${encodeURIComponent(data.pane_id)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ content }),
    }).catch(() => {});
  }

  // Stash the activity scrollTop just before render commits (so the post-render
  // effect above can restore it). Reading during render is the cheapest
  // equivalent of the vanilla pre-rebuild stash.
  if (streamRef.current) priorScroll.current = streamRef.current.scrollTop;

  return (
    <aside id={containerId} class={containerClass}>
      <section class="modal-side-section">
        <h4>Linked</h4>
        <PrCard data={data} onLinkAsk={onLinkAsk} />
        <LinearCard data={data} onLinkAsk={onLinkAsk} />
      </section>
      <section class="modal-side-section modal-side-notes">
        <h4>Notes</h4>
        <NotesEditor key={pid} pid={pid} idPrefix={idPrefix} onRefresh={onRefresh} />
      </section>
      <FilesSection pid={pid} />
      <section class="modal-side-section modal-side-activity">
        <h4>Activity</h4>
        <ActivitySection data={data} streamRef={streamRef} />
      </section>
    </aside>
  );
}
