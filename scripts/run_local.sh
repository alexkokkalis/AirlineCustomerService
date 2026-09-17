#!/usr/bin/env bash
# Start the local FastAPI service, development dashboard, and ngrok tunnel.
# Stop everything started here with Ctrl-C.

set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python}"
UVICORN_BIN="${UVICORN_BIN:-${PROJECT_ROOT}/.venv/bin/uvicorn}"
NGROK_BIN="${NGROK_BIN:-ngrok}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
NGROK_DOMAIN="${NGROK_DOMAIN:-}"

api_pid=""
ngrok_pid=""

cleanup() {
  trap - EXIT INT TERM
  echo ""
  echo "Stopping local services…"
  if [[ -n "${ngrok_pid}" ]] && kill -0 "${ngrok_pid}" 2>/dev/null; then
    kill "${ngrok_pid}" 2>/dev/null || true
  fi
  if [[ -n "${api_pid}" ]] && kill -0 "${api_pid}" 2>/dev/null; then
    kill "${api_pid}" 2>/dev/null || true
  fi
  wait "${ngrok_pid:-}" 2>/dev/null || true
  wait "${api_pid:-}" 2>/dev/null || true
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

if [[ ! -x "${PYTHON_BIN}" || ! -x "${UVICORN_BIN}" ]]; then
  echo "Project virtual environment is unavailable. Run the setup commands in README.md first." >&2
  exit 1
fi
require_command "${NGROK_BIN}"
require_command curl

cd "${PROJECT_ROOT}"
if [[ -z "${NGROK_DOMAIN}" ]]; then
  NGROK_DOMAIN="$("${PYTHON_BIN}" -c 'from app.config import NGROK_DOMAIN; print(NGROK_DOMAIN or "")')"
fi
if [[ ! -f data/ionian_airlines.db ]]; then
  echo "Initialising the sandbox database…"
  "${PYTHON_BIN}" scripts/init_database.py
fi

trap cleanup EXIT INT TERM

"${UVICORN_BIN}" app.ionian_api:app --host "${HOST}" --port "${PORT}" --reload &
api_pid=$!

echo "Waiting for the local API…"
for _ in {1..40}; do
  if curl --silent --fail "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
if ! curl --silent --fail "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
  echo "The FastAPI service did not become healthy on port ${PORT}." >&2
  exit 1
fi

ngrok_args=(http "${PORT}" --log=stdout)
if [[ -n "${NGROK_DOMAIN}" ]]; then
  ngrok_args+=(--url "https://${NGROK_DOMAIN}")
fi
"${NGROK_BIN}" "${ngrok_args[@]}" &
ngrok_pid=$!

echo ""
echo "Ionian Airlines local environment is running:"
echo "  Dashboard: http://${HOST}:${PORT}/dashboard"
echo "  API docs:  http://${HOST}:${PORT}/docs"
echo "  ngrok UI:  http://127.0.0.1:4040"
echo ""
echo "Open the dashboard, select a scenario, and press Run."
if [[ -n "${NGROK_DOMAIN}" ]]; then
  echo "Webhook tunnel: https://${NGROK_DOMAIN}"
else
  echo "No static ngrok domain is configured; this tunnel URL is temporary."
  echo "Reserve one in ngrok and set NGROK_DOMAIN in app/config.py to keep ElevenLabs webhook URLs stable."
fi
echo "Press Ctrl-C to stop Uvicorn and ngrok."

wait "${api_pid}"
