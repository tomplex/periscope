// The grid surface: session groups of cards + the "+ new" tile, the single
// /api/state poll loop, drag-reorder (two MIME types), and the session-header
// actions (collapse, rename, adopt, auto-rename, kill, project menu). Ported
// from grid.js:renderGrid / renderSession / wireGrid + the send-bulk /
// collapse-all wiring that lived in app.js.
//
// CSS contract preserved verbatim: the grid root carries the grid-has-
// attention class; session-group / session-header / session-name / session-
// pill / session-channel-pill / chevron / cards class names are unchanged.
// Keyed by pid (cards) and session (groups) so diffing is real (convention #1).
import { useEffect, useRef, useState } from "preact/hooks";
import { windows, projects, currentFilter, editingTarget, dragState } from "../store.js";
import * as prefs from "../prefs.js";
import { passesFilter } from "../filter.js";
import { relTime, apiCall } from "../util.js";
import { confirmDialog } from "../overlays/Dialog.jsx";
import { startPolling, poll } from "./poll.js";
import { Card } from "./Card.jsx";
import { NewTile } from "./NewTile.jsx";

const CARD_MIME = "application/periscope-card";

// tmux_session → project lookup, rebuilt from the latest /api/state.
function indexProjects(projs) {
  const idx = {};
  for (const p of projs || []) if (p.tmux_session) idx[p.tmux_session] = p;
  return idx;
}

// User-pinned sessions in saved order; fresh (unsaved) sessions float to the
// TOP, newest first by max acted_at. Ported from grid.js:orderedSessions.
function orderedSessions(allSessions, bySession) {
  const saved = prefs.getSessionOrder();
  const ordered = saved.filter((s) => allSessions.includes(s));
  const orderedSet = new Set(ordered);
  const recencyOf = (s) =>
    Math.max(0, ...(bySession.get(s) || []).map((w) => w.acted_at || 0));
  const fresh = allSessions
    .filter((s) => !orderedSet.has(s))
    .sort((a, b) => recencyOf(b) - recencyOf(a));
  return [...fresh, ...ordered];
}

// ── Session channel-alert rollup ────────────────────────────────────────
const _KIND_RANK = { need_human: 3, done: 2, info: 1 };
const _KIND_ICON = { need_human: "⚠", done: "✓", info: "•" };

function sessionChannelAlert(ws) {
  const alerts = ws
    .filter((w) => (w.channel_unread || 0) > 0 && (w.channel_alerts || []).length > 0)
    .map((w) => ({
      kind: w.channel_alerts[w.channel_alerts.length - 1].kind || "info",
      message: w.channel_alerts[w.channel_alerts.length - 1].message || "",
    }));
  if (!alerts.length) return { node: null, kind: null };
  const topKind = alerts.reduce((a, b) =>
    (_KIND_RANK[a.kind] || 0) >= (_KIND_RANK[b.kind] || 0) ? a : b
  ).kind;
  const count = alerts.length;
  const icon = _KIND_ICON[topKind] || "•";
  const top = alerts.find((a) => a.kind === topKind);
  const preview = top ? top.message.slice(0, 60) : "";
  return {
    kind: topKind,
    node: (
      <span
        class={`session-channel-pill session-channel-${topKind}`}
        title={`${count} unread Claude notification(s)`}
      >
        {icon} {preview}
        {count > 1 ? <> <span class="session-channel-more">+{count - 1}</span></> : null}
      </span>
    ),
  };
}

// Session pill: loudest-wins color (needs-input > ci-bad > done > working >
// idle). Ported from grid.js:sessionPill.
function SessionPill({ ws }) {
  const needsInput = ws.filter((w) => w.state === "needs-input").length;
  const done = ws.filter((w) => w.state === "done").length;
  const idle = ws.filter((w) => w.state === "idle").length;
  const working = ws.filter((w) => w.state === "working").length;
  const ciBad = ws.filter((w) => w.ci === "✗").length;
  const parts = [];
  if (needsInput) parts.push(`${needsInput} needs input`);
  if (done) parts.push(`${done} done`);
  if (working) parts.push(`${working} working`);
  if (idle) parts.push(`${idle} idle`);
  if (ciBad) parts.push(`${ciBad} ✗`);
  if (!parts.length) parts.push(`${ws.length}`);
  let cls = "session-pill";
  if (needsInput) cls += " has-needs-input";
  else if (ciBad) cls += " has-ci-bad";
  else if (done) cls += " has-done";
  else if (working) cls += " has-working";
  else if (idle) cls += " has-idle";
  return <span class={cls}>{parts.join(" · ")}</span>;
}

// Editable session/project name. Exposes its begin() opener via registerEdit
// so the project ⋯ menu's "Rename" item can drive it (grid.js had
// startProjectRename do this from the menu and a dblclick path inline). The
// double-fire guard (`committed`) reproduces the vanilla Enter-then-blur
// idempotency; editingTarget pauses the poll while editing.
function EditableSessionName({ project, session, registerEdit }) {
  const [editing, setEditing] = useState(false);
  const ref = useRef(null);
  const committed = useRef(false);
  const displayName = project?.name || session;
  const canRename = !!project && project.pinned_dir !== "__main__";

  function begin() {
    if (!canRename || editingTarget.value) return;
    committed.current = false;
    editingTarget.value = `project:${project.pinned_dir}`;
    setEditing(true);
  }

  useEffect(() => {
    registerEdit?.(begin);
  });

  useEffect(() => {
    if (editing && ref.current) {
      ref.current.focus();
      ref.current.select();
    }
  }, [editing]);

  async function commit(value) {
    if (committed.current) return;
    committed.current = true;
    editingTarget.value = null;
    setEditing(false);
    const newName = (value || "").trim();
    if (!newName || newName === displayName) return;
    await apiCall("rename", "/api/projects/patch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pinned_dir: project.pinned_dir,
        name: newName,
        tmux_session: newName,
      }),
    });
    poll();
  }

  function cancel() {
    if (committed.current) return;
    committed.current = true;
    editingTarget.value = null;
    setEditing(false);
  }

  if (editing) {
    return (
      <input
        ref={ref}
        type="text"
        class="session-name-input"
        value={displayName}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter") { e.preventDefault(); commit(e.currentTarget.value); }
          else if (e.key === "Escape") { e.preventDefault(); cancel(); }
        }}
        onBlur={(e) => commit(e.currentTarget.value)}
      />
    );
  }
  return (
    <h2
      class="session-name"
      onDblClick={canRename ? (e) => { e.stopPropagation(); begin(); } : undefined}
    >
      {displayName}
    </h2>
  );
}

// Project ⋯ menu — fixed-positioned panel anchored under the button, with
// inline two-click archive confirm. Ported from grid.js:handleProjectMenu.
function ProjectMenu({ project, onRename }) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const anchorRef = useRef(null);
  const [pos, setPos] = useState(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e) {
      if (anchorRef.current && anchorRef.current.contains(e.target)) return;
      if (e.target.closest(".project-menu-panel")) return;
      setOpen(false);
      setConfirming(false);
    }
    function onKey(e) {
      if (e.key === "Escape") { setOpen(false); setConfirming(false); }
    }
    // Defer the document listeners one tick so the opening click doesn't
    // immediately fire close.
    const t = setTimeout(() => {
      document.addEventListener("click", onDoc);
      document.addEventListener("keydown", onKey);
    }, 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener("click", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function toggle(e) {
    e.stopPropagation();
    if (!open) {
      const rect = anchorRef.current.getBoundingClientRect();
      setPos({ top: rect.bottom + 2, right: window.innerWidth - rect.right });
    }
    setOpen((v) => !v);
    setConfirming(false);
  }

  async function archive() {
    setOpen(false);
    await apiCall("archive", "/api/projects/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned_dir: project.pinned_dir }),
    });
  }

  return (
    <>
      <button
        ref={anchorRef}
        class="project-menu"
        data-pinned-dir={project.pinned_dir}
        title="project actions"
        onClick={toggle}
      >
        ⋯
      </button>
      {open && (
        <div
          class="project-menu-panel"
          style={`position:fixed;top:${pos.top}px;right:${pos.right}px`}
          onClick={(e) => e.stopPropagation()}
        >
          <button class="project-menu-item" onClick={() => { setOpen(false); onRename(); }}>
            Rename
          </button>
          <button
            class={`project-menu-item${confirming ? " project-menu-item-confirming" : ""}`}
            onClick={() => (confirming ? archive() : setConfirming(true))}
          >
            {confirming ? "Click again to confirm" : "Archive"}
          </button>
        </div>
      )}
    </>
  );
}

// The session header row: chevron + name + meta + pill + alert + action
// buttons. Header buttons stop their own propagation so the row's
// collapse-on-click doesn't fire. The DnD handlers are passed in from <Grid>.
function SessionHeader({ session, ws, total, project, onToggleCollapse, dnd }) {
  const shown = ws.length;
  const meta = shown === total ? `${total} windows` : `${shown}/${total} windows`;
  const recent = Math.max(0, ...ws.map((w) => w.focused_at || 0));
  const recentLabel = recent ? relTime(recent) : "";
  const pinnedDirLabel =
    project && project.pinned_dir && project.pinned_dir !== "__main__"
      ? project.pinned_dir.replace(/^\/Users\/[^/]+/, "~")
      : null;
  const alert = sessionChannelAlert(ws);
  const editOpener = useRef(null);

  async function adopt(e) {
    e.stopPropagation();
    const btn = e.currentTarget;
    btn.disabled = true;
    await apiCall("adopt", "/api/projects/adopt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tmux_session: session }),
    });
    btn.disabled = false;
  }

  async function autoRename(e) {
    e.stopPropagation();
    const btn = e.currentTarget;
    if (btn.dataset.busy) return;
    btn.dataset.busy = "1";
    const orig = btn.innerHTML;
    btn.innerHTML = "✨ thinking…";
    btn.disabled = true;
    try {
      const res = await fetch(
        `/api/auto-rename-session?session=${encodeURIComponent(session)}`,
        { method: "POST" }
      );
      const data = await res.json();
      if (!data.ok) {
        btn.innerHTML = `✗ ${(data.error || "failed").slice(0, 40)}`;
        setTimeout(() => (btn.innerHTML = orig), 4000);
      } else {
        const n = (data.applied || []).length;
        btn.innerHTML = n ? `✓ renamed ${n}` : "✓ all good";
        setTimeout(() => (btn.innerHTML = orig), 2500);
        poll();
      }
    } catch (err) {
      btn.innerHTML = `✗ ${err.message}`.slice(0, 40);
      setTimeout(() => (btn.innerHTML = orig), 4000);
    } finally {
      btn.disabled = false;
      delete btn.dataset.busy;
    }
  }

  async function killSession(e) {
    e.stopPropagation();
    const n = windows.value.filter((w) => w.session === session).length;
    const msg = `Kill session '${session}'?\n\nCloses ${n} window${n === 1 ? "" : "s"} and detaches any attached client.`;
    if (!(await confirmDialog(msg, { okLabel: "Kill", danger: true }))) return;
    await apiCall("kill session", `/api/session?session=${encodeURIComponent(session)}`, {
      method: "DELETE",
    });
    poll();
  }

  return (
    <div
      class="session-header"
      draggable
      data-session={session}
      onClick={onToggleCollapse}
      onDragStart={(e) => dnd.onHeaderDragStart(session, e)}
      onDragOver={(e) => dnd.onGroupDragOver(session, e)}
      onDragLeave={dnd.onHeaderDragLeave}
      onDrop={(e) => dnd.onGroupDrop(session, e)}
    >
      <span class="chevron">▾</span>
      <EditableSessionName
        project={project}
        session={session}
        registerEdit={(fn) => (editOpener.current = fn)}
      />
      {pinnedDirLabel && <span class="session-pinned-dir">{pinnedDirLabel}</span>}
      <span class="session-meta">
        {meta}
        {recentLabel ? ` · ${recentLabel}` : ""}
      </span>
      <SessionPill ws={ws} />
      {alert.node}
      {!project && (
        <button class="adopt" title="register this tmux session as a project" onClick={adopt}>
          + adopt
        </button>
      )}
      {project && project.pinned_dir !== "__main__" && (
        <ProjectMenu project={project} onRename={() => editOpener.current?.()} />
      )}
      <button
        class="auto-rename"
        title="ask Claude to auto-rename windows in this session"
        onClick={autoRename}
      >
        ✨ rename
      </button>
      <button class="kill-session" title="kill this tmux session" onClick={killSession}>
        ✕
      </button>
    </div>
  );
}

// One session group. Sort cards leftmost = most recently opened (acted_at),
// tmux index as the stable tiebreak. Reproduces grid.js's card sort.
function SessionGroup({ session, ws, total, project, collapsed, onToggleCollapse, dnd }) {
  const sorted = ws.slice().sort((a, b) => {
    const da = (b.acted_at || 0) - (a.acted_at || 0);
    if (da !== 0) return da;
    return a.index - b.index;
  });
  const alert = sessionChannelAlert(ws);
  const alertClass = alert.kind ? ` session-has-channel session-has-channel-${alert.kind}` : "";

  return (
    <section class={`session-group${collapsed ? " collapsed" : ""}${alertClass}`} data-session={session}>
      <SessionHeader
        session={session}
        ws={ws}
        total={total}
        project={project}
        onToggleCollapse={onToggleCollapse}
        dnd={dnd}
      />
      <div class="cards">
        {sorted.map((w) => (
          <Card key={w.pid} w={w} />
        ))}
        <NewTile session={session} project={project} />
      </div>
    </section>
  );
}

export function Grid() {
  // Own the single poll loop for this surface.
  useEffect(() => startPolling(), []);

  const ws = windows.value;
  const projs = projects.value;
  const filter = currentFilter.value;
  const projectsByTmux = indexProjects(projs);
  const collapsed = prefs.getCollapsed();

  const filtered = ws.filter((w) => passesFilter(w, filter));
  const bySession = new Map();
  for (const w of filtered) {
    if (!bySession.has(w.session)) bySession.set(w.session, []);
    bySession.get(w.session).push(w);
  }
  const totals = new Map();
  for (const w of ws) totals.set(w.session, (totals.get(w.session) || 0) + 1);
  const order = orderedSessions([...bySession.keys()], bySession);

  // Keep the live rendered order in a ref so a reorder splice matches what the
  // user sees (mirrors grid.js building the order from the DOM).
  const orderRef = useRef(order);
  orderRef.current = order;

  // grid-has-attention fade — same gate as the per-card needsAttention.
  const anyAttention = ws.some(
    (w) =>
      (w.channel_alerts || []).some((r) => r.kind === "need_human") &&
      (w.channel_unread || 0) > 0
  );

  // Reflect the external send-bulk + collapse-all buttons (they live in the
  // header — owned by chrome — but are grid-driven). Imperative because the
  // header may be vanilla or Preact; either way these IDs exist.
  useEffect(() => {
    const sendBulk = document.getElementById("send-bulk");
    const toggleAll = document.getElementById("toggle-all");
    const visible = ws.filter((w) => passesFilter(w, filter));
    if (sendBulk) {
      if (visible.length > 1) {
        sendBulk.hidden = false;
        sendBulk.textContent = `→ send to ${visible.length}`;
      } else {
        sendBulk.hidden = true;
      }
    }
    if (toggleAll) {
      if (order.length === 0) {
        toggleAll.hidden = true;
      } else {
        toggleAll.hidden = false;
        const allCollapsed = order.every((s) => collapsed.has(s));
        toggleAll.textContent = allCollapsed ? "▸ expand all" : "▾ collapse all";
      }
    }
  });

  // collapse-all click handler (wired once; reads the live order via the ref).
  useEffect(() => {
    const toggleAll = document.getElementById("toggle-all");
    if (!toggleAll) return;
    function onClick() {
      const visible = orderRef.current;
      if (visible.length === 0) return;
      const cur = prefs.getCollapsed();
      const allCollapsed = visible.every((s) => cur.has(s));
      if (allCollapsed) for (const s of visible) cur.delete(s);
      else for (const s of visible) cur.add(s);
      prefs.setCollapsed(cur);
    }
    toggleAll.addEventListener("click", onClick);
    return () => toggleAll.removeEventListener("click", onClick);
  }, []);

  function toggleCollapse(session, e) {
    // Header click toggles collapse, unless the click landed on a button/input
    // (which carry their own handlers + stopPropagation).
    if (e.target.closest("button") || e.target.closest("input")) return;
    const cur = prefs.getCollapsed();
    if (cur.has(session)) cur.delete(session);
    else cur.add(session);
    prefs.setCollapsed(cur);
  }

  // ── Drag-and-drop. Two drags via MIME type (coupling #6):
  //   text/plain                  → session header reorder
  //   application/periscope-card  → card move into another session
  // Card drags omit text/plain so the reorder branch ignores them. Identity
  // travels on the dataTransfer payload — no DOM-sibling walks.
  function clearDragArtifacts() {
    document
      .querySelectorAll(".dragging, .drag-over-top, .drag-over-bottom, .card-drop-target")
      .forEach((el) =>
        el.classList.remove("dragging", "drag-over-top", "drag-over-bottom", "card-drop-target")
      );
  }

  function onHeaderDragStart(session, e) {
    e.currentTarget.classList.add("dragging");
    // Pause polling: a commit mid-drag would re-render and drop the source.
    dragState.value = { kind: "session", session };
    e.dataTransfer.setData("text/plain", session);
    e.dataTransfer.effectAllowed = "move";
  }

  function onGroupDragOver(session, e) {
    if (e.dataTransfer.types.includes(CARD_MIME)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      // single active card-drop highlight
      document.querySelectorAll(".card-drop-target").forEach((g) => {
        if (g.dataset.session !== session) g.classList.remove("card-drop-target");
      });
      e.currentTarget.closest(".session-group")?.classList.add("card-drop-target");
      return;
    }
    const header = e.currentTarget;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const rect = header.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    header.classList.toggle("drag-over-top", before);
    header.classList.toggle("drag-over-bottom", !before);
  }

  function onHeaderDragLeave(e) {
    e.currentTarget.classList.remove("drag-over-top", "drag-over-bottom");
  }

  function onGroupDrop(session, e) {
    // Clear the pause flag here too: the drop re-renders, which detaches the
    // dragged element — a subsequent dragend isn't guaranteed.
    dragState.value = null;
    if (e.dataTransfer.types.includes(CARD_MIME)) {
      e.preventDefault();
      const src = e.dataTransfer.getData(CARD_MIME);
      moveCard(src, session);
      clearDragArtifacts();
      return;
    }
    const header = e.currentTarget;
    e.preventDefault();
    const src = e.dataTransfer.getData("text/plain");
    if (src === session) {
      clearDragArtifacts();
      return;
    }
    const rect = header.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    reorderSessions(src, session, before);
    clearDragArtifacts();
  }

  function onDragEnd() {
    dragState.value = null;
    clearDragArtifacts();
  }

  function reorderSessions(src, dst, before) {
    const all = orderRef.current;
    const without = all.filter((s) => s !== src);
    const dstIdx = without.indexOf(dst);
    const insertAt = before ? dstIdx : dstIdx + 1;
    without.splice(insertAt, 0, src);
    prefs.setSessionOrder(without);
  }

  async function moveCard(target, dest) {
    // target = "session:index"; split on the LAST colon (convention #5).
    const i = target.lastIndexOf(":");
    if (i < 0) return;
    const session = target.slice(0, i);
    const index = target.slice(i + 1);
    if (session === dest) return;
    const params = new URLSearchParams({ session, index, dest });
    const data = await apiCall("move window", `/api/window/move?${params.toString()}`, {
      method: "POST",
    });
    if (data) poll();
  }

  const dnd = { onHeaderDragStart, onGroupDragOver, onHeaderDragLeave, onGroupDrop };

  if (filtered.length === 0) {
    return (
      <main class="grid" onDragEnd={onDragEnd}>
        <div class="empty-state">no windows match the current filter</div>
      </main>
    );
  }

  return (
    <main class={anyAttention ? "grid grid-has-attention" : "grid"} onDragEnd={onDragEnd}>
      {order.map((session) => (
        <SessionGroup
          key={session}
          session={session}
          ws={bySession.get(session)}
          total={totals.get(session)}
          project={projectsByTmux[session] || null}
          collapsed={collapsed.has(session)}
          onToggleCollapse={(e) => toggleCollapse(session, e)}
          dnd={dnd}
        />
      ))}
    </main>
  );
}
