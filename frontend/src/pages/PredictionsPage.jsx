import React, { useState } from "react";
import { useApi } from "../hooks/api.jsx";
import { useAsync } from "../hooks/useAsync.js";
import { Async, EmptyState } from "../components/StateViews.jsx";
import { DataNotice, OriginBadge } from "../components/Badges.jsx";
import { KeyValueTable, Kpi, PageHeader } from "../components/Common.jsx";
import LineChart from "../components/charts/LineChart.jsx";
import { approachName, fmtCount, fmtNumber, fmtRunClock, fmtTimestamp, nsToSeconds, shortId, NA } from "../lib/format.js";
import { predictionSeries } from "../lib/metrics.js";
import { hrefFor } from "../lib/router.js";

const PAGE = 25;

export function PredictionsView({ data }) {
  const [page, setPage] = useState(0);
  const items = data.decision_points.items;
  const s = data.summary;
  const series = predictionSeries(items);
  const thr = s.threshold ?? (series.find((p) => p.thr !== null) || {}).thr ?? null;
  const markers = (s.handoff_outcomes || []).map((h) => ({ x: nsToSeconds(h.onset_ns), label: `${h.event_id} (${h.outcome})` }));
  const alarms = items.filter((r) => r.alarm).length;
  const pages = Math.max(1, Math.ceil(items.length / PAGE));
  const rows = items.slice(page * PAGE, page * PAGE + PAGE);
  return (
    <>
      <DataNotice>{data.caveat || "Predictions of simulated handoff degradation."} Times are on the run's own clock (virtual for emulated runs). The score is the model's output compared with the threshold; this project has not verified that it is a calibrated probability.</DataNotice>
      <p><OriginBadge origin={data.provenance ? data.provenance.data_origin : "simulated"} kind="historical" /></p>
      <KeyValueTable rows={[
        ["Experiment", <code key="id">{data.experiment_id}</code>], ["Approach / scenario", `${approachName(data.approach)} / ${data.scenario}`],
        ["Model", `${data.model.id} · ${data.model.algorithm}`], ["Model version", <code key="v">{data.model.version || NA}</code>],
        ["Alarm threshold", thr === null ? NA : fmtNumber(thr, 4)], ["Prediction horizon", `${fmtNumber(items[0] ? items[0].horizon_s : null, 2)} s after each decision time`],
        ["Run in the model's training split", s.run_in_training_split === undefined ? NA : s.run_in_training_split ? "yes (predictions are on training data)" : "no"],
      ]} />
      <div className="kpis">
        <Kpi label="Decision points" value={fmtCount(data.decision_points.total)} hint={items.length < data.decision_points.total ? `${items.length} loaded` : null} />
        <Kpi label="Alarms (loaded points)" value={fmtCount(alarms)} hint={s.alarms !== undefined ? `summary file: ${s.alarms}` : null} />
        <Kpi label="Ground-truth episodes" value={fmtCount((s.ground_truth_episodes || []).length)} hint="windows in which a handoff with an echo gap began" />
        <Kpi label="Handoffs" value={fmtCount((s.handoff_outcomes || []).length)} hint={(s.handoff_outcomes || []).map((h) => h.outcome).join(", ")} />
      </div>
      {s.note ? <p className="muted">{s.note}</p> : null}
      <LineChart id="prediction-scores" title="Prediction score over run time" xLabel="run clock (s)" yLabel="score" points={series.map((p) => ({ x: p.t, y: p.score, flag: p.alarm }))} threshold={thr} markers={markers} />
      <h2>Decision points</h2>
      <div className="table-scroll"><table><thead><tr><th>Decision time</th><th>Horizon end</th><th>Horizon</th><th>Score</th><th>Alarm</th><th>Threshold</th><th>Last observation</th><th>Model version</th></tr></thead>
        <tbody>{rows.map((r, i) => <tr key={`${r.decision_time_ns}-${i}`} className={r.alarm ? "alarm-row" : ""}><td>{fmtRunClock(r.decision_time_ns)}</td><td>{fmtRunClock(r.horizon_end_ns)}</td><td>{fmtNumber(r.horizon_s, 2)} s</td><td>{fmtNumber(r.score, 4)}</td><td>{r.alarm ? "ALARM" : "no"}</td><td>{fmtNumber(r.threshold, 4)}</td><td>{fmtRunClock(r.last_observation_ns)}</td><td>{r.model_version}</td></tr>)}</tbody></table></div>
      <nav className="pager" aria-label="Decision point pages"><button type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button><span>Page {page + 1} of {pages}</span><button type="button" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Next</button></nav>
    </>
  );
}

function Picker({ api, selected }) {
  const state = useAsync(async () => (await api.listExperiments({ status: "completed", limit: 200 })).items, [api]);
  return (
    <Async state={state} what="completed experiments" isEmpty={(items) => !items.some((e) => e.prediction_status === "available")}
      empty={<EmptyState title="No experiment has handoff predictions yet.">Tick “Compute handoff predictions after the run” on the <a href={hrefFor("configure")}>configuration page</a> when starting an experiment.</EmptyState>}>
      {(items) => (<><h2>Experiments with predictions</h2><ul>{items.filter((e) => e.prediction_status === "available").map((e) => <li key={e.id}><a href={hrefFor("predictions", e.id)} aria-current={e.id === selected ? "true" : undefined}>{shortId(e.id)}</a> · {approachName(e.approach)} · {e.scenario} · {fmtTimestamp(e.finished_at)}</li>)}</ul></>)}
    </Async>
  );
}

function Detail({ api, id }) {
  const state = useAsync(() => api.predictions(id), [api, id]);
  return <Async state={state} what="the predictions">{(d) => <PredictionsView key={id} data={d} />}</Async>;
}

export default function PredictionsPage({ id }) {
  const api = useApi();
  return (
    <>
      <PageHeader title="Handoff prediction">{id ? <a href={hrefFor("predictions")}>All experiments with predictions</a> : null}</PageHeader>
      {id ? <Detail api={api} id={id} /> : <Picker api={api} />}
    </>
  );
}
