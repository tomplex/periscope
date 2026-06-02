// Minimal markdown -> Preact vnodes. No innerHTML (convention #8), so this
// renders the subset Claude actually emits — fenced code, inline code, bold,
// italic, links, headings, ordered/unordered lists, blockquotes, hr, hard line
// breaks — directly to vnodes. Not a full CommonMark parser; pragmatic and
// good enough for transcript prose. Nested formatting inside code spans is not
// processed (code is verbatim), matching markdown semantics.

// First inline token: code | bold | italic | link. Code first so * / _ inside
// backticks stay literal.
const INLINE_RE =
  /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\n]+\*)|(_[^_\n]+_)|(\[[^\]]+\]\([^)\s]+\))/;

function renderInline(text) {
  const out = [];
  let rest = String(text);
  let guard = 0;
  while (rest && guard++ < 500) {
    const m = rest.match(INLINE_RE);
    if (!m) {
      out.push(rest);
      break;
    }
    if (m.index > 0) out.push(rest.slice(0, m.index));
    const tok = m[0];
    if (tok[0] === "`") {
      out.push(<code class="md-icode">{tok.slice(1, -1)}</code>);
    } else if (tok.startsWith("**") || tok.startsWith("__")) {
      out.push(<strong>{renderInline(tok.slice(2, -2))}</strong>);
    } else if (tok[0] === "*" || tok[0] === "_") {
      out.push(<em>{renderInline(tok.slice(1, -1))}</em>);
    } else {
      const lm = tok.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      out.push(
        <a class="md-link" href={lm[2]} target="_blank" rel="noopener">{lm[1]}</a>
      );
    }
    rest = rest.slice(m.index + tok.length);
  }
  return out;
}

const BLOCK_START_RE = /^```|^#{1,6}\s|^\s*[-*+]\s|^\s*\d+\.\s|^>\s?|^(?:---|\*\*\*|___)\s*$/;

// Render a markdown string into an array of block-level vnodes.
export function renderMarkdown(text) {
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++; // closing fence
      blocks.push(
        <pre class="md-code" key={blocks.length}><code>{buf.join("\n")}</code></pre>
      );
      continue;
    }

    if (!line.trim()) { i++; continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      // Demote so a top-level # doesn't shout inside a turn: # -> h3.
      const lvl = Math.min(h[1].length + 2, 6);
      const Tag = `h${lvl}`;
      blocks.push(
        <Tag class={`md-h md-h${h[1].length}`} key={blocks.length}>{renderInline(h[2])}</Tag>
      );
      i++; continue;
    }

    if (/^(?:---|\*\*\*|___)\s*$/.test(line)) {
      blocks.push(<hr class="md-hr" key={blocks.length} />);
      i++; continue;
    }

    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push(
        <blockquote class="md-quote" key={blocks.length}>{renderInline(buf.join(" "))}</blockquote>
      );
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(<li key={items.length}>{renderInline(lines[i++].replace(/^\s*[-*+]\s+/, ""))}</li>);
      }
      blocks.push(<ul class="md-ul" key={blocks.length}>{items}</ul>);
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(<li key={items.length}>{renderInline(lines[i++].replace(/^\s*\d+\.\s+/, ""))}</li>);
      }
      blocks.push(<ol class="md-ol" key={blocks.length}>{items}</ol>);
      continue;
    }

    // GFM table: a `| … |` row immediately followed by a `|---|---|` separator.
    if (line.includes("|") && i + 1 < lines.length &&
        /-/.test(lines[i + 1]) && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
      const headers = cells(line);
      i += 2; // header + separator
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(cells(lines[i]));
        i++;
      }
      blocks.push(
        <table class="md-table" key={blocks.length}>
          <thead><tr>{headers.map((h, j) => <th key={j}>{renderInline(h)}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>{headers.map((_, j) => <td key={j}>{renderInline(r[j] || "")}</td>)}</tr>
            ))}
          </tbody>
        </table>
      );
      continue;
    }

    // Paragraph: gather contiguous non-blank, non-block-start lines. Preserve
    // hard line breaks within the paragraph (Claude uses them intentionally).
    const buf = [];
    while (i < lines.length && lines[i].trim() && !BLOCK_START_RE.test(lines[i])) {
      buf.push(lines[i++]);
    }
    const inner = [];
    buf.forEach((l, idx) => {
      if (idx) inner.push(<br key={`br${idx}`} />);
      inner.push(...renderInline(l));
    });
    blocks.push(<p class="md-p" key={blocks.length}>{inner}</p>);
  }
  return blocks;
}
