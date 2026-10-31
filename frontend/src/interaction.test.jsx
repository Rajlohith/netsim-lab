// Interaction tests (user events in a DOM). These need @testing-library/react + jsdom, i.e. `npm install && npm test` (vitest).
// They could NOT be executed in the environment Stage 9 was built in (no network, so no jsdom): see docs/FRONTEND.md "Verification status".
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "./App.jsx";
import { ApiProvider } from "./hooks/api.jsx";
import LivePage from "./pages/LivePage.jsx";
import { ExportButtons } from "./components/Common.jsx";
import { ComparisonView } from "./pages/ComparePage.jsx";
import { HistoryExport } from "./pages/HistoryPage.jsx";
import { comparisonRows } from "./lib/metrics.js";
import { fx, makeApi } from "./test/mockApi.js";

beforeEach(() => { window.location.hash = ""; });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const open = (hash, api) => { window.location.hash = hash; return render(<App api={api} />); };

describe("navigation", () => {
  it("moves between pages with the navigation links", async () => {
    open("#/topology", makeApi());
    expect(await screen.findByRole("heading", { level: 1, name: "Network topology" })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "Model evaluation" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Model evaluation" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Model evaluation" }).getAttribute("aria-current")).toBe("page");
  });
  it("shows an error state with retry when the backend is down", async () => {
    const api = makeApi({ topology: vi.fn().mockRejectedValue(Object.assign(new Error("cannot reach the API"), { code: "network_error", issues: [] })) });
    open("#/topology", api);
    expect(await screen.findByRole("alert")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.topology).toHaveBeenCalledTimes(2));
  });
});

describe("experiment controls", () => {
  it("blocks an invalid start without calling the server, then starts a valid one once", async () => {
    const api = makeApi(); open("#/configure", api);
    const duration = await screen.findByLabelText(/Duration/);
    fireEvent.change(duration, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Start experiment" }));
    expect(await screen.findByText(/duration must be/)).toBeTruthy(); expect(api.startExperiment).toHaveBeenCalledTimes(0);
    fireEvent.change(duration, { target: { value: "10" } });
    const start = screen.getByRole("button", { name: "Start experiment" });
    fireEvent.click(start); fireEvent.click(start);
    await waitFor(() => expect(api.startExperiment).toHaveBeenCalledTimes(1));
    expect(typeof api.startExperiment.mock.calls[0][1]).toBe("string");   // idempotency key
    await waitFor(() => expect(window.location.hash).toContain("#/live/"));
  });
  it("shows the server's field errors when it rejects the request", async () => {
    const { ApiError } = await import("./api/client.js");
    const api = makeApi({ startExperiment: vi.fn().mockRejectedValue(new ApiError({ status: 422, code: "validation_error", message: "bad", issues: [{ field: "seed", message: "seed is reserved" }] })) });
    open("#/configure", api);
    fireEvent.click(await screen.findByRole("button", { name: "Start experiment" }));
    expect(await screen.findByText("seed is reserved")).toBeTruthy();
  });
  it("stop needs a confirmation", async () => {
    const running = { ...fx.list.items[0], status: "running" };
    const api = makeApi({ listExperiments: vi.fn(async () => ({ items: [running], total: 1, limit: 50, offset: 0 })) });
    open("#/configure", api);
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    expect(api.cancelExperiment).toHaveBeenCalledTimes(0);
    fireEvent.click(screen.getByRole("button", { name: "Yes, stop it" }));
    await waitFor(() => expect(api.cancelExperiment).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/cancelled/i)).toBeTruthy();
  });
});

describe("live metrics polling", () => {
  it("polls a running experiment and loads the results when it completes", async () => {
    let n = 0;
    const api = makeApi({ getExperiment: vi.fn(async () => (++n < 2 ? { ...fx.expMbb, status: "running", progress: { phase: "running_experiment", fraction: null }, finished_at: null } : fx.expMbb)) });
    render(<ApiProvider api={api}><LivePage id={fx.expMbb.id} /></ApiProvider>);
    expect(await screen.findByText("running")).toBeTruthy();
    expect(await screen.findByText(/Measured values/, {}, { timeout: 5000 })).toBeTruthy();
    expect(api.metrics).toHaveBeenCalledTimes(1);
  });
  it("tells the user when polling fails and keeps retrying", async () => {
    let n = 0;
    const api = makeApi({ getExperiment: vi.fn(async () => { if (++n === 2) throw new Error("boom"); return { ...fx.expMbb, status: "running", finished_at: null }; }) });
    render(<ApiProvider api={api}><LivePage id={fx.expMbb.id} /></ApiProvider>);
    expect(await screen.findByText(/Connection problem/, {}, { timeout: 6000 })).toBeTruthy();
  });
});

describe("exports", () => {
  it("downloads a server export and reports its name", async () => {
    const api = makeApi(); const save = vi.fn();
    render(<ExportButtons api={api} id={fx.expMbb.id} predictionAvailable save={save} />);
    fireEvent.click(screen.getByRole("button", { name: "CSV · handoffs" }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(api.exportExperiment).toHaveBeenCalledWith(fx.expMbb.id, { format: "csv", table: "handoffs" });
    expect(await screen.findByText(/Saved/)).toBeTruthy();
  });
  it("shows an export failure instead of failing silently", async () => {
    const api = makeApi({ exportExperiment: vi.fn().mockRejectedValue(Object.assign(new Error("not finished"), { code: "results_not_available", issues: [] })) });
    render(<ExportButtons api={api} id="exp-1" save={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
  it("history export passes the active filters", async () => {
    const api = makeApi(); const save = vi.fn();
    render(<HistoryExport api={api} filters={{ status: "failed", approach: "" }} save={save} />);
    fireEvent.click(screen.getByRole("button", { name: "CSV" }));
    await waitFor(() => expect(api.exportHistory).toHaveBeenCalledWith({ status: "failed", approach: "" }, "csv"));
  });
  it("comparison export contains exactly the rows on screen", () => {
    const rows = comparisonRows([{ experiment: fx.expBasic, metrics: fx.mBasic.metrics }, { experiment: fx.expMbb, metrics: fx.mMbb.metrics }]);
    const save = vi.fn(); render(<ComparisonView rows={rows} save={save} />);
    fireEvent.click(screen.getByRole("button", { name: "CSV" }));
    const [csv, name] = save.mock.calls[0]; expect(name).toBe("netsim-comparison.csv");
    expect(csv.trim().split("\r\n")).toHaveLength(rows.length + 1);
    rows.forEach((r) => expect(csv).toContain(r.id));
  });
});

describe("history filters", () => {
  it("re-queries the API when the status filter changes", async () => {
    const api = makeApi(); open("#/history", api);
    const select = await screen.findByLabelText("Status");
    fireEvent.change(select, { target: { value: "failed" } });
    await waitFor(() => expect(api.listExperiments.mock.calls.some((c) => c[0].status === "failed")).toBe(true));
  });
});
