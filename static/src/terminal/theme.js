// xterm.js theme tokens. One object, used by terminalCore. Refinements vs
// the original inline theme:
//   - cursor color sharpened (was #58a6ff, now #7aa2f7) so the blink reads
//     against #282c34 instead of washing out
//   - selectionBackground darkened slightly so selections over Claude's
//     yellow status line stay legible
//   - brightGreen/Red slightly more saturated for diff readability
// Background unchanged — muscle memory + existing screenshots.

export const terminalTheme = {
  background: "#282c34",
  foreground: "#e6edf3",
  cursor: "#7aa2f7",
  cursorAccent: "#282c34",
  selectionBackground: "rgba(88,166,255,0.28)",
  black: "#1d1f21",        red: "#cc6666",  green: "#b5bd68",
  yellow: "#f0c674",       blue: "#81a2be", magenta: "#b294bb",
  cyan: "#8abeb7",         white: "#c5c8c6",
  brightBlack: "#969896",  brightRed: "#ff7373",
  brightGreen: "#cce29b",  brightYellow: "#ffd47b",
  brightBlue: "#9ec5fe",   brightMagenta: "#d8b6db",
  brightCyan: "#a8e0d8",   brightWhite: "#ffffff",
};
