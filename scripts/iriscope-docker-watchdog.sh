#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${IRISCOPE_REPO_DIR:-$(pwd)}"
REPO_URL="${IRISCOPE_REPO_URL:-}"
BRANCH="${IRISCOPE_BRANCH:-main}"
INTERVAL_S="${IRISCOPE_WATCHDOG_INTERVAL_S:-300}"
SERVICE="${IRISCOPE_DOCKER_SERVICE:-iriscope-host}"
ONCE=0

log() {
  printf '[iriscope-docker-watchdog] %s\n' "$*"
}

usage() {
  cat <<'EOF'
Usage: iriscope-docker-watchdog.sh [--once]

Checks GitHub for the configured branch. When the remote commit differs from
the local checkout, the watchdog resets the checkout to GitHub and rebuilds /
restarts the Docker Compose host service.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --once)
      ONCE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "${INTERVAL_S}" =~ ^[0-9]+$ ]] || [[ "${INTERVAL_S}" -lt 10 ]]; then
  INTERVAL_S=300
fi

if [[ -z "${REPO_URL}" ]]; then
  REPO_URL="$(git -C "${REPO_DIR}" config --get remote.origin.url || true)"
fi
if [[ -z "${REPO_URL}" ]]; then
  REPO_URL="https://github.com/MarcusFunt/Iriscope.git"
fi

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    log "Docker Compose is not available."
    return 1
  fi
}

remote_commit() {
  git ls-remote "${REPO_URL}" "refs/heads/${BRANCH}" | awk '{print $1}'
}

local_commit() {
  git -C "${REPO_DIR}" rev-parse HEAD
}

update_and_restart() {
  local remote_sha="$1"

  log "Updating ${REPO_DIR} to ${remote_sha}."
  git -C "${REPO_DIR}" remote set-url origin "${REPO_URL}"
  git -C "${REPO_DIR}" fetch --prune origin "${BRANCH}"
  git -C "${REPO_DIR}" checkout -B "${BRANCH}" "origin/${BRANCH}"
  git -C "${REPO_DIR}" reset --hard "origin/${BRANCH}"
  git -C "${REPO_DIR}" clean -ffd

  log "Rebuilding and restarting Docker service ${SERVICE}."
  compose -f "${REPO_DIR}/docker-compose.yml" up -d --build --force-recreate "${SERVICE}"
}

check_once() {
  local remote_sha
  local local_sha

  remote_sha="$(remote_commit)"
  if [[ -z "${remote_sha}" ]]; then
    log "Could not resolve ${REPO_URL} ${BRANCH}; keeping current container."
    return 0
  fi

  local_sha="$(local_commit)"
  if [[ "${local_sha}" == "${remote_sha}" ]]; then
    log "Already current at ${local_sha:0:12}."
    return 0
  fi

  log "Local ${local_sha:0:12} differs from GitHub ${remote_sha:0:12}."
  update_and_restart "${remote_sha}"
}

main() {
  cd "${REPO_DIR}"
  while true; do
    check_once || log "Check failed; will retry."
    if [[ "${ONCE}" -eq 1 ]]; then
      break
    fi
    sleep "${INTERVAL_S}"
  done
}

main "$@"
