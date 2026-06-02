// Structured Claude-turn transcript for the split-view detail pane. Polls
// /api/pane/turns for the selected pane (full message list per poll; reconcile
// by uuid), renders turn segments with expandable tool calls. No xterm/emulation
// — JSONL is already structured. See the segmented-transcript design spec.
import { useEffect, useState } from "preact/hooks";
import { transcriptSeen } from "../store.js";
import { targetQuery, relTime } from "../util.js";

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

function toolSummary(t) {
  const inp = t.input || {};
  switch (t.name) {
    case "Bash": return inp.command || "";
    case "Read":
    case "Edit":
    case "Write": return inp.file_path || "";
    default: return JSON.stringify(inp).slice(0, 200);
  }
}

function ToolCall({ t }) {
  const [open, setOpen] = useState(false);
  const running = t.result == null;
  return (
    <div class="toolcall">
      <button class="toolcall-head" onClick={() => setOpen(!open)}>
        <span class="toolcall-name">{t.name}</span>
        <span class="toolcall-summary">{toolSummary(t)}</span>
        {running && <span class="toolcall-running">running…</span>}
      </button>
      {open && t.name === "Edit" && (
        <pre class="toolcall-diff">{`- ${t.input?.old_string || ""}\n+ ${t.input?.new_string || ""}`}</pre>
      )}
      {open && !running && t.name !== "Edit" && (
        <pre class="toolcall-output">{t.result}</pre>
      )}
    </div>
  );
}

function TurnSegment({ m }) {
  const [open, setOpen] = useState(false);
  const preview = (m.text || "").split("\n").find((l) => l.trim()) || "";
  return (
    <div class={`turn turn-${m.role}`}>
      <button class="turn-head" onClick={() => setOpen(!open)}>
        <span class="turn-role">{m.role}</span>
        <span class="turn-time">{relTime(Math.floor((m.ts_ms || 0) / 1000))}</span>
        <span class="turn-preview">{preview.slice(0, 140)}</span>
      </button>
      {open && (
        <div class="turn-body">
          {m.text && <div class="turn-text">{m.text}</div>}
          {(m.tool_uses || []).map((t, i) => <ToolCall key={t.id || i} t={t} />)}
        </div>
      )}
    </div>
  );
}

export function TranscriptView({ target, pid, selected }) {
  const messages = useTranscriptPoll(target, pid, selected);
  if (!messages.length) {
    return <div class="transcript transcript-empty">No transcript yet.</div>;
  }
  return (
    <div class="transcript">
      {messages.map((m) =>
        m.role === "system" && m.kind === "compact"
          ? <hr key={m.uuid} class="transcript-compact" />
          : <TurnSegment key={m.uuid} m={m} />
      )}
    </div>
  );
}
