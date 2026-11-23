# Changelog

What changed in each periscope release, newest first. The dashboard's Update
button installs releases, and its popover shows these sections.

## v0.4.0 — 2026-11-23

The rail now puts what wants you first, and another app can start a Claude
on a Linear issue in one call.

- **Rail.** A sonar strip at the top of the rail has one line for each
  track and one blip for each pane. The blip's shape and colour show the
  pane's state: asks, broken, done, working, idle or shell. Click a blip to
  open its pane. Click a track's name to show only that track; this replaces
  the filter chips. The tree is quieter: a branch gets a header only when
  two or more panes share it, and only asking panes pulse. Each row has an
  age column, which turns amber after 10 minutes of waiting and red after
  an hour. The PR chip, with its CI glyph, moves to the row's first line.
- **Next.** A Next queue replaces NEEDS YOU, READY and RUNNING. It lists
  the panes that ask (longest wait first), then the broken ones (an API
  error, or failing CI on an open PR), then the finished ones. Each card
  shows what the agent said, its track and why it is there. × marks a done
  card seen, and "mark all seen" clears every done card at once. ⌘J opens
  the next pane that wants you, from anywhere, the terminal included. The
  rail, ⌘J and Clawd now agree on which panes want you.
- **Pane menu.** On hover, a card shows only ☆ pin, ⋯ more and × kill, so no
  button covers its name. ⋯ or a right-click opens the pane menu: the tab
  colours, Rename colours…, Pin tab, Move to account B, Freeze name, Open in
  the editor and Kill tab. Each item has a key, except Kill tab.
- **Tab colours.** Give a pane card one of six colours, so a group of
  sessions is easy to find. The colour tints the card in the tree, in Next
  and in PINNED. Pick it in the pane menu (1–6, and 0 removes it), and
  rename a colour, for example Iris → "ci". Settings → Tab colours sets the
  tint: Quiet, Default or Strong.
- **History.** Resume stays on /history and links to the dashboard. A
  session with no transcript shows "no transcript" and has no Resume
  button; before, it started an empty pane. The dashboard's resume and the
  `resume_session` tool refuse such a session too. The list and the
  transcript scroll on their own, the filters fold into one disclosure, and
  all text meets 4.5:1 contrast. Recent sorts by a session's last turn, and
  a row names the pane, PR and ticket that last ran it. Sessions with no
  summary are "unsummarized", and an empty result offers to include them.
  The footer says when history last indexed a session.
- **Start from another app.** `POST /api/linear/start` makes or reuses the
  worktree for a Linear issue's branch, starts Claude with a prompt and
  links the issue to the card. An optional `name` names and pins the new
  pane. Send `pr` in place of `branch` to start on a pull request: periscope
  checks it out as `pr-<N>`, or brings that worktree up to the PR's latest
  commit, and says in a `warning` when it cannot. `POST /api/select` selects
  a pane on the dashboard, and `GET /api/open/catalog` gives each repo's
  GitHub `slug`. todo-hub's Start in periscope uses these routes; the README
  describes them.
- **Speed.** Each dashboard refresh starts less than half as many processes
  as before. On a busy machine the dashboard used to fall 3 to 13 seconds
  behind. `gh` calls no longer run macOS's `security` tool each time. The
  resource popover ends with the poll time and the processes per minute.
- **Terminal copy.** Text copied from a terminal tile no longer pastes with
  spaces out to the pane's width. Each run of spaces becomes one space, and
  line breaks stay. This also removes the indentation of copied code.
- **Smaller things.** Codex panes are found again when their process name
  is long. A prompt that starts with `-`, or is over about 16 KB, now
  reaches Claude; before, Claude got an empty input. A key that tmux refuses
  now shows as a failure in the permission and reply buttons and in
  `/api/send-bulk`. The transcript's working dot no longer pulses, and the
  detail header's ↻ is grey when age alone set it off.

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
