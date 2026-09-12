---
name: code-fixer-guidelines
description: >-
  Use this skill when resolving PR review feedback, executing local unit tests, and pushing code fixes.
---

# Code Fixer Guidelines

This skill provides comprehensive instructions for the `code_fixer` agent to resolve pull request review feedback and inline code review comments.

## 1. Remediation & Code Modification Workflow

1. **Base Branch & Remote Synchronization**:
   - Run `git fetch origin` and ensure the target base branch is synchronized with remote changes before making modifications.

2. **Review Feedback Analysis**:
   - Read the requested changes from GitHub PR review comments or review submissions.
   - Locate the target files and lines needing modifications in `/workspace`.

3. **Code Edits & Fix Application**:
   - Make precise edits to address all feedback items without introducing side effects.
   - Preserve existing API contracts and coding standards.

4. **Local Test Execution (Test Gate)**:
   - Check for `.githooks/pre-commit` in the repository and verify pre-commit checks pass prior to committing and pushing.
   - Execute local unit tests (e.g. `python3 -m unittest discover tests`) before committing code.
   - When invoking commands via `run_command`, specify an adequate `WaitMsBeforeAsync` (up to `10000` ms) to allow tests to complete synchronously where possible.
   - **Crucial - Follow-Through on Background Tasks**: If a test or compilation command is backgrounded as a task, do NOT conclude the agent session or end your turn merely stating that tests are running in the background. Follow through until the task finishes, verify all tests pass, and proceed immediately to git commit and push.
   - If tests fail, diagnose and fix the failure. Do NOT push broken code to remote branches.

5. **Git Operations & Remote Push**:
   - Stage modified files and create a clean git commit with a descriptive message.
   - Push changes to the target remote branch (`git push origin <branch>`).
   - **Verification**: Ensure `git push origin <branch>` has successfully executed before finishing the session.

## 2. Safety & Loop Protection

- **Bot Tag Signature**: Always append `<!-- antigravity-auto-reply -->` and `<!-- graviton:code_fixer -->` to **all** GitHub outputs (`gh pr create` body descriptions, `gh pr review` body submissions, `gh issue comment` / `gh pr comment` replies) to prevent infinite agent loop recursion.
- Track iteration count using `<!-- agy-cycle: X/3 -->` to enforce maximum cycle limits.
