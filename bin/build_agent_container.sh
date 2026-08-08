#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${1:-antigravity-agent:latest}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "Building Antigravity Agent Docker container image: ${IMAGE_NAME}..."
docker build -t "${IMAGE_NAME}" -f "${REPO_ROOT}/Dockerfile" "${REPO_ROOT}"

echo "Build complete!"
echo "Run an agent container with: bin/run_agent_container.sh [AGENT_NAME] <PROMPT>"
