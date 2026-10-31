// Small CSV writer (RFC 4180 quoting). Text cells that start with = + - @ TAB or CR get a leading ' so spreadsheets do not treat
// them as formulas - the same rule the backend applies to its CSV exports (docs/API.md "Exports").
const DANGEROUS = /^[=+\-@\t\r]/;

export function csvCell(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : "";
  let s = String(v);
  if (DANGEROUS.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// columns: [{ key, label }], rows: array of objects
export function toCsv(columns, rows) {
  const lines = [columns.map((c) => csvCell(c.label ?? c.key)).join(",")];
  for (const r of rows) lines.push(columns.map((c) => csvCell(r[c.key])).join(","));
  return lines.join("\r\n") + "\r\n";
}
