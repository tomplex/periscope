// A ```mermaid fence → inline SVG. Until it draws the host is empty; if the
// load or the source fails, the fence shows as text so nothing is ever lost.
import { useEffect, useRef, useState } from "preact/hooks";
import { openLightbox } from "../overlays/Lightbox.jsx";
import { renderDiagram } from "./mermaidRender.js";

export function Mermaid({ code, zoom = false, label }) {
  const host = useRef(null);
  const [error, setError] = useState(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let alive = true;
    setError(null);
    setReady(false);
    renderDiagram(code)
      .then(({ svg }) => {
        if (!alive || !host.current) return;
        host.current.innerHTML = svg;
        setReady(true);
      })
      .catch((e) => {
        if (!alive) return;
        if (host.current) host.current.innerHTML = "";
        setError(String(e?.message || e));
      });
    return () => { alive = false; };
  }, [code]);
  // Re-render from source for a fresh id (see Lightbox.jsx): copying this
  // host's innerHTML duplicated the SVG's ids, so the lightbox's arrowheads
  // bound to the original's <marker>s and vanished once this one hid.
  const open = () => openLightbox({ kind: "svg", code });
  const canZoom = zoom && !error;
  return (
    <div class="md-mermaid">
      {/* innerHTML is only ever mermaid's own SVG output, never document text.
          The source can be any previewed markdown; strict mode runs DOMPurify
          on the SVG — the one exception to convention #8. */}
      {/* zoom (the walkthrough): a click on the diagram opens it full size, and
          the button is the keyboard and screen-reader way in. role="button" on
          the diagram itself would hide its text labels from screen readers.
          A click on a link inside the diagram (e.g. a file node) must only
          follow that link, not also pop the lightbox. */}
      <div
        ref={host}
        class={`md-mermaid-svg${canZoom ? " is-zoomable" : ""}`}
        onClick={canZoom ? (e) => { if (!e.target.closest?.("a")) open(); } : undefined}
      />
      {canZoom ? (
        <button
          type="button"
          class="md-mermaid-zoom"
          disabled={!ready}
          aria-label={label ? `open diagram full size: ${label}` : "open diagram full size"}
          title="open full size"
          onClick={open}
        >
          ⤢
        </button>
      ) : null}
      {error ? <pre class="md-code md-mermaid-err">{code}{"\n\n"}{error}</pre> : null}
    </div>
  );
}
