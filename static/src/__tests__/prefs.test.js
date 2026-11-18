// Clawd's day log (prefs.noteClawdDay): what the evening is booked from, so
// a note mustn't drop the other kind's, rewrite today's, or write state.json
// again on every re-run of the effect that notes it. Mocked at the network
// boundary: fetch for the boot load, apiCall for the PATCH.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../util.js", () => ({ apiCall: vi.fn() }));
const { apiCall } = await import("../util.js");
const prefs = await import("../prefs.js");

let server;
beforeEach(() => {
  prefs.prefsSignal.value = { loaded: false, ui: {}, windows: {}, commands: [] };
  server = { clawd_merged: "2026-10-21T11:00" };
  vi.stubGlobal("localStorage", { getItem: () => null });
  vi.stubGlobal("fetch", async () => ({ ok: true, json: async () => ({ ui: { ...server } }) }));
  // The server's top-level merge (store.update_ui), echoed back.
  apiCall.mockImplementation(async (_label, _path, opts) => {
    Object.assign(server, JSON.parse(opts.body));
    return { ok: true, ui: { ...server } };
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("noteClawdDay", () => {
  it("writes only its own kind, so it can't drop the other kind's note", async () => {
    await prefs.loadPrefs();
    expect(await prefs.noteClawdDay("furious", "2026-10-21T14:30")).toBe(true);
    expect(apiCall).toHaveBeenCalledTimes(1);
    const [, path, opts] = apiCall.mock.calls[0];
    expect([path, opts.method]).toEqual(["/api/prefs/ui", "PATCH"]);
    expect(JSON.parse(opts.body)).toEqual({ clawd_furious: "2026-10-21T14:30" });
    expect(prefs.getClawdDay()).toEqual({ merged: "2026-10-21T11:00", furious: "2026-10-21T14:30" });
  });

  it("notes each kind once a day: a later note neither writes nor moves the first", async () => {
    await prefs.loadPrefs();
    await prefs.noteClawdDay("furious", "2026-10-21T14:30");
    expect(await prefs.noteClawdDay("furious", "2026-10-21T17:40")).toBe(false);
    expect(await prefs.noteClawdDay("merged", "2026-10-21T17:45")).toBe(false);
    expect(apiCall).toHaveBeenCalledTimes(1);
    expect(prefs.getClawdDay()).toEqual({ merged: "2026-10-21T11:00", furious: "2026-10-21T14:30" });
    expect(await prefs.noteClawdDay("merged", "2026-10-22T09:10")).toBe(true);
    expect(prefs.getClawdDay().merged).toBe("2026-10-22T09:10");
  });

  it("adopts the day's first note the server kept, when another window noted it first", async () => {
    await prefs.loadPrefs();
    // Another window notes 17:29 after this one loaded; the server keeps the
    // day's first (routes/prefs.py).
    server.clawd_furious = "2026-10-21T17:29";
    apiCall.mockImplementation(async (_label, _path, opts) => {
      for (const [k, v] of Object.entries(JSON.parse(opts.body))) {
        if (!(server[k]?.slice(0, 10) === v.slice(0, 10) && server[k] <= v)) server[k] = v;
      }
      return { ok: true, ui: { ...server } };
    });
    expect(await prefs.noteClawdDay("furious", "2026-10-21T17:31")).toBe(true);
    expect(prefs.getClawdDay().furious).toBe("2026-10-21T17:29");
  });

  it("loads first, so a note before the boot load can't rewrite today's", async () => {
    expect(await prefs.noteClawdDay("merged", "2026-10-21T17:40")).toBe(false);
    expect(apiCall).not.toHaveBeenCalled();
    expect(prefs.getClawdDay().merged).toBe("2026-10-21T11:00");
  });
});

describe("notify prefs", () => {
  it("defaults every toggle on", () => {
    expect(prefs.getNotifyPrefs()).toEqual(prefs.NOTIFY_DEFAULTS);
    expect(Object.values(prefs.NOTIFY_DEFAULTS).every(Boolean)).toBe(true);
  });

  it("writes the whole set, so the server's top-level merge keeps the others", async () => {
    await prefs.setNotifyPref("done", false);
    const sent = JSON.parse(apiCall.mock.calls.at(-1)[2].body);
    expect(sent.notify).toEqual({ ...prefs.NOTIFY_DEFAULTS, done: false });
    await prefs.setNotifyPref("usage", false);
    expect(prefs.getNotifyPrefs()).toMatchObject({ done: false, usage: false, ci_failed: true });
  });
});

describe("tab colours", () => {
  const echoColour = async (_label, _path, opts) => ({ ok: true, annotation: { colour: JSON.parse(opts.body).colour || null } });

  it("shows a picked colour at once, saves it on the pane's annotation, and sends \"\" to remove it", async () => {
    await prefs.loadPrefs();
    apiCall.mockImplementation(echoColour);
    const saving = prefs.setTabColour("p1", "sky");
    // The card repaints before the PUT answers.
    expect(prefs.getTabColour("p1")).toBe("sky");
    await saving;
    const [, path, opts] = apiCall.mock.calls[0];
    expect([path, opts.method, JSON.parse(opts.body)]).toEqual(["/api/prefs/windows/p1", "PUT", { colour: "sky" }]);
    await prefs.setTabColour("p1", null);
    expect(JSON.parse(apiCall.mock.calls[1][2].body)).toEqual({ colour: "" });
    expect(prefs.getTabColour("p1")).toBeNull();
  });

  it("puts the colour back when the save fails", async () => {
    await prefs.loadPrefs();
    prefs.prefsSignal.value = { ...prefs.prefsSignal.value, windows: { p1: { colour: "rose" } } };
    apiCall.mockResolvedValue(null);
    expect(await prefs.setTabColour("p1", "moss")).toBe(false);
    expect(prefs.getTabColour("p1")).toBe("rose");
  });

  it("names a colour by the user's name, else by its hue", async () => {
    server = { tab_colour_names: { iris: "ci" } };
    await prefs.loadPrefs();
    expect(prefs.getTabColourName("iris")).toBe("ci");
    expect(prefs.getTabColourName("sky")).toBe("Sky");
  });

  it("sends the names as typed and keeps the server's cleaned reply", async () => {
    await prefs.loadPrefs();
    apiCall.mockResolvedValue({ ok: true, ui: { tab_colour_names: { iris: "ci" } } });
    await prefs.setTabColourNames({ iris: "  ci ", sky: "", moss: "Moss" });
    expect(JSON.parse(apiCall.mock.calls[0][2].body)).toEqual({ tab_colour_names: { iris: "  ci ", sky: "", moss: "Moss" } });
    expect(prefs.getTabColourNames()).toEqual({ iris: "ci" });
  });

  it("reads the tint as Default until one is picked", async () => {
    await prefs.loadPrefs();
    expect(prefs.getTabTint().id).toBe("default");
    await prefs.setTabTint("quiet");
    expect(prefs.getTabTint()).toEqual({ id: "quiet", name: "Quiet", wash: 9 });
  });
});
