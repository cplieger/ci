#!/usr/bin/env bash
# sbom-summary.sh <sbom.spdx.json> [title]: a job-summary table of the SBOM's
# packages bar the root syft DESCRIBES (the image), capped at 400 rows.
set -euo pipefail

SBOM=${1:?usage: sbom-summary.sh SBOM [TITLE]}
TITLE=${2:-Shipped package versions}
echo "## ${TITLE}"
echo ""
echo "| Package | Version |"
echo "|---|---|"
jq -r '
  ( [ .documentDescribes // [] | .[] ]
    + [ .relationships[]?
        | select(.relationshipType == "DESCRIBES"
                 and .spdxElementId == "SPDXRef-DOCUMENT")
        | .relatedSpdxElement ] ) as $roots
  | [ .packages[]?
      | select(.SPDXID as $id | ($roots | index($id)) == null)
      | "| \(.name) | \(.versionInfo // "(none)") |" ]
  | sort | unique
  | if length > 400 then
      .[0:400] + ["", "(+ \(length - 400) more in the SBOM attested to the image)"]
    else . end
  | .[]' "$SBOM"
