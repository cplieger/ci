# shellcheck shell=bash
# The shell output path for a workflow command or a log line that holds data, the
# twin of scripts/workflow_commands.py. FORMAT is printf's, written by the script;
# each ARG and property value is data. Sourced, never run. The escaping is the
# Actions toolkit's: https://github.com/actions/toolkit/blob/main/packages/core/src/command.ts

# ARG as a command's message: %, CR and LF escaped.
gha_escape() {
  local s=$1
  s=${s//%/%25}
  s=${s//$'\r'/%0D}
  s=${s//$'\n'/%0A}
  printf '%s' "$s"
}

# ARG as a command's property value: gha_escape plus : and ,.
gha_escape_property() {
  local s
  s=$(gha_escape "$1")
  s=${s//:/%3A}
  s=${s//,/%2C}
  printf '%s' "$s"
}

# gha_annotate KIND [-p KEY=VALUE]... FORMAT ARG...: one ::KIND:: line, KIND one
# of notice, warning, error and group. A property with an empty value is left
# out, as the toolkit does; a KEY outside A-Z and a-z is refused with exit 2.
gha_annotate() {
  local kind=$1 props='' kv a
  shift
  case "$kind" in notice | warning | error | group) ;; *) return 2 ;; esac
  while [ "${1-}" = -p ]; do
    kv=${2-}
    shift 2 || return 2
    case "$kv" in [A-Za-z]*=*) ;; *) return 2 ;; esac
    case "${kv%%=*}" in *[!A-Za-z]*) return 2 ;; esac
    [ -z "${kv#*=}" ] || props+="${props:+,}${kv%%=*}=$(gha_escape_property "${kv#*=}")"
  done
  local format=$1
  shift
  local args=()
  for a in "$@"; do args+=("$(gha_escape "$a")"); done
  # shellcheck disable=SC2059 # FORMAT is the caller's literal
  printf "::%s%s::${format}\n" "$kind" "${props:+ $props}" "${args[@]}"
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
