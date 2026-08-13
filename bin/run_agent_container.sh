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

CACHE_DIR="${GRAVITON_WORKSPACE_CACHE_DIR:-}"
RESTORED_FROM_CACHE=false
EXIT_CODE=1

if [ -n "${CACHE_DIR}" ] && [ -d "${CACHE_DIR}" ]; then
  echo "Restoring workspace from cache: ${CACHE_DIR}"
  cp -a "${CACHE_DIR}/." "${TEMP_WORKSPACE}/"
  RESTORED_FROM_CACHE=true
else
  # Fast local git clone to ensure an isolated .git index and working copy
  git clone --local "${WORKSPACE_DIR}" "${TEMP_WORKSPACE}" &>/dev/null || cp -a "${WORKSPACE_DIR}/." "${TEMP_WORKSPACE}/"

  # Restore original remote origin URL (git clone --local sets origin to the local host folder)
  ORIGIN_URL="$(git -C "${WORKSPACE_DIR}" remote get-url origin 2>/dev/null || echo "")"
  if [ -n "${ORIGIN_URL}" ]; then
    git -C "${TEMP_WORKSPACE}" remote set-url origin "${ORIGIN_URL}" &>/dev/null || true
    git -C "${TEMP_WORKSPACE}" fetch origin &>/dev/null || true
    BASE_BRANCH="$(git -C "${TEMP_WORKSPACE}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")"
    if [ "${BASE_BRANCH}" = "HEAD" ]; then
      BASE_BRANCH="main"
    fi
    git -C "${TEMP_WORKSPACE}" checkout "${BASE_BRANCH}" &>/dev/null || true
    git -C "${TEMP_WORKSPACE}" reset --hard "origin/${BASE_BRANCH}" &>/dev/null || true
  fi
fi

# Configure git pre-commit hooks if present in workspace
if [ -f "${TEMP_WORKSPACE}/.githooks/pre-commit" ]; then
  chmod -R +x "${TEMP_WORKSPACE}/.githooks" 2>/dev/null || true
  git -C "${TEMP_WORKSPACE}" config core.hooksPath .githooks 2>/dev/null || true
fi

# Clean up ephemeral workspace and container instance on exit (suppress permission warnings if created files are restricted)
CONTAINER_NAME="graviton-agent-run-${RUN_ID}"
cleanup() {
  docker rm -f "${CONTAINER_NAME}" &>/dev/null || true
  if [ -n "${CACHE_DIR}" ]; then
    if [ "${EXIT_CODE:-1}" -ne 0 ]; then
      echo "Syncing workspace to cache: ${CACHE_DIR}"
      rm -rf "${CACHE_DIR}" 2>/dev/null || true
      mkdir -p "${CACHE_DIR}"
      cp -a "${TEMP_WORKSPACE}/." "${CACHE_DIR}/" 2>/dev/null || true
    else
      echo "Cleaning up workspace cache on success: ${CACHE_DIR}"
      rm -rf "${CACHE_DIR}" 2>/dev/null || true
    fi
  fi
  rm -rf "${TEMP_WORKSPACE}" 2>/dev/null || true
}
trap cleanup EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRAVITON_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

AGY_BIN_MOUNT=()
if command -v agy &>/dev/null; then
  AGY_BIN_MOUNT=(-v "$(command -v agy):/usr/local/bin/agy:ro")
fi

# Mount server-level skills directory into container global config
SKILLS_MOUNT=()
if [ -d "${GRAVITON_ROOT}/skills" ]; then
  SKILLS_MOUNT=(-v "${GRAVITON_ROOT}/skills:/root/.gemini/config/skills:ro")
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

# Launch a persistent container instance to ensure uncommitted files, git branch state,
# and environment variables are preserved across turns via docker exec.
USE_CONTAINER_EXEC=false
set +e
if docker run -d --name "${CONTAINER_NAME}" \
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
    sleep infinity &>/dev/null; then
  USE_CONTAINER_EXEC=true
fi
set -e

# Launch container with retry / continuation loop for turn & timeout limits
MAX_ATTEMPTS="${MAX_AGENT_RETRIES:-3}"
ATTEMPT="${GRAVITON_INITIAL_ATTEMPT:-1}"
AGENT_LOG="${TEMP_WORKSPACE}/agent_output.log"
PYTHON_BIN="$(command -v python3 || command -v python || echo "python3")"

while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
  AGY_ARGS=(agy --agent "${AGENT_NAME}" --dangerously-skip-permissions --log-file /dev/stderr --print-timeout 10m)

  if [ $ATTEMPT -eq 1 ] && [ "$RESTORED_FROM_CACHE" = false ]; then
    AGY_ARGS+=(--prompt "${PROMPT}")
  else
    echo "Agent session hit step limit with unexecuted tool calls. Auto-continuing conversation (Attempt ${ATTEMPT}/${MAX_ATTEMPTS})..."
    AGY_ARGS+=(--continue --prompt "Resume from your existing work in /workspace and complete your goal")
  fi

  set +e
  if [ "$USE_CONTAINER_EXEC" = true ]; then
    docker exec -w /workspace "${CONTAINER_NAME}" "${AGY_ARGS[@]}" 2>&1 | tee -a "${AGENT_LOG}"
    EXIT_CODE=${PIPESTATUS[0]}
  else
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
      "${AGY_ARGS[@]}" 2>&1 | tee -a "${AGENT_LOG}"
    EXIT_CODE=${PIPESTATUS[0]}
  fi
  set -e

  IS_INCOMPLETE=false
  CONVERSATION_ID="$(grep -oE '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' "${AGENT_LOG}" 2>/dev/null | tail -n 1 || echo "")"
  if [ -n "${CONVERSATION_ID}" ]; then
    TRANSCRIPT_PATH="${HOME}/.gemini/antigravity-cli/brain/${CONVERSATION_ID}/.system_generated/logs/transcript.jsonl"
    if [ -f "${TRANSCRIPT_PATH}" ]; then
      if "${PYTHON_BIN}" "${GRAVITON_ROOT}/lib/runner.py" "${TRANSCRIPT_PATH}" &>/dev/null; then
        IS_INCOMPLETE=true
      fi
    fi
  fi

  if [ $EXIT_CODE -eq 0 ] && [ "$IS_INCOMPLETE" = false ]; then
    echo "Agent '${AGENT_NAME}' completed successfully."
    break
  fi

  if [ $EXIT_CODE -ne 0 ]; then
    echo "Agent exited with code ${EXIT_CODE} on attempt ${ATTEMPT}."
  fi

  ATTEMPT=$((ATTEMPT + 1))
done

if [ $EXIT_CODE -ne 0 ]; then
  echo "Agent '${AGENT_NAME}' failed after ${MAX_ATTEMPTS} attempts with exit code ${EXIT_CODE}."
  exit "${EXIT_CODE}"
fi

