#!/usr/bin/env bash
set -euo pipefail

# Helper script to launch smee.io webhook proxy listener for local Graviton debugging.
# Usage: ./bin/run_listener.sh <SMEE_URL> [TARGET_PORT]

SMEE_URL="${1:-}"
PORT="${2:-8000}"

if [ -z "${SMEE_URL}" ]; then
  echo "Usage: $0 <SMEE_URL> [TARGET_PORT]"
  echo "Example: $0 https://smee.io/your-channel-id 8000"
  exit 1
fi

SMEE_CMD=""
if command -v smee &>/dev/null; then
  SMEE_CMD="smee"
elif [ -x "${HOME}/.npm-global/bin/smee" ]; then
  SMEE_CMD="${HOME}/.npm-global/bin/smee"
elif command -v npx &>/dev/null; then
  SMEE_CMD="npx smee"
else
  echo "Error: 'smee' CLI tool not found. Install it using: npm install -g smee-client"
  exit 1
fi

exec ${SMEE_CMD} --url "${SMEE_URL}" --path / --port "${PORT}"
