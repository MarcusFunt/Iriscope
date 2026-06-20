#!/usr/bin/env bash
set -uo pipefail

ENV_FILE="/etc/default/iriscope-boot-update"
LOCK_FILE="/run/iriscope-boot-update.lock"
FALLBACK_PACKAGE_LIST="/usr/local/share/iriscope/apt-packages.txt"

IRISCOPE_REPO_URL="${IRISCOPE_REPO_URL:-https://github.com/MarcusFunt/Iriscope.git}"
IRISCOPE_BRANCH="${IRISCOPE_BRANCH:-main}"
IRISCOPE_APP_ROOT="${IRISCOPE_APP_ROOT:-/opt/iriscope/app}"
IRISCOPE_TARGET_USER="${IRISCOPE_TARGET_USER:-pi}"

log() {
  printf '[iriscope-boot-update] %s\n' "$*"
}

load_environment() {
  if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
  fi
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    log "Run this updater as root."
    return 1
  fi
}

validate_config() {
  if [[ -z "${IRISCOPE_REPO_URL}" ]]; then
    log "IRISCOPE_REPO_URL is empty."
    return 1
  fi
  if [[ -z "${IRISCOPE_BRANCH}" ]]; then
    log "IRISCOPE_BRANCH is empty."
    return 1
  fi
  if [[ -z "${IRISCOPE_APP_ROOT}" || "${IRISCOPE_APP_ROOT}" == "/" ]]; then
    log "IRISCOPE_APP_ROOT must not be empty or /."
    return 1
  fi
}

acquire_lock() {
  exec 9>"${LOCK_FILE}"
  if ! flock -n 9; then
    log "Another update is already running; skipping."
    return 1
  fi
}

clone_repo() {
  local app_parent
  local clone_dir
  local backup_dir

  app_parent="$(dirname "${IRISCOPE_APP_ROOT}")"
  install -d -m 0755 "${app_parent}" || return 1

  clone_dir="$(mktemp -d "${app_parent}/.iriscope-clone.XXXXXX")" || return 1
  if ! git clone --branch "${IRISCOPE_BRANCH}" --single-branch "${IRISCOPE_REPO_URL}" "${clone_dir}"; then
    rm -rf "${clone_dir}"
    return 1
  fi

  if [[ -e "${IRISCOPE_APP_ROOT}" ]]; then
    backup_dir="${IRISCOPE_APP_ROOT}.bak.$(date -u +%Y%m%d%H%M%S)"
    log "Moving non-git app root aside to ${backup_dir}."
    if ! mv "${IRISCOPE_APP_ROOT}" "${backup_dir}"; then
      rm -rf "${clone_dir}"
      return 1
    fi
  fi

  mv "${clone_dir}" "${IRISCOPE_APP_ROOT}" || return 1
}

reset_checkout() {
  git -C "${IRISCOPE_APP_ROOT}" checkout -B "${IRISCOPE_BRANCH}" "origin/${IRISCOPE_BRANCH}" || return 1
  git -C "${IRISCOPE_APP_ROOT}" reset --hard "origin/${IRISCOPE_BRANCH}" || return 1
  git -C "${IRISCOPE_APP_ROOT}" clean -ffd || return 1
  log "Repository is current at $(git -C "${IRISCOPE_APP_ROOT}" rev-parse --short HEAD)."
}

sync_repo() {
  if [[ -d "${IRISCOPE_APP_ROOT}/.git" ]]; then
    log "Fetching ${IRISCOPE_BRANCH} from ${IRISCOPE_REPO_URL}."
    git -C "${IRISCOPE_APP_ROOT}" remote set-url origin "${IRISCOPE_REPO_URL}" || return 1
    if ! git -C "${IRISCOPE_APP_ROOT}" fetch --prune origin "${IRISCOPE_BRANCH}"; then
      log "Fetch failed; trying the last fetched origin/${IRISCOPE_BRANCH}."
      if git -C "${IRISCOPE_APP_ROOT}" rev-parse --verify --quiet "origin/${IRISCOPE_BRANCH}" >/dev/null; then
        reset_checkout || return 1
      fi
      return 1
    fi
  else
    log "Cloning ${IRISCOPE_REPO_URL} (${IRISCOPE_BRANCH}) into ${IRISCOPE_APP_ROOT}."
    clone_repo || return 1
  fi

  reset_checkout
}

package_list_path() {
  local repo_package_list="${IRISCOPE_APP_ROOT}/provision/pi-zero-w/apt-packages.txt"
  if [[ -f "${repo_package_list}" ]]; then
    printf '%s\n' "${repo_package_list}"
    return 0
  fi
  if [[ -f "${FALLBACK_PACKAGE_LIST}" ]]; then
    printf '%s\n' "${FALLBACK_PACKAGE_LIST}"
    return 0
  fi
  return 1
}

install_packages() {
  local package_file
  local packages=()

  if ! package_file="$(package_list_path)"; then
    log "No apt package list found; skipping package install."
    return 0
  fi

  mapfile -t packages < <(sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^$/d' "${package_file}")
  if [[ "${#packages[@]}" -eq 0 ]]; then
    log "Apt package list is empty; skipping package install."
    return 0
  fi

  log "Installing/updating Pi capture packages from ${package_file}."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update || return 1
  apt-get install -y "${packages[@]}" || return 1
}

main() {
  load_environment
  require_root || return 1
  validate_config || return 1
  acquire_lock || return 0

  if ! sync_repo; then
    log "Repository update failed; leaving the existing checkout in place."
  fi

  if ! install_packages; then
    log "Package update failed; keeping the current installed packages."
  fi

  log "Boot update finished."
  return 0
}

main "$@"
