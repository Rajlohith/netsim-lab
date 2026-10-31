import React from "react";
import { useApi } from "../hooks/api.jsx";
import { useAsync } from "../hooks/useAsync.js";
import { usePolledExperiment } from "../hooks/usePolledExperiment.js";
import { Async, EmptyState, ErrorState, Loading } from "../components/StateViews.jsx";
import { ConfirmButton, ExportButtons, Kpi, KeyValueTable, PageHeader } from "../components/Common.jsx";
import { DataNotice, NO_LIVE_STREAM, OriginBadge, StatusBadge } from "../components/Badges.jsx";
import BarChart from "../components/charts/BarChart.jsx";
import TimelineChart from "../components/charts/TimelineChart.jsx";
import { ACTIVE, stopExperiment } from "../lib/experimentActions.js";
import { approachName, fmtBytes, fmtCount, fmtDuration, fmtKbps, fmtMs, fmtPercent, fmtRunClock, fmtTimestamp, shortId, NA } from "../lib/format.js";
import { eventTimeline, extractRunMetrics, handoffSeries, lossSeries, pairHandoffsWithEvents } from "../lib/metrics.js";
import { hrefFor } from "../lib/router.js";

export const PHASES = ["queued", "starting", "running_experiment", "predicting", "finalizing", "done"];

export function ProgressView({ experiment }) {
  const p = experiment.progress || {};
  const idx = PHASES.indexOf(p.phase);
  return (
    <div className="progress" aria-label="Experiment progress">
      <ol className="phases">{PHASES.map((ph, i) => <li key={ph} className={ph === p.phase ? "current" : idx > i || p.phase === "done" ? "past" : ""} aria-current={ph === p.phase ? "step" : undefined}>{ph.replace("_", " ")}</li>)}</ol>
      {p.fraction != null ? <progress max="1" value={p.fraction} aria-label="progress">{Math.round(p.fraction * 100)} %</progress> : null}
      <p className="muted">
        {p.queue_position != null ? `Queue position ${p.queue_position}. ` : ""}
        {p.fraction == null && ACTIVE.has(experiment.status) ? "The runner reports phases only - no percentage is available, so none is shown. " : ""}
        {p.step != null && p.steps_total != null ? `Step ${p.step} of ${p.steps_total}. ` : ""}{p.note || ""}
      </p>
    </div>
  );
}

export function ResultsView({ experiment, metrics, handoffs, events, api }) {
  const m = extractRunMetrics(metrics.metrics);
  const hs = handoffs.handoffs;
  const timeline = eventTimeline(events ? events.events : []);
  const pairs = events ? pairHandoffsWithEvents(hs, events.events) : null;
  const markers = pairs ? pairs.map((p, i) => ({ t: p.t, label: `H${i + 1}` })) : timeline.linkChanges.map((p, i) => ({ t: p.t, label: `link ${i + 1}` }));
  const hSeries = handoffSeries(hs), lSeries = lossSeries(hs);
  return (
    <>
      <section aria-labelledby="kpi-h"><h2 id="kpi-h">Measured values <OriginBadge origin={m.origin} kind="historical" /></h2>
        <div className="kpis">
          <Kpi label="RTT median" value={fmtMs(m.rtt_p50_ms)} hint={`min ${fmtMs(m.rtt_min_ms)} · max ${fmtMs(m.rtt_max_ms)}`} />
          <Kpi label="RTT 95th percentile" value={fmtMs(m.rtt_p95_ms)} />
          <Kpi label="Packet loss" value={fmtPercent(m.loss_percent)} hint={`${fmtCount(m.packets_lost)} of ${fmtCount(m.packets_sent)} probes${m.loss_exact === false ? " (estimate)" : ""}`} />
          <Kpi label="Goodput" value={fmtKbps(m.goodput_kbps === null ? null : m.goodput_kbps * 1000)} />
          <Kpi label="Jitter" value={NA} hint="not reported in the comparison metrics" />
          <Kpi label="Mobility overhead" value={m.overhead_bytes_per_packet === null ? NA : `${m.overhead_bytes_per_packet} B/packet`} />
          <Kpi label="Handoffs" value={fmtCount(m.handoff_count)} hint={m.all_handoffs_recovered === null ? "recovery not determined" : m.all_handoffs_recovered ? "all recovered" : "not all recovered"} />
          <Kpi label="Interruption (mean)" value={fmtMs(m.interruption_mean_ms)} hint={m.interruption_unmeasured ? `${m.interruption_unmeasured} handoff(s) not measurable` : `${m.interruption_measured} handoff(s)`} />
        </div>
        {m.warnings.length ? <ul className="notice">{m.warnings.map((w, i) => <li key={i}>{typeof w === "string" ? w : JSON.stringify(w)}</li>)}</ul> : null}
      </section>
      <section aria-labelledby="ho-h"><h2 id="ho-h">Handoffs</h2>
        {hs.length === 0 ? <EmptyState title="No handoff was measured for this run.">{handoffs.unavailable_reason || "The run recorded no handoff."}</EmptyState> : (
          <>
            <div className="charts"><BarChart id="handoff-times" title="Handoff timing per handoff" unit="ms" {...hSeries} categoryHeader="Handoff" /><BarChart id="handoff-loss" title="Packets lost per handoff" unit="packets" {...lSeries} digits={0} categoryHeader="Handoff" /></div>
            <div className="table-scroll"><table><thead><tr><th>Handoff</th><th>Move</th><th>Recovered</th><th>Latency</th><th>Service gap</th><th>Interruption</th><th>Lost</th><th>RTT before → after</th><th>Signalling</th></tr></thead>
              <tbody>{hs.map((h) => <tr key={h.event_id}><th scope="row">{h.event_id}</th><td>{h.detail}</td><td>{h.recovered ? "yes" : "no"}</td><td>{fmtMs(h.handoff_latency_ms)}</td><td>{fmtMs(h.service_gap_ms)}</td><td>{fmtMs(h.interruption_ms)}</td><td>{fmtCount(h.loss_packets)}</td><td>{fmtMs(h.rtt_before_ms)} → {fmtMs(h.rtt_after_ms)}</td><td>{fmtCount(h.signalling_messages)} msg · {fmtBytes(h.signalling_bytes)}</td></tr>)}</tbody></table></div>
          </>)}
      </section>
      <section aria-labelledby="tl-h"><h2 id="tl-h">Event timeline</h2>
        {events && timeline.points.length ? (
          <>
            <TimelineChart id="event-timeline" title="Approach events" kinds={timeline.kinds} points={timeline.points} markers={markers} tMin={timeline.tMin} tMax={timeline.tMax} />
            <p className="muted">{events.total} events in the run's own clock ({fmtRunClock(timeline.tMin)} – {fmtRunClock(timeline.tMax)}); times are virtual for emulated runs, not wall-clock time. {pairs ? "Handoff markers pair the i-th handoff with the i-th link change." : "Markers show link-change events; handoff pairing was not possible (different counts)."}</p>
          </>) : <EmptyState title="No events available.">This run has no approach event log.</EmptyState>}
      </section>
      <section aria-labelledby="exp-h"><h2 id="exp-h">Export</h2><ExportButtons api={api} id={experiment.id} predictionAvailable={experiment.prediction && experiment.prediction.status === "available"} /></section>
    </>
  );
}

function Results({ api, experiment }) {
  const state = useAsync(async () => {
    const [metrics, handoffs] = await Promise.all([api.metrics(experiment.id), api.handoffs(experiment.id)]);
    let events = null, eventsError = null;
    try { events = await api.events(experiment.id); } catch (e) { eventsError = e; }
    return { metrics, handoffs, events, eventsError };
  }, [api, experiment.id]);
  return <Async state={state} what="the results">{(d) => (<>{d.eventsError ? <ErrorState error={d.eventsError} title="The event log could not be loaded" /> : null}<ResultsView experiment={experiment} metrics={d.metrics} handoffs={d.handoffs} events={d.events} api={api} /></>)}</Async>;
}

function LiveList({ api }) {
  const state = useAsync(async () => ({ active: (await api.listExperiments({ status: ["queued", "running"], sort: "created_at", limit: 50 })).items, recent: (await api.listExperiments({ limit: 8 })).items }), [api]);
  return (
    <Async state={state} what="experiments">{({ active, recent }) => (
      <>
        <DataNotice>{NO_LIVE_STREAM}</DataNotice>
        <h2>Running now</h2>
        {active.length ? <ul>{active.map((e) => <li key={e.id}><a href={hrefFor("live", e.id)}>{shortId(e.id)}</a> · {approachName(e.approach)} · {e.scenario} <StatusBadge status={e.status} /></li>)}</ul> : <EmptyState title="No experiment is queued or running.">Start one on the <a href={hrefFor("configure")}>configuration page</a>.</EmptyState>}
        <h2>Most recent</h2>
        {recent.length ? <ul>{recent.map((e) => <li key={e.id}><a href={hrefFor("live", e.id)}>{shortId(e.id)}</a> · {approachName(e.approach)} · {e.scenario} <StatusBadge status={e.status} /> · {fmtTimestamp(e.created_at)}</li>)}</ul> : <EmptyState title="No experiments yet." />}
      </>)}</Async>
  );
}

export function LiveHeader({ experiment, onStop, stopping, stopMessage }) {
  const c = experiment.configuration || {};
  return (
    <>
      <div className="live-head">
        <p><StatusBadge status={experiment.status} /> <code>{experiment.id}</code> <OriginBadge origin={experiment.data && experiment.data.origin ? experiment.data.origin : "simulated"} kind={ACTIVE.has(experiment.status) ? "in progress" : "historical"} /></p>
        {ACTIVE.has(experiment.status) ? <ConfirmButton label="Stop experiment" confirmLabel="Yes, stop it" onConfirm={onStop} busy={stopping} /> : null}
      </div>
      {stopMessage ? <p role="status">{stopMessage}</p> : null}
      <KeyValueTable rows={[["Approach", approachName(experiment.approach)], ["Scenario", experiment.scenario], ["Environment", experiment.environment],
        ["Duration / rate / payload / seed", `${c.duration_s ?? NA} s / ${c.rate_pps ?? NA} pkt/s / ${c.payload ?? NA} B / ${c.seed ?? NA}`],
        ["Created", fmtTimestamp(experiment.created_at)], ["Started", fmtTimestamp(experiment.started_at)], ["Finished", fmtTimestamp(experiment.finished_at)], ["Wall-clock duration", fmtDuration(experiment.started_at, experiment.finished_at)]]} />
    </>
  );
}

function LiveDetail({ id }) {
  const api = useApi();
  const poll = usePolledExperiment(api, id);
  const [stopState, setStopState] = React.useState({ busy: false, message: null });
  if (poll.loading && !poll.experiment) return poll.pollError ? <ErrorState error={poll.pollError} title="Could not load the experiment" /> : <Loading what="the experiment" />;
  if (!poll.experiment) return <ErrorState error={poll.pollError} title="Could not load the experiment" />;
  const e = poll.experiment;
  async function onStop() { setStopState({ busy: true, message: null }); const r = await stopExperiment(api, id); setStopState({ busy: false, message: r.message }); poll.refresh(); }
  return (
    <>
      <DataNotice>{NO_LIVE_STREAM}</DataNotice>
      {poll.pollError ? <p role="alert" className="state state-error">Connection problem ({poll.pollError.message}); retrying — {poll.failures} failed attempt(s). Shown values may be out of date.</p> : null}
      <LiveHeader experiment={e} onStop={onStop} stopping={stopState.busy} stopMessage={stopState.message} />
      <ProgressView experiment={e} />
      {e.error ? <div role="alert" className="state state-error"><strong>{e.status === "failed" ? "The experiment failed" : "The experiment did not finish"}</strong><p><code>{e.error.code}</code> {e.error.message}</p>{e.partial_outputs ? <><p>Partial output files were kept for diagnosis; they are not shown as results.</p><ExportButtons api={api} id={e.id} partial /></> : null}</div> : null}
      {e.status === "cancelled" ? <EmptyState title="Cancelled.">The run was stopped; its half-written files were removed.</EmptyState> : null}
      <section aria-labelledby="tr-h"><h2 id="tr-h">Lifecycle log</h2>
        <div className="table-scroll"><table><thead><tr><th>#</th><th>Time</th><th>From</th><th>To</th><th>Note</th></tr></thead><tbody>{poll.transitions.map((t) => <tr key={t.seq}><td>{t.seq}</td><td>{fmtTimestamp(t.at)}</td><td>{t.from_status || NA}</td><td>{t.to_status}</td><td>{t.note || ""}</td></tr>)}</tbody></table></div>
        <p className="muted">{ACTIVE.has(e.status) ? `Polling every second · last answer ${fmtTimestamp(poll.lastUpdated)}` : "Polling stopped: the experiment reached a final status."}</p></section>
      {e.status === "completed" ? <Results api={api} experiment={e} /> : ACTIVE.has(e.status) ? <EmptyState title="No metrics yet.">Metrics are written when the run completes; this page loads them automatically.</EmptyState> : null}
    </>
  );
}

export default function LivePage({ id }) {
  const api = useApi();
  return (
    <>
      <PageHeader title="Live metrics">{id ? <a href={hrefFor("live")}>All experiments</a> : null}</PageHeader>
      {id ? <LiveDetail id={id} /> : <LiveList api={api} />}
    </>
  );
}
