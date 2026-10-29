// Full-size view for a walkthrough diagram or image (THI-443 D4). One host,
// mounted at the app root by Overlays.jsx, and one signal: a zoomable diagram
// or image calls openLightbox, and the host draws it at natural size over a
// backdrop that scrolls both ways.
import { signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";

// { kind: "svg", svg } | { kind: "img", src, alt }, plus `from`: the element
// that opened it, refocused on close so keyboard users land where they were.
export const lightbox = signal(null);

export function openLightbox(item) {
  lightbox.value = { ...item, from: typeof document === "undefined" ? null : document.activeElement };
}

export function closeLightbox() {
  const from = lightbox.value?.from;
  lightbox.value = null;
  from?.focus?.();
}

export function LightboxHost() {
  const item = lightbox.value;
  const body = useRef(null);
  const svgHost = useRef(null);
  const closeBtn = useRef(null);
  useEscape(closeLightbox, !!item);
  useEffect(() => {
    if (!item) return;
    // The scroller, not ✕: arrow keys and Page Down then scroll a big diagram.
    body.current?.focus();
    if (item.kind !== "svg" || !svgHost.current) return;
    // Only ever mermaid's own sanitized SVG, copied from a rendered diagram
    // (Mermaid.jsx's innerHTML rule).
    svgHost.current.innerHTML = item.svg;
    const svg = svgHost.current.querySelector("svg");
    const vb = svg?.viewBox?.baseVal;
    if (svg && vb?.width) {
      // Natural size: mermaid's inline max-width and width="100%" would refit it.
      svg.style.maxWidth = "none";
      svg.setAttribute("width", String(Math.round(vb.width)));
      svg.setAttribute("height", String(Math.round(vb.height)));
    }
  }, [item]);
  if (!item) return null;
  const onBackdrop = (e) => { if (e.target === e.currentTarget) closeLightbox(); };
  // aria-modal promises focus stays in here: Tab moves between the scroller and ✕.
  const onKeyDown = (e) => {
    if (e.key !== "Tab") return;
    e.preventDefault();
    (document.activeElement === closeBtn.current ? body.current : closeBtn.current)?.focus();
  };
  return (
    <div class="lightbox" role="dialog" aria-modal="true" aria-label={item.kind === "img" ? "image, full size" : "diagram, full size"}
      onKeyDown={onKeyDown}>
      <button type="button" class="lightbox-close" ref={closeBtn} aria-label="close" onClick={closeLightbox}>✕</button>
      <div class="lightbox-body" ref={body} tabIndex={-1} onClick={onBackdrop}>
        {item.kind === "img" ? <img src={item.src} alt={item.alt} /> : <div class="lightbox-svg" ref={svgHost} />}
      </div>
    </div>
  );
}
