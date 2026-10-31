// Runs the *.test.js / *.test.jsx files that need no DOM (pure logic + server-side rendering) with esbuild + a tiny vitest shim.
// For environments where `npm install` is impossible. The authoritative command is `npm test` (vitest). Files that import
// @testing-library/react are skipped here and reported as such.
import { createRequire } from "node:module";
import { readdirSync, readFileSync, statSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
const { build } = createRequire(import.meta.url)("esbuild");
const walk = (d) => readdirSync(d).flatMap((f) => { const p = join(d, f); return statSync(p).isDirectory() ? walk(p) : [p]; });
const files = walk("src").filter((f) => /\.test\.jsx?$/.test(f));
const out = mkdtempSync(join(tmpdir(), "netsim-tests-"));
let P = 0, F = 0, S = 0; const skippedFiles = [];
for (const f of files) {
  if (readFileSync(f, "utf8").includes("@testing-library/react")) { skippedFiles.push(f); continue; }
  const outfile = join(out, f.replace(/[\\/]/g, "_") + ".mjs");
  await build({ stdin: { contents: `import ${JSON.stringify("./" + f)}; export { __run } from ${JSON.stringify("./test-support/vitest-shim.mjs")};`, resolveDir: process.cwd(), loader: "js" }, bundle: true, outfile, format: "esm", platform: "node", jsx: "automatic", logLevel: "error", loader: { ".json": "json" },
    alias: { vitest: resolve("test-support/vitest-shim.mjs") }, nodePaths: (process.env.NODE_PATH || "").split(":").filter(Boolean), banner: { js: "import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);" } });
  const shim = await import(pathToFileURL(outfile));   // registers tests
  const r = await shim.__run();
  console.log(`${r.fail ? "FAIL" : "ok  "} ${f}: ${r.pass} passed, ${r.fail} failed, ${r.skip} skipped`);
  r.failures.forEach((m) => console.log("   ✗ " + m));
  P += r.pass; F += r.fail; S += r.skip;
}
skippedFiles.forEach((f) => console.log(`SKIP ${f} (needs @testing-library/react + jsdom: run with vitest)`));
console.log(`\nTOTAL (sandbox runner): ${P} passed, ${F} failed, ${S} skipped; ${skippedFiles.length} file(s) not run`);
process.exit(F ? 1 : 0);
