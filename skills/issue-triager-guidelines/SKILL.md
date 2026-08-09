---
name: issue-triager-guidelines
description: >-
  Use this skill when triaging GitHub issues to gather requirements, post design specifications, ask clarifying questions, and manage issue readiness labels.
---

# Issue Triager Guidelines

This skill provides comprehensive instructions for the `issue_triager` agent to analyze GitHub issues and guide them to PR-readiness.

## Triage Workflow

1. **Issue & Context Analysis**:
   - Inspect the issue title, body, and all discussion comments using GitHub CLI (`gh issue view <number> --comments`).
   - Identify missing requirements, reproduction steps, architectural choices, or edge cases.

2. **Clarification vs. Design Specification**:
   - **Missing Information**: If requirements or reproduction steps are unclear or incomplete, post a polite, structured comment asking clarifying questions.
   - **Complete Information**: When all necessary details are present, write and post a detailed design specification comment outlining the proposed implementation plan.

3. **Label Management**:
   - Once a comprehensive design specification is posted and ready for implementation, add the `ready-for-pr` label using `gh issue edit <number> --add-label ready-for-pr`.

4. **Safety & Signature**:
   - **Bot Tag Signature**: Always append `<!-- antigravity-auto-reply -->` to **all** GitHub outputs (`gh pr create` body descriptions, `gh pr review` body submissions, `gh issue comment` / `gh pr comment` replies) to prevent infinite agent loop recursion.
