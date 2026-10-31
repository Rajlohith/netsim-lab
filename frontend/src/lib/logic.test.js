import { describe, expect, it, vi } from "vitest";
import { initialForm, validateForm, formToRequest, runnableEnvironments, issuesToFieldErrors } from "./validation.js";
import { submitExperiment, checkWithServer, stopExperiment, newIdempotencyKey } from "./experimentActions.js";
import { ApiError } from "../api/client.js";
import { extractRunMetrics, comparisonRows, comparabilityNotes, chartSpec, COMPARISON_CHARTS, COMPARISON_COLUMNS, comparisonExportRows, handoffSeries, eventTimeline, pairHandoffsWithEvents, predictionSeries, METRIC_KEYS } from "./metrics.js";
import { toCsv } from "./csv.js";
import approaches from "../test/fixtures/api/approaches.json";
import scenarios from "../test/fixtures/api/scenarios.json";
import capabilities from "../test/fixtures/api/capabilities.json";
import expBasic from "../test/fixtures/api/exp_basic.json";
import expMbb from "../test/fixtures/api/exp_mbb.json";
import expTcp from "../test/fixtures/api/exp_tcp.json";
import mBasic from "../test/fixtures/api/exp_basic_metrics.json";
import mMbb from "../test/fixtures/api/exp_mbb_metrics.json";
import mTcp from "../test/fixtures/api/exp_tcp_metrics.json";
import hMbb from "../test/fixtures/api/exp_mbb_handoffs.json";
import eMbb from "../test/fixtures/api/exp_mbb_events.json";
import pMbb from "../test/fixtures/api/exp_mbb_predictions.json";

const A = approaches.approaches, S = scenarios.scenarios;
const approach = (id) => A.find((a) => a.id === id);
const ctx = (aid = "mip-basic", sid = S[0].id) => ({ capabilities, approach: approach(aid), scenario: S.find((s) => s.id === sid) });
const form = (aid = "mip-basic", sid = S[0].id, patch = {}) => ({ ...initialForm(S.find((s) => s.id === sid), approach(aid)), ...patch });

describe("validation (limits from /api/capabilities)", () => {
  it("accepts the defaults for every runnable approach", () => {
    for (const a of A.filter((x) => runnableEnvironments(x).length)) expect(validateForm(form(a.id), ctx(a.id)).errors).toEqual({});
  });
  it("rejects out-of-range and malformed numbers with field-specific messages", () => {
    const e = validateForm(form("mip-basic", S[0].id, { duration_s: "500", rate_pps: "abc", payload: "10", seed: "-1" }), ctx()).errors;
    expect(Object.keys(e).sort()).toEqual(["duration_s", "payload", "rate_pps", "seed"]);
  });
  it("rejects too many probes", () => {
    const e = validateForm(form("mip-basic", S[0].id, { duration_s: "100", rate_pps: "2000" }), ctx()).errors;
    expect(Object.keys(e).length > 0).toBe(true);
  });
  it("rejects a duration that ends before the scenario's handoffs", () => {
    expect(validateForm(form("mip-basic", S[0].id, { duration_s: "1" }), ctx()).errors.duration_s).toContain("scenario moves");
  });
  it("rejects make-before-break for approaches that cannot do it", () => {
    const noMbb = A.find((a) => !a.supports_make_before_break && runnableEnvironments(a).length);
    expect(validateForm(form(noMbb.id, S[0].id, { handoff_mode: "mbb" }), ctx(noMbb.id)).errors.handoff_mode).toBeTruthy();
  });
  it("rejects environments the server cannot run (live is never exposed)", () => {
    expect(validateForm(form("mip-basic", S[0].id, { environment: "live" }), ctx()).errors.environment).toBeTruthy();
  });
  it("validates condition overrides", () => {
    const e = validateForm(form("mip-basic", S[0].id, { conditions: { latency_scale: "0", loss_percent: "100", ha_distance_ms: "x", registration_loss_percent: "", ha_processing_ms: "" } }), ctx()).errors;
    expect(Object.keys(e).sort()).toEqual(["conditions.ha_distance_ms", "conditions.latency_scale", "conditions.loss_percent"]);
  });
  it("builds a minimal request: unset conditions are not sent; mbb only where supported", () => {
    const r = formToRequest(form("mip-basic"), ctx());
    expect(r.conditions).toBeUndefined(); expect(r.handoff_mode).toBeUndefined(); expect(r.approach).toBe("mip-basic"); expect(typeof r.duration_s).toBe("number");
    const m = formToRequest(form("mip-mbb", S[0].id, { handoff_mode: "mbb", predict: true, conditions: { ...form().conditions, loss_percent: "2" } }), ctx("mip-mbb"));
    expect(m.handoff_mode).toBe("mbb"); expect(m.predict).toBe(true); expect(m.conditions).toEqual({ loss_percent: 2 });
  });
  it("maps server issues onto fields", () => {
    expect(issuesToFieldErrors([{ field: "duration_s", message: "m" }, { field: null, message: "g" }])).toEqual({ fields: { duration_s: "m" }, general: ["g"] });
  });
});

describe("experiment actions", () => {
  const okExp = { id: "exp-0123456789abcdef", status: "queued", warnings: [] };
  it("does not call the server when client validation fails", async () => {
    const api = { startExperiment: vi.fn() };
    const r = await submitExperiment(api, form("mip-basic", S[0].id, { duration_s: "0" }), ctx(), "k");
    expect(r.ok).toBe(false); expect(r.stage).toBe("client"); expect(api.startExperiment).toHaveBeenCalledTimes(0);
  });
  it("starts with the idempotency key and returns the experiment", async () => {
    const api = { startExperiment: vi.fn(async () => ({ data: okExp, status: 202, replayed: false })) };
    const r = await submitExperiment(api, form(), ctx(), "key-9");
    expect(r.ok).toBe(true); expect(r.experiment.id).toBe(okExp.id); expect(api.startExperiment.mock.calls[0][1]).toBe("key-9");
  });
  it("surfaces server validation failures per field", async () => {
    const api = { startExperiment: async () => { throw new ApiError({ status: 422, code: "validation_error", message: "bad", issues: [{ field: "seed", message: "seed taken" }] }); } };
    const r = await submitExperiment(api, form(), ctx(), "k");
    expect(r.ok).toBe(false); expect(r.stage).toBe("server"); expect(r.fieldErrors.seed).toBe("seed taken");
  });
  it("returns (not throws) a busy/queue-full error so the UI can show it", async () => {
    const api = { startExperiment: async () => { throw new ApiError({ status: 429, code: "queue_full", message: "queue is full" }); } };
    const r = await submitExperiment(api, form(), ctx(), "k");
    expect(r.error.code).toBe("queue_full");
  });
  it("checkWithServer reports validity from /experiments/validate", async () => {
    const api = { validateExperiment: async () => ({ valid: true, issues: [], warnings: ["w1"], resolved: { fingerprint: "abc" } }) };
    const r = await checkWithServer(api, form(), ctx());
    expect(r.valid).toBe(true); expect(r.warnings).toContain("w1");
  });
  it("stop: ok, accepted (202) and already-finished", async () => {
    expect((await stopExperiment({ cancelExperiment: async () => ({ data: { status: "cancelled" }, status: 200 }) }, "x")).ok).toBe(true);
    expect((await stopExperiment({ cancelExperiment: async () => ({ data: {}, status: 202 }) }, "x")).accepted).toBe(true);
    const r = await stopExperiment({ cancelExperiment: async () => { throw new ApiError({ status: 409, code: "not_cancellable", message: "done" }); } }, "x");
    expect(r.ok).toBe(false); expect(r.message).toContain("already finished");
  });
  it("idempotency keys are unique and short enough", () => {
    expect(newIdempotencyKey() === newIdempotencyKey()).toBe(false); expect(newIdempotencyKey().length <= 64).toBe(true);
  });
});

describe("metrics from real backend output", () => {
  it("extracts the measured values without inventing any", () => {
    const m = extractRunMetrics(mMbb.metrics);
    expect(m.rtt_p50_ms).toBe(mMbb.metrics.rtt_ms.p50); expect(m.loss_percent).toBe(mMbb.metrics.loss.percent); expect(m.goodput_kbps).toBeCloseTo(mMbb.metrics.goodput_bps / 1000, 6);
    expect(m.handoff_count).toBe(mMbb.metrics.handoff_summary.count); expect(m.jitter_ms).toBeNull(); expect(m.origin).toBe("simulated");
  });
  it("keeps unmeasurable interruption as null, not 0", () => {
    const hs = mTcp.metrics.handoffs; const unmeasured = hs.filter((h) => h.interruption_ms === null).length;
    const m = extractRunMetrics(mTcp.metrics);
    expect(m.interruption_unmeasured).toBe(unmeasured);
    if (unmeasured === hs.length) expect(m.interruption_mean_ms).toBeNull();
  });
  it("copes with an empty/partial document", () => {
    const m = extractRunMetrics({}); expect(m.loss_percent).toBeNull(); expect(m.handoff_count).toBeNull(); expect(extractRunMetrics(null).rtt_p50_ms).toBeNull();
  });
  const entries = [{ experiment: expBasic, metrics: mBasic.metrics }, { experiment: expMbb, metrics: mMbb.metrics }, { experiment: expTcp, metrics: mTcp.metrics }];
  it("comparison rows carry the run's own configuration", () => {
    const rows = comparisonRows(entries);
    expect(rows.map((r) => r.id)).toEqual(entries.map((e) => e.experiment.id)); expect(rows[1].handoff_mode).toBe(expMbb.configuration.handoff_mode); expect(rows[0].seed).toBe(expBasic.configuration.seed);
  });
  it("flags runs that were not made under identical conditions", () => {
    const notes = comparabilityNotes(comparisonRows(entries)).join(" ");
    expect(notes).toContain("scenario differs");
  });
  it("says nothing is different when the same configuration is compared with itself", () => {
    expect(comparabilityNotes(comparisonRows([entries[0], entries[0]]))).toEqual([]);
  });
  it("chart series, table values and CSV export come from the same rows", () => {
    const rows = comparisonRows(entries);
    for (const def of COMPARISON_CHARTS) {
      const spec = chartSpec(def, rows);
      def.metrics.forEach((k, si) => rows.forEach((r, ri) => expect(spec.series[si].values[ri]).toBe(r.values[k])));
    }
    const csv = toCsv(COMPARISON_COLUMNS, comparisonExportRows(rows)).trim().split("\r\n");
    expect(csv).toHaveLength(rows.length + 1);
    const header = csv[0].split(",");
    expect(METRIC_KEYS.every((k) => header.some((h) => h.startsWith(k)))).toBe(true);
    const exp = comparisonExportRows(rows);
    rows.forEach((r, i) => METRIC_KEYS.forEach((k) => expect(exp[i][k]).toBe(r.values[k])));
  });
  it("per-handoff series match the handoffs endpoint", () => {
    const s = handoffSeries(hMbb.handoffs);
    expect(s.categories).toEqual(hMbb.handoffs.map((h) => h.event_id)); expect(s.series[0].values).toEqual(hMbb.handoffs.map((h) => h.handoff_latency_ms));
  });
  it("timeline pairs handoffs with link-change events only when counts match", () => {
    const tl = eventTimeline(eMbb.events);
    expect(tl.points).toHaveLength(eMbb.events.length); expect(tl.tMin <= tl.tMax).toBe(true);
    const pairs = pairHandoffsWithEvents(hMbb.handoffs, eMbb.events);
    if (pairs) expect(pairs).toHaveLength(hMbb.handoffs.length);
    expect(pairHandoffsWithEvents(hMbb.handoffs.slice(1), eMbb.events)).toBeNull();
  });
  it("prediction series convert ns to seconds and keep the model's alarm flags", () => {
    const s = predictionSeries(pMbb.decision_points.items);
    expect(s).toHaveLength(pMbb.decision_points.items.length); expect(s[0].t).toBeCloseTo(pMbb.decision_points.items[0].decision_time_ns / 1e9, 9);
    expect(s.filter((p) => p.alarm).length).toBe(pMbb.decision_points.items.filter((r) => r.alarm).length);
  });
});
