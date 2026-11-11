#!/usr/bin/env bash
# Private PostgreSQL 16 cluster. Never uses or resets another cluster.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PG_BIN=${AGENT_PG_BIN:-"$ROOT/.local/postgresql16/bin"}
if [[ ! -x "$PG_BIN/postgres" ]] && [[ -x /usr/lib/postgresql/16/bin/postgres ]]; then
  PG_BIN=/usr/lib/postgresql/16/bin
fi
if [[ ! -x "$PG_BIN/postgres" ]] || ! "$PG_BIN/postgres" --version | grep -q 'PostgreSQL) 16\.'; then
  printf '%s\n' 'PostgreSQL 16 missing: run scripts/build_postgres16.sh, set AGENT_PG_BIN, or use docker compose.' >&2
  exit 1
fi
CLUSTER="$ROOT/.local/pg16"
SOCKET="$ROOT/.local/pg16-socket"
MARKER="$ROOT/.local/pg16-owned-by-index-agent"
PORT=55432
REQUESTED_SHARED_BUFFERS=${AGENT_PG_SHARED_BUFFERS:-}
if [[ -n "$REQUESTED_SHARED_BUFFERS" && ! "$REQUESTED_SHARED_BUFFERS" =~ ^[1-9][0-9]*(kB|MB|GB)$ ]]; then
  printf '%s\n' 'AGENT_PG_SHARED_BUFFERS must look like 4MB, 128MB, or 1GB' >&2
  exit 2
fi
mkdir -p "$ROOT/.local"
if [[ -e "$CLUSTER" && ! -f "$MARKER" ]]; then
  printf '%s\n' "Refusing unmarked existing directory: $CLUSTER" >&2
  exit 1
fi
if [[ ${1:-start} == stop ]]; then
  [[ -f "$MARKER" ]] || { printf '%s\n' 'No owned cluster'; exit 1; }
  "$PG_BIN/pg_ctl" -D "$CLUSTER" stop -m fast
  exit
fi
if [[ ${1:-start} != start ]]; then
  printf '%s\n' 'Usage: scripts/bootstrap_postgres.sh [start|stop]' >&2
  exit 2
fi
mkdir -p "$SOCKET"
chmod 700 "$SOCKET"
if [[ ! -f "$CLUSTER/PG_VERSION" ]]; then
  "$PG_BIN/initdb" -D "$CLUSTER" -U agent --encoding=UTF8 --locale=C --auth-local=trust --auth-host=reject
  printf '%s\n' 'Dedicated index tuning agent cluster; preserve this data across experiments.' > "$MARKER"
  cat >> "$CLUSTER/postgresql.conf" <<EOF
# Isolation only: query planner and performance settings remain the defaults.
listen_addresses = ''
port = $PORT
unix_socket_directories = '$SOCKET'
unix_socket_permissions = 0700
EOF
  if [[ -n "$REQUESTED_SHARED_BUFFERS" ]]; then
    printf "shared_buffers = '%s'\n" "$REQUESTED_SHARED_BUFFERS" >> "$CLUSTER/postgresql.conf"
  fi
fi
[[ $(cat "$CLUSTER/PG_VERSION") == 16 ]] || { printf '%s\n' 'Wrong cluster version'; exit 1; }
if ! "$PG_BIN/pg_ctl" -D "$CLUSTER" status >/dev/null 2>&1; then
  "$PG_BIN/pg_ctl" -D "$CLUSTER" -l "$ROOT/.local/pg16-server.log" -w start
fi
if [[ -n "$REQUESTED_SHARED_BUFFERS" ]]; then
  ACTUAL_SHARED_BUFFERS=$("$PG_BIN/psql" -X -h "$SOCKET" -p "$PORT" -U agent -d postgres -Atc 'SHOW shared_buffers')
  if [[ "$ACTUAL_SHARED_BUFFERS" != "$REQUESTED_SHARED_BUFFERS" ]]; then
    printf 'Requested shared_buffers=%s but PostgreSQL reports %s\n' \
      "$REQUESTED_SHARED_BUFFERS" "$ACTUAL_SHARED_BUFFERS" >&2
    exit 1
  fi
fi
for db in index_agent index_agent_test; do
  if [[ $("$PG_BIN/psql" -X -h "$SOCKET" -p "$PORT" -U agent -d postgres -Atc "SELECT count(*) FROM pg_database WHERE datname = '$db'") == 0 ]]; then
    "$PG_BIN/createdb" -h "$SOCKET" -p "$PORT" -U agent "$db"
  fi
done
printf 'Dedicated cluster ready. DSN: host=%s port=%s dbname=index_agent user=agent\n' "$SOCKET" "$PORT"
printf 'Integration-test DSN: host=%s port=%s dbname=index_agent_test user=agent\n' "$SOCKET" "$PORT"
printf 'shared_buffers: %s\n' "$("$PG_BIN/psql" -X -h "$SOCKET" -p "$PORT" -U agent -d postgres -Atc 'SHOW shared_buffers')"
