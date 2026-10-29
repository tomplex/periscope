import { options } from "preact";
import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { Mermaid } from "../../split/Mermaid.jsx";
import { renderMarkdown } from "../../split/markdown.jsx";
import { closeLightbox, isBackdropClose, LightboxHost, lightbox, nextFocus, openLightbox } from "../Lightbox.jsx";

afterEach(() => { lightbox.value = null; });

// Vnodes, not a DOM: renderMarkdown/Mermaid return preact vnode trees whose
// children live at `props.children`. Walk that to find one by its class,
// the same shape a real render would hand the browser.
function findByClass(vnode, cls) {
  if (vnode == null || typeof vnode !== "object") return null;
  if (Array.isArray(vnode)) {
    for (const child of vnode) {
      const found = findByClass(child, cls);
      if (found) return found;
    }
    return null;
  }
  if (vnode.props?.class === cls) return vnode;
  return findByClass(vnode.props?.children, cls);
}

describe("lightbox", () => {
  it("renders nothing while closed", () => {
    expect(render(<LightboxHost />)).toBe("");
  });
  it("shows an image at natural size in a focusable scroller with a close button, and closes", () => {
    openLightbox({ kind: "img", src: "https://example.com/a.png", alt: "chart" });
    const html = render(<LightboxHost />);
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain('<img src="https://example.com/a.png" alt="chart"');
    expect(html).toContain('aria-label="close"');
    // The scroller takes focus on open, so the arrow keys scroll a big diagram.
    expect(html).toContain('class="lightbox-body" tabindex="-1"');
    closeLightbox();
    expect(lightbox.value).toBeNull();
  });
  it("gives a diagram an empty host that its effect fills by re-rendering its source", () => {
    openLightbox({ kind: "svg", code: "flowchart LR\n  a --> b" });
    expect(render(<LightboxHost />)).toContain('class="lightbox-svg"');
  });
});

describe("closeLightbox", () => {
  it("refocuses what opened it, if it's still in the document", () => {
    const from = { focus: () => { from.called = true; }, isConnected: true };
    lightbox.value = { kind: "img", src: "x", alt: "", from };
    closeLightbox();
    expect(from.called).toBe(true);
    expect(lightbox.value).toBeNull();
  });
  it("skips focus for an opener that's left the document", () => {
    const from = { focus: () => { throw new Error("must not be called"); }, isConnected: false };
    lightbox.value = { kind: "img", src: "x", alt: "", from };
    closeLightbox();
    expect(lightbox.value).toBeNull();
  });
});

describe("nextFocus", () => {
  it("cycles between the scroller and the close button both ways", () => {
    const body = { name: "body" };
    const close = { name: "close" };
    expect(nextFocus(close, body, close)).toBe(body);
    expect(nextFocus(body, body, close)).toBe(close);
  });
});

describe("isBackdropClose", () => {
  it("closes only when the mousedown, the mouseup and the click all land on the backdrop", () => {
    expect(isBackdropClose(true, true, true)).toBe(true);
  });
  it("stays open for a drag that starts in the content and ends on the backdrop", () => {
    expect(isBackdropClose(false, true, true)).toBe(false);
  });
  it("stays open for a drag that starts on the backdrop and ends in the content", () => {
    expect(isBackdropClose(true, false, true)).toBe(false);
  });
});

describe("markdown zoom -> lightbox", () => {
  it("opens an image at its resolved src when its zoom button is pressed", () => {
    const out = renderMarkdown("![chart](a.png)", { zoom: true, resolveUrl: (u) => `/r/${u}` });
    const btn = findByClass(out, "md-img-zoom");
    expect(btn).toBeTruthy();
    btn.props.onClick();
    expect(lightbox.value).toMatchObject({ kind: "img", src: "/r/a.png", alt: "chart" });
  });
});

describe("Mermaid's own zoom", () => {
  let prevVnode;
  afterEach(() => { options.vnode = prevVnode; });

  it("opens the diagram's own source, and ignores a click on a link inside it", () => {
    let btn;
    let host;
    // options.vnode fires for every vnode h() creates, render-to-string
    // included, so this is how to reach a handler a render-to-string pass
    // would otherwise discard.
    prevVnode = options.vnode;
    options.vnode = (v) => {
      if (v.props?.class === "md-mermaid-zoom") btn = v;
      if (v.props?.class === "md-mermaid-svg is-zoomable") host = v;
    };
    render(<Mermaid code={"flowchart LR\n  a-->b"} zoom />);

    btn.props.onClick();
    expect(lightbox.value).toMatchObject({ kind: "svg", code: "flowchart LR\n  a-->b" });

    lightbox.value = null;
    host.props.onClick({ target: { closest: () => ({}) } }); // a link under the click
    expect(lightbox.value).toBeNull();

    host.props.onClick({ target: { closest: () => null } }); // no link under the click
    expect(lightbox.value).toMatchObject({ kind: "svg", code: "flowchart LR\n  a-->b" });
  });
});
