#!/bin/sh
# ════════════════════════════════════════════════════════════════════
#  RTSP Streamer — container entrypoint
#
#  Seeds a default config into the mounted /app/config directory on the
#  first run, so a fresh (empty) ./config volume comes up working instead
#  of crashing on a missing file. The shipped default lives at
#  /app/defaults/config.ini, which a bind-mounted /app/config cannot hide.
# ════════════════════════════════════════════════════════════════════
set -eu

CONFIG_DIR="${CONFIG_DIR:-/app/config}"
CONFIG_FILE="${CONFIG_FILE:-${CONFIG_DIR}/config.ini}"
DEFAULT_CONFIG="${DEFAULT_CONFIG:-/app/defaults/config.ini}"

mkdir -p "${CONFIG_DIR}"

if [ ! -f "${CONFIG_FILE}" ]; then
  if [ -f "${DEFAULT_CONFIG}" ]; then
    cp "${DEFAULT_CONFIG}" "${CONFIG_FILE}"
    echo "[entrypoint] no config found — seeded default at ${CONFIG_FILE}"
    echo "[entrypoint] edit it on the host and restart to customise."
  else
    echo "[entrypoint] FATAL: no config at ${CONFIG_FILE} and no default to seed at ${DEFAULT_CONFIG}" >&2
    exit 2
  fi
else
  echo "[entrypoint] using config ${CONFIG_FILE}"
fi

# ptyrun gives streamer.py its own pseudo-terminal, so the prod loader
# animation works under `docker compose up` with no `tty:` in the user's
# compose file. It execs directly when a real terminal is already attached
# (docker run -it) and forwards SIGTERM for graceful `docker stop`.
exec python3 /app/ptyrun.py python3 /app/streamer.py --config "${CONFIG_FILE}" "$@"
