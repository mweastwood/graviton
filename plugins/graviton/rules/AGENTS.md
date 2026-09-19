# Graviton Autonomous Task Supervision

When the Graviton plugin is active, you have direct programmatic control over the Graviton autonomous webhook server and ContainerSupervisor runtime via MCP tools.

## Available MCP Tools
- `graviton_status`: Check server health, active worker count, pending queue items, and model quota pacing.
- `graviton_list_tasks`: List currently running tasks, queued tasks, and recent task execution history.
- `graviton_get_task`: Retrieve real-time streaming thoughts, tool calls, status, and trailing logs for a specific task.
- `graviton_submit_review`: Request an autonomous, container-isolated review for a GitHub PR (`repo_full_name`, `pr_number`).
- `graviton_submit_task`: Enqueue an arbitrary task prompt to be executed by a containerized Graviton agent persona.
- `graviton_abort_task`: Cancel an active or queued task.

## Usage Guidelines
- When the user asks about the background reviewer, task queue, or server health, prefer calling `graviton_status` or `graviton_list_tasks`.
- When investigating a specific failure or review run, use `graviton_get_task` with the relevant task ID to inspect its thinking steps and tool executions.
