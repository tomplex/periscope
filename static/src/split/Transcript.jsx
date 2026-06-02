// Structured Claude-turn transcript for the split-view detail pane. Polls
// /api/pane/turns for the selected pane (full message list per poll; reconcile
// by uuid) and renders it like a richer version of Claude Code's own terminal
// output: markdown-rendered prose, tool calls as `⏺ Name(arg)` rows with
// collapsible `⎿` output, Edit diffs. No xterm/emulation — JSONL is already
// structured. See the segmented-transcript design spec.
import { useEffect, useState, useRef } from "preact/hooks";
import { transcriptSeen } from "../store.js";
import { targetQuery, apiCall } from "../util.js";
import { renderMarkdown } from "./markdown.jsx";

const TURNS_POLL_MS = 2000;

// Poll while this pane is the current selection — in EITHER sub-mode, so a
// Terminal-mode pane still discovers its transcript and auto-promotes. Fires
// once immediately on becoming selected, then every TURNS_POLL_MS. On the first
// non-empty response, flips transcriptSeen[pid] (drives the auto-promote).
function useTranscriptPoll(target, pid, selected) {
  const [messages, setMessages] = useState([]);
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
        setMessages(msgs);                       // full-replace (resume-safe)
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
  return messages;
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

function ToolCall({ t }) {
  const [open, setOpen] = useState(false);
  const running = t.result == null;
  const isEdit = t.name === "Edit";
  const isAgent = t.name === "Agent" || t.name === "Task";
  // Something to expand: an Edit (shows the diff) or a non-empty result.
  const expandable = isEdit || (!running && t.result !== "" && t.result != null);
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
        {arg && <span class="tc-arg">{arg}</span>}
        {running && <span class="tc-running">running…</span>}
        {expandable && <span class="tc-caret">{open ? "▾" : "▸"}</span>}
      </button>
      {open && isEdit && <EditDiff input={t.input || {}} />}
      {open && !isEdit && expandable && (
        <pre class="tc-out"><span class="tc-elbow">⎿ </span>{t.result}</pre>
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
function Composer({ target }) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const inputRef = useRef(null);

  async function send() {
    const body = text;
    if (!body.trim() || sending) return;
    setSending(true);
    const ok = await apiCall("send", `/api/send?${targetQuery(target)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paste: body, keys: ["Enter"] }),
    });
    if (ok) {
      setText("");
      if (inputRef.current) inputRef.current.style.height = "auto";  // snap back from auto-grow
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
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
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
    <div class="transcript-composer">
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
      <button
        class="transcript-composer-send"
        disabled={!text.trim() || sending}
        onClick={send}
      >
        Send
      </button>
    </div>
  );
}

export function TranscriptView({ target, pid, selected }) {
  const messages = useTranscriptPoll(target, pid, selected);
  const scrollRef = useRef(null);
  // Follow the conversation: open at the bottom and stay pinned as new turns
  // arrive — UNLESS the user has scrolled up to read, then leave them be.
  const stick = useRef(true);

  const pin = () => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  };

  useEffect(pin, [messages]);

  // Re-pin when the scroll area resizes — e.g. the composer auto-grows as you
  // type a long message, shrinking the transcript; without this the bottom
  // would slide out of view and snap back on the next poll (the "bounce").
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const ro = new ResizeObserver(pin);
    ro.observe(el);
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
      <Composer target={target} />
    </>
  );
}
