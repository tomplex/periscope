// A ```mermaid fence → inline SVG. mermaid is ~3 MB and lives in its own
// vite chunk (vite.config.js mermaidOnly), pulled in by this dynamic import
// on the first diagram only. Until it lands the host is empty; if the import
// or the source fails, the fence shows as text so nothing is ever lost.
import { useEffect, useRef, useState } from "preact/hooks";
import { openLightbox } from "./Lightbox.jsx";
import { mermaidTheme } from "./mermaidTheme.js";

const tokenValue = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

let paint = null;
// Paint a token on a 1×1 canvas and read the pixel back: the portable way to
// turn the stylesheet's oklch() into the #rrggbb mermaid's color parser takes.
function tokenHex(name) {
  if (!paint) paint = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  paint.clearRect(0, 0, 1, 1);
  paint.fillStyle = "#000";
  paint.fillStyle = tokenValue(name);
  paint.fillRect(0, 0, 1, 1);
  const [r, g, b] = paint.getImageData(0, 0, 1, 1).data;
  return `#${[r, g, b].map((x) => x.toString(16).padStart(2, "0")).join("")}`;
}

let mermaidP = null;
function loadMermaid() {
  if (!mermaidP) {
    mermaidP = import("mermaid").then((m) => {
      const mermaid = m.default;
      // Periscope's own tokens on mermaid's base theme (THI-443 D4), read once:
      // the dashboard has one dark theme, so following prefers-color-scheme
      // would only ever draw a light diagram on the dark UI.
      // Without suppressErrorRendering, render() on bad source draws the "Syntax
      // error" bomb SVG into a temp div on document.body and throws before
      // removing it, leaving a stray error diagram on the dashboard.
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        suppressErrorRendering: true,
        ...mermaidTheme({ color: tokenHex, font: tokenValue("--sans") }),
      });
      return mermaid;
    }).catch((e) => {
      // Don't cache a failed chunk fetch (a restart can swap dist under an open tab).
      mermaidP = null;
      throw e;
    });
  }
  return mermaidP;
}

let seq = 0;

export function Mermaid({ code, zoom = false }) {
  const host = useRef(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let alive = true;
    setError(null);
    loadMermaid()
      .then((mermaid) => mermaid.render(`md-mermaid-${++seq}`, code))
      .then(({ svg }) => { if (alive && host.current) host.current.innerHTML = svg; })
      .catch((e) => {
        if (!alive) return;
        if (host.current) host.current.innerHTML = "";
        setError(String(e?.message || e));
      });
    return () => { alive = false; };
  }, [code]);
  const open = () => { if (host.current?.innerHTML) openLightbox({ kind: "svg", svg: host.current.innerHTML }); };
  const canZoom = zoom && !error;
  return (
    <div class="md-mermaid">
      {/* innerHTML is only ever mermaid's own SVG output, never document text.
          The source can be any previewed markdown; strict mode runs DOMPurify
          on the SVG — the one exception to convention #8. */}
      {/* zoom (the walkthrough): a click on the diagram opens it full size, and
          the button is the keyboard and screen-reader way in. role="button" on
          the diagram itself would hide its text labels from screen readers. */}
      <div ref={host} class={`md-mermaid-svg${canZoom ? " is-zoomable" : ""}`} onClick={canZoom ? open : undefined} />
      {canZoom ? (
        <button type="button" class="md-mermaid-zoom" aria-label="open diagram full size" title="open full size" onClick={open}>⤢</button>
      ) : null}
      {error ? <pre class="md-code md-mermaid-err">{code}{"\n\n"}{error}</pre> : null}
    </div>
  );
}
