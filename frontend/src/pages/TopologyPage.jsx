import React from "react";
import { useApi } from "../hooks/api.jsx";
import { useAsync } from "../hooks/useAsync.js";
import { Async } from "../components/StateViews.jsx";
import { DataNotice } from "../components/Badges.jsx";
import { PageHeader } from "../components/Common.jsx";
import { fmtNumber } from "../lib/format.js";

// Role -> position is presentation only; everything shown (nodes, links, delays, addresses) comes from the API.
const POS = { "correspondent-node": [70, 170], "home-agent": [250, 70], "access-network-a": [450, 70], "access-network-b": [450, 270], "mobile-node": [630, 170] };
const ROLE_TEXT = { "correspondent-node": "Correspondent Node", "home-agent": "Home Agent", "access-network-a": "Access Network A", "access-network-b": "Access Network B", "mobile-node": "Mobile Node" };

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
            <rect x="-48" y="-22" width="96" height="44" rx="8" className={`node role-${n.role}`} />
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
      <DataNotice>Configuration read from <code>{data.source_file}</code> (topology “{data.name}”) through <code>GET /api/topology</code>. {data.notes.join(" ")}</DataNotice>
      <section aria-labelledby="diagram-h"><h2 id="diagram-h">Diagram</h2><TopologyDiagram data={data} /></section>
      <section aria-labelledby="nodes-h"><h2 id="nodes-h">Network components</h2>
        <div className="table-scroll"><table><thead><tr><th>Node</th><th>Role</th><th>IP forwarding</th><th>Interfaces (address · link)</th></tr></thead>
          <tbody>{data.nodes.map((n) => <tr key={n.key}><th scope="row">{n.key}</th><td>{n.role}</td><td>{n.forwarding ? "on" : "off"}</td><td>{n.interfaces.map((i) => `${i.ifname} ${i.address} · ${i.link}`).join("; ")}</td></tr>)}</tbody></table></div></section>
      <section aria-labelledby="links-h"><h2 id="links-h">Links and configured impairments</h2>
        <div className="table-scroll"><table><thead><tr><th>Link</th><th>End A</th><th>End B</th><th>Delay (ms)</th><th>Jitter (ms)</th><th>Rate (Mbit/s)</th><th>Loss (%)</th></tr></thead>
          <tbody>{data.links.map((l) => (
            <tr key={l.name}><th scope="row">{l.name}</th><td>{l.a.node}:{l.a.ifname} {l.a.address}</td><td>{l.b.node}:{l.b.ifname} {l.b.address}</td>
              <td>{fmtNumber(l.a.netem.delay_ms, 2)}</td><td>{fmtNumber(l.a.netem.jitter_ms, 2)}</td><td>{l.a.netem.rate_mbit == null ? "unlimited" : fmtNumber(l.a.netem.rate_mbit, 2)}</td><td>{fmtNumber(l.a.netem.loss_percent, 2)}</td></tr>))}</tbody></table></div>
        <p className="muted">Values are the configured netem settings of end A; end B is {data.links.every((l) => JSON.stringify(l.a.netem) === JSON.stringify(l.b.netem)) ? "identical on every link" : "listed in the API response and differs on at least one link"}.</p></section>
      <section aria-labelledby="routes-h"><h2 id="routes-h">Static routes</h2>
        <div className="table-scroll"><table><thead><tr><th>Node</th><th>Destination</th><th>Via</th><th>Metric</th></tr></thead>
          <tbody>{data.routes.map((r, i) => <tr key={i}><td>{r.node} ({byKey[r.node]?.role})</td><td>{r.to}</td><td>{r.via}</td><td>{r.metric ?? "—"}</td></tr>)}</tbody></table></div></section>
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
