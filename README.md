# Graviton 🚀

**Graviton** is an autonomous PR code reviewer, self-healing code fixer, and GitHub webhook event router powered by [Google Antigravity](https://antigravity.google).

It orchestrates sandboxed Docker container agents to automatically review pull requests, resolve review comments, execute local test suites, commit fixes, and push updates back to your repository.

---

## 🌟 Key Features

- **Automated PR Code Review**: Triggers `code_reviewer` on `pull_request` (`opened` / `synchronize`) events to analyze code quality and post structured GitHub Reviews (`APPROVE` or `CHANGES_REQUESTED`).
- **Self-Healing Code Remediation**: Triggers `code_fixer` on `pull_request_review` or inline `pull_request_review_comment` events to automatically parse review comments, modify code, run test suites, and push commits.
- **Periodic Codebase Sweeps & Maintenance**: Runs `TaskScheduler` background daemon to trigger `codebase_auditor` sweeps for bug detection, performance optimizations, readability improvements, and refactoring needs.
- **Infinite Loop Protection**: All agent comments include a signature tag (`<!-- antigravity-auto-reply -->`). Graviton filters out these tags so agents never reply to themselves.
- **Human Comment Support**: Responds directly to human review comments and explicit mention commands (`@antigravity` or `/fix`).
- **Zero External Dependencies**: `bin/graviton-server.py` relies exclusively on Python's standard library (`http.server`, `hmac`, `threading`, `subprocess`, `sched`).

---

## 🛠️ Repository Structure

```text
graviton/
├── README.md                   # Setup guide & documentation
├── LICENSE
├── Dockerfile                  # Sandboxed agent container image definition
├── .github/
│   └── workflows/
│       └── test.yml            # CI workflow for unit tests
├── bin/
│   ├── build_agent_container.sh# Script to build Docker container image
│   ├── graviton-server.py      # Webhook server & event router entrypoint
│   ├── run_agent_container.sh  # Docker container launcher with auth volume mounts
│   └── run_listener.sh         # Smee.io local proxy runner
├── config/
│   └── schedules.json          # Periodic task schedule definitions
├── lib/                        # Core library components
│   ├── __init__.py
│   ├── router.py               # GitHub event parsing & routing logic
│   ├── runner.py               # Subprocess agent container executor
│   ├── scheduler.py            # Periodic background task scheduler engine
│   ├── security.py             # HMAC signature & bot tag verification
│   └── updater.py              # Git sync & hot reload process manager
├── tests/                      # Unit test suite (49+ tests)
│   ├── test_agent_skills.py
│   ├── test_router.py
│   ├── test_runner.py
│   ├── test_scheduler.py
│   ├── test_security.py
│   ├── test_server.py
│   └── test_updater.py
├── agents/                     # Agent role specifications
│   ├── codebase_auditor.json   # Periodic Codebase Audit & Sweep spec
│   ├── issue_triager.json      # Issue Triage & Design Specifier spec
│   ├── code_reviewer.json      # PR Code Reviewer spec
│   └── code_fixer.json         # PR Code Fixer & Thread Responder spec
├── skills/                     # Project agent skills
│   ├── codebase-auditor-guidelines/# Dedicated skill for codebase_auditor
│   │   └── SKILL.md
│   ├── code-review-guidelines/ # Dedicated skill for code_reviewer
│   │   └── SKILL.md
│   ├── code-fixer-guidelines/  # Dedicated skill for code_fixer
│   │   └── SKILL.md
│   └── issue-triager-guidelines/# Dedicated skill for issue_triager
│       └── SKILL.md
└── docs/
    └── ARCHITECTURE.md         # Event state machine & webhook routing specs
```

---

## 🚀 Quickstart

### 1. Build the Sandboxed Agent Container
```bash
./bin/build_agent_container.sh
```

### 2. Start the Graviton Webhook Server
```bash
python3 bin/graviton-server.py --port 8000 --enable-scheduler
```
*Options:*
- `--port` / `-p`: Port to bind (default: `8000`).
- `--secret` / `-s`: Optional GitHub Webhook secret for HMAC SHA-256 signature verification.
- `--reviewer`: Custom reviewer agent name (default: `code_reviewer`).
- `--fixer`: Custom fixer agent name (default: `code_fixer`).
- `--triager`: Custom triager agent name (default: `issue_triager`).
- `--enable-scheduler`: Enables periodic task scheduler for automated bug and quality sweeps.
- `--schedules-config`: Path to custom schedule JSON configuration file (default: `config/schedules.json`).

### 3. Connect Webhook via Smee.io (Local Development)
```bash
./bin/run_listener.sh https://smee.io/your-channel-id 8000
```

---

## ⚙️ GitHub Webhook Configuration

In any of your GitHub repositories:
1. Go to **Settings > Webhooks > Add webhook**.
2. Set **Payload URL**: `https://smee.io/your-channel-id` (or your public server URL).
3. Set **Content type**: `application/json`.
4. Select events:
   - ✅ Pull requests
   - ✅ Pull request reviews
   - ✅ Pull request review comments
   - ✅ Issue comments

---

## 📖 Architecture & State Machine

For full details on event routing, loop prevention, and circuit breakers, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 📜 License
MIT
