// Numeric helpers for the SVG charts.
export function niceStep(range, count = 5) {
  if (!(range > 0)) return 1;
  const raw = range / count, mag = 10 ** Math.floor(Math.log10(raw)), n = raw / mag;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
}
// ticks covering [min, max]; the axis always includes 0 when `zero` is true (bar charts)
export function niceTicks(min, max, count = 5, zero = false) {
  if (zero) { min = Math.min(0, min); max = Math.max(0, max); }
  if (!(max > min)) max = min + 1;
  const step = niceStep(max - min, count);
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.round((v / step) * 1e6) / 1e6 * step);
  return ticks;
}
export const linear = (d0, d1, r0, r1) => (v) => (d1 === d0 ? (r0 + r1) / 2 : r0 + ((v - d0) / (d1 - d0)) * (r1 - r0));
export const tickLabel = (v) => (Math.abs(v) >= 1000 ? `${+(v / 1000).toFixed(2)}k` : String(+v.toFixed(3)));
