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
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

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
        'SKIP',
        'PR-only GitHub API; covered locally by govulncheck',
    ),
    'github/codeql-action/init': ('SKIP', 'CodeQL is GitHub-only; no local equivalent'),
    'github/codeql-action/autobuild': ('SKIP', 'CodeQL build phase; GitHub-only'),
    'github/codeql-action/analyze': ('SKIP', 'CodeQL analysis; GitHub-only'),
    'github/codeql-action/upload-sarif': ('SKIP', 'SARIF upload; GitHub-only'),
    'aquasecurity/trivy-action': (
        'LOCAL',
        # Advisory in CI (every trivy step pins exit-code: 0; findings go to the
        # Security tab, never gate a merge/release). Mirror that locally with
        # --exit-code 0 so a HIGH/CRITICAL finding prints but does not fail the
        # local run, matching CI semantics.
        'trivy fs --severity HIGH,CRITICAL --ignore-unfixed --exit-code 0 .',
    ),
    'docker/setup-buildx-action': ('SKIP', 'buildx assumed available with local Docker'),
    'docker/build-push-action': (
        'SKIP',
        'image build skipped locally; run docker build manually if needed',
    ),
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
Failure = namedtuple('Failure', 'job step rc cmd output')

# One executed step's result. outcome ∈ {pass, fail, missing, skip, unknown, dry}.
# `missing` = exit 127 (the tool isn't on PATH locally): a local-environment gap,
# NOT a code failure — CI has the tool, so it does not fail the local run.
StepResult = namedtuple('StepResult', 'ok status rc cmd output outcome')


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


def evaluate_step_if(expr, step_outputs, caller_inputs=None):
    """Evaluate a GitHub Actions if-expression to True/False.

    Supports:
      - steps.<id>.outputs.<k> == 'true'/'false'
      - github.event.repository.private == false  -> TRUE (local = full run)
      - github.event_name == 'schedule'|'workflow_dispatch' -> TRUE
      - github.event_name == 'pull_request' -> FALSE
      - inputs.<k> -> from caller_inputs
      - && , ||, ! prefix, ${{ }} wrapping
      - always() -> TRUE
      - hashFiles(...) != '' -> resolved against the real filesystem
        (empty string when no glob matches, a hash otherwise — GHA semantics)
    """
    if caller_inputs is None:
        caller_inputs = {}

    # Strip ${{ }} wrapper
    expr = expr.strip()
    if expr.startswith('${{') and expr.endswith('}}'):
        expr = expr[3:-2].strip()

    return _eval_expr(expr, step_outputs, caller_inputs)


def _eval_expr(expr, step_outputs, caller_inputs):
    """Recursive expression evaluator."""
    expr = expr.strip()

    # Handle always()
    if expr == 'always()':
        return True

    # Handle || (lowest precedence)
    # Split on && and || respecting nesting
    parts = _split_logical(expr, '||')
    if len(parts) > 1:
        return any(_eval_expr(p, step_outputs, caller_inputs) for p in parts)

    parts = _split_logical(expr, '&&')
    if len(parts) > 1:
        return all(_eval_expr(p, step_outputs, caller_inputs) for p in parts)

    # Handle ! prefix
    if expr.startswith('!'):
        return not _eval_expr(expr[1:].strip(), step_outputs, caller_inputs)

    # Handle parentheses
    if expr.startswith('(') and expr.endswith(')'):
        return _eval_expr(expr[1:-1], step_outputs, caller_inputs)

    # Handle comparison: X == Y or X != Y
    for op in ('!=', '=='):
        idx = expr.find(op)
        if idx >= 0:
            lhs = _resolve_value(expr[:idx].strip(), step_outputs, caller_inputs)
            rhs = _resolve_value(expr[idx + len(op) :].strip(), step_outputs, caller_inputs)
            if op == '==':
                return str(lhs) == str(rhs)
            return str(lhs) != str(rhs)

    # Handle hashFiles(...) != '' pattern — already handled by comparison above
    # Bare expression: resolve to truthy
    val = _resolve_value(expr, step_outputs, caller_inputs)
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


def _resolve_value(tok, step_outputs, caller_inputs):
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

    # github.event_name
    if tok == 'github.event_name':
        return 'workflow_dispatch'  # local = full run (not PR)

    # hashFiles(...) — resolve against the real filesystem. GitHub Actions'
    # hashFiles returns an empty string when no file matches the glob(s) and a
    # hash otherwise, so a step guarded by `if: hashFiles('x') != ''` runs only
    # when x exists. Mirror that locally: the opt-in `tests/image-smoke.sh`
    # step (advisory) must SKIP when the repo ships no image-smoke script,
    # exactly as it does in CI — assuming the file always exists made the local
    # run try `sh tests/image-smoke.sh` and fail rc=127 on every docker-* repo.
    if tok.startswith('hashFiles(') and tok.endswith(')'):
        args = tok[len('hashFiles(') : -1]
        patterns = [a.strip().strip('\'"') for a in args.split(',') if a.strip()]
        base = Path.cwd()
        for pat in patterns:
            if any(p.is_file() for p in base.glob(pat)):
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


def apply_runner_env(env: dict, cwd: Path) -> dict:
    """Augment env (in place) with the GitHub Actions runner vars that workflow
    steps rely on. Uses setdefault so a real Actions environment (or an explicit
    step `env:`) always wins. Returns env for chaining."""
    rt = _runner_temp_dir()
    env.setdefault('RUNNER_TEMP', rt)
    env.setdefault('RUNNER_OS', 'Linux')
    env.setdefault('RUNNER_ARCH', 'X64')
    env.setdefault('GITHUB_WORKSPACE', str(cwd))
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


def run_profile_step(step, cwd):
    """Execute a profile step's `run:` block with GITHUB_OUTPUT capture.

    Returns dict of {key: value} from the output file.
    """
    run_script = step.get('run', '')
    if not run_script:
        return {}

    outputs = {}
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tf:
            output_file = tf.name

        env = os.environ.copy()
        env['GITHUB_OUTPUT'] = output_file
        # Provide GITHUB_WORKSPACE as the target dir
        env['GITHUB_WORKSPACE'] = str(cwd)
        apply_runner_env(env, cwd)

        proc = subprocess.run(
            ['timeout', '10', 'bash', '-eu', '-o', 'pipefail', '-c', run_script],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
        )

        if proc.returncode == 0 and Path(output_file).is_file():
            with open(output_file) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line:
                        k, v = line.split('=', 1)
                        outputs[k] = v
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        with contextlib.suppress(OSError):
            os.unlink(output_file)

    return outputs


# ---------------------------------------------------------------------------
# Step classification
# ---------------------------------------------------------------------------


def classify_step(step):
    """Return (kind, name, detail).

    kind: 'EXEC' | 'LOCAL' | 'SKIP' | 'UNKNOWN'
    """
    name = step.get('name') or '(unnamed)'

    if 'uses' in step:
        action_ref = step['uses'].split('@', 1)[0].strip()
        # Build-ability gate: CI's required `docker` job builds the image via
        # docker/build-push-action (no push). Mirror it locally with
        # `docker build` (same context/file/target, default = final stage) so a
        # Dockerfile that won't build fails the local run too. SKIP loudly when
        # docker isn't on PATH rather than pretending the gate ran.
        if action_ref == 'docker/build-push-action':
            if not shutil.which('docker'):
                return 'SKIP', name, 'docker not on PATH; build-ability gate NOT exercised locally'
            with_ = step.get('with') or {}
            context = str(with_.get('context', '.')).strip() or '.'
            dockerfile = str(with_.get('file', 'Dockerfile')).strip() or 'Dockerfile'
            target_stage = str(with_.get('target', '')).strip()
            cmd = f'docker build -f {shlex.quote(dockerfile)}'
            if target_stage:
                cmd += f' --target {shlex.quote(target_stage)}'
            # Preserve the image tag(s) so a downstream smoke-test step that runs
            # `docker run <tag>` can find the just-built image. CI tags via the
            # `tags:` input (e.g. ci-smoke:latest); without -t locally the build
            # produces a dangling image and the smoke step fails with "Unable to
            # find image".
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
    import json

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
            ['git', '-C', str(cwd), 'ls-files', '*.md'],
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


def run_step(kind, name, detail, step, base_cwd: Path, dry_run: bool):
    """Execute a step. Returns a StepResult.

    Captures combined stdout+stderr so failures can be replayed in the summary.
    Output is echoed inline only for fail/missing steps (a passing step's output
    is noise for an agent scanning for problems). A 127 exit (command not on
    PATH) is classified `missing` — the tool isn't installed locally, which is a
    local-environment gap rather than a code failure, so it must not fail the run
    (CI has the tool); the summary lists it under NOT VALIDATED LOCALLY instead.
    """
    wd = step.get('working-directory')
    cwd = base_cwd / wd if wd else base_cwd
    env = os.environ.copy()
    for k, v in (step.get('env') or {}).items():
        env[k] = str(v)
    apply_runner_env(env, cwd)

    if kind == 'SKIP':
        return StepResult(True, gray('SKIP'), 0, '', '', 'skip')

    if kind == 'UNKNOWN':
        return StepResult(False, red('UNKNOWN'), 0, '', detail, 'unknown')

    if kind == 'LOCAL':
        cmd = detail
    else:  # EXEC
        cmd = step['run']
        cmd = rewrite_hadolint_docker(cmd)
        cmd = rewrite_markdownlint_gitignore(cmd, cwd)
        cmd = rewrite_gitleaks_download(cmd)
        cmd = rewrite_gitleaks_gitignore(cmd, cwd)
        cmd = rewrite_trivy_gitignore(cmd, cwd)
        cmd = rewrite_ci_failures_path(cmd)

    if dry_run:
        return StepResult(True, blue('DRY'), 0, cmd, '', 'dry')

    try:
        proc = subprocess.run(
            ['timeout', str(_STEP_TIMEOUT_SECS), 'bash', '-eu', '-o', 'pipefail', '-c', cmd],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        return StepResult(False, red('ERR'), 127, cmd, str(e), 'missing')

    output = (proc.stdout or '') + (proc.stderr or '')
    rc = proc.returncode

    if rc == 0:
        return StepResult(True, green('PASS'), 0, cmd, output, 'pass')

    # Echo the failing output inline (last 40 lines) so it's visible at the
    # point of failure, not only in the summary.
    body = _tail(output, 40)
    for line in body:
        print(f'      {line}')
    if len(output.rstrip('\n').splitlines()) > 40:
        print(f'      {gray("... (output trimmed; full tail in summary)")}')

    if rc == 127:
        return StepResult(True, yellow('MISSING (tool not on PATH)'), 127, cmd, output, 'missing')
    if rc == 124:
        return StepResult(False, red(f'FAIL (timed out at {_STEP_TIMEOUT_SECS}s)'), 124, cmd, output, 'fail')
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
    """Mirror the meta ci.yaml detect web-frontend probe: first of static-src/,
    web/, internal/server/static-src/ that contains package.json or jsr.json."""
    for d in WEB_DIR_PROBE:
        p = target / d
        if p.is_dir() and ((p / 'package.json').is_file() or (p / 'jsr.json').is_file()):
            return d
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


def _has_tracked_file(target, *pathspecs):
    """True if git tracks any file matching the pathspecs under target.

    CI's detect job runs `find` on a FRESH CHECKOUT — only git-tracked files
    exist on the runner. Mirror that: a gitignored working-tree file (e.g. a
    scratch *.py under .code-review/) must NOT flip a surface on, or ci-local
    would run a job CI skips. Falls back to a working-tree walk when target is
    not a git repo / git is unavailable."""
    try:
        out = subprocess.run(
            ['git', '-C', str(target), 'ls-files', '-z', '--', *pathspecs],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return bool(out.replace('\x00', '').strip())
    except (OSError, subprocess.CalledProcessError):
        for ps in pathspecs:
            if '/' in ps:
                if any(target.glob(ps)):
                    return True
            elif _has_file_anywhere(target, ps):
                return True
        return False


def compute_local_detect(target):
    """Reproduce the meta ci.yaml `detect` job's surface outputs from local file
    presence. ci-local always runs the applicable surfaces (it mirrors CI's
    fail-safe "treat as code change" path; there is no docs-only skip locally).
    File globs (*.py / *.sh / workflows) are checked against git-tracked files so
    a gitignored scratch file can't flip a surface CI's fresh checkout wouldn't
    see."""
    web = detect_web_dir(target)
    has_dockerfile = (target / 'Dockerfile').is_file()
    has_gomod = (target / 'go.mod').is_file()
    has_jsr = (target / 'jsr.json').is_file()

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

    return {
        'run_go': 'true' if has_gomod else 'false',
        'run_ts': 'true' if has_jsr else 'false',
        'run_web': 'true' if web else 'false',
        'run_shell': 'true' if has_dockerfile else 'false',
        'run_docker': 'true' if has_dockerfile else 'false',
        'run_python': 'true' if run_python else 'false',
        'run_scripts': 'true' if run_scripts else 'false',
        'web_dir': web or '',
    }


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
        'ts': det['run_ts'],
        'web': det['run_web'],
        'shell': det['run_shell'],
        'docker': det['run_docker'],
        'python': det['run_python'],
        'scripts': det['run_scripts'],
    }
    for seg in jobname.split('/'):
        if seg in gate_map:
            return gate_map[seg] == 'true'
    return True  # markdown / detect / validate scaffolding — always runs


def _resolve_with_value(value, inputs, target, strip_unknown=False):
    """Resolve ${{ inputs.X }} (from caller inputs) and
    ${{ needs.<job>.outputs.X }} (from local detect, e.g. web_dir) in a `with:`
    value, a working-directory, or a `run:` script body. Non-strings pass
    through. With strip_unknown=True (used for run-script bodies), any other
    ${{ ... }} expression is replaced with '' so bash doesn't hit a "bad
    substitution" error on an expression GitHub would have substituted."""
    if not isinstance(value, str):
        return value

    def repl(m):
        inner = m.group(1).strip()
        im = re.match(r'inputs\.([\w-]+)$', inner)
        if im:
            return str(inputs.get(im.group(1), ''))
        nm = re.match(r'needs\.\w+\.outputs\.([\w-]+)$', inner)
        if nm:
            return str(compute_local_detect(target).get(nm.group(1), ''))
        return '' if strip_unknown else m.group(0)

    return re.sub(r'\$\{\{\s*(.+?)\s*\}\}', repl, value)


def _expand_job(jobname, job, caller_inputs, target, depth=0, parent_ref=None):
    """Expand one job into terminal (jobname, steps, working_dir, caller_inputs)
    tuples, recursing through nested reusable-workflow callers (consumer ci.yaml
    -> meta ci.yaml -> go-ci/ts-ci/shell-ci/...). `caller_inputs` is None only
    for a plain inline job that never came from a reusable workflow. `parent_ref`
    is the calling workflow's pinned ref, threaded so a nested local `./` ref can
    be fetched at the same commit when no sibling ci/ checkout exists."""
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
    """Process steps from a resolved reusable workflow.

    Runs the profile step first to capture outputs, then evaluates if-conditions.
    Returns (ok, counters, failed_steps). `jobname` is used to attribute
    failures and not-validated notes in the agent-facing summary.
    """
    counters = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'UNKNOWN': 0, 'MISSING': 0}
    failed_steps = []
    overall_ok = True
    step_outputs = {}  # {step_id: {key: value}}

    base_cwd = target / working_dir if working_dir != '.' else target

    # Each job gets a fresh soft-gate marker, mirroring CI's per-job /tmp.
    if not dry_run:
        _clear_failure_marker()

    for step in steps:
        step_id = step.get('id', '')
        name = step.get('name') or '(unnamed)'

        # Resolve input expressions in the step's working-directory
        step_wd = step.get('working-directory', '')
        if step_wd:
            step_wd = _resolve_input_expr(step_wd, caller_inputs)

        # Check if this is a profile/detect step that writes to GITHUB_OUTPUT
        if step_id and 'run' in step and 'GITHUB_OUTPUT' in step.get('run', ''):
            # Determine the cwd for running this step
            run_cwd = target / step_wd if step_wd and step_wd != '.' else base_cwd
            outputs = run_profile_step(step, run_cwd)
            step_outputs[step_id] = outputs
            tag = gray('DETECT')
            out_str = ', '.join(f'{k}={v}' for k, v in outputs.items())
            print(f'  {tag:<7} {name}  ({out_str})')
            counters['SKIP'] += 1
            continue

        # Evaluate if-condition
        if_expr = step.get('if', '')
        if if_expr:
            result = evaluate_step_if(if_expr, step_outputs, caller_inputs)
            if not result:
                tag = gray('SKIP')
                print(f'  {tag:<7} {name}  (if: false)')
                counters['SKIP'] += 1
                continue

        # Build the effective step first: resolve ${{ inputs.* }} / ${{ needs.* }}
        # in `with:` values and working-directory BEFORE classification, so steps
        # like docker/build-push-action receive concrete file/context paths
        # instead of literal `${{ inputs.dockerfile }}`.
        eff_step = dict(step)
        if isinstance(step.get('with'), dict):
            eff_step['with'] = {
                k: _resolve_with_value(v, caller_inputs, target) for k, v in step['with'].items()
            }
        if isinstance(step.get('run'), str):
            # CI substitutes ${{ }} before the shell sees it; mirror that for
            # inputs/needs and strip any other expression so bash doesn't fail
            # with "bad substitution" on a literal `${{ ... }}` in the script.
            eff_step['run'] = _resolve_with_value(
                step['run'], caller_inputs, target, strip_unknown=True
            )
        if step_wd and step_wd != '.':
            eff_step['working-directory'] = step_wd
        elif 'working-directory' not in step and working_dir != '.':
            eff_step['working-directory'] = working_dir
        elif 'working-directory' in eff_step:
            eff_step['working-directory'] = step_wd or None

        kind, _sname, detail = classify_step(eff_step)

        wd = eff_step.get('working-directory')
        wd_str = f' [cwd={wd}]' if wd else ''
        tag = {
            'EXEC': blue('EXEC'),
            'LOCAL': blue('LOCAL'),
            'SKIP': gray('SKIP'),
            'UNKNOWN': red('UNKNOWN'),
        }.get(kind, kind)
        tail = f'  ({detail})' if detail else ''
        print(f'  {tag:<7} {name}{wd_str}{tail}')

        if kind == 'SKIP':
            counters['SKIP'] += 1
            continue
        if kind == 'UNKNOWN':
            counters['UNKNOWN'] += 1
            REPORT.not_validated.append(
                (jobname, name, f'unrecognized action — not run locally ({detail})')
            )
            if not ignore_unknown:
                overall_ok = False
                failed_steps.append(Failure(jobname, name, 0, '', detail))
            continue

        res = run_step(kind, name, detail, eff_step, target, dry_run)
        if res.outcome == 'dry':
            counters['DRY'] += 1
            continue
        print(f'    → {res.status}')
        if res.outcome == 'pass':
            counters['PASS'] += 1
        elif res.outcome == 'missing':
            counters['MISSING'] += 1
            tool = res.cmd.split()[0] if res.cmd else name
            REPORT.not_validated.append(
                (jobname, name, f'`{tool}` not on PATH (exit 127) — CI has it; install to check locally')
            )
        else:  # fail
            # continue-on-error: true makes a step advisory in CI — its failure
            # is recorded but never fails the job/gate. Mirror that locally:
            # report under not_validated instead of failing the run. The image
            # smoke test is the main user of this.
            if step.get('continue-on-error') in (True, 'true'):
                counters['MISSING'] += 1
                REPORT.not_validated.append(
                    (jobname, name, f'advisory step failed (continue-on-error; non-blocking in CI) — rc={res.rc}')
                )
            else:
                counters['FAIL'] += 1
                overall_ok = False
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


def parse_sarif_alerts(sarif_path: Path):
    """Extract findings from a SARIF v2.1.0 file. Returns list of dicts."""
    import json

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
    """Mirror CodeQL Action's failure threshold: tag 'security' AND severity >= 'medium'."""
    tags = alert.get('tags') or []
    if not any(t == 'security' or t.startswith('security') for t in tags):
        return False
    sev = alert.get('severity', '')
    try:
        return float(sev) >= 4.0
    except ValueError:
        return alert.get('level') in ('error', 'warning')


# ---------------------------------------------------------------------------
# CodeQL suppression config (.codeql-suppressions.yaml)
# ---------------------------------------------------------------------------


def load_suppressions(target: Path):
    """Load .codeql-suppressions.yaml from the repo root. Returns list of dicts."""
    path = target / '.codeql-suppressions.yaml'
    if not path.is_file():
        return []
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        print(f'  {yellow("WARN")} could not read {path.name}: {e}', file=sys.stderr)
        return []
    sups = data.get('suppressions') or []
    valid = []
    for s in sups:
        if not isinstance(s, dict) or not s.get('reason'):
            print(
                f'  {yellow("WARN")} skipping suppression without a reason: {s}',
                file=sys.stderr,
            )
            continue
        if not (s.get('fingerprint') or s.get('path')):
            print(
                f'  {yellow("WARN")} skipping suppression with neither fingerprint nor path: {s}',
                file=sys.stderr,
            )
            continue
        valid.append(s)
    return valid


def is_suppressed(alert, suppressions):
    """Return the matching suppression dict if alert is allowlisted, else None."""
    for s in suppressions:
        rule = s.get('rule')
        if rule and rule != alert.get('rule'):
            continue
        fp = s.get('fingerprint')
        if fp:
            if fp == alert.get('fingerprint'):
                return s
            continue
        sp = s.get('path')
        if sp and sp == alert.get('path'):
            return s
    return None


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
    """Build a CodeQL DB, analyze it, filter findings through suppressions."""
    src = source_root or target
    info = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'label': 'CodeQL'}

    print(
        f'  {blue("CODEQL")} analyze (lang={languages}, queries={queries or "default"}, root={src})'
    )

    if not shutil.which('codeql'):
        print(
            f'    → {gray("SKIP")} codeql binary not on PATH; analysis runs in '
            f'CI (install locally via tools.json, or pass --no-codeql to silence)'
        )
        info['SKIP'] += 1
        info['label'] = 'CodeQL (skipped — binary not installed)'
        REPORT.not_validated.append(
            ('codeql', f'analyze ({languages})', 'codeql binary not on PATH — runs in CI')
        )
        return True, info

    if dry_run:
        info['DRY'] += 1
        return True, info

    db_dir = Path('/tmp') / f'codeql-db-{os.getpid()}-{abs(hash(str(src))) % 100000}'
    sarif_out = Path('/tmp') / f'codeql-results-{os.getpid()}-{abs(hash(str(src))) % 100000}.sarif'

    create_cmd = (
        f'timeout 300 codeql database create {db_dir} '
        f'--language={languages} --source-root={src} --overwrite'
    )
    suites = []
    if queries:
        for q in queries.split(','):
            q = q.strip()
            if not q:
                continue
            if '/' in q or q.endswith(('.qls', '.ql')):
                suites.append(q)
            else:
                suites.append(f'{languages}-{q}.qls')
    suites_arg = ' '.join(suites)
    analyze_cmd = (
        f'timeout 300 codeql database analyze {db_dir} {suites_arg} '
        f'--format=sarif-latest --output={sarif_out}'
    )

    try:
        proc = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c', create_cmd], cwd=str(src))
        if proc.returncode != 0:
            info['FAIL'] += 1
            info['label'] = 'CodeQL database create'
            print(f'    → {red(f"FAIL (rc={proc.returncode})")} (database create)')
            return False, info

        proc = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c', analyze_cmd], cwd=str(src))
        if proc.returncode != 0:
            info['FAIL'] += 1
            info['label'] = 'CodeQL analyze'
            print(f'    → {red(f"FAIL (rc={proc.returncode})")} (analyze)')
            return False, info

        alerts = parse_sarif_alerts(sarif_out)
        suppressions = load_suppressions(target)
        sec_alerts = [a for a in alerts if is_security_alert(a)]

        active, suppressed = [], []
        for a in sec_alerts:
            match = is_suppressed(a, suppressions)
            (suppressed if match else active).append((a, match))

        if suppressed:
            print(f'    {gray(f"({len(suppressed)} suppressed via .codeql-suppressions.yaml)")}')
            for a, m in suppressed[:20]:
                print(gray(f'      - {a["rule"]} | {a["path"]}:{a["line"]} — {m["reason"]}'))

        if active:
            info['FAIL'] += 1
            info['label'] = f'CodeQL ({len(active)} security alerts)'
            print(f'    → {red(f"FAIL ({len(active)} security alerts)")}')
            for a, _ in active[:20]:
                sev_str = f'sev={a["severity"]}' if a['severity'] else f'level={a["level"]}'
                print(f'      {red(sev_str)} | {a["rule"]} | {a["path"]}:{a["line"]}')
            if len(active) > 20:
                print(f'      ... and {len(active) - 20} more')
            return False, info

        info['PASS'] += 1
        extra = []
        if suppressed:
            extra.append(f'{len(suppressed)} suppressed')
        nonsec = len(alerts) - len(sec_alerts)
        if nonsec:
            extra.append(f'{nonsec} non-security ignored')
        suffix = f' ({", ".join(extra)})' if extra else ' (no alerts)'
        print(f'    → {green("PASS")}{suffix}')
        return True, info
    finally:
        if db_dir.exists():
            shutil.rmtree(db_dir, ignore_errors=True)


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
    # CI workflow: prefer ci.yaml.
    for name in ('ci.yaml', 'ci.yml', 'validate.yaml', 'validate.yml'):
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
    # CodeQL workflow.
    for name in ('codeql.yml', 'codeql.yaml'):
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
    """Return True if target contains JS/TS sources outside excluded dirs.

    CodeQL's javascript-typescript extractor analyzes the whole tree at once
    (like Python), so a single yes/no signal suffices.
    """
    skip = {'node_modules', 'vendor', '.git', 'dist', 'build', 'out', '.next', 'coverage'}
    for ext in ('*.ts', '*.tsx', '*.js', '*.jsx', '*.mjs', '*.cjs'):
        for p in target.rglob(ext):
            if any(part in skip for part in p.relative_to(target).parts):
                continue
            return True
    return False


def workflow_installs_npm_packages(jobs):
    """Return True if any step's `run:` script invokes `npm install` or `npm ci`."""
    pat = re.compile(r'(?m)^\s*(npm|yarn|pnpm)\s+(install|ci)\b')
    for _jobname, steps in jobs:
        for s in steps:
            run = s.get('run') or ''
            if pat.search(run):
                return True
    return False


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
    """Run a non-CodeQL workflow's jobs/steps. Returns (overall_ok, counters, failed)."""
    counters = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'UNKNOWN': 0, 'MISSING': 0}
    failed_steps = []
    overall_ok = True

    for jobname, steps in jobs:
        # Each job gets a fresh soft-gate marker, mirroring CI's per-job /tmp.
        if not dry_run:
            _clear_failure_marker()
        print(f'--- job: {jobname} ---')
        for step in steps:
            # Resolve/strip GitHub expressions in the run body, mirroring the
            # reusable-step path: bash can't expand `${{ github.event_name }}`
            # (a "bad substitution" abort under `bash -eu`), so a plain workflow
            # whose steps reference GHA context — e.g. the meta ci.yaml's detect
            # step, parsed directly when ci-local runs on the ci repo itself —
            # would spuriously FAIL. `needs.*.outputs.*` resolves from local
            # detect; any other expression is stripped, matching CI's substitution.
            eff_step = step
            if isinstance(step.get('run'), str) and '${{' in step['run']:
                eff_step = dict(step)
                eff_step['run'] = _resolve_with_value(
                    step['run'], {}, target, strip_unknown=True
                )
            kind, name, detail = classify_step(eff_step)
            wd = eff_step.get('working-directory')
            wd_str = f' [cwd={wd}]' if wd else ''
            tag = {
                'EXEC': blue('EXEC'),
                'LOCAL': blue('LOCAL'),
                'SKIP': gray('SKIP'),
                'UNKNOWN': red('UNKNOWN'),
            }[kind]
            tail = f'  ({detail})' if detail else ''
            print(f'  {tag:<7} {name}{wd_str}{tail}')

            if kind == 'SKIP':
                counters['SKIP'] += 1
                continue
            if kind == 'UNKNOWN':
                counters['UNKNOWN'] += 1
                REPORT.not_validated.append(
                    (jobname, name, f'unrecognized action — not run locally ({detail})')
                )
                if not ignore_unknown:
                    overall_ok = False
                    failed_steps.append(Failure(jobname, name, 0, '', detail))
                continue

            res = run_step(kind, name, detail, eff_step, target, dry_run)
            if res.outcome == 'dry':
                counters['DRY'] += 1
                continue
            print(f'    → {res.status}')
            if res.outcome == 'pass':
                counters['PASS'] += 1
            elif res.outcome == 'missing':
                counters['MISSING'] += 1
                tool = res.cmd.split()[0] if res.cmd else name
                REPORT.not_validated.append(
                    (jobname, name, f'`{tool}` not on PATH (exit 127) — CI has it; install to check locally')
                )
            else:  # fail
                # continue-on-error: true makes a step advisory in CI — mirror
                # that locally (report, don't fail the run).
                if eff_step.get('continue-on-error') in (True, 'true'):
                    counters['MISSING'] += 1
                    REPORT.not_validated.append(
                        (jobname, name, f'advisory step failed (continue-on-error; non-blocking in CI) — rc={res.rc}')
                    )
                else:
                    counters['FAIL'] += 1
                    overall_ok = False
                    failed_steps.append(Failure(jobname, name, res.rc, res.cmd, res.output))
        print()

    return overall_ok, counters, failed_steps


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

    # Check if this is a CodeQL workflow (reusable or inline)
    if is_codeql_workflow(jobs_dict):
        if no_codeql:
            print(f'  {gray("SKIP")} (--no-codeql)')
            return True, grand_counters, []

        # If it's a reusable codeql caller, fall through to synthetic codeql
        # (the reusable codeql.yaml uses detect+matrix which we handle via
        # the synthetic pass)
        if is_codeql_reusable(jobs_dict):
            # Signal to caller: use synthetic codeql instead
            return None, grand_counters, []

        # Inline codeql steps: use old handler
        jobs = [(jn, j.get('steps') or []) for jn, j in jobs_dict.items()]
        for jobname, steps in jobs:
            print(f'--- job: {jobname} (codeql) ---')
            ok, counters, failed = run_codeql_for_job(jobname, steps, target, dry_run)
            if not ok:
                grand_ok = False
            for k, v in counters.items():
                grand_counters[k] = grand_counters.get(k, 0) + v
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
            if caller_inputs is not None:
                # This came from a reusable workflow — use if-evaluation
                ok, counters, failed = process_reusable_steps(
                    jobname, steps, target, working_dir, caller_inputs, dry_run, ignore_unknown
                )
            else:
                # Autodetect fallback
                ok, counters, failed = run_workflow_steps(
                    [(jobname, steps)], target, dry_run, ignore_unknown
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
    if counters.get('MISSING'):
        parts.append(f'{yellow("missing-tool")} {counters["MISSING"]}')
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
        if counters.get('MISSING'):
            notes.append(f'{counters["MISSING"]} tool(s) missing locally')
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
            jobs_for_check = load_workflow(wf_path)
            if jobs_for_check and workflow_installs_npm_packages(jobs_for_check):
                will_install_npm = True
                break
            # Also check resolved reusable workflows for npm install
            raw = load_workflow_raw(wf_path)
            if raw:
                for job in (raw.get('jobs') or {}).values():
                    uses = job.get('uses', '')
                    if uses and REUSABLE_RE.match(uses):
                        resolved = resolve_reusable_workflow(uses, target)
                        if resolved:
                            for rjob in (resolved.get('jobs') or {}).values():
                                for s in rjob.get('steps') or []:
                                    run = s.get('run') or ''
                                    if re.search(r'(?m)^\s*(npm|yarn|pnpm)\s+(install|ci)\b', run):
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
    ran_codeql_workflow = False
    use_synthetic_codeql = False

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

            ok, counters, failed = process_workflow_file(
                wf_path, target, args.plan_only, args.ignore_unknown, args.no_codeql
            )

            if ok is None:
                # Signal: reusable codeql -> use synthetic
                use_synthetic_codeql = True
                ran_codeql_workflow = True
                continue

            if 'codeql' in wf_path.name:
                ran_codeql_workflow = True

            if not ok:
                grand_ok = False
            for k, v in counters.items():
                grand_counters[k] = grand_counters.get(k, 0) + v
            grand_failed.extend(failed)

    # Synthetic CodeQL pass
    if not args.no_codeql and (use_synthetic_codeql or not ran_codeql_workflow):
        codeql_targets = []
        for mod in find_go_modules(target):
            rel_str = str(mod.relative_to(target)) if mod != target else '.'
            codeql_targets.append(('go', mod, rel_str))
        if has_python_sources(target):
            codeql_targets.append(('python', target, '.'))
        if has_js_ts_sources(target):
            codeql_targets.append(('javascript-typescript', target, '.'))

        if codeql_targets:
            print()
            langs = sorted({lang for lang, _, _ in codeql_targets})
            print(
                f'{blue("synthetic codeql:")} {len(codeql_targets)} target(s) [{",".join(langs)}]'
            )
            print()
            for lang, src, label in codeql_targets:
                print(f'--- codeql: {lang}: {label} ---')
                ok, sub = run_codeql_analysis(
                    target,
                    lang,
                    'security-extended,security-and-quality',
                    args.plan_only,
                    source_root=src,
                )
                if not ok:
                    grand_ok = False
                for k in ('PASS', 'FAIL', 'DRY'):
                    grand_counters[k] = grand_counters.get(k, 0) + sub.get(k, 0)
                if not ok:
                    grand_failed.append(f'{sub.get("label", "CodeQL")} [{lang}:{label}]')
                print()

    # Summary
    print()
    print_run_summary(grand_counters, grand_failed, grand_ok, args.plan_only)

    sys.exit(0 if grand_ok else 1)


if __name__ == '__main__':
    main()
