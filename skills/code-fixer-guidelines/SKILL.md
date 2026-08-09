---
name: code-fixer-guidelines
description: >-
  Use this skill when resolving PR review feedback, executing local unit tests, drafting initial PRs from ready issues, and pushing code fixes.
---

# Code Fixer & PR Drafter Guidelines

This skill provides comprehensive instructions for the `code_fixer` agent to resolve pull request review feedback and draft initial PRs for ready issues.

## 1. Remediation & Code Modification Workflow

1. **Review Feedback Analysis**:
   - Read the requested changes from GitHub PR review comments or review submissions.
   - Locate the target files and lines needing modifications in `/workspace`.

2. **Code Edits & Fix Application**:
   - Make precise edits to address all feedback items without introducing side effects.
   - Preserve existing API contracts and coding standards.

3. **Local Test Execution (Test Gate)**:
   - Execute local unit tests (e.g. `python3 -m unittest discover tests`) before committing code.
   - If tests fail, diagnose and fix the failure. Do NOT push broken code to remote branches.

4. **Git Operations & Remote Push**:
   - Stage modified files and create a clean git commit with a descriptive message.
   - Push changes to the target remote branch (`git push origin <branch>`).

5. **PR Drafting from Ready Issues**:
   - When triggered on issues labeled `ready-for-pr` or receiving `/draft-pr`:
     - Create a new feature branch from `main`.
     - Implement the required changes and run unit tests.
     - Open a new pull request using `gh pr create` with `<!-- antigravity-auto-reply -->` appended to the PR body description.

6. **Safety & Loop Protection**:
   - **Bot Tag Signature**: Always append `<!-- antigravity-auto-reply -->` to **all** GitHub outputs (`gh pr create` body descriptions, `gh pr review` body submissions, `gh issue comment` / `gh pr comment` replies) to prevent infinite agent loop recursion.
   - Track iteration count using `<!-- agy-cycle: X/3 -->` to enforce maximum cycle limits.
