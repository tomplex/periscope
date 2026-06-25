import render from "preact-render-to-string";
import { describe, expect, it } from "vitest";
import { renderMarkdown } from "../markdown.jsx";

const html = (text, opts) => render(<div>{renderMarkdown(text, opts)}</div>);

describe("headings", () => {
  it("demotes by default (# -> h3), preserving md-h1 class", () => {
    expect(html("# Title")).toContain('<h3 class="md-h md-h1">Title</h3>');
  });
  it("renders real levels with demote: false", () => {
    const out = html("# Title\n\n## Section", { demote: false });
    expect(out).toContain('<h1 class="md-h md-h1">Title</h1>');
    expect(out).toContain('<h2 class="md-h md-h2">Section</h2>');
  });
  it("caps demoted headings at h6", () => {
    expect(html("##### Deep")).toContain('<h6 class="md-h md-h5">Deep</h6>');
  });
});

describe("soft breaks", () => {
  it("renders intra-paragraph newlines as <br> by default (transcript compat)", () => {
    expect(html("line one\nline two")).toMatch(/line one<br\s*\/?>line two/);
  });
  it("renders them as whitespace with softBreaks: 'space'", () => {
    const out = html("line one\nline two", { softBreaks: "space" });
    expect(out).not.toContain("<br");
    expect(out).toMatch(/line one\s+line two/);
  });
  it("renders hard breaks (trailing double space) as <br> in both modes", () => {
    expect(html("one  \ntwo", { softBreaks: "space" })).toContain("<br");
  });
});

describe("inline", () => {
  it("renders inline code, bold, italic, strikethrough", () => {
    const out = html("`x` **b** *i* ~~gone~~");
    expect(out).toContain('<code class="md-icode">x</code>');
    expect(out).toContain("<strong>b</strong>");
    expect(out).toContain("<em>i</em>");
    expect(out).toContain("<del>gone</del>");
  });
  it("autolinks bare URLs (GFM)", () => {
    expect(html("see https://example.com now")).toContain(
      '<a class="md-link" href="https://example.com"'
    );
  });
});

describe("lists", () => {
  it("nests lists", () => {
    const out = html("- a\n  - b");
    expect(out).toMatch(/<li>a<ul class="md-ul"><li>b<\/li><\/ul><\/li>/);
  });
  it("keeps multi-line (lazy continuation) items in the list", () => {
    const out = html("- first line\n  continued\n- second");
    expect((out.match(/<li/g) || []).length).toBe(2);
    expect(out).toContain("continued");
  });
  it("renders task lists with disabled checkboxes", () => {
    const out = html("- [x] done\n- [ ] todo");
    expect(out).toContain('class="md-task"');
    expect(out).toContain("checked");
    expect(out).toContain("disabled");
  });
  it("keeps ordered list start", () => {
    expect(html("3. three\n4. four")).toContain('start="3"');
  });
});

describe("blocks", () => {
  it("renders multi-paragraph blockquotes", () => {
    expect(html("> one\n>\n> two")).toMatch(
      /<blockquote class="md-quote"><p class="md-p">one<\/p><p class="md-p">two<\/p><\/blockquote>/
    );
  });
  it("renders thematic breaks", () => {
    expect(html("a\n\n---\n\nb")).toContain('<hr class="md-hr"');
  });
  it("renders raw HTML as literal text, never markup", () => {
    const out = html('before\n\n<div onclick="x()">hi</div>\n\nafter');
    expect(out).toContain("&lt;div");
    expect(out).not.toContain("<div onclick");
  });
});

describe("images", () => {
  it("renders absolute images", () => {
    expect(html("![shot](https://x.test/y.png)")).toContain(
      '<img class="md-img" src="https://x.test/y.png" alt="shot"'
    );
  });
  it("falls back to alt text for relative src without resolveUrl", () => {
    const out = html("![shot](docs/y.png)");
    expect(out).not.toContain("<img");
    expect(out).toContain("shot");
  });
  it("resolves relative src through resolveUrl", () => {
    const out = html("![shot](docs/y.png)", { resolveUrl: (u) => `/r/${u}` });
    expect(out).toContain('src="/r/docs/y.png"');
  });
});

describe("links + resolveUrl", () => {
  it("resolves relative hrefs; absolute and fragment pass through", () => {
    const out = html("[a](docs/X.md) [b](https://x.test/) [c](#frag)", {
      resolveUrl: (u) => `/r/${u}`,
    });
    expect(out).toContain('href="/r/docs/X.md"');
    expect(out).toContain('href="https://x.test/"');
    expect(out).toContain('href="#frag"');
  });
});

describe("code fences", () => {
  it("renders plain fences verbatim", () => {
    const out = html("```js\nconst x = 1;\n```");
    expect(out).toContain('<pre class="md-code">');
    expect(out).toContain("const x = 1;");
  });
  it("routes fences through the highlight callback with the lang", () => {
    const highlight = (code, lang) => <span data-lang={lang}>{code}</span>;
    expect(html("```python\nx = 1\n```", { highlight })).toContain('data-lang="python"');
  });
  it("skips highlight for lang-less fences", () => {
    const highlight = () => {
      throw new Error("should not be called");
    };
    expect(html("```\nplain\n```", { highlight })).toContain("plain");
  });
});

describe("tables", () => {
  it("applies alignment and pads ragged rows to header width", () => {
    const out = html("| a | b |\n|:-:|---|\n| only |\n");
    expect(out).toContain('style="text-align:center"');
    expect((out.match(/<td/g) || []).length).toBe(2);
  });
});
