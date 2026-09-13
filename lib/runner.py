"""
Agent container execution runner for Graviton.
"""

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional, Union

logger = logging.getLogger("graviton.runner")

# Only run_command and schedule can launch background tasks in Antigravity.
# Read-only inspection tools (view_file, grep_search, etc.) and file mutations
# cannot launch background tasks.
_BACKGROUNDABLE_TOOLS = frozenset({"run_command", "schedule"})


def is_transcript_incomplete(
    transcript_path: Union[str, Path],
    agent_name: Optional[str] = None,
) -> bool:
    """
    Check if an agy agent session transcript ended prematurely.

    Returns True if:
    1. The last planner response has unexecuted non-empty tool_calls.
    2. Any background command task was launched and has not finished or been canceled.
    3. The final response indicates the agent ended the turn waiting for background machine tasks/tests.
    4. The agent is 'pr_drafter' and PR creation deliverable was not completed (missing invocation
       or missing GitHub PR URL following the PR creation invocation).

    Note on agent deliverables:
    Deliverable checking at the runner level is intentionally scoped to 'pr_drafter' (verifying
    successful PR creation with a generated PR URL), where opening a PR is an unambiguous, mandatory
    exit deliverable. For 'code_fixer', while skills/code-fixer-guidelines/SKILL.md instructs the
    agent to verify 'git push' before concluding when pushing code fixes, this is deliberately not
    enforced as a hard deliverable requirement at the runner level because legitimate code_fixer
    sessions may conclude without pushing (e.g. answering reviewer questions, triaging tests, or
    reporting issues requiring human clarification). Note that any in-flight background tasks or
    premature waiting responses for code_fixer are still caught by checks #2 and #3.

    :param transcript_path: Path to transcript.jsonl file.
    :param agent_name: Optional name of the running agent (e.g. 'pr_drafter', 'code_fixer').
    :return: True if session is incomplete and should be resumed/continued, False otherwise.
    """
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return False

        steps = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    try:
                        data = json.loads(stripped)
                        if isinstance(data, dict):
                            steps.append(data)
                    except Exception:
                        continue

        if not steps:
            return False

        # 1. Check for unexecuted tool calls at the end of the session
        last_planner_idx = next(
            (i for i in reversed(range(len(steps))) if steps[i].get("type") == "PLANNER_RESPONSE"),
            None,
        )
        last_planner_step = steps[last_planner_idx] if last_planner_idx is not None else None

        if last_planner_step:
            tool_calls = last_planner_step.get("tool_calls", [])
            if isinstance(tool_calls, list) and tool_calls:
                subsequent_steps = steps[last_planner_idx + 1:]
                has_tool_results = any(
                    s.get("type") in ("TOOL_RESPONSE", "TOOL_RESULT", "GENERIC")
                    for s in subsequent_steps
                )
                if not has_tool_results:
                    return True

        # 2. Check for uncompleted background commands
        bg_launch_pattern = re.compile(
            r"Tool is running as a background task with task id:\s*([a-zA-Z0-9_\-./]+)"
        )
        bg_finish_pattern = re.compile(
            r'Task id\s+"([^"]+)"\s+(?:finished|was canceled)\s+with result:'
        )
        github_pr_url_pattern = re.compile(
            r"https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+"
        )

        active_bg_commands = set()
        pending_tool_calls = []

        for step in steps:
            step_type = step.get("type")
            if step_type == "PLANNER_RESPONSE":
                pending_tool_calls = list(step.get("tool_calls") or [])

            content = step.get("content") or ""
            content_str = content if isinstance(content, str) else ""

            # Genuine background task launches in Antigravity have status == "RUNNING".
            # Steps with status == "DONE" or "ERROR" (e.g. view_file, grep_search, completed git diff)
            # represent completed tool invocations and must not trigger background task tracking.
            # In test environments, step status may be omitted (None), so we only allow "RUNNING" or None.
            step_status = step.get("status")
            is_potential_launch = (step_status in ("RUNNING", None))

            m_launch = (
                bg_launch_pattern.search(content_str)
                if (content_str and is_potential_launch)
                else None
            )
            if m_launch:
                tid = m_launch.group(1)
                origin_tool = None
                if pending_tool_calls:
                    tool_call = pending_tool_calls.pop(0)
                    if isinstance(tool_call, dict):
                        origin_tool = tool_call.get("name")

                # Constrain originating tool to backgroundable tools only
                if origin_tool is None or origin_tool in _BACKGROUNDABLE_TOOLS:
                    m_desc = re.search(r"Task Description:\s*(.*)", content_str)
                    desc_str = m_desc.group(1).strip() if m_desc else ""

                    # Identify timer tasks:
                    # Prefer classifying via originating tool name ('schedule') over internal description format.
                    # Fallback to Task Description prefix 'Timer:' for backwards compatibility.
                    is_timer = (origin_tool == "schedule") or (
                        origin_tool is None and desc_str.startswith("Timer:")
                    )

                    if not is_timer:
                        active_bg_commands.add(tid)
            elif step_type in ("GENERIC", "TOOL_RESPONSE", "TOOL_RESULT") and pending_tool_calls:
                pending_tool_calls.pop(0)

            if content_str:
                for tid in bg_finish_pattern.findall(content_str):
                    active_bg_commands.discard(tid)

        if active_bg_commands:
            return True

        # 3. Check if final response indicates waiting for background task / test completion
        # Heuristic: detect when an agent ends turn explicitly waiting on a background machine
        # task / test suite / command run to finish or complete.
        #
        # Intentionally avoids matching normal completion messages where the agent waits for
        # human review, approval, feedback, or input (e.g., "am waiting for your approval to merge",
        # "waiting for your feedback", "waiting on user input").
        #
        # Matches:
        #   - "I have launched the deep link dispatcher widget tests and am waiting for them to finish."
        #   - "Waiting for the tests to complete."
        #   - "Waiting for tests to finish."
        #   - "Waiting for test suite to complete."
        #   - "Waiting for the command to finish."
        #   - "will wait for background task to complete"
        #   - "Waiting for task completion."
        # Does NOT match:
        #   - "I launched the tests and am waiting for your approval to merge"
        #   - "Tests passed! Waiting for your feedback."
        machine_targets = (
            r"(?:them|it|"
            r"(?:the\s+|background\s+)?(?:tests?|command|task|job|build|process|test\s+suite))"
        )
        completion_verbs = r"(?:finish|complete|conclude|terminate)"
        waiting_verbs = r"(?:(?:am|is|are|will|currently)\s+)?wait(?:ing)?\s+(?:for|on)"

        waiting_pattern = re.compile(
            rf"(?i)\b(?:"
            rf"{waiting_verbs}\s+{machine_targets}\s+to\s+{completion_verbs}|"
            rf"launched\s+.*?\s+and\s+{waiting_verbs}\s+{machine_targets}\s+to\s+{completion_verbs}|"
            rf"{waiting_verbs}\s+(?:task|command|test|build|job)\s+completion"
            rf")\b"
        )
        if last_planner_step:
            last_content = last_planner_step.get("content") or ""
            if isinstance(last_content, str) and waiting_pattern.search(last_content):
                return True

        # 4. Agent-specific deliverables:
        # 4a. pr_drafter: must have executed `gh pr create` AND successfully produced a PR URL
        # in a subsequent step.
        if agent_name == "pr_drafter":
            pr_cmd_invoked = False
            pr_url_found = False
            pr_cmd_step_indices = set()

            for idx, step in enumerate(steps):
                for call in step.get("tool_calls") or []:
                    if isinstance(call, dict):
                        args = call.get("args") or {}
                        if isinstance(args, dict):
                            cmd = args.get("CommandLine") or ""
                            if isinstance(cmd, str) and re.search(r"\bgh\s+pr\s+create\b", cmd):
                                pr_cmd_invoked = True
                                pr_cmd_step_indices.add(idx)
                                break

            if not pr_cmd_invoked:
                return True

            # Ensure the PR URL appears in a tool output step following the gh pr create invocation
            first_pr_step = min(pr_cmd_step_indices)
            for step in steps[first_pr_step + 1:]:
                if step.get("type") not in ("TOOL_RESPONSE", "TOOL_RESULT", "GENERIC"):
                    continue
                content = step.get("content") or ""
                if isinstance(content, str) and github_pr_url_pattern.search(content):
                    pr_url_found = True
                    break

            if not pr_url_found:
                return True

        # 4b. Note on code_fixer:
        # As noted in the docstring, 'code_fixer' deliverable completeness is enforced via prompt/skill
        # guidelines rather than at the runner level, allowing legitimate non-push outcomes (e.g. answering
        # review questions or reporting insurmountable test failures). In-flight background tasks and
        # premature waiting messages are already caught by checks #2 and #3 above.

    except Exception as e:
        logger.debug(f"Error checking transcript completeness for '{transcript_path}': {e}")
    return False



def run_agent_container(
    agent_name: str,
    prompt: str,
    script_path: Path,
    cwd: Path,
    on_output: Optional[Callable[[str], None]] = None,
    max_attempts: Optional[int] = None,
    cached_workspace_dir: Optional[Path] = None,
    initial_attempt: Optional[int] = None,
    quota_pool: Optional[str] = None,
    model: Optional[str] = None,
    on_process_created: Optional[Callable[[subprocess.Popen], None]] = None,
) -> subprocess.CompletedProcess:
    """
    Execute the agent container script synchronously.

    :param agent_name: Name of agent specification (e.g. 'code_reviewer').
    :param prompt: Prompt instruction string for agent.
    :param script_path: Path to run_agent_container.sh.
    :param cwd: Working directory (repository root).
    :param on_output: Optional callback function invoked for each line of stdout/stderr output.
    :param max_attempts: Optional maximum agent retry attempt limit (sets MAX_AGENT_RETRIES env var).
    :param cached_workspace_dir: Optional path to workspace cache directory (sets GRAVITON_WORKSPACE_CACHE_DIR env var).
    :param initial_attempt: Optional initial attempt number to resume execution pass (sets GRAVITON_INITIAL_ATTEMPT env var).
    :param quota_pool: Optional quota pool selected for task execution (sets ANTIGRAVITY_QUOTA_POOL env var).
    :param model: Optional model selected for task execution (sets ANTIGRAVITY_MODEL & MODEL_NAME env vars).
    :param on_process_created: Optional callback function invoked immediately upon subprocess launch.
    :return: subprocess.CompletedProcess instance.
    """
    cmd = [str(script_path), agent_name, prompt]
    logger.info(f"Triggering agent '{agent_name}' with prompt: '{prompt}'")

    env = os.environ.copy()
    if max_attempts is not None:
        env["MAX_AGENT_RETRIES"] = str(max_attempts)
    if cached_workspace_dir is not None:
        env["GRAVITON_WORKSPACE_CACHE_DIR"] = str(cached_workspace_dir)
    if initial_attempt is not None:
        env["GRAVITON_INITIAL_ATTEMPT"] = str(initial_attempt)
    if quota_pool is not None:
        env["ANTIGRAVITY_QUOTA_POOL"] = str(quota_pool)
    if model is not None:
        env["ANTIGRAVITY_MODEL"] = str(model)
        env["MODEL_NAME"] = str(model)

    process = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    if on_process_created:
        try:
            on_process_created(process)
        except Exception as e:
            logger.debug(f"Error in on_process_created callback: {e}")

    stdout_lines = []
    stderr_lines = []

    def read_stream(stream, lines_list):
        if not stream:
            return
        for line in stream:
            lines_list.append(line)
            if on_output:
                try:
                    on_output(line)
                except Exception as e:
                    logger.debug(f"Error in on_output callback: {e}")

    t_out = threading.Thread(target=read_stream, args=(process.stdout, stdout_lines), daemon=True)
    t_err = threading.Thread(target=read_stream, args=(process.stderr, stderr_lines), daemon=True)
    t_out.start()
    t_err.start()

    process.wait()
    t_out.join()
    t_err.join()

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=process.returncode,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
    )


def run_agent_async(
    agent_name: str,
    prompt: str,
    script_path: Path,
    cwd: Path,
    max_attempts: Optional[int] = None,
    cached_workspace_dir: Optional[Path] = None,
    initial_attempt: Optional[int] = None,
    quota_pool: Optional[str] = None,
    model: Optional[str] = None,
    on_process_created: Optional[Callable[[subprocess.Popen], None]] = None,
) -> threading.Thread:
    """
    Execute the agent container asynchronously in a background daemon thread.

    :param agent_name: Name of agent specification.
    :param prompt: Prompt instruction string for agent.
    :param script_path: Path to run_agent_container.sh.
    :param cwd: Working directory.
    :param max_attempts: Optional maximum agent retry attempt limit.
    :param cached_workspace_dir: Optional path to workspace cache directory.
    :param initial_attempt: Optional initial attempt number to resume execution pass.
    :param quota_pool: Optional quota pool selected for task execution.
    :param model: Optional model selected for task execution.
    :param on_process_created: Optional callback function invoked immediately upon subprocess launch.
    :return: Started daemon Thread instance.
    """
    def worker():
        try:
            kwargs = {
                "max_attempts": max_attempts,
                "cached_workspace_dir": cached_workspace_dir,
                "initial_attempt": initial_attempt,
                "quota_pool": quota_pool,
                "model": model,
            }
            if on_process_created is not None:
                kwargs["on_process_created"] = on_process_created
            result = run_agent_container(
                agent_name,
                prompt,
                script_path,
                cwd,
                **kwargs,
            )
            if result.returncode == 0:
                logger.info(f"Agent '{agent_name}' finished successfully for prompt: '{prompt}'")
                if result.stdout:
                    logger.info(f"Agent stdout:\n{result.stdout.strip()}")
            else:
                logger.error(f"Agent '{agent_name}' failed with exit code {result.returncode}")
                if result.stderr:
                    logger.error(f"Agent stderr:\n{result.stderr.strip()}")
        except Exception as e:
            logger.exception(f"Error executing agent container script: {e}")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        target_file = sys.argv[1]
        agent = sys.argv[2] if len(sys.argv) > 2 else None
        if is_transcript_incomplete(target_file, agent_name=agent):
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        sys.exit(2)

