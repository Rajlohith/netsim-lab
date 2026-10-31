import React, { useState } from "react";
import { useApi } from "../hooks/api.jsx";
import { useAsync } from "../hooks/useAsync.js";
import { Async, EmptyState } from "../components/StateViews.jsx";
import { DataNotice, OriginBadge } from "../components/Badges.jsx";
import { KeyValueTable, PageHeader } from "../components/Common.jsx";
import BarChart from "../components/charts/BarChart.jsx";
import { fmtCount, fmtNumber, fmtTimestamp, NA } from "../lib/format.js";

export const MODEL_NAMES = { threshold_baseline: "Threshold baseline", logreg: "Logistic regression", random_forest: "Random Forest" };
const modelName = (k) => MODEL_NAMES[k] || k;
const ORDER = ["threshold_baseline", "logreg", "random_forest"];
const SCORES = [["precision", "Precision"], ["recall", "Recall"], ["f1", "F1"], ["false_alarm_rate", "False-alarm rate"]];

export function ModelEvaluationView({ evaluation }) {
  const held = evaluation.held_out_test_metrics;
  const names = ORDER.filter((k) => held[k]).concat(Object.keys(held).filter((k) => !ORDER.includes(k)));
  const ref = evaluation.reference && evaluation.reference.always_alarm;
  const findings = (evaluation.full_results && evaluation.full_results.findings) || [];
  const chartCats = [...names.map(modelName), ...(ref ? ["Always-alarm reference"] : [])];
  const series = SCORES.slice(0, 3).map(([k, label]) => ({ key: k, name: label, values: [...names.map((n) => (held[n].test ? held[n].test[k] ?? null : null)), ...(ref ? [ref[k] ?? null] : [])] }));
  const split = evaluation.split_balance || {};
  return (
    <>
      <DataNotice><strong>Read this first.</strong> {evaluation.interpretation_note}</DataNotice>
      <p><OriginBadge origin={(evaluation.data_origin || ["simulated"])[0] || "simulated"} kind="historical" /></p>
      <KeyValueTable rows={[["Model", `${evaluation.model.id}`], ["Model version", <code key="v">{evaluation.model.model_version}</code>], ["Trained (UTC)", fmtTimestamp(evaluation.model.created_utc)], ["Training dataset SHA-256", <code key="h">{(evaluation.model.dataset_sha256 || NA).slice(0, 16)}…</code>],
        ["Algorithms", evaluation.model.algorithms.map(modelName).join(", ")], ["Sources", evaluation.source_files.join(", ")]]} />
      <section aria-labelledby="chart-h"><h2 id="chart-h">Held-out test split</h2>
        <BarChart id="model-test-scores" title="Test-split precision, recall and F1" unit="score (0–1)" categories={chartCats} series={series} digits={3} categoryHeader="Model" />
        <div className="table-scroll"><table data-testid="model-table"><thead><tr><th rowSpan="2">Model</th><th colSpan="4">Test split</th><th colSpan="2">Ranking (test)</th><th colSpan="2">Validation</th><th rowSpan="2">Threshold</th></tr>
          <tr>{SCORES.map(([k, l]) => <th key={k}>{l}</th>)}<th>Avg. precision</th><th>ROC AUC</th><th>F1</th><th>Alarms</th></tr></thead>
          <tbody>
            {names.map((n) => { const m = held[n], t = m.test || {}, v = m.validation || {}, r = m.ranking_test || {}; return (
              <tr key={n}><th scope="row">{modelName(n)}</th>{SCORES.map(([k]) => <td key={k}>{fmtNumber(t[k], 3)}</td>)}<td>{fmtNumber(r.average_precision, 3)}</td><td>{fmtNumber(r.roc_auc, 3)}</td><td>{fmtNumber(v.f1, 3)}</td><td>{fmtCount(v.alarms)}</td><td>{fmtNumber(m.threshold, 3)}</td></tr>); })}
            {ref ? <tr className="reference"><th scope="row">Always-alarm reference (no model)</th>{SCORES.map(([k]) => <td key={k}>{fmtNumber(ref[k], 3)}</td>)}<td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr> : null}
          </tbody></table></div>
        <details><summary>95 % bootstrap intervals (test split, resampling whole experiments)</summary>
          <div className="table-scroll"><table><thead><tr><th>Model</th>{SCORES.map(([k, l]) => <th key={k}>{l}</th>)}</tr></thead><tbody>{names.map((n) => { const ci = held[n].bootstrap_test && held[n].bootstrap_test.ci95; return <tr key={n}><th scope="row">{modelName(n)}</th>{SCORES.map(([k]) => <td key={k}>{ci && ci[k] ? `${fmtNumber(ci[k][0], 3)} – ${fmtNumber(ci[k][1], 3)}` : NA}</td>)}</tr>; })}</tbody></table></div>
          <p className="muted">{names.map((n) => held[n].bootstrap_test).find(Boolean) ? `${held[names.find((n) => held[n].bootstrap_test)].bootstrap_test.n_groups} experiment groups in the test split.` : ""}</p></details>
      </section>
      <section aria-labelledby="find-h"><h2 id="find-h">Comparison with the always-alarm reference</h2>
        {findings.length ? (
          <div className="table-scroll"><table data-testid="findings-table"><thead><tr><th>Model / variant</th><th>Test F1</th><th>Always-alarm F1</th><th>Beats the reference</th></tr></thead>
            <tbody>{findings.map((f) => <tr key={f.model}><th scope="row">{f.model}</th><td>{fmtNumber(f.test_f1, 3)}</td><td>{fmtNumber(f.always_alarm_f1, 3)}</td><td>{f.beats_always_alarm_f1 ? "yes" : "no"}</td></tr>)}</tbody></table></div>)
          : <EmptyState title="No full results file matches this model's training data.">Only the bundle's own metrics are shown above.</EmptyState>}
        {evaluation.full_results && evaluation.full_results.baseline_rule ? <p className="muted">Threshold baseline rule: <code>{evaluation.full_results.baseline_rule.rule}</code> (fit on the training split only).</p> : null}
      </section>
      <section aria-labelledby="split-h"><h2 id="split-h">Class balance per split</h2>
        <div className="table-scroll"><table><thead><tr><th>Split</th><th>Decision points</th><th>Positives</th><th>Negatives</th><th>Prevalence</th></tr></thead>
          <tbody>{Object.entries(split).map(([k, s]) => <tr key={k}><th scope="row">{k}</th><td>{fmtCount(s.included)}</td><td>{fmtCount(s.positives)}</td><td>{fmtCount(s.negatives)}</td><td>{fmtNumber(s.prevalence * 100, 1)} %</td></tr>)}</tbody></table></div></section>
      <section aria-labelledby="cav-h"><h2 id="cav-h">Caveats recorded with the model</h2><ul>{(evaluation.caveats || (evaluation.full_results && evaluation.full_results.caveats) || []).map((c, i) => <li key={i}>{c}</li>)}</ul>
        <p className="muted">{evaluation.model.disclaimer}</p></section>
    </>
  );
}

function Detail({ api, id }) {
  const state = useAsync(() => api.modelEvaluation(id), [api, id]);
  return <Async state={state} what="the model evaluation">{(e) => <ModelEvaluationView evaluation={e} />}</Async>;
}

export default function ModelsPage() {
  const api = useApi();
  const models = useAsync(async () => (await api.models()).models, [api]);
  const [picked, setPicked] = useState(null);
  return (
    <>
      <PageHeader title="Model evaluation" />
      <Async state={models} what="the prediction models" isEmpty={(m) => m.length === 0} empty={<EmptyState title="No prediction model is configured on this server." />}>
        {(list) => {
          const usable = list.filter((m) => m.available);
          const id = picked || (usable[0] && usable[0].id);
          return (
            <>
              {list.some((m) => !m.available) ? <ul className="state state-warning">{list.filter((m) => !m.available).map((m) => <li key={m.id}>Model <code>{m.id}</code> is unavailable: {m.reason}</li>)}</ul> : null}
              {usable.length > 1 ? <label>Model <select value={id} onChange={(e) => setPicked(e.target.value)}>{usable.map((m) => <option key={m.id} value={m.id}>{m.id} · {m.model_version}</option>)}</select></label> : null}
              {id ? <Detail api={api} id={id} /> : <EmptyState title="No usable model bundle.">See the reasons above.</EmptyState>}
            </>);
        }}
      </Async>
    </>
  );
}
