import React from "react";
import { fmtNumber } from "../../lib/format.js";

// The exact numbers behind a chart (also what keeps a chart checkable against an export).
export function ChartTable({ categories, series, digits = 2, unit, categoryHeader = "Item" }) {
  return (
    <details className="chart-data">
      <summary>Data table{unit ? ` (${unit})` : ""}</summary>
      <div className="table-scroll">
        <table>
          <thead><tr><th scope="col">{categoryHeader}</th>{series.map((s) => <th scope="col" key={s.key || s.name}>{s.name}</th>)}</tr></thead>
          <tbody>
            {categories.map((c, i) => (
              <tr key={`${c}-${i}`}><th scope="row">{c}</th>{series.map((s) => <td key={s.key || s.name}>{fmtNumber(s.values[i], digits)}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
