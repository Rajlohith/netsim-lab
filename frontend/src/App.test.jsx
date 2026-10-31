import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import App from "./App.jsx";

// The original Stage 1 smoke test, adapted to the dashboard shell: the title and the navigation exist before any data loads.
describe("App shell", () => {
  it("renders the title and the main navigation", () => {
    const html = renderToString(<App api={{ health: () => new Promise(() => {}) }} initialHash="#/topology" />);
    expect(html).toContain("NetSim Lab");
    expect(html).toContain('aria-label="Main navigation"');
  });
});
