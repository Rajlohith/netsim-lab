import React from "react";
import { useApi } from "../hooks/api.jsx";
import { useAsync } from "../hooks/useAsync.js";
import { Async, EmptyState } from "../components/StateViews.jsx";
import { StatusBadge } from "../components/Badges.jsx";
import { Kpi, PageHeader } from "../components/Common.jsx";
import { TopologyDiagram } from "./TopologyPage.jsx";
import { runnableEnvironments } from "../lib/validation.js";
import { approachName, shortId } from "../lib/format.js";
import { hrefFor } from "../lib/router.js";

// Every number here is read from the API; nothing is estimated in the browser.
export function OverviewView({ summary, topology }) {
  const { total, completed, runnable, approaches, scenarios, recent } = summary;
  return (
    <>
      <section className="card hero" aria-label="About NetSim Lab">
        <h2>Compare how mobile hosts stay connected</h2>
        <p>NetSim Lab moves a mobile node between two access networks while sequence-numbered probes keep flowing, then measures packet loss, service gap and handoff latency for each mobility mechanism under identical conditions. All results come from the simulator or loopback sockets, never from a live network.</p>
        <div className="actions"><a className="linklike primary" href={hrefFor("configure")}>Configure experiment</a><a href={hrefFor("topology")}>View network topology</a></div>
      </section>
      <div className="kpis" aria-label="Laboratory summary">
        <Kpi label="Experiments" value={total} hint={`${completed} completed`} />
        <Kpi label="Mobility approaches" value={`${runnable} of ${approaches}`} hint="runnable on this server" />
        <Kpi label="Scenarios" value={scenarios} hint="named network conditions" />
      </div>
      <div className="grid-cols">
        <section aria-labelledby="ov-topo"><h2 id="ov-topo">Topology</h2><TopologyDiagram data={topology} /></section>
        <section aria-labelledby="ov-recent"><h2 id="ov-recent">Recent experiments</h2>
          {recent.length === 0 ? <EmptyState title="No experiments yet.">Start one on the <a href={hrefFor("configure")}>configure page</a>.</EmptyState> : (
            <div className="table-scroll"><table><thead><tr><th>Experiment</th><th>Approach</th><th>Scenario</th><th>Status</th></tr></thead>
              <tbody>{recent.map((e) => <tr key={e.id}><td><a href={hrefFor("live", e.id)}>{shortId(e.id)}</a></td><td>{approachName(e.approach)}</td><td>{e.scenario}</td><td><StatusBadge status={e.status} /></td></tr>)}</tbody></table></div>)}
          <p><a href={hrefFor("history")}>All experiments</a></p>
        </section>
      </div>
    </>
  );
}

export default function OverviewPage() {
  const api = useApi();
  const state = useAsync(async () => {
    const [all, done, a, s, topology] = await Promise.all([api.listExperiments({ limit: 5, sort: "-created_at" }), api.listExperiments({ status: "completed", limit: 1 }), api.approaches(), api.scenarios(), api.topology()]);
    return { topology, summary: { total: all.total, completed: done.total, approaches: a.approaches.length, runnable: a.approaches.filter((x) => runnableEnvironments(x).length > 0).length, scenarios: s.scenarios.length, recent: all.items } };
  }, [api]);
  return (
    <>
      <PageHeader title="Overview"><p>A laboratory for studying mobility and handoff in IP networks.</p></PageHeader>
      <Async state={state} what="the overview">{(d) => <OverviewView summary={d.summary} topology={d.topology} />}</Async>
    </>
  );
}
