// The /history page's resume states: what the detail offers, and what an
// answer from POST /api/window/new does to the row and the page's run.
import { describe, expect, it } from "vitest";
import { resumeAnswer, resumeOffer } from "../../resume.js";

const LOOKS_LIVE = { status: 409, detail: "session looks live (written 12s ago); wait a minute or pick another" };
const ALREADY = { status: 409, detail: "already resumed in resumes:4" };
const NO_TRANSCRIPT = { status: 404, detail: "Claude has no transcript for session abc" };

describe("resumeOffer", () => {
  it("a resumable row offers Resume and says nothing", () => {
    expect(resumeOffer(null, undefined)).toEqual({ button: "ready", status: "", dashboard: false });
  });

  it("a resume this page ran shows its window and the dashboard, with no button", () => {
    expect(resumeOffer(null, { state: "done", target: "resumes:4" })).toEqual({
      button: "none",
      status: "Resumed in tmux window resumes:4.",
      dashboard: true,
    });
  });

  it("a running resume shows a pending button", () => {
    expect(resumeOffer(null, { state: "pending" }).button).toBe("pending");
  });

  it("no transcript is final: no button", () => {
    const offer = resumeOffer(NO_TRANSCRIPT, undefined);
    expect(offer.button).toBe("none");
    expect(offer.status).toContain("no transcript");
  });

  it("a session resumed elsewhere keeps Resume, says where, and links the dashboard", () => {
    expect(resumeOffer(ALREADY, undefined)).toEqual({
      button: "ready",
      status: "Already resumed in resumes:4.",
      dashboard: true,
    });
  });

  it("a session that looks live keeps Resume, since the refusal lapses after a minute", () => {
    const offer = resumeOffer(LOOKS_LIVE, undefined);
    expect(offer.button).toBe("ready");
    expect(offer.status).toMatch(/^Session looks live/);
    expect(offer.dashboard).toBe(false);
  });

  it("a failed resume keeps Resume and says why", () => {
    expect(resumeOffer(null, { state: "failed", detail: "tmux new-window failed" })).toEqual({
      button: "ready",
      status: "Resume failed: tmux new-window failed.",
      dashboard: false,
    });
  });
});

describe("resumeAnswer", () => {
  it("a 2xx records the window and keeps the row's refusal", () => {
    expect(resumeAnswer(200, { target: "resumes:4" }, null)).toEqual({
      run: { state: "done", target: "resumes:4" },
      refusal: null,
      announce: "Resumed in tmux window resumes:4.",
    });
  });

  it("a 404 drops the run and becomes the row's refusal", () => {
    const next = resumeAnswer(404, { detail: NO_TRANSCRIPT.detail }, null);
    expect(next.run).toBeNull();
    expect(next.refusal).toEqual(NO_TRANSCRIPT);
  });

  it("a 409 replaces the row's older refusal", () => {
    const next = resumeAnswer(409, { detail: ALREADY.detail }, LOOKS_LIVE);
    expect(next.run).toBeNull();
    expect(next.refusal).toEqual(ALREADY);
    expect(next.announce).toBe("Resume refused: already resumed in resumes:4.");
  });

  it("another error records a failed run and keeps the row's refusal", () => {
    const next = resumeAnswer(500, { detail: "tmux new-window failed" }, LOOKS_LIVE);
    expect(next.run).toEqual({ state: "failed", detail: "tmux new-window failed" });
    expect(next.refusal).toBe(LOOKS_LIVE);
  });

  it("no answer at all is a failed run", () => {
    expect(resumeAnswer(0, {}, null).run).toEqual({ state: "failed", detail: "periscope did not answer" });
  });

  it("an error with no detail names the status", () => {
    expect(resumeAnswer(502, {}, null).run.detail).toBe("HTTP 502");
  });
});
