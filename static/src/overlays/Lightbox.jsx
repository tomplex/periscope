// Full-size view for a walkthrough diagram or image (THI-443 D4). One host,
// mounted at the app root by Overlays.jsx, and one signal: a zoomable diagram
// or image calls openLightbox, and the host draws it at natural size over a
// backdrop that scrolls both ways.
import { signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { renderDiagram } from "../split/mermaidRender.js";

// { kind: "svg", code } | { kind: "img", src, alt }, plus `from`: the element
// that opened it, refocused on close so keyboard users land where they were.
// A diagram carries its source, not its drawn SVG: copying the host's
// innerHTML duplicated the SVG's ids, so the copy's arrowheads bound to the
// original's <marker>s and vanished once that host was hidden.
export const lightbox = signal(null);

export function openLightbox(item) {
  lightbox.value = { ...item, from: typeof document === "undefined" ? null : document.activeElement };
}

export function closeLightbox() {
  const from = lightbox.value?.from;
  lightbox.value = null;
  // Only if it's still on the page — a card or banner that opened this and
  // was removed underneath it shouldn't yank focus onto a detached node.
  if (from?.isConnected) from.focus?.();
}

// Pure Tab trap: the dialog has exactly two focusable stops (the scroller,
// ✕); Tab always moves to the other one.
export function nextFocus(active, body, close) {
  return active === close ? body : close;
}

export function LightboxHost() {
  const item = lightbox.value;
  const body = useRef(null);
  const svgHost = useRef(null);
  const closeBtn = useRef(null);
  const down = useRef(false); // mousedown landed on the backdrop itself, not a drag out of the content
  useEscape(closeLightbox, !!item);
  useEffect(() => {
    if (!item) return;
    // The scroller, not ✕: arrow keys and Page Down then scroll a big diagram.
    body.current?.focus();
    if (item.kind !== "svg") return;
    let alive = true;
    renderDiagram(item.code)
      .then(({ svg: markup }) => {
        if (!alive || !svgHost.current) return;
        // Only ever mermaid's own sanitized SVG (strict mode runs DOMPurify)
        // — the convention #8 exception, as in Mermaid.jsx.
        svgHost.current.innerHTML = markup;
        const svg = svgHost.current.querySelector("svg");
        const vb = svg?.viewBox?.baseVal;
        if (svg && vb?.width) {
          // Natural size: mermaid's inline max-width and width="100%" would refit it.
          svg.style.maxWidth = "none";
          svg.setAttribute("width", String(Math.round(vb.width)));
          svg.setAttribute("height", String(Math.round(vb.height)));
        }
      })
      .catch(() => {}); // it already drew once on the page; a re-render failure here is inert
    return () => { alive = false; };
  }, [item]);
  if (!item) return null;
  // Close only on a plain click of the backdrop: the mousedown that started
  // the gesture must have landed there too, or dragging a selection out past
  // the content (mouseup over the padding) would close the lightbox.
  const onMouseDown = (e) => { down.current = e.target === e.currentTarget; };
  const onBackdrop = (e) => { if (down.current && e.target === e.currentTarget) closeLightbox(); };
  // aria-modal promises focus stays in here: Tab moves between the scroller and ✕.
  const onKeyDown = (e) => {
    if (e.key !== "Tab") return;
    e.preventDefault();
    nextFocus(document.activeElement, body.current, closeBtn.current)?.focus();
  };
  return (
    <div class="lightbox" role="dialog" aria-modal="true" aria-label={item.kind === "img" ? "image, full size" : "diagram, full size"}
      onKeyDown={onKeyDown}>
      <button type="button" class="lightbox-close" ref={closeBtn} aria-label="close" onClick={closeLightbox}>✕</button>
      <div class="lightbox-body" ref={body} tabIndex={-1} onMouseDown={onMouseDown} onClick={onBackdrop}>
        {item.kind === "img" ? <img src={item.src} alt={item.alt} /> : <div class="lightbox-svg" ref={svgHost} />}
      </div>
    </div>
  );
}
