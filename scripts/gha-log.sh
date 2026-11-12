# shellcheck shell=bash
# The shell output path for a log line that holds target-written data, the twin
# of scripts/mutation_cost.py say and annotate. FORMAT is printf's, written by the
# script; each ARG is data. Sourced, never run.

# ARG as an annotation's message, escaped as the Actions toolkit's escapeData does.
gha_escape() {
  local s=$1
  s=${s//%/%25}
  s=${s//$'\r'/%0D}
  s=${s//$'\n'/%0A}
  printf '%s' "$s"
}

# gha_annotate KIND FORMAT ARG...: one ::KIND:: annotation.
gha_annotate() {
  local kind=$1 format=$2 a
  shift 2
  case "$kind" in notice | warning | error) ;; *) return 2 ;; esac
  local args=()
  for a in "$@"; do args+=("$(gha_escape "$a")"); done
  # shellcheck disable=SC2059 # FORMAT is the caller's literal
  printf "::%s::${format}\n" "$kind" "${args[@]}"
}

# gha_say FORMAT ARG...: one plain line behind "- ", so no ARG starts a command.
# The runner also reads `##[command]` anywhere in a plain line
# (actions/runner ActionCommand.TryParse), so '#' is escaped too.
gha_say() {
  local format=$1 a e
  shift
  local args=()
  for a in "$@"; do
    e=$(gha_escape "$a")
    args+=("${e//#/%23}")
  done
  # shellcheck disable=SC2059 # FORMAT is the caller's literal
  printf -- "- ${format}\n" "${args[@]}"
}
