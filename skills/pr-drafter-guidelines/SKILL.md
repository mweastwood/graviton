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
   - Execute local unit tests (e.g. `python3 -m unittest discover tests`) before committing code.
   - If tests fail, diagnose and fix the failure. Do NOT push broken code to remote branches.

5. **Git Operations & PR Creation**:
   - Stage modified files and create a clean git commit with a descriptive message.
   - Push changes to the remote branch (`git push origin <branch>`).
   - Open a new pull request using `gh pr create` with `<!-- antigravity-auto-reply -->` and `<!-- graviton:pr_drafter -->` appended to the PR body description.

6. **Safety & Loop Protection**:
   - **Bot Tag Signature**: Always append `<!-- antigravity-auto-reply -->` and `<!-- graviton:pr_drafter -->` to **all** GitHub outputs (`gh pr create` body descriptions, `gh pr review` body submissions, `gh issue comment` / `gh pr comment` replies) to prevent infinite agent loop recursion.
