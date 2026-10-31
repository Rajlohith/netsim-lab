import React from "react";
import { fmtNumber } from "../../lib/format.js";
import { linear, niceTicks, tickLabel } from "../../lib/scale.js";

const W = 640, ROW = 26, M = { t: 14, r: 14, b: 40, l: 108 };

// Event lanes (one per event kind) over the run's own clock, with vertical markers for handoffs.
export default function TimelineChart({ id, title, kinds, points, markers = [], tMin, tMax }) {
  const H = M.t + M.b + Math.max(kinds.length, 1) * ROW;
  const lo = tMin ?? 0, hi = tMax !== null && tMax !== undefined && tMax > lo ? tMax : lo + 1;
  const ticks = niceTicks(lo, hi, 6);
  const x = linear(ticks[0], ticks[ticks.length - 1], M.l, W - M.r);
  const laneY = (k) => M.t + kinds.indexOf(k) * ROW + ROW / 2;
  return (
    <figure className="chart" data-chart={id}>
      <figcaption><strong>{title}</strong><span className="muted"> · run clock (s)</span></figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}: ${points.length} events in ${kinds.length} lanes, ${markers.length} handoff markers`} preserveAspectRatio="xMidYMid meet">
        {kinds.map((k) => <g key={k}><line x1={M.l} x2={W - M.r} y1={laneY(k)} y2={laneY(k)} className="grid" /><text x={M.l - 8} y={laneY(k) + 4} textAnchor="end" className="tick">{k}</text></g>)}
        {ticks.map((t) => <text key={t} x={x(t)} y={H - M.b + 16} textAnchor="middle" className="tick">{tickLabel(t)}</text>)}
        <line x1={M.l} x2={W - M.r} y1={H - M.b} y2={H - M.b} className="axis" />
        {markers.map((m, i) => <g key={i}><line x1={x(m.t)} x2={x(m.t)} y1={M.t} y2={H - M.b} className="marker" /><text x={x(m.t) + 3} y={M.t + 8} className="marker-label">{m.label}</text></g>)}
        {points.map((p) => p.t === null ? null : (
          <circle key={p.seq} cx={x(p.t)} cy={laneY(p.kind)} r="4" className={`point ev-${p.kind}`}><title>{`#${p.seq} ${p.kind} · ${p.node} · t=${fmtNumber(p.t, 3)} s${p.detail ? ` · ${p.detail}` : ""}`}</title></circle>
        ))}
      </svg>
    </figure>
  );
}
