// Left rail of the split view (#rail): the Track → (derived Branch) → Pane tree
// derived from prefs (curated order) joined with /api/state (live status).
// Membership is TRACK-ANCHORED (railTree.js header has the full contract): a
// window belongs to its `w.track_id` (resolved server-side, always present).
// The mid tier — branch sub-clusters — is DERIVED from `w.branch`, not a pref:
// a track spanning ≥2 distinct branches renders sub-clusters, otherwise flat.
//
// Read model:
//   - `windows` signal  → live membership + status (rebuilt each poll)
//   - prefs.*           → track order / tab order / collapse / last_selected
//   - `railSelection`   → STRING highlight-key the rows compare against
//
// Two deliberately-different selection shapes (do NOT cross them):
//   - PERSISTED  prefs.last_selected is an OBJECT  ({kind:"pane",pid} / {kind:"review",worktree})
//   - HIGHLIGHT  the rows compare against a STRING ("pane:<pid>" / "review:<worktree>")
// railSelection mirrors the string; setLastSelected stores the object.
//
// Drag identity travels on the drag descriptor captured at dragStart (kind +
// key + trackKey) — NEVER a previousElementSibling DOM walk, which breaks under
// a component tree (#6). The reorder splices run against currentMergedOrder()
// (the same merged tree the render uses), not raw prefs. Two drag kinds:
// "track" (top-level reorder) and "pane" (reorder within a track, or move a tab
// to another track by POST /api/tracks/move-tab?track_id=). The id is a QUERY
// param, never a path segment — a repo-default id is a repo PATH (invariant #6).
import { useRef, useState } from "preact/hooks";
import { passesFilter } from "../filter.js";
import { confirmDialog } from "../overlays/Dialog.jsx";
import * as prefs from "../prefs.js";
import { currentFilter, dismissedAlertIds, dragState, projects, railSelection, tracks, windows, workspaces } from "../store.js";
import { track } from "../track.js";
import { apiCall, paneLabel, targetQuery } from "../util.js";
import { alertItems } from "./alertFeed.js";
import { ActivitySection, AttentionTop } from "./AttentionSections.jsx";
import { awaitingReplyByPid } from "./attention.js";
import { railHovered } from "./layoutFreeze.js";
import { BranchRow, NewTabRow, PaneRow, ReviewRow, TrackRow } from "./RailRows.jsx";
import { maxSeverity, mergeLiveAndPrefs, paneChip, trackKind, trackLabel } from "./railTree.js";
import { SectionHeader } from "./SectionHeader.jsx";

// The branch label used in railTree's flat-fallback bucket for a window with no
// `branch` (non-git cwd / pre-resolution race). Kept in sync with railTree's
// NO_BRANCH so the same window slots into the same key on both sides.
const NO_BRANCH = "";

// Bridge to the launcher modal. The "+ New tab" row opens it via
// window.__periscopeOpenLauncher; no-op if nothing is wired yet.
function openLauncher(key) {
  const fn = window.__periscopeOpenLauncher;
  if (typeof fn === "function") fn(key);
}

// --- Track-scope filter chips ------------------------------------------------
// A chip strip above the rail that narrows the whole rail (attention sort +
// mergeLiveAndPrefs) to a single track. "all" clears the scope. The active
// track id is transient UI state (a signal lives in store), not a pref — it's
// a momentary lens, not durable config. Returns the scoped windows list.
function scopeWindows(live, trackScope) {
  if (!trackScope || trackScope === "all") return live;
  return live.filter((w) => w.track_id === trackScope);
}

function TrackFilterChips({ live, scope, onScope }) {
  // Distinct tracks present in the live set, first-seen order, with labels.
  const seen = [];
  const seenSet = new Set();
  for (const w of live) {
    if (w.track_id && !seenSet.has(w.track_id)) {
      seenSet.add(w.track_id);
      seen.push(w.track_id);
    }
  }
  if (seen.length < 2) return null;   // nothing to scope between
  return (
    <div class="rail-filter-chips" role="tablist" aria-label="filter by track">
      <button
        class={`rail-filter-chip${!scope || scope === "all" ? " is-active" : ""}`}
        onClick={() => onScope("all")}
      >all</button>
      {seen.map((tid) => (
        <button
          key={tid}
          class={`rail-filter-chip${scope === tid ? " is-active" : ""}`}
          onClick={() => onScope(tid)}
          title={trackLabel(tid, live)}
        >{trackLabel(tid, live)}</button>
      ))}
    </div>
  );
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

  const merged = mergeLiveAndPrefs(live, projects.value, workspaces.value, {
    trackOrder: prefs.getTrackOrder(),
    tabsByTrack: prefs.getTabsByTrack(),
    branchOrderByTrack: prefs.getBranchOrderByTrack(),
  }, tracks.value);
  const prefTrackOrder = prefs.getTrackOrder();
  const prefTabsByTrack = prefs.getTabsByTrack();

  const nextTrackOrder = merged.trackOrder;
  const nextTabsByTrack = {};
  for (const t of nextTrackOrder) nextTabsByTrack[t] = merged.tabsByTrack[t] || [];

  if (
    JSON.stringify(nextTrackOrder) === JSON.stringify(prefTrackOrder) &&
    JSON.stringify(nextTabsByTrack) === JSON.stringify(prefTabsByTrack)
  ) return;

  prefs.patchUI({
    track_order: nextTrackOrder,
    tabs_by_track: nextTabsByTrack,
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
  return mergeLiveAndPrefs(windows.value, projects.value, workspaces.value, {
    trackOrder: prefs.getTrackOrder(),
    tabsByTrack: prefs.getTabsByTrack(),
    branchOrderByTrack: prefs.getBranchOrderByTrack(),
  }, tracks.value);
}

async function reorderTracks(draggedKey, targetKey, insertAfter) {
  const dragged = draggedKey.replace(/^track:/, "");
  const target = targetKey.replace(/^track:/, "");
  const { trackOrder } = currentMergedOrder();
  const order = [...trackOrder];
  const from = order.indexOf(dragged);
  const to = order.indexOf(target);
  if (from < 0 || to < 0) return;
  spliceMove(order, from, to, insertAfter);
  await prefs.setTrackOrder(order);
}

// Reorder a tab within its track's flat tab order. Identity from the drag
// descriptor (childKey = pid; trackKey = the owning track id).
async function reorderTabs(dragChildKey, targetChildKey, trackKey, insertAfter) {
  if (!trackKey) return;
  const { tabsByTrack } = currentMergedOrder();
  const list = [...(tabsByTrack[trackKey] || [])];
  const from = list.indexOf(dragChildKey);
  const to = list.indexOf(targetChildKey);
  if (from < 0 || to < 0) return;
  spliceMove(list, from, to, insertAfter);
  const next = { ...prefs.getTabsByTrack(), [trackKey]: list };
  await prefs.setTabsByTrack(next);
}

// Move a tab into another track (cross-track pane drag). Re-tags server-side;
// the next poll re-merges the pane under the destination track.
async function moveTabToTrack(w, trackId) {
  await apiCall("move tab", `/api/tracks/move-tab?track_id=${encodeURIComponent(trackId)}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pane_id: w.pane_id }),
  });
}

// Same-kind drop rule. Identity from the drag descriptor.
//   - track → track  : reorder top-level.
//   - pane  → pane    : same track → reorder; different track → move (re-tag).
//   - pane  → track   : move into that track (header is a drop zone).
function isValidDropTarget(drag, target) {
  if (drag.kind === "track") return target.kind === "track";
  if (drag.kind === "pane") {
    if (target.kind === "pane") return true;       // reorder (same) or move (cross)
    if (target.kind === "track") return true;      // move onto a track header
    return false;
  }
  return false;   // review rows aren't draggable
}

export function Rail() {
  // Drag descriptor (kind/key/trackKey/childKey) — captured on dragStart, read
  // on dragOver/drop. A ref, not state: it must not trigger re-renders mid-drag.
  const drag = useRef(null);
  // Drop indicator: { key, pos:"before"|"after" }.
  const [dropTarget, setDropTarget] = useState(null);
  // Track-scope filter (transient — a momentary lens, not a pref).
  const [trackScope, setTrackScope] = useState("all");

  // Reading these signals subscribes the component → re-render each poll.
  const allLive = windows.value;
  const filter = currentFilter.value;
  const selectedKey = railSelection.value;
  // Subscribe to prefs so collapse/order changes re-render.
  const _prefsBlob = prefs.prefsSignal.value;

  // Reconcile prefs ⇄ live (throttled). Side-effecting read; fine here.
  syncRailPrefs();

  // The filter chips scope the whole rail to one track BEFORE the tree merge.
  const live = scopeWindows(allLive, trackScope);

  const collapsed = prefs.getRailCollapsed();
  const tracksCollapsed = collapsed["sec:projects"] === true;
  // Across-all-windows pid → window map (the flat per-track tab lists).
  const windowsByPid = {};
  for (const w of live) windowsByPid[w.pid] = w;
  // Panes with an unanswered need_human, so the tree row carries the same
  // signal the NEEDS YOU section does. Without it a blocked pane looks normal
  // in the tree, and NEEDS YOU is collapsible — collapse it and the whole
  // escalation disappears.
  const awaiting = awaitingReplyByPid(
    allLive, alertItems.value, dismissedAlertIds.value
  );
  // The spawner's NAME comes from the server (persisted, so it outlives the
  // lead). Liveness is the client's to know, and decides only whether the chip
  // can reveal the lead — a dead lead still shows, greyed.
  const livePids = new Set(allLive.map((w) => w.pid));
  // Registry rows feed EMPTY goal tracks into the tree; a track-scope lens
  // narrows them the same way scopeWindows narrows the live set.
  const allTracks = tracks.value;
  const scopedTracks = (!trackScope || trackScope === "all")
    ? allTracks : allTracks.filter((t) => t.id === trackScope);
  const { trackOrder, tabsByTrack, branchesByTrack, tabsByBranch } = mergeLiveAndPrefs(
    live, projects.value, workspaces.value, {
      trackOrder: prefs.getTrackOrder(),
      tabsByTrack: prefs.getTabsByTrack(),
      branchOrderByTrack: prefs.getBranchOrderByTrack(),
    }, scopedTracks
  );

  // --- Selection -----------------------------------------------------------
  function selectKey(key) {
    railSelection.value = key;       // STRING highlight-key (signal)
    track("pane.focus", { key });
    if (key.startsWith("pane:")) {
      prefs.setLastSelected({ kind: "pane", pid: key.slice("pane:".length) });
    } else if (key.startsWith("review:")) {
      prefs.setLastSelected({ kind: "review", worktree: key.slice("review:".length) });
    }
  }

  function toggleCollapse(key) {
    const cur = prefs.getRailCollapsed()[key] === true;
    prefs.setRailCollapsedKey(key, !cur);
  }

  // --- Close / rename actions ----------------------------------------------
  async function closePane(w) {
    const ok = await confirmDialog(
      `Close tab "${paneLabel(w)}"?\n\nThis kills its tmux window.`,
      { okLabel: "Close", danger: true }
    );
    if (!ok) return;
    await apiCall("close tab", `/api/window?${targetQuery(w.target)}`, { method: "DELETE" });
  }
  async function renamePane(w, next) {
    if (!w.target) return;
    await apiCall("rename tab", `/api/rename?${targetQuery(w.target)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: next }),
    });
  }

  // --- Track lifecycle -----------------------------------------------------
  async function renameTrack(trackId, next) {
    await apiCall("rename track", `/api/tracks?track_id=${encodeURIComponent(trackId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: next }),
    });
  }
  // Dissolve is the safe default: archive the track, its tabs survive and fall
  // back to repo-default/loose on the next poll. No confirm — nothing is killed.
  async function dissolveTrack(trackId) {
    await apiCall("dissolve track", `/api/tracks/dissolve?track_id=${encodeURIComponent(trackId)}`, {
      method: "POST",
    });
  }
  // Tear down is destructive: POST /teardown returns the kill list ({killed}),
  // but the server kills as part of the same call — so confirm FIRST against a
  // preview, then call. The route both computes and kills, so we describe the
  // tabs in the confirm by resolving the track's live tabs locally, then POST.
  async function teardownTrack(trackId, label) {
    const tabs = (tabsByTrack[trackId] || [])
      .map((pid) => windowsByPid[pid])
      .filter(Boolean)
      .map((w) => paneLabel(w));
    const list = tabs.length ? `\n\n• ${tabs.join("\n• ")}` : "";
    const ok = await confirmDialog(
      `Tear down track "${label}"?\n\nThis kills ${tabs.length} tab${tabs.length === 1 ? "" : "s"} (their tmux windows):${list}`,
      { okLabel: "Tear down", danger: true }
    );
    if (!ok) return;
    await apiCall("teardown track", `/api/tracks/teardown?track_id=${encodeURIComponent(trackId)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delete_worktrees: false }),
    });
  }

  // --- Drag plumbing -------------------------------------------------------
  function makeDragProps(desc) {
    return {
      onDragStart: (e) => {
        drag.current = desc;
        dragState.value = { kind: desc.kind };   // pause the poll mid-drag
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", desc.key);
      },
      onDragOver: (e) => {
        // A row sits inside its track's card, which is itself a drop zone
        // (makeCardDropProps). Stop here so the innermost row owns the event
        // and the card only sees drags over its own empty space.
        e.stopPropagation();
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
        e.stopPropagation();   // see onDragOver — the innermost row wins
        const d = drag.current;
        setDropTarget(null);
        dragState.value = null;
        drag.current = null;
        if (!d || d.key === desc.key) return;
        if (!isValidDropTarget(d, desc)) return;
        const rect = e.currentTarget.getBoundingClientRect();
        const insertAfter = (e.clientY - rect.top) > rect.height / 2;
        if (d.kind === "track") {
          await reorderTracks(d.key, desc.key, insertAfter);
        } else {
          // pane drag. Same track → reorder; cross-track (or onto a track
          // header) → move (re-tag into the destination track).
          const targetTrack = desc.kind === "track" ? desc.key.slice("track:".length) : desc.trackKey;
          if (desc.kind === "pane" && d.trackKey === targetTrack) {
            await reorderTabs(d.childKey, desc.childKey, d.trackKey, insertAfter);
          } else {
            const w = windowsByPid[d.childKey];
            if (w && targetTrack) await moveTabToTrack(w, targetTrack);
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

  function dropPosFor(key) {
    return dropTarget && dropTarget.key === key ? dropTarget.pos : undefined;
  }

  // The whole track card is a drop zone for a tab: dropping anywhere in it
  // moves the tab into that track. Previously the ONLY target was the track's
  // seclabel — a 27px uppercase strip — and an EMPTY track's card (the obvious
  // "put it here" box, and the only way to give a fresh track its first tab by
  // drag) had no handler at all, so it rejected every drop with no feedback.
  // Rows inside the card stopPropagation, so this only fires over empty space.
  function makeCardDropProps(trackId) {
    const key = `card:${trackId}`;
    return {
      onDragOver: (e) => {
        const d = drag.current;
        if (!d || d.kind !== "pane" || d.trackKey === trackId) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        setDropTarget({ key, pos: "into" });
      },
      onDrop: async (e) => {
        e.preventDefault();
        const d = drag.current;
        setDropTarget(null);
        dragState.value = null;
        drag.current = null;
        if (!d || d.kind !== "pane" || d.trackKey === trackId) return;
        const w = windowsByPid[d.childKey];
        if (w) await moveTabToTrack(w, trackId);
      },
    };
  }

  // Build the pane rows for a flat list of pids (within a track or a branch
  // sub-cluster). trackKey is the drag/reorder parent.
  function paneRowsFor(pids, trackKey) {
    return pids
      .map((pid) => windowsByPid[pid])
      .filter(Boolean)
      .map((w) => (
        <PaneRow
          key={`pane:${w.pid}`}
          w={w}
          chip={paneChip(w, { isDev: true })}
          selectedKey={selectedKey}
          dim={passesFilter(w, filter)}
          onSelect={selectKey}
          onClose={() => closePane(w)}
          onRename={(next) => renamePane(w, next)}
          dragProps={makeDragProps({ kind: "pane", key: `pane:${w.pid}`, childKey: w.pid, trackKey })}
          dropPos={dropPosFor(`pane:${w.pid}`)}
          pinned={prefs.getPinnedPids().includes(w.pid)}
          onTogglePin={() => prefs.togglePin(w.pid)}
          awaitingSince={awaiting.get(w.pid)}
          spawnerName={w.spawner_name}
          spawnerLive={!!w.spawned_by && livePids.has(w.spawned_by)}
          onRevealSpawner={() => selectKey(`pane:${w.spawned_by}`)}
        />
      ));
  }

  // The LGTM review row is re-homed under its TRACK (Task 12 dropped the
  // "review" tree sentinel). A track with any live LGTM session gets one review
  // row at the top of its body (its worktree key = the first such window's
  // session, which keys the iframe in <Detail>). Approach noted in the report.
  function reviewRowFor(trackId) {
    const w = live.find((x) => x.track_id === trackId && x.lgtm?.slug);
    if (!w) return null;
    const wtKey = w.session;
    return (
      <ReviewRow
        key={`review:${wtKey}`}
        worktreeKey={wtKey}
        lgtmLive={true}
        selectedKey={selectedKey}
        onSelect={selectKey}
        dragProps={{}}
        dropPos={dropPosFor(`review:${wtKey}`)}
      />
    );
  }

  // --- Empty state ---------------------------------------------------------
  if (trackOrder.length === 0) {
    return (
      <aside id="rail" aria-label="tracks rail">
        <div class="rail-head"><span>Tracks</span></div>
        <div class="rail-empty">
          No tmux windows found. Use <code>+ new</code> to start one.
        </div>
      </aside>
    );
  }

  // --- Tree ----------------------------------------------------------------
  return (
    <aside
      id="rail"
      aria-label="tracks rail"
      // Layout-freeze latch: while the pointer is in the rail, the attention
      // sections hold their membership so a row can't move out from under the
      // cursor mid-click. See layoutFreeze.js.
      onMouseEnter={() => { railHovered.value = true; }}
      onMouseLeave={() => { railHovered.value = false; }}
    >
      <AttentionTop />
      <TrackFilterChips live={allLive} scope={trackScope} onScope={setTrackScope} />
      <SectionHeader
        label="TRACKS"
        count={null}
        collapsed={tracksCollapsed}
        onToggle={() => toggleCollapse("sec:projects")}
      />
      {!tracksCollapsed && trackOrder.map((trackId) => {
        const trackKeyStr = `track:${trackId}`;
        const trackCollapsed = collapsed[`repo:${trackId}`] === true;
        const branches = branchesByTrack[trackId] || [];
        const multiBranch = branches.length >= 2;
        const label = trackLabel(trackId, live, allTracks);

        // Rolled-up status + filter-dim over ALL the track's tabs.
        const trackPids = tabsByTrack[trackId] || [];
        const trackWindows = trackPids.map((pid) => windowsByPid[pid]).filter(Boolean);
        const trackStates = trackWindows.map((w) => w.state || "shell");
        const trackRolledUp = maxSeverity(trackStates);
        // `dim` means "matches the filter" (RailRows inverts it). An EMPTY
        // track has no tab to match, and `[].some()` is false — so a freshly
        // created track rendered at .35 opacity under EVERY filter, including
        // `all`, and read as disabled. Emptiness is not a filter miss.
        const trackDim = trackWindows.length === 0
          || trackWindows.some((w) => passesFilter(w, filter));
        const review = reviewRowFor(trackId);

        return (
          <RailFragment key={trackKeyStr}>
            <TrackRow
              kind={trackKind(trackId, live, allTracks)}
              label={label}
              collapsed={trackCollapsed}
              rolledUp={trackRolledUp}
              dim={trackDim}
              onToggle={() => toggleCollapse(`repo:${trackId}`)}
              onRename={(next) => renameTrack(trackId, next)}
              onDissolve={() => dissolveTrack(trackId)}
              onTeardown={() => teardownTrack(trackId, label)}
              dragProps={makeDragProps({ kind: "track", key: trackKeyStr })}
              dropPos={dropPosFor(trackKeyStr)}
            />
            {/* ONE card per track (Model B). The track header (TrackRow) is a
                seclabel sitting ABOVE the card; everything below — optional
                branch sub-clusters, then the flat pane list, then "+ New tab"
                — lives inside this single bordered container. */}
            {!trackCollapsed && (
              <div
                class={`rail-group rail-track-card${dropPosFor(`card:${trackId}`) ? " drop-into" : ""}`}
                {...makeCardDropProps(trackId)}
              >
                {/* Review row (if any LGTM session is live) sits at the top of
                    the card body, above the branches / panes. */}
                {review && <div class="rail-group-body rail-review-head">{review}</div>}
                {multiBranch ? (
                  // Multi-branch: branch sub-clusters INSIDE the card (purple
                  // left-rail subgroups), not separate top-level cards.
                  branches.map((branch) => {
                    const branchKey = `${trackId}::${branch}`;   // track+branch collapse key
                    const branchCollapsed = collapsed[`wt:${branchKey}`] === true;
                    const pids = tabsByBranch[trackId]?.[branch] || [];
                    const childRows = paneRowsFor(pids, trackId);
                    const childStates = pids.map((pid) => windowsByPid[pid]).filter(Boolean).map((w) => w.state || "shell");
                    const branchDim = pids.map((pid) => windowsByPid[pid]).filter(Boolean).some((w) => passesFilter(w, filter));
                    const branchLabel = branch === NO_BRANCH ? "(no branch)" : branch;
                    return (
                      <div class="rail-subgroup" key={`wt:${branchKey}`}>
                        <BranchRow
                          label={branchLabel}
                          collapsed={branchCollapsed}
                          childCount={pids.length}
                          rolledUp={maxSeverity(childStates)}
                          dim={branchDim}
                          onToggle={() => toggleCollapse(`wt:${branchKey}`)}
                          dragProps={{}}
                          dropPos={undefined}
                        />
                        {!branchCollapsed && (
                          <div class="rail-group-body">{childRows}</div>
                        )}
                      </div>
                    );
                  })
                ) : (
                  // Single-branch: flat tab list straight inside the card.
                  <div class="rail-group-body">
                    {paneRowsFor(trackPids, trackId)}
                  </div>
                )}
                {/* ONE "+ New tab" per track, at the bottom of the card. The
                    launcher's branch picker chooses which branch (existing or
                    new) the tab lands in — so a single row covers every branch
                    subgroup above. */}
                <div class="rail-group-body rail-track-newtab">
                  <NewTabRow key={`newtab:${trackId}`} worktreeKey={trackId} onOpen={openLauncher} />
                </div>
              </div>
            )}
          </RailFragment>
        );
      })}
      <ActivitySection />
    </aside>
  );
}

// Plain fragment passthrough (keeps the flat row sequence the CSS sibling
// selectors depend on, since they rely on adjacency, not nesting).
function RailFragment({ children }) {
  return <>{children}</>;
}
