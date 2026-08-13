---
name: codebase-auditor-guidelines
description: >-
  Use this skill when auditing the codebase for periodic bug sweeps, performance optimizations, readability improvements, and modularization refactoring.
---

# Codebase Auditor Guidelines

This skill provides comprehensive instructions for the `codebase_auditor` agent to perform scheduled maintenance sweeps and file actionable GitHub issues.

## Periodic Sweep Workflow

1. **Known Issue Retrieval**:
   - Fetch all open issues using GitHub CLI:
     `gh issue list --state open --json number,title,body,labels`
   - Build an in-memory cache of existing open issue titles, bodies, and labels.

2. **Codebase Inspection**:
   - For **Bug Sweeps**: Search `/workspace` for unhandled exceptions, resource leaks, missing validation, edge cases, broken error paths, or race conditions.
   - For **Quality Sweeps**: Analyze file sizes, long functions, high complexity, missing docstrings/comments, redundant computations, or tightly coupled modules.

3. **Deduplication Check**:
   - Before filing a new issue, compare proposed finding titles and topics against existing open issues.
   - If an open issue already addresses the finding, skip filing to prevent duplicates.

4. **Automated Issue Filing**:
   - File new issues using `gh issue create`:
     - Bug Sweep: `gh issue create --title "[Bug Sweep] <summary>" --body "<details & repro>\n\n<!-- antigravity-auto-reply -->\n<!-- graviton:codebase_auditor -->" --label "bug"`
     - Quality Sweep: `gh issue create --title "[Quality Sweep] <scope>: <recommendation>" --body "<rationale & code snippet>\n\n<!-- antigravity-auto-reply -->\n<!-- graviton:codebase_auditor -->" --label "enhancement"`

5. **Safety Guardrails & Bot Marker**:
   - Always append `<!-- antigravity-auto-reply -->` and `<!-- graviton:codebase_auditor -->` to all filed issue bodies to maintain loop protection.
