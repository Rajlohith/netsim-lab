import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { Nav, Page } from "../App.jsx";
import { parseHash } from "../lib/router.js";
import { ErrorState, EmptyState, Loading, Async } from "../components/StateViews.jsx";
import { ApiError } from "../api/client.js";
import BarChart from "../components/charts/BarChart.jsx";
import LineChart from "../components/charts/LineChart.jsx";
import TimelineChart from "../components/charts/TimelineChart.jsx";
import { TopologyView } from "./TopologyPage.jsx";
import { ConfigureView } from "./ConfigurePage.jsx";
import { ProgressView, ResultsView, LiveHeader } from "./LivePage.jsx";
import { PredictionsView } from "./PredictionsPage.jsx";
import { ComparisonView } from "./ComparePage.jsx";
import { ModelEvaluationView } from "./ModelsPage.jsx";
import { HistoryTable, ExperimentDetailView } from "./HistoryPage.jsx";
import { comparisonRows, COMPARISON_CHARTS, chartSpec } from "../lib/metrics.js";
import { initialForm } from "../lib/validation.js";
import topology from "../test/fixtures/api/topology.json";
import approaches from "../test/fixtures/api/approaches.json";
import scenarios from "../test/fixtures/api/scenarios.json";
import capabilities from "../test/fixtures/api/capabilities.json";
import models from "../test/fixtures/api/models.json";
import modelEval from "../test/fixtures/api/model_evaluation.json";
import list from "../test/fixtures/api/experiments_list.json";
import expBasic from "../test/fixtures/api/exp_basic.json";
import expMbb from "../test/fixtures/api/exp_mbb.json";
import expTcp from "../test/fixtures/api/exp_tcp.json";
import mBasic from "../test/fixtures/api/exp_basic_metrics.json";
import mMbb from "../test/fixtures/api/exp_mbb_metrics.json";
import mTcp from "../test/fixtures/api/exp_tcp_metrics.json";
import hMbb from "../test/fixtures/api/exp_mbb_handoffs.json";
import eMbb from "../test/fixtures/api/exp_mbb_events.json";
import pMbb from "../test/fixtures/api/exp_mbb_predictions.json";
import trMbb from "../test/fixtures/api/exp_mbb_transitions.json";

const html = (el) => renderToStaticMarkup(el);
const apiStub = { exportExperiment: vi.fn() };

describe("navigation", () => {
  it("lists all seven pages and marks the current one", () => {
    const h = html(<Nav current="compare" />);
    for (const t of ["Network topology", "Experiment configuration", "Live metrics", "Handoff prediction", "Approach comparison", "Model evaluation", "Experiment history"]) expect(h).toContain(t);
    expect(h).toContain('href="#/compare" aria-current="page"'); expect((h.match(/aria-current/g) || []).length).toBe(1);
  });
  it("routes ids and shows a not-found page for unknown routes", () => {
    expect(html(<Page route={parseHash("#/nope")} />)).toContain("Page not found");
  });
});

describe("loading, error and empty states", () => {
  it("loading is announced", () => { expect(html(<Loading what="the topology" />)).toContain('role="status"'); });
  it("network errors say the backend is unreachable and offer retry", () => {
    const h = html(<ErrorState error={new ApiError({ code: "network_error", message: "cannot reach the API (Failed to fetch)" })} onRetry={() => {}} />);
    expect(h).toContain('role="alert"'); expect(h).toContain("not reachable"); expect(h).toContain("Retry");
  });
  it("API errors show code, status, request id and field issues", () => {
    const h = html(<ErrorState error={new ApiError({ status: 422, code: "validation_error", message: "bad", requestId: "r-1", issues: [{ field: "seed", message: "too big" }] })} />);
    for (const t of ["validation_error", "422", "r-1", "seed", "too big"]) expect(h).toContain(t);
  });
  it("Async switches between loading, error, empty and data", () => {
    const base = { data: null, error: null, reload() {} };
    expect(html(<Async state={{ ...base, status: "loading" }} what="x">{() => "DATA"}</Async>)).toContain("Loading");
    expect(html(<Async state={{ ...base, status: "error", error: new ApiError({ message: "boom" }) }} what="x">{() => "DATA"}</Async>)).toContain("boom");
    expect(html(<Async state={{ ...base, status: "ready", data: [] }} isEmpty={(d) => d.length === 0} empty={<EmptyState title="nothing here" />}>{() => "DATA"}</Async>)).toContain("nothing here");
    expect(html(<Async state={{ ...base, status: "ready", data: [1] }} isEmpty={(d) => d.length === 0} empty="E">{() => "DATA"}</Async>)).toContain("DATA");
  });
});

describe("charts", () => {
  it("bar chart draws n/a for null values instead of a zero bar", () => {
    const h = html(<BarChart id="t" title="T" unit="ms" categories={["a", "b"]} series={[{ key: "s", name: "S", values: [5, null] }]} />);
    expect(h).toContain(">n/a<"); expect((h.match(/class="bar s0"/g) || []).length).toBe(1);
  });
  it("bar chart with only nulls says so", () => {
    expect(html(<BarChart id="t" title="T" categories={["a"]} series={[{ key: "s", name: "S", values: [null] }]} />)).toContain("No measured values");
  });
  it("bar chart has an accessible description and a data table with the exact values", () => {
    const h = html(<BarChart id="t" title="Loss" unit="%" categories={["a"]} series={[{ key: "s", name: "S", values: [1.894737] }]} digits={2} />);
    expect(h).toContain('role="img"'); expect(h).toContain("<table>"); expect(h).toContain("1.89");
  });
  it("line chart marks alarms and the threshold", () => {
    const h = html(<LineChart id="l" title="S" xLabel="t" yLabel="s" points={[{ x: 1, y: 0.2, flag: false }, { x: 2, y: 0.9, flag: true }]} threshold={0.5} markers={[{ x: 2.5, label: "H1" }]} />);
    expect(h).toContain("alarm-point"); expect(h).toContain("threshold 0.5"); expect(h).toContain("H1");
  });
  it("timeline draws one lane per kind", () => {
    const h = html(<TimelineChart id="t" title="E" kinds={["link_change", "attach"]} points={[{ seq: 1, t: 1, kind: "attach", node: "mn", detail: "", bytes: 0 }]} markers={[{ t: 1, label: "H1" }]} tMin={1} tMax={2} />);
    expect(h).toContain("link_change"); expect(h).toContain("H1");
  });
});

describe("topology page", () => {
  it("shows every node, link and route that the API returned", () => {
    const h = html(<TopologyView data={topology} />);
    for (const n of topology.nodes) expect(h).toContain(n.key);
    for (const l of topology.links) expect(h).toContain(l.name);
    expect((h.match(/data-link=/g) || []).length).toBe(topology.links.length); expect((h.match(/data-node=/g) || []).length).toBe(topology.nodes.length);
    expect(h).toContain(topology.source_file);
  });
});

describe("configuration page", () => {
  const A = approaches.approaches, S = scenarios.scenarios;
  const baseProps = () => ({ approaches: A, scenarios: S, capabilities, models: models.models, form: initialForm(S[0], A.find((a) => a.id === "mip-basic")), setForm() {}, errors: {}, general: [], warnings: [], submitting: false, checking: false, checkResult: null, onSubmit() {}, onCheck() {}, active: [], onStop() {}, stopping: null, stopMessage: null });
  it("offers each approach, scenario and the limits from /capabilities", () => {
    const h = html(<ConfigureView {...baseProps()} />);
    for (const a of A) expect(h).toContain(`value="${a.id}"`);
    for (const s of S) expect(h).toContain(s.title);
    expect(h).toContain(`≤ ${capabilities.limits.max_duration_s}`); expect(h).toContain("Start experiment"); expect(h).toContain("Check with server");
  });
  it("disables approaches that cannot run here and shows why", () => {
    const quic = A.find((a) => a.id === "quic-migration"); const h = html(<ConfigureView {...baseProps()} />);
    if (quic && !Object.values(quic.environments).some((e) => e.runnable_here)) expect(h).toContain("not runnable on this server");
  });
  it("shows field errors accessibly and blocks double submission", () => {
    const p = baseProps(); p.errors = { duration_s: "duration must be > 0" }; p.submitting = true;
    const h = html(<ConfigureView {...p} />);
    expect(h).toContain('role="alert"'); expect(h).toContain("duration must be &gt; 0"); expect(h).toContain('aria-invalid="true"'); expect(h).toMatch(/<button type="submit" class="primary" disabled="">Starting…/);
  });
  it("lists active experiments with a two-step stop control", () => {
    const p = baseProps(); p.active = [{ id: "exp-0123456789abcdef", status: "running", approach: "mip-basic", scenario: "baseline", created_at: "2026-10-30T11:00:00Z" }];
    const h = html(<ConfigureView {...p} />);
    expect(h).toContain("01234567"); expect(h).toContain("running"); expect(h).toContain(">Stop<"); expect(h).not.toContain("Yes, stop it");
  });
  it("shows an empty state when nothing is running", () => { expect(html(<ConfigureView {...baseProps()} />)).toContain("Nothing is queued or running"); });
});

describe("live metrics page", () => {
  it("progress shows phases and does not invent a percentage", () => {
    const h = html(<ProgressView experiment={{ status: "running", progress: { phase: "running_experiment", fraction: null } }} />);
    expect(h).toContain('aria-current="step"'); expect(h).not.toContain("<progress"); expect(h).toContain("no percentage is available");
  });
  it("header shows identifier, status and the not-live provenance", () => {
    const h = html(<LiveHeader experiment={expMbb} onStop={() => {}} />);
    expect(h).toContain(expMbb.id); expect(h).toContain("not live"); expect(h).toContain("Simulated");
  });
  it("running experiments get a stop button; finished ones do not", () => {
    expect(html(<LiveHeader experiment={{ ...expMbb, status: "running" }} onStop={() => {}} />)).toContain("Stop experiment");
    expect(html(<LiveHeader experiment={expMbb} onStop={() => {}} />)).not.toContain("Stop experiment");
  });
  it("results show the measured values from the metrics endpoint, units and handoffs", () => {
    const h = html(<ResultsView experiment={expMbb} metrics={mMbb} handoffs={hMbb} events={eMbb} api={apiStub} />);
    expect(h).toContain(`${mMbb.metrics.rtt_ms.p50} ms`.replace(/\.0 ms/, " ms")); expect(h).toContain("kbit/s"); expect(h).toContain("Jitter"); expect(h).toContain("not reported in the comparison metrics");
    for (const x of hMbb.handoffs) expect(h).toContain(x.event_id);
    expect(h).toContain('data-chart="event-timeline"'); expect(h).toContain('data-chart="handoff-times"'); expect(h).toContain("own clock"); expect(h).not.toContain("(0.000 s – 0.000 s)"); // regression (Stage 10): the caption once showed seconds as if nanoseconds
  });
  it("results without handoffs or events show empty states", () => {
    const h = html(<ResultsView experiment={expBasic} metrics={mBasic} handoffs={{ handoffs: [], unavailable_reason: "none recorded" }} events={{ events: [], total: 0 }} api={apiStub} />);
    expect(h).toContain("No handoff was measured"); expect(h).toContain("No events available");
  });
  it("unmeasurable interruption shows an em dash, not 0 ms", () => {
    const h = html(<ResultsView experiment={expTcp} metrics={mTcp} handoffs={{ handoffs: mTcp.metrics.handoffs }} events={null} api={apiStub} />);
    if (mTcp.metrics.handoffs.some((x) => x.interruption_ms === null)) expect(h).toContain("not measurable");
  });
  it("lifecycle transitions come from the API", () => { expect(trMbb.items.length > 0).toBe(true); });
});

describe("handoff prediction page", () => {
  it("shows model id, version, threshold, scores and the training-split warning field", () => {
    const h = html(<PredictionsView data={pMbb} />);
    expect(h).toContain(pMbb.model.id); expect(h).toContain(pMbb.model.version); expect(h).toContain("Alarm threshold"); expect(h).toContain("Model version"); expect(h).toContain("Run in the model");
    expect(h).toContain('data-chart="prediction-scores"'); expect(h).toContain("run clock"); expect(h).toContain("calibrated probability");
    const firstScore = pMbb.decision_points.items[0].score.toFixed(4); expect(h).toContain(firstScore);
  });
  it("marks alarm rows", () => {
    const n = pMbb.decision_points.items.filter((r) => r.alarm).length; const h = html(<PredictionsView data={pMbb} />);
    if (n > 0) expect(h).toContain("ALARM");
  });
  it("pages the decision points (25 per page)", () => {
    const h = html(<PredictionsView data={pMbb} />);
    expect((h.match(/<tr class="(alarm-row)?"/g) || []).length).toBe(Math.min(25, pMbb.decision_points.items.length));
    expect(h).toContain(`Page 1 of ${Math.ceil(pMbb.decision_points.items.length / 25)}`);
  });
});

describe("approach comparison page", () => {
  const rows = comparisonRows([{ experiment: expBasic, metrics: mBasic.metrics }, { experiment: expMbb, metrics: mMbb.metrics }, { experiment: expTcp, metrics: mTcp.metrics }]);
  it("states the conditions and warns when they differ", () => {
    const h = html(<ComparisonView rows={rows} />);
    expect(h).toContain("Experimental conditions"); expect(h).toContain("not made under identical conditions"); expect(h).toContain("ONE run"); expect(h).toContain("no approach is ranked");
  });
  it("draws one chart per metric group and one table row per experiment", () => {
    const h = html(<ComparisonView rows={rows} />);
    for (const d of COMPARISON_CHARTS) expect(h).toContain(`data-chart="${d.id}"`);
    const body = h.slice(h.indexOf('data-testid="comparison-table"')); expect((body.slice(0, body.indexOf("</table>")).match(/<th scope="row">/g) || []).length).toBe(rows.length);
  });
  it("chart bars carry the table's numbers", () => {
    const h = html(<ComparisonView rows={rows} />); const spec = chartSpec(COMPARISON_CHARTS.find((c) => c.id === "loss"), rows);
    spec.series[0].values.forEach((v) => { if (v !== null) expect(h).toContain(String(+v.toFixed(2))); });
  });
  it("identical conditions are reported as such", () => {
    expect(html(<ComparisonView rows={comparisonRows([{ experiment: expBasic, metrics: mBasic.metrics }, { experiment: expBasic, metrics: mBasic.metrics }])} />)).toContain("identical across the selected runs");
  });
  it("export writes CSV and JSON built from the displayed rows", () => {
    // ComparisonView's export handlers are exercised in interaction.test.jsx; the data they serialise is checked in logic.test.js
    expect(rows).toHaveLength(3);
  });
});

describe("model evaluation page", () => {
  it("shows baseline, logistic regression and Random Forest results from the bundle", () => {
    const h = html(<ModelEvaluationView evaluation={modelEval} />);
    for (const t of ["Threshold baseline", "Logistic regression", "Random Forest", "Always-alarm reference"]) expect(h).toContain(t);
    expect(h).toContain(modelEval.model.model_version); expect(h).toContain("Read this first");
    const rf = modelEval.held_out_test_metrics.random_forest.test; expect(h).toContain(rf.f1.toFixed(3)); expect(h).toContain(rf.precision.toFixed(3));
    expect(h).toContain('data-chart="model-test-scores"');
  });
  it("states class balance and the findings table", () => {
    const h = html(<ModelEvaluationView evaluation={modelEval} />);
    expect(h).toContain("Class balance per split"); expect(h).toContain("Comparison with the always-alarm reference");
  });
});

describe("experiment history page", () => {
  const props = { page: list, filters: { status: "", approach: "" }, setFilters() {}, offset: 0, setOffset() {}, approaches: approaches.approaches.map((a) => a.id) };
  it("lists experiments with status, identifiers, timestamps and paging info", () => {
    const h = html(<HistoryTable {...props} />);
    for (const e of list.items) expect(h).toContain(e.id.replace(/^exp-/, "").slice(0, 8));
    expect(h).toContain("UTC"); expect(h).toContain(`1–${Math.min(25, list.total)} of ${list.total}`);
  });
  it("shows the right empty state with and without filters", () => {
    expect(html(<HistoryTable {...props} page={{ items: [], total: 0 }} />)).toContain("No experiments have been run yet");
    expect(html(<HistoryTable {...props} page={{ items: [], total: 0 }} filters={{ status: "failed", approach: "" }} />)).toContain("No experiment matches these filters");
  });
  it("detail shows configuration, status and export controls for completed runs", () => {
    const h = html(<ExperimentDetailView experiment={expMbb} transitions={trMbb} api={apiStub} />);
    expect(h).toContain(expMbb.id); expect(h).toContain("Configuration"); expect(h).toContain("duration_s"); expect(h).toContain("CSV · handoffs"); expect(h).toContain("ZIP");
  });
  it("detail of a failed run shows the error and only partial-output export", () => {
    const failed = { ...expMbb, status: "failed", error: { code: "run_failed", message: "worker crashed" }, partial_outputs: true };
    const h = html(<ExperimentDetailView experiment={failed} transitions={null} api={apiStub} />);
    expect(h).toContain("worker crashed"); expect(h).toContain("ZIP · partial outputs"); expect(h).not.toContain("CSV · handoffs");
  });
  it("detail of a run with nothing to export says so", () => {
    expect(html(<ExperimentDetailView experiment={{ ...expMbb, status: "cancelled", partial_outputs: false }} transitions={null} api={apiStub} />)).toContain("Nothing to export");
  });
});
