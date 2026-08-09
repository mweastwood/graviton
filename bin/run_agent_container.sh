#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 [AGENT_NAME] <PROMPT>"
  echo "Examples:"
  echo "  $0 code_reviewer \"Review PR #427\""
  echo "  $0 \"Review PR #427\""
  exit 1
fi

# Determine if first argument specifies an agent or is the prompt
AGENT_NAME="${DEFAULT_AGENT:-code_reviewer}"
PROMPT=""

if [ "$#" -ge 2 ]; then
  AGENT_NAME="$1"
  shift
  PROMPT="$*"
else
  PROMPT="$1"
fi

IMAGE_NAME="${ANTIGRAVITY_IMAGE:-antigravity-agent:latest}"
WORKSPACE_DIR="$(pwd)"

# 1. Create an isolated ephemeral workspace for this run to prevent git conflicts
RUN_ID="$(date +%s)_${RANDOM}"
TEMP_WORKSPACE="/tmp/graviton-workspaces/run-${RUN_ID}"
mkdir -p "${TEMP_WORKSPACE}"

# Fast local git clone to ensure an isolated .git index and working copy
git clone --local "${WORKSPACE_DIR}" "${TEMP_WORKSPACE}" &>/dev/null || cp -a "${WORKSPACE_DIR}/." "${TEMP_WORKSPACE}/"

# Restore original remote origin URL (git clone --local sets origin to the local host folder)
ORIGIN_URL="$(git -C "${WORKSPACE_DIR}" remote get-url origin 2>/dev/null || echo "")"
if [ -n "${ORIGIN_URL}" ]; then
  git -C "${TEMP_WORKSPACE}" remote set-url origin "${ORIGIN_URL}" &>/dev/null || true
fi

# Clean up ephemeral workspace on exit (suppress permission warnings if created files are restricted)
trap 'rm -rf "${TEMP_WORKSPACE}" 2>/dev/null || true' EXIT

AGY_BIN_MOUNT=()
if command -v agy &>/dev/null; then
  AGY_BIN_MOUNT=(-v "$(command -v agy):/usr/local/bin/agy:ro")
fi

# Mount repository skills directory if present into container global config
SKILLS_MOUNT=()
if [ -d "${TEMP_WORKSPACE}/skills" ]; then
  SKILLS_MOUNT=(-v "${TEMP_WORKSPACE}/skills:/root/.gemini/config/skills:ro")
fi

# Pass user SSH keys, gh configuration, and antigravity-cli directory if present
SSH_MOUNT=()
if [ -d "${HOME}/.ssh" ]; then
  SSH_MOUNT=(-v "${HOME}/.ssh:/root/.ssh:ro")
fi

GH_CONFIG_MOUNT=()
if [ -d "${HOME}/.config/gh" ]; then
  GH_CONFIG_MOUNT=(-v "${HOME}/.config/gh:/root/.config/gh:ro")
fi

CLI_DIR_MOUNT=()
if [ -d "${HOME}/.gemini/antigravity-cli" ]; then
  CLI_DIR_MOUNT=(-v "${HOME}/.gemini/antigravity-cli:/root/.gemini/antigravity-cli")
fi

# Extract host git identity or environment overrides to inherit commit author details
GIT_USER_NAME="$(git config user.name 2>/dev/null || echo "${GIT_AUTHOR_NAME:-Graviton Bot}")"
GIT_USER_EMAIL="$(git config user.email 2>/dev/null || echo "${GIT_AUTHOR_EMAIL:-graviton-bot@users.noreply.github.com}")"

echo "Starting sandboxed Antigravity Agent container (Agent: ${AGENT_NAME}, Run ID: ${RUN_ID})..."

# Launch container with retry / continuation loop for turn & timeout limits
MAX_ATTEMPTS="${MAX_AGENT_RETRIES:-2}"
ATTEMPT=1
EXIT_CODE=0

while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
  AGY_ARGS=(agy --agent "${AGENT_NAME}" --dangerously-skip-permissions --log-file /dev/stderr --print-timeout 10m)

  if [ $ATTEMPT -eq 1 ]; then
    AGY_ARGS+=(--prompt "${PROMPT}")
  else
    echo "Agent session paused or hit step limit. Auto-continuing conversation (Attempt ${ATTEMPT}/${MAX_ATTEMPTS})..."
    AGY_ARGS+=(--continue --prompt "Continue your work to complete the requested task.")
  fi

  set +e
  docker run --rm \
    "${AGY_BIN_MOUNT[@]}" \
    "${SKILLS_MOUNT[@]}" \
    "${SSH_MOUNT[@]}" \
    "${GH_CONFIG_MOUNT[@]}" \
    "${CLI_DIR_MOUNT[@]}" \
    -v "${TEMP_WORKSPACE}:/workspace" \
    -w /workspace \
    -e GITHUB_TOKEN="$(gh auth token 2>/dev/null || echo "")" \
    -e GIT_AUTHOR_NAME="${GIT_USER_NAME}" \
    -e GIT_AUTHOR_EMAIL="${GIT_USER_EMAIL}" \
    -e GIT_COMMITTER_NAME="${GIT_USER_NAME}" \
    -e GIT_COMMITTER_EMAIL="${GIT_USER_EMAIL}" \
    --security-opt=no-new-privileges \
    "${IMAGE_NAME}" \
    "${AGY_ARGS[@]}"
  EXIT_CODE=$?
  set -e

  if [ $EXIT_CODE -eq 0 ]; then
    echo "Agent '${AGENT_NAME}' completed successfully."
    break
  fi

  echo "Agent exited with code ${EXIT_CODE} on attempt ${ATTEMPT}."
  ATTEMPT=$((ATTEMPT + 1))
done

if [ $EXIT_CODE -ne 0 ]; then
  echo "Agent '${AGENT_NAME}' failed after ${MAX_ATTEMPTS} attempts with exit code ${EXIT_CODE}."
  exit "${EXIT_CODE}"
fi
