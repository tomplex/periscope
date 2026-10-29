// Render-path tests for the walkthrough page (THI-443). render-to-string runs
// no effects, so these drive the pure WalkthroughView with fixed data, and
// press the pure pieces (ReviewedToggle) by calling them, as hunk.test.jsx does.
import { options } from "preact";
import render from "preact-render-to-string";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CommentForm, commentOutcome, drafts, MemoHunk, ReviewedToggle, reveal, sendHint, WalkthroughView,
} from "../Walkthrough.jsx";
import { parseWalkthrough, stopSig } from "../walkthrough.js";

const DOC = `# Gauge

> branch: feat/x · head: abc · pr: 12

Panes cost more and \`nothing\` said so.

## Adds a gauge
> kind: system-touch · refs: a/one.py, b/missing.py

Prose one. See \`a/one.py:1\` and \`b/missing.py\`.

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
  it("links the PR only when the queue's PR is the one the walkthrough names", () => {
    const pr = { url: "https://github.com/o/r/pull/12", number: 12, state: "merged" };
    expect(view({}, { pr })).toContain(
      '<a class="rq-pr-link is-merged" href="https://github.com/o/r/pull/12" target="_blank" rel="noopener">PR #12</a>',
    );
    const other = view({}, { pr: { ...pr, number: 13 } });
    expect(other).toContain("<span>PR #12</span>");
    expect(other).not.toContain("rq-pr-link");
  });
  it("shows a failed refresh in the fixed header, in a status line that is always there", () => {
    expect(view()).toMatch(/<header class="wt-head">.*<div class="wt-error" role="status"><\/div><\/header>/);
    expect(view({}, { error: "HTTP 500" }))
      .toMatch(/<header class="wt-head">.*<div class="wt-error" role="status">refresh failed: HTTP 500<\/div><\/header>/);
  });
});

describe("<WalkthroughView> sparse documents", () => {
  it("falls back to the branch for the title and leaves out an absent problem statement", () => {
    const html = view({ text: "## Only\n> kind: reuse\n\nBody.\n", stale_by: null });
    expect(html).toContain('<h2 class="wt-title">feat/x</h2>');
    expect(html).not.toContain("wt-problem");
    expect(html).toContain('<span class="wt-toc-title">Only</span>');
    expect(html).toContain("Body.");
    expect(html).not.toContain("wt-empty-stops");
  });
  it("says so when there are no stops", () => {
    const html = view({ text: "" });
    expect(html).toContain('<p class="wt-empty-stops">no stops in this walkthrough</p>');
    expect(html).not.toContain("wt-intro");
    expect(html).not.toContain("wt-progress");
    const problemOnly = view({ text: "# T\n\nJust the problem.\n" });
    expect(problemOnly).toContain('class="md-doc wt-problem"><p class="md-p">Just the problem.</p>');
    expect(problemOnly).toContain("no stops in this walkthrough");
    expect(problemOnly).not.toContain("wt-toc");
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
    expect(html).toContain('<p class="wt-nodiff">no worktree, so code changes are unavailable</p>');
    expect(html).not.toContain("wt-file-count");
  });
  it("blames the worktree for missing code changes only when the header says there is none", () => {
    // A worktree whose diff base couldn't be found: staleness known, diff null.
    const html = view({ diff: null, stale_by: 0 });
    expect(html).toContain('<p class="wt-nodiff">code changes are unavailable</p>');
    expect(html).not.toContain("no worktree");
  });
  it("folds a reviewed stop to its title row, and only while its sig matches", () => {
    const folded = view({}, { reviewed: { "Adds a gauge": firstSig } });
    expect(folded).toContain('class="wt-stop wt-kind-system-touch is-folded"');
    expect(folded).toMatch(/<button type="button" class="wt-fold" aria-expanded="false"/);
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

describe("<WalkthroughView> cites and accessibility", () => {
  it("makes a cite chip live only when the code column has its file", () => {
    const html = view();
    expect(html).toContain('<button type="button" class="md-cite" title="show in the code column">a/one.py:1</button>');
    expect(html).toContain(
      '<button type="button" class="md-cite is-inert" title="not among this stop\'s changes" aria-disabled="true">b/missing.py</button>',
    );
    // No worktree: nothing to scroll to, so every chip is inert.
    const bare = view({ diff: null, stale_by: null });
    expect(count(bare, 'class="md-cite is-inert"')).toBe(2);
    expect(bare).not.toContain('class="md-cite"');
  });
  it("names each stop's code column and describes its toggle by the stop's title", () => {
    const html = view();
    expect(html).toContain('aria-label="files and code changes: Adds a gauge"');
    expect(html).toContain('<h3 class="wt-stop-title" id="stop-adds-a-gauge-title">');
    expect(html).toContain('aria-describedby="stop-adds-a-gauge-title" aria-pressed="false">reviewed</button>');
    expect(html).toContain('aria-describedby="stop-reuses-the-cache-title"');
  });
  it("gives the contents ✓ a name", () => {
    expect(view({}, { reviewed: { "Adds a gauge": firstSig } }))
      .toContain('<span class="wt-toc-done" role="img" aria-label="reviewed">✓</span>');
  });
  it("demotes headings in a stop and its comments below the stop's own h3", () => {
    const text = "# T\n\n## Stop\n> kind: reuse\n\nBody.\n\n### Detail\n";
    const html = view({ text, comments: [{ ts: "t", stop: "Stop", resolved: false, body: "## Fix\n\nthis" }] });
    expect(html).toContain('<h5 class="md-h md-h3">Detail</h5>');
    expect(html).toContain('<h4 class="md-h md-h2">Fix</h4>');
  });
  it("moves focus to the stop a contents entry scrolls to", () => {
    let entry;
    const calls = [];
    const el = { scrollIntoView: (o) => calls.push(["scroll", o.block]), focus: (o) => calls.push(["focus", o.preventScroll]) };
    const prev = options.vnode;
    const hadDoc = "document" in globalThis;
    const prevDoc = globalThis.document;
    options.vnode = (v) => {
      prev?.(v);
      if (v.props?.class === "wt-toc-item wt-kind-system-touch") entry = v;
    };
    globalThis.document = { getElementById: (id) => (id === "stop-adds-a-gauge" ? el : null) };
    try {
      // Focusable by script only, so the stop takes focus without joining the tab order.
      expect(view()).toContain('id="stop-adds-a-gauge" tabindex="-1"');
      entry.props.onClick();
      expect(calls).toEqual([["scroll", "start"], ["focus", true]]);
    } finally {
      options.vnode = prev;
      if (hadDoc) globalThis.document = prevDoc;
      else delete globalThis.document;
    }
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

describe("reveal", () => {
  afterEach(() => { vi.unstubAllGlobals(); });
  function setup(overflowY) {
    vi.stubGlobal("getComputedStyle", () => ({ overflowY }));
    const calls = [];
    const col = { scrollHeight: 900, clientHeight: 300, scrollTo: (o) => calls.push(["column", o.top]) };
    const el = {
      offsetTop: 400,
      scrollIntoView: (o) => calls.push(["page", o.block]),
      getBoundingClientRect: () => ({}),
      classList: { remove: () => {}, add: (k) => calls.push(["flash", k]) },
    };
    return { col, el, calls };
  }
  it("scrolls a column that scrolls on its own, not the page", () => {
    const { col, el, calls } = setup("auto");
    reveal(col, el);
    expect(calls).toEqual([["column", 392], ["flash", "is-flash"]]);
  });
  it("scrolls the page as little as it can when the column is static, however tall", () => {
    const { col, el, calls } = setup("visible");
    reveal(col, el);
    expect(calls).toEqual([["page", "nearest"], ["flash", "is-flash"]]);
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
