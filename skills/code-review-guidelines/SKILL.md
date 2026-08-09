---
name: code-review-guidelines
description: >-
  Use this skill when performing PR code reviews to verify code quality, security,
  presubmit CI status, mergeability, error handling, test coverage, and submit review results.
---

# Code Review Guidelines

This skill provides comprehensive instructions for the `code_reviewer` agent to analyze pull requests and submit reviews.

## Review Process

1. **Diff Inspection**:
   - Fetch and analyze the PR diff using `git diff` and GitHub CLI (`gh pr diff <number>`).
   - Review all modified, added, and deleted files carefully.

2. **Presubmit & CI Status Verification**:
   - Query presubmit test results using GitHub CLI (`gh pr checks <number>`) or API (`gh api repos/{owner}/{repo}/pulls/{number}` / `statusCheckRollup`).
   - If any presubmit check runs fail or indicate errors, report the failing check names in the review comments and set the review decision to `CHANGES_REQUESTED`.

3. **Mergeability Verification**:
   - Query PR mergeability using `gh pr view <number> --json mergeable,mergeStateStatus`.
   - Check if `mergeable` status is `MERGEABLE` or `DIRTY` / `CONFLICTING`.
   - If the PR cannot be cleanly merged (e.g. merge conflicts exist), explicitly request a rebase/conflict resolution in the review comments and set the review decision to `CHANGES_REQUESTED`.

4. **Quality & Security Standards**:
   - **Security & Secrets**: Check for exposed API keys, credentials, tokens, or unsafe dynamic execution.
   - **Error Handling**: Verify that exceptions are caught and handled gracefully without silent failures or swallowing errors.
   - **Test Coverage**: Ensure new code components or refactored logic have corresponding unit tests.
   - **Code Quality & Style**: Verify readability, adherence to design patterns, and absence of redundant logic.

5. **Review Submission Decision Logic**:
   - **ONLY** use formal GitHub PR review commands (`gh pr review`) to submit code reviews. Do **NOT** use `gh pr comment` or `gh issue comment` for code review submissions.
   - Use the following formal review commands:
     - `gh pr review <pr_number> --request-changes --body "<body_with_bot_signature>"` (if bugs, missing tests, security concerns, failing presubmit checks, or merge conflicts are identified)
     - `gh pr review <pr_number> --approve --body "<body_with_bot_signature>"` (if all quality standards, presubmit checks, and mergeability checks pass with no critical issues)
     - `gh pr review <pr_number> --comment --body "<body_with_bot_signature>"` (for general comments without approving or requesting changes)
   - Always ensure `<!-- antigravity-auto-reply -->` signature tag is included in the body submission.

6. **Safety & Loop Protection**:
   - **Bot Tag Signature**: Always append `<!-- antigravity-auto-reply -->` to **all** GitHub outputs (`gh pr create` body descriptions, `gh pr review` body submissions, `gh issue comment` / `gh pr comment` replies) to prevent infinite agent loop recursion.
   - **Formal Reviews Only**: Strictly submit code reviews via `gh pr review` (`--request-changes`, `--approve`, or `--comment`), never via `gh pr comment` or `gh issue comment`.



