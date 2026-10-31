// SANDBOX SUBSTITUTE for `vitest` (describe/it/expect/vi subset). Used ONLY by scripts/run-sandbox-tests.mjs where vitest cannot be
// installed (no network). The real suite is run with `npm test` (vitest). Failures here are real failures; a pass here is not a vitest pass.
const suites = [];
let current = { name: "", tests: [], before: [], after: [] };
const root = current;
export function describe(name, fn) { const prev = current; current = { name: prev.name ? `${prev.name} › ${name}` : name, tests: prev.tests, before: [...prev.before], after: [...prev.after] }; fn(); current = prev; }
function reg(name, fn, skip) { current.tests.push({ name: current.name ? `${current.name} › ${name}` : name, fn, skip, before: [...current.before], after: [...current.after] }); }
export const it = (n, f) => reg(n, f, false); it.skip = (n, f) => reg(n, f, true); it.skipIf = (c) => (c ? it.skip : it); export const test = it;
export const beforeEach = (f) => current.before.push(f); export const afterEach = (f) => current.after.push(f);

const eq = (a, b) => { if (Object.is(a, b)) return true; if (typeof a !== "object" || typeof b !== "object" || !a || !b) return false; if (Array.isArray(a) !== Array.isArray(b)) return false; const ka = Object.keys(a), kb = Object.keys(b); return ka.length === kb.length && ka.every((k) => eq(a[k], b[k])); };
const subset = (a, b) => (typeof b !== "object" || b === null ? Object.is(a, b) : a && typeof a === "object" && Object.keys(b).every((k) => subset(a[k], b[k])));
const show = (v) => { try { return typeof v === "string" ? JSON.stringify(v).slice(0, 200) : JSON.stringify(v)?.slice(0, 200) ?? String(v); } catch { return String(v); } };
function matchers(actual, neg) {
  const chk = (ok, msg) => { if (neg ? ok : !ok) throw new Error(`expected ${show(actual)} ${neg ? "not " : ""}${msg}`); };
  return {
    toBe: (e) => chk(Object.is(actual, e), `to be ${show(e)}`), toEqual: (e) => chk(eq(actual, e), `to equal ${show(e)}`), toStrictEqual: (e) => chk(eq(actual, e), `to equal ${show(e)}`),
    toBeNull: () => chk(actual === null, "to be null"), toBeUndefined: () => chk(actual === undefined, "to be undefined"), toBeDefined: () => chk(actual !== undefined, "to be defined"),
    toBeTruthy: () => chk(!!actual, "to be truthy"), toBeFalsy: () => chk(!actual, "to be falsy"),
    toContain: (e) => chk(actual != null && actual.includes(e), `to contain ${show(e)}`), toMatch: (re) => chk(re.test(actual), `to match ${re}`),
    toHaveLength: (n) => chk(actual != null && actual.length === n, `to have length ${n} (was ${actual && actual.length})`),
    toBeCloseTo: (e, d = 2) => chk(Math.abs(actual - e) < 10 ** -d / 2, `to be close to ${e}`), toBeGreaterThan: (e) => chk(actual > e, `to be > ${e}`), toBeLessThan: (e) => chk(actual < e, `to be < ${e}`),
    toMatchObject: (e) => chk(subset(actual, e), `to match object ${show(e)}`),
    toThrow: (m) => { let t = null; try { actual(); } catch (e) { t = e; } chk(t !== null && (m === undefined || (m instanceof RegExp ? m.test(t.message) : t.message.includes(m))), `to throw ${m ?? ""}`); },
    toHaveBeenCalledTimes: (n) => chk(actual.mock.calls.length === n, `to have been called ${n} times (was ${actual.mock.calls.length})`),
    toHaveBeenCalled: () => chk(actual.mock.calls.length > 0, "to have been called"),
    toHaveBeenCalledWith: (...a) => chk(actual.mock.calls.some((c) => eq(c, a)), `to have been called with ${show(a)}`),
  };
}
export function expect(actual) {
  const m = matchers(actual, false); m.not = matchers(actual, true);
  const wrap = (neg) => new Proxy({}, { get: (_, k) => async (...a) => { let v, err = null; try { v = await actual; } catch (e) { err = e; } if (neg === "rejects") { if (err === null) throw new Error("expected promise to reject"); return matchers(err, false)[k](...a); } if (err) throw err; return matchers(v, false)[k](...a); } });
  m.rejects = new Proxy({}, { get: (_, k) => async (...a) => { let err = null; try { await actual; } catch (e) { err = e; } if (err === null) throw new Error("expected promise to reject"); if (k === "toThrow") { const msg = a[0]; if (msg !== undefined && !(msg instanceof RegExp ? msg.test(err.message) : err.message.includes(msg))) throw new Error(`rejected with ${err.message}`); return; } return matchers(err, false)[k](...a); } });
  m.resolves = wrap("resolves");
  return m;
}
export const vi = {
  fn(impl) { const f = (...a) => { f.mock.calls.push(a); return f._impl ? f._impl(...a) : undefined; }; f.mock = { calls: [] }; f._impl = impl; f.mockResolvedValue = (v) => { f._impl = async () => v; return f; }; f.mockRejectedValue = (e) => { f._impl = async () => { throw e; }; return f; }; f.mockImplementation = (i) => { f._impl = i; return f; }; return f; },
  stubGlobal(k, v) { (vi._stubs ||= []).push([k, globalThis[k]]); globalThis[k] = v; }, unstubAllGlobals() { for (const [k, v] of (vi._stubs || []).reverse()) globalThis[k] = v; vi._stubs = []; },
};
export async function __run() {
  let pass = 0, fail = 0, skip = 0; const failures = [];
  for (const t of root.tests) {
    if (t.skip) { skip++; continue; }
    try { for (const b of t.before) await b(); await t.fn(); pass++; } catch (e) { fail++; failures.push(`${t.name}\n      ${e && e.stack ? e.stack.split("\n").slice(0, 3).join("\n      ") : e}`); } finally { for (const a of t.after) { try { await a(); } catch {} } vi.unstubAllGlobals(); }
  }
  return { pass, fail, skip, failures };
}
