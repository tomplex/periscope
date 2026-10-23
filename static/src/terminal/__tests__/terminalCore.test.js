// The live terminal's sizing glue — the mount's hint, the size handler, the
// reconnect — driven through the REAL terminalCore against stub xterm /
// WebSocket / ResizeObserver globals. sizing.test.js pins the rules; these pin
// the call sites that apply them, each a one-line revert that left every rule
// test green while the bug it fixed came back. The browser is a bad oracle
// here: each case needs a server announcing a resize nobody in the page made.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let env; // what the stubs read (the box, hidden) and record (sockets, sends)

const stubEl = () => ({
  parentElement: null,
  appendChild(child) { child.parentElement = this; },
  remove() { this.parentElement = null; },
  addEventListener() {},
  removeEventListener() {},
  classList: { add() {}, remove() {} },
});

class StubTerminal {
  constructor() {
    this.cols = 80;
    this.rows = 24;
    this.unicode = {};
    this.buffer = { active: { viewportY: 0, baseY: 0 } };
    // Mirrors the vendored setter: a same-value write fires nothing.
    let family = "";
    this.options = {
      get fontFamily() { return family; },
      set fontFamily(v) { if (v !== family) { family = v; env.familyWrites.push(v); } },
    };
    env.term = this;
  }
  resize(cols, rows) { this.cols = cols; this.rows = rows; }
  open() {} loadAddon() {} registerLinkProvider() {} focus() {} reset() {}
  attachCustomWheelEventHandler() {} attachCustomKeyEventHandler() {}
  onData() {} onBell() {} write() {} writeln() {} scrollToBottom() {}
}

class StubSocket {
  static OPEN = 1;
  constructor(url) {
    this.readyState = StubSocket.OPEN;
    const q = new URL(url).searchParams;
    env.sockets.push({ hint: q.has("cols") ? { cols: +q.get("cols"), rows: +q.get("rows") } : null });
    env.ws = this;
    setTimeout(() => this.onopen?.(), 0);
  }
  send(data) { env.sent.push(JSON.parse(data)); }
  close() {}
}

const NoopAddon = class { onContextLoss() {} dispose() {} };

let core;
beforeEach(async () => {
  env = { box: { cols: 143, rows: 36 }, hidden: false, sockets: [], sent: [], ws: null, ro: null, term: null, familyWrites: [] };
  vi.useFakeTimers();
  vi.stubGlobal("Terminal", StubTerminal);
  vi.stubGlobal("WebSocket", StubSocket);
  vi.stubGlobal("FitAddon", { FitAddon: class {
    // Under a display:none host the real FitAddon proposes NaN cols.
    proposeDimensions() { return env.hidden ? { cols: NaN, rows: NaN } : { ...env.box }; }
  } });
  vi.stubGlobal("WebglAddon", { WebglAddon: NoopAddon });
  vi.stubGlobal("SearchAddon", { SearchAddon: NoopAddon });
  vi.stubGlobal("Unicode11Addon", { Unicode11Addon: NoopAddon });
  vi.stubGlobal("ResizeObserver", class { constructor(cb) { env.ro = cb; } observe() {} disconnect() {} });
  vi.stubGlobal("document", { createElement: stubEl });
  vi.stubGlobal("location", { protocol: "http:", host: "periscope.test" });
  vi.stubGlobal("requestAnimationFrame", (cb) => setTimeout(cb, 16));
  // A fresh module per test: the pin and the last measurement are page-lifetime state.
  vi.resetModules();
  core = await import("../terminalCore.js");
});

afterEach(() => {
  core.unmountTerminal();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const mount = async (pane) => {
  core.mountTerminal(stubEl(), pane);
  await vi.advanceTimersByTimeAsync(400); // the deferred refit + scroll
};
const announce = async (cols, rows) => { // the reconcile's {type:"size"}
  env.ws.onmessage({ data: JSON.stringify({ type: "size", cols, rows }) });
  await vi.advanceTimersByTimeAsync(100); // the fit's debounce
};
const resizeBox = async (cols, rows) => {
  env.box = { cols, rows };
  env.ro();
  await vi.advanceTimersByTimeAsync(100);
};
const dropSocket = async () => {
  env.ws.onclose({});
  await vi.advanceTimersByTimeAsync(300); // the first reconnect backoff
};

describe("terminal sizing glue", () => {
  it("asks a pane announced taller than the container back down to it, rows only", async () => {
    // Regression: a sibling closed under a 30-row container and the pane came
    // back 40 rows. Nothing else asks (the container didn't change, so no fit
    // runs), and capped at 30 the pane's rows past it painted onto its last,
    // hiding the prompt behind the status line.
    env.box = { cols: 143, rows: 30 };
    await mount("%1");
    await announce(143, 30);
    expect(env.sent).toEqual([]);
    await announce(143, 40);
    expect(env.term.rows).toBe(30);
    expect(env.sent).toStrictEqual([{ type: "resize", rows: 30 }]);
  });

  it("sends a container shrink below the pane as rows only", async () => {
    // With the pin in it, the shrink also set a shared window's width.
    await mount("%1");
    await announce(143, 36);
    await resizeBox(143, 28);
    expect(env.term.rows).toBe(28);
    expect(env.sent).toStrictEqual([{ type: "resize", rows: 28 }]);
  });

  it("regrows its own shrink after a reconnect made while shrunk", async () => {
    // Regression: an unsplit 239x62 window, the container shortened to 40,
    // and the socket dropped (a periscope restart, sleep/wake) before it grew
    // back. The reconnect's seed forgot the shrink, so the regrow was never
    // sent: a 22-row blank band under Claude's TUI, for every viewer.
    env.box = { cols: 239, rows: 62 };
    await mount("%1");
    await announce(239, 62);
    await resizeBox(239, 40);
    await announce(239, 40);
    await dropSocket();
    await announce(239, 40); // the reconnect's handshake
    await resizeBox(239, 62);
    expect(env.sockets[1].hint).toEqual({ cols: 239, rows: 40 });
    expect(env.sent).toStrictEqual([{ type: "resize", rows: 40 }, { type: "resize", rows: 62 }]);
  });

  it("hints a hidden mount with the container's last measurement", async () => {
    // Regression: a pane selected in transcript mode mounts into a display:none
    // host and can't measure. Unhinted, tmux kept the window's width (another
    // viewer's 230), the capture came in wider than the pin, and every frame
    // autowrapped a junk line into scrollback.
    await mount("%1");
    env.hidden = true;
    await mount("%2");
    expect(env.sockets.map((s) => s.hint)).toEqual([{ cols: 143, rows: 36 }, { cols: 143, rows: 36 }]);
  });

  it("sends no hint before anything has been measured, on a reconnect either", async () => {
    // Regression: a left/right split shown only in transcript mode since page
    // load. Nothing measured, the xterm took the pane's 71 cols, and hinted on
    // each reconnect they shrank the 143-col window to 71, then 35. No dims
    // of ours to ask for: tmux's view stands.
    env.hidden = true;
    await mount("%1");
    await announce(71, 36);
    await dropSocket();
    expect(env.sockets.map((s) => s.hint)).toEqual([null, null]);
    expect(env.sent).toEqual([]);
  });

  it("hints a reconnect on a left/right split with the container, not the pane", async () => {
    // Regression: the xterm holds the pane's dims, a slice of the window.
    // Echoed back as the reconnect hint, they resized the WINDOW to them, and
    // it halved on every retry (143 → 71 → 35 cols).
    await mount("%1");
    await announce(71, 36);
    expect(env.term.cols).toBe(71);
    expect(env.sent).toEqual([]);
    await dropSocket();
    await dropSocket();
    expect(env.sockets.map((s) => s.hint)).toEqual([
      { cols: 143, rows: 36 }, { cols: 143, rows: 36 }, { cols: 143, rows: 36 },
    ]);
  });

  it("asks a left/right split's window for the container's width, never the pane's", async () => {
    // Every size message runs the fit's ask, and term.cols is the pane's 71:
    // compared against the 143 last asked, it read as a width change and sent
    // the pane's width, halving the window on each announcement (71, 35, 17).
    await mount("%1");
    await announce(71, 36);
    await resizeBox(143, 28);
    expect(env.sent).toStrictEqual([{ type: "resize", rows: 28 }]);
    await resizeBox(180, 28);
    expect(env.sent.at(-1)).toStrictEqual({ type: "resize", cols: 180, rows: 28 });
  });
});

describe("terminal webfont re-measure", () => {
  // xterm measures its cell once, at open, usually against the Menlo fallback;
  // the bounce on font load is the only re-measure. Each case is a one-line
  // revert the sizing tests above stay green through.
  let faces;
  const settle = async () => {
    for (const f of faces) f.resolve();
    await vi.advanceTimersByTimeAsync(100); // the fit's debounce
  };
  beforeEach(() => {
    faces = [];
    vi.stubGlobal("document", {
      createElement: stubEl,
      fonts: {
        load(spec) {
          const f = { spec };
          f.promise = new Promise((r) => { f.resolve = r; });
          faces.push(f);
          return f.promise;
        },
      },
    });
  });

  it("waits for the regular and bold faces before re-measuring", async () => {
    // Awaiting only the 400 face left bold glyphs cached in the WebGL atlas
    // in the fallback when the 700 file landed second.
    const size = core.XTERM_OPTIONS.fontSize;
    await mount("%1");
    expect(faces.map((f) => f.spec)).toEqual([`${size}px "IBM Plex Mono"`, `bold ${size}px "IBM Plex Mono"`]);
    faces[0].resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(env.familyWrites).toEqual([]);
    faces[1].resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(env.familyWrites.length).toBeGreaterThan(0);
  });

  it("bounces the family through another value, so the write actually fires", async () => {
    // The setter drops a same-value write: assigning XTERM_OPTIONS.fontFamily
    // alone (what the Terminal was built with) re-measures nothing.
    await mount("%1");
    env.term.options.fontFamily = core.XTERM_OPTIONS.fontFamily; // as constructed
    env.familyWrites.length = 0;
    await settle();
    expect(env.familyWrites).toHaveLength(2);
    expect(env.familyWrites[0]).not.toBe(core.XTERM_OPTIONS.fontFamily);
    expect(env.term.options.fontFamily).toBe(core.XTERM_OPTIONS.fontFamily);
  });

  it("refits after the load without resizing tmux for an in-tolerance wobble", async () => {
    // The real face measures a column or two off the fallback; the pin must
    // absorb it, or every page load reflows the pane's scrollback.
    await mount("%1");
    await announce(143, 36);
    env.box = { cols: 141, rows: 36 };
    await settle();
    expect(env.sent).toEqual([]);
  });
});
