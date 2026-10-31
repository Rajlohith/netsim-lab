import React from "react";
import { originText } from "../lib/format.js";

export const StatusBadge = ({ status }) => <span className={`badge status-${status}`}>{status}</span>;

// Says what kind of data this is. Nothing in this backend is live: results are read from files after a run has finished.
export function OriginBadge({ origin, kind = "historical" }) {
  const sim = origin === "simulated";
  return (
    <span className={`badge origin ${sim ? "origin-sim" : "origin-other"}`} title={originText(origin)}>
      {sim ? "Simulated" : originText(origin).split(" (")[0]} · {kind} · not live
    </span>
  );
}

export const DataNotice = ({ children }) => <aside className="notice" role="note">{children}</aside>;

export const NO_LIVE_STREAM = "The backend runs experiments in the simulator's virtual time and writes the metrics when a run finishes. There is no live metric stream, so this dashboard polls the experiment's lifecycle and shows measurements only after completion.";
