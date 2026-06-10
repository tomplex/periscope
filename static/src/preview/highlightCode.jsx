// Fence highlighting for the file viewer's rendered-markdown mode, using the
// lezer parsers already bundled for CodeMirror source view. classHighlighter
// emits stable tok-* classes (plain CSS in styles.css) — HighlightStyle's
// CSS-in-JS only mounts with an EditorView, so it can't be reused here.
//
// Lives in the lazy preview chunk (see manualChunks in vite.config.js) so the
// eager bundle gains nothing.
import { highlightTree, classHighlighter } from "@lezer/highlight";
import {
  javascriptLanguage,
  jsxLanguage,
  typescriptLanguage,
  tsxLanguage,
} from "@codemirror/lang-javascript";
import { pythonLanguage } from "@codemirror/lang-python";
import { rustLanguage } from "@codemirror/lang-rust";
import { jsonLanguage } from "@codemirror/lang-json";
import { cssLanguage } from "@codemirror/lang-css";
import { htmlLanguage } from "@codemirror/lang-html";
import { markdownLanguage } from "@codemirror/lang-markdown";

const LANGS = {
  js: javascriptLanguage,
  mjs: javascriptLanguage,
  cjs: javascriptLanguage,
  javascript: javascriptLanguage,
  jsx: jsxLanguage,
  ts: typescriptLanguage,
  typescript: typescriptLanguage,
  tsx: tsxLanguage,
  py: pythonLanguage,
  python: pythonLanguage,
  rs: rustLanguage,
  rust: rustLanguage,
  json: jsonLanguage,
  css: cssLanguage,
  html: htmlLanguage,
  md: markdownLanguage,
  markdown: markdownLanguage,
};

// code string + fence lang -> vnode array (spans with tok-* classes).
// highlightTree only calls back for highlighted ranges — the gaps (plain
// identifiers, whitespace) must be emitted as text or they vanish.
export function highlightCode(code, lang) {
  const language = LANGS[(lang || "").toLowerCase()];
  if (!language) return code;
  const tree = language.parser.parse(code);
  const out = [];
  let pos = 0;
  highlightTree(tree, classHighlighter, (from, to, classes) => {
    if (from > pos) out.push(code.slice(pos, from));
    out.push(<span class={classes} key={out.length}>{code.slice(from, to)}</span>);
    pos = to;
  });
  if (pos < code.length) out.push(code.slice(pos));
  return out;
}
