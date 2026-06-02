// Left rail of the split view (#rail): the Repo → Worktree → Pane+review tree
// derived from prefs (curated order) joined with /api/state (live status).
// Ported from rail.js — renderRail + all interaction handlers (collapse,
// select, inline rename, close, drag-reorder, syncRailPrefs).
//
// Read model:
//   - `windows` signal  → live membership + status (rebuilt each poll)
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
// OTHER_REPO_KEY is pinned to the bottom at all four enforcement points: merge
// (railTree), isValidDropTarget, reorderRepos, repoRow draggable gate (RailRows).
import { useRef, useState } from "preact/hooks";
import { windows, currentFilter, railSelection, dragState } from "../store.js";
import * as prefs from "../prefs.js";
import { passesFilter } from "../filter.js";
import { apiCall, targetQuery } from "../util.js";
import { confirmDialog } from "../overlays/Dialog.jsx";
import {
  mergeLiveAndPrefs, indexWindowsByWorktree, repoLabelFor, maxSeverity, OTHER_REPO_KEY,
} from "./railTree.js";
import { PaneRow, ReviewRow, NewTabRow, WorktreeRow, RepoRow, WorktreeMeta } from "./RailRows.jsx";

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
    live, prefs.getRepoOrder(), prefs.getWorktreesByRepo(), prefs.getPanesByWorktree()
  );
  const prefRepoOrder = prefs.getRepoOrder();
  const prefWtByRepo = prefs.getWorktreesByRepo();
  const prefPanesByWt = prefs.getPanesByWorktree();

  const nextRepoOrder = merged.repoOrder.filter((r) => r !== OTHER_REPO_KEY);
  const nextWtByRepo = { ...merged.worktreesByRepo };
  delete nextWtByRepo[OTHER_REPO_KEY];
  const nextPanesByWt = {};
  for (const r of nextRepoOrder) {
    for (const wt of (nextWtByRepo[r] || [])) {
      nextPanesByWt[wt] = merged.panesByWorktree[wt] || [];
    }
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
    windows.value, prefs.getRepoOrder(), prefs.getWorktreesByRepo(), prefs.getPanesByWorktree()
  );
}

async function reorderRepos(draggedKey, targetKey, insertAfter) {
  const dragged = draggedKey.replace(/^repo:/, "");
  const target = targetKey.replace(/^repo:/, "");
  if (dragged === OTHER_REPO_KEY || target === OTHER_REPO_KEY) return;
  const { repoOrder } = currentMergedOrder();
  const order = repoOrder.filter((r) => r !== OTHER_REPO_KEY);
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
function isValidDropTarget(drag, target) {
  if (drag.kind === "repo") {
    if (target.kind !== "repo") return false;
    if (target.key === `repo:${OTHER_REPO_KEY}`) return false;
    if (drag.key === `repo:${OTHER_REPO_KEY}`) return false;
    return true;
  }
  if (drag.kind === "worktree") {
    if (target.kind !== "worktree") return false;
    return drag.repoKey === target.repoKey;
  }
  // pane / review
  if (target.kind !== "pane" && target.kind !== "review") return false;
  return drag.worktreeKey === target.worktreeKey;
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
  const filter = currentFilter.value;
  const selectedKey = railSelection.value;
  // Subscribe to prefs so collapse/order changes re-render.
  const prefsBlob = prefs.prefsSignal.value;

  // Reconcile prefs ⇄ live (throttled). Side-effecting read; fine here.
  syncRailPrefs();

  const collapsed = prefs.getRailCollapsed();
  const byWorktree = indexWindowsByWorktree(live);
  const { repoOrder, worktreesByRepo, panesByWorktree } = mergeLiveAndPrefs(
    live, prefs.getRepoOrder(), prefs.getWorktreesByRepo(), prefs.getPanesByWorktree()
  );

  // --- Selection -----------------------------------------------------------
  function selectKey(key) {
    railSelection.value = key;       // STRING highlight-key (signal)
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
          await reorderChildren(d.childKey, desc.childKey, desc.worktreeKey, insertAfter);
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
          No worktree-backed tmux sessions are open. Use <code>+ project</code> or <code>review PR</code> to start one.
        </div>
      </aside>
    );
  }

  // --- Tree ----------------------------------------------------------------
  return (
    <aside id="rail" aria-label="projects rail">
      <div class="rail-head"><span>Projects</span></div>
      {repoOrder.map((repoKey) => {
        const isOther = repoKey === OTHER_REPO_KEY;
        const repoLabel = repoLabelFor(repoKey, live);
        const worktrees = worktreesByRepo[repoKey] || [];
        const repoCollapsed = collapsed[`repo:${repoKey}`] === true;
        const repoChildStates = worktrees.flatMap((wt) => (byWorktree[wt] || []).map((w) => w.state || "shell"));
        const repoRolledUp = maxSeverity(repoChildStates);
        const repoDim = worktrees.some((wt) => (byWorktree[wt] || []).some((w) => passesFilter(w, filter)));
        const repoKeyStr = `repo:${repoKey}`;

        return (
          <RailFragment key={repoKeyStr}>
            <RepoRow
              repoKey={repoKey}
              label={repoLabel}
              collapsed={repoCollapsed}
              rolledUp={repoRolledUp}
              dim={repoDim}
              isOther={isOther}
              onToggle={() => toggleCollapse(repoKeyStr)}
              dragProps={makeDragProps({ kind: "repo", key: repoKeyStr })}
              dropPos={dropPosFor(repoKeyStr)}
            />
            {!repoCollapsed && worktrees.map((wtKey) => {
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
                      selectedKey={selectedKey}
                      dim={passesFilter(w, filter)}
                      onSelect={selectKey}
                      onClose={() => closePane(w)}
                      onRename={(next) => renamePane(w, next)}
                      dragProps={makeDragProps({ kind: "pane", key: `pane:${w.pid}`, childKey: w.pid, worktreeKey: wtKey })}
                      dropPos={dropPosFor(`pane:${w.pid}`)}
                    />
                  );
                }
              }
              childRows.push(<NewTabRow key={`newtab:${wtKey}`} worktreeKey={wtKey} onOpen={openLauncher} />);

              const rolledUp = maxSeverity(childStates);
              const label = isOther ? wtKey : (wtWindows[0]?.branch || wtKey);
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
                    isOther={isOther}
                    onToggle={() => toggleCollapse(`wt:${wtKey}`)}
                    onClose={() => closeWorktree(wtKey)}
                    onRename={(next) => renameWorktree(wtKey, next)}
                    dragProps={makeDragProps({ kind: "worktree", key: `wt:${wtKey}`, repoKey })}
                    dropPos={dropPosFor(`wt:${wtKey}`)}
                  />
                  {!isOther && !wtCollapsed && <WorktreeMeta wtWindows={wtWindows} />}
                  {!wtCollapsed && childRows}
                </RailFragment>
              );
            })}
          </RailFragment>
        );
      })}
    </aside>
  );
}

// Plain fragment passthrough (keeps the flat row sequence the CSS sibling
// selectors — child-row::before/::after, last-in-worktree — depend on, since
// they rely on adjacency, not nesting).
function RailFragment({ children }) {
  return <>{children}</>;
}
