---
name: periscope
description: A dark sonar field over every tmux pane, where colour appears only when an agent has something to report.
colors:
  accent: "oklch(0.74 0.13 232)"
  effort: "oklch(0.72 0.12 330)"
  worktree: "oklch(0.72 0.13 295)"
  linear: "oklch(0.7 0.13 280)"
  account: "oklch(0.75 0.11 190)"
  pin-gold: "#d8b33a"
  s-needs: "oklch(0.74 0.14 38)"
  s-needs-bg: "color-mix(in oklch, oklch(0.74 0.14 38) 16%, transparent)"
  s-working: "oklch(0.80 0.14 92)"
  s-working-bg: "color-mix(in oklch, oklch(0.80 0.14 92) 14%, transparent)"
  s-done: "oklch(0.76 0.17 152)"
  s-done-bg: "color-mix(in oklch, oklch(0.76 0.17 152) 16%, transparent)"
  s-idle: "oklch(0.65 0.01 250)"
  s-shell: "oklch(0.72 0.10 165)"
  s-success: "oklch(0.74 0.13 150)"
  s-danger: "oklch(0.70 0.16 25)"
  bg-0: "oklch(0.235 0.007 250)"
  bg-1: "oklch(0.282 0.008 250)"
  bg-1-hi: "oklch(0.318 0.009 250)"
  bg-2: "oklch(0.335 0.009 250)"
  bg-3: "oklch(0.395 0.010 250)"
  bg-modal: "oklch(0.255 0.008 250)"
  bg-term: "#282c34"
  fg-0: "oklch(0.970 0.004 250)"
  fg-1: "oklch(0.850 0.006 250)"
  fg-2: "oklch(0.660 0.008 250)"
  fg-3: "oklch(0.520 0.008 250)"
  fg-4: "oklch(0.420 0.008 250)"
  line: "color-mix(in oklch, oklch(0.50 0.008 250) 30%, transparent)"
  line-soft: "color-mix(in oklch, oklch(0.50 0.008 250) 16%, transparent)"
  line-strong: "color-mix(in oklch, oklch(0.60 0.008 250) 45%, transparent)"
typography:
  headline:
    fontFamily: "Hanken Grotesk, -apple-system, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 600
    letterSpacing: "-0.005em"
  title:
    fontFamily: "Hanken Grotesk, -apple-system, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 600
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Hanken Grotesk, -apple-system, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0.005em"
    fontFeature: "'liga' 0, 'calt' 0"
  body-sm:
    fontFamily: "Hanken Grotesk, -apple-system, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
  meta:
    fontFamily: "Hanken Grotesk, -apple-system, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 400
  label:
    fontFamily: "Hanken Grotesk, -apple-system, system-ui, sans-serif"
    fontSize: "10px"
    fontWeight: 600
    letterSpacing: "0.07em"
  ref:
    fontFamily: "IBM Plex Mono, ui-monospace, JetBrains Mono, Menlo, monospace"
    fontSize: "10px"
    fontWeight: 400
rounded:
  chip: "3px"
  sm: "4px"
  md: "6px"
  lg: "10px"
  bezel: "16px"
  pill: "999px"
spacing:
  rail-indent: "16px"
  header-gutter: "20px"
components:
  filter-btn:
    backgroundColor: "transparent"
    textColor: "{colors.fg-2}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
  filter-btn-hover:
    backgroundColor: "{colors.bg-1}"
    textColor: "{colors.fg-1}"
  filter-btn-active:
    backgroundColor: "color-mix(in oklch, oklch(0.74 0.13 232) 18%, transparent)"
    textColor: "{colors.fg-0}"
  segment:
    backgroundColor: "{colors.bg-2}"
    textColor: "{colors.fg-3}"
    rounded: "{rounded.chip}"
    padding: "1px 6px"
  segment-active:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.bg-0}"
  dialog-button:
    backgroundColor: "{colors.bg-1}"
    textColor: "{colors.fg-0}"
    rounded: "{rounded.sm}"
    padding: "6px 14px"
  dialog-button-hover:
    backgroundColor: "{colors.bg-1-hi}"
  pane-pill:
    backgroundColor: "{colors.bg-2}"
    textColor: "{colors.fg-2}"
    typography: "{typography.ref}"
    rounded: "{rounded.chip}"
    padding: "0 5px"
    height: "15px"
  keycap:
    backgroundColor: "{colors.bg-2}"
    textColor: "{colors.fg-2}"
    typography: "{typography.ref}"
    rounded: "{rounded.chip}"
    padding: "1px 5px"
  track-card:
    backgroundColor: "{colors.bg-1}"
    rounded: "{rounded.lg}"
    padding: "5px"
  pane-card-expanded:
    backgroundColor: "{colors.bg-2}"
    rounded: "{rounded.md}"
    padding: "9px 10px"
  pane-card-selected:
    backgroundColor: "{colors.bg-1-hi}"
  omnibox-input:
    backgroundColor: "{colors.bg-2}"
    textColor: "{colors.fg-0}"
    rounded: "{rounded.sm}"
    padding: "10px 12px"
  menu:
    backgroundColor: "{colors.bg-1}"
    rounded: "{rounded.md}"
    padding: "4px"
  menu-item:
    textColor: "{colors.fg-2}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.sm}"
    padding: "6px 10px"
  menu-item-hover:
    backgroundColor: "{colors.bg-2}"
    textColor: "{colors.fg-0}"
  toast:
    backgroundColor: "{colors.bg-2}"
    textColor: "{colors.fg-1}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  critter-bezel:
    backgroundColor: "{colors.bg-1}"
    rounded: "{rounded.bezel}"
    width: "136px"
    height: "96px"
---

# Design System: periscope

## Overview

**Creative North Star: "The Sonar Screen"**

periscope is a quiet grey field that agents show up on. Most of the time nothing on it is
lit: surfaces, rules and text all sit in one cool, nearly colourless grey (hue 250, chroma
under 0.010). When an agent has something to report, it appears as a coloured blip: an
orange ping when it needs you, a green contact when it is done, an amber sweep while it
works. The field exists so those blips can't be missed, and a screen that adds colour for
any other reason has broken the instrument.

The density is that of a tool you live in all day. Body text is 13px, metadata 10–11px,
rows have 3–9px of vertical padding, and the whole dashboard is one split view: the rail on
the left, the live pane on the right, a header strip above. Controls are compact and
tactile: small pills, keycaps with a thicker bottom edge, segmented pins. They stay muted
until hovered or chosen. The interface is dark only; there is no light theme.

The one place the instrument lets personality in is Clawd's device in the header's top-right
corner. It is a pixel-art critter inside its own bezel that keeps a daily schedule, holds up
the most urgent item and takes you there when clicked. It is part of the identity, not a
decoration. Its frame keeps it visibly separate from the work around it.

**Rejected looks** (confirmed): SaaS analytics dashboards (KPI tiles, decorative charts,
gradient hero cards, airy spacing); neon hacker terminals (green on black, glow, scanlines);
colour used as decoration.

**Key Characteristics:**
- A cool grey field, made of one hue at very low chroma, with depth built from tonal steps.
- Colour means a state or a kind of thing, never ornament.
- Every status colour has the same lightness, so urgency comes from motion and position
  rather than brightness.
- One interactive accent (Periscope Glass) for selection, focus and "active".
- Sans for labels and prose, mono for anything you could paste: refs, paths and commands.
- Compact, tactile controls: pills, keycaps, segmented pins.
- Unicode glyphs instead of an icon set (◆ ◇ $ ✻ ⬡ ⎇ ★), each coloured by the kind of thing
  it marks.
- Clawd lives in a bezel: personality is welcome but framed.

## Colors

A cool, near-neutral grey field with saturated signal hues that share one brightness and
differ only in hue.

### Primary
- **Periscope Glass** (`accent`): the only interactive accent, a clear cool blue. It means
  "you are here" or "you can act here": the blinking cursor after the wordmark, the
  selected rail row's 2px left edge, active filter and segment fills, focus borders on
  inputs, drop-target lines while dragging, the splitter highlight, and the ◆ repo glyph.
  It never reports agent state.

### Secondary
- **Track Magenta** (`effort`): marks tracks. It colours the track's uppercase label and its
  glyph (⧉ or 🗀) at full strength, and the track card's border at 30% strength.
- **Worktree Violet** (`worktree`): marks branches and worktrees. It colours the ⎇ glyph, the
  2px left rule (40%) on a branch sub-cluster, and merged PR refs. A merged PR reads violet
  so it doesn't pass for live open work.

### Tertiary (kinds of thing)
- **Ticket Purple** (`linear`): Linear ticket refs, wherever they appear.
- **Account Teal** (`account`): the pill naming which subscription a pane bills. Its hue is
  chosen to stay clear of Ticket Purple and Periscope Glass.
- **Pin Gold** (`pin-gold`): the ★ for a pinned pane. It is the one hex-only colour among
  the named ones.

### Status (the blips)
- **Ping Orange** (`s-needs`, needs input): the most urgent state. It colours the pane's
  stripe and blip (both pulse), the Next count while something asks, and permission
  prompts. The Claude ✻ glyph shares this hue at slightly lower chroma
  (`oklch(0.74 0.12 38)`).
- **Sweep Amber** (`s-working`, working): the stripe, running CI, and context usage above
  60%.
- **Contact Green** (`s-done`, finished and not yet looked at): the stripe and a ring blip,
  both at 45% once the pane finished more than an hour ago. Done doesn't pulse.
- **Static Grey** (`s-idle`, idle): the stripe at 50% opacity. Effectively silent.
- **Shore Green** (`s-shell`, a shell pane): the `$` glyph and the stripe at 50%.
- **Success** (`s-success`) and **Danger** (`s-danger`): CI passing or failing, context
  above 80%, destructive buttons, errors, and a **broken** pane (an API error, or failing
  CI on an open PR): its stripe and its square blip.
- Each state has a matching `-bg` token: the same hue mixed at 14–16% into transparent. It
  drives tinted backgrounds and the pulse ring.

### Neutral
- **Abyss** (`bg-0`): the page, the rail tray and the detail area.
- **Hull** (`bg-1`): track cards, menus, the sidebar and the critter's screen.
- **Hull, lit** (`bg-1-hi`): row hover and the selected pane.
- **Raised** (`bg-2`): chips, keycaps, the omnibox input and toasts.
- **Rim** (`bg-3`): chip hover, scrollbar thumbs and the critter's bezel border.
- **Console** (`bg-modal`): modal and dialog cards, slightly lifted off the page.
- **Terminal** (`bg-term`): the xterm canvas background. Anything that frames a terminal
  must match it exactly, or the canvas shows a seam.
- **Text, five steps** (`fg-0` → `fg-4`):
  - `fg-0`: titles and the selected or active label
  - `fg-1`: body text
  - `fg-2`: controls at rest
  - `fg-3`: metadata, narrator status and section labels
  - `fg-4`: the faintest hints and timestamps
- **Rules** (`line-soft`, `line`, `line-strong`): translucent greys at 16%, 30% and 45%.
  - `line-soft`: dividers inside a surface
  - `line`: edges between surfaces
  - `line-strong`: input outlines and the modal's 1px ring

### Named Rules
**The Blip Rule.** Colour on the field is a signal: either a state, or a kind of thing
(repo, track, branch, ticket, account). A hue that reports nothing doesn't ship.

**The Same-Brightness Rule.** Status hues sit at lightness 0.74–0.80 and chroma 0.13–0.17,
and differ only in hue. No state wins by being brighter. Urgency comes from the pulse and
from position (the sonar strip and the Next queue sit at the top of the rail).

**The One Glass Rule.** Periscope Glass is the only interactive accent. Selection, focus,
active and drop targets use it. Agent state never does.

**The Mixed-Tint Rule.** Build a tint by mixing a token into transparent
(`color-mix(in oklch, <token> 16%, transparent)`), the way the `-bg` tokens are built. Transparent
has no hue, so the token keeps its own. When the tint must be opaque, mix into the surface
`in srgb` (as the drop-target wash does). Mixing into a grey surface `in oklch` swings the hue
toward the grey's 250: the danger button's red mix comes out violet. That is drift, not a
pattern to copy.

## Typography

**UI font:** Hanken Grotesk (falling back to -apple-system, system-ui)
**Code font:** IBM Plex Mono (falling back to ui-monospace, JetBrains Mono, Menlo)

**Character:** A plain grotesk for labels and prose beside a mono for anything literal. The
mono is what makes a PR number, branch or path look like something you could paste.
Ligatures and contextual alternates are switched off everywhere (`'liga' 0, 'calt' 0`), so
`->` and `!=` stay as typed.

Both faces can be swapped in Settings → Fonts. The UI slot accepts Hanken Grotesk, Inter,
System sans, or any of the mono faces; the code slot accepts IBM Plex Mono, JetBrains Mono,
SF Mono or Menlo. The faces are set on `--sans` and `--mono` at runtime, so every size and
weight here must still read in any face on that list.

### Hierarchy
- **Headline** (600, 15px, -0.005em): modal and dialog titles, and the omnibox input text.
- **Title** (600, 14px, -0.01em): the `periscope` wordmark.
- **Body** (400, 13px, line-height 1.5, +0.005em): rail rows, omnibox rows and dialog text.
- **Body small** (400, 12px): filter buttons, menu items, the detail header, toasts and
  dialog secondary text.
- **Meta** (400, 11px): narrator status lines, Next card reasons, row ages, omnibox
  sub-paths and footers.
- **Label** (600, 10px, +0.07em to +0.09em, UPPERCASE): rail section headers (NEXT, PINNED,
  TRACKS, ACTIVITY), track labels and omnibox group labels.
- **Ref** (mono, 10px): PR and ticket chips, keycaps and the cwd chip. Larger mono (12–13px)
  is used for paths in headers and for command input.

The scale is tight: 10, 11, 12, 13, 15, about 1.1× per step, because this is a dense tool.
Half sizes (11.5px, 12.5px) appear in places; treat them as drift.

### Named Rules
**The Paste-It Rule.** If the user could copy a value into a terminal (a PR number, ticket
ID, branch, path or command), set it in `--mono`. Labels, status and prose stay `--sans`.

**The Organizer Rule.** UPPERCASE letter-spaced 10px text only labels a section; it is never
content. A track name shown as a label is uppercase; the same name inside a sentence is not.

## Layout

One full-bleed split view sits under a sticky header. The header has two rows: the wordmark,
usage meters and spawn pins on top, and filters with the window counts below. The critter's
bezel takes its own right-hand column spanning both rows. The header gutter is 20px
(`header-gutter`).

Below the header is a two-column grid: the **rail** (default 320px, clamped between 180px
and 50%) and the **detail** area, which holds an optional inspector column (default 280px,
clamped between 200px and 50%). Columns resize by dragging, not by breakpoint. Each splitter
has a 7px invisible hit area straddling a 1px rule, and shows a 2px Periscope Glass line
after a 150ms hover delay, so sweeping the pointer across a seam doesn't flash it. The one
width breakpoint (1100px) hides the modal sidebar and the critter. This is a desktop tool,
and narrow widths are a degraded mode.

The rail nests at most two levels deep:
- A **track** is an uppercase magenta label with a hairline rule running to its + (a new
  tab). It sits above a bordered **track card**. The sonar strip carries the track's state,
  so the label has no status dot.
- Inside the card, a **branch sub-cluster** is marked by a 2px violet left rule and a
  shallow indent, rather than a second card. It exists only for a branch two or more panes
  share; a pane alone on its branch shows the branch, in mono, on its selected card.
- **Pane rows** sit inside the card. Structural rows (track, branch) get 3px of
  vertical padding; pane rows get 7px, because the cards are the content and the chrome
  stays tight.

Spacing is a working rhythm of small steps (2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 20px)
rather than a token scale. The two named spacing values are `rail-indent` (16px per tree
level) and `header-gutter` (20px).

### Named Rules
**The Column Rule.** A pane's metadata lines (cwd chip, narrator status, PR and ticket
chips) each take a full line, indented 21px so they start under the label (14px icon plus a
7px gap). Fields stay in one column from row to row, and never share a line in a way that
makes their position depend on content length.

## Elevation & Depth

Depth is a tonal stack: Abyss tray (`bg-0`) → Hull card (`bg-1`) → Raised card (`bg-2`).
Each step is about +0.05 lightness at the same hue. Structure never uses shadows. Exactly
one thing in the rail lifts: the selected pane's card, which expands onto `bg-1-hi` with a
soft shadow and a 1px Periscope Glass inset ring at 45%. A pane that needs you says so with
its stripe, its blip and its Next card, not by expanding. Real shadows belong only to layers
that float above the page.

### Shadow Vocabulary
- **Pane lift** (`box-shadow: 0 1px 8px oklch(0 0 0 / .3), inset 0 0 0 1px color-mix(in oklch, var(--accent) 45%, transparent)`):
  the selected, expanded pane card.
- **Menu** (`box-shadow: 0 8px 24px oklch(0 0 0 / 0.35)`): toolbar dropdowns.
- **Toast** (`box-shadow: 0 8px 20px oklch(0 0 0 / 0.4)`): the toast stack.
- **Modal** (`box-shadow: 0 0 0 1px var(--line-strong), 0 30px 80px oklch(0 0 0 / 0.55)`):
  modals and the omnibox. The 1px ring keeps the edge visible against a dark backdrop.
- **Backdrop**: `oklch(0 0 0 / 0.55)` with `blur(2px)` behind modals. The sticky header
  blurs what scrolls under it (`blur(8px)`).
- **Drop line** (`box-shadow: inset 0 ±2px 0 var(--accent)`): where a dragged row will land.

### Named Rules
**The Tonal Stack Rule.** Surfaces separate by lightness step, not by shadow. If a new
container needs to read as raised, move it one `bg-` step up.

**The Only-What-Floats Rule.** Ambient shadows belong to menus, toasts, modals and the one
lifted pane. Nothing that sits in the layout gets a drop shadow.

## Shapes

The geometry is small and soft-cornered, with radii that scale with size:
- chips and keycaps: 3px
- rows, inputs and buttons: 4px
- menus, toasts and the expanded pane: 6px
- track cards and modals: 10px

Choice controls (filter buttons, launcher options) are full pills (999px). Status is drawn
with three small shapes:
- the **state stripe**: a 2px rounded bar inset 3px from the pane card's left edge, running
  between 6px from the top and 6px from the bottom
- the **blip**: an 8px mark in a 13px hit area, whose shape says the state as well as its
  colour, so it reads without colour too: asks is a filled circle, broken a 7px square, done
  a 2px ring, working a three-quarter sweep in amber, idle a 5px dot and a shell a 7 × 2px
  bar
- the **pulse**: a 4px ring of the state's `-bg` colour

Clawd's device is the one large rounded form: a 3px Rim bezel with 16px corners, or 20px at
the larger size. It reads as a separate gadget sitting on the header, not as a panel of it.

Icons are Unicode glyphs coloured by kind, not an icon set:
- ◆ repo (Periscope Glass)
- ◇ other
- `$` shell (Shore Green, mono)
- ✻ Claude (orange)
- ⬡ Codex (light blue, `oklch(0.78 0.10 220)`)
- ⎇ branch (Worktree Violet)
- ⧉ goal track, 🗀 project track (both Track Magenta)
- ★ pinned (Pin Gold)

## Components

Compact and tactile: small controls with a little physical weight, muted at rest and brought
forward on hover or selection.

### Buttons
- **Filter button** (header toolbar): a full pill (999px) with a transparent fill and a 1px
  `line-soft` edge, `fg-2` text, 12px, 4px × 10px. On hover the edge goes to `line`, the fill
  to `bg-1` and the text to `fg-1`. When active it gets an 18% Periscope Glass wash, a 50%
  Periscope Glass edge and `fg-0` text. Transitions take 0.12s.
- **Action variant** (`+ new`, `⋯`): the same pill with a dashed edge, which turns solid
  Periscope Glass on hover. The dashed edge means "this makes something".
- **Dialog button**: `bg-1` fill, 1px `line` edge, 4px corners, 6px × 14px. Hover goes to
  `bg-1-hi`; keyboard focus gets a 2px Periscope Glass outline offset by 1px. The **danger**
  variant is meant to be a red-tinted button, never a solid red one: `s-danger` at 25% in
  the fill (38% on hover) with a 50% danger edge. Because the mix is `in oklch` into `bg-1`,
  it currently renders violet; see the Mixed-Tint Rule.
- **Segmented pins** (spawn account, model, effort): tight 3px-cornered segments, 10px/600
  with +0.04em tracking, on `bg-2` with `fg-3` text. The active segment fills with Periscope
  Glass and its text drops to `bg-0`.

### Chips
- **Pane pill and meta chip**: 15px tall, 3px corners, 0 × 5px padding, on `bg-2` (or `bg-1`
  inside an expanded card, so the inset still reads). Refs inside are mono. The text colour
  says what kind of ref it is: PR `fg-1`, ticket Ticket Purple, account Account Teal, CI in
  its status colour, and context % in `fg-3`, turning amber above 60% and red above 80%.
  A merged PR turns Worktree Violet; a closed one dims to `fg-3` with a strikethrough.
- **Count badge** in a section header: a 9px-radius capsule, 10px/700. Next's fills with the
  status colour of its most urgent card (dark `#0d1117` text); PINNED's and ACTIVITY's sit
  on `bg-2`.

### Cards / Containers
- **Track card**: `bg-1` with 10px corners, a 1px Track Magenta edge at 30%, 5px padding and
  5–8px margins inside the rail tray. It sits under its uppercase track label. When a tab is
  dragged over it, the edge turns Periscope Glass and the fill takes an 8% wash. An empty
  card shows a dashed "drop a tab here" landing area.
- **Pane card**: a 4px-cornered row at rest, which lifts on expansion (see Elevation). The
  state stripe on its left edge carries the pane's state. Its first line ends with the PR
  chip and the row's age; on hover the pin and close actions fade in just left of them, on
  a gradient over the name's tail, so the chip stays clickable and the age stays in view.
- **Modal**: `bg-modal` with 10px corners and the Modal shadow. It fills most of the viewport
  (96vw × 94vh) and the user can resize it.

### Inputs / Fields
- **Omnibox input**: `bg-2` with a 1px `line-strong` edge, 4px corners, 15px text, 10px ×
  12px padding. On focus only the edge changes, to Periscope Glass; there is no glow or ring.
- **Prompt input** (inside dialogs): `bg-0` with a 1px `line` edge, and the same Periscope
  Glass edge on focus.

### Navigation
- **Rail row**: 13px body text and a 14px glyph column with a 7px gap. Hover lifts the fill to
  `bg-1-hi`. Selection adds `bg-1-hi` and a 2px Periscope Glass left edge.
- **Sonar strip**: the top of the rail, sticky while the tree scrolls under it, on `bg-0`
  with a `line-soft` rule below. One line per track (its magenta label, then one blip per
  pane, then the count) and a 10px legend. A blip opens its pane; a track's label scopes the
  tree to it, marked with the 18% Periscope Glass wash.
- **Next card**: what wants you, under the strip. A `bg-1` card with 6px corners and the
  pane's state stripe: name and age, then up to three lines of what the agent said (12px;
  a command in mono on `bg-2`), then the track in magenta and the reason. Three show; the
  rest fold into a "+N" row. With two or more finished cards, the title row offers "mark
  all seen", an 11px `fg-2` text button.
- **Menu**: a `bg-1` panel with a 1px `line` edge, 6px corners and 4px padding. Items are
  12px, 6px × 10px, with 4px corners. Hover fills with `bg-2` and turns the text `fg-0`;
  the active item gets the 18% Periscope Glass wash.
- **Omnibox row**: transparent at rest. The active row gets a 16% Periscope Glass wash and a
  2px Periscope Glass inset left edge, and its glyph turns Periscope Glass.

### Keycap
Shortcut glyphs (⌘K, ⌘/, Esc) are drawn as small physical keys: `bg-2`, a 1px `line-soft`
edge with a **2px bottom edge**, 3px corners, 10px mono. The thicker bottom edge is the
tactile cue the rest of the component vocabulary follows.

### Signature: the brand cursor
The wordmark `periscope` (14px/600) ends in a 7 × 13px Periscope Glass block that blinks
(`steps(2)`, 1.1s), like a terminal cursor waiting for input. It is the system's only purely
expressive animation outside the critters, and reduced motion stops it.

### Signature: Clawd's device
A 136 × 96px screen (204 × 144px when the header has three rows of pins) in a 3px Rim bezel
with 16px corners (20px at the larger size), filled with `bg-1`. On hover the bezel turns
`fg-3`. Inside, the pixel critters animate on their own schedule: blinking, breathing,
bobbing, striding and playing instruments. The bezel keeps their motion clearly inside the
gadget.

### Motion
- **Hover:** 0.1–0.12s colour, background and edge transitions.
- **Data-driven change:** 180ms `ease` on the colour, background and edge of stripes, blips,
  chips and rows, so live state doesn't flicker as it updates.
- **Row entry:** 140ms `ease-out`, fading in from 2px above. Rows animate in only; there is
  no exit animation.
- **Pulse:** only needs-input pulses (its stripe and its blip), on a 1.6s `ease-in-out`
  loop, as a 4px ring of `s-needs-bg`. Nothing else in the work surface loops.
- **Reduced motion:** `prefers-reduced-motion` stops the cursor blink, row entry, the
  needs-input pulse and the critters, and cuts every transition to near zero.

## Do's and Don'ts

### Do:
- **Do** keep every surface in the cool-grey family (hue 250, chroma ≤ 0.010) and build
  depth by stepping through `bg-0` → `bg-1` → `bg-2`.
- **Do** give each hue a single meaning everywhere it appears: Periscope Glass for
  interaction, Track Magenta for tracks, Worktree Violet for branches, Ticket Purple for
  tickets, Account Teal for accounts, and the sonar colours for state.
- **Do** put any new status colour on the shared brightness (lightness 0.74–0.80, chroma
  0.13–0.17) and vary only its hue.
- **Do** derive tints with `color-mix(in oklch, <token> N%, transparent)`, or `in srgb` when
  mixing into an opaque surface, instead of adding new hex values.
- **Do** set refs, paths, branches and commands in `--mono`, and check every new size in
  every face on the Settings → Fonts list.
- **Do** show shortcuts as keycaps (the 2px bottom edge).
- **Do** register every new animation in the `prefers-reduced-motion` block.
- **Do** keep critters inside a bezel or their own frame wherever they appear.
- **Do** match `bg-term` (`#282c34`) exactly on anything that frames a terminal canvas.

### Don't:
- **Don't** use colour as decoration. If a hue isn't reporting a state or marking a kind of
  thing, it doesn't ship.
- **Don't** reach for SaaS analytics vocabulary: KPI tiles, decorative charts, gradient hero
  cards, generous empty spacing around a few numbers.
- **Don't** go neon hacker terminal: no green on black, glow, scanlines or "cyber" styling.
- **Don't** add a second interactive accent, and don't use Periscope Glass to report agent
  state.
- **Don't** make one state louder than another by brightness. Urgency comes from the pulse
  and from position.
- **Don't** pulse or loop anything in the work surface except needs-input.
- **Don't** put drop shadows on things that sit in the layout. Shadows are for menus,
  toasts, modals and the one lifted pane.
- **Don't** add half-pixel type sizes or new hard-coded hex values. Both exist today as
  drift, not as patterns.
