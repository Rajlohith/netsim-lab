import React from "react";
import { fmtNumber } from "../../lib/format.js";
import { linear, niceTicks, tickLabel } from "../../lib/scale.js";

const W = 640, H = 300, M = { t: 18, r: 14, b: 44, l: 52 };

// Score-over-time chart: points {x, y, flag}, an optional horizontal threshold and vertical markers (e.g. handoff onsets).
export default function LineChart({ id, title, xLabel, yLabel, points, threshold = null, markers = [], yMin = 0, yMax = 1, digits = 3 }) {
  const pts = points.filter((p) => p.x !== null && p.y !== null);
  const xs = [...pts.map((p) => p.x), ...markers.map((m) => m.x)];
  const xMin = xs.length ? Math.min(...xs) : 0, xMax = xs.length ? Math.max(...xs) : 1;
  const xt = niceTicks(xMin, xMax, 6), yt = niceTicks(yMin, yMax, 5);
  const x = linear(xt[0], xt[xt.length - 1], M.l, W - M.r), y = linear(yt[0], yt[yt.length - 1], H - M.b, M.t);
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`).join(" ");
  const alarms = pts.filter((p) => p.flag).length;
  return (
    <figure className="chart" data-chart={id}>
      <figcaption><strong>{title}</strong></figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}: ${pts.length} points, ${alarms} alarms${threshold !== null ? `, threshold ${fmtNumber(threshold, digits)}` : ""}`} preserveAspectRatio="xMidYMid meet">
        {yt.map((t) => <g key={`y${t}`}><line x1={M.l} x2={W - M.r} y1={y(t)} y2={y(t)} className="grid" /><text x={M.l - 6} y={y(t) + 4} textAnchor="end" className="tick">{tickLabel(t)}</text></g>)}
        {xt.map((t) => <text key={`x${t}`} x={x(t)} y={H - M.b + 16} textAnchor="middle" className="tick">{tickLabel(t)}</text>)}
        <line x1={M.l} x2={W - M.r} y1={H - M.b} y2={H - M.b} className="axis" />
        {markers.map((m, i) => (
          <g key={`m${i}`}><line x1={x(m.x)} x2={x(m.x)} y1={M.t} y2={H - M.b} className="marker" /><text x={x(m.x) + 3} y={M.t + 10} className="marker-label">{m.label}</text></g>
        ))}
        {threshold !== null ? <g><line x1={M.l} x2={W - M.r} y1={y(threshold)} y2={y(threshold)} className="threshold" /><text x={W - M.r - 2} y={y(threshold) - 4} textAnchor="end" className="tick">threshold {fmtNumber(threshold, digits)}</text></g> : null}
        <path d={path} className="line s0" fill="none" />
        {pts.map((p, i) => (p.flag
          ? <path key={i} d={`M${x(p.x)},${y(p.y) - 6} l5,9 h-10 z`} className="alarm-point"><title>{`alarm · t=${fmtNumber(p.x, 3)} s · score ${fmtNumber(p.y, digits)}`}</title></path>
          : <circle key={i} cx={x(p.x)} cy={y(p.y)} r="2.5" className="point s0"><title>{`t=${fmtNumber(p.x, 3)} s · score ${fmtNumber(p.y, digits)}`}</title></circle>))}
        <text x={(M.l + W - M.r) / 2} y={H - 6} textAnchor="middle" className="axis-label">{xLabel}</text>
        <text transform={`translate(12 ${(M.t + H - M.b) / 2}) rotate(-90)`} textAnchor="middle" className="axis-label">{yLabel}</text>
      </svg>
      <ul className="legend"><li><span className="swatch s0" />score</li><li><span className="swatch alarm" />alarm (score ≥ threshold)</li>{markers.length ? <li><span className="swatch marker-swatch" />handoff onset</li> : null}</ul>
    </figure>
  );
}
