#!/usr/bin/env bash
# Check the alert rule files of the repository in the working directory.
# Env: PROMTOOL_VERSION and LOKITOOL_VERSION (the pins), ALERT_RULES_TOOLS
# (tool dir, default $RUNNER_TEMP/alert-rules).
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tools=${ALERT_RULES_TOOLS:-${RUNNER_TEMP:?}/alert-rules}
rc=0

if [ -f alerts/promql.yaml ]; then
  promtool=$(bash "$here/install.sh" promtool "${PROMTOOL_VERSION:?}" "$tools")
  if ! "$promtool" check rules alerts/promql.yaml; then
    echo "::error file=alerts/promql.yaml::promtool check rules rejects alerts/promql.yaml"
    rc=1
  fi
  if [ -f alerts/promql_test.yaml ] && ! "$promtool" test rules alerts/promql_test.yaml; then
    echo "::error file=alerts/promql_test.yaml::promtool test rules fails on alerts/promql_test.yaml"
    rc=1
  fi
elif [ -f alerts/promql_test.yaml ]; then
  echo "::error file=alerts/promql_test.yaml::alerts/promql_test.yaml has no alerts/promql.yaml to test"
  rc=1
fi

if [ -f alerts/logql.yaml ]; then
  lokitool=$(bash "$here/install.sh" lokitool "${LOKITOOL_VERSION:?}" "$tools")
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  # lokitool rules lint rewrites the file it reads and strips its comments
  # while exiting 0, so it parses a copy.
  cp alerts/logql.yaml "$work/logql.yaml"
  if ! "$lokitool" rules lint "$work/logql.yaml"; then
    echo "::error file=alerts/logql.yaml::lokitool cannot parse alerts/logql.yaml"
    rc=1
  fi
fi

if [ ! -f alerts/promql.yaml ] && [ ! -f alerts/logql.yaml ] && [ ! -f alerts/promql_test.yaml ]; then
  echo "no alerts/promql.yaml or alerts/logql.yaml - skipping"
fi
exit "$rc"
