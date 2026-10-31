import React, { useState } from "react";
import { useApi } from "../hooks/api.jsx";
import { useAsync } from "../hooks/useAsync.js";
import { Async, EmptyState, ErrorState } from "../components/StateViews.jsx";
import { DataNotice, OriginBadge } from "../components/Badges.jsx";
import { PageHeader } from "../components/Common.jsx";
import BarChart from "../components/charts/BarChart.jsx";
import { COMPARISON_CHARTS, COMPARISON_COLUMNS, METRICS, METRIC_KEYS, chartSpec, comparabilityNotes, comparisonExportRows, comparisonRows } from "../lib/metrics.js";
import { fmtNumber, fmtTimestamp, shortId, approachName } from "../lib/format.js";
import { toCsv } from "../lib/csv.js";
import { saveText } from "../lib/download.js";
import { hrefFor } from "../lib/router.js";

export const MAX_COMPARE = 6;

export function ComparisonView({ rows, save = saveText }) {
  const notes = comparabilityNotes(rows);
  const exportRows = comparisonExportRows(rows);
  const [exportError, setExportError] = useState(null);
  const doExport = (fmt) => {
    try {
      if (fmt === "csv") save(toCsv(COMPARISON_COLUMNS, exportRows), "netsim-comparison.csv", "text/csv;charset=utf-8");
      else save(JSON.stringify({ generated_from: "GET /api/experiments/{id} and /metrics of the selected experiments", rows: exportRows }, null, 2), "netsim-comparison.json", "application/json");
      setExportError(null);
    } catch (e) { setExportError(e); }
  };
  return (
    <>
      <DataNotice>Each row is ONE run of one configuration (no replication), so differences are observations, not statistically established effects, and no approach is ranked here. For replicated statistics see <code>docs/EXPERIMENTS.md</code> (Stage 7). Values come from <code>/api/experiments/{"{id}"}/metrics</code>; “—” means not measurable, never zero.</DataNotice>
      {notes.length ? <div role="alert" className="state state-warning"><strong>These runs were not made under identical conditions:</strong><ul>{notes.map((n, i) => <li key={i}>{n}</li>)}</ul></div> : rows.length > 1 ? <p className="ok-note" role="status">Scenario, environment, handoff mode, duration, rate, payload, seed and conditions are identical across the selected runs.</p> : null}
      <section aria-labelledby="cond-h"><h2 id="cond-h">Experimental conditions</h2>
        <div className="table-scroll"><table><thead><tr><th>Experiment</th><th>Scenario</th><th>Environment</th><th>Origin</th><th>Handoff mode</th><th>Duration</th><th>Rate</th><th>Payload</th><th>Seed</th><th>Conditions</th></tr></thead>
          <tbody>{rows.map((r) => <tr key={r.id}><th scope="row">{r.label}</th><td>{r.scenario}</td><td>{r.environment}</td><td><OriginBadge origin={r.origin} /></td><td>{r.handoff_mode ?? "—"}</td><td>{r.duration_s} s</td><td>{r.rate_pps} pkt/s</td><td>{r.payload} B</td><td>{r.seed}</td><td>{Object.entries(r.conditions).map(([k, v]) => `${k}=${v}`).join(", ")}</td></tr>)}</tbody></table></div></section>
      <section aria-labelledby="met-h"><h2 id="met-h">Metrics</h2>
        <div className="table-scroll"><table data-testid="comparison-table"><thead><tr><th>Experiment</th>{METRIC_KEYS.map((k) => <th key={k}>{METRICS[k].label}{METRICS[k].unit ? ` [${METRICS[k].unit}]` : ""}</th>)}</tr></thead>
          <tbody>{rows.map((r) => <tr key={r.id}><th scope="row">{r.label}</th>{METRIC_KEYS.map((k) => <td key={k}>{fmtNumber(r.values[k], METRICS[k].digits)}</td>)}</tr>)}</tbody></table></div>
        {rows.some((r) => r.interruption_unmeasured) ? <p className="muted">Interruption time is averaged over the handoffs for which the approach could measure it; the others are excluded, not counted as 0.</p> : null}</section>
      <section aria-labelledby="ch-h"><h2 id="ch-h">Charts</h2><div className="charts">{COMPARISON_CHARTS.map((def) => { const s = chartSpec(def, rows); return <BarChart key={def.id} id={def.id} title={s.title} unit={s.unit} categories={s.categories} series={s.series} digits={def.id === "loss" ? 2 : 1} categoryHeader="Experiment" />; })}</div></section>
      <section aria-labelledby="ex-h"><h2 id="ex-h">Export this comparison</h2>
        <p className="muted">The files contain exactly the rows shown in the table and charts above.</p>
        <div className="exports"><button type="button" onClick={() => doExport("csv")}>CSV</button><button type="button" onClick={() => doExport("json")}>JSON</button></div>
        {exportError ? <ErrorState error={exportError} title="Export failed" /> : null}</section>
    </>
  );
}

export default function ComparePage() {
  const api = useApi();
  const [selected, setSelected] = useState([]);
  const list = useAsync(async () => (await api.listExperiments({ status: "completed", limit: 200 })).items, [api]);
  const key = selected.join(",");
  const detail = useAsync(async () => {
    if (!selected.length) return null;
    const entries = await Promise.all(selected.map(async (id) => { const [experiment, m] = await Promise.all([api.getExperiment(id), api.metrics(id)]); return { experiment, metrics: m.metrics }; }));
    return comparisonRows(entries);
  }, [api, key]);
  const toggle = (id) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : s.length >= MAX_COMPARE ? s : [...s, id]));
  return (
    <>
      <PageHeader title="Approach comparison" />
      <Async state={list} what="completed experiments" isEmpty={(items) => items.length === 0}
        empty={<EmptyState title="There is nothing to compare yet.">Run at least two experiments on the <a href={hrefFor("configure")}>configuration page</a>; completed ones appear here.</EmptyState>}>
        {(items) => (
          <>
            <fieldset><legend>Select 1–{MAX_COMPARE} completed experiments{selected.length >= MAX_COMPARE ? " (maximum reached)" : ""}</legend>
              <div className="table-scroll"><table><thead><tr><th>Compare</th><th>Experiment</th><th>Approach</th><th>Scenario</th><th>Environment</th><th>Label</th><th>Finished</th></tr></thead>
                <tbody>{items.map((e) => { const on = selected.includes(e.id); return (
                  <tr key={e.id} className={on ? "selected" : ""}><td><input type="checkbox" id={`cmp-${e.id}`} checked={on} disabled={!on && selected.length >= MAX_COMPARE} onChange={() => toggle(e.id)} aria-label={`Compare ${shortId(e.id)}`} /></td>
                    <td><label htmlFor={`cmp-${e.id}`}>{shortId(e.id)}</label></td><td>{approachName(e.approach)}</td><td>{e.scenario}</td><td>{e.environment}</td><td>{e.label || "—"}</td><td>{fmtTimestamp(e.finished_at)}</td></tr>); })}</tbody></table></div>
            </fieldset>
            {selected.length === 0 ? <EmptyState title="Select experiments above to compare them." /> : <Async state={detail} what="the selected experiments">{(rows) => (rows ? <ComparisonView rows={rows} /> : null)}</Async>}
          </>)}
      </Async>
    </>
  );
}
