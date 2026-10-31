// Integration test of the dashboard's API layer against a RUNNING backend. Skipped unless NETSIM_API_URL is set, e.g.
//   NETSIM_API_URL=http://127.0.0.1:8000/api npm test        (real FastAPI app:  make run-api)
// or via scripts/run-integration.sh, which serves the real service layer with the stdlib adapter (see docs/FRONTEND.md for what that does and does not prove).
import { describe, expect, it } from "vitest";
import { createApi } from "../api/endpoints.js";
import { createClient, ApiError } from "../api/client.js";
import { createPoller } from "../lib/poll.js";
import { TERMINAL } from "../lib/experimentActions.js";
import { comparisonRows, comparisonExportRows, extractRunMetrics, handoffSeries, METRIC_KEYS } from "../lib/metrics.js";
import { initialForm, formToRequest, validateForm } from "../lib/validation.js";

const URL_ = typeof process !== "undefined" ? process.env.NETSIM_API_URL : undefined;
const live = it.skipIf ? it.skipIf(!URL_) : it;
const api = URL_ ? createApi(createClient({ baseUrl: URL_ })) : null;

function waitFor(id, ms = 120000) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("timeout waiting for " + id)), ms);
    const p = createPoller({ intervalMs: 200, fn: () => api.getExperiment(id), shouldStop: (e) => TERMINAL.has(e.status), onResult: (e) => { if (TERMINAL.has(e.status)) { clearTimeout(t); resolve(e); } }, onError: (e) => { clearTimeout(t); p.stop(); reject(e); } });
    p.start();
  });
}

describe("dashboard API layer against a running backend", () => {
  live("reads the catalogue and the topology", async () => {
    const [a, s, c, t, m] = await Promise.all([api.approaches(), api.scenarios(), api.capabilities(), api.topology(), api.models()]);
    expect(a.approaches.length > 0).toBe(true); expect(s.scenarios.length > 0).toBe(true); expect(c.limits.max_duration_s > 0).toBe(true); expect(t.nodes.length > 0).toBe(true); expect(Array.isArray(m.models)).toBe(true);
    expect(c.live_updates.websocket).toBe(false);   // the dashboard polls because the backend has no WebSocket
  });
  live("a form built from the catalogue is accepted by /experiments/validate", async () => {
    const [a, s, c] = await Promise.all([api.approaches(), api.scenarios(), api.capabilities()]);
    const ap = a.approaches.find((x) => x.id === "mip-basic"), sc = s.scenarios[0];
    const form = initialForm(sc, ap), ctx = { capabilities: c, approach: ap, scenario: sc };
    expect(validateForm(form, ctx).errors).toEqual({});
    const rep = await api.validateExperiment(formToRequest(form, ctx)); expect(rep.valid).toBe(true);
  });
  live("surfaces server validation errors as ApiError", async () => {
    const e = await api.startExperiment({ approach: "mip-basic", environment: "live" }).catch((x) => x);
    expect(e instanceof ApiError).toBe(true); expect(e.status).toBe(422); expect(e.issues.length > 0).toBe(true);
  });
  live("start → poll → completed; metrics, handoffs and exports agree with what the charts use", async () => {
    const key = `it-${Date.now()}`;
    const first = await api.startExperiment({ approach: "mip-mbb", handoff_mode: "mbb", scenario: "baseline", seed: 3, label: "integration test" }, key);
    expect(first.status).toBe(202);
    const again = await api.startExperiment({ approach: "mip-mbb", handoff_mode: "mbb", scenario: "baseline", seed: 3, label: "integration test" }, key);
    expect(again.data.id).toBe(first.data.id); expect(again.replayed).toBe(true);        // idempotent: a double click makes ONE experiment
    const done = await waitFor(first.data.id);
    expect(done.status).toBe("completed");
    const [metrics, handoffs] = await Promise.all([api.metrics(done.id), api.handoffs(done.id)]);
    const x = extractRunMetrics(metrics.metrics);
    // JSON export == the document the charts were built from
    const json = JSON.parse(await (await api.exportExperiment(done.id, { format: "json" })).blob.text());
    expect(JSON.stringify(json.metrics.comparison_metrics ?? json.metrics)).toBe(JSON.stringify(metrics.metrics));
    // CSV export rows == per-handoff series used by the bar charts
    const csv = (await (await api.exportExperiment(done.id, { format: "csv", table: "handoffs" })).blob.text()).trim().split(/\r?\n/);
    const head = csv[0].split(","); const rows = csv.slice(1).map((l) => l.split(","));
    expect(rows).toHaveLength(handoffs.handoffs.length);
    const col = head.indexOf("handoff_latency_ms"); const series = handoffSeries(handoffs.handoffs).series[0].values;
    rows.forEach((r, i) => expect(r[col] === "" ? null : Number(r[col])).toBe(series[i]));
    expect(x.loss_percent).toBe(metrics.metrics.loss.percent);
    const row = comparisonRows([{ experiment: done, metrics: metrics.metrics }]);
    expect(comparisonExportRows(row)[0].loss_percent).toBe(metrics.metrics.loss.percent); expect(METRIC_KEYS.length > 5).toBe(true);
  });
  live("a started experiment can be stopped", async () => {
    const r = await api.startExperiment({ approach: "tcp-reconnect", scenario: "baseline", seed: 11, duration_s: 20 }, `it-stop-${Date.now()}`);
    await api.cancelExperiment(r.data.id).catch((e) => { if (e.code !== "not_cancellable") throw e; });
    const e = await waitFor(r.data.id);
    expect(["cancelled", "completed"]).toContain(e.status);
  });
  live("history lists the experiments and exports them", async () => {
    const l = await api.listExperiments({ limit: 5 }); expect(l.items.length > 0).toBe(true);
    const f = await api.exportHistory({}, "csv"); expect((await f.blob.text()).split(/\r?\n/)[0]).toContain("id");
  });
  live("unknown experiment is a 404 ApiError", async () => {
    const e = await api.getExperiment("exp-0000000000000000").catch((x) => x); expect(e.status).toBe(404);
  });
});
