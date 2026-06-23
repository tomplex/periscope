// Left rail of the split view (#rail): the Repo → Project → Pane+review tree
// derived from prefs (curated order) joined with /api/state (live status).
// Membership is session-anchored (railTree.js header has the full contract);
// dev (MAIN_KEY) renders as a flat pane list at the bottom.
//
// Read model:
//   - `windows` signal  → live membership + status (rebuilt each poll)
//   - `projects` signal → project rows (grouping keys + labels + dev target)
//   - prefs.*           → order / collapse / last_selected (persistence boundary)
//   - `railSelection`   → STRING highlight-key the rows compare against
//
// Two deliberately-different selection shapes (do NOT cross them):
//   - PERSISTED  prefs.last_selected is an OBJECT  ({kind:"pane",pid} / {kind:"review",worktree})
//   - HIGHLIGHT  the rows compare against a STRING ("pane:<pid>" / "review:<worktree>")
// railSelection mirrors the string; setLastSelected stores the object. Storing
// the string into the pref (or the object into the signal) silently breaks
// restore + highlight.
//
// Drag identity travels on the drag descriptor captured at dragStart (kind +
// key + repoKey/worktreeKey) — NEVER a previousElementSibling DOM walk, which
// breaks under a component tree (#6). The reorder splices run against
// currentMergedOrder() (the same merged tree the render uses), not raw prefs.
//
// MAIN_KEY (dev) is pinned to the bottom at all five enforcement points: merge
// (railTree), isValidDropTarget, reorderRepos, RepoRow draggable gate
// (RailRows), and syncRailPrefs (which persists panes_by_worktree[MAIN_KEY]
// but keeps MAIN_KEY out of repo_order / worktrees_by_repo).
import { useRef, useState } from "preact/hooks";
import { track } from "../track.js";
import { windows, projects, workspaces, currentFilter, railSelection, dragState } from "../store.js";
import * as prefs from "../prefs.js";
import { passesFilter } from "../filter.js";
import { apiCall, targetQuery, shortestUniqueSuffix } from "../util.js";
import { confirmDialog } from "../overlays/Dialog.jsx";
import {
  mergeLiveAndPrefs, indexWindowsByWorktree, indexProjects, indexWorkspaces,
  projectLabel, groupLabel, paneChip, maxSeverity, MAIN_KEY,
} from "./railTree.js";
import { PaneRow, ReviewRow, NewTabRow, WorktreeRow, RepoRow, WorktreeMeta } from "./RailRows.jsx";
import { SectionHeader } from "./SectionHeader.jsx";
import { AttentionTop, ActivitySection } from "./AttentionSections.jsx";

// Bridge to the launcher modal. The "+ New tab" row opens it via
// window.__periscopeOpenLauncher — installed by vanilla app.js while the
// launcher is still vanilla (Task 8 not yet done), and by the Preact
// LauncherModal once that lands. Mirrors poll.js's __periscopeOpenModal bridge;
// no-op if nothing is wired yet. Avoids a dynamic import so the committed
// dist/app.js stays a single stable chunk (no content-hashed side bundle).
function openLauncher(worktreeKey) {
  const fn = window.__periscopeOpenLauncher;
  if (typeof fn === "function") fn(worktreeKey);
}

// Trailing path segment, for the workspace header's base-repo chip.
function basename(p) {
  const parts = String(p || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || p;
}

// --- syncRailPrefs: reconcile prefs with live state, throttled to 5s. --------
// Prune dead pref entries + persist new live entries at their merged position,
// so ordering is sticky across reloads even when the user hasn't dragged.
let lastSyncAt = 0;
function syncRailPrefs() {
  if (Date.now() - lastSyncAt < 5000) return;
  const live = windows.value || [];
  if (live.length === 0) return;   // /api/state hasn't loaded — don't write empty
  lastSyncAt = Date.now();

  const merged = mergeLiveAndPrefs(
    live, projects.value, workspaces.value, prefs.getRepoOrder(), prefs.getWorktreesByRepo(), prefs.getPanesByWorktree()
  );
  const prefRepoOrder = prefs.getRepoOrder();
  const prefWtByRepo = prefs.getWorktreesByRepo();
  const prefPanesByWt = prefs.getPanesByWorktree();

  // ws: keys are KEPT in repo_order (interleaved, not bottom-pinned like dev);
  // their flat tagged-pane order persists in panes_by_worktree["ws:<id>"].
  // Only MAIN_KEY is stripped from repo_order / worktrees_by_repo.
  const nextRepoOrder = merged.repoOrder.filter((r) => r !== MAIN_KEY);
  const nextWtByRepo = { ...merged.worktreesByRepo };
  delete nextWtByRepo[MAIN_KEY];
  for (const k of Object.keys(nextWtByRepo)) {
    if (k.startsWith("ws:")) delete nextWtByRepo[k];  // ws: have empty wt lists
  }
  const nextPanesByWt = {};
  for (const r of nextRepoOrder) {
    if (r.startsWith("ws:")) {
      nextPanesByWt[r] = merged.panesByWorktree[r] || [];
      continue;
    }
    for (const wt of (nextWtByRepo[r] || [])) {
      nextPanesByWt[wt] = merged.panesByWorktree[wt] || [];
    }
  }
  // Dev's flat order IS persisted (unlike Other's was) — it's the only
  // ordering state dev has.
  if (merged.panesByWorktree[MAIN_KEY]) {
    nextPanesByWt[MAIN_KEY] = merged.panesByWorktree[MAIN_KEY];
  }

  if (
    JSON.stringify(nextRepoOrder) === JSON.stringify(prefRepoOrder) &&
    JSON.stringify(nextWtByRepo) === JSON.stringify(prefWtByRepo) &&
    JSON.stringify(nextPanesByWt) === JSON.stringify(prefPanesByWt)
  ) return;

  prefs.patchUI({
    repo_order: nextRepoOrder,
    worktrees_by_repo: nextWtByRepo,
    panes_by_worktree: nextPanesByWt,
  });
}

// --- Reorder splices (all seeded from the merged tree, not raw prefs). -------
function spliceMove(list, fromIdx, toIdx, insertAfter) {
  if (fromIdx < 0 || toIdx < 0) return list;
  const [val] = list.splice(fromIdx, 1);
  let landing = toIdx - (fromIdx < toIdx ? 1 : 0);
  if (insertAfter) landing += 1;
  list.splice(landing, 0, val);
  return list;
}

function currentMergedOrder() {
  return mergeLiveAndPrefs(
    windows.value, projects.value, workspaces.value, prefs.getRepoOrder(), prefs.getWorktreesByRepo(), prefs.getPanesByWorktree()
  );
}

async function reorderRepos(draggedKey, targetKey, insertAfter) {
  const dragged = draggedKey.replace(/^repo:/, "");
  const target = targetKey.replace(/^repo:/, "");
  if (dragged === MAIN_KEY || target === MAIN_KEY) return;
  const { repoOrder } = currentMergedOrder();
  const order = repoOrder.filter((r) => r !== MAIN_KEY);
  const from = order.indexOf(dragged);
  const to = order.indexOf(target);
  if (from < 0 || to < 0) return;
  spliceMove(order, from, to, insertAfter);
  await prefs.setRepoOrder(order);
}

async function reorderWorktrees(draggedKey, targetKey, dragRepoKey, insertAfter) {
  const dragged = draggedKey.replace(/^wt:/, "");
  const target = targetKey.replace(/^wt:/, "");
  const { worktreesByRepo } = currentMergedOrder();
  // The dragged worktree's repo comes from the drag descriptor, not a DOM walk.
  const repoKey = dragRepoKey;
  if (!repoKey || !worktreesByRepo[repoKey]) return;
  const list = [...worktreesByRepo[repoKey]];
  if (!list.includes(target)) return;  // cross-repo drag — reject
  const from = list.indexOf(dragged);
  const to = list.indexOf(target);
  if (from < 0 || to < 0) return;
  spliceMove(list, from, to, insertAfter);
  const next = { ...prefs.getWorktreesByRepo(), [repoKey]: list };
  await prefs.setWorktreesByRepo(next);
}

async function reorderChildren(dragChildKey, targetChildKey, worktreeKey, insertAfter) {
  if (!worktreeKey) return;
  const { panesByWorktree } = currentMergedOrder();
  const list = [...(panesByWorktree[worktreeKey] || [])];
  const from = list.indexOf(dragChildKey);
  const to = list.indexOf(targetChildKey);
  if (from < 0 || to < 0) return;
  spliceMove(list, from, to, insertAfter);
  const next = { ...prefs.getPanesByWorktree(), [worktreeKey]: list };
  await prefs.setPanesByWorktree(next);
}

// Same-kind, same-parent drop rule. Identity from the drag descriptor.
// The top-level group a drop row belongs to: a repo path, MAIN_KEY, or
// "ws:<id>". A group header's key is "repo:<group>"; a child row carries its
// group as worktreeKey.
function rowGroup(d) {
  return d.kind === "repo" ? d.key.slice("repo:".length) : d.worktreeKey;
}

function isValidDropTarget(drag, target) {
  if (drag.kind === "repo") {
    if (target.kind !== "repo") return false;
    if (target.key === `repo:${MAIN_KEY}`) return false;
    if (drag.key === `repo:${MAIN_KEY}`) return false;
    return true;
  }
  if (drag.kind === "worktree") {
    if (target.kind !== "worktree") return false;
    return drag.repoKey === target.repoKey;
  }
  // pane / review drag.
  const dragGroup = drag.worktreeKey;
  const targetGroup = rowGroup(target);
  if (dragGroup === targetGroup) {
    // Same group → reorder; only onto child rows, never the group header.
    return target.kind === "pane" || target.kind === "review";
  }
  // Cross-group is a workspace tag/untag/move — panes only (not the review
  // sentinel), and only when a workspace is the source or the destination.
  // The drop lands on the workspace's header OR any of its child rows.
  if (drag.kind !== "pane") return false;
  return targetGroup.startsWith("ws:") || dragGroup.startsWith("ws:");
}

export function Rail() {
  // Drag descriptor (kind/key/repoKey/worktreeKey) — captured on dragStart,
  // read on dragOver/drop. A ref, not state: it must not trigger re-renders
  // mid-drag (which would detach the drag source).
  const drag = useRef(null);
  // Drop indicator: { key, pos:"before"|"after" } — drives the .drop-target
  // class + data-drop-pos attr (CSS draws the insertion line). State so the
  // indicator re-renders; the dragged source itself is keyed/stable.
  const [dropTarget, setDropTarget] = useState(null);

  // Reading these signals subscribes the component → re-render each poll.
  const live = windows.value;
  const projs = projects.value || [];
  const filter = currentFilter.value;
  const selectedKey = railSelection.value;
  // Subscribe to prefs so collapse/order changes re-render.
  const prefsBlob = prefs.prefsSignal.value;

  // Reconcile prefs ⇄ live (throttled). Side-effecting read; fine here.
  syncRailPrefs();

  const collapsed = prefs.getRailCollapsed();
  const projectsCollapsed = collapsed[`sec:projects`] === true;
  const byWorktree = indexWindowsByWorktree(live);
  const projsByPin = indexProjects(projs);
  const projectsBySession = {};
  for (const p of projs) if (p.tmux_session) projectsBySession[p.tmux_session] = p;
  const mainProject = projsByPin[MAIN_KEY] || {};
  const wss = workspaces.value || [];
  const workspacesById = indexWorkspaces(wss);
  // Across-all-windows pid → window map, for the FLAT ws:/dev lists (the
  // per-worktree `windowsByPid` built inside the !isDev branch is scoped to a
  // single session and can't resolve cross-session tagged pids).
  const windowsByPid = {};
  for (const w of live) windowsByPid[w.pid] = w;
  const { repoOrder, worktreesByRepo, panesByWorktree } = mergeLiveAndPrefs(
    live, projs, wss, prefs.getRepoOrder(), prefs.getWorktreesByRepo(), prefs.getPanesByWorktree()
  );
  // Universe for shortest-unique-suffix worktree labels: every non-dev
  // project's displayed name, so a label only grows a segment on real collision.
  const wtLabelUniverse = repoOrder
    .filter((r) => r !== MAIN_KEY)
    .flatMap((r) => (worktreesByRepo[r] || []).map((wt) => projectLabel(projectsBySession[wt], wt)));

  // --- Selection -----------------------------------------------------------
  function selectKey(key) {
    railSelection.value = key;       // STRING highlight-key (signal)
    track("pane.focus", { key });
    if (key.startsWith("pane:")) {
      prefs.setLastSelected({ kind: "pane", pid: key.slice("pane:".length) });  // OBJECT (pref)
    } else if (key.startsWith("review:")) {
      prefs.setLastSelected({ kind: "review", worktree: key.slice("review:".length) });
    }
  }

  function toggleCollapse(key) {
    const cur = prefs.getRailCollapsed()[key] === true;
    prefs.setRailCollapsedKey(key, !cur);
  }

  // --- Close actions -------------------------------------------------------
  async function closePane(w) {
    const ok = await confirmDialog(
      `Close tab "${w.name || (w.is_claude ? "claude" : "shell")}"?\n\nThis kills its tmux window.`,
      { okLabel: "Close", danger: true }
    );
    if (!ok) return;
    await apiCall("close tab", `/api/window?${targetQuery(w.target)}`, { method: "DELETE" });
  }
  async function closeWorktree(session) {
    const ok = await confirmDialog(
      `Close session "${session}"?\n\nThis kills every tmux window in this worktree.\nThe worktree directory on disk is not removed.`,
      { okLabel: "Close", danger: true }
    );
    if (!ok) return;
    await apiCall("close session", `/api/session?session=${encodeURIComponent(session)}`, { method: "DELETE" });
  }

  // --- Rename --------------------------------------------------------------
  async function renamePane(w, next) {
    if (!w.target) return;
    await apiCall("rename tab", `/api/rename?${targetQuery(w.target)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: next }),
    });
  }
  async function renameWorktree(session, next) {
    await apiCall("rename session", `/api/session/rename?session=${encodeURIComponent(session)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: next }),
    });
  }

  // --- Workspaces ----------------------------------------------------------
  // Tag/untag key on w.pane_id (the tmux %N the backend tag keys on) — NOT
  // w.pid (the @periscope_id). The next poll re-merges the tagged pane into
  // its ws:<id> group.
  async function tagPane(w, workspaceId) {
    await apiCall("tag", "/api/workspaces/tag", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: workspaceId, pane_id: w.pane_id }),
    });
  }
  async function untagPane(w) {
    await apiCall("untag", "/api/workspaces/untag", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pane_id: w.pane_id }),
    });
  }
  async function spawnIntoWorkspace(wid) {
    const branch = window.prompt("New worktree branch for this workspace:");
    if (!branch) return;
    const data = await apiCall("spawn", "/api/workspaces/spawn", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: wid, branch }),
    });
    if (data && data.ui) prefs.setUI(data.ui);
  }

  // --- Drag plumbing -------------------------------------------------------
  // dragProps factory: every draggable row gets onDragStart/onDragOver/
  // onDrop/onDragEnd wired to the shared descriptor + indicator. `desc` is the
  // row's identity (kind/key/repoKey/worktreeKey).
  function makeDragProps(desc) {
    return {
      onDragStart: (e) => {
        drag.current = desc;
        dragState.value = { kind: desc.kind };   // pause the poll mid-drag
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", desc.key);  // cross-window compat — unused
      },
      onDragOver: (e) => {
        const d = drag.current;
        if (!d || d.key === desc.key) { setDropTarget(null); return; }
        if (!isValidDropTarget(d, desc)) { setDropTarget(null); return; }
        const rect = e.currentTarget.getBoundingClientRect();
        const insertAfter = (e.clientY - rect.top) > rect.height / 2;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        setDropTarget({ key: desc.key, pos: insertAfter ? "after" : "before" });
      },
      onDrop: async (e) => {
        e.preventDefault();
        const d = drag.current;
        setDropTarget(null);
        dragState.value = null;
        drag.current = null;
        if (!d || d.key === desc.key) return;
        if (!isValidDropTarget(d, desc)) return;
        const rect = e.currentTarget.getBoundingClientRect();
        const insertAfter = (e.clientY - rect.top) > rect.height / 2;
        if (d.kind === "repo") {
          await reorderRepos(d.key, desc.key, insertAfter);
        } else if (d.kind === "worktree") {
          await reorderWorktrees(d.key, desc.key, d.repoKey, insertAfter);
        } else {
          // pane/review. Same group → reorder. Cross-group (a workspace is
          // involved) → tag into the destination workspace, or untag when the
          // tab is dragged out of its workspace onto a repo/dev group.
          const dragGroup = d.worktreeKey;
          const targetGroup = rowGroup(desc);
          if (dragGroup === targetGroup) {
            await reorderChildren(d.childKey, desc.childKey, desc.worktreeKey, insertAfter);
          } else {
            const w = windowsByPid[d.childKey];
            if (w) {
              if (targetGroup.startsWith("ws:")) await tagPane(w, targetGroup.slice(3));
              else await untagPane(w);
            }
          }
        }
      },
      onDragEnd: () => {
        setDropTarget(null);
        dragState.value = null;
        drag.current = null;
      },
    };
  }

  // The drop-indicator position for a given row key (or undefined). Row
  // components merge this into their own class + data-drop-pos so the rail-row
  // base classes aren't clobbered.
  function dropPosFor(key) {
    return dropTarget && dropTarget.key === key ? dropTarget.pos : undefined;
  }

  // --- Empty state ---------------------------------------------------------
  if (repoOrder.length === 0) {
    return (
      <aside id="rail" aria-label="projects rail">
        <div class="rail-head"><span>Projects</span></div>
        <div class="rail-empty">
          No tmux windows found. Use <code>+ project</code> or <code>review PR</code> to start one.
        </div>
      </aside>
    );
  }

  // --- Tree ----------------------------------------------------------------
  return (
    <aside id="rail" aria-label="projects rail">
      <AttentionTop />
      <SectionHeader
        label="PROJECTS"
        count={null}
        collapsed={projectsCollapsed}
        onToggle={() => toggleCollapse("sec:projects")}
      />
      {!projectsCollapsed && repoOrder.map((repoKey) => {
        const isDev = repoKey === MAIN_KEY;
        const isWs = repoKey.startsWith("ws:");
        const wsRow = isWs ? (workspacesById[repoKey.slice(3)] || {}) : null;
        // ws: groups are flat (like dev) — their tagged windows come from the
        // across-all-windows pid map, not byWorktree (which keys on session).
        const wsWindows = isWs
          ? (panesByWorktree[repoKey] || []).map((pid) => windowsByPid[pid]).filter(Boolean)
          : [];
        const repoLabel = isWs ? (wsRow.name || repoKey.slice(3)) : groupLabel(repoKey, projsByPin);
        const repoChip = isWs && wsRow.base_repo ? basename(wsRow.base_repo) : null;
        const worktrees = worktreesByRepo[repoKey] || [];
        const repoCollapsed = collapsed[`repo:${repoKey}`] === true;
        const devWindows = isDev
          ? (panesByWorktree[MAIN_KEY] || []).map((pid) => live.find((w) => w.pid === pid)).filter(Boolean)
          : [];
        const repoChildStates = isDev
          ? devWindows.map((w) => w.state || "shell")
          : isWs
          ? wsWindows.map((w) => w.state || "shell")
          : worktrees.flatMap((wt) => (byWorktree[wt] || []).map((w) => w.state || "shell"));
        const repoRolledUp = maxSeverity(repoChildStates);
        const repoDim = isDev
          ? devWindows.some((w) => passesFilter(w, filter))
          : isWs
          ? wsWindows.some((w) => passesFilter(w, filter))
          : worktrees.some((wt) => (byWorktree[wt] || []).some((w) => passesFilter(w, filter)));
        const repoKeyStr = `repo:${repoKey}`;

        return (
          <RailFragment key={repoKeyStr}>
            <RepoRow
              repoKey={repoKey}
              label={repoLabel}
              chip={repoChip}
              collapsed={repoCollapsed}
              rolledUp={repoRolledUp}
              dim={repoDim}
              isDev={isDev}
              onToggle={() => toggleCollapse(repoKeyStr)}
              dragProps={makeDragProps({ kind: "repo", key: repoKeyStr })}
              dropPos={dropPosFor(repoKeyStr)}
            />
            {isWs && !repoCollapsed && (() => {
              // Workspace: flat pane list of explicitly-tagged tabs (dev-flat
              // shape). Drag descriptors use the ws:<id> key as worktreeKey so
              // cross-tab reorder passes the same-parent drop rule; order
              // persists via panes_by_worktree["ws:<id>"] (syncRailPrefs).
              const rows = wsWindows.map((w) => (
                <PaneRow
                  key={`pane:${w.pid}`}
                  w={w}
                  chip={paneChip(w, { isDev: true })}
                  selectedKey={selectedKey}
                  dim={passesFilter(w, filter)}
                  onSelect={selectKey}
                  onClose={() => closePane(w)}
                  onRename={(next) => renamePane(w, next)}
                  dragProps={makeDragProps({ kind: "pane", key: `pane:${w.pid}`, childKey: w.pid, worktreeKey: repoKey })}
                  dropPos={dropPosFor(`pane:${w.pid}`)}
                  pinned={prefs.getPinnedPids().includes(w.pid)}
                  onTogglePin={() => prefs.togglePin(w.pid)}
                />
              ));
              if (rows.length === 0) {
                rows.push(
                  <div key={`parked:${repoKey}`} class="rail-row child-row rail-dim">
                    <span class="rail-label">parked · spawn from base</span>
                  </div>
                );
              }
              rows.push(
                <NewTabRow
                  key={`newtab:${repoKey}`}
                  worktreeKey={repoKey}
                  onOpen={() => spawnIntoWorkspace(repoKey.slice(3))}
                />
              );
              return rows;
            })()}
            {isDev && !repoCollapsed && (() => {
              // Dev: flat pane list across __main__'s session + folded
              // ad-hoc sessions. Drag descriptors use MAIN_KEY as the
              // worktreeKey so cross-session reorder passes the existing
              // same-parent drop rule; order persists via
              // panes_by_worktree[MAIN_KEY] (syncRailPrefs).
              const rows = devWindows.map((w) => {
                const sessionPrefix = w.session !== mainProject.tmux_session ? w.session : null;
                return (
                  <PaneRow
                    key={`pane:${w.pid}`}
                    w={w}
                    chip={paneChip(w, { isDev: true, sessionPrefix })}
                    selectedKey={selectedKey}
                    dim={passesFilter(w, filter)}
                    onSelect={selectKey}
                    onClose={() => closePane(w)}
                    onRename={(next) => renamePane(w, next)}
                    dragProps={makeDragProps({ kind: "pane", key: `pane:${w.pid}`, childKey: w.pid, worktreeKey: MAIN_KEY })}
                    dropPos={dropPosFor(`pane:${w.pid}`)}
                    pinned={prefs.getPinnedPids().includes(w.pid)}
                    onTogglePin={() => prefs.togglePin(w.pid)}
                  />
                );
              });
              rows.push(
                <NewTabRow
                  key={`newtab:${MAIN_KEY}`}
                  worktreeKey={mainProject.tmux_session || "main"}
                  onOpen={openLauncher}
                />
              );
              return rows;
            })()}
            {!isDev && !repoCollapsed && worktrees.map((wtKey) => {
              const wtWindows = byWorktree[wtKey] || [];
              const windowsByPid = Object.fromEntries(wtWindows.map((w) => [w.pid, w]));
              const childOrder = panesByWorktree[wtKey] || [];
              const wtCollapsed = collapsed[`wt:${wtKey}`] === true;
              const childStates = [];
              const childRows = [];
              for (const child of childOrder) {
                if (child === "review") {
                  const lgtmLive = wtWindows.some((w) => w.lgtm && w.lgtm.slug);
                  childRows.push(
                    <ReviewRow
                      key={`review:${wtKey}`}
                      worktreeKey={wtKey}
                      lgtmLive={lgtmLive}
                      selectedKey={selectedKey}
                      onSelect={selectKey}
                      dragProps={makeDragProps({ kind: "review", key: `review:${wtKey}`, childKey: "review", worktreeKey: wtKey })}
                      dropPos={dropPosFor(`review:${wtKey}`)}
                    />
                  );
                } else {
                  const w = windowsByPid[child];
                  if (!w) continue;  // pane gone; skip silently
                  childStates.push(w.state || "shell");
                  childRows.push(
                    <PaneRow
                      key={`pane:${w.pid}`}
                      w={w}
                      chip={paneChip(w)}
                      selectedKey={selectedKey}
                      dim={passesFilter(w, filter)}
                      onSelect={selectKey}
                      onClose={() => closePane(w)}
                      onRename={(next) => renamePane(w, next)}
                      dragProps={makeDragProps({ kind: "pane", key: `pane:${w.pid}`, childKey: w.pid, worktreeKey: wtKey })}
                      dropPos={dropPosFor(`pane:${w.pid}`)}
                      pinned={prefs.getPinnedPids().includes(w.pid)}
                      onTogglePin={() => prefs.togglePin(w.pid)}
                    />
                  );
                }
              }
              childRows.push(<NewTabRow key={`newtab:${wtKey}`} worktreeKey={wtKey} onOpen={openLauncher} />);

              const rolledUp = maxSeverity(childStates);
              // Stable label from the project row — never the first pane's
              // cwd-derived branch (which churned on cd).
              const label = shortestUniqueSuffix(projectLabel(projectsBySession[wtKey], wtKey), wtLabelUniverse);
              const wtDim = wtWindows.some((w) => passesFilter(w, filter));
              const childCount = childOrder.filter((c) => c === "review" || windowsByPid[c]).length;

              return (
                <RailFragment key={`wt:${wtKey}`}>
                  <WorktreeRow
                    worktreeKey={wtKey}
                    label={label}
                    collapsed={wtCollapsed}
                    childCount={childCount}
                    rolledUp={rolledUp}
                    dim={wtDim}
                    onToggle={() => toggleCollapse(`wt:${wtKey}`)}
                    onClose={() => closeWorktree(wtKey)}
                    onRename={(next) => renameWorktree(wtKey, next)}
                    dragProps={makeDragProps({ kind: "worktree", key: `wt:${wtKey}`, repoKey })}
                    dropPos={dropPosFor(`wt:${wtKey}`)}
                  />
                  <WorktreeMeta wtWindows={wtWindows} />
                  {!wtCollapsed && childRows}
                </RailFragment>
              );
            })}
          </RailFragment>
        );
      })}
      <ActivitySection />
    </aside>
  );
}

// Plain fragment passthrough (keeps the flat row sequence the CSS sibling
// selectors — child-row::before/::after, last-in-worktree — depend on, since
// they rely on adjacency, not nesting).
function RailFragment({ children }) {
  return <>{children}</>;
}
