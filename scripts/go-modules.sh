#!/usr/bin/env bash
# Go module ownership. A module is a dir below the root with a tracked go.mod,
# less those under node_modules, vendor, testdata, static or dist; in a tree a
# path belongs to its nearest owner (owners_in), else to the root '.'.
#   list <rev> | boundaries <rev>: the modules, or the lanes plus the modules
#   their container does not ship. commits and paths: see walk. Env
#   GO_LANES_JSON, REPO_TYPE (docker|go|ts|none|empty). Prints sorted dirs or
#   tab-separated lines; exit 2 on an unreadable rev or input.
set -euo pipefail

die() {
  echo "::error::go-modules: $*" >&2
  exit 2
}
usage="usage: go-modules.sh list <rev> | boundaries <rev> | commits <from> <to> <unit> | paths <from> <to> <unit>..."
[ "$#" -ge 2 ] || die "$usage"
cmd=$1
case "$cmd" in
  list | boundaries) [ "$#" -eq 2 ] || die "$usage" ;;
  commits) [ "$#" -eq 4 ] || die "$usage" ;;
  paths) [ "$#" -ge 4 ] || die "$usage" ;;
  *) die "unknown command '${cmd}'" ;;
esac

tree_of() { # <rev> -> every tracked path at <rev>
  git -c core.quotePath=false ls-tree -r --name-only "$1" || die "cannot list the tree of $1"
}
modules_of() { # <paths> -> the dirs holding a go.mod among them
  # A committed node_modules or vendor tree carries upstream go.mod files.
  grep -E '/go\.mod$' <<<"$1" | grep -Ev '(^|/)(node_modules|vendor|testdata|static|dist)/' \
    | sed 's|/go\.mod$||' | LC_ALL=C sort -u || true
}
if [ "$cmd" = list ]; then
  FILES=$(tree_of "$2") || exit 2
  modules_of "$FILES" | sed '/^$/d'
  exit 0
fi

LANES=$(jq -r 'if type == "array" and all(.[]; type == "string") then .[] else error("not an array of strings") end' \
  <<<"${GO_LANES_JSON:-[]}") || die "GO_LANES_JSON must be a JSON array of dirs, got '${GO_LANES_JSON:-}'"
declare -A IS_LANE=()
while IFS= read -r l; do
  [ -z "$l" ] || IS_LANE["$l"]=1
done <<<"$LANES"
REPO_TYPE=${REPO_TYPE:-}
case "$REPO_TYPE" in
  go | none) ZIP_ROOT=true ;;
  "" | docker | ts) ZIP_ROOT=false ;;
  *) die "REPO_TYPE must be docker, go, ts, none or empty, got '${REPO_TYPE}'" ;;
esac

lane_holding() { # <dir> -> the lane <dir> sits strictly inside; status 1 for none
  local l
  while IFS= read -r l; do
    if [ -n "$l" ] && [[ $1 == "$l/"* ]]; then
      printf '%s' "$l"
      return 0
    fi
  done <<<"$LANES"
  return 1
}
# MOD[<state>|<dir>] marks a module, NEST[<state>|<dir>] one that is no lane
# and that its container does not ship; STATE[<rev>] names <rev>'s state.
declare -A MOD=() NEST=() STATE=()
load_state() { # <rev>: fills MOD and NEST for the state named <rev>
  local k=$1 files modules packages m l p skip
  files=$(tree_of "$k") || exit 2
  modules=$(modules_of "$files")
  packages=$(grep -E '^[^/]+/jsr\.json$' <<<"$files" | sed 's|/jsr\.json$||' || true)
  STATE["$k"]=$k
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    MOD["$k|$m"]=1
    [ -z "${IS_LANE[$m]:-}" ] || continue
    # A go.mod in a TS package (<lane>/web, or a root subpackage outside every
    # lane) only keeps Go tooling out: the package ships its tree.
    if l=$(lane_holding "$m"); then
      [[ $m != "$l/web" && $m != "$l/web/"* ]] || ! grep -qxF -- "$l/web/jsr.json" <<<"$files" \
        || continue
      NEST["$k|$m"]=1
      continue
    fi
    skip=false
    while IFS= read -r p; do
      [[ -z $p || ($m != "$p" && $m != "$p/"*) ]] || skip=true
    done <<<"$packages"
    # A lane is a module zip, which leaves out every nested module. So is a
    # go root; a docker or ts root builds its artifact from the whole tree.
    if ! $skip && $ZIP_ROOT; then
      NEST["$k|$m"]=1
    fi
  done <<<"$modules"
}

if [ "$cmd" = boundaries ]; then
  load_state "$2"
  for key in "${!NEST[@]}"; do
    printf '%s\n' "${key#*|}"
  done | cat - <(printf '%s\n' "$LANES") | sed '/^$/d' | LC_ALL=C sort -u
  exit 0
fi

OWN=()
owners_in() { # <state> <path>: OWN gets the units owning <path> in that state's tree
  local k=$1 d=$2 key
  OWN=()
  while [[ $d == */* ]]; do
    d=${d%/*}
    key="$k|$d"
    if [ -n "${IS_LANE[$d]:-}" ]; then
      OWN+=("$d")
      # A go root's zip holds a lane's files until the lane has its go.mod.
      if ! $ZIP_ROOT || [ -n "${MOD[$key]:-}" ]; then
        return 0
      fi
    elif [ -n "${NEST[$key]:-}" ]; then
      return 0
    fi
  done
  OWN+=(.)
}

# walk: each non-merge commit of <from>..<to> and each path it changes; a path
# counts for its owner in each tree holding it, and a go.mod added or removed
# over other files for its dir's owner in the tree without it.
#   commits <from> <to> <unit>: "<sha>\t<path>" per path <unit> owns so, over
#   <to>'s whole history for an empty <from> or a non-commit one (release-state.sh
#   half_changed passes the empty tree for a first build).
#   paths <from> <to> <unit>...: "<unit>\t<path>" per such path on stdin; one no
#   commit changes counts at <from>'s tree and <to>'s; an empty <from> walks
#   nothing and judges every path at <to>'s tree.
FROM=$2 TO=$3
shift 3
declare -A ASKED=() WANT=() COVERED=() SEEN=()
for u in "$@"; do
  [ "$u" = . ] || [ -n "${IS_LANE[$u]:-}" ] || die "unit '${u}' is neither '.' nor a lane of GO_LANES_JSON"
  ASKED["$u"]=1
done
if [ "$cmd" = paths ]; then
  while IFS= read -r f; do
    [ -z "$f" ] || WANT["$f"]=1
  done
fi
state_of() { # <rev>: K names <rev>'s state, loaded once
  if [ -z "${STATE[$1]:-}" ]; then
    load_state "$1"
  fi
  K=${STATE[$1]}
}
emit() { # <state> <sha> <path>: prints the asked units owning <path> in that state
  local u key
  owners_in "$1" "$3"
  for u in "${OWN[@]}"; do
    [ -n "${ASKED[$u]:-}" ] || continue
    key="$u|$3"
    [ "$cmd" = paths ] || key="$2|$3"
    [ -z "${SEEN[$key]:-}" ] || continue
    SEEN["$key"]=1
    if [ "$cmd" = commits ]; then
      printf '%s\t%s\n' "$2" "$3"
    else
      printf '%s\t%s\n' "$u" "$3"
    fi
  done
}
holds_under() { # <rev> <dir>: status 0 when <rev>'s tree holds a file under <dir>
  local out
  out=$(git -c core.quotePath=false ls-tree --name-only "$1" -- "$2/") || die "cannot list ${2}/ at $1"
  [ -n "$out" ]
}
commit_done() { # SHA PARENT CHANGES: emits one commit's paths
  local pk=- ck st f other
  [ -n "$SHA" ] || return 0
  if [ -n "$PARENT" ]; then
    state_of "$PARENT"
    pk=$K
  fi
  # Only a go.mod or a jsr.json moves an owner: else the parent's state holds.
  if grep -qE '(^|/)(go\.mod|jsr\.json)$' <<<"$CHANGES"; then
    state_of "$SHA"
    ck=$K
  else
    STATE["$SHA"]=$pk
    ck=$pk
  fi
  while IFS=$'\t' read -r st f; do
    [ -n "$f" ] || continue
    if [ "$cmd" = paths ]; then
      [ -n "${WANT[$f]:-}" ] || continue
      COVERED["$f"]=1
    fi
    [ "$st" = A ] || emit "$pk" "$SHA" "$f"
    [ "$st" = D ] || emit "$ck" "$SHA" "$f"
    if [[ $f == */go.mod && ($st == A || $st == D) ]]; then
      other=$PARENT
      [ "$st" = A ] || other=$SHA
      if [ -n "$other" ] && holds_under "$other" "${f%/go.mod}"; then
        if [ "$st" = A ]; then emit "$pk" "$SHA" "$f"; else emit "$ck" "$SHA" "$f"; fi
      fi
    fi
  done <<<"$CHANGES"
}

if [ "$cmd" = commits ] || [ -n "$FROM" ]; then
  RANGE=("$TO")
  if [ -n "$FROM" ] && git rev-parse --verify --quiet "${FROM}^{commit}" >/dev/null; then
    RANGE=("${FROM}..${TO}")
  fi
  LOG=$(git -c core.quotePath=false log --no-merges --root --no-renames --reverse --topo-order \
    --format='%x01%H %P' --name-status "${RANGE[@]}" --) || die "cannot walk ${RANGE[*]}"
  SHA="" PARENT="" CHANGES=""
  while IFS= read -r line; do
    if [[ $line == $'\x01'* ]]; then
      commit_done
      read -r SHA PARENT <<<"${line#$'\x01'}"
      CHANGES=""
    elif [ -n "$line" ]; then
      CHANGES+="$line"$'\n'
    fi
  done <<<"$LOG"
  commit_done
fi
if [ "$cmd" = paths ]; then
  ENDS=("$TO")
  [ -z "$FROM" ] || ENDS+=("$FROM")
  for end in "${ENDS[@]}"; do
    state_of "$end"
    for f in "${!WANT[@]}"; do
      [ -z "${COVERED[$f]:-}" ] || continue
      [ -z "$FROM" ] || git cat-file -e "${end}:${f}" 2>/dev/null || continue
      emit "$K" - "$f"
    done
  done
fi
