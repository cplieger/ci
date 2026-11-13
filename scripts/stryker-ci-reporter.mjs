// mutation-stryker-repo.yaml's `ci` reporter, loaded with --appendPlugins from the ci checkout, so
// it imports nothing from the target's node_modules; scripts/stryker-shard.py reads its files.
// CI_STRYKER_PLAN=<file>: write the worker and runner counts and, per file, its run and early
// mutants, their netTime, each mutant's [first, last line, netTime | null] and its id [start line,
// column, end line, column, mutator, early status | null, reason | null], then exit 0.
// CI_STRYKER_TIMES=<file>, CI_STRYKER_RESULT=<file>: after "#workers", "#launch" and "#start" rows,
// append "<epoch ms>\t<file>\t<start line>:<column>-<end line>:<column> <mutator>" (its plan pass
// id) as each planned mutant finishes; then write the report as the json reporter would, and "#done".
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const launch = Math.round(Date.now() - process.uptime() * 1000);

// Stryker's own resolution of the concurrency option, and of the test runners while checkers
// hold half of it (packages/core/src/concurrent/concurrency-token-provider.ts). freeCheckers()
// returns those slots once checking ends, so runners() is the phase's lower bound.
function workers(concurrency) {
  const n = os.availableParallelism();
  if (typeof concurrency === 'number') return concurrency;
  const pct = typeof concurrency === 'string' && /^(100|[1-9]?[0-9])%$/.exec(concurrency);
  if (pct) return Math.max(1, Math.round((n * Number(pct[1])) / 100));
  return n > 4 ? n - 1 : n;
}

function runners(options, w) {
  return (options.checkers ?? []).length > 0 ? Math.max(Math.floor(w / 2), 1) : w;
}

function relative(file) {
  return path.relative(process.cwd(), file).split(path.sep).join('/');
}

class CiReporter {
  static inject = ['options', 'fileDescriptions'];

  constructor(options, fileDescriptions) {
    this.workers = workers(options.concurrency);
    this.runners = runners(options, this.workers);
    const mutated = Object.entries(fileDescriptions).filter(([, d]) => d.mutate);
    this.mutated = mutated.map(([f]) => relative(f));
    this.ranged = mutated.some(([, d]) => Array.isArray(d.mutate));
    this.plan = process.env.CI_STRYKER_PLAN || '';
    this.times = process.env.CI_STRYKER_TIMES || '';
    this.result = process.env.CI_STRYKER_RESULT || '';
    this.run = new Set();
  }

  onMutationTestingPlanReady({ mutantPlans }) {
    const now = Date.now();
    if (this.plan) {
      const empty = () => ({ run: 0, early: 0, net_ms: 0, mutants: [], ids: [] });
      const files = Object.fromEntries(this.mutated.map((f) => [f, empty()]));
      for (const p of mutantPlans) {
        const f = (files[relative(p.mutant.fileName)] ??= empty());
        const { start, end } = p.mutant.location;
        if (p.plan === 'Run') {
          f.run += 1;
          f.net_ms += p.netTime;
        } else {
          f.early += 1;
        }
        f.mutants.push([start.line + 1, end.line + 1, p.plan === 'Run' ? p.netTime : null]);
        const early = p.plan === 'Run' ? [null, null] : [p.mutant.status, p.mutant.statusReason ?? null];
        f.ids.push([start.line + 1, start.column + 1, end.line + 1, end.column + 1, p.mutant.mutatorName, ...early]);
      }
      const doc = { workers: this.workers, runners: this.runners, ranged: this.ranged, setup_ms: now - launch, files };
      fs.writeFileSync(this.plan, JSON.stringify(doc));
      process.exit(0);
    }
    if (this.times) {
      for (const p of mutantPlans) if (p.plan === 'Run') this.run.add(p.mutant.id);
      fs.appendFileSync(this.times, `#workers\t${this.workers}\n#launch\t${launch}\n#start\t${now}\n`);
    }
  }

  // result.location is already a 1-based schema location (core's toSchemaLocation), as the plan's ids are.
  onMutantTested(result) {
    if (this.times && this.run.has(result.id)) {
      const { start, end } = result.location;
      const key = `${start.line}:${start.column}-${end.line}:${end.column} ${result.mutatorName}`;
      fs.appendFileSync(this.times, `${Date.now()}\t${relative(result.fileName)}\t${key}\n`);
    }
  }

  onMutationTestReportReady(report) {
    if (this.result) fs.writeFileSync(this.result, JSON.stringify(report));
    if (this.times) fs.appendFileSync(this.times, '#done\n');
  }
}

export const strykerPlugins = [{ kind: 'Reporter', name: 'ci', injectableClass: CiReporter }];
