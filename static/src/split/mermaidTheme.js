// Mermaid's "base" theme in periscope's colors (THI-443 D4), app-wide: file
// previews draw diagrams with it too. `color(name)` returns a token as
// #rrggbb, or undefined when the token isn't set. mermaid's color math can't
// parse the stylesheet's oklch(), so Mermaid.jsx converts each token before
// it gets here. Pure, so the mapping is testable without a DOM.

// Pie slices, cScale (mindmap/timeline/journey/kanban) and git branch colors
// all default to rotating primaryColor's hue. primaryColor is --bg-2, a
// near-grey, so every category collapses to the same grey. Cycle the
// dashboard's own status/accent colors instead so categories stay
// distinguishable, app-wide.
const CATEGORIES = ["--accent", "--s-done", "--s-working", "--s-needs", "--effort", "--worktree", "--s-shell", "--s-danger"];

export function mermaidTheme({ color, font }) {
  const card = color("--bg-1");
  const raised = color("--bg-1-hi");
  const line = color("--fg-2"); // ≥ 3:1 against a card: borders and arrows must read
  const label = color("--fg-0");
  const body = color("--fg-1");
  const dark = color("--bg-0"); // dark text, for labels drawn on the mid-light category fills
  const cats = CATEGORIES.map(color);

  const vars = {
    darkMode: true,
    fontFamily: font,
    fontSize: "14px",
    background: card,
    primaryColor: color("--bg-2"),
    primaryTextColor: label,
    nodeTextColor: label,
    titleColor: label,
    primaryBorderColor: line,
    lineColor: line,
    clusterBorder: line,
    secondaryColor: raised,
    clusterBkg: raised,
    tertiaryColor: card,
    edgeLabelBackground: card,
    textColor: body,

    pieSectionTextColor: dark,
    pieTitleTextColor: label,
    pieLegendTextColor: body,
    pieStrokeColor: card,
    pieOuterStrokeColor: line,
    // mermaid's default 0.7 blends a slice into the card, dropping pie4-pie8's
    // dark labels below 4.5:1. A string: mermaid parses this one as CSS, not a color.
    pieOpacity: "1",

    noteBkgColor: raised,
    noteTextColor: label,
    noteBorderColor: line,
    sequenceNumberColor: card,
    altSectionBkgColor: raised,
    taskTextDarkColor: card,
    activeTaskBkgColor: cats[0],
    critBkgColor: color("--s-danger"), // mermaid's default read ~3.6:1 against the card
    excludeBkgColor: raised, // mermaid's default is a flat #eeeeee, blind to the theme
  };
  for (let i = 0; i < 12; i++) {
    vars[`pie${i + 1}`] = cats[i % cats.length];
    vars[`cScale${i}`] = cats[i % cats.length];
    vars[`cScaleLabel${i}`] = dark;
  }
  for (let i = 0; i < cats.length; i++) {
    vars[`git${i}`] = cats[i];
    vars[`gitBranchLabel${i}`] = dark;
  }

  // mermaid re-applies every key in themeVariables over its computed
  // defaults; an explicit undefined would blank a key a missing token
  // shouldn't touch.
  for (const k of Object.keys(vars)) {
    if (vars[k] === undefined) delete vars[k];
  }

  return { theme: "base", themeVariables: vars };
}
