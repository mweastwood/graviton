---
name: code-review-guidelines
description: >-
  Use this skill when performing PR code reviews to verify code quality, security,
  error handling, test coverage, and submit review results.
---

# Code Review Guidelines

This skill provides comprehensive instructions for the `code_reviewer` agent to analyze pull requests and submit reviews.

## Review Process

1. **Diff Inspection**:
   - Fetch and analyze the PR diff using `git diff` and GitHub CLI (`gh pr diff <number>`).
   - Review all modified, added, and deleted files carefully.

2. **Quality & Security Standards**:
   - **Security & Secrets**: Check for exposed API keys, credentials, tokens, or unsafe dynamic execution.
   - **Error Handling**: Verify that exceptions are caught and handled gracefully without silent failures or swallowing errors.
   - **Test Coverage**: Ensure new code components or refactored logic have corresponding unit tests.
   - **Code Quality & Style**: Verify readability, adherence to design patterns, and absence of redundant logic.

3. **Submitting Review**:
   - Use `gh api` or `gh pr review` to submit the review.
   - Set review state to `APPROVE` if all standards are met with no critical issues.
   - Set review state to `CHANGES_REQUESTED` if bugs, missing tests, or security concerns are identified.

4. **Safety & Loop Protection**:
   - **Bot Tag Signature**: Always append `<!-- antigravity-auto-reply -->` to all GitHub review comments to prevent infinite agent loop recursion.
