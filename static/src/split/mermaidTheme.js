// Mermaid's "base" theme in periscope's colors (THI-443 D4), app-wide: file
// previews draw diagrams with it too. `color(name)` returns a token as
// #rrggbb. mermaid's color math can't parse the stylesheet's oklch(), so
// Mermaid.jsx converts each token before it gets here. Pure, so the mapping is
// testable without a DOM.
export function mermaidTheme({ color, font }) {
  const card = color("--bg-1");
  const raised = color("--bg-1-hi");
  const line = color("--fg-2"); // ≥ 3:1 against a card: borders and arrows must read
  return {
    theme: "base",
    themeVariables: {
      darkMode: true,
      fontFamily: font,
      fontSize: "14px",
      background: card,
      primaryColor: color("--bg-2"),
      primaryTextColor: color("--fg-0"),
      nodeTextColor: color("--fg-0"),
      titleColor: color("--fg-0"),
      primaryBorderColor: line,
      lineColor: line,
      clusterBorder: line,
      secondaryColor: raised,
      clusterBkg: raised,
      tertiaryColor: card,
      edgeLabelBackground: card,
      textColor: color("--fg-1"),
    },
  };
}
