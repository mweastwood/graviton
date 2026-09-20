---
name: graviton
description: Manage and query the Graviton autonomous webhook server, active task queue, container supervisor execution, and GitHub PR reviews.
---

# Graviton Management Skill (`/graviton`)

This skill allows you to monitor and control Graviton—an autonomous GitHub webhook responder and ContainerSupervisor pipeline running as an Antigravity background sidecar.

## Common Operations

### 1. Open Live Dashboard in Side Panel
Open the live dashboard in Antigravity's Auxiliary Pane with automatic server-driven updates:
- Call `graviton_dashboard(artifact_path="<artifact_directory>/graviton_dashboard.md")`
- Use `write_to_file` to write the initial content to `<artifact_directory>/graviton_dashboard.md` with `UserFacing=True`
- The Graviton server will continuously update this artifact file on disk whenever tasks start, step, or finish.

### 2. Check Quick Status
Check server health, worker status, and quota pacing directly in chat:
- Call `graviton_status()`

### 3. View Active and Queued Tasks
List running, pending, and recently completed tasks:
- Call `graviton_list_tasks(limit=20)`

### 4. Inspect a Specific Task
Examine model thoughts, tool calls, and output logs for a task ID:
- Call `graviton_get_task(task_id="task-1")`

### 5. Trigger a PR Review
Submit a pull request to be reviewed autonomously inside an isolated Docker container:
- Call `graviton_submit_review(repo_full_name="owner/repo", pr_number=123)`

### 6. Abort a Task
Cancel a running or queued task:
- Call `graviton_abort_task(task_id="task-1")`
