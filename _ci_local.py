#!/usr/bin/env python3
"""Local equivalent of `.github/workflows/ci.yaml` (or validate.yaml, legacy) — parse the workflow
that runs in CI and execute each step in the local environment.

Modes:

  Default (no --path):
    Run from the repo root. Reads `.github/workflows/ci.yaml` (or validate.yaml legacy),
    classifies each step (EXEC, LOCAL, SKIP, UNKNOWN), and executes the
    EXEC/LOCAL steps in order, honoring `working-directory:` and `env:`.

  --path SUBDIR:
    Restrict validation to a subdirectory of the current repo. If the
    subdir contains its own `.github/workflows/ci.yaml` (or validate.yaml
    legacy), that workflow runs; otherwise falls back to autodetect mode (Go suite +
    hadolint + shellcheck + shfmt + gitleaks based on what's in SUBDIR).

  --plan-only:
    Print the plan without executing anything.

  --workflow PATH:
    Override the workflow file location (default
    `.github/workflows/ci.yaml` or validate.yaml legacy).

Step classification:

  EXEC      `run:` block — executed verbatim with bash -c.
  LOCAL     `uses:` action with a known local equivalent (e.g. gitleaks).
  SKIP      `actions/checkout`, `actions/setup-*`, `tj-actions/changed-files`,
            and any step whose name starts with "Install" or matches CI-only
            tool-bootstrap patterns. Local environments have these tools.
  UNKNOWN   `uses:` action we don't recognize. Surfaces as exit code 1
            unless --ignore-unknown is passed.

Special handling:

  - Thin-caller workflows (job-level `uses: cplieger/ci/.github/workflows/<X>.yaml@<ref>`)
    are resolved to the reusable workflow and its steps are executed locally.
  - Reusable workflows with auto-detect gates (`steps.<id>.outputs.<k>`)
    are evaluated by running the Resolve profile step locally.
  - `docker run --rm -i hadolint/hadolint:<ver> hadolint ... - < Dockerfile`
    — If `hadolint` is on PATH locally, the docker invocation is
    rewritten to call the binary directly. Avoids needing a running
    Docker daemon for a fast lint.
"""

import argparse
import atexit
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

try:
    import yaml
except ImportError:
    print('error: PyYAML required (pip install pyyaml)', file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Known `uses:` actions
# ---------------------------------------------------------------------------
# Tuple semantics:
#   ('SKIP',  reason)             — no-op locally, print reason
#   ('LOCAL', shell_command)      — run this shell command instead
KNOWN_USES = {
    'actions/checkout': ('SKIP', 'no-op (already in working tree)'),
    'actions/setup-go': ('SKIP', 'Go assumed installed locally'),
    'actions/setup-node': ('SKIP', 'Node assumed installed locally'),
    'actions/setup-python': ('SKIP', 'Python assumed installed locally'),
    'gitleaks/gitleaks-action': ('LOCAL', 'gitleaks detect --source . --no-banner'),
    'tj-actions/changed-files': ('SKIP', 'changed-files derivation skipped; full tree assumed'),
    'actions/dependency-review-action': (
        'NOLOCAL',
        'PR-only GitHub dependency graph API; no faithful local equivalent',
    ),
    'github/codeql-action/init': ('SKIP', 'handled by the local CodeQL orchestrator'),
    'github/codeql-action/autobuild': ('SKIP', 'handled by the local CodeQL orchestrator'),
    'github/codeql-action/analyze': ('SKIP', 'handled by the local CodeQL orchestrator'),
    'github/codeql-action/upload-sarif': ('SKIP', 'SARIF upload is GitHub-only'),
    'docker/setup-buildx-action': ('SKIP', 'buildx assumed available with local Docker'),
    # docker/build-push-action is handled by a special case in classify_step
    # (rewritten to a local `docker build`), so it has no entry here.
    'docker/login-action': ('SKIP', 'registry login not needed locally'),
    'docker/metadata-action': ('SKIP', 'metadata derivation skipped locally'),
    'actions/upload-artifact': ('SKIP', 'artifact upload skipped locally'),
    'actions/download-artifact': ('SKIP', 'artifact download skipped locally'),
    'anchore/sbom-action': ('LOCAL', 'syft . -o spdx-json=sbom.spdx.json'),
    'sigstore/cosign-installer': ('SKIP', 'cosign assumed installed locally'),
    'peter-evans/dockerhub-description': (
        'SKIP',
        'Docker Hub README sync; network-only, no local equivalent',
    ),
    'docker/setup-qemu-action': ('SKIP', 'QEMU cross-build setup; not needed for local testing'),
    'actions/create-github-app-token': (
        'SKIP',
        'GitHub App token generation; CI-only, no local equivalent',
    ),
    'peter-evans/create-pull-request': ('SKIP', 'PR creation; CI-only, no local equivalent'),
}

# Step name patterns indicating CI-only setup. Skipped locally.
INSTALL_NAME_PATTERNS = [
    r'^install\b',
    r'^setup\s+\w+',
    r'^download\s+go\s+dependencies\b',  # CI-only cache warmup
]


# ---------------------------------------------------------------------------
# Output helpers (color when stdout is a TTY)
# ---------------------------------------------------------------------------
USE_COLOR = sys.stdout.isatty() and os.environ.get('NO_COLOR') is None


def c(code, s):
    if not USE_COLOR:
        return s
    return f'\033[{code}m{s}\033[0m'


def green(s):
    return c('32', s)


def red(s):
    return c('31', s)


def yellow(s):
    return c('33', s)


def blue(s):
    return c('34', s)


def gray(s):
    return c('90', s)


# ---------------------------------------------------------------------------
# Agent-facing run report
# ---------------------------------------------------------------------------
# The (ok, counters, failed) plumbing drives the exit code; this module-level
# accumulator carries the richer, consolidated context the end-of-run summary
# needs so an agent reading the tail can see — without scrolling — what ran,
# what did NOT run locally (and why), and exactly what to fix.

# A real, fixable failure: a step that ran and exited non-zero.
class Failure(NamedTuple):
    job: str
    step: str
    rc: int
    cmd: str
    output: str


# One executed step's result. outcome ∈
# {pass, fail, missing, unavailable, skip, unknown, dry}.
# `missing` = a required local executable is absent; unlike an inherently
# GitHub-only check, that is a failed local validation because CI installs it.
# `unavailable` = the check cannot run faithfully on this host (for example an
# arm64 build without an arm64-capable buildx worker); it is reported explicitly
# under NOT VALIDATED LOCALLY rather than mislabeled as a CI-skipped surface.
class StepResult(NamedTuple):
    ok: bool
    status: str
    rc: int
    cmd: str
    output: str
    outcome: str


class CodeQLConfig(NamedTuple):
    queries: str
    languages: str


class RunReport:
    """Cross-workflow accumulator for the agent-facing summary."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.failures = []       # list[Failure] — ran and failed; fix these
        self.not_validated = []  # list[(job, step, reason)] — CI checks NOT exercised locally
        self.ran_jobs = []       # jobnames that executed >=1 real step locally
        self.skipped_jobs = []   # list[(job, reason)] — gated off / no local steps


REPORT = RunReport()


def _tail(text, n):
    """Last n non-empty-trimmed lines of text, for compact failure echoes."""
    lines = text.rstrip('\n').splitlines()
    return lines[-n:] if len(lines) > n else lines


def _first_cmd_line(cmd):
    """First meaningful line of a (possibly multi-line) run block — a compact
    reproduce hint. Skips shell-wrapper noise like a lone `(` so a soft-gate
    step `( tool ./... ) || {...}` shows `tool ./...`, not `(`."""
    lines = [ln.strip() for ln in cmd.strip().splitlines() if ln.strip()]
    for s in lines:
        if s not in ('(', ')', '{', '}'):
            return s
    return lines[0] if lines else ''


# Per-process path for the cross-step soft-gate marker.  Using a PID-unique
# path means parallel ci-local runs (e.g. running all repos concurrently) each
# maintain their own isolated failure list instead of sharing /tmp/_ci_failures
# and cross-contaminating each other's "Check results" steps.
_CI_FAILURES_PATH = f'/tmp/_ci_failures_{os.getpid()}'

# Per-step wall-clock cap (seconds). CI's own job timeout is 15-20 min; this is
# a local guard against a genuinely hung step. Sized to clear the slowest real
# step across the cplieger repos — wiregen's `go test -race ./...` runs ~317s standalone, so
# 300s produced a false timeout. 600s covers it with margin under light load.
_STEP_TIMEOUT_SECS = 600


def _clear_failure_marker():
    """Remove the cross-step soft-gate marker before a job runs.

    go-ci/shell-ci steps append failures to _CI_FAILURES_PATH and the job's
    'Check results' step reads it. In CI each job has its own /tmp; locally all
    jobs share one, so the marker MUST be cleared per-job — otherwise a real
    failure in one job (e.g. go's golangci-lint) makes a sibling job's Check
    results (e.g. shell) fail too, a phantom cascade CI never produces."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(_CI_FAILURES_PATH)


# ---------------------------------------------------------------------------
# Reusable workflow resolution
# ---------------------------------------------------------------------------
# Two `uses:` forms call a reusable workflow ci-local must expand:
#   1. `cplieger/ci/.github/workflows/<X>.yaml@<ref>` — a consumer's thin
#      ci.yaml calling the meta ci.yaml.
#   2. `./.github/workflows/<X>.yaml` — a LOCAL ref. The meta ci.yaml dispatches
#      to the per-surface sub-workflows (go-ci/ts-ci/shell-ci) this way rather
#      than pinning cplieger/ci@sha, so a sub-workflow fix reaches consumers on
#      their next meta-ci.yaml bump without a second internal-pin + tag cycle.
#      GitHub resolves a local ref against the same commit/repo the calling
#      workflow lives in — and these refs appear ONLY inside cplieger/ci
#      workflows, so locally they resolve against the sibling `ci/` checkout.
# BOTH must expand: if the local ./ form is left unresolved, the `go`/`shell`
# jobs collapse to zero steps and ci-local silently skips the ENTIRE Go and
# shell suites — vet, golangci-lint (the `govet: enable-all` fieldalignment
# authority), race tests, govulncheck, secret scan — with no error to flag it.
REUSABLE_RE = re.compile(r'^cplieger/ci/\.github/workflows/(.+\.ya?ml)@(.+)$')
LOCAL_REUSABLE_RE = re.compile(r'^\./(\.github/workflows/.+\.ya?ml)$')


def is_reusable_ref(uses_ref):
    """True if `uses_ref` is a reusable-workflow call ci-local can expand
    (the cplieger/ci@sha form or a local ./ ref)."""
    return bool(uses_ref) and bool(
        REUSABLE_RE.match(uses_ref) or LOCAL_REUSABLE_RE.match(uses_ref)
    )


def _ci_repo_root(target):
    """Locate the sibling cplieger/ci checkout (…/ci) next to the target repo.

    Walks up from target to the nearest git repo root, then takes its `ci`
    sibling. Both the cplieger/ci@sha form and local ./ refs (which appear only
    in ci-repo workflows) resolve against this checkout.
    """
    repo_root = target
    while repo_root != repo_root.parent:
        if (repo_root / '.git').exists():
            break
        repo_root = repo_root.parent
    return repo_root.parent / 'ci'


def resolve_reusable_workflow(uses_ref, target, parent_ref=None):
    """Resolve a reusable workflow `uses:` to its parsed YAML content.

    Handles both the `cplieger/ci/.github/workflows/<X>.yaml@<ref>` form and the
    local `./.github/workflows/<X>.yaml` form (resolved against the sibling ci/
    checkout, since local refs appear only in ci-repo workflows). `parent_ref`
    carries the calling workflow's pinned ref so the gh-api fallback can fetch a
    local ref at the same commit GitHub would.

    Lookup order:
      1. Sibling checkout: <repo-root>/../ci/<path>
      2. gh api fetch (timeout-wrapped)
      3. None (caller falls back to autodetect)

    Caveat: the sibling-checkout path resolves to the LOCAL `ci/` working tree,
    ignoring the pinned `@sha` in `uses:`. So ci-local validates against the
    current (possibly unreleased) workflow source, not the exact SHA a
    consumer's CI runs. Intended for developing the ci repo; a minor fidelity
    caveat for consumers whose pinned SHA lags `main`.
    """
    m = REUSABLE_RE.match(uses_ref)
    lm = LOCAL_REUSABLE_RE.match(uses_ref)
    if m:
        rel_path = f'.github/workflows/{m.group(1)}'
        ref = m.group(2)
    elif lm:
        rel_path = lm.group(1)
        ref = parent_ref  # local ref: fetch at the calling workflow's commit
    else:
        return None

    # 1. Sibling checkout (local ci repo).
    sibling = _ci_repo_root(target) / rel_path
    if sibling.is_file():
        with open(sibling) as f:
            return yaml.safe_load(f)

    # 2. gh api fetch (needs a ref; a local ref with no known parent_ref can't
    # be fetched, so it falls through to None -> autodetect).
    if ref and shutil.which('gh'):
        try:
            cmd = (
                f'timeout 15 gh api "repos/cplieger/ci/contents/{rel_path}'
                f'?ref={ref}" --jq .content | base64 -d'
            )
            proc = subprocess.run(
                ['bash', '-c', cmd],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return yaml.safe_load(proc.stdout)
        except (subprocess.TimeoutExpired, OSError):
            pass

    return None


# ---------------------------------------------------------------------------
# If-expression evaluator for reusable workflow step gates
# ---------------------------------------------------------------------------


def evaluate_step_if(expr, step_outputs, caller_inputs=None, workspace=None):
    """Evaluate a GitHub Actions if-expression to True/False.

    Supports:
      - steps.<id>.outputs.<k> == 'true'/'false'
      - github.event.repository.private == false  -> TRUE (local = public)
      - github.event_name == 'pull_request' -> TRUE
      - inputs.<k> -> from caller_inputs
      - && , ||, ! prefix, ${{ }} wrapping
      - always() -> TRUE
      - hashFiles(...) != '' -> resolved against the checkout root
        (empty string when no visible file matches, a hash otherwise)
    """
    if caller_inputs is None:
        caller_inputs = {}
    if workspace is None:
        workspace = Path.cwd()

    # Strip ${{ }} wrapper
    expr = expr.strip()
    if expr.startswith('${{') and expr.endswith('}}'):
        expr = expr[3:-2].strip()

    return _eval_expr(expr, step_outputs, caller_inputs, Path(workspace))


def _eval_expr(expr, step_outputs, caller_inputs, workspace):
    """Recursive expression evaluator."""
    expr = expr.strip()

    # Handle always()
    if expr == 'always()':
        return True

    # Handle || (lowest precedence)
    # Split on && and || respecting nesting
    parts = _split_logical(expr, '||')
    if len(parts) > 1:
        return any(_eval_expr(p, step_outputs, caller_inputs, workspace) for p in parts)

    parts = _split_logical(expr, '&&')
    if len(parts) > 1:
        return all(_eval_expr(p, step_outputs, caller_inputs, workspace) for p in parts)

    # Handle ! prefix
    if expr.startswith('!'):
        return not _eval_expr(expr[1:].strip(), step_outputs, caller_inputs, workspace)

    # Handle parentheses
    if expr.startswith('(') and expr.endswith(')'):
        return _eval_expr(expr[1:-1], step_outputs, caller_inputs, workspace)

    # Handle comparison: X == Y or X != Y
    for op in ('!=', '=='):
        idx = expr.find(op)
        if idx >= 0:
            lhs = _resolve_value(
                expr[:idx].strip(), step_outputs, caller_inputs, workspace
            )
            rhs = _resolve_value(
                expr[idx + len(op) :].strip(), step_outputs, caller_inputs, workspace
            )
            if op == '==':
                return str(lhs) == str(rhs)
            return str(lhs) != str(rhs)

    # Handle hashFiles(...) != '' pattern — already handled by comparison above
    # Bare expression: resolve to truthy
    val = _resolve_value(expr, step_outputs, caller_inputs, workspace)
    return bool(val) and str(val).lower() not in ('false', '0', '')


def _split_logical(expr, operator):
    """Split expression on a logical operator, respecting parentheses and quotes."""
    parts = []
    depth = 0
    in_quote = False
    current = []
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "'" and not in_quote:
            in_quote = True
        elif ch == "'" and in_quote:
            in_quote = False
        elif ch == '(' and not in_quote:
            depth += 1
        elif ch == ')' and not in_quote:
            depth -= 1
        elif not in_quote and depth == 0 and expr[i : i + len(operator)] == operator:
            parts.append(''.join(current))
            current = []
            i += len(operator)
            continue
        current.append(ch)
        i += 1
    parts.append(''.join(current))
    return parts if len(parts) > 1 else [expr]


def _resolve_value(tok, step_outputs, caller_inputs, workspace):
    """Resolve a tok to its value."""
    tok = tok.strip()

    # Strip ${{ }} wrapper
    if tok.startswith('${{') and tok.endswith('}}'):
        tok = tok[3:-2].strip()

    # String literal
    if (tok.startswith("'") and tok.endswith("'")) or (tok.startswith('"') and tok.endswith('"')):
        return tok[1:-1]

    # Boolean literals
    if tok == 'true':
        return 'true'
    if tok == 'false':
        return 'false'

    # steps.<id>.outputs.<key>
    m = re.match(r'steps\.(\w+)\.outputs\.(\w+)', tok)
    if m:
        step_id = m.group(1)
        key = m.group(2)
        return step_outputs.get(step_id, {}).get(key, '')

    # inputs.<key>
    m = re.match(r'inputs\.(\S+)', tok)
    if m:
        return str(caller_inputs.get(m.group(1), ''))

    # github.event.repository.private
    if tok == 'github.event.repository.private':
        return 'false'  # treat as public -> full run

    # github.event_name: local validation models the PR gate. The heavy battery
    # still runs fail-safe even without a base SHA, but PR-only checks are
    # surfaced instead of silently disappearing as they did under the former
    # synthetic workflow_dispatch event.
    if tok == 'github.event_name':
        return 'pull_request'

    # hashFiles(...) is always rooted at GITHUB_WORKSPACE, not the current
    # step working directory or the shell that launched ci-local.
    if tok.startswith('hashFiles(') and tok.endswith(')'):
        args = tok[len('hashFiles(') : -1]
        patterns = [a.strip().strip('\'"') for a in args.split(',') if a.strip()]
        for pat in patterns:
            if any(p.is_file() for p in workspace.glob(pat)):
                return 'somehash'
        return ''

    # always()
    if tok == 'always()':
        return 'true'

    return tok


# ---------------------------------------------------------------------------
# Run a "Resolve profile" step and capture its outputs
# ---------------------------------------------------------------------------
# GitHub Actions runner environment parity
# ---------------------------------------------------------------------------
# Workflow `run:` steps assume the standard runner-provided env vars exist.
# The most load-bearing locally is $RUNNER_TEMP — a guaranteed-writable scratch
# dir the markdown job (and others) write configs into. Steps execute under
# `bash -eu`, so a missing var aborts with "unbound variable" rather than
# failing the actual check. Synthesize the vars locally so ci-local mirrors CI.
_RUNNER_TEMP_DIR = None


def _runner_temp_dir() -> str:
    """Lazily create the stand-in for $RUNNER_TEMP and clean it up at exit.

    Real CI guarantees $RUNNER_TEMP is an existing, writable directory; mirror
    that here with a single per-run temp dir reused across all steps.
    """
    global _RUNNER_TEMP_DIR
    if _RUNNER_TEMP_DIR is None or not Path(_RUNNER_TEMP_DIR).is_dir():
        _RUNNER_TEMP_DIR = tempfile.mkdtemp(prefix='ci-local-runner-temp-')
        atexit.register(
            lambda d=_RUNNER_TEMP_DIR: shutil.rmtree(d, ignore_errors=True)
        )
    return _RUNNER_TEMP_DIR


def _restore_file_bytes(path: Path, data, existed: bool):
    """atexit restorer to keep ci-local side-effect-free on the working tree.

    Restores `path` to its pre-run bytes when it existed, or removes a file the
    run created. Best-effort; never raises (runs at interpreter shutdown).
    """
    try:
        if existed:
            path.write_bytes(data)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def apply_runner_env(env: dict, workspace: Path) -> dict:
    """Add the stable GitHub-hosted runner variables used by validation steps.

    `GITHUB_WORKSPACE` is always the checkout root. It must not follow a
    nested job's process cwd: the web job intentionally runs in `static-src/`
    while loading root-level lint configuration through GITHUB_WORKSPACE.
    """
    rt = _runner_temp_dir()
    env.setdefault('RUNNER_TEMP', rt)
    env.setdefault('RUNNER_OS', 'Linux')
    env.setdefault('RUNNER_ARCH', 'X64')
    env.setdefault('GITHUB_WORKSPACE', str(workspace))
    env.setdefault('GITHUB_EVENT_NAME', 'pull_request')
    env.setdefault('CI', 'true')
    # GitHub-runner file sinks. Steps routinely append to these
    # (`>> "$GITHUB_ENV"`, `>> "$GITHUB_STEP_SUMMARY"`, `>> "$GITHUB_OUTPUT"`);
    # under `bash -eu` an unset one is an "unbound variable" error that fails the
    # step — and for the detect profile step it silently discards the outputs it
    # already wrote (rc!=0 path). Point them at writable per-run temp files so the
    # redirects succeed. `setdefault` lets run_profile_step's explicit
    # GITHUB_OUTPUT win.
    for _sink in ('GITHUB_STEP_SUMMARY', 'GITHUB_ENV', 'GITHUB_PATH', 'GITHUB_OUTPUT'):
        env.setdefault(_sink, os.path.join(rt, _sink.lower()))
    return env


# ---------------------------------------------------------------------------


def _read_github_outputs(path):
    """Parse simple `name=value` records written to GITHUB_OUTPUT."""
    outputs = {}
    if not path or not Path(path).is_file():
        return outputs
    with open(path) as f:
        for line in f:
            line = line.strip()
            if '=' in line:
                key, value = line.split('=', 1)
                outputs[key] = value
    return outputs


def predict_profile_outputs(step, cwd, workspace):
    """Predict active profile-step outputs without running workflow code.

    `--plan-only` promises no workflow execution, but later `if:` expressions
    still need the outputs of the three profile shapes used by the CI suite.
    """
    name = step.get('name') or ''
    script = step.get('run') or ''
    if name == 'Detect repo surfaces':
        return compute_local_detect(workspace)
    if 'go list' in script and 'app=true' in script:
        for path in cwd.rglob('*.go'):
            rel = path.relative_to(cwd)
            if path.name.endswith('_test.go') or any(
                part in ('.git', 'vendor', 'node_modules', 'testdata') for part in rel.parts
            ):
                continue
            try:
                if re.search(r'(?m)^package\s+main\s*$', path.read_text(errors='ignore')):
                    return {'app': 'true'}
            except OSError:
                continue
        return {'app': 'false'}
    if '[ -f Dockerfile ]' in script and 'image=true' in script:
        return {'image': 'true' if (cwd / 'Dockerfile').is_file() else 'false'}
    return {}


def run_profile_step(step, cwd, workspace):
    """Execute an output-producing step and return (StepResult, outputs)."""
    run_script = step.get('run', '')
    if not run_script:
        return StepResult(False, red('FAIL'), 1, '', 'empty profile step', 'fail'), {}

    output_file = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tf:
            output_file = tf.name

        env = os.environ.copy()
        for key, value in (step.get('env') or {}).items():
            env[key] = str(value)
        apply_runner_env(env, workspace)
        env['GITHUB_OUTPUT'] = output_file

        proc = subprocess.run(
            [
                'timeout',
                '60',
                'bash',
                '--noprofile',
                '--norc',
                '-e',
                '-o',
                'pipefail',
                '-c',
                run_script,
            ],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
        )
        output = (proc.stdout or '') + (proc.stderr or '')
        if proc.returncode == 0:
            outputs = _read_github_outputs(output_file)
            # Execute detect so its shell validation still fails locally when CI
            # would fail, but report/use the same CI-visible inventory that job
            # expansion uses. Raw `find` sees ignored editor/review artifacts
            # which cannot exist in a fresh Actions checkout.
            if step.get('name') == 'Detect repo surfaces':
                outputs = compute_local_detect(workspace)
            return (
                StepResult(True, green('PASS'), 0, run_script, output, 'pass'),
                outputs,
            )
        outcome = 'missing' if proc.returncode == 127 else 'fail'
        status = yellow('MISSING (tool not on PATH)') if outcome == 'missing' else red(
            f'FAIL (rc={proc.returncode})'
        )
        return StepResult(False, status, proc.returncode, run_script, output, outcome), {}
    except OSError as exc:
        return StepResult(False, red('ERR'), 127, run_script, str(exc), 'missing'), {}
    finally:
        if output_file:
            with contextlib.suppress(OSError):
                os.unlink(output_file)


# ---------------------------------------------------------------------------
# Step classification
# ---------------------------------------------------------------------------


def _truthy_action_input(value):
    return str(value).strip().lower() in ('1', 'true', 'yes')


def trivy_action_command(step):
    """Translate the active trivy-action inputs to the equivalent CLI scan.

    SARIF upload is GitHub-only, so local output stays on stdout, but scan type,
    scanners, severity, unfixed policy, target, and advisory exit semantics all
    match the action invocation.
    """
    with_ = step.get('with') or {}
    image_ref = str(with_.get('image-ref', '')).strip()
    scan_type = str(with_.get('scan-type', '')).strip()
    if image_ref or scan_type in ('image', 'rootfs'):
        subcommand = scan_type or 'image'
        target = image_ref or str(with_.get('scan-ref', '.')).strip() or '.'
    else:
        subcommand = 'fs' if scan_type in ('', 'fs', 'filesystem') else scan_type
        target = str(with_.get('scan-ref', '.')).strip() or '.'

    args = ['trivy', subcommand]
    scanners = str(with_.get('scanners', '')).strip()
    severity = str(with_.get('severity', '')).strip()
    exit_code = str(with_.get('exit-code', '0')).strip() or '0'
    if scanners:
        args.extend(['--scanners', scanners])
    if severity:
        args.extend(['--severity', severity])
    if _truthy_action_input(with_.get('ignore-unfixed', False)):
        args.append('--ignore-unfixed')
    args.extend(['--exit-code', exit_code, target])
    return shlex.join(args)


def classify_step(step):
    """Return (kind, name, detail).

    kind: 'EXEC' | 'LOCAL' | 'NOLOCAL' | 'SKIP' | 'UNKNOWN'
    """
    name = step.get('name') or '(unnamed)'

    if 'uses' in step:
        action_ref = step['uses'].split('@', 1)[0].strip()
        if action_ref == 'aquasecurity/trivy-action':
            return 'LOCAL', name, trivy_action_command(step)
        # The required image gates use build-push-action. Native builds use the
        # daemon builder; the arm64 twin uses buildx with an explicit platform.
        # Platform availability is checked only when executing, so --plan-only
        # remains side-effect free and still shows the complete required plan.
        if action_ref == 'docker/build-push-action':
            with_ = step.get('with') or {}
            context = str(with_.get('context', '.')).strip() or '.'
            dockerfile = str(with_.get('file', 'Dockerfile')).strip() or 'Dockerfile'
            target_stage = str(with_.get('target', '')).strip()
            platform = str(step.get('__platform', '')).strip()
            if platform:
                cmd = f'docker buildx build --platform {shlex.quote(platform)} --load'
            else:
                cmd = 'docker build'
            cmd += f' -f {shlex.quote(dockerfile)}'
            if target_stage:
                cmd += f' --target {shlex.quote(target_stage)}'
            # Preserve tags so a downstream runtime smoke test can run the
            # image produced by the required native build.
            for tag in str(with_.get('tags', '')).replace(',', '\n').splitlines():
                tag = tag.strip()
                if tag:
                    cmd += f' -t {shlex.quote(tag)}'
            cmd += f' {shlex.quote(context)}'
            return 'LOCAL', name, cmd
        if action_ref in KNOWN_USES:
            kind, detail = KNOWN_USES[action_ref]
            return kind, name, detail
        return 'UNKNOWN', name, f'uses: {action_ref}'

    if 'run' in step:
        # GitHub-Actions job-result aggregators reference needs.<job>.result,
        # which only resolves in the Actions DAG. Locally the ${{ }} stays a
        # literal string and the guard always fails — SKIP like other GHA-only
        # steps instead of reporting a false failure.
        env_blob = ' '.join(str(v) for v in (step.get('env') or {}).values())
        if re.search(r'needs\.\w+\.result', step['run'] + ' ' + env_blob):
            return 'SKIP', name, 'GitHub-only job-result aggregation (needs.*.result)'
        # Project-scoped npm installs (npm install / npm ci / npm install --no-save)
        # MUST run locally for version parity with CI. CI does a fresh install
        # on every run from package.json's semver ranges; our cached
        # node_modules can drift from those ranges. Eslint rule sets, knip
        # rule sets, and TS-eslint plugin versions all silently change
        # between minor bumps, and parity-mode catches issues that local
        # cached deps would miss. Don't apply the Install-name SKIP rule
        # to these.
        run_line = step['run'].lstrip()
        if re.match(r'^(npm|yarn|pnpm)\s+(install|ci)\b', run_line):
            return 'EXEC', name, ''
        for pat in INSTALL_NAME_PATTERNS:
            if re.match(pat, name, re.IGNORECASE):
                return 'SKIP', name, 'CI-only install (tool expected installed locally)'
        return 'EXEC', name, ''

    return 'UNKNOWN', name, 'step has neither `run:` nor `uses:`'


# ---------------------------------------------------------------------------
# Hadolint docker-run rewrite
# ---------------------------------------------------------------------------
# CI invokes `docker run --rm -i hadolint/hadolint:<tag> hadolint <flags> - < Dockerfile`.
# Locally we have the hadolint binary; rewrite to call it directly.
HADOLINT_DOCKER_RE = re.compile(
    r'docker\s+run\s+--rm\s+-i\s+hadolint/hadolint(?::\S+)?\s+hadolint\b(.*?)-\s*<\s*(\S+)',
    re.DOTALL,
)


def rewrite_hadolint_docker(cmd: str) -> str:
    """If hadolint binary exists locally, rewrite docker invocation."""
    if not shutil.which('hadolint'):
        return cmd
    # Normalize bash line continuations (\<newline>...) so the regex can
    # span lines that CI workflows commonly break for readability.
    normalized = re.sub(r'\\\n\s*', ' ', cmd)
    return HADOLINT_DOCKER_RE.sub(r'hadolint \1\2', normalized)


# ---------------------------------------------------------------------------
# Gitleaks curl-download rewrite
# ---------------------------------------------------------------------------
# CI downloads gitleaks to the fixed /tmp/gitleaks path and runs it.  When
# multiple ci-local processes run in parallel every one of them tries to
# overwrite that binary simultaneously, which bash rejects with "Text file
# busy".  If gitleaks is already on PATH (installed via install-local-tools.sh)
# skip the download entirely and call the local binary directly.
_GITLEAKS_PATH_RE = re.compile(r'/tmp/gitleaks\b')


def rewrite_gitleaks_download(cmd: str) -> str:
    """If gitleaks is on PATH, replace the curl-download+run sequence.

    Strips the VERSION= line, the curl | tar extraction, and rewrites the
    /tmp/gitleaks invocation to just `gitleaks`.  Works even when the three
    lines are the entire step (the common case in go-ci / shell-ci).

    The `gitleaks dir .` command is preserved verbatim (NOT rewritten to
    `detect`). `dir` scans the working-tree filesystem exactly like CI.
    Rewriting to `detect --source .` would instead scan the full git history and
    flag already-removed/redacted historical secrets that CI's filesystem scan
    never sees (the wtk `routes_test.go` false positive, 2026-07).

    NOTE: `dir` does NOT honour .gitignore — it walks the raw filesystem, so a
    gitignored scratch path (.code-review/ report artifacts, *.dec decrypted
    secrets, node_modules) IS scanned and can raise local-only findings CI's
    fresh checkout never sees (the webhttp .code-review false positives, 2026-07).
    `rewrite_gitleaks_gitignore` (applied next) restores gitignore parity by
    allowlisting exactly what git ignores, mirroring `rewrite_trivy_gitignore`.
    """
    if not shutil.which('gitleaks') or '/tmp/gitleaks' not in cmd:
        return cmd
    lines = cmd.splitlines(keepends=True)
    result = []
    i = 0
    while i < len(lines):
        stripped = lines[i].rstrip()
        # Skip renovate comment + VERSION= line before a gitleaks curl line
        if (
            stripped.lstrip().startswith('# renovate')
            and i + 1 < len(lines)
            and 'VERSION=' in lines[i + 1]
        ):
            # Peek ahead: if the VERSION line is followed by a gitleaks curl, skip both
            if i + 2 < len(lines) and 'gitleaks' in lines[i + 2] and 'curl' in lines[i + 2]:
                i += 2  # skip comment and VERSION=
                continue
        if 'VERSION=' in stripped and i + 1 < len(lines):
            nxt = lines[i + 1].rstrip()
            if 'gitleaks' in nxt and 'curl' in nxt:
                i += 2  # skip VERSION= and curl line
                continue
        if 'curl' in stripped and 'gitleaks' in stripped and '/tmp' in stripped:
            i += 1  # skip curl download line
            continue
        # Rewrite /tmp/gitleaks to the PATH binary; keep `dir .` as-is so the local
        # scan matches CI (a working-tree filesystem scan). gitignore parity is
        # restored separately by rewrite_gitleaks_gitignore. Do NOT rewrite to
        # `detect --source .` — that scans full git history and flags already-removed
        # historical secrets CI's `dir` scan never sees.
        line = _GITLEAKS_PATH_RE.sub('gitleaks', lines[i])
        result.append(line)
        i += 1
    return ''.join(result)


# ---------------------------------------------------------------------------
# Gitleaks gitignore parity
# ---------------------------------------------------------------------------
# `gitleaks dir` walks the raw filesystem and does NOT honour .gitignore. CI runs
# it against a fresh checkout (git-tracked files only); locally the working tree
# also carries gitignored scratch CI never sees — .code-review/ report artifacts,
# a private repo's *.dec decrypted secrets, node_modules — and gitleaks happily
# scans them, raising findings the gate never would (the webhttp .code-review
# false positives, 2026-07). gitleaks has no --skip-dirs/--exclude flag (trivy
# does), so mirror the trivy parity fix through the one lever gitleaks offers: a
# generated config that extends the default ruleset (useDefault=true, so every
# default rule still fires on scanned files) and allowlists exactly the paths git
# ignores. Delivered inline via GITLEAKS_CONFIG_TOML so no temp file is needed.
# Allowlisting a gitignored path cannot hide a shipped secret — a secret in a
# gitignored file is not in the repo CI checks out.
_GITLEAKS_DIR_RE = re.compile(r'((?:\S+/)?gitleaks)\s+dir\b')
_GITLEAKS_CONFIG_FLAG_RE = re.compile(r'(?:^|\s)(?:-c|--config)(?:=|\s)')

# Relative --report-path values (gitleaks git/dir report writes). Absolute
# (/...) and variable ($...) paths are left alone.
_REPORT_PATH_RE = re.compile(r'--report-path[ =](["\']?)(?![/$])([^\s"\']+)\1')


def rewrite_report_artifacts(cmd: str) -> str:
    """Redirect relative report-file writes into $RUNNER_TEMP.

    In CI these report files (security-scan's `--report-path
    gitleaks-history.sarif`) land in a throwaway checkout and feed an
    upload-sarif/upload-artifact step that ci-local SKIPs. Locally the same
    write would land in the real working tree — and a leftover
    gitleaks-history.sarif then false-fails every later `gitleaks dir`
    working-tree scan (the SARIF quotes historical secret matches; observed
    on envx and docker-keepalived). $RUNNER_TEMP always exists locally
    (apply_runner_env) and is cleaned up at exit.
    """
    return _REPORT_PATH_RE.sub(
        lambda m: f'--report-path "${{RUNNER_TEMP}}/{m.group(2)}"', cmd
    )


def rewrite_gitleaks_gitignore(cmd: str, cwd: Path) -> str:
    """Allowlist gitignored paths in a `gitleaks dir` run so it matches CI's fileset.

    CI scans a fresh checkout (tracked files only); locally the working tree
    carries gitignored scratch gitleaks would otherwise flag. Skips a command
    that already pins its own config (-c/--config, or a repo .gitleaks.toml) so
    an explicit configuration is never overridden.
    """
    if not _GITLEAKS_DIR_RE.search(cmd) or not shutil.which('gitleaks') or not shutil.which('git'):
        return cmd
    # Never override an explicit config (flag or repo-local .gitleaks.toml).
    if _GITLEAKS_CONFIG_FLAG_RE.search(cmd) or (cwd / '.gitleaks.toml').is_file():
        return cmd
    try:
        out = subprocess.run(
            ['git', 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory'],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return cmd
    if out.returncode != 0:
        return cmd
    regexes = []
    for entry in out.stdout.splitlines():
        entry = entry.strip()
        if not entry:
            continue
        if entry.endswith('/'):
            regexes.append('^' + re.escape(entry))  # directory: everything under it
        else:
            regexes.append('^' + re.escape(entry) + '$')  # exact file
    if not regexes:
        return cmd
    paths = ', '.join(json.dumps(rx) for rx in regexes)
    config = (
        '[extend]\n'
        'useDefault = true\n\n'
        '[[allowlists]]\n'
        'description = "ci-local: skip git-ignored paths (gitignore parity)"\n'
        f'paths = [{paths}]\n'
    )
    env_assign = 'GITLEAKS_CONFIG_TOML=' + shlex.quote(config) + ' '
    return _GITLEAKS_DIR_RE.sub(lambda m: env_assign + m.group(0), cmd, count=1)


# ---------------------------------------------------------------------------
# Per-process /tmp/_ci_failures rewrite
# ---------------------------------------------------------------------------
# go-ci and shell-ci workflows hardcode /tmp/_ci_failures as the soft-gate
# marker file.  Replace it in every command string with the per-process path so
# parallel ci-local invocations don't cross-contaminate each other's results.
def rewrite_ci_failures_path(cmd: str) -> str:
    """Replace the hardcoded /tmp/_ci_failures with the per-process path."""
    return cmd.replace('/tmp/_ci_failures', _CI_FAILURES_PATH)


# ---------------------------------------------------------------------------
# Trivy filesystem-scan gitignore parity
# ---------------------------------------------------------------------------
# CI runs `trivy fs` against a fresh checkout — only git-tracked files exist.
# Locally the working tree carries gitignored files trivy will happily scan:
# decrypted secrets (a private repo's *.env.dec), .code-review/ artifacts, node_modules.
# Trivy's secret scanner then flags e.g. apps/<app>/.env.dec (a deliberately
# decrypted, gitignored secret) and the run fails with a finding CI never sees.
# Mirror CI by injecting --skip-files / --skip-dirs for everything git ignores
# under the scan dir. `git ls-files --directory` collapses fully-ignored dirs to
# a single entry, keeping the flag list compact.
_TRIVY_FS_RE = re.compile(r'\btrivy\s+(?:fs|filesystem)\b')


def rewrite_trivy_gitignore(cmd: str, cwd: Path) -> str:
    """Inject --skip-files/--skip-dirs for gitignored paths into a trivy fs run.

    Makes the local filesystem scan see the same fileset CI does (tracked only),
    so a gitignored decrypted secret or scratch artifact can't produce a finding
    that the gate never would.
    """
    if not _TRIVY_FS_RE.search(cmd) or not shutil.which('git'):
        return cmd
    try:
        out = subprocess.run(
            ['git', 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory'],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return cmd
    if out.returncode != 0:
        return cmd
    dirs, files = [], []
    for entry in out.stdout.splitlines():
        entry = entry.strip()
        if not entry:
            continue
        (dirs if entry.endswith('/') else files).append(entry.rstrip('/'))
    if not dirs and not files:
        return cmd
    inject = ''
    if dirs:
        inject += f" --skip-dirs {shlex.quote(','.join(dirs))}"
    if files:
        inject += f" --skip-files {shlex.quote(','.join(files))}"
    # Insert right after the `trivy fs` / `trivy filesystem` token. Repeated
    # --skip-dirs is fine (trivy unions them), so an existing --skip-dirs in the
    # command is preserved.
    return _TRIVY_FS_RE.sub(lambda m: m.group(0) + inject, cmd, count=1)
# ---------------------------------------------------------------------------
# CI lints a fresh checkout — only git-tracked files exist. Locally the working
# tree may carry gitignored .md files (generated reports under .code-review/,
# scratch notes, vendored docs) that CI never sees, producing false failures.
# Rewrite the `**/*.md` recursive glob to the explicit git-tracked .md list so
# ci-local lints exactly the fileset CI's checkout would.
MARKDOWNLINT_GLOB_RE = re.compile(r'(["\']?)\*\*/\*\.md\1')


def rewrite_markdownlint_gitignore(cmd: str, cwd: Path) -> str:
    """Replace the markdownlint-cli2 `**/*.md` glob with git-tracked .md files."""
    if 'markdownlint-cli2' not in cmd or not MARKDOWNLINT_GLOB_RE.search(cmd):
        return cmd
    try:
        out = subprocess.run(
            [
                'git',
                '-C',
                str(cwd),
                'ls-files',
                '--cached',
                '--others',
                '--exclude-standard',
                '--',
                '*.md',
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return cmd  # not a git repo or git unavailable; leave the glob as-is
    files = [shlex.quote(f) for f in out.split('\n') if f.strip()]
    if not files:
        return cmd
    return MARKDOWNLINT_GLOB_RE.sub(' '.join(files), cmd)


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------


_DOCKER_PLATFORM_CACHE = {}


def docker_platform_available(platform):
    """Return whether the active buildx worker can execute `platform`."""
    cached = _DOCKER_PLATFORM_CACHE.get(platform)
    if cached is not None:
        return cached
    if not shutil.which('docker'):
        _DOCKER_PLATFORM_CACHE[platform] = False
        return False
    try:
        proc = subprocess.run(
            ['docker', 'buildx', 'inspect', '--bootstrap'],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        _DOCKER_PLATFORM_CACHE[platform] = False
        return False
    supported = False
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if line.strip().startswith('Platforms:'):
                platforms = {item.strip() for item in line.split(':', 1)[1].split(',')}
                supported = platform in platforms or any(
                    item.startswith(f'{platform}/') for item in platforms
                )
                break
    _DOCKER_PLATFORM_CACHE[platform] = supported
    return supported


def run_step(kind, name, detail, step, base_cwd: Path, dry_run: bool):
    """Execute one translated workflow step and return its local result."""
    wd = step.get('working-directory')
    cwd = base_cwd / wd if wd else base_cwd
    env = os.environ.copy()
    for key, value in (step.get('env') or {}).items():
        env[key] = str(value)
    apply_runner_env(env, base_cwd)

    if kind == 'SKIP':
        return StepResult(True, gray('SKIP'), 0, '', '', 'skip')
    if kind == 'NOLOCAL':
        return StepResult(True, yellow('NOT AVAILABLE LOCALLY'), 0, '', detail, 'unavailable')
    if kind == 'UNKNOWN':
        return StepResult(False, red('UNKNOWN'), 0, '', detail, 'unknown')

    cmd = detail if kind == 'LOCAL' else step['run']
    # Local action translations and literal run blocks share the same parity
    # rewrites. Previously Trivy was translated to LOCAL after the rewrite path
    # and therefore scanned gitignored files that do not exist in CI.
    cmd = rewrite_hadolint_docker(cmd)
    cmd = rewrite_markdownlint_gitignore(cmd, cwd)
    cmd = rewrite_gitleaks_download(cmd)
    cmd = rewrite_gitleaks_gitignore(cmd, cwd)
    cmd = rewrite_trivy_gitignore(cmd, cwd)
    cmd = rewrite_report_artifacts(cmd)
    cmd = rewrite_ci_failures_path(cmd)

    if dry_run:
        return StepResult(True, blue('DRY'), 0, cmd, '', 'dry')

    platform_match = re.search(r'\bdocker\s+buildx\s+build\s+--platform\s+(\S+)', cmd)
    if platform_match and not docker_platform_available(platform_match.group(1)):
        platform = platform_match.group(1)
        reason = f'buildx worker does not advertise {platform}; CI runs this on a native runner'
        return StepResult(True, yellow('NOT AVAILABLE LOCALLY'), 0, cmd, reason, 'unavailable')

    try:
        proc = subprocess.run(
            [
                'timeout',
                str(_STEP_TIMEOUT_SECS),
                'bash',
                '--noprofile',
                '--norc',
                '-e',
                '-o',
                'pipefail',
                '-c',
                cmd,
            ],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        return StepResult(False, red('ERR'), 127, cmd, str(exc), 'missing')

    output = (proc.stdout or '') + (proc.stderr or '')
    rc = proc.returncode
    if rc == 0:
        return StepResult(True, green('PASS'), 0, cmd, output, 'pass')

    body = _tail(output, 40)
    for line in body:
        print(f'      {line}')
    if len(output.rstrip('\n').splitlines()) > 40:
        print(f'      {gray("... (output trimmed; full tail in summary)")}')

    missing = rc == 127 or bool(
        re.search(r'(?:command not found|failed to run command)', output, re.IGNORECASE)
    )
    if missing:
        return StepResult(
            False, yellow('MISSING (required tool not on PATH)'), rc, cmd, output, 'missing'
        )
    if rc == 124:
        return StepResult(
            False,
            red(f'FAIL (timed out at {_STEP_TIMEOUT_SECS}s)'),
            124,
            cmd,
            output,
            'fail',
        )
    return StepResult(False, red(f'FAIL (rc={rc})'), rc, cmd, output, 'fail')


# ---------------------------------------------------------------------------
# Autodetect fallback (no validate.yaml in target dir)
# ---------------------------------------------------------------------------


def autodetect_steps(target: Path):
    """Build a synthetic step list when no workflow is available.

    Mirrors the common patterns across the user's repos: Go suite if go.mod
    is present, hadolint if Dockerfile present, shellcheck + shfmt for any
    *.sh (nested included), gitleaks always.
    """
    steps = []

    has_gomod = (target / 'go.mod').is_file()
    has_dockerfile = (target / 'Dockerfile').is_file()
    # Every *.sh in the tree (nested included), matching shell-ci.yaml and the
    # meta ci.yaml `scripts` job — not just root-level scripts.
    sh_files = [
        p
        for p in target.rglob('*.sh')
        if not any(part in ('.git', 'node_modules') for part in p.relative_to(target).parts)
    ]

    if has_gomod:
        steps.extend(
            [
                {'name': 'Verify dependencies', 'run': 'go mod verify'},
                {'name': 'Vet', 'run': 'go vet ./...'},
                {'name': 'Lint', 'run': 'golangci-lint run --timeout=5m ./...'},
                {'name': 'Test', 'run': 'go test -race -count=1 ./...'},
            ]
        )

    if has_dockerfile:
        steps.append(
            {
                'name': 'Validate Dockerfile',
                'run': 'hadolint --ignore DL3018 Dockerfile',
            }
        )

    if sh_files:
        # find-based discovery + flags identical to shell-ci.yaml: shellcheck
        # (-x -S info) and shfmt (2-space, indent switch cases, binary-next-line).
        steps.append(
            {
                'name': 'Lint shell scripts (shellcheck)',
                'run': (
                    "files=$(find . -name '*.sh' -not -path './.git/*')\n"
                    'if [ -n "$files" ]; then\n'
                    '  # shellcheck disable=SC2086\n'
                    '  shellcheck -x -S info $files\n'
                    'fi'
                ),
            }
        )
        steps.append(
            {
                'name': 'Format check shell scripts (shfmt)',
                'run': (
                    "files=$(find . -name '*.sh' -not -path './.git/*')\n"
                    'if [ -n "$files" ]; then\n'
                    '  # shellcheck disable=SC2086\n'
                    '  shfmt -d -i 2 -ci -bn $files\n'
                    'fi'
                ),
            }
        )

    steps.append(
        {
            'name': 'Scan for secrets',
            'uses': 'gitleaks/gitleaks-action@local',  # triggers KNOWN_USES path
        }
    )

    return steps


# ---------------------------------------------------------------------------
# Workflow loading
# ---------------------------------------------------------------------------


def load_workflow(path: Path):
    """Return list of (jobname, [steps]) or None if file doesn't exist."""
    if not path.is_file():
        return None
    with open(path) as f:
        wf = yaml.safe_load(f)
    jobs = wf.get('jobs') or {}
    return [(jobname, job.get('steps') or []) for jobname, job in jobs.items()]


def load_workflow_raw(path: Path):
    """Return raw parsed YAML dict or None."""
    if not path.is_file():
        return None
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Reusable workflow expansion — resolve thin callers to actual steps
# ---------------------------------------------------------------------------


WEB_DIR_PROBE = ('static-src', 'web', 'internal/server/static-src')


def detect_web_dir(target):
    """Mirror the meta CI frontend probe order using CI-visible files."""
    for directory in WEB_DIR_PROBE:
        path = target / directory
        if path.is_dir() and _has_tracked_file(
            target, f'{directory}/package.json', f'{directory}/jsr.json'
        ):
            return directory
    return None


def _has_file_anywhere(target, pattern):
    """True if any working-tree file matches `pattern` under target (ignoring
    .git). Non-git fallback for surface detection."""
    for p in target.rglob(pattern):
        rel = p.relative_to(target)
        if rel.parts and rel.parts[0] == '.git':
            continue
        return True
    return False


def _git_visible_files(target, *pathspecs):
    """Return tracked and nonignored untracked files matching pathspecs.

    A local run should include files about to be committed while excluding
    ignored scratch/build output that cannot exist in CI's checkout.
    """
    output = subprocess.run(
        [
            'git',
            '-C',
            str(target),
            'ls-files',
            '--cached',
            '--others',
            '--exclude-standard',
            '-z',
            '--',
            *pathspecs,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        path
        for path in output.split('\x00')
        if path and (target / path).is_file()
    ]


def _has_tracked_file(target, *pathspecs):
    """True when CI-visible inventory contains a matching file.

    Despite the historical name, this includes nonignored untracked files so a
    contributor validates a newly added surface before committing it. Ignored
    files remain excluded. Falls back to a working-tree walk outside git repos.
    """
    try:
        return any(path.strip() for path in _git_visible_files(target, *pathspecs))
    except (OSError, subprocess.CalledProcessError):
        for pathspec in pathspecs:
            if '/' in pathspec:
                if any(target.glob(pathspec)):
                    return True
            elif _has_file_anywhere(target, pathspec):
                return True
        return False


def _has_root_file(target, filename):
    """True when a root file is tracked or nonignored and untracked."""
    try:
        return filename in _git_visible_files(
            target, f':(top,literal){filename}'
        )
    except (OSError, subprocess.CalledProcessError):
        return (target / filename).is_file()


def _detect_go_nested_dirs(target):
    """Mirror the meta ci.yaml detect job's nested-Go-module discovery: every
    CI-visible `<dir>/go.mod` below the root, pruned of vendored/generated
    trees, and only when the dir has CI-visible .go files (a sentinel go.mod —
    e.g. `module web-ignore` — has nothing to validate). Keep the filters in
    sync with the meta detect step and scripts/test-lane-semantics.sh. Returns a
    sorted list of dirs; empty when git is unavailable."""
    prune = re.compile(r'(^|/)(node_modules|vendor|testdata|static|dist)/')
    try:
        files = _git_visible_files(target, '*/go.mod')
    except (OSError, subprocess.CalledProcessError):
        return []
    dirs = []
    for filename in files:
        if not filename.strip() or prune.search(filename):
            continue
        directory = os.path.dirname(filename)
        try:
            has_go = any(
                path.strip()
                for path in _git_visible_files(target, f'{directory}/*.go')
            )
        except (OSError, subprocess.CalledProcessError):
            has_go = False
        if has_go:
            dirs.append(directory)
    return sorted(dirs)


_DETECT_CACHE = {}


def compute_local_detect(target):
    """Reproduce the meta ci.yaml `detect` job's surface outputs from local file
    presence. ci-local always runs the applicable surfaces (it mirrors CI's
    fail-safe "treat as code change" path; there is no docs-only skip locally).
    Surface probes use tracked plus nonignored untracked files: newly added work
    is validated before commit, while ignored scratch output cannot flip a lane.
    Memoized per target because detection is used by gates and matrix expansion.
    """
    cached = _DETECT_CACHE.get(str(target))
    if cached is not None:
        return cached
    web = detect_web_dir(target)
    has_dockerfile = _has_root_file(target, 'Dockerfile')
    has_gomod = _has_root_file(target, 'go.mod')
    has_jsr = _has_root_file(target, 'jsr.json')

    # Python: any tracked *.py → ruff job, same as the meta ci.yaml find.
    run_python = _has_tracked_file(target, '*.py')

    # Scripts: pure tooling/docs repos ONLY (no go.mod/jsr.json/Dockerfile —
    # Go/TS/Docker repos lint shell + workflows via their own jobs). Fires on any
    # tracked *.sh or workflow file. Mirrors the meta ci.yaml gate exactly so
    # ci-local doesn't run the scripts job on a Go/Docker repo where CI skips it.
    run_scripts = False
    if not has_dockerfile and not has_gomod and not has_jsr:
        run_scripts = _has_tracked_file(
            target, '*.sh', '.github/workflows/*.yml', '.github/workflows/*.yaml'
        )

    go_nested_dirs = _detect_go_nested_dirs(target)

    det = {
        'run_go': 'true' if has_gomod else 'false',
        'run_go_nested': 'true' if go_nested_dirs else 'false',
        # JSON string, matching the CI output shape (consumed via
        # `fromJSON(needs.detect.outputs.go_nested_dirs)` in the matrix).
        'go_nested_dirs': json.dumps(go_nested_dirs),
        'run_ts': 'true' if has_jsr else 'false',
        'run_web': 'true' if web else 'false',
        'run_shell': 'true' if has_dockerfile else 'false',
        'run_docker': 'true' if has_dockerfile else 'false',
        'run_python': 'true' if run_python else 'false',
        'run_scripts': 'true' if run_scripts else 'false',
        'web_dir': web or '',
    }
    _DETECT_CACHE[str(target)] = det
    return det


def job_applies_locally(jobname, target):
    """Gate an expanded (recursed) job by local surface detection, mirroring the
    meta ci.yaml job-level `if: needs.detect.outputs.run_X`. The surface is the
    first path segment matching a known gated surface (go/ts/web/shell/docker/
    python/scripts); markdown and the detect/validate scaffolding have no gate and
    always run, like CI. Position-independent so it works whether the meta is
    reached via a consumer (jobnames like `ci/go/validate`) or run directly on the
    ci repo, where the meta is the top-level workflow (`go/validate`)."""
    det = compute_local_detect(target)
    gate_map = {
        'go': det['run_go'],
        # Nested-module lanes: normally expanded per-dir by the matrix logic
        # in _expand_job (zero instances when no nested modules exist), so
        # this entry only fires on the unexpanded-fallback path (an
        # unresolvable matrix). Exact segment match: 'go-nested' != 'go'.
        'go-nested': det['run_go_nested'],
        'ts': det['run_ts'],
        'web': det['run_web'],
        'shell': det['run_shell'],
        'docker': det['run_docker'],
        # The public arm64 gate is required whenever Docker is detected. It is
        # planned locally and executed through buildx when the active worker
        # advertises arm64; otherwise the summary reports it as NOT VALIDATED.
        'docker-arm64': det['run_docker'],
        'python': det['run_python'],
        'scripts': det['run_scripts'],
    }
    for seg in jobname.split('/'):
        if seg in gate_map:
            return gate_map[seg] == 'true'
    return True  # markdown / detect / validate scaffolding — always runs


def _resolve_with_value(value, inputs, target, strip_unknown=False, env=None):
    """Resolve the GitHub expressions used by the active validation workflows.

    Handles workflow-call inputs, detect-job outputs, workflow/job environment
    values, and the small GitHub event context needed by the meta detect step.
    Unknown expressions can be stripped before a run block reaches bash, just as
    the Actions runner substitutes them before invoking the shell.
    """
    if not isinstance(value, str):
        return value
    env = env or {}

    def repl(m):
        inner = m.group(1).strip()
        im = re.match(r'inputs\.([\w-]+)$', inner)
        if im:
            return str(inputs.get(im.group(1), ''))
        nm = re.match(r'needs\.[\w-]+\.outputs\.([\w-]+)$', inner)
        if nm:
            return str(compute_local_detect(target).get(nm.group(1), ''))
        em = re.match(r'env\.([\w-]+)$', inner)
        if em:
            return str(env.get(em.group(1), ''))
        if inner == 'github.event_name':
            return 'pull_request'
        if inner == 'github.event.repository.private':
            return 'false'
        # No PR base is available locally. Leaving it empty deliberately sends
        # the meta detect script down its fail-safe full-battery path.
        if inner in ('github.event.pull_request.base.sha', 'github.event.before'):
            return ''
        if inner == 'github.sha':
            return ''
        return '' if strip_unknown else m.group(0)

    return re.sub(r'\$\{\{\s*(.+?)\s*\}\}', repl, value)


def _expand_job(jobname, job, caller_inputs, target, depth=0, parent_ref=None):
    """Expand one job into terminal (jobname, steps, working_dir, caller_inputs)
    tuples, recursing through nested reusable-workflow callers (consumer ci.yaml
    -> meta ci.yaml -> go-ci/ts-ci/shell-ci/...). `caller_inputs` is None only
    for a plain inline job that never came from a reusable workflow. `parent_ref`
    is the calling workflow's pinned ref, threaded so a nested local `./` ref can
    be fetched at the same commit when no sibling ci/ checkout exists."""
    # Single-axis strategy.matrix (the meta go-nested job: `dir:
    # ${{ fromJSON(needs.detect.outputs.go_nested_dirs) }}`): expand one job
    # instance per value, substituting `${{ matrix.<axis> }}` textually in the
    # job's `with:` values — mirroring what CI's matrix does before the
    # reusable workflow ever sees the inputs. Zero values = zero instances
    # (the job simply doesn't run, matching CI's run_go_nested gate).
    # Multi-axis or unresolvable matrices fall through unexpanded; the
    # job_applies_locally gate then decides (fail-safe, current behavior).
    matrix = (job.get('strategy') or {}).get('matrix') if isinstance(job, dict) else None
    if isinstance(matrix, dict) and len(matrix) == 1 and '__matrix_expanded' not in job:
        axis, spec = next(iter(matrix.items()))
        values = _resolve_matrix_values(spec, target)
        if values is not None:
            out = []
            for val in values:
                inst = dict(job)
                inst.pop('strategy', None)
                inst['__matrix_expanded'] = True
                if isinstance(job.get('with'), dict):
                    inst['with'] = {
                        k: v.replace(f'${{{{ matrix.{axis} }}}}', val) if isinstance(v, str) else v
                        for k, v in job['with'].items()
                    }
                # Keep the instance name a single path segment (a dir value
                # can contain '/', which would fabricate surface segments for
                # job_applies_locally — e.g. a module dir named 'docker').
                safe = val.replace('/', '~')
                out.extend(
                    _expand_job(
                        f'{jobname}[{safe}]', inst, caller_inputs, target, depth, parent_ref
                    )
                )
            return out

    uses = job.get('uses', '')
    if is_reusable_ref(uses) and depth < 6:
        resolved = resolve_reusable_workflow(uses, target, parent_ref=parent_ref)
        if resolved is None:
            print(
                f'  {yellow("WARN")} could not resolve reusable workflow: {uses}; '
                f'falling back to autodetect for job "{jobname}"',
                file=sys.stderr,
            )
            return [(jobname, autodetect_steps(target), '.', None)]
        # A cplieger/ci@<ref> caller sets the ref for its whole subtree; a nested
        # local ./ ref inherits it (it lives in the same repo at the same commit).
        m = REUSABLE_RE.match(uses)
        child_ref = m.group(2) if m else parent_ref
        # Seed inputs with the reusable's declared workflow_call defaults (PyYAML
        # parses a bare `on:` key as boolean True), then let the caller's `with:`
        # override them. This gives steps concrete input values (e.g. the
        # security scan's `file: ${{ inputs.dockerfile }}` -> ./Dockerfile).
        on_block = resolved.get('on')
        if on_block is None:
            on_block = resolved.get(True)
        wc_inputs = ((on_block or {}).get('workflow_call') or {}).get('inputs') or {}
        merged = {}
        for iname, ispec in wc_inputs.items():
            if isinstance(ispec, dict) and 'default' in ispec:
                merged[iname] = ispec['default']
        for k, v in (job.get('with') or {}).items():
            merged[k] = _resolve_with_value(v, caller_inputs or {}, target)
        out = []
        for rjob_name, rjob in (resolved.get('jobs') or {}).items():
            out.extend(
                _expand_job(
                    f'{jobname}/{rjob_name}', rjob, merged, target, depth + 1, child_ref
                )
            )
        return out

    # Terminal: a job with inline steps.
    steps = job.get('steps') or []
    run_defaults = (job.get('defaults') or {}).get('run') or {}
    wd = _resolve_with_value(run_defaults.get('working-directory', '.'), caller_inputs or {}, target)
    return [(jobname, steps, wd or '.', caller_inputs)]


def expand_reusable_jobs(jobs_dict, target):
    """Expand reusable-workflow callers to (jobname, steps, working_dir,
    caller_inputs) tuples, recursing through nested callers. A consumer's thin
    `ci` job -> the meta ci.yaml -> the per-surface go-ci/ts-ci/web/shell-ci
    workflows, whose steps are what actually gate. caller_inputs is None only for
    plain inline jobs (autodetect-fallback / non-reusable)."""
    result = []
    for jobname, job in jobs_dict.items():
        result.extend(_expand_job(jobname, job, None, target, 0))
    return result


def _resolve_matrix_values(spec, target):
    """Resolve a single matrix axis to a list of string values, or None when it
    cannot be resolved locally. Handles a literal YAML list and the
    `${{ fromJSON(needs.<job>.outputs.<key>) }}` shape (resolved from
    compute_local_detect, which stores JSON strings for list outputs)."""
    if isinstance(spec, list):
        return [str(v) for v in spec]
    if isinstance(spec, str):
        m = re.fullmatch(
            r'\s*\$\{\{\s*fromJSON\(\s*needs\.\w+\.outputs\.([\w-]+)\s*\)\s*\}\}\s*', spec
        )
        if m:
            raw = compute_local_detect(target).get(m.group(1), '[]')
            try:
                vals = json.loads(raw)
            except (TypeError, ValueError):
                return None
            if isinstance(vals, list):
                return [str(v) for v in vals]
    return None


def _resolve_input_expr(expr, inputs):
    """Resolve ${{ inputs.X }} in a string using caller inputs."""

    def replacer(m):
        inner = m.group(1).strip()
        im = re.match(r'inputs\.([\w-]+)', inner)
        if im:
            return str(inputs.get(im.group(1), '.'))
        return m.group(0)

    return re.sub(r'\$\{\{\s*(.+?)\s*\}\}', replacer, str(expr))


# ---------------------------------------------------------------------------
# Process reusable workflow steps with if-evaluation
# ---------------------------------------------------------------------------


def process_reusable_steps(
    jobname, steps, target, working_dir, caller_inputs, dry_run, ignore_unknown
):
    """Process one terminal job from a resolved reusable workflow."""
    counters = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'UNKNOWN': 0, 'MISSING': 0}
    failed_steps = []
    overall_ok = True
    step_outputs = {}
    hard_failed = False
    recorded_soft_failure = False
    base_cwd = target / working_dir if working_dir != '.' else target

    if not dry_run:
        _clear_failure_marker()

    for step in steps:
        step_id = step.get('id', '')
        name = step.get('name') or '(unnamed)'
        if_expr = str(step.get('if', '') or '')

        # GitHub implicitly applies success() to ordinary later steps after a
        # hard failure. Explicit always() cleanup/aggregation still runs.
        if hard_failed and 'always()' not in if_expr:
            print(f'  {gray("SKIP"):<7} {name}  (previous hard step failed)')
            counters['SKIP'] += 1
            continue

        step_wd = step.get('working-directory', '')
        if step_wd:
            step_wd = _resolve_input_expr(step_wd, caller_inputs)

        eff_step = dict(step)
        if 'docker-arm64' in jobname.split('/'):
            eff_step['__platform'] = 'linux/arm64'
        if isinstance(step.get('with'), dict):
            eff_step['with'] = {
                key: _resolve_with_value(value, caller_inputs, target)
                for key, value in step['with'].items()
            }
        if isinstance(step.get('env'), dict):
            eff_step['env'] = {
                key: _resolve_with_value(
                    value, caller_inputs or {}, target, strip_unknown=True
                )
                for key, value in step['env'].items()
            }
        if isinstance(step.get('run'), str):
            eff_step['run'] = _resolve_with_value(
                step['run'], caller_inputs, target, strip_unknown=True
            )
        if step_wd and step_wd != '.':
            eff_step['working-directory'] = step_wd
        elif 'working-directory' not in step and working_dir != '.':
            eff_step['working-directory'] = working_dir
        elif 'working-directory' in eff_step:
            eff_step['working-directory'] = step_wd or None

        if if_expr and not evaluate_step_if(if_expr, step_outputs, caller_inputs, target):
            print(f'  {gray("SKIP"):<7} {name}  (if: false)')
            counters['SKIP'] += 1
            continue

        # Output-producing profile steps are real CI checks. Execute and surface
        # their failures normally; in plan mode predict only the active profile
        # shapes so no workflow command runs.
        if step_id and 'run' in eff_step and 'GITHUB_OUTPUT' in eff_step.get('run', ''):
            run_cwd = target / step_wd if step_wd and step_wd != '.' else base_cwd
            if dry_run:
                outputs = predict_profile_outputs(eff_step, run_cwd, target)
                step_outputs[step_id] = outputs
                out_str = ', '.join(f'{key}={value}' for key, value in outputs.items())
                print(f'  {blue("DRY"):<7} {name}  ({out_str})')
                counters['DRY'] += 1
                continue
            res, outputs = run_profile_step(eff_step, run_cwd, target)
            step_outputs[step_id] = outputs
            out_str = ', '.join(f'{key}={value}' for key, value in outputs.items())
            suffix = f'  ({out_str})' if out_str else ''
            print(f'  {blue("EXEC"):<7} {name}{suffix}')
            print(f'    → {res.status}')
            if res.outcome == 'pass':
                counters['PASS'] += 1
            else:
                counters['FAIL'] += 1
                overall_ok = False
                hard_failed = True
                failed_steps.append(Failure(jobname, name, res.rc, res.cmd, res.output))
            continue

        step_blob = ' '.join(
            [str(value) for value in (step.get('env') or {}).values()]
            + [str(step.get('run', ''))]
        )
        if re.search(r'\$\{\{\s*needs\.[\w-]+\.result\s*\}\}', step_blob):
            print(
                f'  {gray("SKIP"):<7} {name}  '
                "(aggregates CI job results; ci-local's summary covers this)"
            )
            counters['SKIP'] += 1
            continue

        kind, _step_name, detail = classify_step(eff_step)
        wd = eff_step.get('working-directory')
        wd_str = f' [cwd={wd}]' if wd else ''
        tag = {
            'EXEC': blue('EXEC'),
            'LOCAL': blue('LOCAL'),
            'NOLOCAL': yellow('NOLOCAL'),
            'SKIP': gray('SKIP'),
            'UNKNOWN': red('UNKNOWN'),
        }.get(kind, kind)
        tail = f'  ({detail})' if detail else ''
        print(f'  {tag:<7} {name}{wd_str}{tail}')

        if kind == 'SKIP':
            counters['SKIP'] += 1
            continue
        if kind == 'NOLOCAL':
            counters['MISSING'] += 1
            REPORT.not_validated.append((jobname, name, detail))
            continue
        if kind == 'UNKNOWN':
            counters['UNKNOWN'] += 1
            REPORT.not_validated.append(
                (jobname, name, f'unrecognized action — not run locally ({detail})')
            )
            if not ignore_unknown:
                overall_ok = False
                hard_failed = True
                failed_steps.append(Failure(jobname, name, 0, '', detail))
            continue

        res = run_step(kind, name, detail, eff_step, target, dry_run)
        if res.outcome == 'dry':
            counters['DRY'] += 1
            continue
        print(f'    → {res.status}')
        if res.outcome == 'pass':
            counters['PASS'] += 1
            continue
        if res.outcome == 'unavailable':
            counters['MISSING'] += 1
            REPORT.not_validated.append((jobname, name, res.output))
            continue

        soft_gate = step.get('continue-on-error') in (True, 'true')
        records_failure = '/tmp/_ci_failures' in str(step.get('run', ''))
        if res.outcome == 'missing':
            counters['FAIL'] += 1
            overall_ok = False
            failed_steps.append(Failure(jobname, name, res.rc, res.cmd, res.output))
            if not soft_gate:
                hard_failed = True
            else:
                recorded_soft_failure = True
            continue
        if soft_gate and records_failure:
            counters['FAIL'] += 1
            overall_ok = False
            recorded_soft_failure = True
            failed_steps.append(Failure(jobname, name, res.rc, res.cmd, res.output))
            continue
        if soft_gate:
            counters['MISSING'] += 1
            REPORT.not_validated.append(
                (jobname, name, f'advisory step failed (non-blocking in CI) — rc={res.rc}')
            )
            continue

        # The final Check results failure only aggregates already-recorded
        # Option-A failures; keep the underlying checks in the summary instead
        # of adding a duplicate aggregate failure.
        overall_ok = False
        if name == 'Check results' and recorded_soft_failure:
            continue
        counters['FAIL'] += 1
        hard_failed = True
        failed_steps.append(Failure(jobname, name, res.rc, res.cmd, res.output))

    return overall_ok, counters, failed_steps


# ---------------------------------------------------------------------------
# CodeQL workflow handling
# ---------------------------------------------------------------------------
# CodeQL workflows are structured as init -> autobuild -> analyze and don't
# translate cleanly step-by-step; we re-orchestrate them as one local
# `codeql database create` + `codeql database analyze` invocation per job.


def is_codeql_workflow(jobs_raw):
    """Return True if this is a CodeQL workflow (has codeql-action/init or reusable codeql).

    Accepts either:
      - list of (jobname, steps) tuples (old format)
      - raw jobs dict from YAML
    """
    if isinstance(jobs_raw, dict):
        # Check for reusable codeql.yaml call
        for job in jobs_raw.values():
            uses = job.get('uses', '')
            if 'codeql.yaml' in uses or 'codeql.yml' in uses:
                return True
            for s in job.get('steps') or []:
                uses = s.get('uses', '')
                if uses.split('@', 1)[0] == 'github/codeql-action/init':
                    return True
        return False
    # List of (jobname, steps) tuples
    for _jobname, steps in jobs_raw:
        for s in steps:
            uses = s.get('uses', '')
            if uses.split('@', 1)[0] == 'github/codeql-action/init':
                return True
    return False


def is_codeql_reusable(jobs_dict):
    """Return True if any job calls the reusable codeql.yaml."""
    for job in jobs_dict.values():
        uses = job.get('uses', '')
        if 'codeql.yaml' in uses or 'codeql.yml' in uses:
            return True
    return False


def _workflow_call_input_defaults(raw):
    on_block = raw.get('on')
    if on_block is None:
        on_block = raw.get(True)
    specs = ((on_block or {}).get('workflow_call') or {}).get('inputs') or {}
    return {
        name: spec.get('default', '')
        for name, spec in specs.items()
        if isinstance(spec, dict)
    }


def codeql_config_for_workflow(raw, jobs_dict, target):
    """Resolve the language/query inputs for the active CodeQL workflow."""
    for job in jobs_dict.values():
        uses = job.get('uses', '')
        if 'codeql.yaml' not in uses and 'codeql.yml' not in uses:
            continue
        resolved = resolve_reusable_workflow(uses, target)
        if resolved is None:
            return CodeQLConfig('security-extended,security-and-quality', '')
        values = _workflow_call_input_defaults(resolved)
        values.update(job.get('with') or {})
        return CodeQLConfig(
            str(values.get('queries', 'security-extended,security-and-quality')),
            str(values.get('languages', '')),
        )

    # Direct processing of the reusable workflow (e.g. --workflow
    # .github/workflows/codeql.yaml) uses its own workflow_call defaults.
    if any(
        (step.get('uses') or '').split('@', 1)[0] == 'github/codeql-action/init'
        for job in jobs_dict.values()
        for step in (job.get('steps') or [])
    ):
        values = _workflow_call_input_defaults(raw)
        if values:
            return CodeQLConfig(
                str(values.get('queries', 'security-extended,security-and-quality')),
                str(values.get('languages', '')),
            )
    return None


def parse_sarif_alerts(sarif_path: Path):
    """Extract findings from a SARIF v2.1.0 file. Returns list of dicts."""
    with open(sarif_path) as f:
        data = json.load(f)
    alerts = []
    for run in data.get('runs', []):
        # Build a rule_id -> security-severity map from the driver's rules.
        rule_meta = {}
        driver = (run.get('tool') or {}).get('driver') or {}
        for r in driver.get('rules') or []:
            rid = r.get('id')
            props = r.get('properties') or {}
            rule_meta[rid] = {
                'severity': props.get('security-severity', '') or props.get('problem.severity', ''),
                'tags': props.get('tags') or [],
            }
        for result in run.get('results') or []:
            rule_id = result.get('ruleId') or (result.get('rule') or {}).get('id') or '?'
            level = result.get('level', 'warning')
            meta = rule_meta.get(rule_id, {})
            fp = (result.get('partialFingerprints') or {}).get('primaryLocationLineHash', '')
            for loc in result.get('locations') or []:
                phys = loc.get('physicalLocation') or {}
                uri = (phys.get('artifactLocation') or {}).get('uri', '?')
                line = (phys.get('region') or {}).get('startLine', 0)
                alerts.append(
                    {
                        'rule': rule_id,
                        'level': level,
                        'severity': meta.get('severity', ''),
                        'tags': meta.get('tags', []),
                        'path': uri,
                        'line': line,
                        'fingerprint': fp,
                    }
                )
    return alerts


def is_security_alert(alert) -> bool:
    """Return whether a SARIF result is a medium-or-higher security finding."""
    tags = alert.get('tags') or []
    if not any(tag == 'security' or tag.startswith('security') for tag in tags):
        return False
    severity = alert.get('severity', '')
    try:
        return float(severity) >= 4.0
    except ValueError:
        return alert.get('level') in ('error', 'warning')


def run_codeql_for_job(jobname, steps, target: Path, dry_run: bool):
    """Run codeql for a single job. Returns (ok, counters_dict, failed_names)."""
    counters = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0}
    failed = []

    languages = None
    queries = None
    extra_steps = []

    for s in steps:
        uses_full = s.get('uses', '')
        action_ref = uses_full.split('@', 1)[0] if uses_full else ''
        if action_ref == 'github/codeql-action/init':
            w = s.get('with') or {}
            languages = (w.get('languages') or '').strip() or 'go'
            queries = (w.get('queries') or '').strip()
        elif action_ref in (
            'github/codeql-action/autobuild',
            'github/codeql-action/analyze',
            'github/codeql-action/upload-sarif',
        ):
            pass
        else:
            extra_steps.append(s)

    # Replay non-codeql steps via the regular classifier
    for s in extra_steps:
        kind, name, detail = classify_step(s)
        tag = {
            'EXEC': blue('EXEC'),
            'LOCAL': blue('LOCAL'),
            'SKIP': gray('SKIP'),
            'UNKNOWN': red('UNKNOWN'),
        }.get(kind, kind)
        tail = f'  ({detail})' if detail else ''
        print(f'  {tag:<7} {name}{tail}')
        if kind == 'SKIP':
            counters['SKIP'] += 1
            continue
        if kind == 'UNKNOWN':
            counters['SKIP'] += 1
            continue
        res = run_step(kind, name, detail, s, target, dry_run)
        if dry_run or res.outcome == 'dry':
            counters['DRY'] += 1
        elif res.ok:
            counters['PASS'] += 1
            print(f'    → {res.status}')
        else:
            counters['FAIL'] += 1
            failed.append(name)
            print(f'    → {res.status}')

    if languages is None:
        return True, counters, failed

    ok, sub = run_codeql_analysis(target, languages, queries, dry_run)
    for k, v in sub.items():
        if k in counters:
            counters[k] += v
    if not ok:
        failed.append(sub.get('label', 'CodeQL'))
    return ok, counters, failed


def run_codeql_analysis(target: Path, languages, queries, dry_run, source_root=None):
    """Build and analyze one CodeQL database with Analyze-action exit semantics."""
    src = source_root or target
    info = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'label': 'CodeQL'}

    print(
        f'  {blue("CODEQL")} analyze (lang={languages}, queries={queries or "default"}, root={src})'
    )

    if dry_run:
        info['DRY'] += 1
        return True, info

    if not shutil.which('codeql'):
        print(
            f'    → {gray("SKIP")} codeql binary not on PATH; analysis runs in '
            f'CI (install it locally, or pass --no-codeql to skip explicitly)'
        )
        info['SKIP'] += 1
        info['label'] = 'CodeQL (skipped — binary not installed)'
        REPORT.not_validated.append(
            ('codeql', f'analyze ({languages})', 'codeql binary not on PATH — runs in CI')
        )
        return True, info

    db_dir = Path('/tmp') / f'codeql-db-{os.getpid()}-{abs(hash(str(src))) % 100000}'
    sarif_out = Path('/tmp') / f'codeql-results-{os.getpid()}-{abs(hash(str(src))) % 100000}.sarif'

    create_cmd = (
        f'timeout 300 codeql database create {db_dir} '
        f'--language={languages} --source-root={src} --overwrite'
    )
    suites = []
    # The action-facing language id is javascript-typescript; installed query
    # suites retain the CodeQL CLI's `javascript-*` filename prefix.
    suite_language = {
        'javascript-typescript': 'javascript',
    }.get(languages, languages)
    if queries:
        for query in queries.split(','):
            query = query.strip()
            if not query:
                continue
            if '/' in query or query.endswith(('.qls', '.ql')):
                suites.append(query)
            else:
                suites.append(f'{suite_language}-{query}.qls')
    suites_arg = ' '.join(suites)
    analyze_cmd = (
        f'timeout 300 codeql database analyze {db_dir} {suites_arg} '
        f'--format=sarif-latest --output={sarif_out}'
    )

    try:
        proc = subprocess.run(
            ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', create_cmd],
            cwd=str(src),
        )
        if proc.returncode != 0:
            info['FAIL'] += 1
            info['label'] = 'CodeQL database create'
            print(f'    → {red(f"FAIL (rc={proc.returncode})")} (database create)')
            return False, info

        proc = subprocess.run(
            ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', analyze_cmd],
            cwd=str(src),
        )
        if proc.returncode != 0:
            info['FAIL'] += 1
            info['label'] = 'CodeQL analyze'
            print(f'    → {red(f"FAIL (rc={proc.returncode})")} (analyze)')
            return False, info

        alerts = parse_sarif_alerts(sarif_out)
        sec_alerts = [alert for alert in alerts if is_security_alert(alert)]
        if sec_alerts:
            # github/codeql-action/analyze uploads findings but does not fail the
            # Analyze job merely because SARIF contains an alert. Preserve that
            # exit contract locally while still surfacing the findings.
            print(f'    {yellow(f"{len(sec_alerts)} security finding(s) (advisory)")}')
            for alert in sec_alerts[:20]:
                severity = (
                    f'sev={alert["severity"]}'
                    if alert['severity']
                    else f'level={alert["level"]}'
                )
                print(
                    f'      {yellow(severity)} | {alert["rule"]} | '
                    f'{alert["path"]}:{alert["line"]}'
                )
            if len(sec_alerts) > 20:
                print(f'      ... and {len(sec_alerts) - 20} more')

        info['PASS'] += 1
        nonsecurity = len(alerts) - len(sec_alerts)
        details = []
        if sec_alerts:
            details.append(f'{len(sec_alerts)} security advisory')
        if nonsecurity:
            details.append(f'{nonsecurity} non-security result')
        suffix = f' ({", ".join(details)})' if details else ' (no alerts)'
        print(f'    → {green("PASS")}{suffix}')
        return True, info
    finally:
        if db_dir.exists():
            shutil.rmtree(db_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            sarif_out.unlink()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def discover_workflows(target: Path):
    """Return list of workflow Paths in priority order, or [] if none.

    Picks at most one CI workflow (preferring ci.yaml > validate.yaml legacy),
    one security workflow, and one CodeQL workflow.
    """
    workflow_dir = target / '.github' / 'workflows'
    if not workflow_dir.is_dir():
        return []
    found = []
    # In cplieger/ci itself, prefer the active self-caller so local expansion
    # exercises the same wrapper GitHub invokes. Consumer repos have only
    # ci.yaml, so their normal discovery is unchanged.
    for name in ('self-ci.yaml', 'self-ci.yml', 'ci.yaml', 'ci.yml', 'validate.yaml', 'validate.yml'):
        p = workflow_dir / name
        if p.is_file():
            found.append(p)
            break
    # Security workflow.
    for name in ('security.yml', 'security.yaml'):
        p = workflow_dir / name
        if p.is_file():
            found.append(p)
            break
    # CodeQL workflow; the ci repo's self caller is the active entry point.
    for name in ('self-codeql.yml', 'self-codeql.yaml', 'codeql.yml', 'codeql.yaml'):
        p = workflow_dir / name
        if p.is_file():
            found.append(p)
            break
    return found


def find_go_modules(target: Path):
    """Return sorted list of dirs containing a go.mod under target."""
    skip = {'node_modules', 'vendor', 'testdata', '.git'}
    mods = []
    for gomod in target.rglob('go.mod'):
        if any(part in skip for part in gomod.relative_to(target).parts):
            continue
        mods.append(gomod.parent)
    return sorted(mods)


def has_python_sources(target: Path):
    """Return True if target contains .py files outside common excluded dirs."""
    skip = {'node_modules', 'vendor', '.git', '.venv', 'venv', '__pycache__', '.ruff_cache'}
    for py in target.rglob('*.py'):
        if any(part in skip for part in py.relative_to(target).parts):
            continue
        return True
    return False


def has_js_ts_sources(target: Path):
    """Return True if target contains JS/TS sources outside excluded dirs."""
    skip = {'node_modules', 'vendor', '.git', 'dist', 'build', 'out', '.next', 'coverage'}
    for ext in ('*.ts', '*.tsx', '*.js', '*.jsx', '*.mjs', '*.cjs'):
        for path in target.rglob(ext):
            if any(part in skip for part in path.relative_to(target).parts):
                continue
            return True
    return False


def has_go_sources(target: Path):
    skip = {'node_modules', 'vendor', 'testdata', '.git'}
    return any(
        not any(part in skip for part in path.relative_to(target).parts)
        for path in target.rglob('*.go')
    )


def has_ruby_sources(target: Path):
    skip = {'node_modules', 'vendor', '.git', 'dist', 'coverage'}
    return any(
        not any(part in skip for part in path.relative_to(target).parts)
        for path in target.rglob('*.rb')
    )


def has_actions_sources(target: Path):
    workflow_dir = target / '.github' / 'workflows'
    return workflow_dir.is_dir() and any(
        path.is_file() for pattern in ('*.yml', '*.yaml') for path in workflow_dir.glob(pattern)
    )


def detect_codeql_languages(target, explicit=''):
    """Mirror codeql.yaml's language map, including the Actions pack."""
    if explicit.strip():
        return sorted({item.strip() for item in explicit.split(',') if item.strip()})
    languages = []
    if has_go_sources(target):
        languages.append('go')
    if has_js_ts_sources(target):
        languages.append('javascript-typescript')
    if has_python_sources(target):
        languages.append('python')
    if has_ruby_sources(target):
        languages.append('ruby')
    # The reusable workflow unconditionally unions ["actions"] into the API's
    # detected languages. Keep that matrix leg even for a workflow-only repo.
    languages.append('actions')
    return sorted(set(languages))


def clean_stale_node_modules(target: Path):
    """Remove gitignored node_modules/ directories under target.

    Returns the number of directories removed.
    """
    removed = 0
    for nm in target.rglob('node_modules'):
        if not nm.is_dir():
            continue
        rel = nm.relative_to(target)
        if any(part == '.git' for part in rel.parts):
            continue
        if any(part == 'node_modules' for part in rel.parts[:-1]):
            continue
        try:
            r = subprocess.run(
                ['git', 'check-ignore', '--quiet', str(rel)],
                cwd=str(target),
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            return removed
        if r.returncode != 0:
            continue
        shutil.rmtree(nm, ignore_errors=True)
        removed += 1
    return removed


def run_workflow_steps(jobs, target: Path, dry_run: bool, ignore_unknown: bool):
    """Run plain/synthetic jobs through the same terminal-job engine."""
    totals = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'UNKNOWN': 0, 'MISSING': 0}
    failures = []
    overall_ok = True
    for jobname, steps in jobs:
        print(f'--- job: {jobname} ---')
        ok, counters, failed = process_reusable_steps(
            jobname, steps, target, '.', {}, dry_run, ignore_unknown
        )
        overall_ok = overall_ok and ok
        for key, value in counters.items():
            totals[key] = totals.get(key, 0) + value
        failures.extend(failed)
        print()
    return overall_ok, totals, failures


def process_workflow_file(wf_path, target, dry_run, ignore_unknown, no_codeql):
    """Process a single workflow file, handling reusable workflow expansion.

    Returns (ok, counters, failed_steps).
    """
    grand_counters = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'UNKNOWN': 0, 'MISSING': 0}
    grand_failed = []
    grand_ok = True

    raw = load_workflow_raw(wf_path)
    if raw is None:
        print(f'error: workflow not found at {wf_path}', file=sys.stderr)
        return False, grand_counters, [f'workflow not found: {wf_path}']

    jobs_dict = raw.get('jobs') or {}

    # CodeQL's detect + matrix + action lifecycle is orchestrated locally as
    # one CLI analysis per detected language. Preserve the reusable inputs, but
    # do not try to execute matrix expressions as literal shell values.
    if is_codeql_workflow(jobs_dict):
        if no_codeql:
            print(f'  {gray("SKIP")} (--no-codeql)')
            return True, grand_counters, []
        config = codeql_config_for_workflow(raw, jobs_dict, target)
        if config is not None:
            return config, grand_counters, []

        # Legacy static CodeQL workflows still use the direct-step handler.
        jobs = [(job_name, job.get('steps') or []) for job_name, job in jobs_dict.items()]
        for jobname, steps in jobs:
            print(f'--- job: {jobname} (codeql) ---')
            ok, counters, failed = run_codeql_for_job(jobname, steps, target, dry_run)
            if not ok:
                grand_ok = False
            for key, value in counters.items():
                grand_counters[key] = grand_counters.get(key, 0) + value
            grand_failed.extend(failed)
            print()
        return grand_ok, grand_counters, grand_failed

    # Check for reusable workflow calls and expand them
    has_reusable = any(is_reusable_ref(job.get('uses', '')) for job in jobs_dict.values())

    if has_reusable:
        expanded = expand_reusable_jobs(jobs_dict, target)
        for jobname, steps, working_dir, caller_inputs in expanded:
            print(f'--- job: {jobname} ---')
            if not job_applies_locally(jobname, target):
                print(f'  {gray("SKIP")} (surface not present locally)')
                REPORT.skipped_jobs.append((jobname, 'surface not present locally'))
                print()
                continue
            if not steps:
                print(f'  {gray("SKIP")} (no steps to run)')
                REPORT.skipped_jobs.append((jobname, 'no steps to run'))
                print()
                continue
            ok, counters, failed = process_reusable_steps(
                jobname,
                steps,
                target,
                working_dir,
                caller_inputs or {},
                dry_run,
                ignore_unknown,
            )
            REPORT.ran_jobs.append(jobname)
            if not ok:
                grand_ok = False
            for k, v in counters.items():
                grand_counters[k] = grand_counters.get(k, 0) + v
            grand_failed.extend(failed)
            print()
    else:
        # Plain workflow with inline steps
        jobs = [(jn, j.get('steps') or []) for jn, j in jobs_dict.items()]
        ok, counters, failed = run_workflow_steps(jobs, target, dry_run, ignore_unknown)
        if not ok:
            grand_ok = False
        for k, v in counters.items():
            grand_counters[k] = grand_counters.get(k, 0) + v
        grand_failed.extend(failed)

    return grand_ok, grand_counters, grand_failed


def print_run_summary(counters, failures, ok, plan_only):
    """Print the consolidated, agent-facing end-of-run summary.

    Three sections an agent can act on without scrolling the full log:
      FAILED               — steps that ran and exited non-zero (what to fix).
      NOT VALIDATED LOCALLY — CI checks with no local result (codeql, a tool not
                             on PATH, an unrecognized action): a local PASS does
                             NOT cover these.
      skipped              — surfaces absent from this repo (CI skips them too).
    """
    print('=== summary ===')
    parts = [
        f'{green("passed")} {counters.get("PASS", 0)}',
        f'{red("failed")} {counters.get("FAIL", 0)}',
        f'skipped {counters.get("SKIP", 0)}',
    ]
    if REPORT.not_validated:
        parts.append(f'{yellow("not-validated")} {len(REPORT.not_validated)}')
    if counters.get('UNKNOWN'):
        parts.append(f'unknown {counters["UNKNOWN"]}')
    if plan_only and counters.get('DRY'):
        parts.append(f'planned {counters["DRY"]}')
    print('  ' + '   '.join(parts))

    if failures:
        print(f'\n{red("FAILED")} (ran and exited non-zero — fix these):')
        for i, item in enumerate(failures, 1):
            if isinstance(item, Failure):
                print(f'  [{i}] {item.job} › {item.step}  (rc={item.rc})')
                if item.cmd:
                    cmd_line = _first_cmd_line(item.cmd)
                    if cmd_line:
                        print(f'      cmd: {cmd_line}')
                tail = _tail(item.output, 12)
                if tail:
                    print('      out:')
                    for line in tail:
                        print(f'        {line}')
            else:
                print(f'  [{i}] {item}')

    if REPORT.not_validated:
        print(f'\n{yellow("NOT VALIDATED LOCALLY")} (CI runs these; no local result):')
        for job, step, reason in REPORT.not_validated:
            print(f'  - {job} › {step}: {reason}')

    skipped_surfaces = [j for j, r in REPORT.skipped_jobs if r == 'surface not present locally']
    if skipped_surfaces:
        print(f'\n{gray("skipped (surface not present in this repo — CI skips them too):")}')
        print('  ' + ', '.join(skipped_surfaces))

    print()
    if plan_only:
        print(f'{blue("RESULT: plan only")} (no execution)')
        return
    if ok:
        line = green('RESULT: PASS')
        notes = []
        if REPORT.not_validated:
            notes.append(f'{len(REPORT.not_validated)} check(s) not validated locally')
        if notes:
            line += f' — {"; ".join(notes)} (see above)'
        print(line)
    else:
        print(f'{red("RESULT: FAIL")} — {len(failures)} step(s) failed (see FAILED above)')


def main():
    # Ensure print() output flushes promptly
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        # Python < 3.7 has no TextIOWrapper.reconfigure; prompt flushing is
        # best-effort, so fall through without it.
        pass

    REPORT.reset()

    ap = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        '--path',
        metavar='SUBDIR',
        help='Restrict validation to a subdirectory of the current repo.',
    )
    ap.add_argument(
        '--workflow',
        metavar='PATH',
        action='append',
        help=(
            'Workflow file path; repeat for multiple workflows. '
            'Default: auto-discover ci.yaml (or validate.yaml legacy), '
            'security.yml, and codeql.yml in <target>/.github/workflows/.'
        ),
    )
    ap.add_argument(
        '--no-codeql',
        action='store_true',
        help='Skip CodeQL workflow even if codeql.yml is present (saves ~3min).',
    )
    ap.add_argument(
        '--no-clean-node-modules',
        action='store_true',
        help=(
            'Skip the pre-run cleanup of stale node_modules/ directories. '
            'By default ci-local removes gitignored node_modules/ before '
            'running steps when the workflow has npm install, mirroring '
            "CI's fresh-checkout state and avoiding cross-run "
            'contamination of Go-tooling steps that descend into node_modules.'
        ),
    )
    ap.add_argument(
        '--plan-only',
        action='store_true',
        help='Print the plan without executing.',
    )
    ap.add_argument(
        '--ignore-unknown',
        action='store_true',
        help='Treat UNKNOWN steps as warnings instead of failures.',
    )
    args = ap.parse_args()

    cwd = Path.cwd()
    target = (cwd / args.path).resolve() if args.path else cwd

    if not target.is_dir():
        print(f'error: target not a directory: {target}', file=sys.stderr)
        sys.exit(2)

    # Resolve workflow list
    workflow_paths = []
    if args.workflow:
        for w in args.workflow:
            p = Path(w)
            if not p.is_absolute():
                p = cwd / p
            workflow_paths.append(p)
    else:
        workflow_paths = discover_workflows(target)
        if args.no_codeql:
            workflow_paths = [p for p in workflow_paths if 'codeql' not in p.name]

    print(f'{blue("target:")}   {target.relative_to(cwd) if target != cwd else "."}')
    if args.plan_only:
        print(f'{yellow("--plan-only:")} no execution')

    # Clear failure marker
    if not args.plan_only:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(_CI_FAILURES_PATH)

    # Pre-run node_modules cleanup
    if not args.plan_only and not args.no_clean_node_modules:
        will_install_npm = False
        for wf_path in workflow_paths:
            raw = load_workflow_raw(wf_path)
            if not raw or is_codeql_workflow(raw.get('jobs') or {}):
                continue
            expanded = expand_reusable_jobs(raw.get('jobs') or {}, target)
            if any(
                re.search(
                    r'(?m)^\s*(npm|yarn|pnpm)\s+(install|ci)\b',
                    step.get('run') or '',
                )
                for _job, steps, _wd, _inputs in expanded
                for step in steps
            ):
                will_install_npm = True
                break
        if will_install_npm:
            removed = clean_stale_node_modules(target)
            if removed:
                print(
                    f'{yellow("pre-run cleanup:")} removed {removed} stale '
                    f'node_modules/ dir(s) (mirrors CI fresh-checkout state; '
                    f'workflow re-installs them)'
                )

    # Keep ci-local side-effect-free on the working tree (mirrors the
    # node_modules cleanup above). Two CI steps mutate tracked files and would
    # otherwise leave a local diff: the security scan does
    # `printf ... > .trivyignore` (trivy auto-loads it from the workdir), and the
    # Go wiregen-drift step does `git add -A` (staging the whole tree). Snapshot
    # both and restore at exit via atexit, so a timeout, exception, or Ctrl-C
    # between here and the end still restores them.
    if not args.plan_only:
        ti_path = target / '.trivyignore'
        ti_existed = ti_path.exists()
        ti_saved = ti_path.read_bytes() if ti_existed else None
        atexit.register(_restore_file_bytes, ti_path, ti_saved, ti_existed)

        gi = subprocess.run(
            ['git', '-C', str(target), 'rev-parse', '--absolute-git-dir'],
            capture_output=True,
            text=True,
        )
        if gi.returncode == 0:
            index_path = Path(gi.stdout.strip()) / 'index'
            if index_path.is_file():
                atexit.register(_restore_file_bytes, index_path, index_path.read_bytes(), True)

    grand_counters = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'UNKNOWN': 0, 'MISSING': 0}
    grand_failed = []
    grand_ok = True
    codeql_config = None

    if not workflow_paths:
        # Autodetect synthetic workflow
        jobs = [('autodetect', autodetect_steps(target))]
        print(f'{yellow("autodetect mode")} — no ci/validate/codeql workflow in {target}')
        print()
        ok, counters, failed = run_workflow_steps(jobs, target, args.plan_only, args.ignore_unknown)
        grand_ok = ok
        for k, v in counters.items():
            grand_counters[k] += v
        grand_failed.extend(failed)
    else:
        for wf_path in workflow_paths:
            rel = wf_path.relative_to(cwd) if wf_path.is_relative_to(cwd) else wf_path
            print()
            print(f'{blue("workflow:")} {rel}')
            print()

            result, counters, failed = process_workflow_file(
                wf_path, target, args.plan_only, args.ignore_unknown, args.no_codeql
            )

            if isinstance(result, CodeQLConfig):
                codeql_config = result
                continue

            if not result:
                grand_ok = False
            for key, value in counters.items():
                grand_counters[key] = grand_counters.get(key, 0) + value
            grand_failed.extend(failed)

    # CodeQL runs only when the repository's discovered workflow calls it. The
    # previous unconditional fallback analyzed private/bespoke repos whose
    # GitHub Actions suite has no CodeQL workflow at all.
    if not args.no_codeql and codeql_config is not None:
        languages = detect_codeql_languages(target, codeql_config.languages)
        print()
        print(f'{blue("local codeql:")} {len(languages)} language job(s) [{",".join(languages)}]')
        print()
        for language in languages:
            print(f'--- codeql: {language} ---')
            ok, sub = run_codeql_analysis(
                target,
                language,
                codeql_config.queries,
                args.plan_only,
                source_root=target,
            )
            if not ok:
                grand_ok = False
            for key in ('PASS', 'FAIL', 'SKIP', 'DRY'):
                grand_counters[key] = grand_counters.get(key, 0) + sub.get(key, 0)
            if not ok:
                grand_failed.append(f'{sub.get("label", "CodeQL")} [{language}]')
            print()

    # Summary
    print()
    print_run_summary(grand_counters, grand_failed, grand_ok, args.plan_only)

    sys.exit(0 if grand_ok else 1)


if __name__ == '__main__':
    main()
