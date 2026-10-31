// Display helpers. `null`/`undefined` always render as an em dash ("not measurable / not provided") - never as zero.
export const NA = "—";
const isNil = (v) => v === null || v === undefined || (typeof v === "number" && !Number.isFinite(v));

export function fmtNumber(v, digits = 1) {
  return isNil(v) ? NA : Number(v).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}
export const fmtMs = (v, digits = 1) => (isNil(v) ? NA : `${fmtNumber(v, digits)} ms`);
export const fmtPercent = (v, digits = 2) => (isNil(v) ? NA : `${fmtNumber(v, digits)} %`);
export const fmtBytes = (v) => (isNil(v) ? NA : `${fmtNumber(v, 1)} B`);
export const fmtKbps = (bps, digits = 1) => (isNil(bps) ? NA : `${fmtNumber(bps / 1000, digits)} kbit/s`);
export const fmtCount = (v) => (isNil(v) ? NA : fmtNumber(v, 0));

// Run clock: nanoseconds on the run's OWN clock (virtual time for emulated runs) -> seconds
export const nsToSeconds = (ns) => (isNil(ns) ? null : Number(ns) / 1e9);
export const fmtRunClock = (ns, digits = 3) => (isNil(ns) ? NA : `${(Number(ns) / 1e9).toFixed(digits)} s`);

// Wall-clock timestamps from the API are ISO-8601 UTC strings
export function fmtTimestamp(iso) {
  if (!iso) return NA;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return `${d.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}
export function fmtDuration(startIso, endIso) {
  if (!startIso || !endIso) return NA;
  const ms = new Date(endIso) - new Date(startIso);
  return Number.isFinite(ms) && ms >= 0 ? `${(ms / 1000).toFixed(1)} s (wall clock)` : NA;
}
export const shortId = (id) => (id ? String(id).replace(/^exp-/, "").slice(0, 8) : NA);

export const APPROACH_NAMES = {
  "tcp-reconnect": "TCP reconnection",
  "mip-basic": "Mobile IP (basic)",
  "ro-model": "Route optimisation (model)",
  "mip-mbb": "Mobile IP make-before-break",
  "quic-migration": "QUIC migration",
};
export const approachName = (id) => APPROACH_NAMES[id] || id;

export const ORIGIN_TEXT = {
  simulated: "Simulated (discrete-event emulator, virtual time)",
  "real-kernel-loopback": "Loopback (real kernel sockets on the server, no path delay)",
};
export function originText(origin) {
  if (!origin) return "Unknown origin";
  if (ORIGIN_TEXT[origin]) return ORIGIN_TEXT[origin];
  return String(origin).startsWith("real-kernel-loopback") ? ORIGIN_TEXT["real-kernel-loopback"] : String(origin);
}
