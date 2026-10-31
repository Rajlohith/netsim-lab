// Production-bundle sanity check used where `vite build` cannot run (no network -> no vite). It bundles src/main.jsx with esbuild
// (the bundler Vite itself uses), minified, and fails on any unresolved import or syntax error. It is NOT a substitute for `npm run build`.
import { createRequire } from "node:module";
const { build } = createRequire(import.meta.url)("esbuild");   // CJS require honours NODE_PATH (ESM import does not)
import { mkdtempSync, statSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const out = mkdtempSync(join(tmpdir(), "netsim-bundle-"));
const result = await build({
  entryPoints: ["src/main.jsx"], bundle: true, minify: true, outdir: out, format: "esm", jsx: "automatic", platform: "browser", target: "es2020",
  loader: { ".css": "css" }, logLevel: "error", metafile: true, nodePaths: (process.env.NODE_PATH || "").split(":").filter(Boolean),
  define: { "process.env.NODE_ENV": '"production"' },
});
const files = readdirSync(out).map((f) => `${f} ${(statSync(join(out, f)).size / 1024).toFixed(1)} kB`);
console.log("bundle OK:", files.join(", "), `| inputs: ${Object.keys(result.metafile.inputs).length}`);
