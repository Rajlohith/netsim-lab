import React, { useState } from "react";
import { useApi } from "../hooks/api.jsx";
import { useAsync } from "../hooks/useAsync.js";
import { Async, EmptyState, ErrorState } from "../components/StateViews.jsx";
import { OriginBadge, StatusBadge } from "../components/Badges.jsx";
import { ExportButtons, KeyValueTable, PageHeader } from "../components/Common.jsx";
import { approachName, fmtDuration, fmtMs, fmtPercent, fmtTimestamp, shortId, NA } from "../lib/format.js";
import { saveBlob } from "../lib/download.js";
import { hrefFor } from "../lib/router.js";

export const PAGE_SIZE = 25;
export const STATUSES = ["queued", "running", "completed", "failed", "cancelled", "incomplete"];

const flatten = (o, prefix = "") => Object.entries(o || {}).flatMap(([k, v]) => (v && typeof v === "object" && !Array.isArray(v) ? flatten(v, `${prefix}${k}.`) : [[`${prefix}${k}`, Array.isArray(v) ? v.map((x) => (typeof x === "object" ? JSON.stringify(x) : x)).join("; ") : v === null ? NA : String(v)]]));

export function HistoryTable({ page, filters, setFilters, offset, setOffset, approaches }) {
  const { items, total } = page;
  const last = Math.min(offset + PAGE_SIZE, total);
  return (
    <>
      <div className="filters" role="search" aria-label="Filter experiments">
        <label>Status <select value={filters.status} onChange={(e) => { setOffset(0); setFilters({ ...filters, status: e.target.value }); }}><option value="">all</option>{STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}</select></label>
        <label>Approach <select value={filters.approach} onChange={(e) => { setOffset(0); setFilters({ ...filters, approach: e.target.value }); }}><option value="">all</option>{approaches.map((a) => <option key={a} value={a}>{approachName(a)}</option>)}</select></label>
      </div>
      {items.length === 0 ? <EmptyState title={total === 0 && !filters.status && !filters.approach ? "No experiments have been run yet." : "No experiment matches these filters."}>Start one on the <a href={hrefFor("configure")}>configuration page</a>.</EmptyState> : (
        <div className="table-scroll"><table data-testid="history-table"><thead><tr><th>Experiment</th><th>Status</th><th>Approach</th><th>Scenario</th><th>Environment</th><th>Label</th><th>Created</th><th>Finished</th><th>Loss</th><th>Handoff latency (mean)</th><th>Predictions</th></tr></thead>
          <tbody>{items.map((e) => <tr key={e.id}><th scope="row"><a href={hrefFor("history", e.id)}>{shortId(e.id)}</a></th><td><StatusBadge status={e.status} /></td><td>{approachName(e.approach)}</td><td>{e.scenario}</td><td>{e.environment}</td><td>{e.label || NA}</td><td>{fmtTimestamp(e.created_at)}</td><td>{fmtTimestamp(e.finished_at)}</td><td>{fmtPercent(e.summary && e.summary.loss_percent)}</td><td>{fmtMs(e.summary && e.summary.handoff_latency_ms_mean)}</td><td>{e.prediction_status}</td></tr>)}</tbody></table></div>)}
      <nav className="pager" aria-label="History pages"><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button><span>{total ? `${offset + 1}–${last} of ${total}` : "0 of 0"}</span><button type="button" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button></nav>
    </>
  );
}

export function HistoryExport({ api, filters, save = saveBlob }) {
  const [state, setState] = useState({ busy: null, error: null, done: null });
  async function run(fmt) {
    setState({ busy: fmt, error: null, done: null });
    try { const f = await api.exportHistory(filters, fmt); save(f.blob, f.filename); setState({ busy: null, error: null, done: f.filename }); } catch (e) { setState({ busy: null, error: e, done: null }); }
  }
  return (
    <div className="exports" role="group" aria-label="Export the history list">
      <span>Export the filtered list (summary values only):</span>
      <button type="button" disabled={state.busy !== null} onClick={() => run("csv")}>{state.busy === "csv" ? "Exporting…" : "CSV"}</button>
      <button type="button" disabled={state.busy !== null} onClick={() => run("json")}>{state.busy === "json" ? "Exporting…" : "JSON"}</button>
      {state.done ? <span role="status" className="muted">Saved {state.done}</span> : null}
      {state.error ? <ErrorState error={state.error} title="Export failed" /> : null}
    </div>
  );
}

export function ExperimentDetailView({ experiment, transitions, api }) {
  const e = experiment, done = e.status === "completed";
  return (
    <>
      <p><StatusBadge status={e.status} /> <code>{e.id}</code> {e.data && e.data.origin ? <OriginBadge origin={e.data.origin} /> : null}</p>
      {e.error ? <div role="alert" className="state state-error"><strong>{e.status}</strong>: <code>{e.error.code}</code> {e.error.message}</div> : null}
      <KeyValueTable caption="Overview" rows={[["Approach", approachName(e.approach)], ["Scenario", e.scenario], ["Environment", e.environment], ["Label", e.label || NA], ["Created", fmtTimestamp(e.created_at)], ["Started", fmtTimestamp(e.started_at)], ["Finished", fmtTimestamp(e.finished_at)], ["Wall-clock duration", fmtDuration(e.started_at, e.finished_at)],
        ["Partial outputs kept", e.partial_outputs ? "yes (not shown as results)" : "no"], ["Predictions", e.prediction ? `${e.prediction.status}${e.prediction.model ? ` · ${e.prediction.model}/${e.prediction.algorithm}` : ""}` : NA]]} />
      <details open><summary>Configuration (as stored with the experiment)</summary><KeyValueTable rows={flatten(e.configuration)} /></details>
      {transitions ? <details><summary>Lifecycle log ({transitions.items.length})</summary><div className="table-scroll"><table><thead><tr><th>#</th><th>Time</th><th>From</th><th>To</th><th>Note</th></tr></thead><tbody>{transitions.items.map((t) => <tr key={t.seq}><td>{t.seq}</td><td>{fmtTimestamp(t.at)}</td><td>{t.from_status || NA}</td><td>{t.to_status}</td><td>{t.note || ""}</td></tr>)}</tbody></table></div></details> : null}
      <p><a href={hrefFor("live", e.id)}>Open metrics and timeline</a>{e.prediction && e.prediction.status === "available" ? <> · <a href={hrefFor("predictions", e.id)}>Open predictions</a></> : null}</p>
      <h2>Export</h2>
      {done ? <ExportButtons api={api} id={e.id} predictionAvailable={e.prediction && e.prediction.status === "available"} />
        : e.partial_outputs ? <ExportButtons api={api} id={e.id} partial /> : <p className="muted">Nothing to export: only completed experiments have results (status is {e.status}).</p>}
    </>
  );
}

function Detail({ api, id }) {
  const state = useAsync(async () => { const [experiment, transitions] = await Promise.all([api.getExperiment(id), api.transitions(id)]); return { experiment, transitions }; }, [api, id]);
  return <Async state={state} what="the experiment">{(d) => <ExperimentDetailView experiment={d.experiment} transitions={d.transitions} api={api} />}</Async>;
}

function List({ api }) {
  const [filters, setFilters] = useState({ status: "", approach: "" });
  const [offset, setOffset] = useState(0);
  const approaches = useAsync(async () => (await api.approaches()).approaches.map((a) => a.id), [api]);
  const page = useAsync(() => api.listExperiments({ status: filters.status || undefined, approach: filters.approach || undefined, limit: PAGE_SIZE, offset }), [api, filters.status, filters.approach, offset]);
  return (
    <>
      <Async state={page} what="the experiment history">{(d) => <HistoryTable page={d} filters={filters} setFilters={setFilters} offset={offset} setOffset={setOffset} approaches={approaches.data || []} />}</Async>
      <HistoryExport api={api} filters={filters} />
    </>
  );
}

export default function HistoryPage({ id }) {
  const api = useApi();
  return (
    <>
      <PageHeader title="Experiment history">{id ? <a href={hrefFor("history")}>All experiments</a> : null}</PageHeader>
      {id ? <Detail api={api} id={id} /> : <List api={api} />}
    </>
  );
}
