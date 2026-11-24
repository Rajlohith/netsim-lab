import React, { useEffect, useMemo, useRef, useState } from "react";
import { useApi } from "../hooks/api.jsx";
import { useAsync } from "../hooks/useAsync.js";
import { Async, ErrorState, EmptyState } from "../components/StateViews.jsx";
import { ConfirmButton, KeyValueTable, PageHeader } from "../components/Common.jsx";
import { StatusBadge } from "../components/Badges.jsx";
import { CONDITION_KEYS, CONDITION_RANGES, formToRequest, initialForm, runnableEnvironments } from "../lib/validation.js";
import { checkWithServer, newIdempotencyKey, stopExperiment, submitExperiment } from "../lib/experimentActions.js";
import { approachName, fmtTimestamp, shortId } from "../lib/format.js";
import { hrefFor, navigate } from "../lib/router.js";

const Field = ({ id, label, error, hint, children }) => (
  <div className={`field${error ? " has-error" : ""}`}>
    <label htmlFor={id}>{label}</label>
    {children}
    {hint ? <small className="muted">{hint}</small> : null}
    {error ? <small id={`${id}-err`} role="alert" className="field-error">{error}</small> : null}
  </div>
);

// Pure view: everything it shows and every action is a prop (tested without a browser).
export function ConfigureView({ approaches, scenarios, capabilities, form, setForm, errors, general, warnings, submitting, checking, checkResult, onSubmit, onCheck, active, onStop, stopping, stopMessage }) {
  const approach = approaches.find((a) => a.id === form.approach);
  const scenario = scenarios.find((s) => s.id === form.scenario);
  const envs = runnableEnvironments(approach);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
  const setCond = (k) => (e) => setForm((f) => ({ ...f, conditions: { ...f.conditions, [k]: e.target.value } }));
  const lim = capabilities.limits;
  const hasErrors = Object.keys(errors).length > 0;
  const ADV = ["environment", "rate_pps", "payload", "seed", "label"];
  const advancedHasError = Object.keys(errors).some((k) => ADV.includes(k) || k.startsWith("conditions."));
  return (
    <>
      <p className="muted">Runs are background jobs in the simulator's virtual time or on loopback sockets. Privileged <code>live</code> namespace runs are never available through the API.</p>
      <div className="grid-2">
      <form onSubmit={onSubmit} noValidate aria-label="Experiment configuration">
        <fieldset><legend>Approach and scenario</legend>
          <Field id="approach" label="Mobility approach" error={errors.approach}>
            <select id="approach" value={form.approach} onChange={(e) => { const a = approaches.find((x) => x.id === e.target.value); setForm((f) => ({ ...f, approach: e.target.value, environment: runnableEnvironments(a).includes(f.environment) ? f.environment : runnableEnvironments(a)[0] || f.environment, handoff_mode: a && a.supports_make_before_break ? "mbb" : "bbm" })); }}>
              {approaches.map((a) => { const ok = runnableEnvironments(a).length > 0; return <option key={a.id} value={a.id} disabled={!ok}>{approachName(a.id)}{ok ? "" : " (not runnable on this server)"}</option>; })}
            </select>
          </Field>
          {approach ? <details className="approach-info"><summary>About {approach.name}</summary>
            <p><strong>Mechanism:</strong> {approach.mechanism}</p><p><strong>Standards status:</strong> {approach.standards_status}</p><p><strong>Limits:</strong> {approach.scope_limits}</p>
            {Object.entries(approach.environments).filter(([, e]) => !e.runnable_here).map(([k, e]) => <p key={k} className="muted">Environment <code>{k}</code> unavailable: {e.check_reason || e.support_reason}</p>)}
          </details> : null}
          <Field id="scenario" label="Scenario" error={errors.scenario} hint={scenario ? scenario.description : null}>
            <select id="scenario" value={form.scenario} onChange={(e) => { const s = scenarios.find((x) => x.id === e.target.value); setForm((f) => ({ ...f, scenario: e.target.value, duration_s: s ? String(s.parameters.duration_s) : f.duration_s })); }}>
              {scenarios.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}
            </select>
          </Field>
          <div className="row">
            <Field id="duration_s" label="Duration (s)" error={errors.duration_s} hint={`> 0, ≤ ${lim.max_duration_s}`}><input id="duration_s" inputMode="decimal" value={form.duration_s} onChange={set("duration_s")} aria-invalid={!!errors.duration_s} /></Field>
            {form.approach === "tcp-reconnect" ? <Field id="tcp_detect" label="TCP loss detection"><select id="tcp_detect" value={form.tcp_detect} onChange={set("tcp_detect")}><option value="event">on network-change event</option><option value="timeout">on timeout</option></select></Field> : null}
          </div>
          {scenario ? <p className="muted">Handoff schedule (fixed by the scenario): {scenario.parameters.moves.map((m) => `→ ${m.interface} at ${m.at_s} s`).join(", ")}.</p> : null}
          {errors.handoff_mode && !(approach && approach.supports_make_before_break) ? <small role="alert" className="field-error">{errors.handoff_mode}</small> : null}
        </fieldset>

        <details className="advanced advanced-plain" open={advancedHasError}><summary>Advanced settings</summary>
          <div className="row">
            <Field id="environment" label="Environment" error={errors.environment}>
              <select id="environment" value={form.environment} onChange={set("environment")}>
                {(approach ? Object.keys(approach.environments).filter((k) => approach.environments[k].exposed_via_api) : ["emulated"]).map((k) => <option key={k} value={k} disabled={!envs.includes(k)}>{k}{envs.includes(k) ? "" : " (unavailable)"}</option>)}
              </select>
            </Field>
            <Field id="rate_pps" label="Probe rate (packets/s)" error={errors.rate_pps} hint={`> 0, ≤ ${lim.max_rate_pps}; ≤ ${lim.max_probes} probes in total`}><input id="rate_pps" inputMode="decimal" value={form.rate_pps} onChange={set("rate_pps")} aria-invalid={!!errors.rate_pps} /></Field>
            <Field id="payload" label="Payload (bytes)" error={errors.payload} hint={`${lim.payload_bytes[0]}–${lim.payload_bytes[1]}`}><input id="payload" inputMode="numeric" value={form.payload} onChange={set("payload")} aria-invalid={!!errors.payload} /></Field>
            <Field id="seed" label="Seed" error={errors.seed} hint="Same seed, same result"><input id="seed" inputMode="numeric" value={form.seed} onChange={set("seed")} aria-invalid={!!errors.seed} /></Field>
          </div>
          <hp>Network condition overrides</hp>
          <p className="muted">Leave a field blank to use the scenario's value.</p>
          <div className="row">
            {CONDITION_KEYS.map((k) => (
              <Field key={k} id={`cond-${k}`} label={CONDITION_RANGES[k].label} error={errors[`conditions.${k}`]} hint={scenario ? `scenario: ${scenario.parameters.conditions[k]}` : null}>
                <input id={`cond-${k}`} inputMode="decimal" value={form.conditions[k]} onChange={setCond(k)} placeholder={scenario ? String(scenario.parameters.conditions[k]) : ""} aria-invalid={!!errors[`conditions.${k}`]} />
              </Field>))}
          </div>
          <hp>Label</hp>
          <Field id="label" label="Label (optional)" error={errors.label}><input id="label" value={form.label} maxLength={200} onChange={set("label")} /></Field>
        </details>

        {general.length ? <ul role="alert" className="state state-error">{general.map((g, i) => <li key={i}>{g}</li>)}</ul> : null}
        {hasErrors ? <p role="alert" className="field-error">Fix the highlighted fields before starting.</p> : null}
        {warnings.length ? <ul className="notice" role="status">{warnings.map((w, i) => <li key={i}>{w}</li>)}</ul> : null}
        {checkResult && checkResult.valid ? <p role="status" className="ok-note">The server accepts this configuration{checkResult.resolved && checkResult.resolved.fingerprint ? ` (fingerprint ${checkResult.resolved.fingerprint.slice(0, 12)})` : ""}.</p> : null}
        <div className="actions">
          <button type="submit" className="primary" disabled={submitting}>{submitting ? "Starting…" : "Start experiment"}</button>
          <button type="button" onClick={onCheck} disabled={checking || submitting}>{checking ? "Checking…" : "Check with server"}</button>
        </div>
      </form>
      <aside className="card sticky" aria-label="Selected configuration">
        <h2>Summary</h2>
        <dl className="summary-list">
          <dt>Approach</dt><dd>{approach ? approachName(approach.id) : "—"}</dd>
          <dt>Scenario</dt><dd>{scenario ? scenario.title : "—"}</dd>
          <dt>Environment</dt><dd>{form.environment} (simulated or loopback)</dd>
          <dt>Duration</dt><dd>{form.duration_s} s</dd>
          <dt>Probe rate</dt><dd>{form.rate_pps} packets/s, {form.payload} B</dd>
          <dt>Seed</dt><dd>{form.seed}</dd>
          <dt>Handoff mode</dt><dd>{approach && approach.supports_make_before_break ? "make-before-break (fixed)" : "break-before-make"}</dd>
        </dl>
      </aside>
      </div>

      <section aria-labelledby="active-h"><h2 id="active-h">Active experiments</h2>
        {stopMessage ? <p role="status">{stopMessage}</p> : null}
        {active.length === 0 ? <EmptyState title="Nothing is queued or running.">Started experiments appear here until they finish.</EmptyState> : (
          <div className="table-scroll"><table><thead><tr><th>Experiment</th><th>Status</th><th>Approach</th><th>Scenario</th><th>Created</th><th>Control</th></tr></thead>
            <tbody>{active.map((e) => (
              <tr key={e.id}><td><a href={hrefFor("live", e.id)}>{shortId(e.id)}</a></td><td><StatusBadge status={e.status} /></td><td>{approachName(e.approach)}</td><td>{e.scenario}</td><td>{fmtTimestamp(e.created_at)}</td>
                <td><ConfirmButton label="Stop" confirmLabel="Yes, stop it" onConfirm={() => onStop(e.id)} busy={stopping === e.id} /></td></tr>))}</tbody></table></div>)}
      </section>
    </>
  );
}

export default function ConfigurePage() {
  const api = useApi();
  const catalog = useAsync(async () => {
    const [a, s, c] = await Promise.all([api.approaches(), api.scenarios(), api.capabilities()]);
    return { approaches: a.approaches, scenarios: s.scenarios, capabilities: c };
  }, [api]);
  const activeState = useAsync(async () => (await api.listExperiments({ status: ["queued", "running"], limit: 50, sort: "created_at" })).items, [api]);
  return (
    <>
      <PageHeader title="Configure experiment" />
      <Async state={catalog} what="the approach and scenario catalogue">{(c) => <ConfigureLoaded catalog={c} api={api} activeState={activeState} />}</Async>
    </>
  );
}

function ConfigureLoaded({ catalog, api, activeState }) {
  const { approaches, scenarios, capabilities } = catalog;
  const firstApproach = approaches.find((a) => runnableEnvironments(a).length) || approaches[0];
  const [form, setForm] = useState(() => initialForm(scenarios[0], firstApproach));
  const [errors, setErrors] = useState({});
  const [general, setGeneral] = useState([]);
  const [warnings, setWarnings] = useState([]);
  const [busy, setBusy] = useState({ submitting: false, checking: false });
  const [checkResult, setCheckResult] = useState(null);
  const [topError, setTopError] = useState(null);
  const [stopping, setStopping] = useState(null);
  const [stopMessage, setStopMessage] = useState(null);
  const approach = approaches.find((a) => a.id === form.approach);
  const scenario = scenarios.find((s) => s.id === form.scenario);
  const ctx = useMemo(() => ({ capabilities, approach, scenario }), [capabilities, approach, scenario]);
  // One idempotency key per distinct request: a double click or a retry after a lost response cannot create two experiments.
  const keyRef = useRef({ json: null, key: null });
  const keyFor = () => { const json = JSON.stringify(formToRequest(form, ctx)); if (keyRef.current.json !== json) keyRef.current = { json, key: newIdempotencyKey() }; return keyRef.current.key; };
  useEffect(() => { setCheckResult(null); }, [form]);

  const apply = (r) => { setErrors(r.fieldErrors || {}); setGeneral(r.general || []); setWarnings(r.warnings || []); };

  async function onSubmit(e) {
    e.preventDefault();
    if (busy.submitting) return;
    setTopError(null); setBusy((b) => ({ ...b, submitting: true }));
    try {
      const r = await submitExperiment(api, form, ctx, keyFor());
      if (r.ok) { apply({ fieldErrors: {}, general: [], warnings: r.warnings }); navigate("live", r.experiment.id); return; }
      apply(r);
      if (r.stage === "server" && r.error && !r.error.issues.length) setTopError(r.error);
    } catch (err) { setTopError(err); } finally { setBusy((b) => ({ ...b, submitting: false })); }
  }
  async function onCheck() {
    setTopError(null); setBusy((b) => ({ ...b, checking: true }));
    try { const r = await checkWithServer(api, form, ctx); apply({ fieldErrors: r.fieldErrors, general: r.general, warnings: r.warnings }); setCheckResult(r); }
    catch (err) { setTopError(err); } finally { setBusy((b) => ({ ...b, checking: false })); }
  }
  async function onStop(id) {
    setStopping(id); setStopMessage(null);
    const r = await stopExperiment(api, id);
    setStopMessage(r.message); setStopping(null); activeState.reload();
  }
  return (
    <>
      {topError ? <ErrorState error={topError} title="The experiment was not started" /> : null}
      <ConfigureView approaches={approaches} scenarios={scenarios} capabilities={capabilities} form={form} setForm={setForm} errors={errors} general={general} warnings={warnings}
        submitting={busy.submitting} checking={busy.checking} checkResult={checkResult} onSubmit={onSubmit} onCheck={onCheck}
        active={activeState.data || []} onStop={onStop} stopping={stopping} stopMessage={stopMessage} />
      {activeState.status === "error" ? <ErrorState error={activeState.error} onRetry={activeState.reload} title="Could not load the active experiments" /> : null}
    </>
  );
}
