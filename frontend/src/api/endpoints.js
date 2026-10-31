// One function per documented endpoint (docs/API.md). Pages depend on this object, never on fetch.
import { createClient } from "./client.js";

export function createApi(client = createClient()) {
  const exp = (id) => `/experiments/${encodeURIComponent(id)}`;
  return {
    client,
    health: () => client.get("/health"),
    approaches: () => client.get("/approaches"),
    scenarios: () => client.get("/scenarios"),
    capabilities: () => client.get("/capabilities"),
    status: () => client.get("/status"),
    topology: () => client.get("/topology"),
    models: () => client.get("/models"),
    modelEvaluation: (id) => client.get(`/models/${encodeURIComponent(id)}/evaluation`),

    validateExperiment: (body) => client.post("/experiments/validate", body),
    // resolves { data, status, replayed }; a repeated idempotencyKey returns the original experiment instead of a second one
    startExperiment: (body, idempotencyKey) =>
      client.postWithStatus("/experiments", body, idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : {}),
    cancelExperiment: (id) => client.postWithStatus(`${exp(id)}/cancel`, undefined),
    listExperiments: (params = {}) =>
      client.get("/experiments", {
        status: params.status, approach: params.approach, scenario: params.scenario, environment: params.environment,
        label: params.label, sort: params.sort, limit: params.limit, offset: params.offset,
      }),
    getExperiment: (id) => client.get(exp(id)),
    transitions: (id, afterSeq = 0) => client.get(`${exp(id)}/transitions`, { after_seq: afterSeq }),
    metrics: (id) => client.get(`${exp(id)}/metrics`),
    handoffs: (id) => client.get(`${exp(id)}/handoffs`),
    events: (id, { afterSeq = 0, limit = 5000 } = {}) => client.get(`${exp(id)}/events`, { after_seq: afterSeq, limit }),
    predictions: (id, { limit = 5000, offset = 0 } = {}) => client.get(`${exp(id)}/predictions`, { limit, offset }),

    // file downloads (resolve { blob, filename, contentType })
    exportExperiment: (id, { format = "json", table, includePartial } = {}) =>
      client.getFile(`${exp(id)}/export`, { format, table: format === "csv" ? table || "handoffs" : undefined, include_partial: includePartial ? "true" : undefined }, `${id}.${format}`),
    exportHistory: (filters = {}, format = "csv") =>
      client.getFile("/history/export", { format, status: filters.status, approach: filters.approach, scenario: filters.scenario, environment: filters.environment }, `history.${format}`),
  };
}

// VITE_API_BASE_URL lets a deployment point the dashboard at another origin (the API must then allow CORS, see docs/API.md); default: same origin /api
export const api = createApi(createClient({ baseUrl: (import.meta.env && import.meta.env.VITE_API_BASE_URL) || "/api" }));
