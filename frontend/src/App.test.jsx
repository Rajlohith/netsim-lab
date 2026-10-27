import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import App from "./App.jsx";

test("renders title", () => {
  vi.stubGlobal("fetch", () => new Promise(() => {}));
  render(<App />);
  expect(screen.getByText("NetSim Lab")).toBeTruthy();
});
