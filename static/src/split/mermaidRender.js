// DOM side of a ```mermaid fence: lazy-load mermaid (its own ~3 MB vite
// chunk, see vite.config.js mermaidOnly, pulled in on the first diagram
// only), convert periscope's oklch tokens to the hex mermaid's color math
// needs, and render. Kept out of Mermaid.jsx so the component stays a thin
// host; nothing here runs under render-to-string.
import { mermaidTheme } from "./mermaidTheme.js";

const tokenValue = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

let paint = null;
// Paint a token on a 1×1 canvas and read the pixel back: the portable way to
// turn the stylesheet's oklch() into the #rrggbb mermaid's color parser
// takes. undefined (not black) for an empty token, so mermaidTheme drops the
// key instead of painting it #000000.
function tokenHex(name) {
  const v = tokenValue(name);
  if (!v) return undefined;
  if (!paint) paint = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  paint.clearRect(0, 0, 1, 1);
  paint.fillStyle = "#000";
  paint.fillStyle = v;
  paint.fillRect(0, 0, 1, 1);
  const [r, g, b] = paint.getImageData(0, 0, 1, 1).data;
  return `#${[r, g, b].map((x) => x.toString(16).padStart(2, "0")).join("")}`;
}

let mermaidP = null;
let themedFont = null; // the --sans mermaid was last initialized with

// Theme first, then the fixed options: a theme key can never shadow
// securityLevel/suppressErrorRendering.
function initialize(mermaid, font) {
  themedFont = font;
  mermaid.initialize({
    ...mermaidTheme({ color: tokenHex, font }),
    startOnLoad: false,
    securityLevel: "strict",
    suppressErrorRendering: true,
  });
}

function loadMermaid() {
  if (!mermaidP) {
    mermaidP = import("mermaid").then((m) => {
      const mermaid = m.default;
      // Periscope's own tokens on mermaid's base theme (THI-443 D4): the
      // dashboard has one dark theme, so following prefers-color-scheme
      // would only ever draw a light diagram on the dark UI.
      // Without suppressErrorRendering, render() on bad source draws the "Syntax
      // error" bomb SVG into a temp div on document.body and throws before
      // removing it, leaving a stray error diagram on the dashboard.
      initialize(mermaid, tokenValue("--sans"));
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

// Render mermaid source into {svg, bindFunctions}. Waits for the UI font so
// mermaid measures label boxes against the font it will actually draw with
// (terminal/terminalCore.js awaits the same font before xterm measures).
export async function renderDiagram(code) {
  const mermaid = await loadMermaid();
  const font = tokenValue("--sans");
  try {
    // Capped: a stalled woff2 request would otherwise hang every diagram
    // until the browser's own fetch timeout, long after 1.5s stops helping anyone.
    await Promise.race([document.fonts?.load?.(`14px ${font}`), new Promise((r) => setTimeout(r, 1500))]);
  } catch {
    // Font-loading API absence/failure isn't fatal — mermaid falls back to
    // whatever's already loaded.
  }
  // Settings → Fonts can change --sans at runtime; re-theme rather than
  // leave every later diagram on the font mermaid started with.
  if (font !== themedFont) initialize(mermaid, font);
  return mermaid.render(`md-mermaid-${++seq}`, code);
}
