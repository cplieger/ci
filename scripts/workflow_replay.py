#!/usr/bin/env python3
"""Replay one job of a GitHub Actions workflow locally, for the shell probes.

Runs the job's steps in --cwd: `run:` bodies with bash and composite actions,
with every `${{ }}` and `if:` evaluated against --context (github, inputs,
needs, secrets, matrix, job; github.workspace is --cwd and runner.temp the
replay's temp dir) and the steps so far. `actions/checkout` of the
job's own repository is a no-op (the caller prepares --cwd); a checkout of
`job.workflow_repository` into a `path` links that path to --actions-root,
the ci source under test. A local action (`./<path>`) runs from --cwd, and a
`cplieger/ci/actions/<name>@<ref>` one from --actions-root, which replays an
older revision's workflow. Any other action fails unless --skip names its
step. Writes the job's and steps' outputs and every expression evaluated to
--out; exits 1 naming the step that failed. Stdlib plus PyYAML.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

CHECKOUT = 'actions/checkout@'
OWN_ACTION = re.compile(r'^cplieger/ci/actions/([A-Za-z0-9_-]+)@')
LOCAL_ACTION = re.compile(r'^\./([A-Za-z0-9_./-]+)$')
CI_REPOSITORY = 'cplieger/ci'
EXPR = re.compile(r'\$\{\{(.*?)\}\}', re.DOTALL)
TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)|(?P<op>==|!=|<=|>=|&&|\|\||[!<>()\[\].,])"
    r'|(?P<id>[A-Za-z_][A-Za-z0-9_-]*))'
)


class ReplayError(Exception):
    pass


# ── Expressions ───────────────────────────────────────────────────────────────


def to_str(v) -> str:
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


def truthy(v) -> bool:
    if isinstance(v, float) and math.isnan(v):
        return False
    return v not in (None, False, 0, '')


def to_num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == '':
            return 0.0
        try:
            return float(int(s, 16)) if s.lower().startswith('0x') else float(s)
        except ValueError:
            return math.nan
    return math.nan


def equal(a, b) -> bool:
    """GitHub's loose equality: strings case-insensitively, mixed types as numbers."""
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if type(a) is type(b) and not isinstance(a, (int, float)):
        return a == b
    return to_num(a) == to_num(b)


class Parser:
    def __init__(self, text: str, lookup, functions):
        self.toks = []
        pos = 0
        text = text.strip()
        while pos < len(text):
            m = TOKEN.match(text, pos)
            if not m or m.end() == pos:
                raise ReplayError(f'cannot parse expression {text!r} at {text[pos:]!r}')
            pos = m.end()
            kind = m.lastgroup
            self.toks.append((kind, m.group(kind)))
        self.i = 0
        self.lookup = lookup
        self.functions = functions

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise ReplayError(f'expected {value!r}, got {tok[1]!r}')
        self.i += 1
        return tok

    def parse(self):
        v = self.or_()
        if self.i != len(self.toks):
            raise ReplayError(f'trailing tokens in expression: {self.toks[self.i :]}')
        return v

    def or_(self):
        v = self.and_()
        while self.peek()[1] == '||':
            self.take()
            rhs = self.and_()
            v = v if truthy(v) else rhs
        return v

    def and_(self):
        v = self.cmp()
        while self.peek()[1] == '&&':
            self.take()
            rhs = self.cmp()
            v = rhs if truthy(v) else v
        return v

    def cmp(self):
        v = self.unary()
        while self.peek()[1] in ('==', '!=', '<', '>', '<=', '>='):
            op = self.take()[1]
            rhs = self.unary()
            if op == '==':
                v = equal(v, rhs)
            elif op == '!=':
                v = not equal(v, rhs)
            else:
                a, b = to_num(v), to_num(rhs)
                v = {'<': a < b, '>': a > b, '<=': a <= b, '>=': a >= b}[op]
        return v

    def unary(self):
        if self.peek()[1] == '!':
            self.take()
            return not truthy(self.unary())
        return self.postfix()

    def postfix(self):
        v = self.primary()
        while self.peek()[1] in ('.', '['):
            if self.take()[1] == '.':
                v = index(v, self.take()[1])
            else:
                key = self.or_()
                self.take(']')
                v = index(v, key)
        return v

    def primary(self):
        kind, value = self.take()
        if kind == 'str':
            return value[1:-1].replace("''", "'")
        if kind == 'num':
            return float(value)
        if value == '(':
            v = self.or_()
            self.take(')')
            return v
        if kind != 'id':
            raise ReplayError(f'unexpected token {value!r}')
        if value in ('true', 'false'):
            return value == 'true'
        if value == 'null':
            return None
        if self.peek()[1] == '(':
            self.take()
            args = []
            while self.peek()[1] != ')':
                args.append(self.or_())
                if self.peek()[1] == ',':
                    self.take()
            self.take(')')
            if value not in self.functions:
                raise ReplayError(f'unsupported function {value}()')
            return self.functions[value](*args)
        return self.lookup(value)


def index(v, key):
    if isinstance(v, dict):
        if key in v:
            return v[key]
        folded = {str(k).casefold(): k for k in v}
        return v.get(folded.get(str(key).casefold()))
    if isinstance(v, list) and isinstance(key, (int, float)) and 0 <= int(key) < len(v):
        return v[int(key)]
    return None


def from_json(s):
    try:
        return json.loads(to_str(s))
    except json.JSONDecodeError as exc:
        raise ReplayError(f'fromJSON({to_str(s)!r}): {exc}') from exc


FUNCTIONS = {
    'fromJSON': from_json,
    'contains': lambda h, n: (
        any(equal(x, n) for x in h)
        if isinstance(h, list)
        else to_str(n).casefold() in to_str(h).casefold()
    ),
}


class Scope:
    """One evaluation context; records every expression it evaluates."""

    def __init__(self, contexts: dict, status: dict, log: list):
        self.contexts = contexts
        self.status = status
        self.log = log

    def lookup(self, name):
        if name not in self.contexts:
            raise ReplayError(f'unsupported context {name!r}')
        return self.contexts[name]

    def functions(self):
        return {
            **FUNCTIONS,
            'success': lambda: not self.status.get('failed'),
            'failure': lambda: bool(self.status.get('failed')),
            'always': lambda: True,
            'cancelled': lambda: False,
        }

    def evaluate(self, text: str):
        self.log.append(text.strip())
        return Parser(text, self.lookup, self.functions()).parse()

    def render(self, value) -> str:
        if not isinstance(value, str):
            return to_str(value)
        whole = EXPR.fullmatch(value.strip())
        if whole:
            return to_str(self.evaluate(whole.group(1)))
        return EXPR.sub(lambda m: to_str(self.evaluate(m.group(1))), value)

    def condition(self, cond) -> bool:
        if cond is None:
            return not self.status.get('failed')
        if isinstance(cond, bool):
            return cond
        text = str(cond).strip()
        whole = EXPR.fullmatch(text)
        return truthy(self.evaluate(whole.group(1) if whole else text))


class JobScope(Scope):
    """A job-level `if:`: success() and failure() read every ancestor job's result."""

    def __init__(self, contexts: dict, log: list, results):
        super().__init__(contexts, {}, log)
        self.results = results

    def functions(self):
        return {
            **super().functions(),
            'success': lambda: all(r == 'success' for r in self.results()),
            'failure': lambda: 'failure' in self.results(),
        }


STATUS_CALL = re.compile(r'\b(?:success|failure|always|cancelled)\s*\(')


def job_condition(workflow: dict, job_name: str, contexts: dict, log: list) -> bool:
    """Evaluate a job's `if:` as GitHub does: a condition with no status function
    is `success() && (...)`, and the status functions cover every ancestor job,
    not only the direct needs. The result of an ancestor a status function reads
    must be in contexts['needs'].
    https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions
    """
    jobs = workflow['jobs']
    ancestors: list[str] = []
    pending = [jobs[job_name].get('needs')]
    while pending:
        needs = pending.pop()
        for name in [needs] if isinstance(needs, str) else needs or []:
            if name not in ancestors:
                ancestors.append(name)
                pending.append(jobs[name].get('needs'))

    def results():
        known = contexts.get('needs') or {}
        missing = sorted(a for a in ancestors if 'result' not in (known.get(a) or {}))
        if missing:
            raise ReplayError(f'{job_name}: no result in needs for ancestor job(s) {missing}')
        return [known[a]['result'] for a in ancestors]

    cond = jobs[job_name].get('if', 'success()')
    if isinstance(cond, bool):
        return cond
    text = str(cond).strip()
    whole = EXPR.fullmatch(text)
    text = (whole.group(1) if whole else text).strip()
    if not STATUS_CALL.search(text):
        text = f'success() && ({text})'
    return truthy(JobScope(contexts, log, results).evaluate(text))


# ── Steps ─────────────────────────────────────────────────────────────────────


def read_kv_file(path: Path) -> dict[str, str]:
    """$GITHUB_OUTPUT / $GITHUB_ENV syntax: name=value or name<<DELIM ... DELIM."""
    out: dict[str, str] = {}
    lines = path.read_text().splitlines() if path.exists() else []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        m = re.match(r'^([^=<\s]+)<<(.+)$', line)
        if m:
            body = []
            while i < len(lines) and lines[i] != m.group(2):
                body.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ReplayError(f'{path.name}: unterminated {m.group(1)}<<{m.group(2)}')
            i += 1
            out[m.group(1)] = '\n'.join(body)
        elif '=' in line:
            k, v = line.split('=', 1)
            out[k] = v
    return out


def require_replayable(label: str, step: dict) -> None:
    """A step whose failure the job tolerates, or that runs another shell, is not replayed."""
    if step.get('continue-on-error') or str(step.get('shell', 'bash')) != 'bash':
        raise ReplayError(f'{label}: continue-on-error and non-bash shells are not replayed')


class Job:
    def __init__(self, args, context: dict):
        self.args = args
        self.cwd = Path(args.cwd)
        self.actions_root = Path(args.actions_root)
        self.skip = set(args.skip)
        self.hooks = dict(h.split('=', 1) for h in args.hook)
        self.base_env = {**os.environ}
        self.temp = Path(tempfile.mkdtemp(prefix='replay-runner-'))
        self.log: list[str] = []
        self.ran: list[str] = []
        self.context = context

    def run_bash(self, name: str, body: str, env: dict[str, str]) -> dict[str, str]:
        out_file = self.temp / f'out-{len(self.ran)}'
        env_file = self.temp / f'env-{len(self.ran)}'
        path_file = self.temp / f'path-{len(self.ran)}'
        script = self.temp / f'step-{len(self.ran)}.sh'
        script.write_text(body)
        full = {
            **self.base_env,
            **env,
            'GITHUB_OUTPUT': str(out_file),
            'GITHUB_ENV': str(env_file),
            'GITHUB_PATH': str(path_file),
            'GITHUB_STEP_SUMMARY': str(self.temp / 'summary.md'),
            'RUNNER_TEMP': str(self.temp),
            'GITHUB_WORKSPACE': str(self.cwd),
        }
        proc = subprocess.run(
            ['bash', '--noprofile', '--norc', '-eo', 'pipefail', str(script)],
            cwd=self.cwd,
            env=full,
            capture_output=True,
            text=True,
            check=False,
        )
        (self.temp / f'log-{len(self.ran)}.txt').write_text(proc.stdout + proc.stderr)
        self.ran.append(name)
        if proc.returncode != 0:
            raise ReplayError(
                f'step {name!r} exited {proc.returncode}:\n{(proc.stdout + proc.stderr)[-3000:]}'
            )
        self.base_env.update(read_kv_file(env_file))
        if path_file.exists():
            extra = [p for p in path_file.read_text().splitlines() if p]
            self.base_env['PATH'] = ':'.join([*reversed(extra), self.base_env['PATH']])
        return read_kv_file(out_file)

    def checkout(self, name: str, with_: dict, scope: Scope) -> None:
        """The ci-source checkout links its path to --actions-root; any other is --cwd."""
        if 'path' not in with_:
            self.ran.append(f'{name} (checkout: --cwd)')
            return
        repo = scope.render(with_.get('repository', ''))
        if repo != scope.render('${{ job.workflow_repository }}'):
            raise ReplayError(f'{name}: checkout of {repo!r} into a path is not replayed')
        if not scope.render(with_.get('ref', '')):
            raise ReplayError(f'{name}: the ci-source checkout names no ref')
        dest = self.cwd / scope.render(with_['path'])
        if dest.is_symlink() and dest.resolve() == self.actions_root.resolve():
            pass
        elif dest.exists() or dest.is_symlink():
            raise ReplayError(f'{name}: {dest} already exists')
        else:
            dest.symlink_to(self.actions_root.resolve())
        self.ran.append(f'{name} (checkout: --actions-root)')

    def composite(self, name: str, path: Path, with_: dict, scope: Scope, env: dict) -> dict:
        action = path.name
        if not (path / 'action.yml').is_file():
            raise ReplayError(f'{name}: no action at {path}')
        spec = yaml.safe_load((path / 'action.yml').read_text())
        inputs = {k: to_str(v.get('default', '')) for k, v in (spec.get('inputs') or {}).items()}
        unknown = set(with_) - set(inputs)
        if unknown:
            raise ReplayError(f'{name}: {action} declares no input {sorted(unknown)}')
        inputs.update({k: scope.render(v) for k, v in with_.items()})
        steps: dict = {}
        inner = Scope({**scope.contexts, 'inputs': inputs, 'steps': steps}, scope.status, self.log)
        for step in spec['runs']['steps']:
            sid = step.get('id') or step.get('name')
            label = f'{action}/{sid}'
            if label in self.skip:
                self.ran.append(f'{label} (skipped)')
                continue
            if not inner.condition(step.get('if')):
                continue
            require_replayable(label, step)
            if 'run' not in step:
                raise ReplayError(f'{label}: only run steps are replayed inside a composite')
            step_env = {
                **env,
                **{k: inner.render(v) for k, v in (step.get('env') or {}).items()},
                'GITHUB_ACTION_PATH': str(path),
            }
            outs = self.run_bash(label, inner.render(step['run']), step_env)
            steps[step.get('id', sid)] = {
                'outputs': outs,
                'outcome': 'success',
                'conclusion': 'success',
            }
        return {k: inner.render(v.get('value', '')) for k, v in (spec.get('outputs') or {}).items()}

    def run(self, workflow: dict, job_name: str) -> dict:
        job = workflow['jobs'][job_name]
        steps: dict = {}
        status: dict = {}
        wf_env = {k: to_str(v) for k, v in (workflow.get('env') or {}).items()}
        job_ctx = {
            'status': 'success',
            'workflow_repository': CI_REPOSITORY,
            'workflow_sha': '0' * 40,
        }
        contexts = {
            **self.context,
            'github': {'workspace': str(self.cwd), **self.context.get('github', {})},
            'runner': {'temp': str(self.temp), **self.context.get('runner', {})},
            'job': {**job_ctx, **self.context.get('job', {})},
            'steps': steps,
            'env': dict(wf_env),
        }
        scope = Scope(contexts, status, self.log)
        result = {'skipped': False, 'steps': steps, 'ran': self.ran, 'expressions': self.log}
        if not job_condition(workflow, job_name, contexts, self.log):
            result['skipped'] = True
            return result
        job_env = {**wf_env, **{k: scope.render(v) for k, v in (job.get('env') or {}).items()}}
        contexts['env'] = dict(job_env)
        for step in job['steps']:
            name = step.get('name') or step.get('id') or step.get('uses')
            if name == self.args.stop_before:
                break
            if name in self.hooks:
                hook = subprocess.run(['bash', '-c', self.hooks[name]], cwd=self.cwd, check=False)
                if hook.returncode:
                    raise ReplayError(f'the hook before {name!r} exited {hook.returncode}')
            if name in self.skip:
                self.ran.append(f'{name} (skipped)')
                continue
            if not scope.condition(step.get('if')):
                self.ran.append(f'{name} (if false)')
                continue
            require_replayable(name, step)
            env = {**job_env, **{k: scope.render(v) for k, v in (step.get('env') or {}).items()}}
            uses = step.get('uses', '')
            if 'run' in step:
                outs = self.run_bash(name, scope.render(step['run']), env)
            elif uses.startswith(CHECKOUT):
                self.checkout(name, step.get('with') or {}, scope)
                outs = {}
            elif LOCAL_ACTION.match(uses) or OWN_ACTION.match(uses):
                local = LOCAL_ACTION.match(uses)
                path = (
                    self.cwd / local.group(1)
                    if local
                    else self.actions_root / 'actions' / OWN_ACTION.match(uses).group(1)
                )
                outs = self.composite(name, path, step.get('with') or {}, scope, env)
                self.ran.append(name)
            else:
                raise ReplayError(f'step {name!r} uses {uses!r}. Pass --skip to replay without it.')
            if step.get('id'):
                steps[step['id']] = {'outputs': outs, 'outcome': 'success', 'conclusion': 'success'}
        result['outputs'] = {k: scope.render(v) for k, v in (job.get('outputs') or {}).items()}
        return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--workflow', required=True)
    p.add_argument('--job', required=True)
    p.add_argument(
        '--context', required=True, help='JSON file: github, inputs, needs, secrets, matrix, job'
    )
    p.add_argument('--cwd', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--actions-root', default=str(Path(__file__).resolve().parent.parent))
    p.add_argument('--skip', action='append', default=[], help='a step name, or <action>/<step id>')
    p.add_argument('--stop-before', default='', help='end the job before this step')
    p.add_argument('--hook', action='append', default=[], help='STEP=COMMAND, run before STEP')
    args = p.parse_args(argv)
    workflow = yaml.safe_load(Path(args.workflow).read_text())
    context = json.loads(Path(args.context).read_text())
    job = Job(args, context)
    try:
        result = job.run(workflow, args.job)
        rc = 0
    except ReplayError as exc:
        print(f'replay {args.job}: {exc}', file=sys.stderr)
        result = {'failed': str(exc), 'ran': job.ran, 'expressions': job.log}
        rc = 1
    result['runner_temp'] = str(job.temp)
    Path(args.out).write_text(json.dumps(result, indent=1, default=to_str))
    return rc


if __name__ == '__main__':
    sys.exit(main())
