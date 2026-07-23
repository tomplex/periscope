// Which files in a diff are generated artifacts rather than work you wrote.
//
// The committed bundle (static/dist/app.js) changes on every build, so it lands
// in every diff as a huge unreadable hunk — and because viewed-marks expire on
// content change, it would re-surface every single rebuild. That's precisely
// backwards: the noisiest file demands the most attention.
//
// Generated files are folded away by default and sorted last. They are never
// hidden — you can always expand one, and that choice sticks (see reviewState).
//
// Path patterns only, deliberately. Auto-collapsing by SIZE was tempting but
// would fold away a large genuine refactor, which is the one thing you most
// need to read.

const GENERATED_DIRS = ["dist/", "build/", "vendor/", ".next/", "target/debug/", "target/release/"];

const GENERATED_FILES = new Set([
  "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
  "uv.lock", "poetry.lock", "Cargo.lock", "Gemfile.lock", "composer.lock",
  "go.sum",
]);

const GENERATED_SUFFIXES = [".min.js", ".min.css", ".map", ".snap"];

export function isGenerated(path) {
  const p = String(path || "");
  if (!p) return false;
  const leaf = p.split("/").pop() || "";
  if (GENERATED_FILES.has(leaf)) return true;
  if (GENERATED_SUFFIXES.some((s) => leaf.endsWith(s))) return true;
  // Match on a path SEGMENT so "redistribute/x.js" isn't mistaken for "dist/".
  const segs = `${p}/`;
  return GENERATED_DIRS.some((d) => segs.includes(`/${d}`) || segs.startsWith(d));
}

/**
 * Real files first, generated last. Stable within each group, so git's
 * ordering is preserved and the list doesn't reshuffle between refreshes.
 */
export function sortFiles(files) {
  const real = [];
  const noise = [];
  for (const f of files || []) (isGenerated(f.path) ? noise : real).push(f);
  return real.concat(noise);
}
