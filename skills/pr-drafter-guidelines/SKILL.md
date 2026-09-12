---
name: pr-drafter-guidelines
description: >-
  Use this skill when drafting initial PRs from ready issues, creating feature branches, executing unit tests, and opening PRs.
---

# PR Drafter Guidelines

This skill provides comprehensive instructions for the `pr_drafter` agent to create initial pull requests for triaged issues.

## PR Drafting Workflow

1. **Base Branch & Remote Synchronization**:
   - Fetch remote changes (`git fetch origin`) and ensure the base branch is updated with the latest remote copy (`git checkout main && git pull origin main` or branch off `origin/main`).

2. **Feature Branch Creation**:
   - Create a fresh feature branch from `main` (or `origin/main`).

3. **Code Implementation & Edits**:
   - Implement the feature requirements or issue specifications precisely.
   - Preserve existing API contracts and coding standards.

4. **Local Test Execution (Test Gate)**:
   - Check for `.githooks/pre-commit` in the repository and verify pre-commit checks pass prior to committing and pushing.
   - Execute local unit tests (e.g. `python3 -m unittest discover tests`) before committing code.
   - When invoking commands via `run_command`, specify an adequate `WaitMsBeforeAsync` (up to `10000` ms) to allow tests to complete synchronously where possible.
   - **Crucial - Follow-Through on Background Tasks**: If a test or compilation command is backgrounded as a task, do NOT conclude the agent session or end your turn merely stating that tests are running in the background. Follow through until the task finishes, verify all tests pass, and proceed immediately to git commit, push, and PR creation.
   - If tests fail, diagnose and fix the failure. Do NOT push broken code to remote branches.

5. **Git Operations & PR Creation**:
   - Stage modified files and create a clean git commit with a descriptive message.
   - Push changes to the remote branch (`git push origin <branch>`).
   - Open a new pull request using `gh pr create` with `<!-- antigravity-auto-reply -->` and `<!-- graviton:pr_drafter -->` appended to the PR body description.
   - **Verification**: Ensure `gh pr create` has successfully executed and created the PR URL before finishing the session. A PR drafting task is only complete once the PR is opened.

6. **Safety & Loop Protection**:
   - **Bot Tag Signature**: Always append `<!-- antigravity-auto-reply -->` and `<!-- graviton:pr_drafter -->` to **all** GitHub outputs (`gh pr create` body descriptions, `gh pr review` body submissions, `gh issue comment` / `gh pr comment` replies) to prevent infinite agent loop recursion.
