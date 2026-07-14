#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
#  Docker Hub helper for mikayelbag/rtsp-streamer
#
#  Pushes the README as the repo overview, lists tags, and prunes old
#  (non-functional) tags — everything via the Docker Hub API, no GitHub.
#
#  Auth (EXPORTED env vars — a plain shell variable is not enough):
#    bash/zsh:  export DOCKERHUB_USER=mikayelbag
#               export DOCKERHUB_TOKEN=<access token>       # Account → Security
#    fish:      set -x DOCKERHUB_USER mikayelbag
#               set -x DOCKERHUB_TOKEN <access token>       # -x = export!
#      (a Personal Access Token; required if 2FA is on — a raw password
#       will be rejected. The same token works for `docker login`.
#       No sudo needed — this script only talks to the Docker Hub API.)
#
#  Usage:
#    ./dockerhub.sh list-tags
#    ./dockerhub.sh push-readme
#    ./dockerhub.sh prune-tags                 # keeps only latest
#    ./dockerhub.sh prune-tags latest 2.9.0    # keep an explicit set
#    ./dockerhub.sh prune-tags --all           # delete EVERY tag (repush after!)
# ════════════════════════════════════════════════════════════════════
set -euo pipefail

REPO="${DOCKERHUB_REPO:-mikayelbag/rtsp-streamer}"
NAMESPACE="${REPO%%/*}"
NAME="${REPO##*/}"
API="https://hub.docker.com/v2"
README_FILE="${README_FILE:-$(dirname "$0")/README.md}"
# Docker Hub caps the short description at 100 chars (longer -> HTTP 400).
SHORT_DESC="${SHORT_DESC:-Looping live RTSP streams from a folder of videos — benchmark video-analytics engines.}"

die() { echo "error: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }
need curl
need python3

login() {
  : "${DOCKERHUB_USER:?not exported — bash: export DOCKERHUB_USER=...  fish: set -x DOCKERHUB_USER ...}"
  : "${DOCKERHUB_TOKEN:?not exported — bash: export DOCKERHUB_TOKEN=...  fish: set -x DOCKERHUB_TOKEN ... (a Docker Hub access token)}"
  curl -fsS -H "Content-Type: application/json" \
    -d "{\"username\":\"${DOCKERHUB_USER}\",\"password\":\"${DOCKERHUB_TOKEN}\"}" \
    "${API}/users/login/" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' \
    || die "login failed (check DOCKERHUB_USER / DOCKERHUB_TOKEN; use a PAT if 2FA is on)"
}

all_tags() {
  local token="$1" url="${API}/repositories/${NAMESPACE}/${NAME}/tags/?page_size=100"
  # follow pagination so repos with >100 tags are fully covered
  while [ -n "$url" ] && [ "$url" != "null" ]; do
    local page
    page=$(curl -fsS -H "Authorization: JWT ${token}" "$url")
    printf '%s' "$page" | python3 -c 'import sys,json;[print(t["name"]) for t in json.load(sys.stdin)["results"]]'
    url=$(printf '%s' "$page" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("next") or "")')
  done
}

cmd_list_tags() {
  all_tags "$(login)"
}

cmd_push_readme() {
  local token body
  token=$(login)
  [ -f "$README_FILE" ] || die "README not found: $README_FILE"
  body=$(README_FILE="$README_FILE" SHORT_DESC="$SHORT_DESC" python3 - <<'PY'
import json, os
full = open(os.environ["README_FILE"], encoding="utf-8").read()
desc = os.environ["SHORT_DESC"][:100]   # Docker Hub hard limit; over -> HTTP 400
print(json.dumps({"full_description": full, "description": desc}))
PY
)
  local resp code payload
  resp=$(curl -sS -w $'\n%{http_code}' -X PATCH \
    -H "Authorization: JWT ${token}" \
    -H "Content-Type: application/json" \
    -d "$body" \
    "${API}/repositories/${NAMESPACE}/${NAME}/")
  code=${resp##*$'\n'}
  payload=${resp%$'\n'*}
  if [ "$code" -lt 200 ] || [ "$code" -ge 300 ]; then
    die "push-readme failed (HTTP ${code}): ${payload}"
  fi
  echo "pushed README + short description to ${REPO}"
}

cmd_prune_tags() {
  local token keep t
  token=$(login)
  if [ "${1:-}" = "--all" ]; then
    keep="  "                     # empty keep set: wipe every tag
    echo "keeping: nothing (--all) — repush when done"
  else
    keep=" ${*:-latest} "
    echo "keeping:${keep}"
  fi
  for t in $(all_tags "$token"); do
    case "$keep" in
      *" $t "*) echo "keep    $t"; continue ;;
    esac
    printf 'delete  %s ... ' "$t"
    if curl -fsS -o /dev/null -X DELETE -H "Authorization: JWT ${token}" \
        "${API}/repositories/${NAMESPACE}/${NAME}/tags/${t}/"; then
      echo "ok"
    else
      echo "FAILED"
    fi
  done
}

case "${1:-}" in
  list-tags)   shift; cmd_list_tags "$@" ;;
  push-readme) shift; cmd_push_readme "$@" ;;
  prune-tags)  shift; cmd_prune_tags "$@" ;;
  *) cat >&2 <<EOF
usage: $0 <command>
  list-tags                 list every tag on ${REPO}
  push-readme               set the repo overview from ${README_FILE}
  prune-tags [keep...]      delete all tags except the keep set (default: latest)
  prune-tags --all          delete every tag, keep nothing (repush after)
EOF
     exit 1 ;;
esac
