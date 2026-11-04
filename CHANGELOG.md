# Changelog

What changed in each periscope release, newest first. The dashboard's Update
button installs releases, and its popover shows these sections.

## v0.3.0 — 2026-11-04

Reviews get a proper reading surface, and the dashboard starts showing what
everything costs the machine.

- **Walkthroughs.** A walkthrough reads as a sequence of stop cards under a
  fixed header that names the PR, its staleness and your progress, with a
  contents list for jumping between stops. Each card puts the prose beside
  its code — the diff column stays put as you scroll — and lifts diagrams
  into a full-width row above. Mark a stop *reviewed* and it folds, showing
  its comment count; the marks persist, so a walkthrough reopens where you
  left it. Files over 300 lines start collapsed behind "show N lines", and
  the comment box stays closed until you open it.
- **Diagrams.** Mermaid diagrams render in periscope's own fonts and colors.
  Click a diagram or image for a full-size view (Esc or the backdrop closes
  it). Wide diagrams keep their labels legible and scroll sideways instead of
  shrinking to fit.
- **Reviews queue.** The column headers sort the table — click again to
  reverse, once more to return to readiness order. Merged PRs stay at the
  bottom, and the sort survives opening a walkthrough and coming back.
- **Resources.** A header pill shows memory and CPU for the periscope server,
  the app's webview and each pane, heaviest first, against the machine's
  physical RAM; CPU is a share of the whole machine, so the owners add up.
  A pane's own figures sit in its detail header, and the ↻ chip on a
  long-lived Claude reads its real footprint and says which limit tripped.
- **macOS app.** Webview recycling — the shell's answer to WKWebView's
  graphics leak — now waits until periscope has been out of focus for 30
  seconds (or the machine idle for five minutes), with the renderer over
  2 GB and at least 30 minutes old. The rebuilt window never takes focus
  from the app you're in, a minimized window stays minimized, and leftover
  renderer processes are cleaned up. View → Recycle Webview still recycles
  on demand.
- **Notifications.** A *done* banner quotes Claude's last reply from the
  transcript, not the status bar or whatever row happened to be last on
  screen. Allow on a Bash permission banner now works when the dialog
  carries a "This command requires approval" row.
- **Pane status.** The API-error chip clears as soon as Claude writes
  anything after the error — no new prompt needed — and its tooltip says
  Claude has written nothing since.
- **Smaller things.** Faradaysie joins the Tamagotchi critters.
  `bin/periscope install-skill` links a `release` skill alongside
  `walkthrough`.

## v0.2.0 — 2026-10-28

The first tagged release. From here on the Update button installs releases
rather than whatever is on `main`: the header pill names the release
(`↑ v0.3.0`) and its popover shows these notes. To run unreleased code,
`git pull` and `bin/periscope restart` as before.

Highlights from the last two weeks:

- **Reviews.** A Reviews view (header button, or ⌘K) lists every open PR that
  is yours or waiting on your review, with the pane working on it joined on.
  *Prepare* spawns a Claude that writes a walkthrough of the PR — fetching it
  into a worktree first if nothing has it checked out — and the walkthrough
  opens in periscope with its diff, citations and comments. Merge stays
  blocked until the comments are resolved. This replaces the LGTM integration.
- **Banner buttons.** In the macOS app, notifications act directly: Allow or
  Deny a permission prompt, reply to a question, or ask the agent to fix
  what failed — each re-checked against the pane when pressed. Settings →
  Notifications toggles each kind of banner.
- **Pane detection.** A pane counts as Claude whenever its composer sits at the
  bottom, whatever the status line prints; a permission dialog reads as
  needs-input; a stream cut's `API Error` flags the pane.
- **Smaller things.** Linear tickets show from the PR title or branch name
  without `link_linear`. Settings has a fonts picker. Mermaid blocks render as
  diagrams in previews. A split window no longer shows a frozen screen under
  the live one. The Tamagotchi critters gained hats, props, instruments and a
  stride.
