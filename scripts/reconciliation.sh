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
# 2 when the commit does not exist.
is_reconciliation() {
  local line
  local -a p
  known_commit "$1" || return 2
  line=$(git rev-list --parents -n1 "$1^{commit}" --)
  read -ra p <<<"$line"
  [ "${#p[@]}" -eq 3 ] || return 1
  [ "$(git rev-parse "${p[0]}^{tree}")" = "$(git rev-parse "${p[2]}^{tree}")" ] || return 1
  [ "$(git log -1 --format=%s "${p[0]}")" = "$RECONCILIATION_SUBJECT" ]
}

# newest_reconciliation <ref>: the newest reconciliation commit on the
# first-parent chain of <ref>, or nothing. Status 2 on an unknown <ref>.
newest_reconciliation() {
  local c
  known_commit "$1" || return 2
  while IFS= read -r c; do
    if is_reconciliation "$c"; then
      printf '%s\n' "$c"
      return 0
    fi
  done < <(git rev-list --first-parent --merges "$1")
}

# reconciliations_in <A> <B>: the reconciliation commits on the first-parent
# chain of <B> that <A> cannot reach, oldest first. An empty <A> means all.
# Status 2 on an unknown <A> or <B>.
reconciliations_in() {
  local c range=("$2")
  known_commit "$2" || return 2
  if [ -n "$1" ]; then
    known_commit "$1" || return 2
    range+=("^$1")
  fi
  while IFS= read -r c; do
    if is_reconciliation "$c"; then
      printf '%s\n' "$c"
    fi
  done < <(git rev-list --first-parent --merges --reverse "${range[@]}")
}

# promoted_digest <commit> [lane]: the lane's digest from the trailers of a
# reconciliation commit (the root lane when [lane] is empty). Status 1 when
# the lane has no trailer, more than one, or a malformed value; 2 when the
# commit does not exist.
promoted_digest() {
  local lane="${2:-}" value found="" count=0 digest
  known_commit "$1" || return 2
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
  done < <(git log -1 --format=%B "$1" | git interpret-trailers --parse | sed -n 's/^Promoted-Digest: //p')
  [ "$count" -eq 1 ] || return 1
  [[ $found =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
  printf '%s\n' "$found"
}
