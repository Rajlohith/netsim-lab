import React from "react";
import { PageHeader } from "../components/Common.jsx";
import { DataNotice } from "../components/Badges.jsx";
import { hrefFor } from "../lib/router.js";

const COMMANDS = [
  ["Show the design of a campaign without running it", "make exp-plan"],
  ["Run or resume the example campaign (372 runs)", "make exp-example"],
  ["Recompute statistics and exports from saved results", "make exp-analyze DIR=data/experiments/stage7-example"],
  ["Draw charts from the saved analysis", "make exp-plots DIR=data/experiments/stage7-example"],
  ["Re-run a sample of saved runs and check they are identical", "make exp-reproduce DIR=data/experiments/stage7-example"],
];

// Campaigns are a command-line workflow: there is no campaign endpoint in the API, so this page describes the workflow instead of showing results it cannot load.
export default function ResearchPage() {
  return (
    <>
      <PageHeader title="Campaign analysis"><p>Replicated experiments and statistical comparison of mobility approaches.</p></PageHeader>
      <DataNotice>Campaigns run from the command line and write their raw runs, statistics and charts to files. The dashboard does not load campaign results yet; the single-run pages show one run per configuration, so their differences are observations, not established effects.</DataNotice>
      <section className="card" aria-labelledby="rs-what"><h2 id="rs-what">What a campaign does</h2>
        <ul>
          <li>Repeats every approach and scenario with fixed, recorded seeds so any run can be reproduced.</li>
          <li>Summarises each metric with medians, quartiles and exact distribution-free 95 % confidence intervals.</li>
          <li>Compares approaches pairwise with a Wilcoxon signed-rank test, Holm correction for multiple comparisons and seeded bootstrap intervals for the median difference.</li>
          <li>Treats repeated samples from one run as one observation, never as independent ones.</li>
        </ul>
        <p className="muted">Method and limits: <code>docs/EXPERIMENTS.md</code>.</p>
      </section>
      <section aria-labelledby="rs-cli"><h2 id="rs-cli">Commands</h2>
        <div className="table-scroll"><table><thead><tr><th>Task</th><th>Command</th></tr></thead>
          <tbody>{COMMANDS.map(([t, c]) => <tr key={c}><td>{t}</td><td><code>{c}</code></td></tr>)}</tbody></table></div>
      </section>
      <p>Related: <a href={hrefFor("models")}>Model evaluation</a> · <a href={hrefFor("predictions")}>Handoff prediction</a></p>
    </>
  );
}
