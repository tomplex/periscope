// Render-path tests for the walkthrough page (THI-443). render-to-string runs
// no effects, so these drive the pure WalkthroughView with fixed data, and
// press the pure pieces (ReviewedToggle) by calling them, as hunk.test.jsx does.
import { options } from "preact";
import render from "preact-render-to-string";
import { describe, expect, it } from "vitest";
import { CommentForm, commentOutcome, drafts, MemoHunk, ReviewedToggle, sendHint, WalkthroughView } from "../Walkthrough.jsx";
import { parseWalkthrough, stopSig } from "../walkthrough.js";

const DOC = `# Gauge

> branch: feat/x · head: abc · pr: 12

Panes cost more and \`nothing\` said so.

## Adds a gauge
> kind: system-touch · refs: a/one.py, b/missing.py

Prose one.

\`\`\`mermaid
flowchart LR
  a --> b
\`\`\`

## Reuses the cache
> kind: reuse

Prose two.
`;
const diff = { files: [{ path: "a/one.py", hunks: [{ new_start: 1, lines: [{ kind: "add", text: "x" }] }] }] };
const base = { text: DOC, comments: [], diff, stale_by: 0 };
const view = (over = {}, props = {}) => render(
  <WalkthroughView data={{ ...base, ...over }} repo="fdy" branch="feat/x" pr={null} hl={null}
    reviewed={{}} toggleErrors={{}} error={null} onToggleReviewed={() => {}} onPosted={() => {}} {...props} />,
);
const count = (html, s) => html.split(s).length - 1;
const firstSig = stopSig(parseWalkthrough(DOC).stops[0]);

describe("<WalkthroughView> header", () => {
  it("shows the title, the PR and progress, and no staleness badge when current", () => {
    const html = view();
    expect(html).toContain('<h2 class="wt-title">Gauge</h2>');
    expect(html).toContain("PR #12");
    expect(html).toContain('<span class="wt-progress">0 / 2 reviewed</span>');
    expect(html).not.toContain("wt-badge");
  });
  it("warns when stale and says when there is no worktree", () => {
    expect(view({ stale_by: 3 })).toContain('<span class="wt-badge is-warn">stale: 3 commits</span>');
    expect(view({ stale_by: 1 })).toContain(">stale: 1 commit<");
    expect(view({ stale_by: null, diff: null })).toContain('<span class="wt-badge">no worktree</span>');
  });
  it("says staleness is unknown when the worktree no longer has the recorded head", () => {
    // stale_by null with a diff: the head was rebased away (reviews.stale_by).
    expect(view({ stale_by: null })).toContain('<span class="wt-badge is-warn">stale: unknown</span>');
  });
});

describe("<WalkthroughView> body", () => {
  it("renders the problem statement as markdown", () => {
    expect(view()).toMatch(/class="md-doc wt-problem"><p class="md-p">Panes cost more and <code class="md-icode">nothing<\/code>/);
  });
  it("numbers the contents and the stops, and marks reviewed ones in the contents", () => {
    const html = view({}, { reviewed: { "Adds a gauge": firstSig } });
    expect(html).toContain('<span class="wt-toc-n">1.</span>');
    expect(html).toContain('<span class="wt-toc-n">2.</span>');
    // The ✓ sits on the reviewed entry, and only there.
    expect(html).toMatch(/<span class="wt-toc-title">Adds a gauge<\/span><span class="wt-toc-done"/);
    expect(count(html, 'class="wt-toc-done"')).toBe(1);
    expect(html).toContain('<span class="wt-stop-n">2.</span> Reuses the cache');
  });
  it("colors each card by its kind", () => {
    const html = view();
    expect(html).toContain('class="wt-stop wt-kind-system-touch"');
    expect(html).toContain('class="wt-stop wt-kind-reuse"');
  });
  it("draws a stop's diagrams in their own row, out of the prose", () => {
    const html = view();
    expect(count(html, 'class="wt-diagrams"')).toBe(1);
    expect(count(html, 'class="md-mermaid"')).toBe(1);
    expect(html).toMatch(/class="wt-diagrams"><div class="md-mermaid"><div class="md-mermaid-svg is-zoomable"/);
  });
  it("puts the prose beside the code column, with the hidden files in one line", () => {
    const html = view();
    expect(count(html, 'class="wt-cols"')).toBe(2);
    expect(html).toContain('<span class="wt-file-name">one.py</span>');
    expect(html).toContain('<span class="wt-file-count">1 change</span>');
    expect(html).toContain("<summary>1 not in this diff</summary>");
  });
  it("collapses a file block past 300 lines to a button, and keeps its row and count", () => {
    const lines = (n) => Array.from({ length: n }, () => ({ kind: "add", text: "x" }));
    const big = { files: [{ path: "a/one.py", hunks: [{ new_start: 1, lines: lines(301) }] }] };
    const html = view({ diff: big });
    expect(html).toContain('<span class="wt-file-count">1 change</span>');
    expect(html).toContain('<button type="button" class="wt-ev-more">show 301 lines</button>');
    expect(html).not.toContain('class="dhunk"');
    const edge = { files: [{ path: "a/one.py", hunks: [{ new_start: 1, lines: lines(300) }] }] };
    expect(view({ diff: edge })).not.toContain("wt-ev-more");
    expect(view({ diff: edge })).toContain('class="dhunk"');
  });
  it("lists files without hunks when there is no worktree", () => {
    const html = view({ diff: null, stale_by: null });
    expect(html).toContain('<span class="wt-file-name">missing.py</span>');
    expect(html).toContain("no worktree, so code changes are unavailable");
    expect(html).not.toContain("wt-file-count");
  });
  it("folds a reviewed stop to its title row, and only while its sig matches", () => {
    const folded = view({}, { reviewed: { "Adds a gauge": firstSig } });
    expect(folded).toContain('class="wt-stop wt-kind-system-touch is-folded"');
    expect(folded).toContain('class="wt-fold"');
    expect(folded).not.toContain("Prose one.");
    expect(folded).toContain("Prose two.");
    expect(folded).toContain("1 / 2 reviewed");
    expect(view({}, { reviewed: { "Adds a gauge": "00000000" } })).toContain("Prose one.");
  });
  it("collapses the comment box to a link named for its stop, over one live status line", () => {
    const html = view();
    expect(html).toContain('<button type="button" class="wt-link" aria-label="comment on Adds a gauge">comment</button>');
    expect(html).toContain('<button type="button" class="wt-link" aria-label="comment on Reuses the cache">comment</button>');
    expect(html).not.toContain("<textarea");
    // Present while empty: a live region that mounts with its text is often not announced.
    expect(count(html, '<span class="wt-comment-note" role="status"></span>')).toBe(2);
  });
  it("reopens a stop's box from its draft, and cancel discards the draft", () => {
    const key = "fdy\nfeat/x\nAdds a gauge";
    drafts.set(key, "half a thought");
    let cancel;
    const prev = options.vnode;
    options.vnode = (v) => {
      prev?.(v);
      if (v.props?.class === "wt-link" && v.props.children === "cancel") cancel = v;
    };
    try {
      const html = view();
      expect(html).toContain('<div class="wt-comment-box is-open">');
      expect(html).toContain('aria-label="comment on Adds a gauge">half a thought</textarea>');
      cancel.props.onClick();
      expect(drafts.has(key)).toBe(false);
      expect(view()).not.toContain("<textarea");
    } finally {
      options.vnode = prev;
      drafts.delete(key);
    }
  });
  it("shows a failed mark's error beside its toggle", () => {
    expect(view({}, { toggleErrors: { "Adds a gauge": "not saved: HTTP 500" } }))
      .toContain('<span class="wt-reviewed-err" role="alert">not saved: HTTP 500</span>');
  });
});

describe("<WalkthroughView> comments", () => {
  const comment = (stop, body, resolved = false) => ({ ts: "2026-10-29 10:00", stop, resolved, body });
  const comments = [comment("Adds a gauge", "why?"), comment("Adds a gauge", "fixed", true), comment("Gone stop", "orphan")];
  const stopAt = (html, title) => html.indexOf(`</span> ${title}`);
  it("lists each comment under its stop, and the ones for unknown titles last", () => {
    const html = view({ comments });
    const one = stopAt(html, "Adds a gauge");
    const two = stopAt(html, "Reuses the cache");
    const loose = html.indexOf("unattached comments");
    expect(html.indexOf("why?")).toBeGreaterThan(one);
    expect(html.indexOf("why?")).toBeLessThan(two);
    expect(html.indexOf("fixed")).toBeLessThan(two);
    expect(loose).toBeGreaterThan(two);
    expect(html.indexOf("orphan")).toBeGreaterThan(loose);
    expect(count(html, 'class="wt-comments"')).toBe(2);
  });
  it("puts a folded stop's comment count in its title row, since folding hides the replies", () => {
    const folded = view({ comments }, { reviewed: { "Adds a gauge": firstSig } });
    expect(folded).not.toContain("why?");
    expect(folded).toMatch(/class="wt-stop-head">.*<span class="wt-comment-chip">2 comments · 1 open<\/span>/);
    expect(count(folded, "wt-comment-chip")).toBe(1);
    const resolved = view({ comments: [comment("Adds a gauge", "ok", true)] }, { reviewed: { "Adds a gauge": firstSig } });
    expect(resolved).toContain('<span class="wt-comment-chip">1 comment</span>');
    // Unfolded, the comments are on show and need no chip; folded with none, nothing to count.
    expect(view({ comments })).not.toContain("wt-comment-chip");
    expect(view({}, { reviewed: { "Adds a gauge": firstSig } })).not.toContain("wt-comment-chip");
  });
});

describe("walkthrough pieces", () => {
  it("MemoHunk redraws a hunk only when one of its props changes", () => {
    const props = { hunk: diff.files[0].hunks[0], lang: "python", hl: null, onOpen: null };
    const m = new MemoHunk(props);
    m.props = props;
    expect(m.shouldComponentUpdate({ ...props })).toBe(false);
    expect(m.shouldComponentUpdate({ ...props, hl: () => "" })).toBe(true);
    expect(m.shouldComponentUpdate({ ...props, hunk: { ...props.hunk } })).toBe(true);
  });
  it("ReviewedToggle presses through to onToggle and reports its state", () => {
    let pressed = 0;
    const button = ReviewedToggle({ on: false, onToggle: () => { pressed++; }, error: null }).props.children[1];
    expect(button.props["aria-pressed"]).toBe("false");
    button.props.onClick();
    expect(pressed).toBe(1);
    // One accessible name, "reviewed", in both states: aria-pressed carries the
    // state, and the ✓ is decoration (a changing label plus pressed would be read twice).
    expect(render(<ReviewedToggle on onToggle={() => {}} error={null} />))
      .toContain('aria-pressed="true"><span aria-hidden="true">✓ </span>reviewed</button>');
    expect(render(<ReviewedToggle on={false} onToggle={() => {}} error={null} />))
      .toContain('aria-pressed="false">reviewed</button>');
  });
  it("CommentForm shows the text, the send hint and its buttons, and leaves the note to its box", () => {
    const html = render(
      <CommentForm stop="Adds a gauge" text="why?" busy={false} textRef={null}
        onInput={() => {}} onSend={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain('aria-label="comment on Adds a gauge">why?</textarea>');
    expect(html).toContain('<button type="button" class="wt-btn">send</button>');
    expect(html).toContain('<button type="button" class="wt-link">cancel</button>');
    expect(html).toMatch(/<span class="wt-hint">(⌘|Ctrl\+)↩ to send<\/span>/);
    expect(html).not.toContain("wt-comment-note");
    expect(render(
      <CommentForm stop="S" text="  " busy={false} textRef={null} onInput={() => {}} onSend={() => {}} onCancel={() => {}} />,
    )).toMatch(/<button type="button" class="wt-btn" disabled(="")?>send<\/button>/);
  });
  it("sendHint names the keys for the platform", () => {
    expect(sendHint("MacIntel")).toBe("⌘↩ to send");
    expect(sendHint("Win32")).toBe("Ctrl+↩ to send");
    expect(sendHint(undefined)).toBe("Ctrl+↩ to send");
  });
  it("commentOutcome closes the box on delivery or save, and keeps the text on error", () => {
    expect(commentOutcome({ ok: true, status: 200, data: {} })).toEqual({ close: true, note: "sent to the owning pane" });
    expect(commentOutcome({ ok: false, status: 409, data: { detail: "no pane" } })).toEqual({ close: true, note: "saved, no pane" });
    expect(commentOutcome({ ok: false, status: 500, data: {} })).toEqual({ close: false, note: "HTTP 500" });
  });
  it("commentOutcome reads only a string detail: FastAPI's 422 detail is a list", () => {
    const list = [{ loc: ["body", "stop"], msg: "Field required", type: "missing" }];
    expect(commentOutcome({ ok: false, status: 422, data: { detail: list } })).toEqual({ close: false, note: "HTTP 422" });
    expect(commentOutcome({ ok: false, status: 409, data: { detail: list } })).toEqual({ close: true, note: "saved, not delivered" });
    expect(commentOutcome({ ok: false, status: 400, data: { detail: "bad stop" } })).toEqual({ close: false, note: "bad stop" });
  });
});
