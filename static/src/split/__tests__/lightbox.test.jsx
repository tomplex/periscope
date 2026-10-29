import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { closeLightbox, LightboxHost, lightbox, openLightbox } from "../Lightbox.jsx";

afterEach(() => { lightbox.value = null; });

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
  it("gives a diagram an empty host that its effect fills with the SVG", () => {
    openLightbox({ kind: "svg", svg: "<svg></svg>" });
    expect(render(<LightboxHost />)).toContain('class="lightbox-svg"');
  });
});
