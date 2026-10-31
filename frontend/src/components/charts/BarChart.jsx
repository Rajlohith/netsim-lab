import React from "react";
import { fmtNumber } from "../../lib/format.js";
import { linear, niceTicks, tickLabel } from "../../lib/scale.js";
import { ChartTable } from "./ChartTable.jsx";

const W = 640, H = 300, M = { t: 16, r: 12, b: 64, l: 52 };

// Grouped bars. `null` values are drawn as "n/a" (never as a zero-height bar). Responsive: the SVG scales with its container.
export default function BarChart({ id, title, unit, categories, series, digits = 1, categoryHeader }) {
  const vals = series.flatMap((s) => s.values).filter((v) => v !== null && v !== undefined);
  const max = vals.length ? Math.max(...vals) : 0;
  const ticks = niceTicks(0, max, 5, true);
  const y = linear(ticks[0], ticks[ticks.length - 1], H - M.b, M.t);
  const gw = (W - M.l - M.r) / Math.max(categories.length, 1);
  const bw = Math.min(46, (gw * 0.72) / Math.max(series.length, 1));
  const label = `${title}${unit ? ` in ${unit}` : ""}: ${categories.map((c, i) => `${c} ${series.map((s) => `${s.name} ${s.values[i] === null || s.values[i] === undefined ? "not available" : fmtNumber(s.values[i], digits)}`).join(", ")}`).join("; ")}`;
  return (
    <figure className="chart" data-chart={id}>
      <figcaption><strong>{title}</strong>{unit ? <span className="muted"> · {unit}</span> : null}</figcaption>
      {vals.length === 0 ? <p className="muted chart-empty">No measured values for this chart (all values are “not measurable”).</p> : null}
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} preserveAspectRatio="xMidYMid meet">
        {ticks.map((t) => (
          <g key={t}><line x1={M.l} x2={W - M.r} y1={y(t)} y2={y(t)} className="grid" /><text x={M.l - 6} y={y(t) + 4} textAnchor="end" className="tick">{tickLabel(t)}</text></g>
        ))}
        <line x1={M.l} x2={W - M.r} y1={y(0)} y2={y(0)} className="axis" />
        {categories.map((c, ci) => {
          const gx = M.l + ci * gw + (gw - bw * series.length) / 2;
          const parts = String(c).split(" · ");
          return (
            <g key={`${c}-${ci}`}>
              {series.map((s, si) => {
                const v = s.values[ci], x = gx + si * bw;
                if (v === null || v === undefined) return <text key={s.key || si} x={x + bw / 2} y={y(0) - 4} textAnchor="middle" className="na">n/a</text>;
                return (
                  <g key={s.key || si}>
                    <rect x={x} width={bw - 2} y={Math.min(y(v), y(0))} height={Math.abs(y(0) - y(v))} className={`bar s${si % 5}`}><title>{`${c} — ${s.name}: ${fmtNumber(v, digits)}${unit ? ` ${unit}` : ""}`}</title></rect>
                    <text x={x + (bw - 2) / 2} y={y(v) - 4} textAnchor="middle" className="value">{fmtNumber(v, digits)}</text>
                  </g>
                );
              })}
              <text x={M.l + ci * gw + gw / 2} y={H - M.b + 16} textAnchor="middle" className="cat">{parts.map((p, k) => <tspan key={k} x={M.l + ci * gw + gw / 2} dy={k ? 13 : 0}>{p}</tspan>)}</text>
            </g>
          );
        })}
      </svg>
      {series.length > 1 ? <ul className="legend">{series.map((s, si) => <li key={s.key || si}><span className={`swatch s${si % 5}`} />{s.name}</li>)}</ul> : null}
      <ChartTable categories={categories} series={series} digits={digits} unit={unit} categoryHeader={categoryHeader} />
    </figure>
  );
}
