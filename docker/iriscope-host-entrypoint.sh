#!/usr/bin/env sh
set -eu

if [ -n "${IRISCOPE_PI_SSH_KEY:-}" ] && [ -f "${IRISCOPE_PI_SSH_KEY}" ]; then
  install -d -m 0700 /run/iriscope
  cp "${IRISCOPE_PI_SSH_KEY}" /run/iriscope/ssh_key
  chmod 0600 /run/iriscope/ssh_key
  export IRISCOPE_PI_SSH_KEY=/run/iriscope/ssh_key
fi

exec "$@"
