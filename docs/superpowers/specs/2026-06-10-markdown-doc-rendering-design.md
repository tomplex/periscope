# Markdown document rendering in the file viewer

## Problem

The file viewer's "Rendered" mode for markdown reuses the transcript renderer
(`static/src/split/markdown.jsx`) and the transcript's `.turn-prose` styles.
Both are built for chat prose, not documents:

- Headings are demoted (`#` → h3 at 15.5px; `##` lands at body-text size), so
  a full document has almost no visual hierarchy.
- Code fences render as plain `<pre>` with no syntax highlighting.
- The hand-rolled parser misses nested lists, multi-line list items, task
  lists, images, strikethrough, and multi-paragraph blockquotes.
- 13.5px full-width text with no max-width measure.

## Design

One parser, two skins. The transcript keeps its compact look; the file viewer
gets document typography. Parser correctness improvements benefit both.

### 1. Parser swap (`static/src/split/markdown.jsx`)

New deps: `mdast-util-from-markdown`, `mdast-util-gfm`,
`micromark-extension-gfm` (eager bundle — the transcript imports this module).

`renderMarkdown(text, opts)` keeps its name and vnode-array return.
`opts = { demote = true, highlight = null, resolveUrl = null }`:

- Internals become an mdast → vnode walker emitting the same `md-*` classes
  the existing CSS targets. `demote: true` (default) preserves today's
  `#` → h3 transcript behavior, so `Transcript.jsx` call sites are unchanged.
- New node types: nested lists, task-list items (disabled
  `<input type="checkbox">`), strikethrough (`<del>`), images
  (`<img class="md-img">`), multi-paragraph blockquotes, table cell alignment
  (mdast `align` → `text-align` style).
- `highlight(code, lang)` — optional callback returning vnodes for fenced
  code; absent → plain text fence (transcript behavior).
- `resolveUrl(src)` — optional callback rewriting image `src` values.
- Raw HTML nodes (`html` in mdast) render as literal text. No innerHTML
  anywhere (existing convention #8).

### 2. Fence highlighting (`static/src/preview/highlightCode.js`)

Lives in the lazy preview chunk alongside CodeMirror — the eager bundle gains
nothing. Exports `highlightCode(code, lang) → vnodes`:

- Alias map onto the lezer parsers already bundled:
  `js/jsx/mjs/javascript`, `ts/tsx/typescript` → `@codemirror/lang-javascript`
  parsers (with jsx/typescript options); `py/python`; `rs/rust`; `json`;
  `css`; `html`; `md/markdown`.
- `highlightTree(parser.parse(code), classHighlighter)` → spans with stable
  `tok-*` classes. ~15 lines of plain CSS map `tok-*` to the One-Dark palette
  used by `PREVIEW_HIGHLIGHT` (plain classes because `HighlightStyle`'s
  CSS-in-JS only mounts with an EditorView).
- Unknown/missing lang → plain text.

### 3. Document skin (`.md-doc` in `static/styles.css`)

`PreviewTabInner` swaps `turn-prose` → `md-doc` on the preview host:

- ~72ch max-width, centered; 15px base; line-height ~1.65.
- Heading scale: h1 ~26px with bottom rule, h2 ~20px, h3 ~17px, h4 ~15px.
  No demotion (`demote: false`).
- Roomier block spacing than transcript; `img { max-width: 100% }`;
  task-list checkboxes; `tok-*` palette rules.

### 4. Relative image resolution

The viewer passes `resolveUrl` backed by the existing
`renderUrl(target, absPath)` helper: relative srcs resolve against the
directory of the doc's resolved path and serve via `/api/fs/render/...`;
absolute `http(s)`/`data:` srcs pass through untouched.

## Testing

New `static/src/split/__tests__/markdown.test.jsx` (vitest;
`preact-render-to-string` added as devDependency). Cases: demote on/off,
nested lists, task lists, multi-paragraph blockquote, strikethrough, image
with and without `resolveUrl`, fence with and without `highlight`, raw HTML
rendered as text, GFM table with alignment.

Manual verification: open a real README in the file viewer (dev server),
check headings/fences/images render; confirm transcript view is visually
unchanged.

## Out of scope

- Raw HTML rendering (kept as literal text).
- Highlighting fences in the transcript view.
- New languages beyond the lezer parsers already bundled.
