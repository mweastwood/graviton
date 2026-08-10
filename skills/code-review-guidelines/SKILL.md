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
   - **Strict Review Decision Rules**:
     - **Any Changes Needed**: If **any** changes (including minor fixes, style tweaks, nits, docstrings, additional test assertions, bugs, missing tests, security concerns, failing presubmit checks, or merge conflicts) are required, submit `gh pr review <pr_number> --request-changes --body "<body_with_bot_signature>"`. You must **never** use `--comment` for actionable review findings or code fixes because bot `COMMENTED` reviews are ignored by the webhook router and will not trigger `code_fixer`. Any review finding or requested change must trigger `--request-changes` so `code_fixer` is automatically triggered to resolve the review findings.
     - **No Changes Needed**: Submit `gh pr review <pr_number> --approve --body "<body_with_bot_signature>"` (if all quality standards, presubmit checks, and mergeability checks pass with no code changes needed) or `gh pr review <pr_number> --comment --body "<body_with_bot_signature>"` (only if **no code changes at all** are needed, e.g. purely advisory notes).
   - Always ensure `<!-- antigravity-auto-reply -->` signature tag is included in the body submission.

6. **Safety & Loop Protection**:
   - **Bot Tag Signature**: Always append `<!-- antigravity-auto-reply -->` to **all** GitHub outputs (`gh pr create` body descriptions, `gh pr review` body submissions, `gh issue comment` / `gh pr comment` replies) to prevent infinite agent loop recursion.
   - **Formal Reviews Only**: Strictly submit code reviews via `gh pr review` (`--request-changes`, `--approve`, or `--comment`), never via `gh pr comment` or `gh issue comment`.
   - **Triggering Fixer**: Any review finding that requires code modifications (including minor nits and style tweaks) must set the review state to `CHANGES_REQUESTED` (`--request-changes`) so the event is not dropped by the router and `code_fixer` can process the findings. `--comment` must **never** be used for actionable review findings or fixes because bot `COMMENTED` reviews are ignored by the webhook router.

## Review Body Templates

### 1. Template for CHANGES_REQUESTED

Submitted via `gh pr review <pr_number> --request-changes --body "..."` when presubmit checks fail, merge conflicts exist, or any code changes (bugs, style tweaks, missing tests, etc.) are required.

```markdown
## Code Review Summary: Changes Requested ❌

### 1. Presubmit & CI Status
- **Status**: FAIL / PASS
- **Details**: <failing_check_names_or_all_passed>

### 2. Mergeability Verification
- **Status**: MERGEABLE / CONFLICTING
- **Details**: <rebase_required_or_clean>

### 3. Action Items & Required Changes
- **Security & Safety**: <findings_or_none>
- **Bugs & Correctness**: <findings_or_none>
- **Test Coverage**: <findings_or_none>
- **Code Quality & Style**: <findings_or_none>

### 4. Next Steps
Please resolve the actionable items above and push the fixes for re-review.

<!-- antigravity-auto-reply -->
```

### 2. Template for APPROVE / NO_CHANGES_NEEDED

Submitted via `gh pr review <pr_number> --approve --body "..."` (or `--comment` only for purely advisory notes) when all quality, CI, and mergeability standards pass without required code changes.

```markdown
## Code Review Summary: Approved ✅

### Verification Checklist
- [x] **Presubmit & CI**: Passed
- [x] **Mergeability**: Cleanly mergeable
- [x] **Security & Safety**: Verified
- [x] **Test Coverage**: Adequate
- [x] **Code Quality**: Meets standards

### Summary & Notes
<summary_of_changes_and_optional_notes>

<!-- antigravity-auto-reply -->
```
