#!/usr/bin/env bash
# Recognises a promotion's reconciliation commit on main; source it (it sets
# no shell options). Shape: exactly two parents, the second parent's tree,
# subject RECONCILIATION_SUBJECT. "On main" is main's first-parent chain,
# which is all the walkers below visit.

# Trailer contract: one `Promoted-Digest:` trailer per image lane, valued
# `sha256:<64 hex>` for the root lane and `<lane dir> sha256:<64 hex>` for a
# nested one.
RECONCILIATION_SUBJECT='release: promote dev into main'

known_commit() {
  git rev-parse --verify --quiet "$1^{commit}" >/dev/null && return 0
  echo "reconciliation: unknown commit '$1'" >&2
  return 1
}

# is_reconciliation <commit>: status 0 when it has the shape, 1 when not,
# 2 when the commit does not exist or git cannot read it.
is_reconciliation() {
  local line first second subject
  local -a p
  known_commit "$1" || return 2
  line=$(git rev-list --parents -n1 "$1^{commit}" --) || return 2
  read -ra p <<<"$line"
  [ "${#p[@]}" -eq 3 ] || return 1
  first=$(git rev-parse "${p[0]}^{tree}") || return 2
  second=$(git rev-parse "${p[2]}^{tree}") || return 2
  [ "$first" = "$second" ] || return 1
  subject=$(git log -1 --format=%s "${p[0]}") || return 2
  [ "$subject" = "$RECONCILIATION_SUBJECT" ]
}

# reconciliations_of <first|all> <rev-list args...>: the reconciliation
# commits rev-list names, in its order, or only the first of them. Status 2
# when git cannot list or read them.
reconciliations_of() {
  local which=$1 c revs rc
  shift
  revs=$(git rev-list "$@") || return 2
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    rc=0
    is_reconciliation "$c" || rc=$?
    case "$rc" in
      0)
        printf '%s\n' "$c"
        [ "$which" = all ] || return 0
        ;;
      1) ;;
      *) return 2 ;;
    esac
  done <<<"$revs"
}

# newest_reconciliation <ref>: the newest reconciliation commit on the
# first-parent chain of <ref>, or nothing. Status 2 on an unknown <ref> or
# a chain git cannot walk.
newest_reconciliation() {
  known_commit "$1" || return 2
  reconciliations_of first --first-parent --merges "$1"
}

# reconciliations_in <A> <B>: the reconciliation commits on the first-parent
# chain of <B> that <A> cannot reach, oldest first. An empty <A> means all.
# Status 2 on an unknown <A> or <B>, or a chain git cannot walk.
reconciliations_in() {
  local range=("$2")
  known_commit "$2" || return 2
  if [ -n "$1" ]; then
    known_commit "$1" || return 2
    range+=("^$1")
  fi
  reconciliations_of all --first-parent --merges --reverse "${range[@]}"
}

# promoted_digest <commit> [lane]: the lane's digest from the trailers of a
# reconciliation commit (the root lane when [lane] is empty). Status 1 when
# the lane has no trailer, more than one, or a malformed value; 2 when the
# commit does not exist or git cannot read its trailers.
promoted_digest() {
  local lane="${2:-}" value found="" count=0 digest message trailers values
  known_commit "$1" || return 2
  message=$(git log -1 --format=%B "$1") || return 2
  trailers=$(git interpret-trailers --parse <<<"$message") || return 2
  values=$(sed -n 's/^Promoted-Digest: //p' <<<"$trailers")
  while IFS= read -r value; do
    if [ -n "$lane" ]; then
      [ "${value%% *}" = "$lane" ] || continue
      digest="${value#* }"
    else
      case "$value" in *" "*) continue ;; esac
      digest="$value"
    fi
    count=$((count + 1))
    found="$digest"
  done <<<"$values"
  [ "$count" -eq 1 ] || return 1
  [[ $found =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
  printf '%s\n' "$found"
}
