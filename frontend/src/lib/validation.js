// Client-side validation of the experiment form. The limits come from GET /api/capabilities (never hard-coded duplicates of server
// settings); anything the server decides on its own (duplicates, queue, unavailable dependencies) is reported by the server's reply.
import { parseNumberField } from "./numbers.js";

export const CONDITION_RANGES = {
  latency_scale: { min: 0, max: 100, minExclusive: true, label: "latency scale" },
  loss_percent: { min: 0, max: 100, maxExclusive: true, label: "random loss (%)" },
  ha_distance_ms: { min: 0, max: 5000, label: "Home Agent distance (ms)" },
  registration_loss_percent: { min: 0, max: 100, maxExclusive: true, label: "registration loss (%)" },
  ha_processing_ms: { min: 0, max: 5000, label: "Home Agent processing (ms)" },
};
export const CONDITION_KEYS = Object.keys(CONDITION_RANGES);

export function initialForm(scenario, approach) {
  const p = (scenario && scenario.parameters) || {};
  return {
    approach: approach ? approach.id : "", scenario: scenario ? scenario.id : "", environment: "emulated", handoff_mode: "bbm",
    duration_s: p.duration_s != null ? String(p.duration_s) : "10", rate_pps: "100", payload: "64", seed: "0", tcp_detect: "event",
    conditions: Object.fromEntries(CONDITION_KEYS.map((k) => [k, ""])), predict: false, prediction_algorithm: "random_forest", label: "",
  };
}

export function runnableEnvironments(approach) {
  if (!approach) return [];
  return Object.entries(approach.environments || {}).filter(([, e]) => e.exposed_via_api && e.runnable_here).map(([k]) => k);
}

function inRange(v, r) {
  if (r.min !== undefined && (r.minExclusive ? v <= r.min : v < r.min)) return false;
  if (r.max !== undefined && (r.maxExclusive ? v >= r.max : v > r.max)) return false;
  return true;
}

// -> { errors: { field: message }, warnings: [message] }
export function validateForm(form, { capabilities, approach, scenario }) {
  const errors = {}, warnings = [];
  const lim = (capabilities && capabilities.limits) || {};
  if (!approach) errors.approach = "choose a mobility approach";
  if (!scenario) errors.scenario = "choose a scenario";

  if (approach) {
    const envs = runnableEnvironments(approach);
    if (!form.environment || !envs.includes(form.environment)) {
      const e = approach.environments && approach.environments[form.environment];
      errors.environment = e && e.check_reason ? e.check_reason : `${form.environment || "environment"} cannot run this approach here (runnable: ${envs.join(", ") || "none"})`;
    }
    if (form.handoff_mode === "mbb" && !approach.supports_make_before_break) errors.handoff_mode = approach.make_before_break_note || "this approach does not support make-before-break";
    if (scenario && scenario.requires_home_agent && !approach.uses_home_agent) warnings.push("This scenario acts through the Home Agent; the chosen approach does not use one, so the run executes unchanged.");
  }

  const dur = parseNumberField(form.duration_s), rate = parseNumberField(form.rate_pps), payload = parseNumberField(form.payload), seed = parseNumberField(form.seed);
  if (dur === null || Number.isNaN(dur) || dur <= 0 || (lim.max_duration_s && dur > lim.max_duration_s)) errors.duration_s = `duration must be > 0 and ≤ ${lim.max_duration_s ?? "the server limit"} s`;
  if (rate === null || Number.isNaN(rate) || rate <= 0 || (lim.max_rate_pps && rate > lim.max_rate_pps)) errors.rate_pps = `probe rate must be > 0 and ≤ ${lim.max_rate_pps ?? "the server limit"} packets/s`;
  const [pmin, pmax] = lim.payload_bytes || [36, 1472];
  if (payload === null || !Number.isInteger(payload) || payload < pmin || payload > pmax) errors.payload = `payload must be a whole number of bytes between ${pmin} and ${pmax}`;
  const [smin, smax] = lim.seed || [0, 2 ** 31 - 1];
  if (seed === null || !Number.isInteger(seed) || seed < smin || seed > smax) errors.seed = `seed must be a whole number between ${smin} and ${smax}`;
  if (!errors.duration_s && !errors.rate_pps && lim.max_probes && dur * rate > lim.max_probes) errors.rate_pps = `duration × rate = ${Math.round(dur * rate)} probes exceeds the limit of ${lim.max_probes}`;
  if (!errors.duration_s && scenario && scenario.parameters && Array.isArray(scenario.parameters.moves)) {
    const late = scenario.parameters.moves.filter((m) => m.at_s >= dur);
    if (late.length) errors.duration_s = `the scenario moves the mobile node at ${late.map((m) => m.at_s).join(", ")} s, which is not inside a ${dur} s run`;
  }
  for (const k of CONDITION_KEYS) {
    const raw = form.conditions[k];
    if (raw === "" || raw === undefined) continue;
    const v = parseNumberField(raw);
    if (Number.isNaN(v) || v === null || !inRange(v, CONDITION_RANGES[k])) errors[`conditions.${k}`] = `${CONDITION_RANGES[k].label} must be a number in ${CONDITION_RANGES[k].minExclusive ? "(" : "["}${CONDITION_RANGES[k].min}, ${CONDITION_RANGES[k].max}${CONDITION_RANGES[k].maxExclusive ? ")" : "]"}`;
  }
  if ((form.label || "").length > (lim.label_max_chars || 80)) errors.label = `label may have at most ${lim.label_max_chars || 80} characters`;
  if (form.predict && (!capabilities || !(capabilities.prediction_models || []).length)) errors.predict = "no prediction model is available on this server";
  return { errors, warnings };
}

// Only fields the user set (or that the form always carries) are sent; unset conditions fall back to the scenario's values.
export function formToRequest(form, { approach }) {
  const req = {
    approach: form.approach, scenario: form.scenario, environment: form.environment,
    duration_s: parseNumberField(form.duration_s), rate_pps: parseNumberField(form.rate_pps), payload: parseNumberField(form.payload), seed: parseNumberField(form.seed),
  };
  if (approach && approach.supports_make_before_break) req.handoff_mode = form.handoff_mode;
  if (form.approach === "tcp-reconnect") req.tcp_detect = form.tcp_detect;
  const conditions = {};
  for (const k of CONDITION_KEYS) if (form.conditions[k] !== "" && form.conditions[k] !== undefined) conditions[k] = parseNumberField(form.conditions[k]);
  if (Object.keys(conditions).length) req.conditions = conditions;
  if (form.predict) { req.predict = true; req.prediction_algorithm = form.prediction_algorithm; }
  if ((form.label || "").trim()) req.label = form.label.trim();
  return req;
}

// Map a server reply (422 issues or a /validate report) onto form fields. Issues without a known field stay in `general`.
export function issuesToFieldErrors(issues = []) {
  const fields = {}, general = [];
  for (const i of issues) {
    if (i.field) fields[i.field] = i.message; else general.push(i.message);
  }
  return { fields, general };
}
