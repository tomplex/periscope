// Structured Claude-turn transcript for the split-view detail pane. Polls
// /api/pane/turns for the selected pane (full message list per poll; reconcile
// by uuid) and renders it like a richer version of Claude Code's own terminal
// output: markdown-rendered prose, tool calls as `⏺ Name(arg)` rows with
// collapsible `⎿` output, Edit diffs. No xterm/emulation — JSONL is already
// structured. See the segmented-transcript design spec.
import { useEffect, useState, useRef } from "preact/hooks";
import { transcriptSeen, paneTranscript, previewPath } from "../store.js";
import { targetQuery, apiCall } from "../util.js";
import { renderMarkdown } from "./markdown.jsx";

const TURNS_POLL_MS = 2000;

// Poll /api/pane/turns while THIS pane is the current selection. Writes
// the response to the shared `paneTranscript` signal (one entry per pid)
// so both TranscriptView (rendered messages) and Sidebar's Files section
// (selected pane's messages) read from the same store. Also flips
// transcriptSeen[pid] on first non-empty response — load-bearing for the
// auto-promote toggle (see computeMode in Detail.jsx). Eviction lives in
// Detail.jsx's openedTr pruning path.
function useTranscriptPoll(target, pid, selected) {
  useEffect(() => {
    if (!selected || !target) return;
    let alive = true;
    let timer = null;
    async function tick() {
      try {
        const res = await fetch(`/api/pane/turns?${targetQuery(target)}`);
        const data = await res.json();
        if (!alive) return;
        const msgs = data && data.turns === null ? [] : (data.messages || []);
        const sessionId = data?.session_id || null;
        paneTranscript.value = {
          ...paneTranscript.value,
          [pid]: { messages: msgs, sessionId },
        };
        if (msgs.length && !transcriptSeen.value[pid]) {
          transcriptSeen.value = { ...transcriptSeen.value, [pid]: true };
        }
      } catch (_) {
        /* transient; the next tick retries */
      }
      if (alive) timer = setTimeout(tick, TURNS_POLL_MS);
    }
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [target, pid, selected]);
  // No return — consumers read from the signal directly.
}

// A short human-readable arg for a tool call, by tool name — the bit Claude's
// terminal shows in parens. Falls back to a compact JSON of the input.
function toolArg(t) {
  const inp = t.input || {};
  switch (t.name) {
    case "Bash": return inp.command || "";
    case "Read":
    case "Edit":
    case "Write":
    case "NotebookEdit": return inp.file_path || inp.notebook_path || "";
    case "Grep": return inp.pattern || "";
    case "Glob": return inp.pattern || "";
    case "Task":
    case "Agent": return inp.description || "";   // subagent_type rendered as a chip
    case "TaskCreate": return inp.subject || "";
    case "TaskUpdate": return inp.taskId ? `#${inp.taskId}` : "";
    case "WebFetch": return inp.url || "";
    case "WebSearch": return inp.query || "";
    case "Skill": return inp.skill || inp.command || "";
    default: {
      const s = JSON.stringify(inp);
      if (s === "{}" || s === "null") return "";
      return s.length > 120 ? s.slice(0, 120) + "…" : s;
    }
  }
}

function EditDiff({ input }) {
  const oldL = String(input.old_string || "").split("\n");
  const newL = String(input.new_string || "").split("\n");
  return (
    <div class="tc-diff">
      {oldL.map((l, i) => <div class="tc-diff-del" key={`d${i}`}>{l || " "}</div>)}
      {newL.map((l, i) => <div class="tc-diff-add" key={`a${i}`}>{l || " "}</div>)}
    </div>
  );
}

// The full input worth showing verbatim on expand (preserving newlines/&&),
// distinct from the one-line header preview. Mostly the Bash command; for a
// Write it's the file content. Other tools' inputs are fully shown in the header.
function fullInput(t) {
  const inp = t.input || {};
  switch (t.name) {
    case "Bash": return inp.command || "";
    case "Write": return inp.content || "";
    default: return "";
  }
}

function ToolCall({ t }) {
  const [open, setOpen] = useState(false);
  const running = t.result == null;
  const isEdit = t.name === "Edit";
  const isAgent = t.name === "Agent" || t.name === "Task";
  const detail = fullInput(t);
  const hasResult = !running && t.result != null && t.result !== "";
  // Expandable if there's a full command/content, an Edit diff, or output.
  const expandable = isEdit || !!detail || hasResult;
  const arg = toolArg(t);
  const subtype = isAgent ? (t.input?.subagent_type || "") : "";
  return (
    <div class={`tc${isAgent ? " tc-agent" : ""}`}>
      <button
        class={`tc-head${expandable ? " expandable" : ""}`}
        onClick={expandable ? () => setOpen(!open) : undefined}
      >
        <span class="tc-dot">{isAgent ? "⚇" : "⏺"}</span>
        <span class="tc-name">{t.name}</span>
        {subtype && <span class="tc-agent-type">{subtype}</span>}
        {arg && (() => {
          const isFileTool =
            t.name === "Read" || t.name === "Edit" || t.name === "Write" ||
            t.name === "MultiEdit" || t.name === "NotebookEdit";
          if (!isFileTool) {
            return <span class="tc-arg">{arg}</span>;
          }
          return (
            <span
              class="tc-arg tc-arg-clickable"
              title="Open preview"
              onClick={(e) => {
                e.stopPropagation();
                previewPath.value = { path: arg, line: null };
              }}
            >{arg}</span>
          );
        })()}
        {running && <span class="tc-running">running…</span>}
        {expandable && <span class="tc-caret">{open ? "▾" : "▸"}</span>}
      </button>
      {open && (
        <div class="tc-detail">
          {isEdit
            ? <EditDiff input={t.input || {}} />
            : detail && <pre class="tc-cmd">{detail}</pre>}
          {hasResult && <pre class="tc-out">{t.result}</pre>}
        </div>
      )}
    </div>
  );
}

function Turn({ m }) {
  if (m.role === "user") {
    return (
      <div class="turn turn-user">
        <span class="turn-user-mark">›</span>
        <div class="turn-user-body">{renderMarkdown(m.text)}</div>
      </div>
    );
  }
  const tools = m.tool_uses || [];
  return (
    <div class="turn turn-assistant">
      {m.text && <div class="turn-prose">{renderMarkdown(m.text)}</div>}
      {tools.length > 0 && (
        <div class="turn-tools">
          {tools.map((t, i) => <ToolCall key={t.id || i} t={t} />)}
        </div>
      )}
    </div>
  );
}

// Bottom composer — the only way to type to Claude in transcript mode (the live
// terminal is hidden). Sends multi-line text via /api/send's paste-buffer path
// then Enter; the new turn shows up on the next poll. Enter submits,
// Shift+Enter inserts a newline (chat convention, matches Claude's own input).
function Composer({ target, composerRef }) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const inputRef = useRef(null);

  async function send() {
    const body = text;
    if (!body.trim() || sending) return;
    setSending(true);
    // Paste first, THEN submit as a separate Enter after a beat — a long paste
    // can still be rendering when a same-call Enter lands, leaving the message
    // staged-but-unsent in Claude's input. The delay lets the paste settle.
    const pasted = await apiCall("send", `/api/send?${targetQuery(target)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paste: body }),
    });
    if (pasted) {
      await new Promise((r) => setTimeout(r, 250));
      await apiCall("send", `/api/send?${targetQuery(target)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: ["Enter"] }),
      });
      setText("");
      if (inputRef.current) inputRef.current.style.height = "";  // back to CSS min-height
    }
    setSending(false);
  }

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  function onInput(e) {
    const el = e.currentTarget;
    setText(el.value);
    // Collapse to 0 before measuring so the box tracks content both ways
    // (grows and shrinks); cap matches CSS max-height.
    el.style.height = "0px";
    el.style.height = Math.min(el.scrollHeight, 140) + "px";
  }

  // Paste a screenshot: upload it (deliver=false so it lands in THIS message,
  // not the live pane) and splice the resulting @path into the box. Claude
  // resolves @-paths on submit, same as the terminal's paste path.
  async function onPaste(e) {
    const items = e.clipboardData?.items || [];
    for (const item of items) {
      if (item.kind !== "file" || !item.type.startsWith("image/")) continue;
      const blob = item.getAsFile();
      if (!blob) continue;
      e.preventDefault();
      const res = await fetch(`/api/paste-image?${targetQuery(target)}&deliver=false`, {
        method: "POST",
        headers: { "Content-Type": blob.type || "image/png" },
        body: blob,
      });
      const d = await res.json().catch(() => ({}));
      if (d.ok) setText((prev) => `${prev}${prev && !prev.endsWith(" ") ? " " : ""}@${d.path} `);
      return;
    }
  }

  return (
    <div class="transcript-composer" ref={composerRef}>
      <textarea
        ref={inputRef}
        class="transcript-composer-input"
        placeholder="Message Claude…  (Enter to send · Shift+Enter for newline · paste screenshots)"
        value={text}
        rows={1}
        onInput={onInput}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
      />
    </div>
  );
}

export function TranscriptView({ target, pid, selected }) {
  useTranscriptPoll(target, pid, selected);
  const messages = paneTranscript.value[pid]?.messages || [];
  const scrollRef = useRef(null);
  const composerRef = useRef(null);
  // Follow the conversation: open at the bottom and stay pinned as new turns
  // arrive — UNLESS the user has scrolled up to read, then leave them be.
  const stick = useRef(true);
  const lastKey = useRef("");

  const pin = () => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  };

  // Pin ONLY when the content actually changed — not on every 2s poll (the
  // message list is replaced each poll even when identical). Re-pinning on an
  // unchanged poll is what yanked the view to the bottom while you were typing.
  useEffect(() => {
    const last = messages[messages.length - 1];
    const key = `${messages.length}:${last ? last.uuid : ""}:${last ? (last.tool_uses || []).length : 0}`;
    if (key !== lastKey.current) {
      lastKey.current = key;
      pin();
    }
  }, [messages]);

  // The composer overlays the bottom of the (full-height) transcript. Keep the
  // transcript's bottom padding equal to the composer's height so the last
  // message always clears the box, and re-pin if we're stuck — so growing the
  // composer just reflows padding instead of resizing the scroll viewport (no
  // bounce). rAF: measure after layout settles.
  useEffect(() => {
    const box = composerRef.current;
    const scroll = scrollRef.current;
    if (!box || !scroll) return;
    const sync = () => {
      scroll.style.paddingBottom = box.offsetHeight + 14 + "px";
      requestAnimationFrame(pin);
    };
    sync();
    const ro = new ResizeObserver(sync);
    ro.observe(box);
    return () => ro.disconnect();
  }, []);

  function onScroll(e) {
    const el = e.currentTarget;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  return (
    <>
      <div class="transcript" ref={scrollRef} onScroll={onScroll}>
        {messages.length === 0
          ? <div class="transcript-empty">No transcript yet.</div>
          : messages.map((m) =>
              m.role === "system" && m.kind === "compact"
                ? <div key={m.uuid} class="transcript-compact"><span>context compacted</span></div>
                : <Turn key={m.uuid} m={m} />
            )}
      </div>
      <Composer target={target} composerRef={composerRef} />
    </>
  );
}
