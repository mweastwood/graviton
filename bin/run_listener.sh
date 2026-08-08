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

if ! command -v smee &>/dev/null; then
  echo "Error: 'smee' CLI tool not found. Install it using: npm install -g smee-client"
  exit 1
fi

echo "Starting smee webhook listener relaying to http://localhost:${PORT}/..."
smee --url "${SMEE_URL}" --path / --port "${PORT}"
