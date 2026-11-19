#!/usr/bin/env bash
# Go module ownership. A module is a dir below the root with a tracked go.mod,
# less those under node_modules, vendor, testdata, static or dist. In a tree a
# path belongs to the units whose artifact ships it (owners_in): the root '.'
# or a lane, each a module zip but a docker or ts root, built from the tree.
#   list <rev> | boundaries <rev>: the modules, or the lanes plus the go.mod
#   dirs their container does not ship. commits and paths: see walk. Env
#   GO_LANES_JSON, REPO_TYPE (docker|go|ts|none|empty). Prints sorted dirs or
#   tab-separated lines; exit 2 on an unreadable rev or input.
set -euo pipefail

# shellcheck source=SCRIPTDIR/gha-log.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/gha-log.sh"

die() {
  gha_annotate error 'go-modules: %s' "$*" >&2
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

tree_of() { # <rev> -> "<mode> <type> <object>\t<path>" per tracked path at <rev>
  git -c core.quotePath=false ls-tree -r "$1" || die "cannot list the tree of $1"
}
modules_of() { # <paths> -> the dirs holding a go.mod among them
  # A committed node_modules or vendor tree carries upstream go.mod files.
  grep -E '/go\.mod$' <<<"$1" | grep -Ev '(^|/)(node_modules|vendor|testdata|static|dist)/' \
    | sed 's|/go\.mod$||' | LC_ALL=C sort -u || true
}
if [ "$cmd" = list ]; then
  FILES=$(tree_of "$2" | cut -f2-) || exit 2
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
go_new() { # <rev> <go.mod path>: status 0 when its go line is 1.24 or later
  local v maj min
  v=$(git cat-file -p "$1:$2" 2>/dev/null \
    | sed -nE '/^[[:space:]]*go[[:space:]]/{s/^[[:space:]]*go[[:space:]]+([0-9]+)\.([0-9]+).*/\1 \2/p;q;}') || true
  read -r maj min <<<"$v" || true
  [ -n "${min:-}" ] && ((10#$maj > 1 || (10#$maj == 1 && 10#$min >= 24)))
}
# MOD[<state>|<dir>] marks a dir holding a go.mod, NEST[<state>|<dir>] one that
# is no lane and that its container does not ship, PKG[<state>|<dir>] a TS
# package, NEWVENDOR[<state>|<unit>] an asked zip unit on go 1.24 or later;
# STATE[<rev>] names <rev>'s state.
declare -A MOD=() NEST=() PKG=() NEWVENDOR=() STATE=() ASKED=()
load_state() { # <rev>: fills MOD, NEST, PKG and NEWVENDOR for the state named <rev>
  local k=$1 tree files dirs m l p
  tree=$(tree_of "$k") || exit 2
  files=$(cut -f2- <<<"$tree")
  # golang.org/x/mod/zip leaves out every dir holding a regular go.mod, in any case.
  dirs=$(grep -E '^100' <<<"$tree" | cut -f2- | grep -iE '/go\.mod$' | sed 's|/[^/]*$||' | LC_ALL=C sort -u || true)
  STATE["$k"]=$k
  while IFS= read -r p; do
    [ -z "$p" ] || PKG["$k|$p"]=1
  done < <(grep -E '^[^/]+/jsr\.json$' <<<"$files" | sed 's|/jsr\.json$||' || true)
  while IFS= read -r l; do
    [ -n "$l" ] || continue
    ! grep -qxF -- "$l/web/jsr.json" <<<"$files" || PKG["$k|$l/web"]=1
    [ -n "${ASKED[$l]:-}" ] || continue
    if go_new "$k" "$l/go.mod"; then NEWVENDOR["$k|$l"]=1; fi
  done <<<"$LANES"
  if $ZIP_ROOT && [ -n "${ASKED["."]:-}" ] && go_new "$k" go.mod; then NEWVENDOR["$k|."]=1; fi
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    MOD["$k|$m"]=1
    [ -z "${IS_LANE[$m]:-}" ] || continue
    # A go.mod in a TS package (<lane>/web, or a root subpackage outside every
    # lane) only keeps Go tooling out: the package ships its tree.
    if l=$(lane_holding "$m"); then
      [ -z "${PKG["$k|$l/web"]:-}" ] || [[ $m != "$l/web" && $m != "$l/web/"* ]] || continue
      NEST["$k|$m"]=1
      continue
    fi
    # A lane and a go root are module zips; a docker or ts root ships its whole tree.
    if $ZIP_ROOT && [ -z "${PKG["$k|${m%%/*}"]:-}" ]; then
      NEST["$k|$m"]=1
    fi
  done <<<"$dirs"
}

if [ "$cmd" = boundaries ]; then
  load_state "$2"
  for key in "${!NEST[@]}"; do
    printf '%s\n' "${key#*|}"
  done | cat - <(printf '%s\n' "$LANES") | sed '/^$/d' | LC_ALL=C sort -u
  exit 0
fi

ships() { # <state> <unit> <path> <mode> [same], <path> one <unit> owns there: status 0 when <unit>'s artifact holds it; a non-empty same marks a change of mode alone
  local rel=$3 pkg=${3%%/*}
  if [ "$2" = . ]; then
    $ZIP_ROOT || return 0
  else
    rel=${3#"$2"/} pkg=$2/web
  fi
  [[ -z ${PKG["$1|$pkg"]:-} || $3 != "$pkg/"* ]] || return 0
  # golang.org/x/mod/zip keeps no mode and leaves out a symlink, a gitlink,
  # .hg_archival.txt and a vendored package (isVendoredPackage): from go 1.24 vendor/modules.txt
  # too, but not a file directly in a nested <dir>/vendor/ (golang.org/issue/37397).
  [[ $4 == 100* && -z ${5:-} && $rel != .hg_archival.txt ]] || return 1
  if [ -n "${NEWVENDOR["$1|$2"]:-}" ]; then
    case $rel in
      vendor/modules.txt | vendor/*/* | */vendor/*/*) return 1 ;;
    esac
  else
    case $rel in
      vendor/*/* | */vendor/*) return 1 ;;
    esac
  fi
}
OWN=()
owners_in() { # <state> <path> <mode> [same]: OWN gets the units whose artifact holds <path> in that state's tree
  local k=$1 d=$2 key u root=true
  local -a found=()
  while [[ $d == */* ]]; do
    d=${d%/*}
    key="$k|$d"
    if [ -n "${IS_LANE[$d]:-}" ]; then
      found+=("$d")
      # A go root's zip holds a lane's files until the lane has its go.mod.
      if ! $ZIP_ROOT || [ -n "${MOD[$key]:-}" ]; then
        root=false
        break
      fi
    elif [ -n "${NEST[$key]:-}" ]; then
      root=false
      break
    fi
  done
  ! $root || found+=(.)
  OWN=()
  for u in "${found[@]}"; do
    ! ships "$k" "$u" "$2" "$3" "${4:-}" || OWN+=("$u")
  done
}

# walk: each non-merge commit of <from>..<to> and each path it changes; a path
# counts for the units holding it in each tree holding it, a regular go.mod
# added or removed for those also holding a file under its dir without it.
#   commits <from> <to> <unit>: "<sha>\t<path>" per path <unit> owns so; an empty
#   or non-commit <from> (half_changed's empty tree) walks all of <to>.
#   paths <from> <to> <unit>...: "<unit>\t<path>" per such path on stdin; one no
#   commit changes counts at <from>'s tree and <to>'s; an empty <from> walks
#   nothing and judges every path at <to>'s tree.
FROM=$2 TO=$3
shift 3
declare -A WANT=() COVERED=() SEEN=()
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
emit() { # <state> <sha> <path> <mode> [same]: prints the asked units holding <path> in that state
  owners_in "$1" "$3" "$4" "${5:-}"
  emit_units "$2" "$3" "${OWN[@]}"
}
emit_units() { # <sha> <path> <unit>...: prints the asked units among them for <path>
  local sha=$1 f=$2 u key
  shift 2
  for u in "$@"; do
    [ -n "${ASKED[$u]:-}" ] || continue
    key="$u|$f"
    [ "$cmd" = paths ] || key="$sha|$f"
    [ -z "${SEEN[$key]:-}" ] || continue
    SEEN["$key"]=1
    if [ "$cmd" = commits ]; then
      printf '%s\t%s\n' "$sha" "$f"
    else
      printf '%s\t%s\n' "$u" "$f"
    fi
  done
}
CARVED=()
carved() { # <state> <rev> <dir>: CARVED gets the asked units holding <dir>/go.mod in that state and a file under <dir> at <rev>
  local files meta f u n=0
  local -a mods=()
  local -A want=() held=()
  CARVED=()
  owners_in "$1" "$3/go.mod" 100644
  for u in "${OWN[@]}"; do
    [ -z "${ASKED[$u]:-}" ] || mods+=("$u")
  done
  [ "${#mods[@]}" -gt 0 ] || return 0
  for u in "${mods[@]}"; do
    want["$u"]=1
  done
  files=$(git -c core.quotePath=false ls-tree -r "$2" -- "$3/") || die "cannot list ${3}/ at $2"
  while IFS=$'\t' read -r meta f; do
    [ -n "$f" ] || continue
    owners_in "$1" "$f" "${meta%% *}"
    for u in "${OWN[@]}"; do
      [[ -n ${want[$u]:-} && -z ${held[$u]:-} ]] || continue
      held["$u"]=1
      n=$((n + 1))
    done
    [ "$n" -lt "${#mods[@]}" ] || break
  done <<<"$files"
  for u in "${mods[@]}"; do
    [ -z "${held[$u]:-}" ] || CARVED+=("$u")
  done
}
commit_done() { # SHA PARENT CHANGES: emits one commit's paths
  local pk=- ck meta om nm oo no st f same
  [ -n "$SHA" ] || return 0
  if [ -n "$PARENT" ]; then
    state_of "$PARENT"
    pk=$K
  fi
  # Only a go.mod or a jsr.json moves an owner: else the parent's state holds.
  if grep -qiE $'[\t/](go\\.mod|jsr\\.json)$' <<<"$CHANGES"; then
    state_of "$SHA"
    ck=$K
  else
    STATE["$SHA"]=$pk
    ck=$pk
  fi
  while IFS=$'\t' read -r meta f; do
    [ -n "$f" ] || continue
    if [ "$cmd" = paths ]; then
      [ -n "${WANT[$f]:-}" ] || continue
      COVERED["$f"]=1
    fi
    read -r om nm oo no st <<<"${meta#:}"
    same=""
    [[ $om != 100* || $nm != 100* || $oo != "$no" ]] || same=1
    [ "$st" = A ] || emit "$pk" "$SHA" "$f" "$om" "$same"
    [ "$st" = D ] || emit "$ck" "$SHA" "$f" "$nm" "$same"
    [[ -n $PARENT && ${f,,} == */go.mod ]] || continue
    CARVED=()
    if [[ $nm == 100* && $om != 100* ]]; then
      carved "$pk" "$PARENT" "${f%/*}"
    elif [[ $om == 100* && $nm != 100* ]]; then
      carved "$ck" "$SHA" "${f%/*}"
    fi
    emit_units "$SHA" "$f" "${CARVED[@]}"
  done <<<"$CHANGES"
}

if [ "$cmd" = commits ] || [ -n "$FROM" ]; then
  RANGE=("$TO")
  if [ -n "$FROM" ] && git rev-parse --verify --quiet "${FROM}^{commit}" >/dev/null; then
    RANGE=("${FROM}..${TO}")
  fi
  LOG=$(git -c core.quotePath=false log --no-merges --root --no-renames --reverse --topo-order \
    --format='%x01%H %P' --raw "${RANGE[@]}" --) || die "cannot walk ${RANGE[*]}"
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
  declare -A ODD=()
  for end in "${ENDS[@]}"; do
    state_of "$end"
    [ "${#COVERED[@]}" -lt "${#WANT[@]}" ] || continue
    ODD=()
    TREE=$(tree_of "$end") || exit 2
    while IFS=$'\t' read -r meta f; do
      ODD["$f"]=${meta%% *}
    done < <(grep -Ev '^100' <<<"$TREE" || true)
    for f in "${!WANT[@]}"; do
      [ -z "${COVERED[$f]:-}" ] || continue
      [ -z "$FROM" ] || git cat-file -e "${end}:${f}" 2>/dev/null || continue
      emit "$K" - "$f" "${ODD[$f]:-100644}"
    done
  done
fi
