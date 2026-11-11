#!/usr/bin/env bash
# Run development, freeze, formal repeats, and analysis in an isolated workspace.
# Execute this file as a child process; never source it into an interactive shell.

if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
  printf '%s\n' \
    'Do not source this script. Run: bash scripts/run_full_experiment.sh' >&2
  return 2
fi

set -Eeuo pipefail
set +x

PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
RUN_ROOT=${INDEX_TUNING_RUN_ROOT:-"$(dirname -- "$PROJECT_ROOT")"}
RUN_DIR=${INDEX_TUNING_RUN_DIR:-"$RUN_ROOT/IndexTuningAgent-run-$(date +%Y%m%d-%H%M%S)"}
CURRENT_STAGE=setup

report_failure() {
  local status=$?
  trap - ERR
  {
    printf '\n%s\n' '完整实验失败，但当前终端不会退出。'
    printf '失败阶段: %s\n' "$CURRENT_STAGE"
    printf '退出码: %s\n' "$status"
    printf '实验目录: %s\n' "$RUN_DIR"
    printf '%s\n' '已保留数据库、事件日志和分析产物。'
    if [[ -f "$RUN_DIR/artifacts/formal/progress.log" ]]; then
      printf '%s\n' '正式实验日志末尾:'
      tail -n 30 "$RUN_DIR/artifacts/formal/progress.log"
    elif [[ -f "$RUN_DIR/artifacts/development/progress.log" ]]; then
      printf '%s\n' '开发实验日志末尾:'
      tail -n 30 "$RUN_DIR/artifacts/development/progress.log"
    fi
  } >&2
  if [[ -d "$RUN_DIR/artifacts" ]]; then
    printf 'status=failed\nstage=%s\nexit_code=%s\n' \
      "$CURRENT_STAGE" "$status" > "$RUN_DIR/artifacts/run-status.txt"
  fi
  exit "$status"
}
trap report_failure ERR

if [[ -e "$RUN_DIR" ]]; then
  printf 'Refusing existing run directory: %s\n' "$RUN_DIR" >&2
  exit 1
fi

CURRENT_STAGE=copy_workspace
mkdir -p "$RUN_DIR"
rsync -a \
  --exclude='.venv/' \
  --exclude='.local/' \
  --exclude='.env' \
  --exclude='artifacts/' \
  --exclude='results/' \
  --exclude='dist/' \
  --exclude='__pycache__/' \
  "$PROJECT_ROOT/" "$RUN_DIR/"

cd "$RUN_DIR"
source "$PROJECT_ROOT/.venv/bin/activate"
export PYTHONPATH="$RUN_DIR/src"

if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  printf '%s\n' 'Missing local .env; copy .env.example to .env and configure it first.' >&2
  exit 2
fi
set -a
source "$PROJECT_ROOT/.env"
set +a
: "${MODEL_API_KEY:?MODEL_API_KEY 未配置}"
: "${MODEL_PROXY_URL:?MODEL_PROXY_URL 未配置}"
: "${MODEL_MIN_REQUEST_INTERVAL_SECONDS:?模型请求间隔未配置}"

export AGENT_PG_BIN="$PROJECT_ROOT/.local/postgresql16/bin"
export PG_DSN="host=$RUN_DIR/.local/pg16-socket port=55432 dbname=index_agent user=agent"

mkdir -p artifacts/development artifacts/formal artifacts/results

CURRENT_STAGE=start_postgresql
bash scripts/bootstrap_postgres.sh start

CURRENT_STAGE=initialize
python -m pg_tuning manifests
python -m pg_tuning init

CURRENT_STAGE=development_run
python -m pg_tuning run \
  --mode dev \
  --output artifacts/development/events.jsonl \
  2>&1 | tee artifacts/development/progress.log

CURRENT_STAGE=development_analysis
python -m pg_tuning analyze \
  --log artifacts/development/events.jsonl \
  --output artifacts/development/analysis

CURRENT_STAGE=freeze
python -m pg_tuning freeze \
  --development-log artifacts/development/events.jsonl \
  --output artifacts/freeze.json

CURRENT_STAGE=formal_run
python -m pg_tuning run \
  --mode formal \
  --freeze artifacts/freeze.json \
  --output artifacts/formal/events.jsonl \
  2>&1 | tee artifacts/formal/progress.log

CURRENT_STAGE=formal_analysis
python -m pg_tuning analyze \
  --log artifacts/formal/events.jsonl \
  --output artifacts/results

cat artifacts/results/integrity.json
cat artifacts/results/formal_results.csv
cat artifacts/results/cost_adjusted_results.csv
cat artifacts/results/costs.csv

printf 'status=completed\nstage=complete\nexit_code=0\n' > artifacts/run-status.txt
printf '\n完整实验结束: %s\n' "$RUN_DIR"
