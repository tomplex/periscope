// "▣ 15.5G" pill: memory held by the periscope server, its webview and every
// pane's process tree (resources.py). Click → each owner's memory and CPU,
// panes heaviest first, then everything else on the machine and the machine's
// load / swap / free memory. Hidden when the server sent no measurement (its
// process snapshot failed that tick).
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { selectPane } from "../split/alertFeed.js";
import { resources, windows } from "../store.js";
import { paneLabel } from "../util.js";
import { fmtCpu, fmtMem, machineLine, ownedMb, paneLines } from "./resources.js";

const CPU_TIP = "CPU per core: 100% is one core fully busy";

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

export function ResourcePill() {
  const ref = useRef(null);
  const [open, setOpen] = useState(false);
  const r = resources.value;
  const close = useCallback(() => setOpen(false), []);
  useEscape(close, open);
  useEffect(() => {
    if (!open) return;
    function onDocClick(e) {
      if (ref.current && !ref.current.contains(e.target)) close();
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);
  // The pill can vanish with the popover open (a tick with no measurement);
  // close it, or Escape stays bound to an invisible popover — returning null
  // below doesn't unmount this component, so the open state would survive.
  useEffect(() => {
    if (!r && open) close();
  }, [r, open]);
  if (!r) return null;

  return (
    <div class="res-dd" ref={ref}>
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
