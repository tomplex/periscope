// modalRequest is the no-toast fetch helper; Rail.movePaneAccount relies on
// its failure shape carrying the HTTP status so a 409 can become a confirm.
import { afterEach, describe, expect, it, vi } from "vitest";
import { modalRequest } from "../modalRequest.js";

afterEach(() => {
  vi.unstubAllGlobals();
});

function respond(status, body) {
  vi.stubGlobal("fetch", async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }));
}

describe("modalRequest", () => {
  it("returns {data} on success", async () => {
    respond(200, { pid: "bb22" });
    expect(await modalRequest("move", "/x")).toEqual({ data: { pid: "bb22" } });
  });

  it("returns the route's detail and the status on an HTTP error", async () => {
    respond(409, { detail: "session looks live (written 12s ago); wait a minute or pick another" });
    const r = await modalRequest("move", "/x");
    expect(r.status).toBe(409);
    expect(r.error).toMatch(/^session looks live/);
    expect(r.data).toBeUndefined();
  });

  it("has no status on a network failure", async () => {
    vi.stubGlobal("fetch", async () => { throw new Error("offline"); });
    const r = await modalRequest("move", "/x");
    expect(r.error).toBe("move failed: offline");
    expect(r.status).toBeUndefined();
  });
});
