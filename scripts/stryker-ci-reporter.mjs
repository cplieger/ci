// mutation-stryker-repo.yaml's `ci` reporter, loaded with --appendPlugins from the ci checkout, so it
// imports nothing from the target's node_modules; scripts/stryker-shard.py reads its files.
// CI_STRYKER_PLAN=<file>: once the plan is ready (after the dry run, before any mutant runs),
// write the worker and test runner counts and each file to mutate with its mutants to run, its
// early results and their summed netTime, then exit 0.
// CI_STRYKER_TIMES=<file> and CI_STRYKER_RESULT=<file>: append "<epoch ms>\t<file>" as each planned
// mutant finishes, after "#workers", "#launch" (process start) and "#start" rows; once the report
// is complete, write it, as the json reporter would, to the result file, then append "#done".
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
      const files = Object.fromEntries(this.mutated.map((f) => [f, { run: 0, early: 0, net_ms: 0 }]));
      for (const p of mutantPlans) {
        const f = (files[relative(p.mutant.fileName)] ??= { run: 0, early: 0, net_ms: 0 });
        if (p.plan === 'Run') {
          f.run += 1;
          f.net_ms += p.netTime;
        } else {
          f.early += 1;
        }
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

  onMutantTested(result) {
    if (this.times && this.run.has(result.id)) {
      fs.appendFileSync(this.times, `${Date.now()}\t${relative(result.fileName)}\n`);
    }
  }

  onMutationTestReportReady(report) {
    if (this.result) fs.writeFileSync(this.result, JSON.stringify(report));
    if (this.times) fs.appendFileSync(this.times, '#done\n');
  }
}

export const strykerPlugins = [{ kind: 'Reporter', name: 'ci', injectableClass: CiReporter }];
