---
name: graviton
description: Manage and query the Graviton autonomous webhook server, active task queue, container supervisor execution, and GitHub PR reviews.
---

# Graviton Management Skill (`/graviton`)

This skill allows you to monitor and control Graviton—an autonomous GitHub webhook responder and ContainerSupervisor pipeline running as an Antigravity background sidecar.

## Common Operations

### 1. Check Status
Check server health, worker status, and quota pacing:
- Call `graviton_status()`

### 2. View Active and Queued Tasks
List running, pending, and recently completed tasks:
- Call `graviton_list_tasks(limit=20)`

### 3. Inspect a Specific Task
Examine model thoughts, tool calls, and output logs for a task ID:
- Call `graviton_get_task(task_id="task-1")`

### 4. Trigger a PR Review
Submit a pull request to be reviewed autonomously inside an isolated Docker container:
- Call `graviton_submit_review(repo_full_name="owner/repo", pr_number=123)`

### 5. Abort a Task
Cancel a running or queued task:
- Call `graviton_abort_task(task_id="task-1")`
