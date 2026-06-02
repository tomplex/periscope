import { defineConfig } from "vitest/config";
import preact from "@preact/preset-vite";

// Test config — covers ONLY pure data transforms (selectors, parsers).
// React/Preact components are verified in the browser per CLAUDE.md
// ("UI work: test in the browser"). Add component tests only for state
// reducers where the browser is a bad oracle.
export default defineConfig({
  plugins: [preact()],
  test: {
    include: ["static/src/**/__tests__/**/*.test.{js,jsx}"],
    environment: "node",
  },
});
