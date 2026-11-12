// Left rail of the split view (#rail): the sonar strip (every pane as a blip,
// pinned to the top), the Next queue (what wants you), PINNED, then the
// Track → (derived Branch) → Pane tree derived from prefs (curated order)
// joined with /api/state (live status). Membership is TRACK-ANCHORED
// (railTree.js header has the full contract): a window belongs to its
// `w.track_id` (resolved server-side, always present). The mid tier — branch
// sub-clusters — is DERIVED from `w.branch`, not a pref: a branch two or more
// of a track's tabs share clusters, every other tab sits flat.
//
// Read model:
//   - `windows` signal  → live membership + status (rebuilt each poll)
//   - prefs.*           → track order / tab order / collapse / last_selected
//   - `railSelection`   → STRING highlight-key the rows compare against
//
// Two deliberately-different selection shapes (do NOT cross them):
//   - PERSISTED  prefs.last_selected is an OBJECT  ({kind:"pane",pid} |
//                {kind:"review",repo,branch} | {kind:"reviews"})
//   - HIGHLIGHT  the rows compare against a STRING ("pane:<pid>" |
//                "review:<repo>/<slug>" | "reviews")
// railSelection mirrors the string; setLastSelected stores the object.
//
// Drag identity travels on the drag descriptor captured at dragStart (kind +
// key + trackKey) — NEVER a previousElementSibling DOM walk, which breaks under
// a component tree (#6). The reorder splices run against currentMergedOrder()
// (the same merged tree the render uses), not raw prefs. Two drag kinds:
// "track" (top-level reorder) and "pane" (reorder within a track, or move a tab
// to another track by POST /api/tracks/move-tab?track_id=). The id is a QUERY
// param, never a path segment — a repo-default id is a repo PATH (invariant #6).
import { useEffect, useRef, useState } from "preact/hooks";
import { passesFilter } from "../filter.js";
import { confirmDialog } from "../overlays/Dialog.jsx";
import { modalRequest } from "../overlays/modalRequest.js";
import { showToast } from "../overlays/Toast.jsx";
import * as prefs from "../prefs.js";
import { currentFilter, dragState, editor as editorSignal, projects, railSelection, reviews, tracks, windows, workspaces } from "../store.js";
import { track } from "../track.js";
import { apiCall, paneLabel, targetQuery } from "../util.js";
import { ActivitySection, NextQueue, PinnedSection } from "./AttentionSections.jsx";
import { nextEntries, selectReview, selectReviewsQueue } from "./alertFeed.js";
import { paneBlip } from "./attention.js";
import { railFocused, railHovered } from "./layoutFreeze.js";
import { BranchRow, PaneRow, ReviewRow, TrackRow } from "./RailRows.jsx";
import { mergeLiveAndPrefs, paneChip, reviewRowsByTrack, trackKind, trackLabel } from "./railTree.js";
import { SectionHeader } from "./SectionHeader.jsx";
import { SonarStrip } from "./SonarStrip.jsx";

// Bridge to the launcher modal. A track header's + opens it via
// window.__periscopeOpenLauncher; no-op if nothing is wired yet.
function openLauncher(key) {
  const fn = window.__periscopeOpenLauncher;
  if (typeof fn === "function") fn(key);
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

  const liveIds = [...new Set(live.map(w => w.track_id))];
  const registryIds = (tracks.value || []).map(t => t.id);
  prefs.patchUI({
    track_order: nextTrackOrder,
    tabs_by_track: nextTabsByTrack,
  }, `sync live=${JSON.stringify(liveIds)} registry=${JSON.stringify(registryIds)} cached=${JSON.stringify(prefTrackOrder)}`);
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
  await prefs.setTrackOrder(order, `drag ${dragged} ${insertAfter ? "after" : "before"} ${target}`);
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
    body: JSON.stringify({ pid: w.pid }),
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
  return false;   // no other row kind is draggable
}

export function Rail() {
  // Drag descriptor (kind/key/trackKey/childKey) — captured on dragStart, read
  // on dragOver/drop. A ref, not state: it must not trigger re-renders mid-drag.
  const drag = useRef(null);
  // Drop indicator: { key, pos:"before"|"after" }.
  const [dropTarget, setDropTarget] = useState(null);
  // Track scope (transient — a momentary lens, not a pref): clicking a track's
  // name in the sonar strip shows only that track in the tree.
  const [trackScope, setTrackScope] = useState("all");

  // Reading these signals subscribes the component → re-render each poll.
  const live = windows.value;
  const filter = currentFilter.value;
  const selectedKey = railSelection.value;
  // Subscribe to prefs so collapse/order changes re-render.
  const _prefsBlob = prefs.prefsSignal.value;

  // Reconcile prefs ⇄ live (throttled). Side-effecting read; fine here.
  syncRailPrefs();

  const collapsed = prefs.getRailCollapsed();
  const tracksCollapsed = collapsed["sec:projects"] === true;
  // Across-all-windows pid → window map (the flat per-track tab lists).
  const windowsByPid = {};
  for (const w of live) windowsByPid[w.pid] = w;
  // The panes Next lists as asking, and since when. The rail's one paneBlip
  // takes its asks from them, and the strip, the Next cards' stripes, PINNED
  // and the tree rows all read it, so they can't disagree about a pane's state.
  const waitingSince = new Map(nextEntries.value.filter((e) => e.kind === "asks" && e.w).map((e) => [e.w.pid, e.since]));
  const nowS = Math.floor(Date.now() / 1000);
  const blipOf = (w) => paneBlip(w, nowS, waitingSince.has(w.pid));
  // Registry rows feed EMPTY goal tracks into the tree.
  const allTracks = tracks.value;
  const trackOf = (w) => trackLabel(w.track_id, live, allTracks);
  const { trackOrder, tabsByTrack, rowsByTrack } = mergeLiveAndPrefs(
    live, projects.value, workspaces.value, {
      trackOrder: prefs.getTrackOrder(),
      tabsByTrack: prefs.getTabsByTrack(),
    }, allTracks
  );
  const stripTracks = trackOrder
    .filter((t) => (tabsByTrack[t] || []).length > 0)
    .map((t) => ({
      id: t, label: trackLabel(t, live, allTracks),
      panes: tabsByTrack[t].map((pid) => windowsByPid[pid]).filter(Boolean),
    }));
  // A scope whose track has emptied or gone is cleared, not just bypassed: the
  // strip has no line left to show it, so the track's next pane mustn't narrow
  // the tree again without warning.
  const scoped = stripTracks.some((t) => t.id === trackScope);
  useEffect(() => {
    if (!scoped && trackScope !== "all") setTrackScope("all");
  }, [scoped, trackScope]);
  const treeOrder = scoped ? [trackScope] : trackOrder;

  // Review rows by track → branch (placement rules in railTree).
  const reviewRows = reviewRowsByTrack(trackOrder, tabsByTrack, windowsByPid, reviews.value || []);

  // --- Selection -----------------------------------------------------------
  function selectKey(key) {
    if (key === "reviews") { selectReviewsQueue(); return; }
    if (key.startsWith("review:")) {
      const k = key.slice("review:".length); const i = k.indexOf("/");
      const row = (reviews.value || []).find((r) => r.repo === k.slice(0, i) && r.slug === k.slice(i + 1));
      if (row) selectReview(row.repo, row.branch);
      return;
    }
    railSelection.value = key;       // STRING highlight-key (signal)
    track("pane.focus", { key });
    if (key.startsWith("pane:")) {
      prefs.setLastSelected({ kind: "pane", pid: key.slice("pane:".length) });
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
  // Re-open a pane's Claude session on another subscription. NOT a migration:
  // the server spawns a SECOND pane resuming the same transcript under the
  // target account and leaves this one running, so nothing is lost if the
  // resume doesn't take.
  //
  // Goes through modalRequest, not apiCall: apiCall toasts every failure and
  // returns null, and the one failure this action must READ is the 409 "session
  // looks live" — Claude writes the limit-reached message into the transcript,
  // so a walled pane (the one most worth moving) always trips the server's
  // 60s mtime guard. That 409 becomes a confirm and one forced retry; the
  // `force` parameter bounds the recursion to a single level. Not
  // `danger: true`: the move is additive (the original pane stays open).
  async function movePaneAccount(w, accountId, force = false) {
    const url =
      `/api/pane/move-account?pid=${encodeURIComponent(w.pid)}&account=${encodeURIComponent(accountId)}` +
      (force ? "&force=1" : "");
    const { data, error, reason, status } = await modalRequest("move account", url, { method: "POST" });
    // apiCall used to emit this on every call; keep the instrumentation event.
    track("api:move account", { path: url, method: "POST", ok: !!data });
    if (data?.pid) {
      // Select the pane we just made — same reason /api/open does it: a spawn
      // the user can't see reads as a no-op. It lands in this pane's own
      // track, so the selection moves one row, not across the rail.
      railSelection.value = `pane:${data.pid}`;
      prefs.setLastSelected({ kind: "pane", pid: data.pid });
      return;
    }
    // Prefix pinned by routes/sessions._window_new_resume's mtime guard — change both or neither.
    if (status === 409 && !force && reason?.startsWith("session looks live")) {
      const ok = await confirmDialog(
        `${error}\n\nMove anyway? The original pane stays open; if the session really is mid-turn, the two copies will interleave writes to one transcript.`,
        { okLabel: "Move anyway" }
      );
      if (ok) await movePaneAccount(w, accountId, true);
      return;
    }
    showToast(`move account failed: ${reason || "unknown error"}`, "bad", 6000);
  }
  async function renamePane(w, next) {
    if (!w.target) return;
    await apiCall("rename tab", `/api/rename?${targetQuery(w.target)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: next }),
    });
  }

  // Freeze/release the pane's name against the narrator's auto-rename. The
  // rename above pins on its own (server-side); this is the release, plus a
  // way to keep a generated name you happen to like.
  // The server resolves pid → pane cwd → git toplevel and launches the
  // configured app, so the client sends only the pid. A failure (no editor
  // set, pane not in a repo, launch refused) comes back as a 4xx/5xx whose
  // detail apiCall already toasts.
  async function openInEditor(w) {
    await apiCall("open in editor", "/api/editor/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pid: w.pid }),
    });
  }

  async function toggleNamePin(w) {
    await apiCall("pin tab name", "/api/name-pin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pid: w.pid, pinned: !w.name_pinned }),
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
  // sub-cluster). trackKey is the drag/reorder parent; showBranch is false
  // inside a cluster, whose header names the branch.
  function paneRowsFor(pids, trackKey, showBranch) {
    return pids
      .map((pid) => windowsByPid[pid])
      .filter(Boolean)
      .map((w) => (
        <PaneRow
          key={`pane:${w.pid}`}
          w={w}
          chip={paneChip(w)}
          blip={blipOf(w)}
          showBranch={showBranch}
          selectedKey={selectedKey}
          dim={passesFilter(w, filter)}
          onSelect={selectKey}
          onClose={() => closePane(w)}
          onRename={(next) => renamePane(w, next)}
          onMoveAccount={(acct) => movePaneAccount(w, acct)}
          dragProps={makeDragProps({ kind: "pane", key: `pane:${w.pid}`, childKey: w.pid, trackKey })}
          dropPos={dropPosFor(`pane:${w.pid}`)}
          pinned={prefs.getPinnedPids().includes(w.pid)}
          onTogglePin={() => prefs.togglePin(w.pid)}
          onToggleNamePin={() => toggleNamePin(w)}
          editor={editorSignal.value}
          onOpenInEditor={() => openInEditor(w)}
          waitingSince={waitingSince.get(w.pid)}
        />
      ));
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
      // Layout-freeze latch: while the pointer or keyboard focus is in the
      // rail, the strip and Next hold their membership so a row can't move out
      // from under the cursor mid-click. Only :focus-visible counts, so a
      // button a mouse click focused doesn't keep the rail held once the
      // pointer leaves. See layoutFreeze.js.
      onMouseEnter={() => { railHovered.value = true; }}
      onMouseLeave={() => { railHovered.value = false; }}
      onFocusIn={(e) => { railFocused.value = e.target.matches(":focus-visible"); }}
      onFocusOut={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) railFocused.value = false; }}
    >
      <SonarStrip
        tracks={stripTracks}
        blipOf={blipOf}
        selectedKey={selectedKey}
        scope={trackScope}
        onScope={setTrackScope}
        onSelect={selectKey}
      />
      <NextQueue blipOf={blipOf} trackOf={trackOf} />
      <PinnedSection blipOf={blipOf} trackOf={trackOf} />
      <SectionHeader
        label="TRACKS"
        count={null}
        collapsed={tracksCollapsed}
        onToggle={() => toggleCollapse("sec:projects")}
      />
      {!tracksCollapsed && treeOrder.map((trackId) => {
        const trackKeyStr = `track:${trackId}`;
        const trackCollapsed = collapsed[`repo:${trackId}`] === true;
        const label = trackLabel(trackId, live, allTracks);

        // Filter-dim over ALL the track's tabs.
        const trackWindows = (tabsByTrack[trackId] || []).map((pid) => windowsByPid[pid]).filter(Boolean);
        // `dim` means "matches the filter" (RailRows inverts it). An EMPTY
        // track has no tab to match, and `[].some()` is false — so a freshly
        // created track rendered at .35 opacity under EVERY filter, including
        // `all`, and read as disabled. Emptiness is not a filter miss.
        const trackDim = trackWindows.length === 0
          || trackWindows.some((w) => passesFilter(w, filter));
        // A branch's review rows sit just above its first tab (or inside its
        // cluster), once.
        const reviewsShown = new Set();
        const reviewsFor = (branch) => {
          if (reviewsShown.has(branch)) return null;
          reviewsShown.add(branch);
          return (reviewRows[trackId]?.[branch] || []).map((row) => (
            <ReviewRow key={`review:${row.repo}/${row.slug}`} row={row} selectedKey={selectedKey} onSelect={selectKey} />
          ));
        };

        return (
          <RailFragment key={trackKeyStr}>
            <TrackRow
              kind={trackKind(trackId, live, allTracks)}
              label={label}
              collapsed={trackCollapsed}
              dim={trackDim}
              onToggle={() => toggleCollapse(`repo:${trackId}`)}
              onRename={(next) => renameTrack(trackId, next)}
              onNewTab={() => openLauncher(trackId)}
              onDissolve={() => dissolveTrack(trackId)}
              onTeardown={() => teardownTrack(trackId, label)}
              dragProps={makeDragProps({ kind: "track", key: trackKeyStr })}
              dropPos={dropPosFor(trackKeyStr)}
            />
            {/* ONE card per track (Model B). The track header (TrackRow) is a
                seclabel sitting ABOVE the card; its tabs, with any shared
                branch's cluster in place, live inside this bordered container.
                The launcher's branch picker chooses which branch a new tab
                lands in, so the header's + covers every branch. */}
            {!trackCollapsed && (
              <div
                class={`rail-group rail-track-card${dropPosFor(`card:${trackId}`) ? " drop-into" : ""}`}
                {...makeCardDropProps(trackId)}
              >
                <div class="rail-group-body">
                  {(rowsByTrack[trackId] || []).map((item) => {
                    if (!item.pids) {
                      const w = windowsByPid[item.pid];
                      return (
                        <RailFragment key={`pane:${item.pid}`}>
                          {w && reviewsFor(w.branch || "")}
                          {paneRowsFor([item.pid], trackId, true)}
                        </RailFragment>
                      );
                    }
                    const branchKey = `${trackId}::${item.branch}`;   // track+branch collapse key
                    const branchCollapsed = collapsed[`wt:${branchKey}`] === true;
                    const members = item.pids.map((pid) => windowsByPid[pid]).filter(Boolean);
                    return (
                      <div class="rail-subgroup" key={`wt:${branchKey}`}>
                        <BranchRow
                          label={item.branch}
                          collapsed={branchCollapsed}
                          childCount={item.pids.length}
                          dim={members.some((w) => passesFilter(w, filter))}
                          onToggle={() => toggleCollapse(`wt:${branchKey}`)}
                        />
                        {!branchCollapsed && (
                          <div class="rail-group-body">
                            {reviewsFor(item.branch)}
                            {paneRowsFor(item.pids, trackId, false)}
                          </div>
                        )}
                      </div>
                    );
                  })}
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
