# Graviton 🚀

**Graviton** is an autonomous PR code reviewer, self-healing code fixer, and GitHub webhook supervisor powered by [Google Antigravity](https://antigravity.google).

Packaged as a first-class **Antigravity Plugin**, Graviton orchestrates sandboxed Docker container agents to automatically review pull requests, resolve review comments, execute test suites, commit fixes, and push updates back to your repository. It connects directly to the **Antigravity Remote Control site** (`https://antigravity.google.com`) for live streaming turn visibility and interactive oversight.

---

## 🌟 Key Features

- **Programmatic Stream-JSON Supervisor**: Eliminates one-shot print timeouts and transcript file scraping. Drives `agy` inside isolated Docker containers via `--input-format stream-json --output-format stream-json` with multi-turn `/goal` instructions and watchdog timers.
- **Antigravity Remote Control Integration**: Every running task automatically captures or synthesizes its Remote Control session URL (`https://antigravity.google.com/c/<conversation_id>`). Live links are surfaced in GitHub PR comments, the REST API, MCP tools, and the TUI dashboard.
- **Antigravity Plugin & Sidecar Daemon**: Packaged as an installable plugin (`plugin/`, symlinked to `.agents/plugins/graviton`) with native slash command (`/graviton`), Model Context Protocol (MCP) server, first-class sub-agents, and automated background sidecar execution.
- **Dual-Pool Quota & Adaptive Pacing**: Real-time tracking of both Gemini and Claude/GPT quota windows (5-hour and 1-week). Automatically balances models, enforces pacing delay windows, and pauses tasks before quota exhaustion.
- **Automated PR Code Review & Fix Cycles**:
  - `code_reviewer`: Analyzes diffs, checks test suites, and posts GitHub Reviews (`APPROVE` or `CHANGES_REQUESTED`).
  - `code_fixer`: Automatically addresses review comments, repairs failing tests in an ephemeral workspace, commits, and pushes updates.
  - `issue_triager`: Automatically engages on new issues, clarifies requirements, and marks issues `ready-for-pr`.
  - `pr_drafter`: Automatically implements features on dedicated branches and opens pull requests.
- **Interactive Terminal Dashboard (TUI)**: Beautiful split-pane terminal UI featuring real-time task queues, active workers, dual-pool quota gauges, and live container logs. Press `o` to immediately launch the active agent session in your browser.
- **Model Context Protocol (MCP)**: Exposes programmatic supervisor tools (`graviton_status`, `graviton_list_tasks`, `graviton_get_task`, `graviton_submit_review`, `graviton_submit_task`, `graviton_abort_task`) to any MCP-enabled assistant.
- **GitHub Mobile Release Controller**: Triggers release tagging scripts (e.g. `bin/tag.sh patch|minor|major`) via comments on a dedicated GitHub issue, configured per-repo via `.graviton.json`.
- **Zero External Dependencies**: Core server and supervisor rely exclusively on Python's standard library.

---

## 🛠️ Repository Structure

```text
graviton/
├── bin/                          # Server entrypoints (graviton-server.py, etc.)
├── config/                       # Schedules & repo config
├── lib/                          # Core Python server & supervisor libraries
├── tests/                        # Comprehensive unit test suite
│
├── plugin/                       # Single top-level directory for all plugin assets
│   ├── plugin.json               # Plugin manifest & metadata
│   ├── mcp_config.json           # MCP tool definitions
│   ├── hooks.json                # Agent lifecycle hooks
│   ├── bin/                      # Plugin executables (graviton-sidecar & graviton-mcp)
│   ├── rules/
│   │   └── AGENTS.md             # Supervisor rules
│   ├── agents/                   # First-class sub-agents exposed to users:
│   │   ├── code_reviewer/agent.md
│   │   ├── code_fixer/agent.md
│   │   ├── issue_triager/agent.md
│   │   ├── pr_drafter/agent.md
│   │   └── codebase_auditor/agent.md
│   └── skills/                   # Unified skills & runbooks:
│       ├── graviton/SKILL.md     # Supervisor management skill (/graviton)
│       ├── code-review-guidelines/SKILL.md
│       ├── code-fixer-guidelines/SKILL.md
│       ├── issue-triager-guidelines/SKILL.md
│       ├── pr-drafter-guidelines/SKILL.md
│       └── codebase-auditor-guidelines/SKILL.md
│
├── .agents/plugins/graviton      # Symlink bridge -> ../../plugin
└── docs/
    └── ARCHITECTURE.md           # Event state machine & supervisor specifications
```

---

## 🚀 Quickstart

### 1. Build the Sandboxed Agent Container
```bash
./bin/build_agent_container.sh
```

### 2. Start the Graviton Webhook Server
```bash
python3 bin/graviton-server.py --port 8000
```
*Options:*
- `--port` / `-p`: Port to bind (default: `8000`).
- `--secret` / `-s`: Optional GitHub Webhook secret for HMAC SHA-256 signature verification.
- `--smee-url`: Smee.io channel URL to automatically launch background webhook proxy listener (env: `SMEE_URL`).
- `--post-start-comment`: Post an initial comment with live Remote Control link upon agent start (env: `GRAVITON_POST_START_COMMENT`).
- `--post-completion-comment`: Post structured completion comment with session replay link upon agent finish (env: `GRAVITON_POST_COMPLETION_COMMENT`).
- `--max-workers`: Maximum concurrent active task workers (default: `2`).
- `--quota-pool`: Quota pool preference (`gemini`, `claude_gpt`, `auto`, or `equal`).

### 3. Connect Webhook via Smee.io (Local Development)
Pass `--smee-url` when starting `graviton-server.py` to automatically spawn the Smee webhook proxy listener:
```bash
python3 bin/graviton-server.py --port 8000 --smee-url https://smee.io/your-channel-id
```

---

## 🔌 Antigravity Plugin & MCP Server

Graviton is packaged as an official Antigravity plugin located in the top-level `plugin/` directory (with `.agents/plugins/graviton` symlink bridge).

### Installing the Plugin
```bash
agy plugin install plugin
# or via the installer script:
bin/graviton-plugin-install --workspace
```

### First-Class Sub-Agents
Users can directly invoke Graviton's specialized sub-agents in interactive chat sessions:
- `@code_reviewer`: Automated PR code reviewer for external and internal pull requests.
- `@code_fixer`: Automated PR code fixer and review responder.
- `@issue_triager`: Autonomous GitHub issue triager and design specifier.
- `@pr_drafter`: Automated initial PR drafter from triaged issues.
- `@codebase_auditor`: Autonomous codebase auditor for bug detection, performance sweeps, and refactoring.

### Native Slash Command
Type `/graviton` in the Antigravity chat to query status, inspect tasks, or submit reviews directly:
```text
/graviton status
/graviton review owner/repo#42
/graviton tasks
```

### MCP Tools
When running as an MCP server, Graviton exposes the following tools:
- `graviton_dashboard`: Generate live formatted markdown dashboard and register an Antigravity artifact path for continuous real-time server updates.
- `graviton_status`: Check health, worker count, queue depth, and model quota pacing.
- `graviton_list_tasks`: List active, queued, and completed tasks with live Remote Control URLs.
- `graviton_get_task`: Retrieve real-time streaming thoughts, tool calls, and logs for a specific task.
- `graviton_submit_review`: Request container-isolated autonomous PR review (`repo_full_name`, `pr_number`).
- `graviton_submit_task`: Enqueue an arbitrary task prompt to be executed by a containerized agent persona.
- `graviton_abort_task`: Cancel an active or queued task.

---

## 🌌 Live Dashboard (Antigravity Side Panel & Web)

Graviton provides an autonomous live dashboard that is automatically kept up to date by the server on disk:

1. **Antigravity Side Panel (Auxiliary Pane)**:
   - Run `/graviton dashboard` or call `graviton_dashboard(artifact_path="<artifact_path>")`.
   - The Graviton server registers the artifact file on disk and automatically re-writes it in real-time as tasks progress, step, or finish.
   - Antigravity's file-watcher instantly live-refreshes the side panel with zero manual reloading required!
2. **Standalone Web Dashboard**:
   - Access `http://localhost:8000/dashboard` in any web browser for a responsive, dark-mode real-time view with auto-refreshing task metrics and model pacing.

---

## 🖥️ Legacy Terminal Dashboard (TUI) & Headless Server

`bin/graviton-server.py` now runs in **headless daemon mode by default**, making it ideal for background sidecars and containerized deployment.

The curses-based Terminal Dashboard is deprecated:
- To run the legacy console UI, pass the `--tui` flag:
  ```bash
  python3 bin/graviton-server.py --tui
  ```

---

## ⚠️ Deprecation Notice

- **`bin/run_agent_container.sh`** and the legacy one-shot runner in `lib/runner.py` are deprecated.
- **Terminal UI (`--tui`)** is deprecated in favor of the Graviton Antigravity plugin, live side panel artifact, and web dashboard.
- All container executions default to the programmatic `lib.supervisor.ContainerSupervisor` using the NDJSON stream protocol (`--input-format stream-json --output-format stream-json`).
- If you must temporarily run without supervisor, pass `--no-supervisor` (deprecated).

---

## 🚀 GitHub Mobile Release Controller

Graviton allows you to trigger release tagging scripts (e.g. `bin/tag.sh`) across your app repositories right from the GitHub Mobile app.

### 1. Configure `.graviton.json` in Your App Repository

```json
{
  "release": {
    "issue_pattern": "(?i)^🚀?\\s*release(?:\\s+(?:controller|tracker))?\\s*$",
    "branch": "main",
    "allowed_users": ["your_github_username"],
    "commands": {
      "patch": "bin/tag.sh patch",
      "minor": "bin/tag.sh minor",
      "major": "bin/tag.sh major"
    },
    "pre_flight": [
      "git diff --quiet"
    ]
  }
}
```

### 2. Tag Releases on GitHub Mobile

Open the pinned **`🚀 Release Controller`** issue on GitHub Mobile and comment:
- `patch` or `/tag patch`
- `minor` or `/release minor`
- `major` or `/tag major`
- `help`

---

## 📜 License
MIT
