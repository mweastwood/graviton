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

2. **Author Verification & Submission Strategy**:
   - Inspect the PR author prior to review submission using `gh pr view <number> --json author`.
   - **External Author PRs (Not Author)**:
     - Submit formal PR reviews using `gh pr review <number> --request-changes` (for actionable findings) or `gh pr review <number> --approve` (if no changes needed).
   - **Own PRs (Author is Graviton/Bot)**:
     - **Never** attempt to submit formal reviews via `gh pr review` (saves execution cycles and avoids GitHub API rejection "Can't submit review on your own pull request").
     - **Changes Needed**: Post a PR comment via `gh pr comment <number> --body "..."` containing the explicit trigger keyword `/fix` (e.g. `/fix Action items required: ...`) so `code_fixer` is triggered to address findings.
     - **No Changes Needed**: Post an informational approval comment via `gh pr comment <number> --body "..."` without invoking `gh pr review`.

3. **Presubmit & CI Status Verification**:
   - Query presubmit test results using GitHub CLI (`gh pr checks <number>`) or API (`gh api repos/{owner}/{repo}/pulls/{number}` / `statusCheckRollup`).
   - If any presubmit check runs fail or indicate errors, report the failing check names in the review comments and set the review decision to `CHANGES_REQUESTED` (for external PRs) or include `/fix` trigger in `gh pr comment` (for own PRs).

4. **Mergeability Verification**:
   - Query PR mergeability using `gh pr view <number> --json mergeable,mergeStateStatus`.
   - Check if `mergeable` status is `MERGEABLE` or `DIRTY` / `CONFLICTING`.
   - If the PR cannot be cleanly merged (e.g. merge conflicts exist), explicitly request a rebase/conflict resolution in the review comments and set the review decision to `CHANGES_REQUESTED` (for external PRs) or include `/fix` trigger in `gh pr comment` (for own PRs).

5. **Quality & Security Standards**:
   - **Security & Secrets**: Check for exposed API keys, credentials, tokens, or unsafe dynamic execution.
   - **Error Handling**: Verify that exceptions are caught and handled gracefully without silent failures or swallowing errors.
   - **Test Coverage**: Ensure new code components or refactored logic have corresponding unit tests.
   - **Code Quality & Style**: Verify readability, adherence to design patterns, and absence of redundant logic.

6. **Review Submission Decision Logic**:
   - **External Author PRs**:
     - **ONLY** use formal GitHub PR review commands (`gh pr review`). Do **NOT** use `gh pr comment` or `gh issue comment` for code review submissions on external PRs.
     - **Strict Review Decision Rules**:
       - **Any Changes Needed**: If **any** changes (including minor fixes, style tweaks, nits, docstrings, additional test assertions, bugs, missing tests, security concerns, failing presubmit checks, or merge conflicts) are required, submit `gh pr review <pr_number> --request-changes --body "<body_with_bot_signature>"`. You must **never** use `--comment` for actionable review findings or code fixes because bot `COMMENTED` reviews are ignored by the webhook router and will not trigger `code_fixer`. Any review finding or requested change must trigger `--request-changes` so `code_fixer` is automatically triggered to resolve the review findings.
       - **No Changes Needed**: Submit `gh pr review <pr_number> --approve --body "<body_with_bot_signature>"` (if all quality standards, presubmit checks, and mergeability checks pass with no code changes needed) or `gh pr review <pr_number> --comment --body "<body_with_bot_signature>"` (only if **no code changes at all** are needed, e.g. purely advisory notes).
   - **Own PRs (Author is Graviton/Bot)**:
     - Do **NOT** use `gh pr review` commands (avoids GitHub API errors and cycle waste).
     - **Changes Needed**: Submit `gh pr comment <pr_number> --body "<body_with_fix_trigger_and_bot_signature>"` including the `/fix` command trigger.
     - **No Changes Needed**: Submit `gh pr comment <pr_number> --body "<body_with_bot_signature>"`.
   - Always ensure `<!-- antigravity-auto-reply -->` signature tag is included in the body submission.

7. **Safety & Loop Protection**:
   - **Bot Tag Signature**: Always append `<!-- antigravity-auto-reply -->` to **all** GitHub outputs (`gh pr create` body descriptions, `gh pr review` body submissions, `gh issue comment` / `gh pr comment` replies) to prevent infinite agent loop recursion.
   - **Formal Reviews for External PRs Only**: Submit formal code reviews via `gh pr review` (`--request-changes`, `--approve`, or `--comment`) when not the author. Never attempt `gh pr review` on own PRs.
   - **Triggering Fixer**: On external PRs, use `CHANGES_REQUESTED` (`--request-changes`). On own PRs, post `gh pr comment` with the explicit `/fix` trigger command so `code_fixer` can process the findings.

## Review Body Templates

### 1. Template for CHANGES_REQUESTED (External PRs)

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

### 2. Template for Own PR Review with Changes Needed (`/fix` Trigger)

Submitted via `gh pr comment <pr_number> --body "..."` when reviewing our own PR and changes are needed.

```markdown
/fix Action items required:

## Code Review Summary: Changes Requested (Own PR) ❌

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

<!-- antigravity-auto-reply -->
```

### 3. Template for APPROVE / NO_CHANGES_NEEDED

Submitted via `gh pr review <pr_number> --approve --body "..."` (for external PRs) or `gh pr comment <pr_number> --body "..."` (for own PRs) when all quality, CI, and mergeability standards pass without required code changes.

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
