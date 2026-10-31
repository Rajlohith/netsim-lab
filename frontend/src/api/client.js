// Low-level HTTP client for the NetSim Lab API. No React in here: pages never call fetch directly.
// Every failure becomes an ApiError (network errors included) so the UI has exactly one error shape to show.

export class ApiError extends Error {
  constructor({ status = 0, code = "error", message = "request failed", issues = [], context = {}, requestId = null, retryAfter = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.issues = issues;
    this.context = context;
    this.requestId = requestId;
    this.retryAfter = retryAfter;
  }
}

export function buildQuery(params = {}) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => x !== undefined && x !== null && x !== "" && sp.append(k, String(x)));
    else sp.append(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

async function toApiError(res) {
  let env = null;
  try {
    env = await res.json();
  } catch {
    env = null;
  }
  const e = env && env.error ? env.error : null;
  return new ApiError({
    status: res.status,
    code: (e && e.code) || `http_${res.status}`,
    message: (e && e.message) || `the server answered HTTP ${res.status}`,
    issues: (e && e.issues) || [],
    context: (e && e.context) || {},
    requestId: (e && e.request_id) || (res.headers && res.headers.get && res.headers.get("X-Request-ID")) || null,
    retryAfter: (res.headers && res.headers.get && res.headers.get("Retry-After")) || null,
  });
}

export function filenameFromDisposition(value, fallback) {
  if (!value) return fallback;
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(value);
  return m ? decodeURIComponent(m[1]) : fallback;
}

export function createClient({ baseUrl = "/api", fetchImpl, token } = {}) {
  const doFetch = (...a) => (fetchImpl || globalThis.fetch)(...a);
  const headers = (extra) => ({ Accept: "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...extra });

  async function send(method, path, { query, body, headers: extra } = {}) {
    let res;
    try {
      res = await doFetch(`${baseUrl}${path}${buildQuery(query)}`, {
        method,
        headers: headers({ ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...extra }),
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      throw new ApiError({ status: 0, code: "network_error", message: `cannot reach the API (${e && e.message ? e.message : e})` });
    }
    return res;
  }

  async function request(method, path, opts) {
    const res = await send(method, path, opts);
    if (!res.ok) throw await toApiError(res);
    if (res.status === 204) return { data: null, status: 204, headers: res.headers };
    let data;
    try {
      data = await res.json();
    } catch {
      throw new ApiError({ status: res.status, code: "invalid_response", message: "the server answered with something that is not JSON" });
    }
    return { data, status: res.status, headers: res.headers };
  }

  return {
    baseUrl,
    get: async (path, query) => (await request("GET", path, { query })).data,
    post: async (path, body, opts = {}) => (await request("POST", path, { body, ...opts })).data,
    // POST that also reports the HTTP status (202 = created, 200 = idempotent replay)
    postWithStatus: async (path, body, opts = {}) => {
      const r = await request("POST", path, { body, ...opts });
      return { data: r.data, status: r.status, replayed: r.headers && r.headers.get ? r.headers.get("Idempotent-Replayed") === "true" : false };
    },
    // GET a file: resolves { blob, filename, contentType } or throws ApiError
    getFile: async (path, query, fallbackName = "download") => {
      const res = await send("GET", path, { query, headers: { Accept: "*/*" } });
      if (!res.ok) throw await toApiError(res);
      const blob = await res.blob();
      return { blob, filename: filenameFromDisposition(res.headers.get("Content-Disposition"), fallbackName), contentType: res.headers.get("Content-Type") };
    },
  };
}
