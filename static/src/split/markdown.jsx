// Markdown -> Preact vnodes via mdast (micromark). No innerHTML (convention
// #8) — raw HTML nodes render as literal text. One parser, two skins:
// transcript (demoted headings, soft breaks as <br>) and the file viewer's
// document mode (real heading scale, CommonMark soft breaks, highlighted
// fences, doc-relative URL resolution).
import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { gfm } from "micromark-extension-gfm";
import { openLightbox } from "../overlays/Lightbox.jsx";
import { Mermaid } from "./Mermaid.jsx";
import { metaPairs, parseCite, refTokens, splitCites } from "./mdMeta.js";

// Scheme-qualified, root-absolute, and fragment URLs pass through untouched;
// everything else is doc-relative and goes through ctx.resolveUrl.
const ABSOLUTE_RE = /^([a-z][a-z0-9+.-]*:|\/|#)/i;

function url(raw, ctx) {
  if (!ctx.resolveUrl || ABSOLUTE_RE.test(raw)) return raw;
  return ctx.resolveUrl(raw);
}

function inlineAll(children, ctx) {
  return (children || []).map((c, i) => inline(c, ctx, i));
}

function inline(node, ctx, key) {
  switch (node.type) {
    case "text": {
      const parts = ctx.softBreaks === "br" && node.value.includes("\n")
        ? node.value.split("\n")
        : [node.value];
      const out = [];
      parts.forEach((part, i) => {
        if (i) out.push(<br key={`${key}b${i}`} />);
        if (!ctx.cite) { out.push(part); return; }
        splitCites(part).forEach((seg, j) => {
          if (typeof seg === "string") { out.push(seg); return; }
          const v = ctx.cite(seg);
          out.push(v == null ? seg.raw : <span key={`${key}c${i}-${j}`}>{v}</span>);
        });
      });
      return out.length === 1 ? out[0] : out;
    }
    case "inlineCode": {
      const cite = ctx.cite ? parseCite(node.value) : null;
      const v = cite ? ctx.cite(cite) : null;
      return v != null ? <span key={key}>{v}</span> : <code class="md-icode" key={key}>{node.value}</code>;
    }
    case "strong":
      return <strong key={key}>{inlineAll(node.children, ctx)}</strong>;
    case "emphasis":
      return <em key={key}>{inlineAll(node.children, ctx)}</em>;
    case "delete":
      return <del key={key}>{inlineAll(node.children, ctx)}</del>;
    case "link":
      return (
        <a class="md-link" href={url(node.url, ctx)} target="_blank" rel="noopener" key={key}>
          {/* zoom off inside a link: a zoom button in an <a> would both open the
              lightbox and follow the link on one click. */}
          {inlineAll(node.children, { ...ctx, cite: null, zoom: false })}
        </a>
      );
    case "image":
      if (!ABSOLUTE_RE.test(node.url) && !ctx.resolveUrl) {
        // Transcript: repo-relative paths would 404 against the dashboard
        // origin — show the alt text, not a broken image icon.
        return node.alt || node.url;
      }
      {
        const src = url(node.url, ctx);
        const alt = node.alt || "";
        if (!ctx.zoom) return <img class="md-img" src={src} alt={alt} key={key} />;
        // A real button around the image: <img role="button"> fails biome's
        // noNoninteractiveElementToInteractiveRole, and a button gets Enter and Space free.
        return (
          <button type="button" class="md-img-zoom" key={key}
            aria-label={alt ? `open image full size: ${alt}` : "open image full size"}
            onClick={() => openLightbox({ kind: "img", src, alt })}>
            <img class="md-img is-zoomable" src={src} alt={alt} />
          </button>
        );
      }
    case "break":
      return <br key={key} />;
    case "html":
      return node.value; // literal text, never innerHTML
    case "footnoteReference":
      return <sup key={key}>[{node.label || node.identifier}]</sup>;
    default:
      return node.children ? inlineAll(node.children, ctx) : (node.value ?? null);
  }
}

function listItem(item, ctx, key) {
  // Tight list items carry a paragraph wrapper in mdast; unwrap it so <li>
  // doesn't inherit paragraph margins.
  const inner = (item.children || []).map((c, i) =>
    c.type === "paragraph" && !item.spread ? inlineAll(c.children, ctx) : block(c, ctx, i)
  );
  if (item.checked == null) return <li key={key}>{inner}</li>;
  return (
    <li class="md-task" key={key}>
      <input type="checkbox" checked={item.checked} disabled /> {inner}
    </li>
  );
}

function table(node, ctx, key) {
  const align = node.align || [];
  const [head, ...rows] = node.children;
  const width = head.children.length;
  const style = (j) => (align[j] ? `text-align:${align[j]}` : undefined);
  // Pad ragged rows to header width — the td:last-child / last-row border
  // CSS depends on full rows.
  const cells = (row) => Array.from({ length: width }, (_, j) => row.children[j] ?? null);
  return (
    <table class="md-table" key={key}>
      <thead>
        <tr>
          {cells(head).map((c, j) => (
            <th key={j} style={style(j)}>{c ? inlineAll(c.children, ctx) : ""}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, ri) => (
          <tr key={ri}>
            {cells(row).map((c, j) => (
              <td key={j} style={style(j)}>{c ? inlineAll(c.children, ctx) : ""}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function block(node, ctx, key, prev) {
  switch (node.type) {
    case "paragraph":
      return <p class="md-p" key={key}>{inlineAll(node.children, ctx)}</p>;
    case "heading": {
      // Transcript demotes so a top-level # doesn't shout inside a turn.
      const lvl = ctx.demote ? Math.min(node.depth + 2, 6) : node.depth;
      const Tag = `h${lvl}`;
      return <Tag class={`md-h md-h${node.depth}`} key={key}>{inlineAll(node.children, ctx)}</Tag>;
    }
    case "code":
      if (ctx.mermaid && node.lang === "mermaid") return <Mermaid code={node.value} zoom={ctx.zoom} key={key} />;
      return (
        <pre class="md-code" key={key}>
          <code>
            {ctx.highlight && node.lang ? ctx.highlight(node.value, node.lang) : node.value}
          </code>
        </pre>
      );
    case "blockquote": {
      const pairs = ctx.meta && prev?.type === "heading" ? metaPairs(node, ctx.src) : null;
      if (pairs) {
        return (
          <div class="md-meta" key={key}>
            {pairs.map(([k, v], i) => (
              <span class="md-meta-kv" data-key={k} key={i}>
                <span class="md-meta-k">{k}</span>
                <span class="md-meta-v">
                  {k === "refs" && ctx.cite
                    ? refTokens(v).map((s, j) => {
                        const c = parseCite(s);
                        const chip = c ? ctx.cite(c) : null;
                        return <span key={j}>{j ? ", " : ""}{chip ?? s}</span>;
                      })
                    : v}
                </span>
              </span>
            ))}
          </div>
        );
      }
      return (
        <blockquote class="md-quote" key={key}>
          {(node.children || []).map((c, i) => block(c, ctx, i))}
        </blockquote>
      );
    }
    case "list": {
      const items = (node.children || []).map((c, i) => listItem(c, ctx, i));
      return node.ordered ? (
        <ol class="md-ol" start={node.start !== 1 ? node.start : undefined} key={key}>{items}</ol>
      ) : (
        <ul class="md-ul" key={key}>{items}</ul>
      );
    }
    case "thematicBreak":
      return <hr class="md-hr" key={key} />;
    case "table":
      return table(node, ctx, key);
    case "html":
      return <p class="md-p" key={key}>{node.value}</p>;
    default:
      // Unknown blocks (footnoteDefinition, ...): render their text rather
      // than dropping content. Link-reference definitions have neither
      // children nor value and vanish, which is correct.
      if (node.children) return <p class="md-p" key={key}>{inlineAll(node.children, ctx)}</p>;
      return node.value ? <p class="md-p" key={key}>{node.value}</p> : null;
  }
}

// Render a markdown string into an array of block-level vnodes.
export function renderMarkdown(text, opts = {}) {
  const src = String(text || "");
  const ctx = {
    src,
    demote: opts.demote ?? true,
    softBreaks: opts.softBreaks ?? "br",
    highlight: opts.highlight ?? null,
    resolveUrl: opts.resolveUrl ?? null,
    meta: opts.meta ?? false,
    cite: opts.cite ?? null,
    mermaid: opts.mermaid ?? false,
    // Images and diagrams open full size on click (the walkthrough, THI-443).
    zoom: opts.zoom ?? false,
  };
  const tree = fromMarkdown(src, {
    extensions: [gfm()],
    mdastExtensions: [gfmFromMarkdown()],
  });
  return tree.children.map((node, i) => block(node, ctx, i, tree.children[i - 1]));
}
