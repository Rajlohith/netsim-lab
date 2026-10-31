import { describe, expect, it } from "vitest";
import { fmtKbps, fmtMs, fmtPercent, fmtRunClock, fmtTimestamp, NA, originText, shortId } from "./format.js";
import { csvCell, toCsv } from "./csv.js";
import { parseHash, hrefFor, ROUTES } from "./router.js";
import { niceTicks } from "./scale.js";
import { createPoller } from "./poll.js";
import { parseNumberField } from "./numbers.js";

describe("format", () => {
  it("renders missing values as an em dash, never as zero", () => {
    expect(fmtMs(null)).toBe(NA); expect(fmtPercent(undefined)).toBe(NA); expect(fmtKbps(NaN)).toBe(NA); expect(fmtRunClock(null)).toBe(NA);
    expect(fmtMs(0)).toBe("0 ms");
  });
  it("shows units", () => {
    expect(fmtMs(33)).toBe("33 ms"); expect(fmtPercent(1.894737)).toBe("1.89 %"); expect(fmtKbps(50228.872)).toBe("50.2 kbit/s"); expect(fmtRunClock(14e9)).toBe("14.000 s");
  });
  it("formats UTC timestamps and ids", () => {
    expect(fmtTimestamp("2026-10-30T11:10:48.374434Z")).toBe("2026-10-30 11:10:48 UTC"); expect(shortId("exp-33735f5023a6bc9f")).toBe("33735f50");
  });
  it("says what a data origin is", () => {
    expect(originText("simulated")).toContain("Simulated"); expect(originText("real-kernel-loopback-tcp")).toContain("Loopback");
  });
});

describe("csv", () => {
  it("quotes and guards formula injection", () => {
    expect(csvCell('a,"b"')).toBe('"a,""b"""'); expect(csvCell("=HYPERLINK(1)")).toBe("'=HYPERLINK(1)"); expect(csvCell(null)).toBe(""); expect(csvCell(1.5)).toBe("1.5"); expect(csvCell(true)).toBe("true");
  });
  it("writes header + rows", () => {
    expect(toCsv([{ key: "a", label: "A" }, { key: "b" }], [{ a: 1, b: null }])).toBe("A,b\r\n1,\r\n");
  });
});

describe("router", () => {
  it("parses every page and ids", () => {
    for (const r of ROUTES) expect(parseHash(`#/${r.name}`).name).toBe(r.name);
    expect(parseHash("#/live/exp-0123456789abcdef")).toMatchObject({ name: "live", params: { id: "exp-0123456789abcdef" } });
    expect(parseHash("")).toMatchObject({ name: "topology" });
  });
  it("rejects unknown routes and ids on pages that take none", () => {
    expect(parseHash("#/nope").name).toBe("not-found"); expect(parseHash("#/topology/x").name).toBe("not-found"); expect(parseHash("#/live/a/b").name).toBe("not-found");
  });
  it("builds hrefs", () => { expect(hrefFor("history", "exp-1")).toBe("#/history/exp-1"); expect(hrefFor("models")).toBe("#/models"); });
});

describe("scale + numbers", () => {
  it("niceTicks cover the range and include 0 for bars", () => {
    const t = niceTicks(0, 91, 5, true); expect(t[0]).toBe(0); expect(t[t.length - 1] >= 91).toBe(true);
  });
  it("parseNumberField distinguishes empty, invalid and numbers", () => {
    expect(parseNumberField("")).toBeNull(); expect(Number.isNaN(parseNumberField("abc"))).toBe(true); expect(parseNumberField(" 2.5 ")).toBe(2.5); expect(parseNumberField("1e3")).toBe(1000);
  });
});

describe("poller", () => {
  function harness() { const q = []; return { q, schedule: (f, ms) => { q.push({ f, ms }); return q.length; }, cancel: () => {} }; }
  const flush = () => new Promise((r) => setTimeout(r, 0));
  it("polls until shouldStop, then stops scheduling", async () => {
    const h = harness(); let n = 0; const seen = [];
    const p = createPoller({ fn: async () => ++n, shouldStop: (v) => v >= 3, onResult: (v) => seen.push(v), schedule: h.schedule, cancel: h.cancel, intervalMs: 50 });
    p.start(); await flush(); h.q.shift().f(); await flush(); h.q.shift().f(); await flush();
    expect(seen).toEqual([1, 2, 3]); expect(h.q).toHaveLength(0); expect(p.running).toBe(false);
  });
  it("backs off after errors and recovers", async () => {
    const h = harness(); let n = 0; const errs = [];
    const p = createPoller({ fn: async () => { n++; if (n < 3) throw new Error("down"); return n; }, onError: (e, k) => errs.push(k), schedule: h.schedule, cancel: h.cancel, intervalMs: 100, maxBackoffMs: 350 });
    p.start(); await flush(); expect(h.q[0].ms).toBe(200); h.q.shift().f(); await flush(); expect(h.q[0].ms).toBe(350);
    h.q.shift().f(); await flush(); expect(errs).toEqual([1, 2]); expect(h.q[0].ms).toBe(100);
  });
  it("drops answers that arrive after stop()", async () => {
    const h = harness(); const seen = []; let release;
    const p = createPoller({ fn: () => new Promise((r) => { release = r; }), onResult: (v) => seen.push(v), schedule: h.schedule, cancel: h.cancel });
    p.start(); p.stop(); release(1); await flush(); expect(seen).toEqual([]);
  });
});
