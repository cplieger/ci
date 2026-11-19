#!/usr/bin/env bash
# Extract FILE from the newest unexpired artifact NAME that a workflow run of
# this repository on branch REF uploaded, into OUTDIR. With none, or when the
# lookup fails or FILE is missing, it writes nothing and exits 0: the caller then
# plans without history, from its tool's prior. Matching the run's own ref keeps
# a branch dispatch from feeding main's runs; matching its head repository keeps
# a fork's pull request run, whose branch can bear any name, from feeding either.
# Usage: mutation-history.sh NAME REF OUTDIR FILE; needs GH_TOKEN with actions: read and GITHUB_REPOSITORY.
set -euo pipefail

# shellcheck source=SCRIPTDIR/gha-log.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/gha-log.sh"

name="${1:?artifact name}"
ref="${2:?ref}"
out="${3:?output dir}"
file="${4:?file in the artifact}"
: "${GITHUB_REPOSITORY:?}"
# shellcheck source=SCRIPTDIR/retry.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/retry.sh"

work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT

list() {
  gh api --paginate "repos/${GITHUB_REPOSITORY}/actions/artifacts?name=${name}&per_page=100" \
    --jq '.artifacts[]' >"${work}/list.jsonl"
}
if ! retry list; then
  gha_annotate warning '%s: the artifact list could not be read, so this run has no history' "${name}"
  exit 0
fi
id=$(jq -rs --arg ref "${ref}" '
  map(select((.expired | not) and .workflow_run.head_branch == $ref
    and .workflow_run.repository_id != null
    and .workflow_run.head_repository_id == .workflow_run.repository_id))
  | sort_by(.created_at) | last | .id // empty' "${work}/list.jsonl")
if [ -z "${id}" ]; then
  gha_say '%s: no earlier run on %s left one, so this run has no history' "${name}" "${ref}"
  exit 0
fi
download() {
  gh api "repos/${GITHUB_REPOSITORY}/actions/artifacts/${id}/zip" >"${work}/a.zip"
}
if ! retry download; then
  gha_annotate warning '%s: artifact %s could not be downloaded, so this run has no history' "${name}" "${id}"
  exit 0
fi
if ! unzip -p "${work}/a.zip" "${file}" >"${work}/file" || [ ! -s "${work}/file" ]; then
  gha_annotate warning '%s: artifact %s has no readable %s, so this run has no history' "${name}" "${id}" "${file}"
  exit 0
fi
mkdir -p "${out}"
mv "${work}/file" "${out}/${file}"
gha_say '%s: history from artifact %s' "${name}" "${id}"
