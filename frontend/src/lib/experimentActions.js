// Start / stop logic for experiments, independent of React so it can be tested directly.
import { ApiError } from "../api/client.js";
import { formToRequest, issuesToFieldErrors, validateForm } from "./validation.js";

export const ACTIVE = new Set(["queued", "running"]);
export const TERMINAL = new Set(["completed", "failed", "cancelled", "incomplete"]);

export function newIdempotencyKey(rand = () => Math.random().toString(36).slice(2, 10), now = () => Date.now()) {
  return `ui-${now().toString(36)}-${rand()}${rand()}`.slice(0, 64);
}

// -> { ok: true, experiment, replayed } | { ok: false, stage: 'client'|'server', fieldErrors, general, error }
export async function submitExperiment(api, form, ctx, idempotencyKey) {
  const local = validateForm(form, ctx);
  if (Object.keys(local.errors).length) return { ok: false, stage: "client", fieldErrors: local.errors, general: [], warnings: local.warnings, error: null };
  try {
    const r = await api.startExperiment(formToRequest(form, ctx), idempotencyKey);
    return { ok: true, experiment: r.data, replayed: r.replayed, warnings: r.data.warnings || local.warnings };
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    const { fields, general } = issuesToFieldErrors(e.issues);
    return { ok: false, stage: "server", fieldErrors: fields, general, warnings: local.warnings, error: e };
  }
}

// Ask the server to check the request without running it: -> { valid, fieldErrors, general, warnings, resolved } (throws ApiError on transport errors)
export async function checkWithServer(api, form, ctx) {
  const local = validateForm(form, ctx);
  if (Object.keys(local.errors).length) return { valid: false, fieldErrors: local.errors, general: [], warnings: local.warnings, resolved: null };
  const rep = await api.validateExperiment(formToRequest(form, ctx));
  const { fields, general } = issuesToFieldErrors(rep.issues);
  return { valid: !!rep.valid, fieldErrors: fields, general, warnings: [...local.warnings, ...(rep.warnings || []).map((w) => (typeof w === "string" ? w : w.message || JSON.stringify(w)))], resolved: rep.resolved || null };
}

// -> { ok, experiment, message }
export async function stopExperiment(api, id) {
  try {
    const r = await api.cancelExperiment(id);
    return { ok: true, experiment: r.data, accepted: r.status === 202, message: r.status === 202 ? "Cancellation requested; the worker is being stopped." : "Experiment cancelled." };
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    const msg = e.code === "not_cancellable" ? "This experiment already finished and can no longer be cancelled." : e.message;
    return { ok: false, experiment: null, error: e, message: msg };
  }
}
