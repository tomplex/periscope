---
name: release
description: "Cut a periscope release: draft the CHANGELOG section, get Tom's OK, run bin/release. Use when asked to cut, ship or tag a release, bump the version, or 'release X.Y.Z'."
---

# Release

A release is a `vX.Y.Z` tag on `main`. It is the only thing other installs receive — their Update button targets the newest tag, never `main`'s tip — and its CHANGELOG section is the release's whole public face: the Update popover on every install and the GitHub Release body. The tooling (`bin/release`, `bin/release-notes`, `.github/workflows/release.yml`) does the mechanics; this skill produces the one input it can't — the notes — and stops for Tom's OK before anything public happens.

Run everything in `~/dev/periscope`, the prod checkout, never a worktree. `CHANGELOG.md` is the one prod-tree file this skill edits.

## Steps

### 1. Preflight

```sh
git fetch --tags origin
git status --short
git tag --list 'v*' --sort=-v:refname | head -1      # <last>
git log --no-merges --format='- %s' <last>..HEAD      # the raw material
```

Anything in `git status` other than `CHANGELOG.md` → stop and name it to Tom. `bin/release` refuses a dirty tree, an off-`main` checkout, or `main` behind `origin/main`; each refusal is the safety net — fix the cause, never work around it. An empty log means nothing to release: say so and stop.

### 2. Version

Tom named one → that. Otherwise, from the log since `<last>`: every commit a fix, docs, build, test or release chore → patch (`X.Y.Z+1`); any commit adds a feature or changes what a user sees → minor (`X.Y+1.0`). Under 1.0 there is no major. The proposed version goes in the draft's heading so Tom overrides it in the same reply as the notes.

### 3. Draft the notes

Insert `## vX.Y.Z — YYYY-MM-DD` directly above the previous release's `## ` heading (after the file's intro paragraph). `bin/release-notes` reads only the FIRST `## ` section, so position is correctness, not style.

The reader runs periscope and has read none of the code. Match the shape of the previous section:

- Grouped by feature; each bullet opens with a bold lead (`**Reviews.**`) and says what is new or what behaves differently.
- Every bullet is rewritten from what the commit *did*. Commit subjects, ticket IDs and PR numbers stay in `git log`. Code identifiers appear only where the user types or sees them (`⌘K`, a setting's name, `bin/periscope restart`).
- A commit earns a line only if the user can tell the release happened: `build:`, `docs`, tests and `release:` commits get none. A fix to code that is itself new in this release is part of that feature's bullet, not its own; a fix to already-released behavior is its own line.
- A sentence or two of framing above the bullets when the release has a theme; none when it doesn't.

### 4. Tom's OK

`open_document` on `CHANGELOG.md`, and one line in chat: the proposed version, that the draft is up, and what the log contained that the notes left out. Wait. Edits requested → apply, re-open, wait again. `bin/release` runs only after an explicit OK on the draft as it stands — a tag is public within the hour and cannot be re-cut.

### 5. Cut

```sh
bin/release X.Y.Z
```

It bumps `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml`, `pyproject.toml`, re-locks `Cargo.lock` and `uv.lock`, commits `release: vX.Y.Z` with the changelog, tags, and pushes `main` and the tag. Every refusal happens before it touches a file: fix the reason and rerun. Two failures land after that point — `cargo metadata` / `uv lock` failing leaves the bumped manifests dirty, so `git checkout -- src-tauri pyproject.toml` before the rerun; a push failing leaves the commit and tag local, so `git push origin HEAD && git push origin vX.Y.Z` finishes it (a rerun would refuse: the tag already exists).

### 6. Done

```sh
git ls-remote --tags origin vX.Y.Z
```

Prints the tag → done. Report the version and the actions URL `bin/release` printed (the tag's workflow builds the `.dmg` and publishes the GitHub Release; it takes a few minutes and nothing here waits on it). `notify` kind `done`.
