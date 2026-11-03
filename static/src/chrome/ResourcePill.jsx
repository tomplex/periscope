// "▣ 15.5G" pill: memory held by the periscope server, its webview and every
// pane's process tree (resources.py). Click → each owner's memory and CPU,
// panes heaviest first, then everything else on the machine, the machine's
// load / swap / free memory, and what a dashboard poll costs. Hidden when
// the server sent no measurement (its process snapshot failed that tick).
import { useCallback, useRef, useState } from "preact/hooks";
import { usePopoverDismiss } from "../hooks/usePopoverDismiss.js";
import { selectPane } from "../split/alertFeed.js";
import { buildStats, resources, windows } from "../store.js";
import { paneLabel } from "../util.js";
import { buildStatsLine, fmtCpu, fmtMem, machineLine, ownedMb, paneLines, slowestLine, topForksLine } from "./resources.js";

const CPU_TIP = "share of the whole machine's CPU: 100% is every core busy";

function Line({ label, group, onClick }) {
  const children = (
    <>
      <span class="res-label">{label}</span>
      <span class="res-mem">{fmtMem(group.mem_mb)}</span>
      <span class="res-cpu" title={CPU_TIP}>{fmtCpu(group.cpu_pct)}</span>
    </>
  );
  if (onClick) {
    return (
      <button type="button" class="res-line is-pane" onClick={onClick}>
        {children}
      </button>
    );
  }
  return <div class="res-line">{children}</div>;
}

// The popover's open state lives here, not in <ResourcePill>, so a tick with
// no measurement (the pill vanishing) unmounts this and discards it — no
// separate close-when-hidden effect needed.
function ResourcePopover({ r }) {
  const ref = useRef(null);
  const btnRef = useRef(null);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => {
    // Hiding the menu drops focus to <body> unless it moves first — send it
    // back to the pill when a menu item or the pane list had it.
    const hadFocus = ref.current?.contains(document.activeElement);
    setOpen(false);
    if (hadFocus) btnRef.current?.focus();
  }, []);
  usePopoverDismiss(ref, open, close);
  const bs = buildStats.value;

  return (
    <div class="pill-dd" ref={ref}>
      <button
        type="button"
        class="res-pill"
        ref={btnRef}
        title="memory held by periscope's server, its webview and its panes — click for each one's memory and CPU"
        aria-haspopup="dialog"
        aria-expanded={open ? "true" : "false"}
        onClick={() => setOpen(!open)}
      >
        ▣ {fmtMem(ownedMb(r))}
      </button>
      <div class="tb-dd-menu res-menu" role="dialog" aria-label="resource usage" hidden={!open}>
        <Line label="periscope server" group={r.server} />
        {r.webview && <Line label="webview" group={r.webview} />}
        <Line label="all panes" group={r.panes} />
        <div class="res-pane-lines">
          {paneLines(windows.value || []).map(({ w, mem_mb, cpu_pct }) => (
            <Line
              key={w.pid}
              label={paneLabel(w)}
              group={{ mem_mb, cpu_pct }}
              onClick={() => { selectPane(w); close(); }}
            />
          ))}
        </div>
        <Line label="elsewhere" group={r.elsewhere} />
        <div class="res-machine">{machineLine(r.machine)}</div>
        {bs && (
          <div class="res-poll">
            <div class="res-poll-line">{buildStatsLine(bs)}</div>
            {slowestLine(bs) && <div class="res-poll-line res-poll-detail">{slowestLine(bs)}</div>}
            {topForksLine(bs) && <div class="res-poll-line res-poll-detail">{topForksLine(bs)}</div>}
          </div>
        )}
      </div>
    </div>
  );
}

export function ResourcePill() {
  const r = resources.value;
  return r && <ResourcePopover r={r} />;
}
