// Turning backend documents into the numbers the pages show. ONE place for every metric definition, so the tables, the charts and the
// CSV/JSON exports of the comparison cannot disagree. `null` means "not measurable / not provided" and is never turned into 0.
import { approachName, shortId, nsToSeconds } from "./format.js";

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

// metrics = the `metrics` member of GET /api/experiments/{id}/metrics  (= the run's comparison_metrics.json)
export function extractRunMetrics(metrics) {
  const m = metrics || {};
  const hs = m.handoff_summary || {};
  const handoffs = Array.isArray(m.handoffs) ? m.handoffs : [];
  const interruptions = handoffs.map((h) => num(h.interruption_ms)).filter((v) => v !== null);
  return {
    handoff_count: num(hs.count),
    all_handoffs_recovered: m.continuity ? m.continuity.all_handoffs_recovered ?? null : null,
    interruption_mean_ms: mean(interruptions),
    interruption_measured: interruptions.length,
    interruption_unmeasured: handoffs.length - interruptions.length,
    service_gap_mean_ms: num(hs.service_gap_ms_mean),
    service_gap_max_ms: num(hs.service_gap_ms_max),
    handoff_latency_mean_ms: num(hs.handoff_latency_ms_mean),
    handoff_latency_max_ms: num(hs.handoff_latency_ms_max),
    loss_percent: m.loss ? num(m.loss.percent) : null,
    packets_lost: m.loss ? num(m.loss.lost) : null,
    packets_sent: m.loss ? num(m.loss.sent) : null,
    loss_exact: m.loss ? m.loss.exact ?? null : null,
    rtt_min_ms: m.rtt_ms ? num(m.rtt_ms.min) : null,
    rtt_p50_ms: m.rtt_ms ? num(m.rtt_ms.p50) : null,
    rtt_p95_ms: m.rtt_ms ? num(m.rtt_ms.p95) : null,
    rtt_max_ms: m.rtt_ms ? num(m.rtt_ms.max) : null,
    goodput_kbps: num(m.goodput_bps) === null ? null : m.goodput_bps / 1000,
    overhead_bytes_per_packet: m.mobility_overhead ? num(m.mobility_overhead.mobility_bytes_per_packet) : null,
    signalling_bytes_total: num(hs.signalling_bytes_total),
    signalling_messages_total: num(hs.signalling_messages_total),
    duplicates: num(m.duplicates),
    reordered_packets: num(m.reordered_packets),
    run_status: m.run_status ?? null,
    origin: m.provenance ? m.provenance.data_origin ?? null : null,
    warnings: Array.isArray(m.warnings) ? m.warnings : [],
    // the comparison metric set has no jitter series; Stage 3 computes jitter for raw traffic runs only
    jitter_ms: null,
  };
}

export const METRICS = {
  interruption_mean_ms: { label: "Interruption time (mean over measured handoffs)", unit: "ms", digits: 1 },
  service_gap_mean_ms: { label: "Service gap (mean)", unit: "ms", digits: 1 },
  handoff_latency_mean_ms: { label: "Handoff latency (mean)", unit: "ms", digits: 1 },
  rtt_p50_ms: { label: "RTT median", unit: "ms", digits: 1 },
  rtt_p95_ms: { label: "RTT 95th percentile", unit: "ms", digits: 1 },
  loss_percent: { label: "Packet loss", unit: "%", digits: 2 },
  goodput_kbps: { label: "Goodput", unit: "kbit/s", digits: 1 },
  overhead_bytes_per_packet: { label: "Mobility overhead", unit: "B/packet", digits: 1 },
  signalling_bytes_total: { label: "Handoff signalling (total)", unit: "B", digits: 0 },
  handoff_count: { label: "Handoffs", unit: "", digits: 0 },
};
export const METRIC_KEYS = Object.keys(METRICS);

export const COMPARISON_CHARTS = [
  { id: "interruption", title: "Interruption time and service gap", unit: "ms", metrics: ["interruption_mean_ms", "service_gap_mean_ms"] },
  { id: "loss", title: "Packet loss", unit: "%", metrics: ["loss_percent"] },
  { id: "latency", title: "Latency", unit: "ms", metrics: ["rtt_p50_ms", "rtt_p95_ms", "handoff_latency_mean_ms"] },
  { id: "overhead", title: "Mobility overhead per packet", unit: "B/packet", metrics: ["overhead_bytes_per_packet"] },
  { id: "signalling", title: "Handoff signalling volume", unit: "B", metrics: ["signalling_bytes_total"] },
  { id: "goodput", title: "Goodput", unit: "kbit/s", metrics: ["goodput_kbps"] },
];

// entries: [{ experiment (GET /experiments/{id} view), metrics (GET .../metrics `metrics` member) }]
export function comparisonRows(entries) {
  return entries.map(({ experiment, metrics }) => {
    const x = extractRunMetrics(metrics);
    const c = experiment.configuration || {};
    const values = {};
    for (const k of METRIC_KEYS) values[k] = x[k] ?? null;
    return {
      id: experiment.id, label: `${approachName(experiment.approach)} · ${shortId(experiment.id)}`,
      approach: experiment.approach, scenario: experiment.scenario, environment: experiment.environment, origin: x.origin,
      handoff_mode: c.handoff_mode ?? null, duration_s: c.duration_s ?? null, rate_pps: c.rate_pps ?? null, payload: c.payload ?? null, seed: c.seed ?? null,
      conditions: c.conditions || {}, run_status: x.run_status, interruption_unmeasured: x.interruption_unmeasured, values,
    };
  });
}

const CONDITION_FIELDS = ["scenario", "environment", "handoff_mode", "duration_s", "rate_pps", "payload", "seed"];
// Sentences telling the reader what differs between the compared runs (so a table is never read as like-for-like by accident)
export function comparabilityNotes(rows) {
  const notes = [];
  if (rows.length < 2) return notes;
  for (const f of CONDITION_FIELDS) {
    const vals = [...new Set(rows.map((r) => JSON.stringify(r[f])))];
    if (vals.length > 1) notes.push(`${f} differs between the selected experiments (${vals.map((v) => JSON.parse(v)).join(", ")}).`);
  }
  const condKeys = [...new Set(rows.flatMap((r) => Object.keys(r.conditions)))];
  for (const k of condKeys) {
    const vals = [...new Set(rows.map((r) => JSON.stringify(r.conditions[k] ?? null)))];
    if (vals.length > 1) notes.push(`condition ${k} differs (${vals.map((v) => JSON.parse(v)).join(", ")}).`);
  }
  if (rows.some((r) => r.run_status && r.run_status !== "complete")) notes.push("at least one run did not report run_status 'complete'.");
  if (new Set(rows.map((r) => r.origin)).size > 1) notes.push("the data origins differ (simulated vs loopback): timings are not comparable across origins.");
  return notes;
}

export function chartSpec(def, rows) {
  return {
    id: def.id, title: def.title, unit: def.unit, categories: rows.map((r) => r.label),
    series: def.metrics.map((k) => ({ key: k, name: METRICS[k].label, values: rows.map((r) => r.values[k]) })),
  };
}

// The comparison's export is built from the SAME rows the tables and charts use.
export const COMPARISON_COLUMNS = [
  { key: "experiment_id", label: "experiment_id" }, { key: "approach", label: "approach" }, { key: "scenario", label: "scenario" },
  { key: "environment", label: "environment" }, { key: "data_origin", label: "data_origin" }, { key: "handoff_mode", label: "handoff_mode" },
  { key: "duration_s", label: "duration_s" }, { key: "rate_pps", label: "rate_pps" }, { key: "payload", label: "payload_bytes" }, { key: "seed", label: "seed" },
  { key: "conditions", label: "conditions_json" }, { key: "run_status", label: "run_status" },
  ...METRIC_KEYS.map((k) => ({ key: k, label: METRICS[k].unit ? `${k} [${METRICS[k].unit}]` : k })),
];
export function comparisonExportRows(rows) {
  return rows.map((r) => ({
    experiment_id: r.id, approach: r.approach, scenario: r.scenario, environment: r.environment, data_origin: r.origin, handoff_mode: r.handoff_mode,
    duration_s: r.duration_s, rate_pps: r.rate_pps, payload: r.payload, seed: r.seed, conditions: JSON.stringify(r.conditions), run_status: r.run_status, ...r.values,
  }));
}

// ---- per-handoff + event data (live metrics page) ---------------------------------------------------------------------------
export function handoffSeries(handoffs) {
  const hs = Array.isArray(handoffs) ? handoffs : [];
  return {
    categories: hs.map((h) => h.event_id),
    series: [
      { key: "handoff_latency_ms", name: "Handoff latency", values: hs.map((h) => num(h.handoff_latency_ms)) },
      { key: "service_gap_ms", name: "Service gap", values: hs.map((h) => num(h.service_gap_ms)) },
      { key: "interruption_ms", name: "Interruption time", values: hs.map((h) => num(h.interruption_ms)) },
    ],
  };
}
export function lossSeries(handoffs) {
  const hs = Array.isArray(handoffs) ? handoffs : [];
  return { categories: hs.map((h) => h.event_id), series: [{ key: "loss_packets", name: "Packets lost", values: hs.map((h) => num(h.loss_packets)) }] };
}

export const KIND_ORDER = ["link_change", "attach", "signal_tx", "signal_rx", "path_validated", "conn_opened", "conn_closed", "retransmit", "failure", "recovered", "note"];
// events: GET .../events `events` (mono_ns is the run's own clock)
export function eventTimeline(events) {
  const evs = Array.isArray(events) ? events : [];
  const points = evs.map((e) => ({ seq: e.seq, t: nsToSeconds(e.mono_ns), kind: e.kind, node: e.node, detail: e.detail, bytes: e.bytes }));
  const kinds = KIND_ORDER.filter((k) => points.some((p) => p.kind === k)).concat([...new Set(points.map((p) => p.kind))].filter((k) => !KIND_ORDER.includes(k)));
  const ts = points.map((p) => p.t).filter((t) => t !== null);
  const linkChanges = points.filter((p) => p.kind === "link_change");
  return { points, kinds, tMin: ts.length ? Math.min(...ts) : null, tMax: ts.length ? Math.max(...ts) : null, linkChanges };
}
// Handoff i <-> i-th link_change event. Only offered when both lists have the same length (otherwise the pairing would be a guess).
export function pairHandoffsWithEvents(handoffs, events) {
  const lc = eventTimeline(events).linkChanges;
  if (!Array.isArray(handoffs) || handoffs.length === 0 || handoffs.length !== lc.length) return null;
  return handoffs.map((h, i) => ({ event_id: h.event_id, detail: h.detail, t: lc[i].t }));
}

// ---- predictions ------------------------------------------------------------------------------------------------------------
export function predictionSeries(items) {
  return (Array.isArray(items) ? items : []).map((r) => ({ t: nsToSeconds(r.decision_time_ns), score: num(r.score), alarm: !!r.alarm, thr: num(r.threshold) }));
}
