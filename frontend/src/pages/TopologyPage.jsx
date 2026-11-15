import React from "react";
import { useApi } from "../hooks/api.jsx";
import { useAsync } from "../hooks/useAsync.js";
import { Async } from "../components/StateViews.jsx";
import { PageHeader } from "../components/Common.jsx";
import { fmtNumber } from "../lib/format.js";

// Role -> position is presentation only; everything shown (nodes, links, delays, addresses) comes from the API.
const POS = { "correspondent-node": [70, 170], "home-agent": [250, 70], "access-network-a": [450, 70], "access-network-b": [450, 270], "mobile-node": [630, 170] };
const ROLE_TEXT = { "correspondent-node": "Correspondent node", "home-agent": "Home agent", "access-network-a": "Access network A", "access-network-b": "Access network B", "mobile-node": "Mobile node" };

export const netemText = (n) => {
  if (!n) return "—";
  const parts = [];
  if (n.delay_ms) parts.push(`${fmtNumber(n.delay_ms, 2)} ms${n.jitter_ms ? ` ± ${fmtNumber(n.jitter_ms, 2)}` : ""}`);
  if (n.rate_mbit != null) parts.push(`${fmtNumber(n.rate_mbit, 2)} Mbit/s`);
  if (n.loss_percent) parts.push(`${fmtNumber(n.loss_percent, 2)} % loss`);
  return parts.join(" · ") || "no impairment";
};

export function TopologyDiagram({ data }) {
  const pos = Object.fromEntries(data.nodes.map((n) => [n.key, POS[n.role] || [350, 170]]));
  return (
    <svg viewBox="0 0 700 340" role="img" className="topology" aria-label={`Topology ${data.name}: ${data.nodes.length} nodes, ${data.links.length} links`}>
      {data.links.map((l) => {
        const [x1, y1] = pos[l.a.node], [x2, y2] = pos[l.b.node];
        return (
          <g key={l.name} data-link={l.name}>
            <line x1={x1} y1={y1} x2={x2} y2={y2} className="link" />
            <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 4} textAnchor="middle" className="link-label">{l.name}: {netemText(l.a.netem)}</text>
          </g>
        );
      })}
      {data.nodes.map((n) => {
        const [x, y] = pos[n.key];
        return (
          <g key={n.key} data-node={n.key} transform={`translate(${x} ${y})`}>
            <rect x="-58" y="-22" width="116" height="44" rx="8" className={`node role-${n.role}${n.role === "mobile-node" ? " mobile" : ""}`} />
            <text y="-3" textAnchor="middle" className="node-title">{ROLE_TEXT[n.role] || n.role}</text>
            <text y="12" textAnchor="middle" className="node-sub">{n.key}{n.forwarding ? " · forwards" : ""}</text>
          </g>
        );
      })}
    </svg>
  );
}

export function TopologyView({ data }) {
  const byKey = Object.fromEntries(data.nodes.map((n) => [n.key, n]));
  return (
    <>
      <section aria-labelledby="diagram-h"><h2 id="diagram-h">Diagram</h2><TopologyDiagram data={data} />
        <p className="muted">The mobile node starts on access network A and moves to access network B. The home agent keeps it reachable by tunnelling in the Mobile IP approaches; TCP reconnection and QUIC rely on the endpoints instead. Link labels show the configured delay, rate and loss.</p></section>
      <details className="advanced" open><summary>Network components</summary>
        <div className="table-scroll"><table><thead><tr><th>Node</th><th>Role</th><th>IP forwarding</th><th>Interfaces (address · link)</th></tr></thead>
          <tbody>{data.nodes.map((n) => <tr key={n.key}><th scope="row">{n.key}</th><td>{n.role}</td><td>{n.forwarding ? "on" : "off"}</td><td>{n.interfaces.map((i) => `${i.ifname} ${i.address} · ${i.link}`).join("; ")}</td></tr>)}</tbody></table></div></details>
      <details className="advanced" open><summary>Links and configured impairments</summary>
        <div className="table-scroll"><table><thead><tr><th>Link</th><th>End A</th><th>End B</th><th>Delay (ms)</th><th>Jitter (ms)</th><th>Rate (Mbit/s)</th><th>Loss (%)</th></tr></thead>
          <tbody>{data.links.map((l) => (
            <tr key={l.name}><th scope="row">{l.name}</th><td>{l.a.node}:{l.a.ifname} {l.a.address}</td><td>{l.b.node}:{l.b.ifname} {l.b.address}</td>
              <td>{fmtNumber(l.a.netem.delay_ms, 2)}</td><td>{fmtNumber(l.a.netem.jitter_ms, 2)}</td><td>{l.a.netem.rate_mbit == null ? "unlimited" : fmtNumber(l.a.netem.rate_mbit, 2)}</td><td>{fmtNumber(l.a.netem.loss_percent, 2)}</td></tr>))}</tbody></table></div>
        <p className="muted">Values are the configured netem settings of end A; end B is {data.links.every((l) => JSON.stringify(l.a.netem) === JSON.stringify(l.b.netem)) ? "identical on every link" : "listed in the API response and differs on at least one link"}.</p></details>
      <details className="advanced"><summary>Static routes and source</summary>
        <div className="table-scroll"><table><thead><tr><th>Node</th><th>Destination</th><th>Via</th><th>Metric</th></tr></thead>
          <tbody>{data.routes.map((r, i) => <tr key={i}><td>{r.node} ({byKey[r.node]?.role})</td><td>{r.to}</td><td>{r.via}</td><td>{r.metric ?? "—"}</td></tr>)}</tbody></table></div>
        <p className="muted">Read from <code>{data.source_file}</code> (topology “{data.name}”) through <code>GET /api/topology</code>. {data.notes.join(" ")}</p></details>
    </>
  );
}

export default function TopologyPage() {
  const api = useApi();
  const state = useAsync(() => api.topology(), [api]);
  return (
    <>
      <PageHeader title="Network topology" />
      <Async state={state} what="the topology">{(data) => <TopologyView data={data} />}</Async>
    </>
  );
}
