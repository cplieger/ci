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
    hadolint + shellcheck + gitleaks based on what's in SUBDIR).

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
# Reusable workflow resolution
# ---------------------------------------------------------------------------
REUSABLE_RE = re.compile(r'^cplieger/ci/\.github/workflows/(.+\.ya?ml)@(.+)$')


def resolve_reusable_workflow(uses_ref, target):
    """Resolve a reusable workflow `uses:` to its parsed YAML content.

    Lookup order:
      1. Sibling checkout: <repo-root>/../ci/.github/workflows/<X>.yaml
      2. gh api fetch (timeout-wrapped)
      3. None (caller falls back to autodetect)

    Caveat: the sibling-checkout path resolves to the LOCAL `ci/` working tree,
    ignoring the pinned `@sha` in `uses:`. So ci-local validates against the
    current (possibly unreleased) workflow source, not the exact SHA a
    consumer's CI runs. Intended for developing the ci repo; a minor fidelity
    caveat for consumers whose pinned SHA lags `main`.
    """
    m = REUSABLE_RE.match(uses_ref)
    if not m:
        return None
    filename = m.group(1)
    ref = m.group(2)

    # 1. Sibling checkout (local ci repo)
    # Walk up from target to find repo root (has .git)
    repo_root = target
    while repo_root != repo_root.parent:
        if (repo_root / '.git').exists():
            break
        repo_root = repo_root.parent
    sibling = repo_root.parent / 'ci' / '.github' / 'workflows' / filename
    if sibling.is_file():
        with open(sibling) as f:
            return yaml.safe_load(f)

    # 2. gh api fetch
    if shutil.which('gh'):
        try:
            cmd = (
                f'timeout 15 gh api "repos/cplieger/ci/contents/.github/workflows/{filename}'
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
# markdownlint-cli2 gitignore parity
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
    """Execute a step. Return (passed: bool, status_label: str)."""
    wd = step.get('working-directory')
    cwd = base_cwd / wd if wd else base_cwd
    env = os.environ.copy()
    for k, v in (step.get('env') or {}).items():
        env[k] = str(v)
    apply_runner_env(env, cwd)

    if kind == 'SKIP':
        return True, gray('SKIP')

    if kind == 'UNKNOWN':
        print(f'  {red("UNKNOWN")} action — {detail}', file=sys.stderr)
        return False, red('UNKNOWN')

    if kind == 'LOCAL':
        cmd = detail
    else:  # EXEC
        cmd = step['run']
        cmd = rewrite_hadolint_docker(cmd)
        cmd = rewrite_markdownlint_gitignore(cmd, cwd)

    if dry_run:
        return True, blue('DRY')

    try:
        proc = subprocess.run(
            ['timeout', '300', 'bash', '-eu', '-o', 'pipefail', '-c', cmd],
            cwd=str(cwd),
            env=env,
        )
        if proc.returncode == 0:
            return True, green('PASS')
        return False, red(f'FAIL (rc={proc.returncode})')
    except FileNotFoundError as e:
        print(f'  {red("ERR")} {e}', file=sys.stderr)
        return False, red('ERR')


# ---------------------------------------------------------------------------
# Autodetect fallback (no validate.yaml in target dir)
# ---------------------------------------------------------------------------


def autodetect_steps(target: Path):
    """Build a synthetic step list when no workflow is available.

    Mirrors the common patterns across the user's repos: Go suite if go.mod
    is present, hadolint if Dockerfile present, shellcheck for any *.sh,
    gitleaks always.
    """
    steps = []

    has_gomod = (target / 'go.mod').is_file()
    has_dockerfile = (target / 'Dockerfile').is_file()
    sh_files = sorted(p.name for p in target.glob('*.sh'))

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
        files_arg = ' '.join(sh_files)
        steps.append(
            {
                'name': 'Lint shell scripts',
                'run': f'shellcheck -x -S info {files_arg}',
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


def compute_local_detect(target):
    """Reproduce the meta ci.yaml `detect` job's surface outputs from local file
    presence. ci-local always runs the applicable surfaces (it mirrors CI's
    fail-safe "treat as code change" path; there is no docs-only skip locally)."""
    web = detect_web_dir(target)
    has_dockerfile = (target / 'Dockerfile').is_file()
    return {
        'run_go': 'true' if (target / 'go.mod').is_file() else 'false',
        'run_ts': 'true' if (target / 'jsr.json').is_file() else 'false',
        'run_web': 'true' if web else 'false',
        'run_shell': 'true' if has_dockerfile else 'false',
        'run_docker': 'true' if has_dockerfile else 'false',
        'web_dir': web or '',
    }


def job_applies_locally(jobname, target):
    """Gate an expanded (recursed) job by local surface detection, mirroring the
    meta ci.yaml job-level `if: needs.detect.outputs.run_X`. The surface is the
    meta job component (second path segment, e.g. 'go' in 'ci/go/test')."""
    parts = jobname.split('/')
    meta = parts[1] if len(parts) > 1 else parts[0]
    det = compute_local_detect(target)
    gate = {
        'go': det['run_go'],
        'ts': det['run_ts'],
        'web': det['run_web'],
        'shell': det['run_shell'],
        'docker': det['run_docker'],
    }.get(meta)
    return True if gate is None else gate == 'true'


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


def _expand_job(jobname, job, caller_inputs, target, depth=0):
    """Expand one job into terminal (jobname, steps, working_dir, caller_inputs)
    tuples, recursing through nested reusable-workflow callers (consumer ci.yaml
    -> meta ci.yaml -> go-ci/ts-ci/shell-ci/...). `caller_inputs` is None only
    for a plain inline job that never came from a reusable workflow."""
    uses = job.get('uses', '')
    if uses and REUSABLE_RE.match(uses) and depth < 6:
        resolved = resolve_reusable_workflow(uses, target)
        if resolved is None:
            print(
                f'  {yellow("WARN")} could not resolve reusable workflow: {uses}; '
                f'falling back to autodetect for job "{jobname}"',
                file=sys.stderr,
            )
            return [(jobname, autodetect_steps(target), '.', None)]
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
            out.extend(_expand_job(f'{jobname}/{rjob_name}', rjob, merged, target, depth + 1))
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


def process_reusable_steps(steps, target, working_dir, caller_inputs, dry_run, ignore_unknown):
    """Process steps from a resolved reusable workflow.

    Runs the profile step first to capture outputs, then evaluates if-conditions.
    Returns (ok, counters, failed_steps).
    """
    counters = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'UNKNOWN': 0}
    failed_steps = []
    overall_ok = True
    step_outputs = {}  # {step_id: {key: value}}

    base_cwd = target / working_dir if working_dir != '.' else target

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
            if not ignore_unknown:
                overall_ok = False
                failed_steps.append(name)
            continue

        ok, status = run_step(kind, name, detail, eff_step, target, dry_run)
        if dry_run:
            counters['DRY'] += 1
        elif ok:
            counters['PASS'] += 1
            print(f'    → {status}')
        else:
            counters['FAIL'] += 1
            overall_ok = False
            failed_steps.append(name)
            print(f'    → {status}')

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
        ok, status = run_step(kind, name, detail, s, target, dry_run)
        if dry_run:
            counters['DRY'] += 1
        elif ok:
            counters['PASS'] += 1
            print(f'    → {status}')
        else:
            counters['FAIL'] += 1
            failed.append(name)
            print(f'    → {status}')

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
    counters = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'UNKNOWN': 0}
    failed_steps = []
    overall_ok = True

    for jobname, steps in jobs:
        print(f'--- job: {jobname} ---')
        for step in steps:
            kind, name, detail = classify_step(step)
            wd = step.get('working-directory')
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
                if not ignore_unknown:
                    overall_ok = False
                    failed_steps.append(name)
                continue

            ok, status = run_step(kind, name, detail, step, target, dry_run)
            if dry_run:
                counters['DRY'] += 1
            elif ok:
                counters['PASS'] += 1
                print(f'    → {status}')
            else:
                counters['FAIL'] += 1
                overall_ok = False
                failed_steps.append(name)
                print(f'    → {status}')
        print()

    return overall_ok, counters, failed_steps


def process_workflow_file(wf_path, target, dry_run, ignore_unknown, no_codeql):
    """Process a single workflow file, handling reusable workflow expansion.

    Returns (ok, counters, failed_steps).
    """
    grand_counters = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'UNKNOWN': 0}
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
    has_reusable = any(REUSABLE_RE.match(job.get('uses', '')) for job in jobs_dict.values())

    if has_reusable:
        expanded = expand_reusable_jobs(jobs_dict, target)
        for jobname, steps, working_dir, caller_inputs in expanded:
            print(f'--- job: {jobname} ---')
            if not job_applies_locally(jobname, target):
                print(f'  {gray("SKIP")} (surface not present locally)')
                print()
                continue
            if not steps:
                print(f'  {gray("SKIP")} (no steps to run)')
                print()
                continue
            if caller_inputs is not None:
                # This came from a reusable workflow — use if-evaluation
                ok, counters, failed = process_reusable_steps(
                    steps, target, working_dir, caller_inputs, dry_run, ignore_unknown
                )
            else:
                # Autodetect fallback
                ok, counters, failed = run_workflow_steps(
                    [(jobname, steps)], target, dry_run, ignore_unknown
                )
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


def main():
    # Ensure print() output flushes promptly
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

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
        marker = '/tmp/_ci_failures'
        with contextlib.suppress(FileNotFoundError):
            os.unlink(marker)

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

    grand_counters = {'PASS': 0, 'FAIL': 0, 'SKIP': 0, 'DRY': 0, 'UNKNOWN': 0}
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
    print('=== summary ===')
    print('  ' + '  '.join(f'{k}={v}' for k, v in grand_counters.items() if v))
    if grand_failed:
        print(f'\n{red("failed:")}')
        for s in grand_failed:
            print(f'  - {s}')

    sys.exit(0 if grand_ok else 1)


if __name__ == '__main__':
    main()
