import { describe, expect, it, vi } from "vitest";
import { ApiError, buildQuery, createClient, filenameFromDisposition } from "./client.js";
import { createApi } from "./endpoints.js";
import errLive from "../test/fixtures/api/error_422_live_environment.json";
import err404 from "../test/fixtures/api/error_404_unknown_experiment.json";

const res = (status, body, headers = {}) => ({ ok: status >= 200 && status < 300, status, headers: { get: (k) => headers[k] ?? headers[k.toLowerCase()] ?? null }, json: async () => body, blob: async () => ({ size: 3 }) });

describe("client", () => {
  it("builds queries, skipping empty values and repeating arrays", () => {
    expect(buildQuery({ a: 1, b: "", c: null, d: ["x", "y"] })).toBe("?a=1&d=x&d=y"); expect(buildQuery({})).toBe("");
  });
  it("sends JSON and a bearer token when configured", async () => {
    const f = vi.fn(async () => res(200, { ok: 1 }));
    const c = createClient({ baseUrl: "/api", fetchImpl: f, token: "t0k" });
    await c.post("/x", { a: 1 });
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/x"); expect(init.method).toBe("POST"); expect(init.body).toBe('{"a":1}'); expect(init.headers.Authorization).toBe("Bearer t0k"); expect(init.headers["Content-Type"]).toBe("application/json");
  });
  it("turns the real 422 envelope into an ApiError with issues", async () => {
    const { status, ...body } = errLive;
    const c = createClient({ fetchImpl: async () => res(status, body) });
    const e = await c.get("/x").catch((x) => x);
    expect(e instanceof ApiError).toBe(true); expect(e.status).toBe(422); expect(e.code).toBe(body.error.code); expect(e.issues.length > 0).toBe(true);
  });
  it("maps the real 404 envelope", async () => {
    const { status, ...body } = err404;
    const e = await createClient({ fetchImpl: async () => res(status, body) }).get("/x").catch((x) => x);
    expect(e.status).toBe(404); expect(e.code).toBe("not_found");
  });
  it("reports network failures as network_error (status 0)", async () => {
    const e = await createClient({ fetchImpl: async () => { throw new TypeError("Failed to fetch"); } }).get("/x").catch((x) => x);
    expect(e.code).toBe("network_error"); expect(e.status).toBe(0); expect(e.message).toContain("Failed to fetch");
  });
  it("handles non-JSON error pages and non-JSON success bodies", async () => {
    const bad = { ok: false, status: 502, headers: { get: () => null }, json: async () => { throw new Error("html"); } };
    expect((await createClient({ fetchImpl: async () => bad }).get("/x").catch((x) => x)).code).toBe("http_502");
    const ok = { ok: true, status: 200, headers: { get: () => null }, json: async () => { throw new Error("html"); } };
    expect((await createClient({ fetchImpl: async () => ok }).get("/x").catch((x) => x)).code).toBe("invalid_response");
  });
  it("getFile returns blob and the server's file name", async () => {
    const c = createClient({ fetchImpl: async () => res(200, null, { "Content-Disposition": 'attachment; filename="exp-1.json"', "Content-Type": "application/json" }) });
    const f = await c.getFile("/x", {}, "fallback.bin");
    expect(f.filename).toBe("exp-1.json"); expect(f.blob.size).toBe(3);
    expect(filenameFromDisposition(null, "fb")).toBe("fb");
  });
  it("getFile throws ApiError on failure", async () => {
    const c = createClient({ fetchImpl: async () => res(409, { error: { code: "results_not_available", message: "not finished", issues: [], context: {} } }) });
    expect((await c.getFile("/x").catch((x) => x)).code).toBe("results_not_available");
  });
});

describe("endpoints (URL scheme of docs/API.md)", () => {
  function api() { const f = vi.fn(async () => res(200, {})); return { f, api: createApi(createClient({ baseUrl: "/api", fetchImpl: f })) }; }
  const last = (f) => f.mock.calls[f.mock.calls.length - 1];
  it("uses the documented paths", async () => {
    const { f, api: a } = api();
    await a.approaches(); expect(last(f)[0]).toBe("/api/approaches");
    await a.topology(); expect(last(f)[0]).toBe("/api/topology");
    await a.modelEvaluation("stage6"); expect(last(f)[0]).toBe("/api/models/stage6/evaluation");
    await a.listExperiments({ status: ["queued", "running"], limit: 25, offset: 50, approach: "" }); expect(last(f)[0]).toBe("/api/experiments?status=queued&status=running&limit=25&offset=50");
    await a.transitions("exp-1", 4); expect(last(f)[0]).toBe("/api/experiments/exp-1/transitions?after_seq=4");
    await a.predictions("exp-1", { limit: 10, offset: 20 }); expect(last(f)[0]).toBe("/api/experiments/exp-1/predictions?limit=10&offset=20");
    await a.exportExperiment("exp-1", { format: "csv", table: "events" }).catch(() => {}); expect(last(f)[0]).toBe("/api/experiments/exp-1/export?format=csv&table=events");
    await a.exportExperiment("exp-1", { format: "zip", includePartial: true }).catch(() => {}); expect(last(f)[0]).toBe("/api/experiments/exp-1/export?format=zip&include_partial=true");
    await a.exportHistory({ status: "completed" }, "json").catch(() => {}); expect(last(f)[0]).toBe("/api/history/export?format=json&status=completed");
  });
  it("starts with an Idempotency-Key header and reports replays; cancel posts to /cancel", async () => {
    const f = vi.fn(async () => res(200, { id: "exp-1" }, { "Idempotent-Replayed": "true" }));
    const a = createApi(createClient({ fetchImpl: f }));
    const r = await a.startExperiment({ approach: "mip-basic" }, "key-1");
    expect(last(f)[1].headers["Idempotency-Key"]).toBe("key-1"); expect(r.replayed).toBe(true); expect(r.status).toBe(200);
    await a.cancelExperiment("exp-1"); expect(last(f)[0]).toBe("/api/experiments/exp-1/cancel"); expect(last(f)[1].method).toBe("POST");
  });
});
