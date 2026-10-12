#!/usr/bin/env bash
# Regression probe for scripts/reconciliation.sh, the one recogniser of a
# promotion's reconciliation commit, run against throwaway repositories.
set -euo pipefail

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=probe GIT_AUTHOR_EMAIL=probe@example.invalid
export GIT_COMMITTER_NAME=probe GIT_COMMITTER_EMAIL=probe@example.invalid

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

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d /tmp/reconciliation-probe.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
# shellcheck source=scripts/reconciliation.sh
. "$ROOT/scripts/reconciliation.sh"

SUBJECT='release: promote dev into main'
D1="sha256:$(printf 'a%.0s' {1..64})"
D2="sha256:$(printf 'b%.0s' {1..64})"
status() { # <cmd...> -> its exit status
  local rc=0
  "$@" >/dev/null 2>&1 || rc=$?
  echo "$rc"
}
commit() { # <message> <file> -> sha; appends a line to <file> on the current branch
  echo "$1" >>"$2"
  git add -A
  git commit -qm "$1"
  git rev-parse HEAD
}
move_main() { # <new> -> points main at <new> and checks it out
  git checkout -q --detach
  git update-ref refs/heads/main "$1"
  git checkout -q main
}
mkr() { # <tree-of> <message> <parent>... -> sha of a commit-tree commit
  local tree="$1" msg="$2" args=()
  shift 2
  for p in "$@"; do args+=(-p "$p"); done
  printf '%s\n' "$msg" | git commit-tree "$(git rev-parse "$tree^{tree}")" "${args[@]}"
}

R="$WORK/repo"
git init -q -b main "$R"
cd "$R" || exit 1
B=$(commit "feat: base" app.go)
git checkout -q -b dev
T=$(commit "feat: dev work" app.go)
git checkout -q main
M=$(commit "fix(deps): main maintenance" go.mod)

# ── Shape ────────────────────────────────────────────────────────────────────
GOOD=$(mkr "$T" "$SUBJECT

Promoted-Digest: $D1" "$M" "$T")
chk "R1 a [M,T] merge with T's tree and the subject is a reconciliation" "$(status is_reconciliation "$GOOD")" 0
chk "R2 a wrong subject is not" "$(status is_reconciliation "$(mkr "$T" "release: promote dev to main" "$M" "$T")")" 1
chk "R2 nor is the subject with a suffix" "$(status is_reconciliation "$(mkr "$T" "$SUBJECT (manual)" "$M" "$T")")" 1
chk "R3 a tree differing from the second parent's is not" "$(status is_reconciliation "$(mkr "$M" "$SUBJECT" "$M" "$T")")" 1
chk "R3 nor are the parents swapped (tree equal to the first parent's)" \
  "$(status is_reconciliation "$(mkr "$T" "$SUBJECT" "$T" "$M")")" 1
chk "R4 an octopus is not, even with the second parent's tree" \
  "$(status is_reconciliation "$(mkr "$T" "$SUBJECT" "$M" "$T" "$B")")" 1
chk "R5 a single-parent commit with the subject and its parent's tree is not" \
  "$(status is_reconciliation "$(mkr "$T" "$SUBJECT" "$T")")" 1
chk "R6 an unknown commit is status 2" "$(status is_reconciliation 0123456789abcdef0123456789abcdef01234567)" 2

# ── Walkers: main's first-parent chain only ──────────────────────────────────
chk "R7 no reconciliation yet on main" "$(newest_reconciliation main)|$(status newest_reconciliation main)" "|0"
move_main "$GOOD"
S=$(commit "fix(deps): after the promotion" go.mod)
chk "R8 the newest reconciliation is found past a later commit" "$(newest_reconciliation main)" "$GOOD"
git checkout -q dev
T2=$(commit "feat: more dev work" app.go)
git checkout -q main
GOOD2=$(mkr "$T2" "$SUBJECT" "$S" "$T2")
move_main "$GOOD2"
chk "R9 the newer of two reconciliations wins" "$(newest_reconciliation main)" "$GOOD2"
chk "R10 every reconciliation on main, oldest first" "$(reconciliations_in "" main | tr '\n' ' ')" "$GOOD $GOOD2 "
chk "R10 a range start excludes what it reaches" "$(reconciliations_in "$GOOD" main)" "$GOOD2"
chk "R10 an empty range yields nothing" "$(reconciliations_in main main)" ""
# A shape-valid commit reachable only through a second parent is not on main.
git checkout -q -b side "$GOOD2"
# Dated later than GOOD2, so a walk that left the first-parent chain would meet it first.
SIDE_R=$(GIT_COMMITTER_DATE='2030-01-01T00:00:00Z' mkr "$T2" "$SUBJECT" "$GOOD2" "$T2")
git checkout -q main
git merge -q --no-ff -m "chore: side merge" "$SIDE_R"
chk "R11 the side commit has the shape" "$(status is_reconciliation "$SIDE_R")" 0
chk "R11 but is not on main's first-parent chain" "$(newest_reconciliation main)" "$GOOD2"
chk "R11 and the range walk skips it" "$(reconciliations_in "" main | tr '\n' ' ')" "$GOOD $GOOD2 "
chk "R12 an unknown ref is status 2 for both walkers" \
  "$(status newest_reconciliation nope) $(status reconciliations_in nope main) $(status reconciliations_in "" nope)" "2 2 2"

# ── Promoted-Digest trailers ─────────────────────────────────────────────────
chk "R13 the root lane's digest" "$(promoted_digest "$GOOD")" "$D1"
TWO=$(mkr "$T" "$SUBJECT

Promoted-Digest: $D1
Promoted-Digest: tools/gen $D2" "$M" "$T")
chk "R14 two lanes: the root lane" "$(promoted_digest "$TWO")" "$D1"
chk "R14 two lanes: the nested lane" "$(promoted_digest "$TWO" tools/gen)" "$D2"
chk "R15 a lane with no trailer is status 1" "$(status promoted_digest "$TWO" web)" 1
chk "R15 no trailer at all is status 1" "$(status promoted_digest "$GOOD2")" 1
DUP=$(mkr "$T" "$SUBJECT

Promoted-Digest: $D1
Promoted-Digest: $D2" "$M" "$T")
chk "R16 two digests for one lane are status 1" "$(status promoted_digest "$DUP")" 1
EMPTY_TRAILER='Promoted-Digest: '
EMPTY_DUP=$(mkr "$T" "$SUBJECT

$EMPTY_TRAILER
Promoted-Digest: $D1" "$M" "$T")
chk "R16 the empty trailer reaches the parser" \
  "$(git log -1 --format=%B "$EMPTY_DUP" | git interpret-trailers --parse | grep -c '^Promoted-Digest: $')" 1
chk "R16 as are an empty root trailer beside a valid one" "$(status promoted_digest "$EMPTY_DUP")" 1
BAD=$(mkr "$T" "$SUBJECT

Promoted-Digest: sha256:abc" "$M" "$T")
chk "R17 a malformed digest is status 1" "$(status promoted_digest "$BAD")" 1
BODY=$(mkr "$T" "$SUBJECT

Promoted-Digest: $D1

Signed-off-by: probe <probe@example.invalid>" "$M" "$T")
chk "R18 a trailer outside the last paragraph is not read" "$(status promoted_digest "$BODY")" 1
chk "R19 an unknown commit is status 2, not a missing trailer" \
  "$(status promoted_digest 0123456789abcdef0123456789abcdef01234567)" 2

# ── A git that fails: status 2, never "no" ───────────────────────────────────
# The stub fails only call FAIL_GIT_N among those whose argv matches the glob
# FAIL_GIT; git_calls is how many matched.
GIT_FAIL="$WORK/git-fail"
mkdir -p "$GIT_FAIL"
cat >"$GIT_FAIL/git" <<SH
#!/usr/bin/env bash
if [[ "\$*" == \${FAIL_GIT:?} ]]; then
  n=\$((\$(cat "$GIT_FAIL/n" 2>/dev/null || echo 0) + 1))
  echo "\$n" >"$GIT_FAIL/n"
  [ "\$n" -ne "\${FAIL_GIT_N:?}" ] || { echo "stub: git \$* fails" >&2; exit 92; }
fi
exec "$(command -v git)" "\$@"
SH
chmod 755 "$GIT_FAIL/git"
failing() { # <argv glob> <N> <function> <args...> -> its exit status|matching git calls
  local rc=0
  rm -f "$GIT_FAIL/n"
  FAIL_GIT="$1" FAIL_GIT_N="$2" PATH="$GIT_FAIL:$PATH" "${@:3}" >/dev/null 2>&1 || rc=$?
  echo "$rc|$(cat "$GIT_FAIL/n" 2>/dev/null || echo 0)"
}
chk "R20 a commit whose parents git cannot read is status 2" \
  "$(failing 'rev-list --parents -n1 *' 1 is_reconciliation "$GOOD")" "2|1"
chk "R20 as is one whose own tree git cannot read" \
  "$(failing "rev-parse $GOOD^{tree}" 1 is_reconciliation "$GOOD")" "2|1"
chk "R20 or whose second parent's" "$(failing "rev-parse $T^{tree}" 1 is_reconciliation "$GOOD")" "2|1"
chk "R20 or whose subject git cannot read" "$(failing 'log -1 --format=%s *' 1 is_reconciliation "$GOOD")" "2|1"
chk "R21 a first-parent chain git cannot walk is status 2 for the newest" \
  "$(failing 'rev-list --first-parent --merges *' 1 newest_reconciliation main)" "2|1"
# main's first-parent merges, newest first: the side merge, GOOD2, GOOD.
chk "R21 as is a reconciliation on it git cannot read" \
  "$(failing 'rev-list --parents -n1 *' 2 newest_reconciliation main)" "2|2"
chk "R21 and for the range" "$(failing 'rev-list --first-parent --merges --reverse *' 1 reconciliations_in "" main)" "2|1"
chk "R21 as is a reconciliation in it git cannot read" \
  "$(failing 'rev-list --parents -n1 *' 2 reconciliations_in "" main)" "2|2"
chk "R22 trailers whose message git cannot read are status 2, not a missing trailer" \
  "$(failing 'log -1 --format=%B *' 1 promoted_digest "$GOOD")" "2|1"
chk "R22 as are trailers git cannot parse" "$(failing 'interpret-trailers --parse' 1 promoted_digest "$GOOD")" "2|1"

echo "PASS: reconciliation recogniser ($PASS checks)"
