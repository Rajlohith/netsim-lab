import React, { useState } from "react";
import { saveBlob } from "../lib/download.js";
import { ErrorState } from "./StateViews.jsx";

export const PageHeader = ({ title, children }) => (<header className="page-header"><h1>{title}</h1>{children}</header>);

export const Kpi = ({ label, value, hint }) => (
  <div className="kpi"><div className="kpi-value">{value}</div><div className="kpi-label">{label}</div>{hint ? <div className="kpi-hint muted">{hint}</div> : null}</div>
);

export const KeyValueTable = ({ rows, caption }) => (
  <div className="table-scroll"><table className="kv">{caption ? <caption>{caption}</caption> : null}<tbody>{rows.map(([k, v]) => <tr key={k}><th scope="row">{k}</th><td>{v}</td></tr>)}</tbody></table></div>
);

// Two-step destructive action: nothing happens on the first click.
export function ConfirmButton({ label, confirmLabel = "Confirm", cancelLabel = "Keep", onConfirm, disabled, busy }) {
  const [asking, setAsking] = useState(false);
  if (!asking) return <button type="button" className="danger" disabled={disabled || busy} onClick={() => setAsking(true)}>{label}</button>;
  return (
    <span className="confirm" role="group" aria-label={`Confirm: ${label}`}>
      <button type="button" className="danger" disabled={busy} onClick={async () => { await onConfirm(); setAsking(false); }}>{busy ? "Working…" : confirmLabel}</button>
      <button type="button" onClick={() => setAsking(false)}>{cancelLabel}</button>
    </span>
  );
}

// Download buttons for one experiment (server-side exports: JSON, CSV table, ZIP). Failures are shown, never swallowed.
export function ExportButtons({ api, id, predictionAvailable = false, partial = false, save = saveBlob }) {
  const [state, setState] = useState({ busy: null, error: null, done: null });
  async function run(key, opts) {
    setState({ busy: key, error: null, done: null });
    try {
      const f = await api.exportExperiment(id, opts);
      save(f.blob, f.filename);
      setState({ busy: null, error: null, done: f.filename });
    } catch (e) {
      setState({ busy: null, error: e, done: null });
    }
  }
  const b = (key, label, opts, disabled = false) => (
    <button type="button" key={key} disabled={disabled || state.busy !== null} onClick={() => run(key, opts)}>{state.busy === key ? "Exporting…" : label}</button>
  );
  return (
    <div className="exports" role="group" aria-label={`Export ${id}`}>
      {partial ? null : b("json", "JSON", { format: "json" })}
      {partial ? null : b("csv-handoffs", "CSV · handoffs", { format: "csv", table: "handoffs" })}
      {partial ? null : b("csv-events", "CSV · events", { format: "csv", table: "events" })}
      {partial ? null : b("csv-predictions", "CSV · predictions", { format: "csv", table: "predictions" }, !predictionAvailable)}
      {b("zip", partial ? "ZIP · partial outputs" : "ZIP · raw run files", { format: "zip", includePartial: partial })}
      {state.done ? <span role="status" className="muted">Saved {state.done}</span> : null}
      {state.error ? <ErrorState error={state.error} title="Export failed" /> : null}
    </div>
  );
}
