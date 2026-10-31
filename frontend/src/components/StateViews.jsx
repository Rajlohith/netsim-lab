import React from "react";
import { ApiError } from "../api/client.js";

export const Loading = ({ what = "data" }) => <p role="status" className="state state-loading">Loading {what}…</p>;

export function ErrorState({ error, onRetry, title = "Something went wrong" }) {
  const e = error || {};
  const net = e.code === "network_error";
  return (
    <div role="alert" className="state state-error">
      <strong>{net ? "The backend is not reachable" : title}</strong>
      <p>{e.message || String(error)}</p>
      {e instanceof ApiError && e.code && e.code !== "network_error" ? <p className="muted">code <code>{e.code}</code>{e.status ? <> · HTTP {e.status}</> : null}{e.requestId ? <> · request <code>{e.requestId}</code></> : null}</p> : null}
      {(e.issues || []).length ? <ul>{e.issues.map((i, k) => <li key={k}>{i.field ? <code>{i.field}</code> : null} {i.message}</li>)}</ul> : null}
      {net ? <p className="muted">Start the API (<code>make run-api</code>) and reload; the dev server proxies <code>/api</code> to 127.0.0.1:8000.</p> : null}
      {onRetry ? <button type="button" onClick={onRetry}>Retry</button> : null}
    </div>
  );
}

export const EmptyState = ({ title, children }) => (
  <div className="state state-empty" role="note"><strong>{title}</strong>{children ? <p>{children}</p> : null}</div>
);

// <Async state={useAsync(...)}>{(data) => ...}</Async>
export function Async({ state, what, children, isEmpty, empty }) {
  if (state.status === "loading" && !state.data) return <Loading what={what} />;
  if (state.status === "error") return <ErrorState error={state.error} onRetry={state.reload} title={`Could not load ${what || "data"}`} />;
  if (isEmpty && isEmpty(state.data)) return empty;
  return children(state.data);
}
