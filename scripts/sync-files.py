#!/usr/bin/env python3
"""In-house file-sync engine: push canonical files into consumer repos as PRs.

Replaces BetaHuhn/repo-file-sync-action (unmaintained since 2024, and it held
a fleet-wide write PAT — a supply-chain surface this first-party script
removes). Feature scope is deliberately the subset the fleet used:

  * read the runtime manifest classify-repos.sh generates (.github/sync.yml)
  * per target repo: shallow-clone, branch `repo-sync/ci/default` off the
    default branch, copy each mapped file from THIS checkout, commit
    `chore(sync): ...`, force-push, ensure an open PR labelled `dependencies`
  * no diff -> no PR; a leftover open sync PR whose diff has evaporated
    (content landed some other way) is closed and its branch deleted
  * failure isolation: one repo failing never aborts the rest; the run exits
    non-zero at the end if anything failed

NOT supported on purpose (the action offered these; the fleet never used
them): templating, per-group commit messages, and orphan-file DELETION —
syncing only ever adds or updates files.

Contract stability: the branch name, commit subject, PR title, and label all
match what the action produced, so consumer history stays uniform and
sync.yaml's separate auto-merge sweep (`gh pr list --head repo-sync/ci/default`)
keeps working unchanged.

Auth: uses the ambient `gh` credentials (GH_TOKEN / SYNC_PAT in CI). Git push
authenticates through gh's credential helper wired repo-locally on each clone
— no token ever appears in a remote URL or in process output.

Run:
  scripts/sync-files.py --manifest .github/sync.yml            # real sync
  scripts/sync-files.py --manifest .github/sync.yml --dry-run  # report only
  scripts/sync-files.py ... --only atomicfile,httpx            # limit targets
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

BRANCH = "repo-sync/ci/default"
COMMIT_SUBJECT = "chore(sync): synced file(s) with cplieger/ci"
PR_TITLE = COMMIT_SUBJECT
PR_LABEL = "dependencies"
GIT_USER = "github-actions[bot]"
GIT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
# Repo-local credential helper: git asks gh, gh uses GH_TOKEN/keyring. The
# leading ! marks a shell-out helper; scoped to each clone, never global.
CRED_HELPER = "!gh auth git-credential"


def run(args, cwd=None, check=True):
    """subprocess.run wrapper: captured text output, optional check."""
    return subprocess.run(
        args, cwd=cwd, check=check, capture_output=True, text=True
    )


def load_mapping(manifest_path):
    """Manifest -> {repo: {dest: source}}. A repo in several groups gets the
    union of their files; a duplicate dest keeps the LAST group's source
    (groups are emitted most-generic-first by classify-repos.sh)."""
    cfg = yaml.safe_load(Path(manifest_path).read_text()) or {}
    mapping = {}
    for group in cfg.get("group", []):
        repos = [r.strip() for r in (group.get("repos") or "").splitlines() if r.strip()]
        files = group.get("files") or []
        for repo in repos:
            dest_map = mapping.setdefault(repo, {})
            for entry in files:
                if isinstance(entry, str):
                    dest_map[entry] = entry
                else:
                    dest_map[entry["dest"]] = entry["source"]
    return mapping


def clone(repo, dest_dir):
    """Shallow-clone the default branch with the gh credential helper wired
    in repo-locally (covers private targets and the later push)."""
    run([
        "git", "-c", f"credential.helper={CRED_HELPER}",
        "clone", "--quiet", "--depth", "1",
        f"https://github.com/{repo}.git", str(dest_dir),
    ])
    run(["git", "config", "credential.helper", CRED_HELPER], cwd=dest_dir)
    run(["git", "config", "user.name", GIT_USER], cwd=dest_dir)
    run(["git", "config", "user.email", GIT_EMAIL], cwd=dest_dir)


def copy_files(source_root, clone_dir, dest_map):
    """Copy sources into the clone; return the staged dest paths that differ."""
    for dest, source in sorted(dest_map.items()):
        src = source_root / source
        if not src.is_file():
            msg = f"source file missing in ci checkout: {source}"
            raise FileNotFoundError(msg)
        target = clone_dir / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target)  # copies bytes + mode (x-bit survives)
    run(["git", "add", "--", *sorted(dest_map)], cwd=clone_dir)
    diff = run(
        ["git", "diff", "--cached", "--name-only"], cwd=clone_dir
    ).stdout.split()
    return sorted(diff)


def open_pr_number(repo):
    """Number of the open sync PR for this repo, or None."""
    out = run([
        "gh", "pr", "list", "-R", repo, "--head", BRANCH,
        "--state", "open", "--json", "number", "--jq", ".[0].number",
    ]).stdout.strip()
    return int(out) if out else None


def ensure_pr(repo, changed):
    """Create the sync PR if none is open; label failures are non-fatal."""
    if open_pr_number(repo) is not None:
        print("  PR already open; force-push refreshed it")
        return
    body_lines = [
        "Synced from [cplieger/ci](https://github.com/cplieger/ci) by",
        "`scripts/sync-files.py`. Files carrying a `DO NOT EDIT` header are",
        "overwritten on every sync — change the canonical copy in cplieger/ci",
        "instead. Auto-merges once this repo's required checks pass.",
        "",
        "Files updated in this run:",
        *[f"- `{path}`" for path in changed],
    ]
    create = [
        "gh", "pr", "create", "-R", repo, "--head", BRANCH,
        "--title", PR_TITLE, "--body", "\n".join(body_lines),
        "--label", PR_LABEL,
    ]
    result = run(create, check=False)
    if result.returncode != 0 and "label" in (result.stderr or "").lower():
        # Missing label must not block the sync; retry unlabelled.
        result = run(create[:-2], check=False)
    if result.returncode != 0:
        msg = f"gh pr create failed: {result.stderr.strip()}"
        raise RuntimeError(msg)
    print(f"  opened PR: {result.stdout.strip()}")


def close_stale_pr(repo):
    """No diff this run: close a leftover open sync PR whose content has
    since landed on main some other way (its diff is empty/obsolete)."""
    number = open_pr_number(repo)
    if number is None:
        return
    run([
        "gh", "pr", "close", "-R", repo, str(number), "--delete-branch",
        "--comment",
        "Closing: the target branch already contains this sync's content.",
    ], check=False)
    print(f"  closed stale sync PR #{number}")


def sync_repo(repo, dest_map, source_root, dry_run):
    """Sync one repo. Returns 'changed', 'clean', or 'dry'."""
    with tempfile.TemporaryDirectory(prefix="sync-") as tmp:
        clone_dir = Path(tmp) / "repo"
        clone(repo, clone_dir)
        run(["git", "checkout", "--quiet", "-B", BRANCH], cwd=clone_dir)
        changed = copy_files(source_root, clone_dir, dest_map)

        if not changed:
            print("  in sync (no diff)")
            if not dry_run:
                close_stale_pr(repo)
            return "clean"

        print(f"  {len(changed)} file(s) differ: {', '.join(changed)}")
        if dry_run:
            return "dry"

        run(["git", "commit", "--quiet", "-m", COMMIT_SUBJECT], cwd=clone_dir)
        run(
            ["git", "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{BRANCH}"],
            cwd=clone_dir,
        )
        ensure_pr(repo, changed)
        return "changed"


def main():
    ap = argparse.ArgumentParser(description="cplieger file-sync engine")
    ap.add_argument("--manifest", default=".github/sync.yml",
                    help="repo↔file mapping (generated by classify-repos.sh)")
    ap.add_argument("--source-dir", default=".",
                    help="root of the cplieger/ci checkout holding the sources")
    ap.add_argument("--dry-run", action="store_true",
                    help="report diffs only; no push, no PR, no close")
    ap.add_argument("--only", default="",
                    help="comma/space-separated repo names to limit the run")
    args = ap.parse_args()

    source_root = Path(args.source_dir).resolve()
    mapping = load_mapping(args.manifest)
    if args.only:
        wanted = {w.strip() for w in args.only.replace(",", " ").split() if w.strip()}
        mapping = {r: f for r, f in mapping.items() if r.split("/")[-1] in wanted}

    if not mapping:
        print("nothing to sync (empty mapping after filters)")
        return

    counts = {"changed": 0, "clean": 0, "dry": 0}
    failures = []
    for repo in sorted(mapping):
        print(f"::group::{repo}")
        try:
            outcome = sync_repo(repo, mapping[repo], source_root, args.dry_run)
            counts[outcome] += 1
        except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
            detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
            print(f"::warning::{repo}: sync failed — {detail}")
            failures.append(repo)
        print("::endgroup::")

    total = len(mapping)
    print(f"\n{total} repo(s): {counts['changed']} synced · "
          f"{counts['clean']} already in sync · {counts['dry']} with pending diffs (dry-run) · "
          f"{len(failures)} failed{' (' + ', '.join(failures) + ')' if failures else ''}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
