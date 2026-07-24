// Word-level highlighting inside a changed line pair.
//
// A red/green row already tells you the line changed; what it doesn't tell you
// is WHERE. On a rename or a single-argument change, the eye has to diff two
// nearly identical lines by hand. This marks the differing span.
//
// Deliberately conservative — two rules keep it from producing confetti:
//   1. Only balanced del-run/add-run pairs are considered. If 2 lines were
//      removed and 3 added, which pairs with which is a guess, so we don't.
//   2. A pair is only highlighted when it's similar enough to actually BE the
//      same line edited. Two unrelated lines that happen to be adjacent would
//      otherwise light up end-to-end, which is worse than no highlight at all.
//
// Segments are computed by trimming the common prefix and suffix rather than
// running an LCS: it can't interleave, so it never produces a speckled line,
// and for real edits (a renamed identifier, a changed value) it lands on
// exactly the right span.

const TOKEN_RE = /[A-Za-z0-9_]+|\s+|[^A-Za-z0-9_\s]/g;

/** Split into word / whitespace / single-punctuation tokens. Joining the
 *  result reproduces the input exactly — segments must be lossless. */
export function tokenize(s) {
  return String(s ?? "").match(TOKEN_RE) || [];
}

function commonPrefix(a, b) {
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  return i;
}

function commonSuffix(a, b, floor) {
  let i = 0;
  while (
    i < a.length - floor &&
    i < b.length - floor &&
    a[a.length - 1 - i] === b[b.length - 1 - i]
  ) i++;
  return i;
}

/**
 * Segment a removed/added line pair into unchanged and changed spans.
 * Returns {del: [{text, changed}], add: [...]} or null when the pair isn't
 * similar enough to treat as one edited line.
 */
export function intralineSegments(delText, addText, minSim = 0.5) {
  const a = tokenize(delText);
  const b = tokenize(addText);
  if (!a.length || !b.length) return null;

  const pre = commonPrefix(a, b);
  const suf = commonSuffix(a, b, pre);
  const shared = pre + suf;
  // 2*shared/(|a|+|b|): 1.0 when identical, 0 when nothing lines up.
  const sim = (2 * shared) / (a.length + b.length);
  if (sim < minSim) return null;

  const seg = (toks, changedFrom, changedTo) => {
    const out = [];
    const push = (text, changed) => {
      if (!text) return;
      const last = out[out.length - 1];
      if (last && last.changed === changed) last.text += text;
      else out.push({ text, changed });
    };
    push(toks.slice(0, changedFrom).join(""), false);
    push(toks.slice(changedFrom, changedTo).join(""), true);
    push(toks.slice(changedTo).join(""), false);
    return out;
  };

  const delSegs = seg(a, pre, a.length - suf);
  const addSegs = seg(b, pre, b.length - suf);
  // Nothing actually differs (identical lines) — no highlight to draw.
  if (!delSegs.some((s) => s.changed) && !addSegs.some((s) => s.changed)) return null;
  return { del: delSegs, add: addSegs };
}

/**
 * Annotate a hunk's lines with intraline segments, pairing each maximal
 * del-run with the add-run that immediately follows it.
 * Returns a new array; input is untouched.
 */
export function withIntraline(lines, minSim = 0.5) {
  const out = lines.map((l) => ({ ...l }));
  let i = 0;
  while (i < out.length) {
    if (out[i].kind !== "del") { i++; continue; }
    let d = i;
    while (d < out.length && out[d].kind === "del") d++;
    let a = d;
    while (a < out.length && out[a].kind === "add") a++;
    const dels = d - i;
    const adds = a - d;
    // Only balanced runs pair unambiguously.
    if (dels > 0 && dels === adds) {
      for (let k = 0; k < dels; k++) {
        const segs = intralineSegments(out[i + k].text, out[d + k].text, minSim);
        if (segs) {
          out[i + k].segs = segs.del;
          out[d + k].segs = segs.add;
        }
      }
    }
    i = a > i ? a : i + 1;
  }
  return out;
}
