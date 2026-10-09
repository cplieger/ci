#!/usr/bin/env bash
# Probe for release.yaml's two-branch state machine: the extracted detect steps
# against HEAD's (legacy characterization), scripts/release-state.sh on fixture
# histories (pending promotions, numbering, receipts and repair, the dev
# barrier, renumber) with stub gh, curl and docker that refuse any argv they do
# not expect, and the wiring of release.yaml and the caller template.
# CLIFF_BIN=/path/to/git-cliff skips the download.
# shellcheck disable=SC2016 # expected values quote workflow expressions and markdown
set -euo pipefail
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=probe GIT_AUTHOR_EMAIL=probe@example.invalid
export GIT_COMMITTER_NAME=probe GIT_COMMITTER_EMAIL=probe@example.invalid
unset GITHUB_TOKEN GH_TOKEN

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE_YAML="$ROOT/.github/workflows/release.yaml"
TEMPLATE="$ROOT/.github/workflow-templates/release.yml"
RS="$ROOT/scripts/release-state.sh"
COMPUTE="$ROOT/actions/git-cliff-version/compute.sh"
WORK="$(mktemp -d /tmp/release-detect-probe.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
fail() {
  echo "FAIL: $*" >&2
  exit 1
}
chk() { # label actual expected
  if [ "$2" = "$3" ]; then
    PASS=$((PASS + 1))
    echo "ok: $1 -> $2"
  else
    fail "$1: expected '$3', got '$2'"
  fi
}
chk_has() { # label haystack needle
  case "$2" in
    *"$3"*)
      PASS=$((PASS + 1))
      echo "ok: $1"
      ;;
    *) fail "$1: output does not contain '$3'
--- output ---
$2" ;;
  esac
}
chk_lacks() { # label haystack needle
  case "$2" in
    *"$3"*) fail "$1: output contains '$3'
--- output ---
$2" ;;
    *)
      PASS=$((PASS + 1))
      echo "ok: $1"
      ;;
  esac
}

if [ -n "${CLIFF_BIN:-}" ]; then
  CLIFF="$CLIFF_BIN"
else
  VERSION=$(grep -m1 -oE 'CLIFF_VERSION=v[0-9.]+' "$RELEASE_YAML" | cut -d= -f2)
  SHA256=$(grep -m1 -oE 'CLIFF_SHA256=[a-f0-9]{64}' "$RELEASE_YAML" | cut -d= -f2)
  [ -n "$VERSION" ] && [ -n "$SHA256" ] || fail "no git-cliff pin found in $RELEASE_YAML"
  curl -fsSL --retry 7 --retry-max-time 150 --retry-all-errors -o "$WORK/git-cliff.tgz" \
    "https://github.com/orhun/git-cliff/releases/download/${VERSION}/git-cliff-${VERSION#v}-x86_64-unknown-linux-gnu.tar.gz"
  echo "${SHA256}  ${WORK}/git-cliff.tgz" | sha256sum -c -
  tar xzf "$WORK/git-cliff.tgz" -C "$WORK" --strip-components=1 "git-cliff-${VERSION#v}/git-cliff"
  CLIFF="$WORK/git-cliff"
fi
"$CLIFF" --version >/dev/null || fail "git-cliff binary unusable"

# ── Extract the subjects ─────────────────────────────────────────────────────
git -C "$ROOT" show HEAD:.github/workflows/release.yaml >"$WORK/release-head.yaml"
cp "$WORK/release-head.yaml" "$WORK/release.yaml-head"
git -C "$ROOT" show HEAD:.github/workflows/docker-release.yaml >"$WORK/docker-release.yaml-head"
git -C "$ROOT" show HEAD:.github/workflow-templates/release.yml >"$WORK/template-head.yml"
python3 - "$RELEASE_YAML" "$WORK/release-head.yaml" "$TEMPLATE" "$WORK/template-head.yml" "$WORK" <<'PY'
import json, os, re, sys, yaml

new, head, tmpl, tmpl_head, out = sys.argv[1:]
jobs = yaml.safe_load(open(new))["jobs"]
hjobs = yaml.safe_load(open(head))["jobs"]

def step(js, job, name):
    return next(s for s in js[job]["steps"] if s.get("name") == name)

for tag, js in (("new", jobs), ("head", hjobs)):
    for name, f in (("Select channel", "channel"), ("Select version", "select")):
        open(f"{out}/{f}-{tag}.sh", "w").write(step(js, "detect", name)["run"])
open(f"{out}/select-env.txt", "w").write(" ".join(sorted(step(jobs, "detect", "Select version")["env"])))
open(f"{out}/channel-env.json", "w").write(json.dumps(step(jobs, "detect", "Select channel").get("env", {})))
vp = step(jobs, "receipts", "Record completion receipts")
open(f"{out}/receipt-step.sh", "w").write(vp["run"])

facts = {}
d = {s.get("name"): s for s in jobs["detect"]["steps"]}
for name in ("Find pending promotions", "Install git-cliff for promotion numbering", "Number pending promotions",
             "Read release receipts", "Detect finalize state"):
    facts[f"if:{name}"] = d[name].get("if", "")
facts["cliff-with"] = d["Compute version (cliff)"]["with"]
facts["names"] = [s.get("name") for s in jobs["detect"]["steps"]]
for job in ("repair-notes", "repair-assets", "repair-release", "repair-publish", "repair", "barrier", "renumber", "renumber-tag", "renumber-ts", "renumber-npm", "renumber-subpackages", "docker", "go", "ts", "subpackage", "go-nested", "verify-publish", "receipts"):
    facts[f"needs:{job}"] = jobs[job].get("needs")
    facts[f"jobif:{job}"] = " ".join(str(jobs[job].get("if", "")).split())
    facts[f"perms:{job}"] = jobs[job].get("permissions")
facts["lanecliff-with"] = step(jobs, "go-nested", "Compute lane version (cliff)")["with"]
open(f"{out}/lane-select.sh", "w").write(step(jobs, "go-nested", "Select lane version")["run"])
facts["lane-select-env"] = step(jobs, "go-nested", "Select lane version")["env"]
facts["vp-steps"] = [s.get("name") for s in jobs["verify-publish"]["steps"]]
facts["receipts-step-ifs"] = [s.get("if", "") for s in jobs["receipts"]["steps"]]
facts["repair-if"] = {s.get("name"): s.get("if", "") for s in jobs["repair"]["steps"]}
facts["repair-env"] = step(jobs, "repair", "Read back and record the receipt")["env"]
facts["repair-cosign"] = jobs["repair"].get("env", {}).get("COSIGN_VERSION")
facts["barrier-env"] = step(jobs, "barrier", "Hold for dev")["env"]
open(f"{out}/barrier-step.sh", "w").write(step(jobs, "barrier", "Hold for dev")["run"])
facts["barrier-timeout"] = jobs["barrier"]["timeout-minutes"]
sys.path.insert(0, os.path.join(os.path.dirname(new), "..", "..", "scripts"))
from workflow_replay import Scope  # noqa: E402

for tag, js in (("new", jobs), ("head", hjobs)):
    for branch in ("main", "dev"):
        github = {"event": {"repository": {"default_branch": branch, "private": False, "fork": False}}}
        timeout = Scope({"github": github}, {}, []).render(js["detect"]["timeout-minutes"])
        facts[f"detect-timeout:{tag}:{branch}"] = timeout
for flag in ("private", "fork"):
    github = {"event": {"repository": {"default_branch": "dev", "private": False, "fork": False, flag: True}}}
    facts[f"detect-timeout:new:dev-{flag}"] = Scope({"github": github}, {}, []).render(
        jobs["detect"]["timeout-minutes"])
facts["renumber-timeout"] = max(jobs["renumber"]["timeout-minutes"] + jobs["renumber-tag"]["timeout-minutes"],
                               jobs["renumber-ts"]["timeout-minutes"] + jobs["renumber-npm"]["timeout-minutes"]
                               ) + jobs["renumber-subpackages"]["timeout-minutes"]
notes = {
    "go": step(jobs, "go", "Generate release notes"),
    "ts": step(jobs, "ts", "Generate release notes"),
    "lane": step(jobs, "go-nested", "Generate lane release notes"),
    "repair": step(jobs, "repair-notes", "Render the missing Release notes"),
    "pending": d["Find pending promotions"],
    "number": d["Number pending promotions"],
    "publish": d["Add pending promotions to the publication"],
}
facts["token-free"] = {k: sorted(set(s.get("env", {})) & {"GH_TOKEN", "GITHUB_TOKEN"}) for k, s in notes.items()}
facts["renders"] = {k: "render-notes.sh" in s["run"] for k, s in notes.items() if k not in ("pending", "number", "publish")}
facts["outputs"] = jobs["detect"]["outputs"]
facts["if:publish"] = " ".join(d["Add pending promotions to the publication"].get("if", "").split())
facts["select-env"] = step(jobs, "detect", "Select version")["env"]
facts["publish-after"] = facts["names"].index("Detect changed paths") < facts["names"].index(
    "Add pending promotions to the publication") < facts["names"].index("Select version")
facts["renumber-steps"] = [s.get("name") for s in jobs["renumber"]["steps"]]
facts["renumber-if"] = {s.get("name"): s.get("if", "") for s in jobs["renumber"]["steps"]}
facts["renumber-tag-steps"] = [s.get("name") for s in jobs["renumber-tag"]["steps"]]
facts["renumber-tag-if"] = {s.get("name"): s.get("if", "") for s in jobs["renumber-tag"]["steps"]}
facts["renumber-tag-env"] = {s.get("name"): s.get("env", {}) for s in jobs["renumber-tag"]["steps"]}
facts["renumber-upload"] = step(jobs, "renumber", "Upload the handoff")["with"]
facts["renumber-fetch"] = step(jobs, "renumber-tag", "Fetch the handoff")["with"]
open(f"{out}/renumber-write.sh", "w").write(step(jobs, "renumber", "Write the handoff")["run"])
open(f"{out}/renumber-read.sh", "w").write(step(jobs, "renumber-tag", "Read the handoff")["run"])
facts["repair-release-steps"] = [s.get("name") for s in jobs["repair-release"]["steps"]]
facts["repair-upload"] = step(jobs, "repair-notes", "Hand the notes on")["with"]
facts["repair-fetch"] = step(jobs, "repair-release", "Fetch the notes")["with"]
facts["repair-render-out"] = "--out \"$RUNNER_TEMP/notes/NOTES.md\"" in step(jobs, "repair-notes", "Render the missing Release notes")["run"]
pub = step(jobs, "repair-release", "Publish the missing Release")
facts["repair-publish-run"] = pub["run"]
open(f"{out}/repair-release-publish.sh", "w").write(pub["run"])
facts["repair-release-publish"] = {"if": pub.get("if", ""), **pub["env"]}
fa = step(jobs, "repair-release", "Fetch the assets")
facts["repair-fetch-assets"] = {"if": fa.get("if", ""), **fa["with"]}
facts["repair-assets-call"] = {"uses": jobs["repair-assets"]["uses"], "matrix": jobs["repair-assets"]["strategy"]["matrix"],
                               **jobs["repair-assets"]["with"]}
for job in ("repair-notes", "repair-release"):
    open(f"{out}/{job}-check.sh", "w").write(step(jobs, job, "Check the Release")["run"])
facts["repair-steps"] = [s.get("name") for s in jobs["repair"]["steps"]]
open(f"{out}/repair-complete.sh", "w").write(step(jobs, "repair-publish", "Complete the subpackages")["run"])
open(f"{out}/repair-readback.sh", "w").write(step(jobs, "repair", "Read back and record the receipt")["run"])
facts["repair-complete-env"] = step(jobs, "repair-publish", "Complete the subpackages")["env"]
facts["repair-publish-steps"] = [s.get("name") for s in jobs["repair-publish"]["steps"]]
facts["repair-publish-matrix"] = jobs["repair-publish"]["strategy"]["matrix"]["include"]
gate = step(jobs, "repair", "Require the Release and the subpackages")
open(f"{out}/repair-gate.sh", "w").write(gate["run"])
facts["repair-gate-env"] = gate["env"]
open(f"{out}/renumber-step.sh", "w").write(step(jobs, "renumber-tag", "Renumber")["run"])
open(f"{out}/renumber-npm-step.sh", "w").write(step(jobs, "renumber-npm", "Renumber")["run"])
open(f"{out}/renumber-ts-step.sh", "w").write(step(jobs, "renumber-ts", "Hand the version on")["run"])
facts["renumber-matrix-outputs"] = "outputs" in jobs["renumber"]
facts["renumber-outputs"] = jobs["renumber-ts"]["outputs"]
facts["renumber-ts-steps"] = [s.get("name") for s in jobs["renumber-ts"]["steps"]]
facts["renumber-npm-steps"] = [s.get("name") for s in jobs["renumber-npm"]["steps"]]
facts["renumber-npm-env"] = step(jobs, "renumber-npm", "Renumber")["env"]
facts["renumber-npm-target-env"] = step(jobs, "renumber-npm", "Check out the numbered build")["env"]
facts["rsub-steps"] = [s.get("name") for s in jobs["renumber-subpackages"]["steps"]]
facts["rsub-if"] = {s.get("name"): s.get("if", "") for s in jobs["renumber-subpackages"]["steps"]}
facts["rsub-env"] = {s.get("name"): s.get("env", {}) for s in jobs["renumber-subpackages"]["steps"]}
open(f"{out}/rsub-publish.sh", "w").write(step(jobs, "renumber-subpackages", "Publish the subpackages")["run"])

# Every job that executes the repository's cliff config (git-cliff, the compute
# action or render-notes.sh), and which of them hold OIDC.
def runs_config(s):
    return any(k in (s.get("run") or "") + str(s.get("uses") or "") for k in ("git-cliff", "render-notes.sh"))
census = []
for wf in ("release.yaml", "docker-release.yaml"):
    w = yaml.safe_load(open(new.replace("release.yaml", wf)))
    for name, job in w["jobs"].items():
        perms = job.get("permissions", w.get("permissions")) or {}
        if perms.get("id-token") == "write" and any(runs_config(s) for s in job.get("steps", [])):
            census.append(f"{wf}:{name}")
facts["oidc-config"] = sorted(census)
hcensus = []
for wf in ("release.yaml", "docker-release.yaml"):
    w = yaml.safe_load(open(f"{out}/{wf}-head"))
    for name, job in w["jobs"].items():
        perms = job.get("permissions", w.get("permissions")) or {}
        if perms.get("id-token") == "write" and any(runs_config(s) for s in job.get("steps", [])):
            hcensus.append(f"{wf}:{name}")
facts["oidc-config-head"] = sorted(hcensus)
# A step can reach every later step of its job through GITHUB_PATH or
# GITHUB_ENV, so a job that runs the cliff config is judged by every scope it
# holds, not by which step is handed the token.
def write_config(w):
    hits = []
    for name, job in w["jobs"].items():
        perms = job.get("permissions", w.get("permissions")) or {}
        if "write" in perms.values() and any(runs_config(s) for s in job.get("steps", [])):
            hits.append(name)
    return hits
facts["write-config"] = sorted(f"{wf}:{n}" for wf in ("release.yaml", "docker-release.yaml")
                               for n in write_config(yaml.safe_load(open(new.replace("release.yaml", wf)))))
facts["write-config-head"] = sorted(f"{wf}:{n}" for wf in ("release.yaml", "docker-release.yaml")
                                    for n in write_config(yaml.safe_load(open(f"{out}/{wf}-head"))))

# A job condition evaluated as the runner would, for the operators these use.
def ev(expr, res, outs):
    py = []
    for t in re.findall(r"\$\{\{|\}\}|always\(\)|cancelled\(\)|needs\.[A-Za-z0-9_-]+\.(?:result|outputs\.[A-Za-z0-9_]+)"
                        r"|'[^']*'|==|!=|&&|\|\||!|\(|\)|\S+", str(expr)):
        if t in ("${{", "}}"):
            continue
        if t in ("always()", "cancelled()"):
            py.append(str(t == "always()"))
        elif t.startswith("needs."):
            _, j, kind, *rest = t.split(".")
            if kind == "result":
                py.append(repr(res[j]))
            elif j == "detect":
                py.append(repr(outs.get(rest[0], "")))
            else:
                raise SystemExit(f"unsupported output read {t} in {expr!r}")
        elif t.startswith("'"):
            py.append(repr(t[1:-1]))
        elif t in ("==", "!=", "(", ")"):
            py.append(t)
        elif t in ("&&", "||", "!"):
            py.append({"&&": " and ", "||": " or ", "!": " not "}[t])
        else:
            raise SystemExit(f"unsupported token {t!r} in {expr!r}")
    return eval("".join(py))
# Every job that can write a registry, a tag or a Release on a normal stable
# run. The repair jobs are the repair itself, and the renumber jobs need
# mode=renumber.
EXEMPT = {"repair-notes", "repair-assets", "repair-release", "repair-publish", "renumber", "renumber-tag", "renumber-ts", "renumber-npm",
          "renumber-subpackages"}
writers = [n for n, j in jobs.items() if n not in EXEMPT and (
    str(j.get("uses", "")).endswith("docker-release.yaml")
    or {k for k, v in (j.get("permissions") or {}).items() if v == "write"} & {"id-token", "packages", "contents"})]
facts["stable-writers"] = writers
def simulate(gates, outs):
    res = {"detect": "success", **gates}
    for n in writers:
        if not re.search(r"always\(\)|cancelled\(\)", str(jobs[n].get("if", ""))):
            raise SystemExit(f"{n}'s condition has no status function; simulate it as success()")
        res[n] = "success" if ev(jobs[n].get("if", ""), res, outs) else "skipped"
    return " ".join(n for n in writers if res[n] == "success")
sim = {}
for t in ("docker", "go", "ts"):
    outs = {"type": t, "channel": "stable", "release_model": "two-branch", "mode": "normal", "release": "true",
            "finalize": "false", "root_changed": "true", "subpackages_to_publish": '["web"]', "go_modules_to_release": '["yamlenv"]'}
    for r, b in (("success", "success"), ("skipped", "skipped"), ("failure", "skipped"), ("skipped", "failure"), ("cancelled", "skipped")):
        sim[f"{t} repair={r} barrier={b}"] = simulate({"repair": r, "barrier": b}, outs)
facts["gate-sim"] = sim
rsub = {}
for tag_r, npm_r, mode, subs in (("success", "skipped", "renumber", '["web"]'), ("skipped", "success", "renumber", '["web"]'),
                                 ("failure", "skipped", "renumber", '["web"]'), ("success", "failure", "renumber", '["web"]'),
                                 ("success", "skipped", "normal", '["web"]'), ("success", "skipped", "renumber", "[]")):
    res = {"detect": "success", "renumber-tag": tag_r, "renumber-npm": npm_r}
    rsub[f"{tag_r} {npm_r} {mode} {subs}"] = ev(jobs["renumber-subpackages"]["if"], res, {"mode": mode, "subpackages": subs})
facts["rsub-gate"] = rsub
# Legacy runs skip both gates, so the subpackage job must decide as HEAD's did.
import itertools
mism = []
for d, ts_, g, rel, fin, subs in itertools.product(*[("success", "skipped", "failure", "cancelled")] * 3,
                                                   ("true", "false"), ("true", "false"), ("[]", '["web"]')):
    res = {"detect": "success", "repair": "skipped", "barrier": "skipped", "docker": d, "ts": ts_, "go": g}
    outs = {"release": rel, "finalize": fin, "subpackages_to_publish": subs}
    if ev(jobs["subpackage"]["if"], res, outs) != ev(hjobs["subpackage"]["if"], res, outs):
        mism.append(f"{d}/{ts_}/{g}/{rel}/{fin}/{subs}")
facts["subpkg-legacy-mismatch"] = mism
facts["subpkg-legacy-cases"] = 4 ** 3 * 8
# A matrix job's same-named outputs are last-writer-wins across its legs, so
# no job may read a value off one.
mcons = []
for wf in ("release.yaml", "docker-release.yaml"):
    path = new.replace("release.yaml", wf)
    w = yaml.safe_load(open(path))
    matrixed = {n for n, j in w["jobs"].items() if "matrix" in (j.get("strategy") or {})}
    mcons += [f"{wf}:{n}" for n in sorted(set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.outputs", open(path).read())) & matrixed)]
facts["matrix-output-reads"] = mcons
sp_new, sp_head = step(jobs, "subpackage", "Publish subpackage to npm + JSR"), step(hjobs, "subpackage", "Publish subpackage to npm + JSR")
open(f"{out}/subpkg-new.sh", "w").write(sp_new["run"])
open(f"{out}/subpkg-head.sh", "w").write(sp_head["run"])
facts["subpkg-env"] = sp_new["env"]
facts["subpkg-steps"] = [s.get("name") for s in jobs["subpackage"]["steps"]]
facts["docker-with"] = jobs["docker"]["with"]
dr = yaml.safe_load(open(new.replace("release.yaml", "docker-release.yaml")))
facts["dr-receipt-if"] = " ".join(str(dr["jobs"]["receipt"]["if"]).split())
facts["dr-defer"] = dr.get("on", dr.get(True))["workflow_call"]["inputs"]["defer-receipt"]
src = open(new).read()
facts["jsr-pins"] = sorted(set(re.findall(r"JSR_VERSION[=:] *([0-9.]+)", src)))
facts["jsr-pin-sites"] = len(re.findall(r"# renovate: datasource=npm depName=jsr\n *JSR_VERSION[=:] *[0-9.]+", src))

t, th = yaml.safe_load(open(tmpl)), yaml.safe_load(open(tmpl_head))
trig = t.get("on", t.get(True))
facts["tmpl-push"] = trig["push"]["branches"]
facts["tmpl-inputs"] = sorted(trig["workflow_dispatch"]["inputs"])
facts["tmpl-mode"] = trig["workflow_dispatch"]["inputs"]["mode"]
facts["tmpl-with"] = "with" in t["jobs"]["release"]
facts["tmpl-uses"] = bool(re.fullmatch(r"cplieger/ci/\.github/workflows/release\.yaml@[0-9a-f]{40}",
                                       t["jobs"]["release"]["uses"])) and "# v3\n" in open(tmpl).read()
facts["tmpl-perms"] = t["jobs"]["release"]["permissions"]
for key, job, name in (("go", "go", "Tag + GitHub Release"), ("ts", "ts", "Tag + GitHub Release"),
                       ("lane", "go-nested", "Tag + GitHub Release (lane)")):
    open(f"{out}/publish-{key}.sh", "w").write(step(jobs, job, name)["run"])
    names = [s.get("name") for s in jobs[job]["steps"]]
    ci_src = step(jobs, job, "Check out the ci source")
    facts[f"publish-tools:{key}"] = "|".join((
        step(jobs, job, name)["env"].get("CI_TOOLS", ""), ci_src["with"]["path"],
        str(names.index("Check out the ci source") < names.index(name)), ci_src.get("if", "")))
    facts[f"publish-model:{key}"] = step(jobs, job, name)["env"].get("RELEASE_MODEL", "")
facts["release-view-sites"] = " ".join(str(open(new.replace("release.yaml", wf)).read().count("gh release view"))
                                       for wf in ("release.yaml", "docker-release.yaml"))
# A workflow cannot declare a job conditionally, so a legacy run's page lists every
# v3-only job; each must evaluate as skipped there.
V3_ONLY_JOBS = {
    "release.yaml": ["barrier", "receipts", "renumber", "renumber-npm", "renumber-subpackages", "renumber-tag",
                     "renumber-ts", "repair", "repair-assets", "repair-notes", "repair-publish", "repair-release"],
    "docker-release.yaml": ["receipt", "repair-assets"],
}
legacy_new = {}
for wf in ("release.yaml", "docker-release.yaml"):
    n_jobs = yaml.safe_load(open(new.replace("release.yaml", wf)))["jobs"]
    legacy_new[wf] = sorted(n for n in V3_ONLY_JOBS[wf] if n in n_jobs)
facts["legacy-new-jobs"] = legacy_new
lruns = {}
for t in ("docker", "go", "ts", "none"):
    for subs in ("[]", '["web"]'):
        outs = {"type": t, "channel": "stable", "release_model": "legacy", "mode": "normal", "release": "true",
                "finalize": "false", "subpackages": subs, "subpackages_to_publish": subs}
        res = {**{n: "success" for n in hjobs}, **{n: "skipped" for n in legacy_new["release.yaml"]}}
        lruns[f"{t} {subs}"] = [n for n in legacy_new["release.yaml"] if ev(
            re.sub(r"needs\.(?!detect\.)[A-Za-z0-9_-]+\.outputs\.[A-Za-z0-9_]+", "''", str(jobs[n].get("if", ""))), res, outs)]
facts["legacy-new-jobs-run"] = lruns
facts["repair-tag-callers"] = sorted(n for n, j in jobs.items()
                                     if str(j.get("uses", "")).endswith("docker-release.yaml") and "repair-tag" in (j.get("with") or {}))
facts["dr-repair-assets-if"] = " ".join(str(dr["jobs"]["repair-assets"]["if"]).split())
json.dump(facts, open(f"{out}/facts.json", "w"))
PY
fact() { jq -r "$1" "$WORK/facts.json"; }

# ── Select channel: legacy outputs are HEAD's, plus the two model keys ──────
channel_out() { # <head|new> <ref> <default branch> <mode> [private] [fork] -> the step's outputs, or EXIT=n
  : >"$WORK/out"
  GITHUB_REF="$2" DEFAULT_BRANCH="$3" INPUT_MODE="$4" REPO_PRIVATE="${5:-false}" REPO_FORK="${6:-false}" \
    GITHUB_OUTPUT="$WORK/out" \
    bash "$WORK/channel-$1.sh" >/dev/null 2>"$WORK/channel.err" || {
    echo "EXIT=$?"
    return 0
  }
  cat "$WORK/out"
}
for ref in refs/heads/main refs/heads/dev refs/heads/feature; do
  for mode in "" normal renumber junk; do
    # Both sides drop the model keys: a pre-v3 HEAD emits neither, and a landed HEAD is this file.
    chk "C1 legacy ${ref#refs/heads/} mode='${mode}' matches HEAD" \
      "$(channel_out new "$ref" main "$mode" | grep -v -e '^release_model=' -e '^mode=')" \
      "$(channel_out head "$ref" main "$mode" | grep -v -e '^release_model=' -e '^mode=')"
    chk "C1 legacy ${ref#refs/heads/} mode='${mode}' adds legacy/normal" \
      "$(channel_out new "$ref" main "$mode" | grep -e '^release_model=' -e '^mode=' | tr '\n' ' ')" \
      "release_model=legacy mode=normal "
  done
done
chk "C2 a dev default branch selects two-branch" "$(channel_out new refs/heads/main dev "" | tr '\n' ' ')" \
  "channel=stable release_model=two-branch mode=normal "
chk "C2 renumber on dev" "$(channel_out new refs/heads/dev dev renumber | tr '\n' ' ')" \
  "channel=dev release_model=two-branch mode=renumber "
for flags in "true false" "false true"; do
  # shellcheck disable=SC2086 # the two flags are two words
  chk "C2 a private or forked dev-default repo stays legacy (private fork: ${flags})" \
    "$(channel_out new refs/heads/main dev "" $flags | tr '\n' ' ')" "channel=stable release_model=legacy mode=normal "
done
chk "C3 renumber on main is refused" "$(channel_out new refs/heads/main dev renumber | tail -1)" "EXIT=1"
chk "C3 an unknown mode is refused" "$(channel_out new refs/heads/dev dev junk | tail -1)" "EXIT=1"
chk "C4 the mode is read off the event, never inputs" "$(jq -r .INPUT_MODE "$WORK/channel-env.json")" \
  '${{ github.event.inputs.mode }}'
chk "C4 release.yaml reads no inputs.mode but the event's" \
  "$(grep -o '[a-z.]*inputs\.mode' "$RELEASE_YAML" | sort -u | tr '\n' ' ')" "github.event.inputs.mode "
chk "C4 the model is read off the default branch" "$(jq -r .DEFAULT_BRANCH "$WORK/channel-env.json")" \
  '${{ github.event.repository.default_branch }}'
chk "C4 and off the repository's visibility and fork flag" \
  "$(jq -r '.REPO_PRIVATE + " " + .REPO_FORK' "$WORK/channel-env.json")" \
  '${{ github.event.repository.private }} ${{ github.event.repository.fork }}'

# ── Select version: legacy outputs are HEAD's ───────────────────────────────
select_out() { # <head|new> <channel> <mode> <root_changed> <subs> <anchor> <base> -> outputs
  : >"$WORK/out"
  CHANNEL="$2" MODE="$3" ROOT_CHANGED="$4" SUBPACKAGES_TO_PUBLISH="$5" ANCHOR_SHA="$6" GITHUB_SHA=head \
    BASE="$7" DEV_VERSION="$7-dev.1" FLOOR_BASE=v1.2.1 FLOOR_DEV_VERSION=v1.2.1-dev.1 LATEST=v1.2.0 \
    GITHUB_OUTPUT="$WORK/out" bash "$WORK/select-$1.sh" >/dev/null 2>&1 || echo "EXIT=$?"
  cat "$WORK/out"
}
n=0
for ch in stable dev; do
  for rc in true false; do
    for subs in '[]' '["web"]'; do
      for anchor in head older; do
        for base in v1.3.0 v1.2.0 ""; do
          a=$(select_out new "$ch" normal "$rc" "$subs" "$anchor" "$base")
          b=$(select_out head "$ch" normal "$rc" "$subs" "$anchor" "$base")
          [ "$a" = "$b" ] || fail "C5 Select version differs from HEAD for $ch $rc $subs $anchor '$base': '$a' vs '$b'"
          n=$((n + 1))
        done
      done
    done
  done
done
chk "C5 Select version equals HEAD's over the legacy grid" "$n" "48"
chk "C6 renumber publishes nothing" "$(select_out new dev renumber true '[]' older v1.3.0 | sed -n 's/^release=//p')" "false"
chk "C6 renumber still names the dev version" "$(select_out new dev renumber true '[]' older v1.3.0 | sed -n 's/^version=//p')" "v1.3.0-dev.1"
chk_has "C6 Select version reads the mode" "$(cat "$WORK/select-env.txt")" "MODE"
chk "C7 a main-default detect keeps HEAD's timeout" "$(fact '."detect-timeout:new:main"')" "$(fact '."detect-timeout:head:main"')"
chk "C7 the legacy detect timeout is 3 minutes" "$(fact '."detect-timeout:new:main"')" "3"
chk "C7 a dev-default detect has 5 minutes" "$(fact '."detect-timeout:new:dev"')" "5"
chk "C7 a private dev-default detect keeps 3 minutes" "$(fact '."detect-timeout:new:dev-private"')" "3"
chk "C7 a forked dev-default detect keeps 3 minutes" "$(fact '."detect-timeout:new:dev-fork"')" "3"

# ── Stubs ────────────────────────────────────────────────────────────────────
BIN="$WORK/bin"
export GH_DIR="$WORK/gh" GH_LOG="$WORK/gh.log" CURL_DIR="$WORK/curl" DOCKER_LOG="$WORK/docker.log"
export COSIGN_DIR="$WORK/cosign" NPM_DIR="$WORK/npm"
mkdir -p "$BIN" "$GH_DIR" "$CURL_DIR" "$COSIGN_DIR" "$NPM_DIR"
cat >"$BIN/gh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$GH_LOG"
[ "${1:-}" = api ] || { echo "stub: unexpected gh $*" >&2; exit 22; }
shift
jqf="" ep="" method=GET fields=0 paginate=0 hdr="" fl=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --jq) jqf=$2; shift 2 ;;
    -X) method=$2; shift 2 ;;
    -H) hdr=$2; shift 2 ;;
    -f | -F) fields=1 fl="$fl $2"; shift 2 ;;
    --paginate) paginate=1; shift ;;
    *) ep=$1; shift ;;
  esac
done
if [ "$method" = GET ] && [ "$fields" = 1 ]; then method=POST; fi
# A FILE.p2 beside a page is its second page, served only to --paginate; the
# filter applies per page, as gh applies it.
serve() {
  local f pages=("$1")
  if [ "$paginate" = 1 ] && [ -f "$1.p2" ]; then pages+=("$1.p2"); fi
  for f in "${pages[@]}"; do
    if [ -n "$jqf" ]; then jq -r "$jqf" "$f"; else cat "$f"; fi
  done
}
nf() { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
R=repos/o/app
case "$method $ep" in
  "GET $R/commits/"*"/statuses?per_page=100")
    [ ! -f "$GH_DIR/fail-statuses" ] || { echo "gh: HTTP 502" >&2; exit 1; }
    sha=${ep#"$R"/commits/}
    f="$GH_DIR/statuses-${sha%%/*}.json"
    [ -f "$f" ] || { echo '[]' >"$GH_DIR/empty.json"; f="$GH_DIR/empty.json"; }
    serve "$f" ;;
  "GET $R/pulls?state=closed&base="*"&sort=updated&direction=desc&per_page=100&page="*)
    p=${ep##*&page=} b=${ep#*&base=}
    b=${b%%&*}
    case "$b" in main) f="$GH_DIR/pulls.json" ;; dev) f="$GH_DIR/pulls-dev.json" ;; *) echo "stub: base $b" >&2; exit 22 ;; esac
    [ "$p" = 1 ] || f="$f.p$p"
    if [ -f "$f" ]; then cat "$f"; else echo '[]'; fi ;;
  "GET $R/releases/tags/"*)
    [ ! -f "$GH_DIR/fail-release" ] || { echo "gh: HTTP 502" >&2; exit 1; }
    tag=${ep#"$R"/releases/tags/}
    [ -f "$GH_DIR/release-${tag//\//%}" ] || nf
    echo '{}' ;;
  "POST $R/statuses/"*) [ ! -f "$GH_DIR/fail-post" ] || exit 1 ;;
  # A dispatch answers with the run $GH_DIR/on-dispatch (renumber) or
  # $GH_DIR/on-rebuild (normal) names in $GH_DIR/dispatched-id, else with none.
  # API version 2026-03-10 always answers with the run; the default version
  # answers 204, an empty body, unless return_run_details is true.
  "POST $R/actions/workflows/release.yaml/dispatches")
    details=0
    case "$fl" in *" return_run_details=true"*) details=1 fl=${fl/ return_run_details=true/} ;; esac
    case "$fl" in
      " ref=dev inputs[mode]=renumber") hook=on-dispatch ;;
      " ref=dev") hook=on-rebuild ;;
      *) echo "stub: unexpected dispatch fields$fl" >&2; exit 22 ;;
    esac
    if [ -x "$GH_DIR/$hook" ]; then "$GH_DIR/$hook"; fi
    id=$(cat "$GH_DIR/dispatched-id" 2>/dev/null || true)
    rm -f "$GH_DIR/dispatched-id"
    [ "$hdr" = "X-GitHub-Api-Version: 2026-03-10" ] || [ "$details" = 1 ] || exit 0
    jq -n --arg id "$id" 'if $id == "" then {} else {workflow_run_id: ($id | tonumber)} end' | jq -r "$jqf" ;;
  # Without $GH_DIR/head-runs.json, dev's head has its own finished push run.
  # $GH_DIR/head-runs-later.json replaces it after HEAD_BUSY_CALLS reads (1),
  # running $GH_DIR/on-head-later once when first served.
  "GET $R/actions/workflows/release.yaml/runs?branch=dev&per_page=100&head_sha="*)
    [ ! -f "$GH_DIR/fail-head-runs" ] || { echo "gh: HTTP 502" >&2; exit 1; }
    n=$(($(cat "$GH_DIR/head-calls" 2>/dev/null || echo 0) + 1))
    echo "$n" >"$GH_DIR/head-calls"
    f="$GH_DIR/head-runs.json"
    if [ -f "$GH_DIR/head-runs-later.json" ] && [ "$n" -gt "${HEAD_BUSY_CALLS:-1}" ]; then
      f="$GH_DIR/head-runs-later.json"
      if [ -x "$GH_DIR/on-head-later" ]; then
        "$GH_DIR/on-head-later"
        rm "$GH_DIR/on-head-later"
      fi
    fi
    [ -f "$f" ] || { echo '{"workflow_runs":[{"id":60,"status":"completed","conclusion":"success","event":"push"}]}' >"$GH_DIR/head-default.json"; f="$GH_DIR/head-default.json"; }
    serve "$f" ;;
  # One poll queries requested first, so that query counts the polls; each
  # status query sees only the runs in that status, as GitHub's filter serves
  # them. With $GH_DIR/runs-seq ("<from query> <file>" lines) the served file
  # changes between single queries instead, counted in runs-queries.
  "GET $R/actions/workflows/release.yaml/runs?branch=dev&per_page=100&status="*)
    [ ! -f "$GH_DIR/hang-runs" ] || exec sleep 600
    st=${ep##*&status=}
    if [ "$st" = requested ]; then
      echo "$(($(cat "$GH_DIR/runs-calls" 2>/dev/null || echo 0) + 1))" >"$GH_DIR/runs-calls"
    fi
    n=$(cat "$GH_DIR/runs-calls" 2>/dev/null || echo 0)
    if [ "$n" -le "${GH_BUSY_CALLS:-0}" ]; then f="$GH_DIR/runs-busy.json"; else f="$GH_DIR/runs-idle.json"; fi
    # $GH_DIR/on-idle runs once, when the busy runs first read as finished.
    if [ "$f" = "$GH_DIR/runs-idle.json" ] && [ -x "$GH_DIR/on-idle" ]; then
      "$GH_DIR/on-idle"
      rm "$GH_DIR/on-idle"
    fi
    if [ -f "$GH_DIR/runs-seq" ]; then
      q=$(($(cat "$GH_DIR/runs-queries" 2>/dev/null || echo 0) + 1))
      echo "$q" >"$GH_DIR/runs-queries"
      f=$(awk -v q="$q" '$1 <= q { f = $2 } END { print f }' "$GH_DIR/runs-seq")
    fi
    pages=("$f")
    if [ "$paginate" = 1 ] && [ -f "$f.p2" ]; then pages+=("$f.p2"); fi
    for f in "${pages[@]}"; do
      jq --arg s "$st" '.workflow_runs |= map(select(.status == $s))' "$f" | jq -r "$jqf"
    done ;;
  "GET $R/actions/runs/"*) serve "$GH_DIR/run-${ep##*/}.json" ;;
  "GET $R/git/ref/tags/"*)
    tag=${ep#"$R"/git/ref/tags/}
    [ -f "$GH_DIR/tag-${tag//\//%}" ] || nf
    cat "$GH_DIR/tag-${tag//\//%}" ;;
  "POST $R/git/refs") [ ! -f "$GH_DIR/fail-ref" ] || exit 1 ;;
  "DELETE $R/git/refs/tags/"*) ;;
  *) echo "stub: unexpected gh $method $ep" >&2; exit 22 ;;
esac
SH
cat >"$BIN/curl" <<'SH'
#!/usr/bin/env bash
out="" url="" head=0 nofail=0 fmt=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -w) fmt=$2; shift 2 ;;
    -H | --connect-timeout | --max-time | --retry) shift 2 ;;
    -fsSI) head=1; shift ;;
    -sSI) head=1 nofail=1; shift ;;
    -sS) nofail=1; shift ;;
    http*) url=$1; shift ;;
    *) shift ;;
  esac
done
printf '%s\n' "$url" >>"$CURL_DIR/log"
emit() { if [ -n "$out" ] && [ "$out" != /dev/null ]; then printf '%s' "$1" >"$out"; else printf '%s' "$1"; fi; }
# Without -f an HTTP error is an answer, not a failure: a status line or -w's code.
answer() { # <code> [body]
  emit "${2:-}"
  if [ -n "$fmt" ]; then printf '%b' "${fmt//'%{http_code}'/$1}"; fi
  exit 0
}
manifest() { # <file prefix>
  ref=${url##*/manifests/}
  [ "$head" = 1 ] || exit 22
  if [ -f "$CURL_DIR/$1-$ref" ]; then
    printf 'HTTP/2 200\r\nDocker-Content-Digest: %s\r\n\r\n' "$(cat "$CURL_DIR/$1-$ref")"
  elif [ "$nofail" = 1 ] && [ -f "$CURL_DIR/status-$ref" ]; then
    printf 'HTTP/2 %s\r\n\r\n' "$(cat "$CURL_DIR/status-$ref")"
  elif [ "$nofail" = 1 ]; then
    printf 'HTTP/2 404\r\n\r\n'
  else
    exit 22
  fi
}
case "$url" in
  https://ghcr.io/token\?scope=repository:o/app:pull | https://ghcr.io/token\?scope=repository:o/app/dashboard:pull \
    | "https://auth.docker.io/token?service=registry.docker.io&scope=repository:o/app:pull") emit '{"token":"stub"}' ;;
  https://ghcr.io/v2/o/app/manifests/*) manifest manifest ;;
  https://ghcr.io/v2/o/app/dashboard/manifests/*) manifest dash-manifest ;;
  https://registry-1.docker.io/v2/o/app/manifests/*) manifest hub-manifest ;;
  *)
    # $CURL_DIR/docs holds "<url> <body>" lines, $CURL_DIR/down URLs that do not connect.
    if grep -qxF "$url" "$CURL_DIR/down" 2>/dev/null; then exit 7; fi
    doc=$(awk -v u="$url" '$1 == u { sub(/^[^ ]+ /, ""); print; exit }' "$CURL_DIR/docs" 2>/dev/null)
    if [ -n "$doc" ]; then
      answer 200 "$doc"
    elif grep -qxF "$url" "$CURL_DIR/ok" 2>/dev/null; then
      if [ "$nofail" = 1 ]; then answer 200 '{}'; fi
      emit '{}'
    elif [ "$nofail" = 1 ]; then
      answer 404 '{"error":"Not found"}'
    else
      exit 22
    fi
    ;;
esac
SH
cat >"$BIN/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$DOCKER_LOG"
[ "$1 $2 $3" = "buildx imagetools create" ] && [ "$4" = --metadata-file ] && [ "$6" = -t ] && [ "$#" -eq 8 ] ||
  { echo "stub: unexpected docker $*" >&2; exit 22; }
printf '{"containerimage.descriptor":{"digest":"%s"}}' "${DOCKER_DIGEST:-${8##*@}}" >"$5"
SH
# Verifies a signature only against the identity release-state.sh must
# require, and only for a "<digest> <commit>" line in $COSIGN_DIR/signed
# ($COSIGN_DIR/attested for an SPDX SBOM attestation), answering a mismatch
# as cosign does. $COSIGN_DIR/flaky holds a count of calls to fail first, and
# $COSIGN_DIR/down fails every call, with a transport error.
cat >"$BIN/cosign" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$COSIGN_DIR/log"
id='--certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity-regexp ^https://github\.com/cplieger/ci/\.github/workflows/docker-release\.yaml@ --certificate-github-workflow-repository o/app --certificate-github-workflow-sha'
case "$*" in
  "verify $id "*) n=10 f=signed ;;
  "verify-attestation --type spdxjson $id "*) n=12 f=attested ;;
  *) echo "stub: unexpected cosign $*" >&2; exit 22 ;;
esac
[ "$#" -eq "$n" ] || exit 22
flaky=$(cat "$COSIGN_DIR/flaky" 2>/dev/null || echo 0)
if [ -f "$COSIGN_DIR/down" ] || [ "$flaky" -gt 0 ]; then
  echo "$((flaky - 1))" >"$COSIGN_DIR/flaky"
  printf 'Error: getting signatures: Get "https://ghcr.io/v2/": read: connection reset by peer\nerror during command execution: getting signatures: connection reset by peer\n' >&2
  exit 1
fi
ref=${*: -1} sha=${*: -2:1}
grep -qxF "${ref##*@} $sha" "$COSIGN_DIR/$f" 2>/dev/null && exit 0
printf 'Error: no matching signatures: expected GitHub Workflow SHA not found in certificate\nmain.go:74: error during command execution: no matching signatures: expected GitHub Workflow SHA not found in certificate\n' >&2
exit 1
SH
# npm: `publish` makes the package readable at the curl stub unless
# $NPM_DIR/silent exists; what npm already serves is the curl stub's.
cat >"$BIN/npm" <<'SH'
#!/usr/bin/env bash
printf 'npm %s\n' "$*" >>"$GH_LOG"
case "$*" in
  "pkg set version="*) echo "${3#version=}" >"$NPM_DIR/version" ;;
  "publish --access public --tag dev")
    [ -f "$NPM_DIR/silent" ] || echo "https://registry.npmjs.org/$(jq -r .name package.json)/$(cat "$NPM_DIR/version")" >>"$CURL_DIR/ok" ;;
  *) echo "stub: unexpected npm $*" >&2; exit 22 ;;
esac
SH
chmod 755 "$BIN/gh" "$BIN/curl" "$BIN/docker" "$BIN/cosign" "$BIN/npm"
export PATH="$BIN:$PATH" GITHUB_REPOSITORY=o/app GITHUB_SERVER_URL=https://gh GITHUB_RUN_ID=7 READBACK_SLEEP=0 POLL_SECONDS=0

# ── Fixture: a two-branch Go repo with a nested lane ─────────────────────────
O="$WORK/origin"
git init -q -b main "$O"
commit_in() { # <repo> <message> [file=content ...]
  local repo="$1" msg="$2" kv
  shift 2
  for kv in "$@"; do
    mkdir -p "$(dirname "$repo/${kv%%=*}")"
    printf '%s\n' "${kv#*=}" >"$repo/${kv%%=*}"
    git -C "$repo" add "${kv%%=*}"
  done
  git -C "$repo" commit -q --allow-empty -m "$msg"
  git -C "$repo" rev-parse HEAD
}
promote() { # <T> [subject] -> R on main: tree T's, parents [main, T]
  local m r
  m=$(git -C "$O" rev-parse main)
  r=$(GIT_COMMITTER_DATE=2026-10-01T12:00:00Z git -C "$O" commit-tree "$1^{tree}" -p "$m" -p "$1" -m "${2:-release: promote dev into main}")
  git -C "$O" checkout -q main
  git -C "$O" merge -q --ff-only "$r"
  echo "$r"
}
cp "$ROOT/configs/cliff-stable.toml" "$O/cliff.toml"
C0=$(commit_in "$O" "feat: initial" .gitignore=cliff.toml go.mod='module example.com/app' main.go=v1 go.sum=a \
  yamlenv/go.mod='module example.com/app/yamlenv' yamlenv/y.go=v1)
git -C "$O" tag v1.0.0 "$C0"
git -C "$O" tag yamlenv/v1.0.0 "$C0"
git -C "$O" checkout -q -b dev
D1=$(commit_in "$O" "feat: root api" main.go=v2)
git -C "$O" tag v1.1.0-dev.1 "$D1"
R1=$(promote "$D1")

export REPO_TYPE=go SUBPACKAGES_JSON='[]' GO_LANES_JSON='["yamlenv"]'
pending_at() { # <dir> <channel> -> the state JSON; outputs in $WORK/out
  : >"$WORK/out"
  (cd "$1" && CHANNEL="$2" GITHUB_OUTPUT="$WORK/out" bash "$RS" pending) >"$WORK/pending.log" 2>&1 \
    || fail "pending failed: $(cat "$WORK/pending.log")"
  sed -n 's/^state=//p' "$WORK/out"
}
outkey() { sed -n "s/^$1=//p" "$WORK/out" | head -1; }
lane() { jq -r --arg k "$1" ".[\$k].$2" <<<"$3"; }

chk "P0 R1 has the reconciliation shape" "$(. "$ROOT/scripts/reconciliation.sh" && cd "$O" && is_reconciliation "$R1" && echo yes)" "yes"
s=$(pending_at "$O" stable)
chk "P1 at R a root promotion is pending for the root" "$(lane . commit "$s")" "$R1"
chk "P1 and in this run's range" "$(lane . in_range "$s")|$(outkey root_in_range)|$(outkey in_range_any)" "true|true|true"
chk "P1 the root kind line names the promoted dev build" "$(outkey root_kind_note)" 'Promoted from `v1.1.0-dev.1`'
chk "P2 a root-only R leaves the nested lane non-pending" "$(lane yamlenv commit "$s")|$(lane yamlenv in_range "$s")" "|false"
chk "P2 and the lane's kind line is the main-built one" "$(lane yamlenv kind_note "$s")" \
  'Built from `main` for dependency and system-package updates'
chk "P3 the lane matrix lists the root and the lane" "$(outkey lanes)" '[{"key":".","lane":""},{"key":"yamlenv","lane":"yamlenv"}]'

S1=$(commit_in "$O" "fix(deps): update module example.org/x" go.sum=b)
s=$(pending_at "$O" stable)
chk "P4 at a later S the promotion is still pending" "$(lane . commit "$s")|$(lane . in_range "$s")" "$R1|true"
chk "P4 and S's kind line says it was built from main" "$(outkey root_kind_note)" \
  'Built from `main`, including the promotion of `v1.1.0-dev.1`'

W="$WORK/work"
git clone -q "$O" "$W"
git -C "$W" checkout -q dev
s=$(pending_at "$W" dev)
chk "P5 a dev run sees the promotion pending on main" "$(lane . commit "$s")|$(lane . in_range "$s")" "$R1|false"
chk "P5 and writes no kind line" "$(outkey root_kind_note)" ""
git -C "$W" update-ref -d refs/remotes/origin/main
chk "P6 a dev run without origin/main is refused" \
  "$( (cd "$W" && CHANNEL=dev GITHUB_OUTPUT="$WORK/out" bash "$RS" pending) >/dev/null 2>&1 && echo ran || echo refused)" "refused"

# ── Numbering and the version arithmetic it feeds ────────────────────────────
number() { # <state> -> the state with versions; outputs in $WORK/out
  : >"$WORK/out"
  (cd "$O" && STATE="$1" CLIFF_SCRIPT="$COMPUTE" CLIFF_BIN="$CLIFF" EXCLUDE_PATHS='yamlenv/**' \
    GITHUB_OUTPUT="$WORK/out" bash "$RS" number) >"$WORK/number.log" 2>&1 || fail "number failed: $(cat "$WORK/number.log")"
  sed -n 's/^state=//p' "$WORK/out"
}
s=$(number "$(pending_at "$O" stable)")
chk "N1 the root promotion is numbered from its feature" "$(outkey root_version)" "v1.1.0"
chk "N1 a lane with nothing pending gets no version" "$(lane yamlenv version "$s")" ""
compute_at() { # <pending-in-range> -> the stable version compute.sh selects at main's head
  : >"$WORK/cout"
  (cd "$O" && CHANNEL=stable RELEASE_MODEL=two-branch PENDING_IN_RANGE="$1" EXCLUDE_PATHS='yamlenv/**' CLIFF_BIN="$CLIFF" \
    GITHUB_OUTPUT="$WORK/cout" bash "$COMPUTE") >/dev/null 2>&1 || fail "compute.sh failed"
  sed -n 's/^base=//p' "$WORK/cout"
}
chk "N2 the pending range the state reports publishes the promotion's version" "$(compute_at "$(lane . in_range "$s")")" "v1.1.0"
chk "N2 the same main run with nothing pending is capped at a patch" "$(compute_at false)" "v1.0.1"
git -C "$O" tag v1.1.0 "$S1"
rm -rf "$WORK/stale"
git clone -q "$O" "$WORK/stale"
git -C "$WORK/stale" checkout -q --detach "$R1"
chk "P10 a stable run behind the highest stable tag is refused" \
  "$( (cd "$WORK/stale" && CHANNEL=stable GITHUB_OUTPUT="$WORK/out" bash "$RS" pending) >"$WORK/stale.log" 2>&1 && echo ran || echo refused)" "refused"
chk_has "P10 naming the tag it is behind" "$(cat "$WORK/stale.log")" "the highest stable tag v1.1.0 is on $S1, which HEAD does not contain"

git -C "$O" checkout -q dev
D2=$(commit_in "$O" "fix(yamlenv): parse anchors" yamlenv/y.go=v2)
git -C "$O" tag yamlenv/v1.0.1-dev.1 "$D2"
D3=$(commit_in "$O" "fix(deps): update module example.org/x" go.sum=b)
R2=$(promote "$D3")
s=$(pending_at "$O" stable)
chk "P7 a nested-only R leaves the root non-pending" "$(lane . commit "$s")|$(lane . in_range "$s")" "|false"
chk "P7 and the root's kind line is the main-built one" "$(outkey root_kind_note)" \
  'Built from `main` for dependency and system-package updates'
chk "P8 a nested-only R is the lane's pending promotion" "$(lane yamlenv commit "$s")|$(lane yamlenv in_range "$s")" "$R2|true"
chk "P8 with the lane's dev build in its kind line" "$(lane yamlenv kind_note "$s")" 'Promoted from `yamlenv/v1.0.1-dev.1`'
s=$(number "$s")
chk "N3 a fixes-only lane promotion is numbered the next lane minor" "$(lane yamlenv version "$s")" "yamlenv/v1.1.0"
chk "N3 and the root gets no version" "$(outkey root_version)" ""

# ── Publication: a pending promotion publishes its lanes on its own ──────────
publish_at() { # <dir> <root_changed> <subs> <mods> <state> [channel] -> outputs in $WORK/out; EXIT=n on failure
  : >"$WORK/out"
  (cd "$1" && CHANNEL="${6:-stable}" STATE="$5" ROOT_CHANGED="$2" SUBPACKAGES_TO_PUBLISH="$3" GO_MODULES_TO_RELEASE="$4" \
    GITHUB_OUTPUT="$WORK/out" bash "$RS" publish) >"$WORK/publish.log" 2>&1 || echo "EXIT=$?"
}
publish_sets() { echo "$(outkey root_changed)|$(outkey subpackages_to_publish)|$(outkey go_modules_to_release)"; }
chk "Q1 a pending nested lane is added to the lane releases" "$(publish_at "$O" false '[]' '[]' "$s")$(publish_sets)" 'false|[]|["yamlenv"]'
chk "Q1 once" "$(publish_at "$O" true '[]' '["yamlenv"]' "$s")$(publish_sets)" 'true|[]|["yamlenv"]'
chk "Q1 never on a dev run" "$(publish_at "$O" false '[]' '[]' "$s" dev)" "EXIT=1"

# A first promotion whose release failed before any tag, then a docs-only S:
# S's own diff ships nothing, so only the pending promotion can publish it.
G="$WORK/tagless"
git init -q -b main "$G"
cp "$ROOT/configs/cliff-stable.toml" "$G/cliff.toml"
commit_in "$G" "chore: initial" .gitignore=cliff.toml go.mod='module example.com/g' main.go=v1 >/dev/null
git -C "$G" checkout -q -b dev
GT=$(commit_in "$G" "feat: first api" main.go=v2)
git -C "$G" checkout -q main
GR=$(git -C "$G" commit-tree "$GT^{tree}" -p main -p "$GT" -m "release: promote dev into main")
git -C "$G" merge -q --ff-only "$GR"
GS=$(commit_in "$G" "docs: describe the api" README.md=api)
: >"$WORK/out"
(cd "$G" && REPO_TYPE=go GO_LANES_JSON='[]' CHANNEL=stable GITHUB_OUTPUT="$WORK/out" bash "$RS" pending) >/dev/null 2>&1 \
  || fail "pending failed in the tagless fixture"
GSTATE=$(outkey state)
chk "Q2 the failed promotion is pending at the docs-only S" "$(lane . commit "$GSTATE")|$(lane . in_range "$GSTATE")" "$GR|true"
: >"$WORK/out"
(cd "$G" && BEFORE="$GR" HEAD="$GS" ANCHOR_SHA="" CHANNEL=stable REPO_TYPE=go SUBPACKAGES_JSON='[]' GO_LANES_JSON='[]' \
  GITHUB_OUTPUT="$WORK/out" GITHUB_STEP_SUMMARY=/dev/null bash "$ROOT/scripts/path-significance.sh") >/dev/null 2>&1 \
  || fail "path-significance failed in the tagless fixture"
chk "Q2 S's own diff ships nothing" "$(outkey root_changed)" "false"
q_out=$(REPO_TYPE=go GO_LANES_JSON='[]' publish_at "$G" false '[]' '[]' "$GSTATE")
chk "Q3 the pending promotion makes the root publish" "$q_out$(publish_sets)" 'true|[]|[]'
q_rc=$(outkey root_changed)
chk "Q3 and Select version releases S" "$(select_out new stable normal "$q_rc" '[]' "" v1.0.0 | sed -n 's/^release=//p')" "true"

# Two promotions past the root's stable tag, the first's run failed: R1 changed
# only the web subpackage, R2 only the root. R2's run owes both.
U="$WORK/two-promotions"
git init -q -b main "$U"
cp "$ROOT/configs/cliff-stable.toml" "$U/cliff.toml"
U0=$(commit_in "$U" "chore: initial" .gitignore=cliff.toml go.mod='module example.com/u' main.go=v1 \
  web/jsr.json='{"name":"@o/web"}' web/package.json='{"name":"@o/web"}' web/index.ts=v1)
git -C "$U" checkout -q -b dev
UT1=$(commit_in "$U" "feat(web): add api" web/index.ts=v2)
git -C "$U" checkout -q main
UR1=$(git -C "$U" commit-tree "$UT1^{tree}" -p main -p "$UT1" -m "release: promote dev into main")
git -C "$U" merge -q --ff-only "$UR1"
git -C "$U" checkout -q dev
UT2=$(commit_in "$U" "feat: root behaviour" main.go=v2)
git -C "$U" checkout -q main
UR2=$(git -C "$U" commit-tree "$UT2^{tree}" -p main -p "$UT2" -m "release: promote dev into main")
git -C "$U" merge -q --ff-only "$UR2"
u_publish() { # -> root_changed|subpackages_to_publish of a stable run at UR2 triggered by UR2's push
  : >"$WORK/out"
  (cd "$U" && REPO_TYPE=go SUBPACKAGES_JSON='["web"]' GO_LANES_JSON='[]' CHANNEL=stable GITHUB_OUTPUT="$WORK/out" \
    bash "$RS" pending) >/dev/null 2>&1 || fail "pending failed in the two-promotion fixture"
  local st
  st=$(outkey state)
  : >"$WORK/out"
  (cd "$U" && BEFORE="$UR1" HEAD="$UR2" ANCHOR_SHA="" CHANNEL=stable REPO_TYPE=go SUBPACKAGES_JSON='["web"]' GO_LANES_JSON='[]' \
    GITHUB_OUTPUT="$WORK/out" GITHUB_STEP_SUMMARY=/dev/null bash "$ROOT/scripts/path-significance.sh") >/dev/null 2>&1 \
    || fail "path-significance failed in the two-promotion fixture"
  local trig_rc trig_subs
  trig_rc=$(outkey root_changed) trig_subs=$(outkey subpackages_to_publish)
  REPO_TYPE=go SUBPACKAGES_JSON='["web"]' GO_LANES_JSON='[]' publish_at "$U" "$trig_rc" "$trig_subs" '[]' "$st" >/dev/null
  echo "$(lane . commit "$st")|$trig_subs|$(outkey root_changed)|$(outkey subpackages_to_publish)"
}
chk "Q4 tagless: R2 is the pending promotion and its own push names no subpackage" "$(u_publish | cut -d'|' -f1,2)" "$UR2|[]"
chk "Q4 tagless: the run publishes the root and the subpackage R1 changed" "$(u_publish | cut -d'|' -f3,4)" 'true|["web"]'
git -C "$U" tag v1.0.0 "$U0"
chk "Q4 with a stable tag below both: the same" "$(u_publish | cut -d'|' -f3,4)" 'true|["web"]'
git -C "$U" tag v1.1.0 "$UR1"
chk "Q4 a promotion its own run published is not owed again" "$(u_publish | cut -d'|' -f3,4)" 'true|[]'

git -C "$O" checkout -q -b impostor "$S1"
WRONG=$(git -C "$O" commit-tree "$D3^{tree}" -p "$S1" -p "$D3" -m "Merge branch 'dev'")
git -C "$O" merge -q --ff-only "$WRONG"
s=$(pending_at "$O" stable)
chk "P9 a merge with another subject is no promotion" "$(lane . commit "$s")|$(lane yamlenv commit "$s")" "|"
git -C "$O" checkout -q main

# ── Receipts and the repair they owe ─────────────────────────────────────────
git -C "$O" tag yamlenv/v1.1.0 "$R2"
STATE_R2=$(pending_at "$O" stable)
chk "R0 the lane's repair kind line is its promotion's" "$(lane yamlenv repair_kind_note "$STATE_R2")" \
  'Promoted from `yamlenv/v1.0.1-dev.1`'
chk "R0 the root's is main-built with the promotion it carried" "$(lane . repair_kind_note "$STATE_R2")" \
  'Built from `main`, including the promotion of `v1.1.0-dev.1`'
printf '[{"context":"release/complete/v1.1.0","state":"success"}]\n' >"$GH_DIR/statuses-$S1.json"
printf '[{"context":"release/complete/yamlenv/v1.1.0","state":"pending"},{"context":"release/tag/yamlenv/v1.1.0","state":"success"}]\n' >"$GH_DIR/statuses-$R2.json"
cat >"$GH_DIR/pulls.json" <<JSON
[
  {"merged_at": "2026-10-01T10:00:00Z", "head": {"ref": "renovate/main-golang-x-net"}, "labels": [{"name": "security"}], "merge_commit_sha": "aaa1"},
  {"merged_at": "2026-10-01T10:00:00Z", "head": {"ref": "fix/tls"}, "labels": [{"name": "security"}], "merge_commit_sha": "bbb2"},
  {"merged_at": null, "head": {"ref": "renovate/main-golang-x-crypto"}, "labels": [{"name": "security"}], "merge_commit_sha": "ccc3"},
  {"merged_at": "2026-10-01T10:00:00Z", "head": {"ref": "renovate/main-weekly"}, "labels": [{"name": "dependencies"}], "merge_commit_sha": "ddd4"}
]
JSON
receipts() { # -> the repairs JSON; outputs in $WORK/out
  : >"$WORK/out"
  (cd "$O" && STATE="$STATE_R2" GITHUB_OUTPUT="$WORK/out" bash "$RS" receipts) >"$WORK/receipts.log" 2>&1 || {
    echo "EXIT"
    return 0
  }
  outkey repairs
}
r=$(receipts)
chk "R1 only the tag without its receipt is repaired" "$(jq -r '[.[].tag] | join(" ")' <<<"$r")" "yamlenv/v1.1.0"
chk "R1 at its own commit, on the lane site" "$(jq -r '.[0] | "\(.commit) \(.site) \(.lane) \(.key)"' <<<"$r")" "$R2 lane yamlenv yamlenv"
chk "R1 with its kind line" "$(jq -r '.[0].kind_note' <<<"$r")" 'Promoted from `yamlenv/v1.0.1-dev.1`'
chk "R24 a lane's repair owes no image assets" "$(outkey repair_images)" "[]"
cp "$GH_DIR/statuses-$S1.json" "$WORK/statuses-s1.saved"
echo '[]' >"$GH_DIR/statuses-$S1.json"
r=$(REPO_TYPE=docker receipts)
chk "R24 an image repo repairing its root and a lane hands only the image's to repair-assets" \
  "$(jq -r '[.[].site] | sort | join(" ")' <<<"$r")|$(outkey repair_images | jq -r '[.[] | "\(.site) \(.tag) \(.commit)"] | join(",")')" \
  "docker lane|docker v1.1.0 $S1"
mv "$WORK/statuses-s1.saved" "$GH_DIR/statuses-$S1.json"
chk "R2 only merged Renovate security PRs are marked" "$(outkey security_shas)" "aaa1"
cat >"$GH_DIR/pulls-dev.json" <<JSON
[
  {"merged_at": "2026-10-01T10:00:00Z", "head": {"ref": "renovate/dev-golang-x-net"}, "labels": [{"name": "security"}], "merge_commit_sha": "eee5"},
  {"merged_at": "2026-10-01T10:00:00Z", "head": {"ref": "renovate/dev-weekly"}, "labels": [{"name": "dependencies"}], "merge_commit_sha": "fff6"},
  {"merged_at": "2026-10-01T10:00:00Z", "head": {"ref": "renovate/dev-golang-x-net"}, "labels": [{"name": "security"}], "merge_commit_sha": "aaa1"}
]
JSON
r=$(receipts)
chk "R2 a security PR merged into dev is marked too, each SHA once" "$(outkey security_shas)" "aaa1 eee5"
mv "$GH_DIR/pulls-dev.json" "$GH_DIR/pulls-dev.json.p2"
jq -n '[range(100) | {merged_at: "2099-01-02T10:00:00Z", updated_at: "2099-01-02T10:00:00Z", head: {ref: "fix/d\(.)"}, labels: [], merge_commit_sha: "d\(.)"}]' >"$GH_DIR/pulls-dev.json"
r=$(receipts)
chk "R2 a dev security PR behind 100 newer closures is still marked" "$(outkey security_shas)" "aaa1 eee5"
rm "$GH_DIR/pulls-dev.json" "$GH_DIR/pulls-dev.json.p2"
cp "$GH_DIR/pulls.json" "$GH_DIR/pulls.saved"
jq -n '[range(100) | {merged_at: "2099-01-02T10:00:00Z", updated_at: "2099-01-02T10:00:00Z", head: {ref: "fix/n\(.)"}, labels: [], merge_commit_sha: "f\(.)"}]' >"$GH_DIR/pulls.json"
cp "$GH_DIR/pulls.saved" "$GH_DIR/pulls.json.p2"
r=$(receipts)
chk "R2 a security PR behind 100 newer closures is still marked" "$(outkey security_shas)" "aaa1"
jq '.[-1].updated_at = "2000-01-01T00:00:00Z"' "$GH_DIR/pulls.json" >"$GH_DIR/pulls.old" && mv "$GH_DIR/pulls.old" "$GH_DIR/pulls.json"
: >"$GH_LOG"
r=$(receipts)
chk "R2 a page ending before every notes range stops the read" "$(outkey security_shas)|$(grep -c '&page=2$' "$GH_LOG" || true)" "|0"
r=$(STATE_R2=$(jq -c '.yamlenv.h_tag = "" | .yamlenv.h_commit = ""' <<<"$STATE_R2") receipts)
chk "R2 a lane with no stable tag reads every page" "$(outkey security_shas)" "aaa1"
# Dated history: a repaired lane's notes start at the tag below it.
F="$WORK/dated"
git init -q -b main "$F"
FA=$(GIT_COMMITTER_DATE=2026-01-01T00:00:00Z commit_in "$F" "feat: a" a=1)
FB=$(GIT_COMMITTER_DATE=2026-06-01T00:00:00Z commit_in "$F" "feat: b" a=2)
git -C "$F" tag v1.0.0 "$FA"
git -C "$F" tag v1.1.0 "$FB"
jq '.[-1].updated_at = "2026-03-01T00:00:00Z"' "$GH_DIR/pulls.json" >"$GH_DIR/pulls.old" && mv "$GH_DIR/pulls.old" "$GH_DIR/pulls.json"
dated_receipts() { # -> security_shas of a receipts run over the dated fixture
  : >"$WORK/out"
  (cd "$F" && REPO_TYPE=go GO_LANES_JSON='[]' GITHUB_OUTPUT="$WORK/out" \
    STATE="{\".\": {\"h_tag\": \"v1.1.0\", \"h_commit\": \"$FB\", \"repair_kind_note\": \"\"}}" bash "$RS" receipts) >/dev/null 2>&1 \
    || echo EXIT
  outkey security_shas
}
chk "R14 a repaired version's notes reach back to the tag below it" "$(dated_receipts)" "aaa1"
printf '[{"context":"release/complete/v1.1.0","state":"success"}]\n' >"$GH_DIR/statuses-$FB.json"
chk "R14 a receipted version's notes start at the version itself" "$(dated_receipts)" ""
mv "$GH_DIR/pulls.saved" "$GH_DIR/pulls.json"
rm "$GH_DIR/pulls.json.p2"
touch "$GH_DIR/fail-statuses"
chk "R3 an unreadable status list fails rather than guessing" "$(receipts)" "EXIT"
chk "R3 and writes no repairs" "$(grep -c '^repairs=' "$WORK/out" || true)" "0"
rm "$GH_DIR/fail-statuses"

# Seam: a promotion whose dev range holds a Renovate security merge; the
# receipts output, handed on as release.yaml does, marks that update alone.
SS="$WORK/security-seam"
git init -q -b main "$SS"
cp "$ROOT/configs/cliff-stable.toml" "$SS/cliff.toml"
seam_mod() { printf 'module example.com/app\n\ngo 1.26\n\nrequire (\n\texample.com/a %s\n\texample.com/b %s\n)\n' "$1" "$2"; }
SSB=$(commit_in "$SS" "feat: initial" .gitignore=cliff.toml go.mod="$(seam_mod v1.0.0 v1.0.0)" main.go=v1)
git -C "$SS" tag v1.0.0 "$SSB"
git -C "$SS" checkout -q -b dev
SSEC=$(commit_in "$SS" "fix(deps): update module example.com/a to v1.0.1 (#5)" go.mod="$(seam_mod v1.0.1 v1.0.0)")
SDEP=$(commit_in "$SS" "fix(deps): update module example.com/b to v1.1.0 (#6)" go.mod="$(seam_mod v1.0.1 v1.1.0)")
commit_in "$SS" "feat: add export (#7)" main.go=v2 >/dev/null
SSR=$(git -C "$SS" commit-tree "dev^{tree}" -p main -p dev -m "release: promote dev into main")
cat >"$GH_DIR/pulls-dev.json" <<JSON
[
  {"merged_at": "2026-10-01T10:00:00Z", "head": {"ref": "renovate/dev-example-a"}, "labels": [{"name": "security"}], "merge_commit_sha": "$SSEC"},
  {"merged_at": "2026-10-01T10:00:00Z", "head": {"ref": "renovate/dev-example-b"}, "labels": [{"name": "dependencies"}], "merge_commit_sha": "$SDEP"}
]
JSON
printf '[{"context":"release/complete/v1.0.0","state":"success"}]\n' >"$GH_DIR/statuses-$SSB.json"
: >"$WORK/out"
(cd "$SS" && REPO_TYPE=go GO_LANES_JSON='[]' GITHUB_OUTPUT="$WORK/out" \
  STATE="{\".\": {\"h_tag\": \"v1.0.0\", \"h_commit\": \"$SSB\", \"repair_kind_note\": \"\"}}" bash "$RS" receipts) \
  >"$WORK/seam.log" 2>&1 || fail "seam receipts failed: $(cat "$WORK/seam.log")"
SECURITY_SHAS=$(outkey security_shas)
chk_has "R21 the dev security merge reaches the receipts output" "$SECURITY_SHAS" "$SSEC"
tr ' ' '\n' <<<"$SECURITY_SHAS" >"$WORK/seam-shas"
(cd "$SS" && CLIFF_BIN="$CLIFF" bash "$ROOT/scripts/render-notes.sh" --release-model two-branch --repo o/app \
  --release-commit "$SSR" --site go --version v1.1.0 --go-lanes '[]' --security-shas "$WORK/seam-shas" \
  --out "$WORK/seam-notes") >"$WORK/seam.log" 2>&1 || fail "seam render failed: $(cat "$WORK/seam.log")"
chk "R21 and render-notes marks its update as a security update" \
  "$(grep -cxF -- '- `example.com/a` v1.0.0 to v1.0.1 (Go, security update)' "$WORK/seam-notes" || true)" "1"
chk "R21 but not the unrelated update beside it" \
  "$(grep -cxF -- '- `example.com/b` v1.0.0 to v1.1.0 (Go)' "$WORK/seam-notes" || true)" "1"
rm "$GH_DIR/pulls-dev.json"

receipt() { # <tag> <commit> -> runs the receipt subcommand; prints EXIT=n on failure
  : >"$GH_LOG"
  (cd "$O" && bash "$RS" receipt "$1" "$2") >"$WORK/receipt.log" 2>&1 || echo "EXIT=$?"
}
chk "R4 no Release means no receipt" "$(receipt yamlenv/v1.1.0 "$R2")" "EXIT=1"
chk "R4 and no status is posted" "$(grep -c 'statuses/' "$GH_LOG" || true)" "0"
touch "$GH_DIR/release-yamlenv%v1.1.0"
chk "R5 a published Release gets its receipt" "$(receipt yamlenv/v1.1.0 "$R2")" ""
chk "R5 posted on the tag's commit under the completion context" \
  "$(grep -c "^api -X POST repos/o/app/statuses/$R2 -f state=success -f context=release/complete/yamlenv/v1.1.0 " "$GH_LOG")" "1"
chk "R5 after the Release read" "$(head -1 "$GH_LOG")" "api repos/o/app/releases/tags/yamlenv/v1.1.0"

readback() { # <site> <tag> [lane], in $RB_DIR (default the origin)
  : >"$CURL_DIR/log"
  : >"$COSIGN_DIR/log"
  (cd "${RB_DIR:-$O}" && IMAGE=o/app bash "$RS" readback "$@") >"$WORK/readback.log" 2>&1 && echo ok || echo refused
}
chk "R6 a lane readback the proxy cannot answer fails" "$(readback lane yamlenv/v1.1.0 yamlenv)" "refused"
chk "R6 after the shared wait's fourteen tries" "$(grep -c 'proxy.golang.org/example.com/app/yamlenv/@v/v1.1.0.info' "$CURL_DIR/log")" "14"
echo "https://proxy.golang.org/example.com/app/yamlenv/@v/v1.1.0.info" >"$CURL_DIR/ok"
chk "R6 a lane readback the proxy answers passes" "$(readback lane yamlenv/v1.1.0 yamlenv)" "ok"
chk "R7 an image readback with no manifest fails" "$(readback docker v1.1.0)" "refused"
IMG_A="sha256:$(printf 'a%.0s' $(seq 64))"
IMG_E="sha256:$(printf 'e%.0s' $(seq 64))"
echo "$IMG_A" >"$CURL_DIR/manifest-v1.1.0"
chk "R7 a GHCR tag no release run signed is refused" "$(readback docker v1.1.0)" "refused"
chk_has "R7 naming the commit it was not signed at" "$(cat "$WORK/readback.log")" \
  "which no docker-release.yaml run at $S1, an ancestor whose image it carries, or a later main commit below the next stable tag signed"
echo "$IMG_A $R1" >"$COSIGN_DIR/signed"
chk "R7 a digest signed at another commit is refused" "$(readback docker v1.1.0)" "refused"
echo "$IMG_A $S1" >"$COSIGN_DIR/signed"
chk "R7 a built tag signed at its own commit reads back from GHCR" "$(readback docker v1.1.0)" "ok"
chk "R7 asking cosign for this repository's run at that commit" "$(cat "$COSIGN_DIR/log")" \
  "verify --certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity-regexp ^https://github\.com/cplieger/ci/\.github/workflows/docker-release\.yaml@ --certificate-github-workflow-repository o/app --certificate-github-workflow-sha $S1 ghcr.io/o/app@$IMG_A"
# A tag the v2 pipeline re-pushed from a later main commit that released nothing.
echo "$IMG_A $R2" >"$COSIGN_DIR/signed"
chk "R13 a tag re-pushed from a later main commit reads back" "$(readback docker v1.1.0)" "ok"
chk "R13 asking for the tag's commit first, then the later one" \
  "$(sed 's/.*--certificate-github-workflow-sha \([0-9a-f]*\) .*/\1/' "$COSIGN_DIR/log" | tr '\n' ' ')" "$S1 $R2 "
echo "$IMG_A $D3" >"$COSIGN_DIR/signed"
chk "R13 a signature from a dev commit is refused" "$(readback docker v1.1.0)" "refused"
FP="$WORK/first-parent"
git clone -q "$O" "$FP"
SIDE=$(git -C "$FP" commit-tree "$S1^{tree}" -p "$S1" -m "fix: side work")
MERGE=$(git -C "$FP" commit-tree "$R2^{tree}" -p "$R2" -p "$SIDE" -m "Merge side")
git -C "$FP" checkout -q --detach "$MERGE"
echo "$IMG_A $SIDE" >"$COSIGN_DIR/signed"
chk "R13 a descendant reached through a second parent is refused" "$(RB_DIR=$FP readback docker v1.1.0)" "refused"
echo "$IMG_A $MERGE" >"$COSIGN_DIR/signed"
chk "R13 the first-parent commit that merged it reads back" "$(RB_DIR=$FP readback docker v1.1.0)" "ok"
git -C "$O" tag v1.2.0 "$R2"
echo "$IMG_A $R2" >"$COSIGN_DIR/signed"
chk "R13 a signature at the next stable tag's commit is refused" "$(readback docker v1.1.0)" "refused"
chk "R13 and nothing past the window is asked" "$(wc -l <"$COSIGN_DIR/log" | tr -d ' ')" "1"
git -C "$O" tag -d v1.2.0 >/dev/null
echo "$IMG_A $S1" >"$COSIGN_DIR/signed"
echo 1 >"$COSIGN_DIR/flaky"
chk "R15 a transient cosign failure at the signing commit is retried" "$(readback docker v1.1.0)" "ok"
chk "R15 by asking that commit again" "$(grep -c -- "-sha $S1 " "$COSIGN_DIR/log")" "2"
touch "$COSIGN_DIR/down"
chk "R15 a cosign that never answers is refused" "$(readback docker v1.1.0)" "refused"
chk_has "R15 naming cosign's own error, not a missing signature" "$(cat "$WORK/readback.log")" \
  "cosign could not verify ghcr.io/o/app@$IMG_A at $S1: error during command execution: getting signatures: connection reset by peer"
chk "R15 after five tries, without walking on to a later commit" "$(grep -c -- "-sha $S1 " "$COSIGN_DIR/log")|$(wc -l <"$COSIGN_DIR/log" | tr -d ' ')" "5|5"
rm "$COSIGN_DIR/down" "$COSIGN_DIR/flaky"
chk "R11 GHCR right but Docker Hub missing is refused" "$(REGISTRIES=ghcr,dockerhub readback docker v1.1.0)" "refused"
chk_has "R11 naming the registry" "$(cat "$WORK/readback.log")" "docker.io/o/app:v1.1.0 at $IMG_A does not answer"
echo "$IMG_E" >"$CURL_DIR/hub-manifest-v1.1.0"
chk "R11 Docker Hub at another digest is refused" "$(REGISTRIES=ghcr,dockerhub readback docker v1.1.0)" "refused"
echo "$IMG_A" >"$CURL_DIR/hub-manifest-v1.1.0"
chk "R11 both registries at the signed digest pass" "$(REGISTRIES=ghcr,dockerhub readback docker v1.1.0)" "ok"

# A main release that re-tagged its first-parent ancestor's build reads back
# at that build's signature, never past a commit that owed its own image.
AN="$WORK/ancestor"
git init -q -b main "$AN"
A0=$(commit_in "$AN" "feat: initial" main.go=v1 web/package.json='{"name":"@o/web","dependencies":{"a":"1.0.0"}}')
A1=$(commit_in "$AN" "docs: readme" README.md=a)
git -C "$AN" tag v1.0.1 "$A1"
A2=$(commit_in "$AN" "fix(deps): update dependency a to v1.0.1" web/package.json='{"name":"@o/web","dependencies":{"a":"1.0.1"}}')
git -C "$AN" tag v1.0.2 "$A2"
echo "$IMG_A" >"$CURL_DIR/manifest-v1.0.1"
echo "$IMG_A" >"$CURL_DIR/manifest-v1.0.2"
echo "$IMG_A $A0" >"$COSIGN_DIR/signed"
anc() { EXCLUDE_RE='^docs/' SUBPACKAGES_JSON="${1}" RB_DIR=$AN readback docker "$2"; }
chk "R22 a tag whose digest was signed at the ancestor it re-tagged reads back" "$(anc '["web"]' v1.0.1)" "ok"
chk "R22 asking the tag's commit, then that ancestor" \
  "$(sed 's/.*--certificate-github-workflow-sha \([0-9a-f]*\) .*/\1/' "$COSIGN_DIR/log" | tr '\n' ' ')" "$A1 $A0 "
chk "R22 a release owed to a subpackage manifest is refused an ancestor's signature" "$(anc '["web"]' v1.0.2)" "refused"
chk "R22 where an undeclared package.json would have carried it" "$(anc '[]' v1.0.2)" "ok"
chk "R22 without the exclusion list only the tag's own window is asked" "$(EXCLUDE_RE='' RB_DIR=$AN readback docker v1.0.1)" "refused"
echo "$IMG_A $S1" >"$COSIGN_DIR/signed"
# A promotion's tag reads back at its trailer digest, and its dashboard at a
# digest the promotion's own run signed.
P="$WORK/promo"
git init -q -b main "$P"
commit_in "$P" "feat: initial" a=1 >/dev/null
git -C "$P" checkout -q -b dev
PT=$(commit_in "$P" "feat: a dashboard" grafana-dashboard.json='{"uid":"x"}')
git -C "$P" checkout -q main
PR=$(git -C "$P" commit-tree "$PT^{tree}" -p main -p "$PT" -m "release: promote dev into main" -m "Promoted-Digest: $IMG_E")
git -C "$P" merge -q --ff-only "$PR"
git -C "$P" tag v2.0.0 "$PR"
DASH="sha256:$(printf 'd%.0s' $(seq 64))"
echo "$DASH" >"$CURL_DIR/dash-manifest-v2.0.0"
printf '%s\n' "$IMG_A $PR" "$DASH $PR" "$IMG_E $PT" >"$COSIGN_DIR/signed"
echo "$IMG_E $PT" >"$COSIGN_DIR/attested"
echo "$IMG_A" >"$CURL_DIR/manifest-v2.0.0"
chk "R12 a promotion's tag at a digest other than its trailer's is refused, signed or not" "$(RB_DIR=$P readback docker v2.0.0)" "refused"
chk_has "R12 naming the trailer digest" "$(cat "$WORK/readback.log")" "ghcr.io/o/app:v2.0.0 at $IMG_E does not answer"
echo "$IMG_E" >"$CURL_DIR/manifest-v2.0.0"
chk "R12 the trailer digest and a dashboard signed at R read back" "$(RB_DIR=$P readback docker v2.0.0)" "ok"
chk "R12 the trailer digest's signature and SBOM are asked of the promoted commit, never of R" \
  "$(grep -c -- "-sha $PT ghcr.io/o/app@$IMG_E\$" "$COSIGN_DIR/log")|$(grep -c -- "-sha $PR ghcr.io/o/app@$IMG_E\$" "$COSIGN_DIR/log" || true)" "2|0"
# A dev run pushes its sha- tag before it signs and attests, so a digest GHCR
# serves can be one nothing vouches for; a promotion re-tags without either.
printf '%s\n' "$DASH $PR" >"$COSIGN_DIR/signed"
chk "R23 a promoted digest its dev build never signed is not complete" "$(RB_DIR=$P readback docker v2.0.0)" "refused"
chk_has "R23 naming the missing signature" "$(cat "$WORK/readback.log")" "ghcr.io/o/app@$IMG_E carries no signature"
printf '%s\n' "$DASH $PR" "$IMG_E $PT" >"$COSIGN_DIR/signed"
: >"$COSIGN_DIR/attested"
chk "R23 nor one it signed and never attested" "$(RB_DIR=$P readback docker v2.0.0)" "refused"
chk_has "R23 naming the missing attestation" "$(cat "$WORK/readback.log")" "carries no SPDX SBOM attestation"
printf '%s\n' "$DASH $PR" "$IMG_E $PR" >"$COSIGN_DIR/signed"
echo "$IMG_E $PR" >"$COSIGN_DIR/attested"
chk "R23 nor one signed and attested at R, which never built it" "$(RB_DIR=$P readback docker v2.0.0)" "refused"
echo "$IMG_E $PT" >"$COSIGN_DIR/attested"
PC=$(commit_in "$P" "chore(sync): update shared files" .editorconfig=x)
printf '%s\n' "$DASH $PC" "$IMG_E $PT" >"$COSIGN_DIR/signed"
chk "R13 a dashboard re-pushed from a later main commit reads back" "$(RB_DIR=$P readback docker v2.0.0)" "ok"
printf '%s\n' "$DASH $R1" "$IMG_E $PT" >"$COSIGN_DIR/signed"
chk "R12 a dashboard no run at R signed is refused" "$(RB_DIR=$P readback docker v2.0.0)" "refused"
rm "$CURL_DIR/dash-manifest-v2.0.0"
chk "R12 no published dashboard is refused" "$(RB_DIR=$P readback docker v2.0.0)" "refused"
chk_has "R12 naming the dashboard" "$(cat "$WORK/readback.log")" "ghcr.io/o/app/dashboard:v2.0.0 is ''"

# The verify-publish step: a receipt per lane that published, none without a Release.
git -C "$O" checkout -q main
vp_receipts() { # <go result> -> runs the extracted step at main's head
  : >"$GH_LOG"
  (cd "$O" && CI_TOOLS="$ROOT/scripts" VERSION="${VERSION_TAG:-v1.1.0}" GO_LANES_JSON='["yamlenv"]' GO_RESULT="$1" TS_RESULT=skipped \
    DOCKER_RESULT="${DOCKER_RESULT:-skipped}" SUBPACKAGE_RESULT="${SUBPACKAGE_RESULT:-skipped}" \
    GITHUB_SHA="$(git -C "$O" rev-parse HEAD)" GO_NESTED_RESULT="${GO_NESTED_RESULT:-success}" \
    bash "$WORK/receipt-step.sh") >"$WORK/vp.log" 2>&1 || echo "EXIT=$?"
}
chk "R8 the lane tag at HEAD is receipted once its Release exists" "$(vp_receipts skipped)" ""
chk "R8 under its own context" "$(grep -c "statuses/$R2 -f state=success -f context=release/complete/yamlenv/v1.1.0 " "$GH_LOG")" "1"
chk "R9 a root publish without a Release fails the step" "$(vp_receipts success)" "EXIT=1"
chk "R9 and posts no root receipt" "$(grep -c 'context=release/complete/v1.1.0 ' "$GH_LOG" || true)" "0"
# An image whose version subpackages share is receipted here, once they published.
git -C "$O" tag v9.0.0 HEAD
touch "$GH_DIR/release-v9.0.0"
vp_root() { # <docker result> <subpackage result> -> root receipts posted for v9.0.0
  VERSION_TAG=v9.0.0 DOCKER_RESULT="$1" SUBPACKAGE_RESULT="$2" GO_NESTED_RESULT=skipped vp_receipts skipped >/dev/null
  grep -c "statuses/$(git -C "$O" rev-parse HEAD) -f state=success -f context=release/complete/v9.0.0 " "$GH_LOG" || true
}
chk "R16 an image whose subpackages published is receipted after them, at HEAD" "$(vp_root success success)" "1"
chk "R16 an image whose subpackages did not publish is not" "$(vp_root success skipped)|$(vp_root success failure)" "0|0"
chk "R16 nor one this run did not build" "$(vp_root skipped success)" "0"
git -C "$O" tag -d v9.0.0 >/dev/null
git -C "$O" tag v9.0.0 "$C0"
chk "R16 nor a version this run did not tag" "$(vp_root success success)" "0"
git -C "$O" tag -d v9.0.0 >/dev/null
rm "$GH_DIR/release-v9.0.0"

# ── A root release's TS subpackages, repaired at the root tag's commit ───────
BIN2="$WORK/bin2"
mkdir -p "$BIN2"
export SP_LOG="$WORK/sp.log"
# npm and npx as the subpackage publish calls them; a publish makes the
# version readable at the curl stub, recording the tree it published from.
cat >"$BIN2/npm" <<'SH'
#!/usr/bin/env bash
printf 'npm %s @%s\n' "$*" "${PWD##*/}" >>"$SP_LOG"
case "$*" in
  "pkg get name") jq -c .name package.json ;;
  "pkg set version="*) echo "${3#version=}" >"$SP_LOG.version" ;;
  "view "*" version") grep -qxF "https://registry.npmjs.org/${2%@*}/${2##*@}" "$CURL_DIR/ok" 2>/dev/null ;;
  "publish --access public" | "publish --access public --tag dev")
    printf 'published %s\n' "$(cat mod.ts)" >>"$SP_LOG"
    echo "https://registry.npmjs.org/$(jq -r .name package.json)/$(cat "$SP_LOG.version")" >>"$CURL_DIR/ok" ;;
  "install --no-save") ;;
  *) echo "stub: unexpected npm $*" >&2; exit 22 ;;
esac
SH
cat >"$BIN2/npx" <<'SH'
#!/usr/bin/env bash
printf 'npx %s @%s\n' "$*" "${PWD##*/}" >>"$SP_LOG"
case "$*" in
  "-y jsr@"*" publish --allow-dirty") echo "https://jsr.io/$(jq -r .name jsr.json)/$(jq -r .version jsr.json)_meta.json" >>"$CURL_DIR/ok" ;;
  *) echo "stub: unexpected npx $*" >&2; exit 22 ;;
esac
SH
chmod 755 "$BIN2/npm" "$BIN2/npx"
H="$WORK/hybrid"
git init -q -b main "$H"
H0=$(commit_in "$H" "feat: initial" go.mod='module example.com/h' main.go=v1 web/mod.ts=v1 \
  web/jsr.json='{"name":"@o/web","version":"0.0.0"}' web/package.json='{"name":"@o/web","version":"0.0.0"}')
git -C "$H" tag v1.0.0 "$H0"
H1=$(commit_in "$H" "feat(web): add" web/mod.ts=v2)
git -C "$H" tag v1.1.0 "$H1"
H2=$(commit_in "$H" "fix: root only" main.go=v2)
git -C "$H" tag v1.2.0 "$H2"
git -C "$H" checkout -q -b dev
HT=$(commit_in "$H" "feat(web): promoted" web/mod.ts=v3)
git -C "$H" checkout -q main
HR=$(git -C "$H" commit-tree "$HT^{tree}" -p main -p "$HT" -m "release: promote dev into main")
git -C "$H" merge -q --ff-only "$HR"
HS=$(commit_in "$H" "fix(web): restore" web/mod.ts=v2)
git -C "$H" tag v1.3.0 "$HS"
commit_in "$H" "feat(web): newer, untagged" web/mod.ts=v9 >/dev/null
hrepair() { # <h_tag> <h_commit> -> the subpackages the root repair owes
  : >"$WORK/out"
  (cd "$H" && REPO_TYPE=go GO_LANES_JSON='[]' GITHUB_OUTPUT="$WORK/out" \
    STATE="{\".\": {\"h_tag\": \"$1\", \"h_commit\": \"$2\", \"repair_kind_note\": \"\"}}" bash "$RS" receipts) \
    >"$WORK/hrepair.log" 2>&1 || {
    echo EXIT
    return 0
  }
  outkey repairs | jq -r '.[0].subpackages'
}
chk "R17 a root repair owes the subpackages its range changed" "$(hrepair v1.1.0 "$H1")" '["web"]'
chk "R17 and none when the range changed the root only" "$(hrepair v1.2.0 "$H2")" '[]'
chk "R17 every subpackage for a first release" "$(hrepair v1.0.0 "$H0")" '["web"]'
chk "R17 a promotion's own change counts though a later commit undid it" "$(hrepair v1.3.0 "$HS")" '["web"]'
publishes() { # <h_tag> <h_commit> -> the tags of the repairs the OIDC job completes
  hrepair "$1" "$2" >/dev/null
  outkey repair_publishes | jq -r '[.[].tag] | join(" ")'
}
chk "R17 the publishing job receives a repair owing subpackages" "$(publishes v1.1.0 "$H1")" "v1.1.0"
chk "R17 and no repair owing none" "$(publishes v1.2.0 "$H2")" ""
HC="$WORK/hybrid-clone"
git clone -q "$H" "$HC"
HCURL="$WORK/hcurl"
mkdir -p "$HCURL"
echo "https://proxy.golang.org/example.com/h/@v/v1.1.0.info" >"$HCURL/ok"
touch "$GH_DIR/release-v1.1.0"
hreadback() { # <subpackages JSON> -> the extracted repair readback step for v1.1.0, ok or refused
  : >"$GH_LOG"
  (cd "$HC" && CURL_DIR="$HCURL" CI_TOOLS="$ROOT/scripts" SITE=go LANE="" TAG=v1.1.0 COMMIT="$H1" SUBPACKAGES="$1" \
    IMAGE=o/h REGISTRIES="" bash "$WORK/repair-readback.sh") >"$WORK/hreadback.log" 2>&1 && echo ok || echo refused
}
receipted() { grep -c "statuses/$H1 -f state=success -f context=release/complete/v1.1.0 " "$GH_LOG" || true; }
chk "R18 a root that owes no subpackage is receipted on its own readback" "$(hreadback '[]')|$(receipted)" "ok|1"
chk "R18 a release interrupted after its root tag is not receipted" "$(hreadback '["web"]')|$(receipted)" "refused|0"
chk_has "R18 naming the subpackage" "$(cat "$WORK/hreadback.log")" "npm @o/web@1.1.0 does not answer"
rm -rf "$WORK/rt"
mkdir -p "$WORK/rt"
: >"$SP_LOG"
(cd "$HC" && PATH="$BIN2:$PATH" CURL_DIR="$HCURL" RUNNER_TEMP="$WORK/rt" CI_TOOLS="$ROOT/scripts" TAG=v1.1.0 COMMIT="$H1" \
  SUBPACKAGES='["web"]' JSR_VERSION="$(fact '."repair-complete-env".JSR_VERSION')" bash "$WORK/repair-complete.sh") \
  >"$WORK/hcomplete.log" 2>&1 || fail "the repair's subpackage step failed: $(cat "$WORK/hcomplete.log")"
chk "R19 the repair publishes the subpackage from the tag's own tree, on stable" \
  "$(grep -E '^(npm publish|published|npx)' "$SP_LOG" | tr '\n' ';')" \
  "npm publish --access public @web;published v2;npx -y jsr@0.14.3 publish --allow-dirty @web;"
chk "R19 under the root version" "$(cat "$SP_LOG.version")" "1.1.0"
chk "R19 then reads it back and records the receipt" "$(hreadback '["web"]')|$(receipted)" "ok|1"
sed -i '/jsr.io/d' "$HCURL/ok"
chk "R18 a subpackage missing from JSR is not receipted either" "$(hreadback '["web"]')|$(receipted)" "refused|0"
rm "$GH_DIR/release-v1.1.0"

# The subpackage job publishes through the same script, as HEAD's inline step did.
sp_run() { # <head|new> <channel> <npm has it: y|n> <jsr has it: y|n> -> the npm and npx calls, then jsr.json
  local d c
  d=$(mktemp -d "$WORK/sp.XXXXXX")
  c="$d/curl"
  mkdir -p "$d/web" "$c"
  printf '%s\n' '{"name":"@o/web","version":"0.0.0"}' >"$d/web/jsr.json"
  printf '%s\n' '{"name":"@o/web","version":"0.0.0"}' >"$d/web/package.json"
  echo v5 >"$d/web/mod.ts"
  : >"$c/ok"
  [ "$3" = n ] || echo "https://registry.npmjs.org/@o/web/2.0.0" >>"$c/ok"
  [ "$4" = n ] || echo 'https://jsr.io/@o/web/meta.json {"versions":{"2.0.0":{}}}' >"$c/docs"
  : >"$SP_LOG"
  (cd "$d/web" && PATH="$BIN2:$PATH" CURL_DIR="$c" VERSION=v2.0.0 CHANNEL="$2" CI_TOOLS="$ROOT/scripts" \
    JSR_VERSION="$(fact '."subpkg-env".JSR_VERSION')" bash "$WORK/subpkg-$1.sh") >/dev/null 2>&1 || echo "EXIT=$?"
  cat "$SP_LOG" "$d/web/jsr.json"
}
for sp_case in "stable n n" "stable y y" "stable n y" "stable y n" "dev n n" "dev y n"; do
  read -r sp_ch sp_npm sp_jsr <<<"$sp_case"
  chk "R20 the subpackage publish matches HEAD's inline step ($sp_case)" \
    "$(sp_run new "$sp_ch" "$sp_npm" "$sp_jsr")" "$(sp_run head "$sp_ch" "$sp_npm" "$sp_jsr")"
done
chk_has "R20 a stable publish still reaches JSR" "$(sp_run new stable n n)" "npx -y jsr@0.14.3 publish --allow-dirty @web"
chk_lacks "R20 and the shared script runs to the end" "$(sp_run new stable n n)$(sp_run new dev n n)" "EXIT="

# ── The dev barrier ──────────────────────────────────────────────────────────
rm -rf "$W"
git clone -q "$O" "$W"
git -C "$W" checkout -q --detach "$R1"
BARRIER_STATE=$(jq -c --arg r "$R1" '{".": {commit: $r, in_range: true, version: ""}, "yamlenv": {commit: "", in_range: false, version: ""}}' <<<'{}')
printf '{"workflow_runs":[{"id":1,"status":"in_progress","created_at":"2026-10-01T11:00:00Z"},{"id":3,"status":"in_progress","created_at":"2026-10-01T12:00:03Z"},{"id":2,"status":"in_progress","created_at":"2026-10-01T13:00:00Z"}]}\n' >"$GH_DIR/runs-busy.json"
printf '{"workflow_runs":[{"id":1,"status":"completed","created_at":"2026-10-01T11:00:00Z"},{"id":3,"status":"completed","created_at":"2026-10-01T12:00:03Z"},{"id":2,"status":"in_progress","created_at":"2026-10-01T13:00:00Z"}]}\n' >"$GH_DIR/runs-idle.json"
# R1 was written at 12:00:00; main took it, and this stable run was created, at 12:00:05.
printf '{"id":7,"created_at":"2026-10-01T12:00:05Z"}\n' >"$GH_DIR/run-7.json"
barrier() { # [state] -> runs the barrier at R1; prints EXIT=n on failure
  : >"$GH_LOG"
  rm -f "$GH_DIR/runs-calls" "$GH_DIR/runs-queries" "$GH_DIR/head-calls" "$GH_DIR/next-id"
  (cd "$W" && STATE="${1:-$BARRIER_STATE}" ROOT_VERSION=v1.1.0 WORKFLOW=release.yaml BARRIER_POLLS="${POLLS:-7}" \
    RENUMBER_POLLS="${RESERVE:-0}" bash "$RS" barrier) >"$WORK/barrier.log" 2>&1 || echo "EXIT=$?"
}
dispatched() { grep -cF 'actions/workflows/release.yaml/dispatches -f ref=dev -f inputs[mode]=renumber --jq' "$GH_LOG" || true; }
export GH_BUSY_CALLS=2
out_b=$(barrier)
chk "B1 the barrier waits for the dev run created before R" "$out_b|$(grep -c '^waiting for dev release runs' "$WORK/barrier.log")" "|2"
chk "B1 a dev run created after R is not waited for, once two polls agree" "$(cat "$GH_DIR/runs-calls")" "4"
chk_has "B1 a dev run created between R's commit and main's update is waited for" "$(cat "$WORK/barrier.log")" \
  "waiting for dev release runs created before 2026-10-01T12:00:05Z (2)"
mv "$GH_DIR/run-7.json" "$GH_DIR/run-7.saved"
out_b=$(barrier)
mv "$GH_DIR/run-7.saved" "$GH_DIR/run-7.json"
chk "B1 an unreadable run creation time holds the publish" "$out_b|$(grep -c 'actions/workflows' "$GH_LOG" || true)" "EXIT=1|0"
chk "B2 with no dev build past the promoted commit nothing is dispatched" "$(dispatched)" "0"
GH_BUSY_CALLS=9
out_b=$(barrier)
chk "B3 a dev run that never finishes holds the publish" "$out_b" "EXIT=1"
chk_has "B3 and says why" "$(cat "$WORK/barrier.log")" "gave up waiting for dev release runs"

# A dev run past T pushed its image or npm version, then died before its git
# tag: no dev tag shows the build, so the rank checks cannot see it.
GH_BUSY_CALLS=0
echo "sha256:$(printf 'e%.0s' $(seq 64))" >"$CURL_DIR/manifest-sha-$D3"
out_b=$(POLLS=5 REPO_TYPE=docker IMAGE=o/app barrier)
chk "B19 an untagged image past T waits for every dev run, the later one included" \
  "$out_b|$(dispatched)|$(grep -c '^waiting for every dev release run' "$WORK/barrier.log")" "EXIT=1|0|3"
chk_has "B19 and gives up holding the publish" "$(cat "$WORK/barrier.log")" "gave up waiting for every dev release run"
cp "$GH_DIR/runs-idle.json" "$GH_DIR/runs-idle.saved"
jq '.workflow_runs[].status = "completed"' "$GH_DIR/runs-idle.saved" >"$GH_DIR/runs-idle.json"
out_b=$(REPO_TYPE=docker IMAGE=o/app barrier)
chk "B19 once every run finished, an image still untagged holds the publish and dispatches nothing" "$out_b|$(dispatched)" "EXIT=1|0"
chk_has "B19 naming the build" "$(cat "$WORK/barrier.log")" \
  "lane .: dev's build of $D3 is published with no dev tag and no newer build is tagged, so its version cannot rank above v1.1.0"
chk_has "B19 and the dev release that supersedes it, not a rerun of its own" "$(cat "$WORK/barrier.log")" \
  "Dispatch release.yaml on dev to build and tag dev's head, then rerun this run."
# An older tagged build past T does not cover a newer untagged one.
git -C "$O" tag v1.1.0-dev.9 "$D2"
out_b=$(REPO_TYPE=docker IMAGE=o/app barrier)
git -C "$O" tag -d v1.1.0-dev.9 >/dev/null
git -C "$W" tag -d v1.1.0-dev.9 >/dev/null
chk "B19 an untagged image newer than every tagged build past T still holds the publish" "$out_b|$(dispatched)" "EXIT=1|0"
chk_has "B19 naming the newer build" "$(cat "$WORK/barrier.log")" "lane .: dev's build of $D3 is published with no dev tag"
echo 503 >"$CURL_DIR/status-sha-$D3"
rm "$CURL_DIR/manifest-sha-$D3"
out_b=$(REPO_TYPE=docker IMAGE=o/app barrier)
rm "$CURL_DIR/status-sha-$D3"
chk "B19 a GHCR that cannot answer holds the publish" "$out_b|$(dispatched)" "EXIT=1|0"
chk_has "B19 and says so" "$(cat "$WORK/barrier.log")" "lane .: cannot tell whether dev holds a published, untagged build"
out_b=$(REPO_TYPE=docker IMAGE=o/app barrier)
chk "B19 with no image past T, nothing is held" "$out_b|$(dispatched)" "|0"
printf '{"name":"@o/app"}\n' >"$W/package.json"
echo "https://registry.npmjs.org/@o/app {\"versions\":{\"1.0.0-dev.4\":{\"gitHead\":\"$D3\"}}}" >"$CURL_DIR/docs"
out_b=$(REPO_TYPE=ts barrier)
chk "B19 an npm dev version published from a commit past T with no tag holds the publish" "$out_b|$(dispatched)" "EXIT=1|0"
chk_has "B19 naming that commit" "$(cat "$WORK/barrier.log")" "lane .: dev's build of $D3 is published with no dev tag"
git -C "$O" tag v1.1.0-dev.9 "$D2"
out_b=$(REPO_TYPE=ts barrier)
git -C "$O" tag -d v1.1.0-dev.9 >/dev/null
git -C "$W" tag -d v1.1.0-dev.9 >/dev/null
chk "B19 an npm dev version newer than every tagged build past T still holds the publish" "$out_b|$(dispatched)" "EXIT=1|0"
FIRST=$(git -C "$O" rev-list --max-parents=0 "$D1")
chk "B19 the fixture's first commit carries no dev tag" "$(git -C "$O" tag --points-at "$FIRST" | grep -c -- '-dev\.' || true)" "0"
echo "https://registry.npmjs.org/@o/app {\"versions\":{\"1.0.0-dev.4\":{\"gitHead\":\"$FIRST\"},\"1.0.0\":{\"gitHead\":\"$D3\"}}}" >"$CURL_DIR/docs"
out_b=$(REPO_TYPE=ts barrier)
chk "B19 npm dev versions T reaches, and stable versions, hold nothing" "$out_b|$(dispatched)" "|0"
rm "$CURL_DIR/docs"
echo "https://registry.npmjs.org/@o/app" >"$CURL_DIR/down"
out_b=$(REPO_TYPE=ts barrier)
rm "$CURL_DIR/down" "$W/package.json"
chk "B19 an npm registry that cannot answer holds the publish" "$out_b|$(dispatched)" "EXIT=1|0"
# Every dev run has finished from here on, so a renumber dispatch waits for none.
rm "$GH_DIR/runs-idle.saved"

GH_BUSY_CALLS=0
git -C "$O" tag v1.1.0-dev.2 "$D3"
make_hook() { # <conclusion> <tag to create on dev, or ''>: the renumber dispatch creates run 99, which concludes so
  cat >"$GH_DIR/on-dispatch" <<SH
#!/usr/bin/env bash
echo 99 >"$GH_DIR/dispatched-id"
echo '{"status":"completed","conclusion":"$1"}' >"$GH_DIR/run-99.json"
${2:+git -C "$O" tag "$2" "$D3"}
SH
  chmod 755 "$GH_DIR/on-dispatch"
}
make_hook success ""
# A run whose tag create failed once dev moved on left its build untagged; the
# next run's tagged build carries it, so renumber covers it.
echo "sha256:$(printf 'e%.0s' $(seq 64))" >"$CURL_DIR/manifest-sha-$D2"
out_b=$(REPO_TYPE=docker IMAGE=o/app barrier)
rm "$CURL_DIR/manifest-sha-$D2"
chk "B19 an untagged image below a newer tagged build past T holds nothing and dispatches renumber" \
  "$(dispatched)|$(grep -c 'published with no dev tag' "$WORK/barrier.log" || true)" "1|0"
make_hook success ""
printf '{"name":"@o/app"}\n' >"$W/package.json"
echo "https://registry.npmjs.org/@o/app {\"versions\":{\"1.1.0-dev.5\":{\"gitHead\":\"$D2\"}}}" >"$CURL_DIR/docs"
out_b=$(REPO_TYPE=ts barrier)
rm "$CURL_DIR/docs" "$W/package.json"
chk "B19 as does an npm dev version below a newer tagged build" \
  "$(dispatched)|$(grep -c 'published with no dev tag' "$WORK/barrier.log" || true)" "1|0"
SIDE=$(git -C "$O" commit-tree "$D3^{tree}" -p "$D3" -m "feat: a side-branch experiment")
git -C "$O" tag v1.3.0-dev.1 "$SIDE"
make_hook success ""
out_b=$(barrier)
chk "B28 a dev tag above the version on a commit dev does not contain releases no hold" "$out_b|$(dispatched)" "EXIT=1|1"
chk_has "B28 the renumber must leave a build on dev" "$(cat "$WORK/barrier.log")" "lane .: renumber run 99 left no dev build above v1.1.0"
git -C "$O" tag -d v1.3.0-dev.1 >/dev/null
git -C "$W" tag -d v1.3.0-dev.1 >/dev/null
make_hook success ""
out_b=$(barrier)
chk "B4 a dev build past T numbered below the version dispatches renumber" "$(dispatched)" "1"
chk "B4 and a renumber that numbers nothing above it holds the publish" "$out_b" "EXIT=1"
chk_has "B4 naming the lane" "$(cat "$WORK/barrier.log")" "lane .: renumber run 99 left no dev build above v1.1.0"
make_hook failure ""
out_b=$(barrier)
chk "B5 a failed renumber run holds the publish" "$out_b|$(dispatched)" "EXIT=1|1"
chk_has "B5 and says so" "$(cat "$WORK/barrier.log")" "renumber run 99 concluded 'failure'"
make_hook success v1.2.0-dev.1
out_b=$(barrier)
chk "B6 a renumber that ranks dev above the version releases the hold" "$out_b|$(dispatched)" "|1"
chk "B6 waiting for the new run, not an older dispatch" "$(grep -c '^api repos/o/app/actions/runs/99 ' "$GH_LOG")" "2"
rm -f "$GH_DIR/on-dispatch"
out_b=$(barrier)
chk "B7 once dev ranks above the version nothing is dispatched" "$out_b|$(dispatched)" "|0"
out_b=$(barrier '{".": {"commit": "", "in_range": false, "version": ""}}')
chk "B8 nothing pending in range holds nothing" "$out_b|$(wc -l <"$GH_LOG")" "|0"
cp "$GH_DIR/runs-busy.json" "$GH_DIR/runs-busy.saved"
cp "$GH_DIR/runs-idle.json" "$GH_DIR/runs-idle.saved"
printf '{"workflow_runs":[{"id":4,"status":"queued","created_at":"2026-10-01T12:00:05Z"}]}\n' >"$GH_DIR/runs-busy.json"
printf '{"workflow_runs":[{"id":4,"status":"completed","created_at":"2026-10-01T12:00:05Z"}]}\n' >"$GH_DIR/runs-idle.json"
GH_BUSY_CALLS=1
out_b=$(barrier)
chk "B9 a dev run created in the cutoff's own second is waited for" "$out_b|$(grep -c '^waiting for dev release runs' "$WORK/barrier.log")" "|1"
jq -n '{workflow_runs: [range(100) | {id: (100 + .), status: "completed", created_at: "2026-10-01T10:00:00Z"}]}' >"$GH_DIR/runs-busy.json"
printf '{"workflow_runs":[{"id":5,"status":"in_progress","created_at":"2026-10-01T11:30:00Z"}]}\n' >"$GH_DIR/runs-busy.json.p2"
out_b=$(barrier)
chk "B10 an unfinished dev run on the second page is waited for" "$out_b|$(grep -c '^waiting for dev release runs' "$WORK/barrier.log")" "|1"
rm "$GH_DIR/runs-busy.json.p2"
printf '{"workflow_runs":[{"id":6,"status":"waiting","created_at":"2026-10-01T11:00:00Z"},{"id":7,"status":"requested","created_at":"2026-10-01T11:00:00Z"},{"id":8,"status":"pending","created_at":"2026-10-01T11:00:00Z"}]}\n' >"$GH_DIR/runs-busy.json"
out_b=$(barrier)
chk_has "B12 waiting, requested and pending dev runs are waited for" "$out_b|$(cat "$WORK/barrier.log")" \
  "waiting for dev release runs created before 2026-10-01T12:00:05Z (3)"
chk "B12 each poll asks for the five unfinished statuses only" \
  "$(sed -n 's/.*runs?branch=dev&per_page=100&status=\([a-z_]*\) .*/\1/p' "$GH_LOG" | sort -u | tr '\n' ' ')" \
  "in_progress pending queued requested waiting "
chk "B12 and never lists every dev run" "$(grep -c 'runs?branch=dev&per_page=100 ' "$GH_LOG" || true)" "0"
chk "B12 in lifecycle order" \
  "$(sed -n 's/.*runs?branch=dev&per_page=100&status=\([a-z_]*\) .*/\1/p' "$GH_LOG" | head -5 | tr '\n' ' ')" \
  "requested pending waiting queued in_progress "
# Each status is its own read, so a live run can change status between two
# reads of one poll: forward from pending to queued, or back into waiting.
seq_barrier() { # <"<from query> <status>" ...> -> runs the barrier with run 9 in each status from that query on
  local kv
  : >"$GH_DIR/runs-seq"
  for kv in "$@"; do
    printf '{"workflow_runs":[{"id":9,"status":"%s","created_at":"2026-10-01T11:00:00Z"}]}\n' "${kv#* }" >"$GH_DIR/runs-${kv%% *}.json"
    echo "${kv%% *} $GH_DIR/runs-${kv%% *}.json" >>"$GH_DIR/runs-seq"
  done
  barrier
  rm -f "$GH_DIR/runs-seq" "$GH_DIR"/runs-[0-9]*.json
}
out_b=$(seq_barrier "1 pending" "2 queued" "11 completed")
chk "B13 a run that leaves pending for queued mid-poll is still waited for" \
  "$out_b|$(grep -c '^waiting for dev release runs created before 2026-10-01T12:00:05Z (1)$' "$WORK/barrier.log")" "|2"
out_b=$(seq_barrier "1 in_progress" "5 waiting" "11 completed")
chk "B13 a run that moves back into waiting after its status was read is met on the next poll" \
  "$out_b|$(grep -c '^waiting for dev release runs created before 2026-10-01T12:00:05Z (1)$' "$WORK/barrier.log")" "|1"
chk "B13 and the hold ends only after two polls read zero" "$(cat "$GH_DIR/runs-calls")" "4"
mv "$GH_DIR/runs-busy.saved" "$GH_DIR/runs-busy.json"
mv "$GH_DIR/runs-idle.saved" "$GH_DIR/runs-idle.json"
GH_BUSY_CALLS=0

# A TS root's dev channel is npm: a git tag alone does not rank dev above.
printf '{"name":"@o/app"}\n' >"$W/package.json"
make_hook success ""
out_b=$(REPO_TYPE=ts barrier)
chk "B11 a git-only dev tag above the version does not release a TS hold" "$out_b|$(dispatched)" "EXIT=1|1"
chk_has "B11 the renumber must leave a published build" "$(cat "$WORK/barrier.log")" "lane .: renumber run 99 left no dev build above v1.1.0"
echo "https://registry.npmjs.org/@o/app/1.2.0-dev.1" >>"$CURL_DIR/ok"
rm -f "$GH_DIR/on-dispatch"
out_b=$(REPO_TYPE=ts barrier)
chk "B11 the same tag once npm serves it releases the hold" "$out_b|$(dispatched)" "|0"
rm "$W/package.json"

# One read budget covers every wait: what the dev runs spent, the renumber run's wait cannot.
git -C "$O" tag -d v1.2.0-dev.1 >/dev/null
git -C "$W" tag -d v1.2.0-dev.1 >/dev/null
GH_BUSY_CALLS=2
out_b=$(barrier)
chk "B14 a renumber dispatch that names no run holds the publish" "$out_b|$(dispatched)" "EXIT=1|1"
chk_has "B14 and says so" "$(cat "$WORK/barrier.log")" \
  "the renumber dispatch named no run (''), so the stable publish stays held"
make_hook success ""
echo '{"status":"in_progress","conclusion":null}' >"$GH_DIR/run-99.json"
sed -i '/run-99.json/d' "$GH_DIR/on-dispatch"
out_b=$(barrier)
rm -f "$GH_DIR/on-dispatch"
chk "B14 a renumber run that never finishes holds the publish" "$out_b|$(dispatched)" "EXIT=1|1"
chk_has "B14 and says what it waited for" "$(cat "$WORK/barrier.log")" "gave up waiting for renumber run 99"
chk "B14 the wait gets only the reads the dev-run waits left" \
  "$(cat "$GH_DIR/runs-calls")|$(grep -c '^api repos/o/app/actions/runs/99 ' "$GH_LOG")" "6|1"
# The dev-run wait stops short of the reads reserved for the renumber run.
GH_BUSY_CALLS=9
out_b=$(POLLS=8 RESERVE=3 barrier)
chk "B16 a dev-run wait gives up with the renumber reserve unspent" "$out_b|$(cat "$GH_DIR/runs-calls")|$(dispatched)" "EXIT=1|5|0"
chk_has "B16 naming the dev runs" "$(cat "$WORK/barrier.log")" "gave up waiting for dev release runs"
GH_BUSY_CALLS=3
make_hook success v1.2.0-dev.1
out_b=$(POLLS=10 RESERVE=3 barrier)
chk "B17 dev-run waits that spend their whole share leave the renumber its reserve" \
  "$out_b|$(cat "$GH_DIR/runs-calls")|$(dispatched)" "|7|1"
rm -f "$GH_DIR/on-dispatch"
git -C "$O" tag -d v1.2.0-dev.1 >/dev/null
git -C "$W" tag -d v1.2.0-dev.1 >/dev/null
GH_BUSY_CALLS=0

# Dev's push runs and the renumber run share one concurrency group, where a
# newly queued run cancels the pending one.
cp "$GH_DIR/runs-busy.json" "$GH_DIR/runs-busy.saved"
printf '{"workflow_runs":[{"id":2,"status":"in_progress","created_at":"2026-10-01T13:00:00Z"}]}\n' >"$GH_DIR/runs-busy.json"
GH_BUSY_CALLS=3
make_hook success v1.2.0-dev.1
out_b=$(POLLS=8 barrier)
chk "B22 a renumber dispatch first waits for the dev run created after R" \
  "$out_b|$(dispatched)|$(cat "$GH_DIR/runs-calls")" "|1|5"
chk_has "B22 naming why" "$(cat "$WORK/barrier.log")" \
  "waiting for every dev release run, before the renumber run joins their concurrency group (1)"
chk "B22 and dispatches only after every poll" \
  "$(awk '/status=requested/ { n++ } /dispatches -f ref=dev -f inputs\[mode\]=renumber / { print n; exit }' "$GH_LOG")" "5"
git -C "$O" tag -d v1.2.0-dev.1 >/dev/null
git -C "$W" tag -d v1.2.0-dev.1 >/dev/null
rm -f "$GH_DIR/on-dispatch"
cat >"$GH_DIR/on-idle" <<SH
#!/usr/bin/env bash
git -C "$O" tag v1.2.0-dev.1 "$D3"
SH
chmod 755 "$GH_DIR/on-idle"
out_b=$(POLLS=8 barrier)
chk "B22 a dev run that finished meanwhile ranking dev above the version leaves nothing to dispatch" \
  "$out_b|$(dispatched)|$(cat "$GH_DIR/runs-calls")" "|0|5"
git -C "$O" tag -d v1.2.0-dev.1 >/dev/null
git -C "$W" tag -d v1.2.0-dev.1 >/dev/null
GH_BUSY_CALLS=9
out_b=$(POLLS=8 RESERVE=3 barrier)
chk "B22 that wait stops short of the renumber reserve" "$out_b|$(cat "$GH_DIR/runs-calls")|$(dispatched)" "EXIT=1|5|0"
chk_has "B22 naming it" "$(cat "$WORK/barrier.log")" \
  "gave up waiting for every dev release run, before the renumber run joins their concurrency group"
mv "$GH_DIR/runs-busy.saved" "$GH_DIR/runs-busy.json"
GH_BUSY_CALLS=0

# A renumber run builds nothing and a push run can fail, so every pass that
# releases the hold needs a release run of dev's head that succeeded; a dev tag
# at the head or a dispatched renumber run is no proof of one.
DEVH=$(git -C "$O" commit-tree "$D3^{tree}" -p "$D3" -m "docs: dev's newest change")
git -C "$O" update-ref refs/heads/dev "$DEVH"
rebuilt() { grep -cF 'actions/workflows/release.yaml/dispatches -f ref=dev --jq' "$GH_LOG" || true; }
head_asked() { grep -c "runs?branch=dev&per_page=100&head_sha=$DEVH " "$GH_LOG" || true; }
head_runs() { # <"id status conclusion event created_at" ...>; conclusion "null" while unfinished
  printf '%s\n' "$@" | jq -Rn '{workflow_runs: [inputs | split(" ") | {id: (.[0] | tonumber), status: .[1],
    conclusion: (if .[2] == "null" then null else .[2] end), event: .[3], created_at: .[4]}]}' >"$GH_DIR/head-runs.json"
}
untag() { git -C "$O" tag -d "$1" >/dev/null && git -C "$W" tag -d "$1" >/dev/null; }
# A normal dev dispatch creates run 51, then 52 and so on, at dev's head as it stands.
rebuild_run() { # <conclusion> -> the hook lines that create the next run, concluding so
  cat <<SH
id=\$(cat "$GH_DIR/next-id" 2>/dev/null || echo 51)
echo \$((id + 1)) >"$GH_DIR/next-id"
echo "\$id" >"$GH_DIR/dispatched-id"
printf '{"status":"completed","conclusion":"%s","head_sha":"%s"}\n' "$1" "\$(git -C "$O" rev-parse refs/heads/dev)" >"$GH_DIR/run-\$id.json"
SH
}
make_rebuild() { # <conclusion> [tag to create on dev's head]: a normal dev dispatch adds a run that concludes so
  {
    echo '#!/usr/bin/env bash'
    rebuild_run "$1"
    echo "${2:+git -C "$O" tag "$2" "$DEVH"}"
  } >"$GH_DIR/on-rebuild"
  chmod 755 "$GH_DIR/on-rebuild"
}
make_rebuild success
POLLS=12
CANCELLED="61 completed cancelled push 2026-10-01T12:01:00Z"
RENUMBER="99 completed success workflow_dispatch 2026-10-01T12:05:00Z"
make_hook success v1.2.0-dev.1
head_runs "$CANCELLED" "$RENUMBER"
out_b=$(barrier)
chk "B23 a dev head whose own run the renumber run cancelled gets a dev release dispatched" \
  "$out_b|$(dispatched)|$(rebuilt)" "|1|1"
chk "B23 asking about dev's head" "$(head_asked)" "1"
chk_has "B23 and says so" "$(cat "$WORK/barrier.log")" \
  "dev's head $DEVH has no push release run that succeeded or is still running, so a dev release of it was dispatched"
chk_has "B23 naming the renumber run it dispatched" "$(cat "$WORK/barrier.log")" "dispatched the renumber run 99"
chk_has "B23 naming the dev release run it dispatched" "$(cat "$WORK/barrier.log")" "dispatched the dev release run 51"
for b23 in "completed success push 2026-10-01T12:02:00Z 0" \
  "completed success workflow_dispatch 2026-10-01T11:00:00Z 1" "completed failure push 2026-10-01T12:02:00Z 1" \
  "completed success workflow_dispatch 2026-10-01T12:03:00Z 1" "completed success workflow_dispatch 2026-10-01T12:00:05Z 1"; do
  untag v1.2.0-dev.1
  make_hook success v1.2.0-dev.1
  head_runs "$CANCELLED" "62 ${b23% *}" "$RENUMBER"
  out_b=$(barrier)
  chk "B23 a head whose other run is ${b23% *}: dev releases dispatched" "$out_b|$(dispatched)|$(rebuilt)" "|1|${b23##* }"
done
untag v1.2.0-dev.1
make_hook success v1.2.0-dev.1
head_runs "$CANCELLED" "62 in_progress null push 2026-10-01T12:02:00Z" "$RENUMBER"
out_b=$(barrier)
chk "B23 a head whose push run never finishes holds the publish and gets no dev release beside it" \
  "$out_b|$(dispatched)|$(rebuilt)" "EXIT=1|1|0"
chk_has "B23 naming the run it waited for" "$(cat "$WORK/barrier.log")" \
  "gave up waiting for the push release run of dev's head $DEVH"
untag v1.2.0-dev.1
make_hook success v1.2.0-dev.1
head_runs "60 completed failure push 2026-10-01T11:30:00Z" "$CANCELLED" "$RENUMBER"
out_b=$(barrier)
chk "B23 an earlier failed run of the head is no build of it" "$out_b|$(dispatched)|$(rebuilt)" "|1|1"
untag v1.2.0-dev.1
make_hook success v1.2.0-dev.1
git -C "$O" tag yamlenv/v1.1.0-dev.7 "$DEVH"
head_runs "$CANCELLED" "63 completed failure push 2026-10-01T12:02:00Z" "$RENUMBER"
out_b=$(barrier)
untag yamlenv/v1.1.0-dev.7
chk "B23 a head carrying a lane's dev tag beside a failed root run gets a dev release dispatched" \
  "$out_b|$(dispatched)|$(rebuilt)|$(head_asked)" "|1|1|1"
untag v1.2.0-dev.1
make_hook success v1.2.0-dev.1
git -C "$O" tag v1.1.0-dev.7 "$DEVH"
head_runs "$CANCELLED" "64 completed success push 2026-10-01T12:02:00Z" "$RENUMBER"
out_b=$(barrier)
untag v1.1.0-dev.7
chk "B23 a head whose push run succeeded needs no dispatch, tagged or not" "$out_b|$(dispatched)|$(rebuilt)|$(head_asked)" "|1|0|1"
head_runs "$CANCELLED" "$RENUMBER"
untag v1.2.0-dev.1
make_hook success v1.2.0-dev.1
touch "$GH_DIR/fail-head-runs"
out_b=$(barrier)
rm "$GH_DIR/fail-head-runs"
chk "B23 runs that cannot be listed hold the publish" "$out_b|$(rebuilt)" "EXIT=1|0"
chk_has "B23 naming the head and the remedy" "$(cat "$WORK/barrier.log")" "could not list the dev release runs of $DEVH. Rerun this run."
rm -f "$GH_DIR/on-dispatch"
out_b=$(barrier)
chk "B23 the rerun, dev already ranking above, still dispatches the head's dev release" \
  "$out_b|$(dispatched)|$(rebuilt)|$(head_asked)" "|0|1|1"
chk_has "B23 the rerun releases the hold on the renumbered rank" "$(cat "$WORK/barrier.log")" "lane .: dev already ranks above v1.1.0"
rm "$GH_DIR/head-runs.json"
out_b=$(barrier)
chk "B23 a rerun whose head has its own finished run dispatches nothing" "$out_b|$(dispatched)|$(rebuilt)|$(head_asked)" "|0|0|1"
untag v1.2.0-dev.1
make_rebuild failure
head_runs "$CANCELLED" "$RENUMBER"
rm -f "$GH_DIR/on-dispatch"
git -C "$O" tag v1.2.0-dev.1 "$D3"
out_b=$(barrier)
untag v1.2.0-dev.1
chk "B23 a dispatched dev release of the head that fails holds the publish" "$out_b|$(dispatched)|$(rebuilt)" "EXIT=1|0|1"
chk_has "B23 naming that run" "$(cat "$WORK/barrier.log")" "of dev's head $DEVH concluded 'failure', so the stable publish stays held"

# The dispatch answers with its own run, and another dispatch of the workflow
# (a renumber, a manual run) can replace it in dev's concurrency group.
{
  echo '#!/usr/bin/env bash'
  rebuild_run cancelled
  printf '%s\n' "printf '{\"status\":\"completed\",\"conclusion\":\"success\",\"head_sha\":\"%s\"}\n' $DEVH >\"$GH_DIR/run-\$((id + 1)).json\"" \
    "echo \$((id + 2)) >\"$GH_DIR/next-id\""
} >"$GH_DIR/on-rebuild"
chmod 755 "$GH_DIR/on-rebuild"
git -C "$O" tag v1.2.0-dev.1 "$D3"
out_b=$(barrier)
untag v1.2.0-dev.1
chk "B27 a dev release replaced by a later dispatch that succeeds holds the publish" \
  "$out_b|$(dispatched)|$(($(rebuilt) > 1))" "EXIT=1|0|1"
chk_has "B27 dispatching the head's dev release again" "$(cat "$WORK/barrier.log")" \
  "dev release run 51 was cancelled; settling dev's head again"
chk_has "B27 until the reads run out" "$(cat "$WORK/barrier.log")" "gave up waiting for dev release run"
chk "B27 never reading the other dispatch's run" "$(grep -c '^api repos/o/app/actions/runs/52 ' "$GH_LOG" || true)" "0"
{
  echo '#!/usr/bin/env bash'
  echo "c=success; if [ -f \"$GH_DIR/cancel-once\" ]; then rm \"$GH_DIR/cancel-once\"; c=cancelled; fi"
  rebuild_run '$c'
} >"$GH_DIR/on-rebuild"
chmod 755 "$GH_DIR/on-rebuild"
touch "$GH_DIR/cancel-once"
git -C "$O" tag v1.2.0-dev.1 "$D3"
out_b=$(barrier)
untag v1.2.0-dev.1
chk "B27 a cancelled dev release is dispatched again, and its success releases the hold" "$out_b|$(dispatched)|$(rebuilt)" "|0|2"
chk_has "B27 after the cancelled run" "$(cat "$WORK/barrier.log")" "dev release run 51 was cancelled; settling dev's head again"
NEWH=$(git -C "$O" commit-tree "$DEVH^{tree}" -p "$DEVH" -m "fix: a newer dev change")
{
  echo '#!/usr/bin/env bash'
  rebuild_run cancelled
  echo "git -C \"$O\" update-ref refs/heads/dev $NEWH"
  printf '%s\n' "echo '{\"workflow_runs\":[{\"id\":70,\"status\":\"completed\",\"conclusion\":\"success\",\"event\":\"push\",\"created_at\":\"2026-10-01T12:20:00Z\"}]}' >\"$GH_DIR/head-runs.json\""
} >"$GH_DIR/on-rebuild"
chmod 755 "$GH_DIR/on-rebuild"
git -C "$O" tag v1.2.0-dev.1 "$D3"
out_b=$(barrier)
untag v1.2.0-dev.1
git -C "$O" update-ref refs/heads/dev "$DEVH"
head_runs "$CANCELLED" "$RENUMBER"
chk "B27 a dev release cancelled by a newer dev push settles on that push's run" "$out_b|$(dispatched)|$(rebuilt)" "|0|1"
chk "B27 asking about the new head" "$(grep -c "runs?branch=dev&per_page=100&head_sha=$NEWH " "$GH_LOG" || true)" "2"
{
  echo '#!/usr/bin/env bash'
  echo "git -C \"$O\" update-ref refs/heads/dev $NEWH"
  rebuild_run success
} >"$GH_DIR/on-rebuild"
chmod 755 "$GH_DIR/on-rebuild"
git -C "$O" tag v1.2.0-dev.1 "$D3"
out_b=$(barrier)
untag v1.2.0-dev.1
git -C "$O" update-ref refs/heads/dev "$DEVH"
chk "B27 a dev release that built a head newer than the one fetched settles the head it built" \
  "$out_b|$(dispatched)|$(rebuilt)" "|0|1"
# A push run of dev's head created after the cutoff is not waited for by the
# cutoff poll, and no dev tag shows its build yet: the hold waits for it, then
# ranks dev again.
untag v1.1.0-dev.2
b26() { # <later conclusion> -> runs the barrier with dev's head push run unfinished for two reads, then so
  head_runs "62 in_progress null push 2026-10-01T12:02:00Z"
  head_runs "62 completed $1 push 2026-10-01T12:02:00Z"
  mv "$GH_DIR/head-runs.json" "$GH_DIR/head-runs-later.json"
  head_runs "62 in_progress null push 2026-10-01T12:02:00Z"
  HEAD_BUSY_CALLS=2 barrier
  rm -f "$GH_DIR/head-runs-later.json" "$GH_DIR/head-runs.json" "$GH_DIR/on-head-later"
}
make_rebuild success v1.2.0-dev.1
out_b=$(b26 failure)
chk_has "B26 the fixture's dev holds no build past the promoted commit" "$(cat "$WORK/barrier.log")" \
  "lane .: dev holds no build past the promoted commit"
chk "B26 a head push run that fails after the cutoff gets a dev release, waited for" "$out_b|$(dispatched)|$(rebuilt)" "|0|1"
chk_has "B26 having waited for the push run" "$(cat "$WORK/barrier.log")" \
  "waiting for the push release run of dev's head $DEVH (1)"
chk_has "B26 and ranked dev again on the build it made" "$(cat "$WORK/barrier.log")" "lane .: dev already ranks above v1.1.0"
untag v1.2.0-dev.1
make_rebuild failure
out_b=$(b26 failure)
chk "B26 one whose dev release fails too holds the publish" "$out_b|$(dispatched)|$(rebuilt)" "EXIT=1|0|1"
chk_has "B26 naming that run" "$(cat "$WORK/barrier.log")" \
  "dev release run 51 of dev's head $DEVH concluded 'failure', so the stable publish stays held"
rm "$GH_DIR/on-rebuild"
out_b=$(b26 success)
chk "B26 a head push run that succeeds after the wait releases the hold" "$out_b|$(dispatched)|$(rebuilt)" "|0|0"
chk_has "B26 after waiting for it" "$(cat "$WORK/barrier.log")" "waiting for the push release run of dev's head $DEVH (1)"
cat >"$GH_DIR/on-head-later" <<SH
#!/usr/bin/env bash
git -C "$O" tag v1.1.0-dev.3 "$DEVH"
SH
chmod 755 "$GH_DIR/on-head-later"
make_hook success v1.2.0-dev.1
out_b=$(b26 success)
untag v1.1.0-dev.3
untag v1.2.0-dev.1
chk "B26 one whose build ranks below the version holds the publish behind a renumber" "$out_b|$(dispatched)|$(rebuilt)" "|1|0"
chk_has "B26 naming the rank" "$(cat "$WORK/barrier.log")" \
  "lane .: dev holds a build past the promoted commit numbered below v1.1.0"
rm -f "$GH_DIR/on-dispatch"
git -C "$O" tag v1.1.0-dev.2 "$D3"
unset POLLS
git -C "$O" update-ref refs/heads/dev "$D3"
# A dev push queued behind the renumber run cancels it, and its own build
# ranks dev above the version.
make_hook cancelled v1.2.0-dev.1
out_b=$(POLLS=9 barrier)
chk "B24 a renumber run cancelled by a newer dev run that ranks dev above releases the hold" "$out_b|$(dispatched)" "|1"
chk "B24 after waiting for every dev run" \
  "$(grep -c '^waiting for every dev release run, as renumber run 99 was cancelled' "$WORK/barrier.log" || true)|$(cat "$GH_DIR/runs-calls")" "0|6"
git -C "$O" tag -d v1.2.0-dev.1 >/dev/null
git -C "$W" tag -d v1.2.0-dev.1 >/dev/null
make_hook cancelled ""
out_b=$(POLLS=9 barrier)
chk "B24 one that leaves dev below the version holds the publish" "$out_b|$(dispatched)" "EXIT=1|1"
chk_has "B24 naming the lane" "$(cat "$WORK/barrier.log")" "lane .: renumber run 99 left no dev build above v1.1.0"
rm -f "$GH_DIR/on-dispatch"
git -C "$O" tag v1.2.0-dev.1 "$D3"
polls=$(grep -o 'BARRIER_POLLS:-[0-9]*' "$RS" | sort -u | cut -d- -f2)
poll_s=$(grep -o 'POLL_SECONDS:-[0-9]*' "$RS" | sort -u | cut -d- -f2)
reserve=$(grep -o 'RENUMBER_POLLS:-[0-9]*' "$RS" | sort -u | cut -d- -f2)
deadline=$(grep -o 'BARRIER_SECONDS:-[0-9]*' "$RS" | sort -u | cut -d- -f2)
kill_after=$(grep -o 'timeout --kill-after=[0-9]* "$BARRIER_SECONDS"' "$RS" | sed 's/.*=\([0-9]*\) .*/\1/') \
  || fail "B15 the barrier does not run under its deadline"
chk "B15 the barrier's deadline, its kill grace and 5 min of setup fit its job timeout" \
  "$deadline|$kill_after|$(($(fact '."barrier-timeout"') * 60 >= deadline + kill_after + 300))" "9000|10|1"
chk "B15 and the deadline leaves the read budget's sleeps whole" "$polls|$poll_s|$((deadline >= polls * poll_s))" "210|30|1"
chk "B15 the renumber reserve outlasts the three chained renumber jobs' timeouts plus 5 min of queueing each" \
  "$reserve|$((reserve * poll_s >= $(fact '."renumber-timeout"') * 60 + 900))" "90|1"
chk "B15 and leaves the dev-run wait an hour of reads" "$(((polls - reserve) * poll_s >= 3600))" "1"
# A status read that never answers spends no read, so only the deadline ends it.
touch "$GH_DIR/hang-runs"
t0=$(date +%s)
out_b=$(BARRIER_SECONDS=2 barrier)
t1=$(date +%s)
rm "$GH_DIR/hang-runs"
chk "B18 a wedged status read holds the publish at the barrier's own deadline" "$out_b|$(((t1 - t0) < 30))" "EXIT=1|1"
chk_has "B18 with the barrier's reason" "$(cat "$WORK/barrier.log")" \
  "gave up waiting, because the barrier passed its 2 s deadline. The stable publish stays held."
chk "B18 and dispatches nothing" "$(dispatched)" "0"
# The deadline's exit status is the runner's timeout(1); name which one ran.
echo "info: B18 ran under $(timeout --version 2>&1 | head -1)"

# ── Renumber ─────────────────────────────────────────────────────────────────
git -C "$O" tag -d v1.2.0-dev.1 >/dev/null
rm -rf "$W"
git clone -q "$O" "$W"
git -C "$W" checkout -q --detach origin/dev
renumber() { # <repo type> <dev version> [channel]
  : >"$GH_LOG"
  : >"$DOCKER_LOG"
  (cd "$W" && CHANNEL="${3:-dev}" LANE_KEY=. DEV_VERSION="$2" REPO_TYPE="$1" IMAGE=o/app bash "$RS" renumber) \
    >"$WORK/renumber.log" 2>&1 || echo "EXIT=$?"
}
chk "U1 a missing dev build is refused, never built" "$(renumber docker v1.2.0-dev.1)" "EXIT=1"
chk_has "U1 and says so" "$(cat "$WORK/renumber.log")" "renumber re-tags an existing build but never builds one"
chk "U1 with no docker call and no tag" "$(wc -l <"$DOCKER_LOG")|$(grep -c 'git/refs' "$GH_LOG" || true)" "0|0"
DIGEST="sha256:$(printf 'b%.0s' $(seq 64))"
echo "$DIGEST" >"$CURL_DIR/manifest-sha-$D3"
chk "U2 an existing dev build is re-tagged" "$(renumber docker v1.2.0-dev.1)" ""
chk "U2 by digest under the new pre-release" "$(cat "$DOCKER_LOG")" \
  "buildx imagetools create --metadata-file $(sed -n 's/.*--metadata-file \([^ ]*\).*/\1/p' "$DOCKER_LOG") -t ghcr.io/o/app:v1.2.0-dev.1 ghcr.io/o/app@$DIGEST"
chk "U2 the git tag lands on the built commit" "$(grep -c "^api repos/o/app/git/refs -f ref=refs/tags/v1.2.0-dev.1 -f sha=$D3$" "$GH_LOG")" "1"
chk "U2 with its dev tag receipt" "$(grep -c "statuses/$D3 -f state=success -f context=release/tag/v1.2.0-dev.1 " "$GH_LOG")" "1"
DOCKER_DIGEST="sha256:$(printf 'c%.0s' $(seq 64))"
export DOCKER_DIGEST
out_u=$(renumber docker v1.2.0-dev.1)
unset DOCKER_DIGEST
chk "U3 a re-tag that changes the digest is refused before any git tag" "$out_u|$(grep -c 'git/refs' "$GH_LOG" || true)" "EXIT=1|0"
echo "$DIGEST" >"$CURL_DIR/manifest-v1.2.0-dev.1"
chk "U13 a version tag already serving the build is not written again" "$(renumber docker v1.2.0-dev.1)|$(wc -l <"$DOCKER_LOG")" "|0"
chk "U13 and the build is tagged" "$(grep -c "^api repos/o/app/git/refs -f ref=refs/tags/v1.2.0-dev.1 -f sha=$D3$" "$GH_LOG")" "1"
# A newer dev run pushed its image under the version, then died before its
# git tag: renumber still selects the tagged build, and must not move the tag.
D2=$(commit_in "$W" "fix: a dev fix published and never tagged" main.go=v2)
DIGEST2="sha256:$(printf 'd%.0s' $(seq 64))"
echo "$DIGEST2" >"$CURL_DIR/manifest-sha-$D2"
echo "$DIGEST2" >"$CURL_DIR/manifest-v1.2.0-dev.1"
: >"$WORK/out"
(cd "$W" && LANE_KEY=. GITHUB_OUTPUT="$WORK/out" bash "$RS" renumber-target) >/dev/null 2>&1 || fail "renumber-target failed"
chk "U14 renumber-target passes over the untagged build" "$(outkey commit)" "$D3"
out_u=$(renumber docker v1.2.0-dev.1)
chk "U14 a version tag serving a newer untagged build is refused" \
  "$out_u|$(wc -l <"$DOCKER_LOG")|$(grep -c 'git/refs' "$GH_LOG" || true)" "EXIT=1|0|0"
chk_has "U14 naming both builds" "$(cat "$WORK/renumber.log")" \
  "ghcr.io/o/app:v1.2.0-dev.1 already serves $DIGEST2, not the build at $D3 ($DIGEST)"
rm "$CURL_DIR/manifest-v1.2.0-dev.1"
echo 503 >"$CURL_DIR/status-v1.2.0-dev.1"
out_u=$(renumber docker v1.2.0-dev.1)
rm "$CURL_DIR/status-v1.2.0-dev.1"
chk "U15 a version tag GHCR cannot answer for is not written" \
  "$out_u|$(wc -l <"$DOCKER_LOG")|$(grep -c 'git/refs' "$GH_LOG" || true)" "EXIT=1|0|0"
chk_has "U15 and says so" "$(cat "$WORK/renumber.log")" "cannot tell whether ghcr.io/o/app:v1.2.0-dev.1 exists"
chk "U4 a go root tags the commit with no image" "$(renumber go v1.2.0-dev.1)|$(wc -l <"$DOCKER_LOG")" "|0"
chk "U4 and records the receipt" "$(grep -c 'context=release/tag/v1.2.0-dev.1 ' "$GH_LOG")" "1"
chk "U4 publishing nothing to npm" "$(grep -c '^npm ' "$GH_LOG" || true)" "0"
chk "U5 a build already at or above the new version is left alone" "$(renumber docker v1.1.0-dev.3)|$(wc -l <"$GH_LOG")" "|0"
chk "U6 renumber outside dev is refused" "$(renumber docker v1.2.0-dev.1 stable)" "EXIT=1"
touch "$GH_DIR/fail-post"
chk "U7 a failed receipt deletes the tag the run created" "$(renumber go v1.2.0-dev.1)|$(grep -c '^api -X DELETE repos/o/app/git/refs/tags/v1.2.0-dev.1$' "$GH_LOG")" "EXIT=1|1"
rm "$GH_DIR/fail-post"

# The version and the tag name one commit: an older built fix below a newer
# unbuilt breaking commit is numbered and tagged at the build.
UF=$(commit_in "$W" "fix: a built dev fix" main.go=v3)
cp "$ROOT/configs/cliff-stable.toml" "$W/cliff.toml"
git -C "$W" tag v1.1.0-dev.3 "$UF"
UB=$(commit_in "$W" "feat!: an unbuilt break" main.go=v4)
dev_compute() { # -> the dev version compute.sh selects at W's HEAD
  : >"$WORK/cout"
  (cd "$W" && CHANNEL=dev RELEASE_MODEL=two-branch EXCLUDE_PATHS='yamlenv/**' CLIFF_BIN="$CLIFF" \
    GITHUB_OUTPUT="$WORK/cout" bash "$COMPUTE") >/dev/null 2>&1 || fail "compute.sh failed in $W"
  sed -n 's/^dev_version=//p' "$WORK/cout"
}
chk "U8 at the unbuilt head the arithmetic reads the break" "$(dev_compute)" "v2.0.0-dev.1"
chk "U8 a version computed past the build is refused" "$(renumber go v2.0.0-dev.1)|$(grep -c 'git/refs' "$GH_LOG" || true)" "EXIT=1|0"
chk_has "U8 naming the build to check out" "$(cat "$WORK/renumber.log")" "the newest build is v1.1.0-dev.3 at $UF"
: >"$WORK/out"
(cd "$W" && LANE_KEY=. GITHUB_OUTPUT="$WORK/out" bash "$RS" renumber-target) >/dev/null 2>&1 || fail "renumber-target failed"
chk "U9 renumber-target checks the newest build out" "$(outkey commit)|$(git -C "$W" rev-parse HEAD)" "$UF|$UF"
chk "U9 where the arithmetic reads the build's own range" "$(dev_compute)" "v1.2.0-dev.1"
chk "U9 and that version is tagged on the build" "$(renumber go v1.2.0-dev.1)|$(grep -c "^api repos/o/app/git/refs -f ref=refs/tags/v1.2.0-dev.1 -f sha=$UF$" "$GH_LOG")" "|1"
printf '{"name":"@o/app"}\n' >"$W/package.json"
out_u=$(renumber ts v1.2.0-dev.2)
chk "U10 a TS root publishes the build to npm under the dev dist-tag" "$out_u|$(grep '^npm ' "$GH_LOG" | tr '\n' ';')" \
  "|npm pkg set version=1.2.0-dev.2;npm publish --access public --tag dev;"
pub=$(grep -n '^npm publish' "$GH_LOG" | cut -d: -f1)
tagged=$(grep -n "git/refs -f ref=refs/tags/v1.2.0-dev.2 -f sha=$UF$" "$GH_LOG" | cut -d: -f1)
chk "U10 then tags the build" "$([ -n "$pub" ] && [ -n "$tagged" ] && [ "$pub" -lt "$tagged" ] && echo after)" "after"
touch "$NPM_DIR/silent"
out_u=$(renumber ts v1.2.0-dev.3)
rm "$NPM_DIR/silent"
chk "U11 an npm version that does not read back is never tagged" "$out_u|$(grep -c 'git/refs' "$GH_LOG" || true)" "EXIT=1|0"
chk_has "U11 and says so" "$(cat "$WORK/renumber.log")" "npm @o/app@1.2.0-dev.3 does not read back, so v1.2.0-dev.3 is not tagged"
NPM_URL=https://registry.npmjs.org/@o/app
echo "$NPM_URL/1.2.0-dev.2 {\"version\":\"1.2.0-dev.2\",\"gitHead\":\"$UF\"}" >"$CURL_DIR/docs"
out_u=$(renumber ts v1.2.0-dev.2)
chk "U12 an npm version already published from the build is read back and tagged, not republished" \
  "$out_u|$(grep -c '^npm publish' "$GH_LOG" || true)|$(grep -c 'git/refs -f ref=refs/tags/v1.2.0-dev.2 ' "$GH_LOG")" "|0|1"
# The same partial publication on npm: the version belongs to the newer UB.
echo "$NPM_URL/1.2.0-dev.5 {\"version\":\"1.2.0-dev.5\",\"gitHead\":\"$UB\"}" >>"$CURL_DIR/docs"
out_u=$(renumber ts v1.2.0-dev.5)
chk "U16 an npm version published from a newer untagged build is refused" \
  "$out_u|$(grep -c '^npm p' "$GH_LOG" || true)|$(grep -c 'git/refs' "$GH_LOG" || true)" "EXIT=1|0|0"
chk_has "U16 naming both commits" "$(cat "$WORK/renumber.log")" \
  "npm @o/app@1.2.0-dev.5 is already published from '$UB', not $UF"
echo "$NPM_URL/1.2.0-dev.6 {\"version\":\"1.2.0-dev.6\"}" >>"$CURL_DIR/docs"
out_u=$(renumber ts v1.2.0-dev.6)
chk "U17 an npm version with no recorded commit is refused" \
  "$out_u|$(grep -c '^npm p' "$GH_LOG" || true)|$(grep -c 'git/refs' "$GH_LOG" || true)" "EXIT=1|0|0"
chk_has "U17 and says so" "$(cat "$WORK/renumber.log")" "is already published from 'an unrecorded commit'"
echo "$NPM_URL/1.2.0-dev.7" >"$CURL_DIR/down"
out_u=$(renumber ts v1.2.0-dev.7)
rm "$CURL_DIR/down" "$CURL_DIR/docs"
chk "U18 an npm registry that cannot answer publishes and tags nothing" \
  "$out_u|$(grep -c '^npm p' "$GH_LOG" || true)|$(grep -c 'git/refs' "$GH_LOG" || true)" "EXIT=1|0|0"
chk_has "U18 and says so" "$(cat "$WORK/renumber.log")" "npm cannot tell whether @o/app@1.2.0-dev.7 exists (HTTP 000)"
# The job that computes the version runs the cliff config and holds no OIDC,
# so a TS root's renumber there hands the version to renumber-npm.
handoff() { # <repo type> <dev version> -> the outputs, or EXIT=n
  : >"$GH_LOG"
  : >"$WORK/out"
  (cd "$W" && CHANNEL=dev LANE_KEY=. DEV_VERSION="$2" REPO_TYPE="$1" GITHUB_OUTPUT="$WORK/out" bash "$RS" renumber-handoff) \
    >"$WORK/renumber.log" 2>&1 || {
    echo "EXIT=$?"
    return 0
  }
  tr '\n' ' ' <"$WORK/out"
}
chk "U19 a TS root hands its renumber on, naming the build" "$(handoff ts v1.2.0-dev.8)" "version=v1.2.0-dev.8 commit=$UF "
chk "U19 publishing and tagging nothing itself" "$(wc -l <"$GH_LOG")" "0"
chk "U19 a build already ranking there hands nothing on" "$(handoff ts v1.1.0-dev.3)" ""
chk "U19 every lane hands its renumber on" "$(handoff go v1.2.0-dev.8)" "version=v1.2.0-dev.8 commit=$UF "
chk "U19 a stable version is refused" "$(handoff ts v1.2.0)" "EXIT=1"
# The tagging job reads what a job that ran the config wrote, as data.
hread() { # <json> -> the outputs, or EXIT=n
  : >"$WORK/out"
  printf '%s' "$1" >"$WORK/handoff.json"
  GITHUB_OUTPUT="$WORK/out" bash "$RS" handoff-read "$WORK/handoff.json" >/dev/null 2>&1 || {
    echo "EXIT=$?"
    return 0
  }
  tr '\n' ' ' <"$WORK/out"
}
chk "U22 a handoff names a version and its build" "$(hread "{\"version\":\"v1.2.0-dev.8\",\"commit\":\"$UF\"}")" \
  "version=v1.2.0-dev.8 commit=$UF "
chk "U22 an empty handoff is nothing to renumber" "$(hread '{"version":"","commit":""}')" "version= commit= "
chk "U22 a version smuggling an output line is refused" \
  "$(hread "$(jq -nc --arg c "$UF" '{version: "v1.2.0-dev.8\ncommit=0000000000000000000000000000000000000000", commit: $c}')")" "EXIT=1"
chk "U22 as are a short commit, a build with no version, and a missing key" \
  "$(hread '{"version":"v1.2.0-dev.8","commit":"abc"}') $(hread "{\"version\":\"\",\"commit\":\"$UF\"}") $(hread '{"version":"v1.2.0-dev.8"}')" \
  "EXIT=1 EXIT=1 EXIT=1"
RUNNER_TEMP="$WORK/rt"
mkdir -p "$RUNNER_TEMP"
: >"$WORK/out"
(cd "$W" && CI_TOOLS="$ROOT/scripts" LANE_KEY=yamlenv VERSION=yamlenv/v1.2.0-dev.1 COMMIT="$UF" GITHUB_OUTPUT="$WORK/out" \
  RUNNER_TEMP="$RUNNER_TEMP" bash "$WORK/renumber-write.sh") >/dev/null 2>&1 || fail "the renumber handoff step failed"
cp "$RUNNER_TEMP/handoff/handoff.json" "$WORK/written.json"
written_name=$(outkey artifact)
: >"$WORK/out"
(GITHUB_OUTPUT="$WORK/out" CI_TOOLS="$ROOT/scripts" RUNNER_TEMP="$RUNNER_TEMP" bash "$WORK/renumber-read.sh") >/dev/null 2>&1 \
  || fail "the renumber-tag read step failed"
chk "U22 what the renumber leg writes, its renumber-tag leg reads back" "$(tr '\n' ' ' <"$WORK/out")" \
  "version=yamlenv/v1.2.0-dev.1 commit=$UF "
name_of() { bash "$RS" handoff-name "$1" "$2"; }
chk "U22 under a name of its lane alone" "$written_name" "$(name_of renumber yamlenv)"
chk "U22 an artifact name, which takes no slash, even for a nested lane or its tag" \
  "$(name_of renumber tools/yamlenv | tr -cd '/' | wc -c)|$(name_of repair-notes tools/yamlenv/v1.0.0 | tr -cd '/' | wc -c)|$(name_of renumber tools/yamlenv | grep -c '^renumber-[0-9a-f]\{16\}$')" \
  "0|0|1"
chk "U22 which two lanes or tags never share" \
  "$([ "$(name_of renumber a-b)" != "$(name_of renumber a/b)" ] && [ "$(name_of repair-notes a-b/v1.0.0)" != "$(name_of repair-notes a/b-v1.0.0)" ] && [ "$(name_of renumber .)" != "$(name_of renumber yamlenv)" ] && echo distinct)" \
  "distinct"
rm -rf "$RUNNER_TEMP"
unset RUNNER_TEMP
step_out() { # <extracted step> <repo type> [env...] -> the outputs, or EXIT=n
  local body=$1 type=$2
  shift 2
  : >"$GH_LOG"
  : >"$WORK/out"
  (cd "$W" && env CI_TOOLS="$ROOT/scripts" CHANNEL=dev LANE_KEY=. DEV_VERSION=v1.2.0-dev.8 REPO_TYPE="$type" IMAGE=o/app \
    GITHUB_OUTPUT="$WORK/out" "$@" bash "$WORK/$body") >"$WORK/renumber.log" 2>&1 || echo "EXIT=$?"
  tr '\n' ' ' <"$WORK/out"
}
chk "U21 renumber-ts hands a TS root's version on and writes nothing" \
  "$(step_out renumber-ts-step.sh ts)|$(wc -l <"$GH_LOG")" "version=v1.2.0-dev.8 commit=$UF |0"
chk "U21 renumber-tag's legs tag the handed-on build" \
  "$(step_out renumber-step.sh go)|$(grep -c "git/refs -f ref=refs/tags/v1.2.0-dev.8 -f sha=$UF$" "$GH_LOG")" "|1"
chk "U21 renumber-npm publishes the handed-on version, then tags" \
  "$(step_out renumber-npm-step.sh ts)|$(grep '^npm p' "$GH_LOG" | tr '\n' ';')|$(grep -c "git/refs -f ref=refs/tags/v1.2.0-dev.8 -f sha=$UF$" "$GH_LOG")" \
  "|npm pkg set version=1.2.0-dev.8;npm publish --access public --tag dev;|1"
target() { # <expected commit> -> ok|refused
  : >"$WORK/out"
  (cd "$W" && LANE_KEY=. EXPECT_COMMIT="$1" GITHUB_OUTPUT="$WORK/out" bash "$RS" renumber-target) >"$WORK/renumber.log" 2>&1 \
    && echo ok || echo refused
}
chk "U20 renumber-npm refuses a build other than the one numbered" "$(target "$D3")|$(outkey commit)" "refused|"
chk_has "U20 naming both" "$(cat "$WORK/renumber.log")" "the newest build is now v1.1.0-dev.3 at $UF, not the $D3 this run numbered"
chk "U20 and selects the numbered one" "$(target "$UF")|$(outkey commit)" "ok|$UF"
rm "$W/package.json"
git -C "$W" checkout -q --detach "$UB"

# ── A promotion's TS subpackages and the dev barrier ─────────────────────────
# A dev run tags its root build, then publishes a changed subpackage: a run
# that failed in between leaves a root tag with no subpackage behind it.
X="$WORK/hybrid-dev"
git init -q -b main "$X"
X0=$(commit_in "$X" "feat: initial" go.mod='module example.com/x' main.go=v1 web/mod.ts=v1 \
  web/jsr.json='{"name":"@o/web","version":"0.0.0"}' web/package.json='{"name":"@o/web","version":"0.0.0"}')
git -C "$X" tag v1.0.0 "$X0"
git -C "$X" checkout -q -b dev
XD=$(commit_in "$X" "fix: root only" main.go=v2)
XW1=$(commit_in "$X" "feat(web): add" web/mod.ts=v2)
git -C "$X" tag v1.1.0-dev.1 "$XW1"
xpromote() { # <T> -> R on X's main
  local r
  r=$(GIT_COMMITTER_DATE=2026-10-01T12:00:00Z git -C "$X" commit-tree "$1^{tree}" -p main -p "$1" -m "release: promote dev into main")
  git -C "$X" update-ref refs/heads/main "$r"
  echo "$r"
}
XR0=$(xpromote "$XD")
XR1=$(xpromote "$XW1")
XC="$WORK/hybrid-dev-clone"
git clone -q "$X" "$XC"
git -C "$XC" checkout -q --detach "$XR1"
xstate() { jq -nc --arg r "$1" '{".": {commit: $r, in_range: true, version: ""}}'; }
NPM_WEB=https://registry.npmjs.org/@o/web
xweb() { # [<version> <gitHead> ...] -> the packument npm serves for @o/web
  local v=() a
  while [ "$#" -gt 1 ]; do
    v+=("$(jq -nc --arg k "$1" --arg h "$2" '{($k): {gitHead: $h}}')")
    shift 2
  done
  a=$(printf '%s\n' "${v[@]}" | jq -sc 'add // {}')
  sed -i "\\|^$NPM_WEB |d" "$CURL_DIR/docs" 2>/dev/null || true
  echo "$NPM_WEB {\"versions\":$a}" >>"$CURL_DIR/docs"
}
xbarrier() { # <R> -> runs the barrier at XR1 in the clone; prints EXIT=n on failure
  : >"$GH_LOG"
  : >"$CURL_DIR/log"
  rm -f "$GH_DIR/runs-calls" "$GH_DIR/runs-queries"
  (cd "$XC" && STATE="$(xstate "$1")" ROOT_VERSION=v1.1.0 WORKFLOW=release.yaml REPO_TYPE=go BARRIER_POLLS=12 \
  RENUMBER_POLLS=4 bash "$RS" barrier) >"$WORK/barrier.log" 2>&1 || echo "EXIT=$?"
}
xhook() { # <conclusion> [<version> <gitHead> ...] -> the renumber dispatch, after which npm serves those @o/web versions
  local conclusion=$1
  shift
  cp "$CURL_DIR/docs" "$GH_DIR/docs-after"
  if [ "$#" -gt 0 ]; then
    cp "$CURL_DIR/docs" "$GH_DIR/docs-before"
    xweb "$@"
    cp "$CURL_DIR/docs" "$GH_DIR/docs-after"
    cp "$GH_DIR/docs-before" "$CURL_DIR/docs"
  fi
  cat >"$GH_DIR/on-dispatch" <<SH
#!/usr/bin/env bash
echo 99 >"$GH_DIR/dispatched-id"
echo '{"status":"completed","conclusion":"$conclusion"}' >"$GH_DIR/run-99.json"
cp "$GH_DIR/docs-after" "$CURL_DIR/docs"
SH
  chmod 755 "$GH_DIR/on-dispatch"
}
cp "$GH_DIR/runs-idle.json" "$GH_DIR/runs-idle.saved"
jq '.workflow_runs[].status = "completed"' "$GH_DIR/runs-idle.saved" >"$GH_DIR/runs-idle.json"
GH_BUSY_CALLS=0
xweb 1.0.0-dev.3 "$X0"
xhook success
out_b=$(xbarrier "$XR1")
chk "B20 a subpackage whose newest dev change has no dev build holds the publish behind a renumber" \
  "$out_b|$(dispatched)" "EXIT=1|1"
chk_has "B20 after waiting for every dev run, as its own run may still publish it" "$(cat "$WORK/barrier.log")" \
  "subpackage web: dev's change $XW1 has no dev build on npm"
chk "B20 every dev run was read, not only those before the cutoff" "$(cat "$GH_DIR/runs-calls")" "4"
chk_has "B20 and a renumber that publishes nothing keeps it held" "$(cat "$WORK/barrier.log")" \
  "subpackage web: renumber run 99 left dev's change $XW1 with no dev build on npm, so the stable publish stays held"
xweb 1.0.0-dev.3 "$X0"
xhook success 1.2.0-dev.1 "$XW1"
out_b=$(xbarrier "$XR1")
chk "B20 the renumber's dev build of the subpackage releases the hold" "$out_b|$(dispatched)" "|1"
rm -f "$GH_DIR/on-dispatch"
xweb 1.1.0-dev.1 "$XW1"
out_b=$(xbarrier "$XR1")
chk "B20 a subpackage dev build of T's own change holds nothing and waits for no later run" \
  "$out_b|$(dispatched)|$(cat "$GH_DIR/runs-calls")" "|0|2"
echo "$NPM_WEB" >"$CURL_DIR/down"
out_b=$(xbarrier "$XR1")
rm "$CURL_DIR/down"
chk "B20 an npm registry that cannot answer holds the publish and dispatches nothing" "$out_b|$(dispatched)" "EXIT=1|0"
chk_has "B20 and says so" "$(cat "$WORK/barrier.log")" "subpackage web: cannot tell whether npm serves its newest dev build"
out_b=$(xbarrier "$XR0")
chk "B20 a root-only promotion reads no subpackage registry" "$(grep -c "^$NPM_WEB" "$CURL_DIR/log" || true)" "0"
# Past T, dev's subpackage must rank above the version, as a root build must.
git -C "$X" checkout -q dev
XW2=$(commit_in "$X" "fix(web): later" web/mod.ts=v3)
git -C "$X" tag v1.1.0-dev.2 "$XW2"
git -C "$X" tag v1.2.0-dev.1 "$XW2"
git -C "$X" checkout -q main
xweb 1.1.0-dev.1 "$XW1"
xhook success
out_b=$(xbarrier "$XR1")
chk "B21 a dev build of an older change does not cover the newer change past T" "$out_b|$(dispatched)" "EXIT=1|1"
chk_has "B21 naming the newer change" "$(cat "$WORK/barrier.log")" "subpackage web: dev's change $XW2 has no dev build on npm"
xweb 1.1.0-dev.2 "$XW2"
xhook success
out_b=$(xbarrier "$XR1")
chk "B21 a subpackage build past T below the version holds the publish behind a renumber, the root already above before and after the dev-run wait" \
  "$out_b|$(dispatched)|$(grep -c 'lane .: dev already ranks above v1.1.0' "$WORK/barrier.log")" "EXIT=1|1|2"
chk_has "B21 and names the rank" "$(cat "$WORK/barrier.log")" \
  "subpackage web: renumber run 99 left dev's change $XW2 with no dev build on npm above the version"
xweb 1.1.0-dev.2 "$XW2"
xhook success 1.2.0-dev.1 "$XW2"
out_b=$(xbarrier "$XR1")
chk "B21 the renumbered subpackage above the version releases the hold" "$out_b|$(dispatched)" "|1"
# Two promotions past the root's stable tag, the first's run failed: R1
# changed only web, R2 only the root, and dev's root already ranks above.
Y="$WORK/hybrid-two"
git init -q -b main "$Y"
Y0=$(commit_in "$Y" "feat: initial" go.mod='module example.com/y' main.go=v1 web/mod.ts=v1 \
  web/jsr.json='{"name":"@o/web","version":"0.0.0"}' web/package.json='{"name":"@o/web","version":"0.0.0"}')
git -C "$Y" tag v1.0.0 "$Y0"
git -C "$Y" checkout -q -b dev
YW=$(commit_in "$Y" "feat(web): add" web/mod.ts=v2)
YD=$(commit_in "$Y" "fix: root only" main.go=v2)
git -C "$Y" tag v1.2.0-dev.1 "$YD"
YR1=$(GIT_COMMITTER_DATE=2026-10-01T12:00:00Z git -C "$Y" commit-tree "$YW^{tree}" -p main -p "$YW" -m "release: promote dev into main")
YR2=$(GIT_COMMITTER_DATE=2026-10-01T12:00:00Z git -C "$Y" commit-tree "$YD^{tree}" -p "$YR1" -p "$YD" -m "release: promote dev into main")
git -C "$Y" update-ref refs/heads/main "$YR2"
YC="$WORK/hybrid-two-clone"
git clone -q "$Y" "$YC"
git -C "$YC" checkout -q --detach "$YR2"
ybarrier() { # -> runs the barrier at YR2 with the root's stable tag at Y0; prints EXIT=n on failure
  : >"$GH_LOG"
  : >"$CURL_DIR/log"
  rm -f "$GH_DIR/runs-calls" "$GH_DIR/runs-queries"
  (cd "$YC" && STATE="$(jq -nc --arg r "$YR2" --arg h "$Y0" '{".": {commit: $r, h_commit: $h, in_range: true, version: ""}}')" \
  ROOT_VERSION=v1.1.0 WORKFLOW=release.yaml REPO_TYPE=go BARRIER_POLLS=12 RENUMBER_POLLS=4 bash "$RS" barrier) >"$WORK/barrier.log" 2>&1 \
    || echo "EXIT=$?"
}
chk "B25 the fixture's newest promotion changes the root alone" \
  "$(git -C "$Y" diff --name-only "$YR2^1" "$YR2")|$(git -C "$Y" diff --name-only "$YR1^1" "$YR1")" "main.go|web/mod.ts"
xweb
xhook success
out_b=$(ybarrier)
chk "B25 an earlier promotion's subpackage with no dev build holds the publish behind a renumber" "$out_b|$(dispatched)" "EXIT=1|1"
chk_has "B25 naming that subpackage's change" "$(cat "$WORK/barrier.log")" \
  "subpackage web: renumber run 99 left dev's change $YW with no dev build on npm, so the stable publish stays held"
chk "B25 having read its registry" "$([ "$(grep -c "^$NPM_WEB" "$CURL_DIR/log" || true)" -gt 0 ] && echo read || echo unread)" "read"
rm -f "$GH_DIR/on-dispatch"
xweb 1.1.0-dev.1 "$YW"
out_b=$(ybarrier)
chk "B25 its dev build releases the hold with no renumber" "$out_b|$(dispatched)" "|0"
rm -f "$GH_DIR/on-dispatch"
mv "$GH_DIR/runs-idle.saved" "$GH_DIR/runs-idle.json"
# The renumber run publishes the subpackages the barrier would hold, from the
# root's renumbered build.
git -C "$XC" fetch -q --tags origin '+refs/heads/dev:refs/remotes/origin/dev'
rsub() { # <checkout> -> the outputs, or EXIT=n
  : >"$WORK/out"
  (cd "$XC" && git checkout -q --detach "$1" && CHANNEL=dev REPO_TYPE=go STATE="$(jq -nc --arg r "$XR1" '{".": {commit: $r, version: "v1.1.0"}}')" \
  GITHUB_OUTPUT="$WORK/out" bash "$RS" renumber-subpackages) >"$WORK/rsub.log" 2>&1 || {
    echo "EXIT=$?"
    return 0
  }
  tr '\n' ' ' <"$WORK/out"
}
xweb 1.1.0-dev.2 "$XW2"
chk "U23 a subpackage below the pending version is published from the build under its newest dev tag" \
  "$(rsub "$XW2")" 'version=v1.2.0-dev.1 dirs=["web"] '
xweb 1.1.0-dev.2 "$XW2" 1.2.0-dev.1 "$XW2"
chk "U23 one already above it is left alone" "$(rsub "$XW2")" 'version=v1.2.0-dev.1 dirs=[] '
xweb
chk "U23 as is a missing one, published from the build carrying its change" "$(rsub "$XW2")" 'version=v1.2.0-dev.1 dirs=["web"] '
chk "U23 a checkout that is not a dev build is refused" "$(rsub "$XD")" "EXIT=1"
git -C "$X" checkout -q dev
XW3=$(commit_in "$X" "feat(web): newest, unbuilt" web/mod.ts=v4)
git -C "$X" checkout -q main
git -C "$XC" fetch -q --tags origin '+refs/heads/dev:refs/remotes/origin/dev'
chk "U23 a change newer than the build is left to its own dev run" "$(rsub "$XW2")" 'version=v1.2.0-dev.1 dirs=[] '
chk_has "U23 and says so" "$(cat "$WORK/rsub.log")" "subpackage web: dev's change $XW3 is newer than the build v1.2.0-dev.1"
git -C "$X" checkout -q dev
commit_in "$X" "test(web): cover the newest" web/mod.test.ts=t >/dev/null
commit_in "$X" "fix: root after" main.go=v3 >/dev/null
git -C "$X" checkout -q main
git -C "$XC" fetch -q --tags origin '+refs/heads/dev:refs/remotes/origin/dev'
rsub "$XW2" >/dev/null
chk_has "U23 a later test-only or root-only commit is not the subpackage's change" "$(cat "$WORK/rsub.log")" \
  "subpackage web: dev's change $XW3 is newer than the build"
XCURL="$WORK/xcurl"
mkdir -p "$XCURL"
: >"$XCURL/ok"
: >"$SP_LOG"
git -C "$XC" checkout -q --detach "$XW2"
(cd "$XC" && env -u JSR_VERSION PATH="$BIN2:$PATH" CURL_DIR="$XCURL" CI_TOOLS="$ROOT/scripts" VERSION=v1.2.0-dev.1 CHANNEL=dev \
  DIRS='["web"]' bash "$WORK/rsub-publish.sh") >"$WORK/rsubpub.log" 2>&1 || fail "the renumber subpackage publish failed: $(cat "$WORK/rsubpub.log")"
chk "U23 the publish step puts each subpackage on npm's dev dist-tag from the build, with no JSR pin" \
  "$(grep -E '^(npm publish|published|npx)' "$SP_LOG" | tr '\n' ';')|$(cat "$SP_LOG.version")" \
  "npm publish --access public --tag dev @web;published v3;|1.2.0-dev.1"
# A subpackage whose only change is the repository's first commit: its change
# is that root commit, diffed against the empty tree.
Z="$WORK/hybrid-root"
git init -q -b main "$Z"
Z0=$(commit_in "$Z" "feat: initial" go.mod='module example.com/z' main.go=v1 web/mod.ts=v1 \
  web/jsr.json='{"name":"@o/web","version":"0.0.0"}' web/package.json='{"name":"@o/web","version":"0.0.0"}')
git -C "$Z" tag v1.0.0 "$Z0"
git -C "$Z" checkout -q -b dev
ZD=$(commit_in "$Z" "fix: root only" main.go=v2)
git -C "$Z" tag v1.1.0-dev.1 "$ZD"
ZR=$(GIT_COMMITTER_DATE=2026-10-01T12:00:00Z git -C "$Z" commit-tree "$ZD^{tree}" -p main -p "$ZD" -m "release: promote dev into main")
git -C "$Z" update-ref refs/heads/main "$ZR"
ZC="$WORK/hybrid-root-clone"
git clone -q "$Z" "$ZC"
git -C "$ZC" fetch -q --tags origin '+refs/heads/dev:refs/remotes/origin/dev'
chk "U24 the fixture's subpackage changed in the root commit alone" \
  "$(git -C "$Z" rev-list --first-parent dev -- web | tr '\n' ' ')|$(git -C "$Z" rev-list --max-parents=0 dev)" "$Z0 |$Z0"
zsub() { # -> renumber-subpackages' outputs at ZD, or EXIT=n
  : >"$WORK/out"
  (cd "$ZC" && git checkout -q --detach "$ZD" && CHANNEL=dev REPO_TYPE=go STATE="$(jq -nc --arg r "$ZR" '{".": {commit: $r, version: "v1.1.0"}}')" \
  GITHUB_OUTPUT="$WORK/out" bash "$RS" renumber-subpackages) >"$WORK/zsub.log" 2>&1 || {
    echo "EXIT=$?"
    return 0
  }
  tr '\n' ' ' <"$WORK/out"
}
xweb
chk "U24 with no dev build on npm it is published from the root build" "$(zsub)" 'version=v1.1.0-dev.1 dirs=["web"] '
chk_has "U24 naming the root commit as its change" "$(cat "$WORK/zsub.log")" \
  "subpackage web: publishing the build v1.1.0-dev.1 for dev's change $Z0"
xweb 1.0.0-dev.1 "$Z0"
chk "U24 a dev build of that root commit leaves it alone" "$(zsub)" 'version=v1.1.0-dev.1 dirs=[] '
rm -f "$CURL_DIR/docs"

# ── Wiring ───────────────────────────────────────────────────────────────────
TB="steps.channel.outputs.release_model == 'two-branch'"
chk_has "W1 pending promotions are read under two-branch only" "$(fact '."if:Find pending promotions"')" "$TB"
chk "W1 numbering runs only when something is pending" \
  "$(fact '."if:Install git-cliff for promotion numbering"')|$(fact '."if:Number pending promotions"')" \
  "\${{ steps.pending.outputs.any == 'true' }}|\${{ steps.pending.outputs.any == 'true' }}"
chk_has "W1 receipts are read under two-branch stable only" "$(fact '."if:Read release receipts"')" "$TB && steps.channel.outputs.channel == 'stable'"
chk_has "W2 legacy keeps the finalize repair" "$(fact '."if:Detect finalize state"')" "steps.channel.outputs.release_model == 'legacy'"
chk "W3 the compute action reads the model" "$(fact '."cliff-with"."release-model"')" '${{ steps.channel.outputs.release_model }}'
chk "W3 and the root's pending range" "$(fact '."cliff-with"."pending-in-range"')" "\${{ steps.pending.outputs.root_in_range || 'false' }}"
chk "W3 and the root's pending version on dev" "$(fact '."cliff-with"."pending-version"')" \
  "\${{ steps.channel.outputs.channel == 'dev' && steps.number.outputs.root_version || '' }}"
chk "W3 pending promotions are numbered before the version" \
  "$(fact '.names | (index("Number pending promotions") < index("Compute version (cliff)"))')" "true"
chk "W4 the lane compute reads its pending range" "$(fact '."lanecliff-with"."pending-in-range"')" \
  "\${{ fromJSON(needs.detect.outputs.lane_state || '{}')[matrix.dir].in_range && 'true' || 'false' }}"
chk "W4 the lane select reads the model" "$(fact '."lane-select-env".RELEASE_MODEL')" '${{ needs.detect.outputs.release_model }}'
lane_finalize() { # <model> -> finalize|gh calls of a stable lane run at a tagged HEAD
  : >"$WORK/out"
  : >"$GH_LOG"
  RELEASE_MODEL="$1" DIR=yamlenv CHANNEL=stable BASE=yamlenv/v9.9.9 DEV_VERSION=yamlenv/v9.9.9-dev.1 FLOOR_BASE="" \
    FLOOR_DEV_VERSION="" LATEST=yamlenv/v9.9.9 ANCHOR_SHA=head GITHUB_SHA=head GITHUB_OUTPUT="$WORK/out" \
    bash "$WORK/lane-select.sh" >/dev/null 2>&1 || echo "EXIT=$?"
  printf '%s|%s' "$(outkey finalize)" "$(wc -l <"$GH_LOG")"
}
chk "W4 a legacy lane run at a tagged HEAD repairs by finalize" "$(lane_finalize legacy)" "true|1"
chk "W4 a two-branch lane run leaves it to the repair job" "$(lane_finalize two-branch)" "false|0"
chk_has "W5 the barrier runs on two-branch stable runs with a promotion in range" "$(fact '."jobif:barrier"')" \
  "needs.detect.outputs.release_model == 'two-branch' && needs.detect.outputs.channel == 'stable' && needs.detect.outputs.in_range_any == 'true'"
chk "W5 and may dispatch" "$(fact '."perms:barrier".actions')" "write"
chk_has "W6 renumber runs in renumber mode only" "$(fact '."jobif:renumber"')" "needs.detect.outputs.mode == 'renumber'"
chk_has "W6 repair runs only on repairs owed" "$(fact '."jobif:repair"')" "needs.detect.outputs.repairs != '[]'"
chk_has "W6 as does its notes job" "$(fact '."jobif:repair-notes"')" "needs.detect.outputs.repairs != '[]'"
GATE="!cancelled() && needs.detect.result == 'success' && (needs.repair.result == 'success' || needs.repair.result == 'skipped') && (needs.barrier.result == 'success' || needs.barrier.result == 'skipped')"
for job in docker go ts go-nested; do
  chk "W7 $job waits for the repair and the barrier" "$(fact ".\"needs:$job\" | join(\" \")")" "detect repair barrier"
  chk_has "W7 $job runs when both were skipped" "$(fact ".\"jobif:$job\"")" "$GATE"
done
chk_has "W7 go-nested publishes nothing in renumber mode" "$(fact '."jobif:go-nested"')" "needs.detect.outputs.mode != 'renumber'"
chk "W8 receipts are recorded under two-branch stable only, once verify-publish succeeded" "$(fact '."jobif:receipts"')" \
  "always() && needs.verify-publish.result == 'success' && needs.detect.outputs.release_model == 'two-branch' && needs.detect.outputs.channel == 'stable'"
chk "W8 in a job after the readback, its steps unconditional" \
  "$(fact '."needs:receipts" | join(" ")')|$(fact '."receipts-step-ifs" | join("")')" "detect docker go ts subpackage go-nested verify-publish|"
chk "W8 that job alone holds the statuses scope" "$(fact '."perms:receipts" | tojson')" '{"contents":"read","statuses":"write"}'
chk "W8 verify-publish, which legacy runs reach, keeps a read-only token" "$(fact '."perms:verify-publish" | tojson')" '{"contents":"read"}'
chk "W8 and records no receipt itself" "$(fact '."vp-steps" | join(",")')" "Checkout,Check out the ci source,Verify published artifacts"
chk "W13 a TS root's renumber publishes in its own job, with OIDC, once a version is handed on" \
  "$(fact '."jobif:renumber-npm"')|$(fact '."needs:renumber-npm" | join(" ")')|$(fact '."perms:renumber-npm"."id-token"')" \
  "\${{ needs.renumber-ts.outputs.npm_version != '' }}|detect renumber-ts|write"
chk "W13 from the version and build renumber-ts handed on" \
  "$(fact '."renumber-outputs" | "\(.npm_version) \(.npm_commit)"')|$(fact '."renumber-npm-env".DEV_VERSION')|$(fact '."renumber-npm-target-env".EXPECT_COMMIT')" \
  "\${{ steps.handoff.outputs.version }} \${{ steps.handoff.outputs.commit }}|\${{ needs.renumber-ts.outputs.npm_version }}|\${{ needs.renumber-ts.outputs.npm_commit }}"
chk "W13 renumber-ts is one job for the TS root in renumber mode, computing then handing on" \
  "$(fact '."jobif:renumber-ts"')|$(fact '."renumber-ts-steps" | join(",")')" \
  "\${{ needs.detect.outputs.mode == 'renumber' && needs.detect.outputs.type == 'ts' }}|Checkout,Check out the ci source,Check out the newest build,Compute version (cliff),Hand the version on"
chk "W13 and the renumber matrix skips the TS root and exports nothing" \
  "$(fact '."renumber-if"."Check out the newest build"')|$(fact '."renumber-matrix-outputs"')" \
  "\${{ matrix.key != '.' || needs.detect.outputs.type != 'ts' }}|false"
chk "W20 no job reads an output off a matrix job" "$(fact '."matrix-output-reads" | join(" ")')" ""
chk "W23 a renumber run publishes the subpackages after the root renumber, with OIDC and no other write scope" \
  "$(fact '."needs:renumber-subpackages" | join(" ")')|$(fact '."perms:renumber-subpackages" | tojson')" \
  'detect renumber-tag renumber-npm|{"contents":"read","id-token":"write"}'
chk "W23 once the root renumber succeeded or had nothing to do, in renumber mode, in a repo with subpackages" \
  "$(fact '."rsub-gate" | [.[]] | map(tostring) | join(" ")')" "true true false false false false"
chk "W23 checking the newest build out, selecting, then publishing through the shared script" \
  "$(fact '."rsub-steps" | join(",")')|$(grep -c 'publish-subpackage.sh' "$WORK/rsub-publish.sh")" \
  "Checkout,Check out the ci source,Check out the newest build,Select the subpackages,Setup Node.js,Publish the subpackages|1"
chk "W23 selecting only at a build, publishing only what was selected, on the dev channel" \
  "$(fact '."rsub-if"."Select the subpackages"')|$(fact '."rsub-if"."Publish the subpackages"')|$(fact '."rsub-env"."Publish the subpackages" | "\(.CHANNEL) \(.VERSION) \(.DIRS)"')" \
  "\${{ steps.target.outputs.commit != '' }}|\${{ steps.plan.outputs.dirs != '' && steps.plan.outputs.dirs != '[]' }}|dev \${{ steps.plan.outputs.version }} \${{ steps.plan.outputs.dirs }}"
chk "W13 checking that build out and installing before it tags" \
  "$(fact '."renumber-npm-steps" | join(",")')" "Checkout,Check out the ci source,Move the ci source out of the package,Check out the numbered build,Setup Node.js,Install dependencies,Renumber"
chk "W14 an image repair installs the pinned cosign" "$(fact '."repair-if"."Install Cosign"')|$(fact '."repair-cosign"')" \
  "\${{ matrix.site == 'docker' }}|$(sed -n 's/^  COSIGN_VERSION: //p' "$ROOT/.github/workflows/docker-release.yaml")"
chk "W14 and reads back every registry the run wrote" "$(fact '."repair-env".REGISTRIES')" '${{ needs.detect.outputs.registries }}'
chk "W14 and asks about the ancestors whose image a tag re-tagged, as the digest walk judged them" \
  "$(fact '."repair-env".EXCLUDE_RE')|$(fact '."repair-env".SUBPACKAGES_JSON')" \
  '${{ needs.detect.outputs.exclude_re }}|${{ needs.detect.outputs.subpackages }}'
chk "W15 the barrier knows which registries a dev build writes" "$(fact '."barrier-env".REPO_TYPE')" '${{ needs.detect.outputs.type }}'
# The step itself, with release-state.sh stubbed: it logs the caller's workflow ref and
# dispatches that file by name, and refuses a ref naming another repository's workflow.
mkdir -p "$WORK/barrier-tools"
printf '%s\n' 'echo "barrier WORKFLOW=$WORKFLOW"' >"$WORK/barrier-tools/release-state.sh"
hold() { (GITHUB_REPOSITORY=o/app GITHUB_WORKFLOW_REF="$1" CI_TOOLS="$WORK/barrier-tools" bash "$WORK/barrier-step.sh") 2>&1 || echo "EXIT=$?"; }
chk "W15 the barrier logs the caller's workflow ref and dispatches the caller's file" \
  "$(hold o/app/.github/workflows/release.yaml@refs/heads/main)" \
  "caller workflow: o/app/.github/workflows/release.yaml@refs/heads/main
barrier WORKFLOW=release.yaml"
chk "W15 and refuses a ref naming the called workflow's own repository" \
  "$(hold cplieger/ci/.github/workflows/release.yaml@refs/tags/v3)" \
  "caller workflow: cplieger/ci/.github/workflows/release.yaml@refs/tags/v3
::error::GITHUB_WORKFLOW_REF names cplieger/ci/.github/workflows/release.yaml@refs/tags/v3, no workflow of o/app, so the barrier has nothing to dispatch
EXIT=1"
chk "W16 a repair owing subpackages completes them in repair-publish, one leg per such repair" \
  "$(fact '."repair-publish-matrix"')|$(fact '."repair-publish-steps" | join(",")')" \
  "\${{ fromJSON(needs.detect.outputs.repair_publishes) }}|Checkout,Check out the ci source,Setup Node.js,Complete the subpackages"
chk_has "W16 which runs only when one is owed" "$(fact '."jobif:repair-publish"')" "needs.detect.outputs.repair_publishes != '[]'"
chk "W16 detect exports that list" "$(fact '.outputs.repair_publishes')" '${{ steps.receipts.outputs.repair_publishes }}'
chk "W16 after the Release and before the readback, which reads them too" \
  "$(fact '."needs:repair-publish" | join(" ")')|$(fact '."needs:repair" | join(" ")')|$(fact '."repair-env".SUBPACKAGES')" \
  "detect repair-release|detect repair-notes repair-release repair-publish|\${{ matrix.subpackages || '[]' }}"
chk "W16 with OIDC for npm and JSR" "$(fact '."perms:repair-publish" | tojson')" '{"contents":"read","id-token":"write"}'
chk "W16 the subpackage job publishes through the shared script" \
  "$(fact '."subpkg-steps" | join(",")')|$(grep -c 'publish-subpackage.sh' "$WORK/subpkg-new.sh")" \
  "Checkout,Setup Node.js,Check out the ci source,Publish subpackage to npm + JSR|1"
chk "W16 every jsr CLI pin is one Renovate-tracked version" "$(fact '."jsr-pins" | join(",")')|$(fact '."jsr-pin-sites"')" "0.14.3|3"
chk "W17 docker-release leaves the receipt to release.yaml when subpackages share the version" \
  "$(fact '."docker-with"."defer-receipt"')|$(fact '."dr-defer".default')" "\${{ needs.detect.outputs.subpackages_to_publish != '[]' }}|false"
chk_has "W17 and its receipt job honours it" "$(fact '."dr-receipt-if"')" "&& !inputs.defer-receipt"
chk "W18 no job this pipeline adds runs the cliff config while holding OIDC" \
  "$(fact '."oidc-config" | join(" ")')" "$(fact '."oidc-config-head" | join(" ")')"
chk "W18 those two are main-default's own ts and finalize jobs" "$(fact '."oidc-config-head" | join(" ")')" \
  "docker-release.yaml:finalize release.yaml:ts"
chk "W18 the jobs that render, compute or read back hold no OIDC" \
  "$(fact '."perms:repair-notes" | tojson')|$(fact '."perms:repair" | tojson')|$(fact '."perms:renumber" | has("id-token")')|$(fact '."perms:renumber-ts" | tojson')" \
  '{"contents":"read"}|{"contents":"read","statuses":"write"}|false|{"contents":"read"}'
chk "W18 nor does any job this pipeline adds hold a write scope a poisoned GITHUB_PATH could reach after the config" \
  "$(fact '."write-config" | join(" ")')" "$(fact '."write-config-head" | join(" ")')"
chk "W18 those are main-default's own publishing jobs" "$(fact '."write-config-head" | join(" ")')" \
  "docker-release.yaml:finalize release.yaml:go release.yaml:go-nested release.yaml:ts"
chk "W18 the repair renders to a file it hands on as an artifact of its own tag" \
  "$(fact '."repair-render-out"')|$(fact '."repair-upload".path')|$(fact '."repair-upload".name')|$(fact '."repair-fetch".name')" \
  'true|${{ runner.temp }}/notes/NOTES.md|${{ steps.release.outputs.artifact }}|${{ steps.release.outputs.artifact }}'
chk "W18 and the Release is written by a job that checks out and runs nothing of the repository" \
  "$(fact '."repair-release-steps" | join(",")')|$(fact '."perms:repair-release" | tojson')|$(fact '."needs:repair-release" | join(" ")')" \
  'Check out the ci source,Check the Release,Fetch the notes,Fetch the assets,Publish the missing Release|{"contents":"write"}|detect repair-notes repair-assets'
chk_has "W18 from that file" "$(fact '."repair-publish-run"')" '--notes-file "$RUNNER_TEMP/notes/NOTES.md"'
release_check() { # <job> -> its missing output for v9.9.9, or EXIT=n
  local rt
  rt=$(mktemp -d "$WORK/rt.XXXXXX")
  : >"$WORK/out"
  (RUNNER_TEMP="$rt" CI_TOOLS="$ROOT/scripts" TAG=v9.9.9 GITHUB_OUTPUT="$WORK/out" bash "$WORK/$1-check.sh") \
    >"$WORK/check.log" 2>&1 || echo "EXIT=$?"
  outkey missing
}
for job in repair-notes repair-release; do
  touch "$GH_DIR/release-v9.9.9"
  chk "W22 $job reads a present Release as present" "$(release_check "$job")" "false"
  rm "$GH_DIR/release-v9.9.9"
  chk "W22 $job reads only a 404 as missing" "$(release_check "$job")" "true"
  touch "$GH_DIR/fail-release"
  chk "W22 $job fails on any other read error" "$(release_check "$job")" "EXIT=1"
  chk_has "W22 naming the Release read" "$(cat "$WORK/check.log")" "could not determine whether Release v9.9.9 exists"
  rm "$GH_DIR/fail-release"
done
rex() { bash "$ROOT/scripts/release-exists.sh" v9.9.9 2>"$WORK/rex.err" || echo "EXIT=$?"; }
touch "$GH_DIR/release-v9.9.9"
chk "W22 release-exists.sh reads a published Release as present" "$(rex)" "present"
rm "$GH_DIR/release-v9.9.9"
chk "W22 and only a 404 as absent" "$(rex)" "absent"
touch "$GH_DIR/fail-release"
chk "W22 any other failure is no answer" "$(rex)" "EXIT=1"
chk_has "W22 said on stderr with the error" "$(cat "$WORK/rex.err")" \
  "::error::could not determine whether Release v9.9.9 exists:
gh: HTTP 502"
rm "$GH_DIR/fail-release"
chk "W21 every job that writes a registry, a tag or a Release on a stable run is simulated" \
  "$(fact '."stable-writers" | join(" ")')" "docker go ts subpackage go-nested"
for t in docker go ts; do
  chk "W21 a $t repo publishes once both gates passed or were skipped" \
    "$(fact ".\"gate-sim\".\"$t repair=success barrier=success\"")|$(fact ".\"gate-sim\".\"$t repair=skipped barrier=skipped\"")" \
    "$t subpackage go-nested|$t subpackage go-nested"
  for g in "repair=failure barrier=skipped" "repair=skipped barrier=failure" "repair=cancelled barrier=skipped"; do
    chk "W21 a $t repo writes nothing when $g" "$(fact ".\"gate-sim\".\"$t $g\"")" ""
  done
done
chk "W21 the subpackage job reads both gates itself" "$(fact '."needs:subpackage" | join(" ")')" "detect repair barrier docker ts go"
chk "W21 legacy runs, which skip both gates, decide subpackages as HEAD did" \
  "$(fact '."subpkg-legacy-mismatch" | join(" ")')|$(fact '."subpkg-legacy-cases"')" "|512"
gate() { # <repair-notes result> <repair-release result> <repair-publish result> -> ok|refused
  NOTES=$1 RELEASE=$2 PUBLISH=$3 bash "$WORK/repair-gate.sh" >/dev/null 2>&1 && echo ok || echo refused
}
chk "W19 the repair records a receipt only after the notes, the Release and any subpackages" \
  "$(gate success success success) $(gate success success skipped) $(gate failure success skipped) $(gate success failure skipped) $(gate success skipped skipped) $(gate success success failure) $(gate skipped skipped skipped) $(gate success success cancelled)" \
  "ok ok refused refused refused refused refused refused"
chk "W19 reading the three results" "$(fact '."repair-gate-env" | "\(.NOTES) \(.RELEASE) \(.PUBLISH)"')" \
  '${{ needs.repair-notes.result }} ${{ needs.repair-release.result }} ${{ needs.repair-publish.result }}'
chk_has "W19 and runs whenever repairs are owed, so a failure there fails it" "$(fact '."jobif:repair"')" \
  "!cancelled() && needs.detect.outputs.repairs != '' && needs.detect.outputs.repairs != '[]'"
# ── Two-branch: an image repair publishes its Release with its assets ───────
chk "W24 detect exports the image repairs" "$(fact '.outputs.repair_images')" '${{ steps.receipts.outputs.repair_images }}'
chk "W24 repair-assets runs once per image repair, after detect alone" \
  "$(fact '."jobif:repair-assets"')|$(fact '."needs:repair-assets"')|$(fact '."repair-assets-call".matrix.include')" \
  "\${{ needs.detect.outputs.repair_images != '' && needs.detect.outputs.repair_images != '[]' }}|detect|\${{ fromJSON(needs.detect.outputs.repair_images) }}"
chk "W24 by calling docker-release.yaml, whose identity signs release assets, in its repair mode" \
  "$(fact '."repair-assets-call" | "\(.uses) \(.channel) \(."release-model") \(."repair-tag")"')" \
  './.github/workflows/docker-release.yaml stable two-branch ${{ matrix.tag }}'
chk "W24 with the window inputs the tag's signing commits are read from" \
  "$(fact '."repair-assets-call" | "\(."exclude-re") \(.subpackages)"')" \
  '${{ needs.detect.outputs.exclude_re }} ${{ needs.detect.outputs.subpackages }}'
chk "W24 granting what docker-release.yaml's jobs declare, as the docker job does" \
  "$(fact '."perms:repair-assets" | tojson')" "$(fact '."perms:docker" | tojson')"
chk "W24 repair-release waits for the notes and accepts no image assets only when none were owed" \
  "$(fact '."jobif:repair-release"')" \
  "!cancelled() && needs.repair-notes.result == 'success' && (needs.repair-assets.result == 'success' || needs.repair-assets.result == 'skipped') && needs.detect.outputs.repairs != '' && needs.detect.outputs.repairs != '[]'"
chk "W24 and fetches the image's assets by their own tag's name" \
  "$(fact '."repair-fetch-assets" | "\(.if)|\(.name)|\(.path)"')" \
  "\${{ steps.release.outputs.missing == 'true' && matrix.site == 'docker' }}|\${{ steps.release.outputs.assets }}|\${{ runner.temp }}/assets"
chk "W24 publishing only a missing Release, per site" \
  "$(fact '."repair-release-publish" | "\(.if)|\(.SITE)|\(.TAG)"')" \
  "\${{ steps.release.outputs.missing == 'true' }}|\${{ matrix.site }}|\${{ matrix.tag }}"
rm -f "$GH_DIR/release-v9.9.9" "$GH_DIR/fail-release"
: >"$WORK/out"
(RUNNER_TEMP="$WORK" CI_TOOLS="$ROOT/scripts" TAG=v9.9.9 GITHUB_OUTPUT="$WORK/out" bash "$WORK/repair-release-check.sh") >/dev/null 2>&1
chk "W24 the check names the assets artifact of the same tag repair-assets uploads" \
  "$(outkey assets)" "$(bash "$RS" handoff-name repair-assets v9.9.9)"
PBIN="$WORK/pbin" PUB_DIR="$WORK/pub"
mkdir -p "$PBIN"
cat >"$PBIN/gh" <<'SH'
#!/usr/bin/env bash
# Refuses everything but the calls the publish step makes; a create records
# its asset arguments, which the by-tag read serves back minus PUB_DROP.
printf '%s\n' "$*" >>"$PUB_DIR/log"
case "$*" in
  "api --paginate repos/o/app/releases?per_page=100")
    cat "$PUB_DIR/releases.json"
    if [ -f "$PUB_DIR/releases.json.p2" ]; then cat "$PUB_DIR/releases.json.p2"; fi
    exit 0 ;;
  "api -X DELETE repos/o/app/releases/"*) exit 0 ;;
  "api repos/o/app/releases/tags/v9.9.9 --jq .assets[].name")
    grep -vxF "${PUB_DROP:-}" "$PUB_DIR/served" || true
    exit 0 ;;
esac
if [ "$1 $2" = "release create" ]; then
  shift 3
  : >"$PUB_DIR/served"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -R | --title | --notes-file) shift 2 ;;
      --verify-tag) shift ;;
      *) printf '%s\n' "$1" >>"$PUB_DIR/served"; shift ;;
    esac
  done
  exit 0
fi
echo "stub: unexpected gh $*" >&2
exit 22
SH
chmod 755 "$PBIN/gh"
publish_release() { # <site> [asset file ...] -> rc|the gh calls, one per line joined by ';'
  local rt rc=0 f
  rm -rf "$PUB_DIR"
  rt="$PUB_DIR/rt"
  mkdir -p "$rt/notes" "$rt/assets"
  echo notes >"$rt/notes/NOTES.md"
  echo '[]' >"$PUB_DIR/releases.json"
  if [ -f "$WORK/releases.page1" ]; then cp "$WORK/releases.page1" "$PUB_DIR/releases.json"; fi
  if [ -f "$WORK/releases.page2" ]; then cp "$WORK/releases.page2" "$PUB_DIR/releases.json.p2"; fi
  for f in "${@:2}"; do
    case "$f" in
      empty:*) : >"$rt/assets/${f#empty:}" ;;
      *) echo "bytes of $f" >"$rt/assets/$f" ;;
    esac
  done
  (cd "$WORK" && PATH="$PBIN:$PATH" PUB_DIR="$PUB_DIR" RUNNER_TEMP="$rt" GITHUB_REPOSITORY=o/app GH_TOKEN=t TAG=v9.9.9 SITE="$1" \
    bash "$WORK/repair-release-publish.sh") >"$WORK/publish.log" 2>&1 || rc=$?
  echo "$rc|$(sed "s#$rt#RT#g" "$PUB_DIR/log" 2>/dev/null | paste -sd ';' -)"
}
SBOM_PAIR="sbom.spdx.json sbom.spdx.json.sigstore.json"
DASH="grafana-dashboard.json grafana-dashboard.json.sha256 grafana-dashboard.json.sigstore.json"
LIST="api --paginate repos/o/app/releases?per_page=100"
CREATE="release create v9.9.9 -R o/app --verify-tag --title v9.9.9 --notes-file RT/notes/NOTES.md"
READ="api repos/o/app/releases/tags/v9.9.9 --jq .assets[].name"
# shellcheck disable=SC2086 # the asset lists are word lists
{
  chk "W25 an image Release is created once, with its SBOM and the SBOM's signature, then read back" \
    "$(publish_release docker $SBOM_PAIR)" "0|$LIST;$CREATE $SBOM_PAIR;$READ"
  chk "W25 and the dashboard, its checksum and its signature when the tag ships one" \
    "$(publish_release docker $SBOM_PAIR $DASH)" "0|$LIST;$CREATE $SBOM_PAIR $DASH;$READ"
  chk "W25 a missing SBOM signature publishes nothing" "$(publish_release docker sbom.spdx.json)" "1|"
  chk_has "W25 naming it" "$(cat "$WORK/publish.log")" "::error::sbom.spdx.json.sigstore.json is missing. v9.9.9 is not published without it."
  chk "W25 nor does a missing SBOM" "$(publish_release docker sbom.spdx.json.sigstore.json)" "1|"
  chk_has "W25 naming it" "$(cat "$WORK/publish.log")" "::error::sbom.spdx.json is missing. v9.9.9 is not published without it."
  chk "W25 nor an empty one" "$(publish_release docker empty:sbom.spdx.json sbom.spdx.json.sigstore.json)" "1|"
  chk "W25 a dashboard handed on without its signature publishes nothing" \
    "$(publish_release docker $SBOM_PAIR grafana-dashboard.json grafana-dashboard.json.sha256)" "1|"
  chk_has "W25 naming the signature" "$(cat "$WORK/publish.log")" "::error::grafana-dashboard.json.sigstore.json is missing"
  chk "W25 nor one handed on as its checksum alone" "$(publish_release docker $SBOM_PAIR grafana-dashboard.json.sha256)" "1|"
  chk_has "W25 naming the dashboard" "$(cat "$WORK/publish.log")" "::error::grafana-dashboard.json is missing"
  jq -n '[{id: 1, draft: false, tag_name: "v9.9.9"}, {id: 2, draft: true, tag_name: "v9.9.8"}]' >"$WORK/releases.page1"
  jq -n '[{id: 77, draft: true, tag_name: "v9.9.9"}]' >"$WORK/releases.page2"
  chk "W25 an orphaned draft of the tag, even on a later page, is deleted before the create" \
    "$(publish_release docker $SBOM_PAIR)" "0|$LIST;api -X DELETE repos/o/app/releases/77;$CREATE $SBOM_PAIR;$READ"
  rm "$WORK/releases.page1" "$WORK/releases.page2"
  chk "W25 a Release that reads back without an asset fails" \
    "$(PUB_DROP=sbom.spdx.json.sigstore.json publish_release docker $SBOM_PAIR | cut -d'|' -f1)" "1"
  chk_has "W25 naming it" "$(cat "$WORK/publish.log")" "::error::Release v9.9.9 is missing asset sbom.spdx.json.sigstore.json"
  chk "W25 every other site keeps its notes-only create" "$(publish_release go)" "0|$CREATE"
  chk "W25 even with files beside it" "$(publish_release lane $SBOM_PAIR)" "0|$CREATE"
}
# ── Every run: a Release read that is not a 404 is no answer ────────────────
RBIN="$WORK/rbin"
mkdir -p "$RBIN"
cat >"$RBIN/gh" <<'SH'
#!/usr/bin/env bash
# The tag exists at this commit; the by-tag Release read answers REL_READ,
# and the release listing holds a draft of REL_DRAFT (or fails on "fail").
printf '%s\n' "$*" >>"$REL_LOG"
case "$*" in
  "api repos/o/app/git/ref/tags/$VERSION --jq .object.sha") echo "$GITHUB_SHA"; exit 0 ;;
  "api repos/o/app/releases/tags/$VERSION")
    case "$REL_READ" in
      200) echo '{}'; exit 0 ;;
      404) echo '{"message":"Not Found"}'; echo 'gh: Not Found (HTTP 404)' >&2 ;;
      403) echo 'gh: API rate limit exceeded for user ID 1. (HTTP 403)' >&2 ;;
      *) echo "gh: HTTP $REL_READ" >&2 ;;
    esac
    exit 1 ;;
  "api --paginate repos/o/app/releases?per_page=100 --jq .[] | select(.draft) | .tag_name")
    if [ "${REL_DRAFT:-}" = fail ]; then echo 'gh: HTTP 502' >&2; exit 1; fi
    printf '%s\n' "$VERSION-dev.1" ${REL_DRAFT:+"$REL_DRAFT"}
    exit 0 ;;
  "release create $VERSION --title $VERSION --notes-file NOTES.md") exit 0 ;;
esac
echo "stub: unexpected gh $*" >&2
exit 22
SH
chmod 755 "$RBIN/gh"
publish_site() { # <site> <read status> -> rc|the gh calls joined by ';'; env MODEL (legacy), DRAFT
  local d rc=0 rtag=v9.9.9 body="$WORK/publish-$1.sh" draft="${DRAFT:-}"
  if [ "$1" = lane ]; then rtag=yamlenv/v9.9.9; fi
  if [ "$draft" = tag ]; then draft=$rtag; fi
  d=$(mktemp -d "$WORK/ps.XXXXXX")
  echo notes >"$d/NOTES.md"
  : >"$d/log"
  (cd "$d" && PATH="$RBIN:$PATH" REL_LOG="$d/log" REL_READ="$2" REL_DRAFT="$draft" GH_TOKEN=t GITHUB_REPOSITORY=o/app \
    GITHUB_SHA=c0ffee VERSION="$rtag" CHANNEL=stable RELEASE_MODEL="${MODEL:-legacy}" CI_TOOLS="$ROOT/scripts" bash -e "$body") >"$WORK/ps.log" 2>&1 || rc=$?
  echo "$rc|$(paste -sd ';' "$d/log")"
}
chk "W26 no workflow reads a Release through gh release view, which bills GraphQL" "$(fact '."release-view-sites"')" "0 0"
for s in go ts lane; do
  rtag=v9.9.9 gate="\${{ needs.detect.outputs.channel == 'stable' }}"
  if [ "$s" = lane ]; then rtag=yamlenv/v9.9.9 gate=""; fi
  REF_S="api repos/o/app/git/ref/tags/$rtag --jq .object.sha" READ_S="api repos/o/app/releases/tags/$rtag"
  CREATE_S="release create $rtag --title $rtag --notes-file NOTES.md"
  LIST_S="api --paginate repos/o/app/releases?per_page=100 --jq .[] | select(.draft) | .tag_name"
  chk "W26 the $s site runs the helper from the ci source it checked out first, on the stable path" \
    "$(fact ".\"publish-tools:$s\"")" "\${{ github.workspace }}/.cplieger-ci/scripts|.cplieger-ci|True|$gate"
  chk "W26 the $s site reads the release model detect chose" "$(fact ".\"publish-model:$s\"")" '${{ needs.detect.outputs.release_model }}'
  chk "W26 the $s site leaves a present Release alone" "$(publish_site "$s" 200)" "0|$REF_S;$READ_S"
  chk "W26 the $s site creates the Release on a 404 with no draft of the tag" "$(publish_site "$s" 404)" \
    "0|$REF_S;$READ_S;$LIST_S;$CREATE_S"
  chk "W26 and on a legacy run leaves a draft of the tag alone, as gh release view read one" \
    "$(DRAFT=tag publish_site "$s" 404)" "0|$REF_S;$READ_S;$LIST_S"
  chk "W26 failing, not creating, when the release listing cannot be read" "$(DRAFT=fail publish_site "$s" 404)" \
    "1|$REF_S;$READ_S;$LIST_S"
  chk_has "W26 naming the read" "$(cat "$WORK/ps.log")" "::error::could not determine whether Release $rtag exists:
gh: HTTP 502"
  chk "W26 a two-branch run publishes beside a draft, which is no Release there" \
    "$(MODEL=two-branch DRAFT=tag publish_site "$s" 404)" "0|$REF_S;$READ_S;$CREATE_S"
  chk "W26 the $s site fails on a rate limit and creates nothing" "$(publish_site "$s" 403)" "1|$REF_S;$READ_S"
  chk_has "W26 naming the read and the limit" "$(cat "$WORK/ps.log")" \
    "::error::could not determine whether Release $rtag exists:
gh: API rate limit exceeded for user ID 1. (HTTP 403)"
  chk "W26 and on an outage" "$(publish_site "$s" 502)" "1|$REF_S;$READ_S"
done
REL_LOG="$WORK/rex.log" REL_READ=403 VERSION=v9.9.9 GITHUB_SHA=c0ffee PATH="$RBIN:$PATH" \
  bash "$ROOT/scripts/release-exists.sh" v9.9.9 >"$WORK/rex.out" 2>"$WORK/rex.err" || echo "EXIT=$?" >>"$WORK/rex.out"
chk "W26 release-exists.sh reads a rate limit as no answer, on stdout nothing a caller could read as absent" \
  "$(cat "$WORK/rex.out")" "EXIT=1"
chk "W27 a legacy run lists exactly these release.yaml jobs HEAD lacks" \
  "$(fact '."legacy-new-jobs"."release.yaml" | join(" ")')" \
  "barrier receipts renumber renumber-npm renumber-subpackages renumber-tag renumber-ts repair repair-assets repair-notes repair-publish repair-release"
chk "W27 and these docker-release.yaml jobs" "$(fact '."legacy-new-jobs"."docker-release.yaml" | join(" ")')" "receipt repair-assets"
for k in "docker []" 'docker ["web"]' "go []" "ts []" 'ts ["web"]' "none []"; do
  chk "W27 every one is skipped on a legacy $k run" "$(fact ".\"legacy-new-jobs-run\".\"$k\" | join(\" \")")" ""
done
chk "W27 only repair-assets passes repair-tag, which docker-release.yaml's repair-assets job alone reads" \
  "$(fact '."repair-tag-callers" | join(" ")')|$(fact '."dr-repair-assets-if"')" "repair-assets|\${{ inputs.repair-tag != '' }}"
chk_has "W27 and the docker receipt job needs a two-branch caller" "$(fact '."dr-receipt-if"')" "inputs.release-model == 'two-branch'"
chk "W9 every git-cliff step is token-free" "$(fact '."token-free" | [.[] | length] | add')" "0"
chk "W9 every notes site renders through render-notes.sh" "$(fact '.renders | [.[]] | all')" "true"
chk "W10 the root kind line replaces the promotion note" "$(fact '.outputs | has("promotion_note")')|$(fact '.outputs.release_kind_note')" \
  'false|${{ steps.pending.outputs.root_kind_note }}'
chk "W11 pending promotions join the publication on two-branch stable runs with one in range" "$(fact '."if:publish"')" \
  "steps.channel.outputs.release_model == 'two-branch' && steps.channel.outputs.channel == 'stable' && steps.pending.outputs.in_range_any == 'true'"
chk "W11 between the path diff and the release decision" "$(fact '."publish-after"')" "true"
for key in root_changed subpackages_to_publish go_modules_to_release; do
  chk "W11 detect publishes the joined $key" "$(fact ".outputs.$key")" \
    "\${{ steps.publish.outputs.$key || steps.changes.outputs.$key }}"
done
chk "W11 Select version decides on the joined sets" \
  "$(fact '."select-env".ROOT_CHANGED')|$(fact '."select-env".SUBPACKAGES_TO_PUBLISH')" \
  '${{ steps.publish.outputs.root_changed || steps.changes.outputs.root_changed }}|${{ steps.publish.outputs.subpackages_to_publish || steps.changes.outputs.subpackages_to_publish }}'
chk "W12 renumber checks the build out before computing its version" \
  "$(fact '."renumber-steps" | (index("Check out the newest build") < index("Compute lane version (cliff)"))')" "true"
chk "W12 and computes and hands on only when there is a build" \
  "$(fact '."renumber-if"."Compute lane version (cliff)"')|$(fact '."renumber-if"."Hand the version on"')" \
  "\${{ steps.target.outputs.commit != '' }}|\${{ steps.target.outputs.commit != '' }}"
chk "W12 renumber-tag re-selects, then tags, only the build handed on" \
  "$(fact '."renumber-tag-if"."Check out the numbered build"')|$(fact '."renumber-tag-if".Renumber')|$(fact '."renumber-tag-env"."Check out the numbered build".EXPECT_COMMIT')|$(fact '."renumber-tag-env".Renumber.DEV_VERSION')" \
  "\${{ steps.handoff.outputs.commit != '' }}|\${{ steps.handoff.outputs.commit != '' }}|\${{ steps.handoff.outputs.commit }}|\${{ steps.handoff.outputs.version }}"
chk "W12 reading the artifact its own leg of renumber wrote" \
  "$(fact '."renumber-upload".name')|$(fact '."renumber-fetch".name')|$(grep -c 'handoff-name renumber "$LANE_KEY"' "$WORK/renumber-write.sh")" \
  "\${{ steps.write.outputs.artifact }}|\${{ steps.name.outputs.artifact }}|1"
chk "W12 and running no cliff config" "$(fact '."renumber-tag-steps" | join(",")')" \
  "Checkout,Check out the ci source,Name the handoff,Fetch the handoff,Read the handoff,Check out the numbered build,Log in to GHCR,Renumber"
chk "W12 after every leg of renumber, whose write scopes it alone holds" \
  "$(fact '."needs:renumber-tag" | join(" ")')|$(fact '."perms:renumber" | tojson')|$(fact '."perms:renumber-tag" | tojson')" \
  'detect renumber|{"contents":"read"}|{"contents":"write","statuses":"write","packages":"write"}'
chk "T1 the caller pins the v3 pipeline by full commit" "$(fact '."tmpl-uses"')" "true"
chk "T1 with no with: key" "$(fact '."tmpl-with"')" "false"
chk "T1 and triggers on main and dev" "$(fact '."tmpl-push" | join(" ")')" "main dev"
chk "T2 the dispatch carries only the mode" "$(fact '."tmpl-inputs" | join(" ")')" "mode"
chk "T2 defaulting to normal" "$(fact '."tmpl-mode" | "\(.type) \(.default) \(.options | join(","))"')" "choice normal normal,renumber"
chk "T3 the caller grants the barrier's dispatch" "$(fact '."tmpl-perms".actions')" "write"

echo "PASS: release detect state machine ($PASS checks)"
