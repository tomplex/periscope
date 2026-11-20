// The /history page's resume states, kept pure so vitest can run them
// (static/src/__tests__/historyResume.test.js). Self-contained, like util.js.
//
// `refusal` is the row's `resume_refusal` from /api/history/search: null, or
// { status, detail }. `run` is the page's own record of a resume it started:
// { state: "pending" | "done" | "failed", target, detail }.

const NO_TRANSCRIPT = "Claude has no transcript for this session, so it cannot resume it.";

const sentence = (s) => `${s.charAt(0).toUpperCase()}${s.slice(1)}.`;

// What the detail offers: the Resume button ("ready", "pending" or "none"),
// the status line, and whether to link the dashboard, where the resumed
// window runs.
export function resumeOffer(refusal, run) {
  if (run?.state === "done") return { button: "none", status: `Resumed in tmux window ${run.target}.`, dashboard: true };
  if (run?.state === "pending") return { button: "pending", status: "", dashboard: false };
  if (refusal?.status === 404) return { button: "none", status: NO_TRANSCRIPT, dashboard: false };
  // A 409 lapses (a minute of quiet, the resumed window closing), and the
  // route checks again on every click: say why, and keep the button.
  if (refusal) {
    return { button: "ready", status: sentence(refusal.detail), dashboard: refusal.detail.startsWith("already resumed") };
  }
  return { button: "ready", status: run?.state === "failed" ? sentence(`Resume failed: ${run.detail}`) : "", dashboard: false };
}

// What an answer from POST /api/window/new does: the run to keep (null drops
// it), the row's refusal, and the words for screen readers. `status` 0 means
// periscope did not answer.
export function resumeAnswer(status, body, refusal) {
  if (status >= 200 && status < 300) {
    return { run: { state: "done", target: body.target }, refusal, announce: `Resumed in tmux window ${body.target}.` };
  }
  const detail = body.detail || (status ? `HTTP ${status}` : "periscope did not answer");
  // The server's answer is newer than the row's: it becomes the row's.
  if (status === 404 || status === 409) {
    return { run: null, refusal: { status, detail }, announce: sentence(`Resume refused: ${detail}`) };
  }
  return { run: { state: "failed", detail }, refusal, announce: sentence(`Resume failed: ${detail}`) };
}
