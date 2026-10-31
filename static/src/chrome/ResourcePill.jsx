// "▣ 15.5G" pill: memory held by the periscope server, its webview and every
// pane's process tree (resources.py). Click → each owner's memory and CPU,
// panes heaviest first, then everything else on the machine and the machine's
// load / swap / free memory. Hidden when the server sent no measurement (its
// process snapshot failed that tick).
import { useCallback, useRef, useState } from "preact/hooks";
import { usePopoverDismiss } from "../hooks/usePopoverDismiss.js";
import { selectPane } from "../split/alertFeed.js";
import { resources, windows } from "../store.js";
import { paneLabel } from "../util.js";
import { fmtCpu, fmtMem, machineLine, ownedMb, paneLines } from "./resources.js";

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
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  usePopoverDismiss(ref, open, close);

  return (
    <div class="pill-dd" ref={ref}>
      <button
        type="button"
        class="res-pill"
        title="memory held by periscope's server, its webview and its panes — click for each one's memory and CPU"
        aria-haspopup="dialog"
        aria-expanded={open ? "true" : "false"}
        onClick={() => setOpen(!open)}
      >
        ▣ {fmtMem(ownedMb(r))}
      </button>
      <div class="tb-dd-menu res-menu" role="dialog" hidden={!open}>
        <Line label="periscope server" group={r.server} />
        {r.webview && <Line label="webview" group={r.webview} />}
        {paneLines(windows.value || []).map(({ w, mem_mb, cpu_pct }) => (
          <Line
            key={w.pid}
            label={paneLabel(w)}
            group={{ mem_mb, cpu_pct }}
            onClick={() => { selectPane(w); close(); }}
          />
        ))}
        <Line label="elsewhere" group={r.elsewhere} />
        <div class="res-machine">{machineLine(r.machine)}</div>
      </div>
    </div>
  );
}

export function ResourcePill() {
  const r = resources.value;
  return r && <ResourcePopover r={r} />;
}
