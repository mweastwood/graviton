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
   - Check for `.githooks/pre-commit` in the repository and verify pre-commit checks pass prior to committing code or filing findings.
   - For **Bug Sweeps**: Search `/workspace` for unhandled exceptions, resource leaks, missing validation, edge cases, broken error paths, or race conditions.
   - For **Quality Sweeps**: Analyze file sizes, long functions, high complexity, missing docstrings/comments, redundant computations, or tightly coupled modules.
   - For **Security Sweeps**: Scan package dependencies for known CVEs, search for hardcoded secrets/credentials, SQL/shell injection vectors, insecure deserialization, or weak cryptography.
   - For **Test Sweeps**: Identify untested modules, missing assertions, untested critical utility functions, or untested error-handling branches.
   - For **Typing Sweeps**: Inspect codebase for missing type annotations, `Any` overuse, deprecated constructs, and type signature mismatches.
   - For **Dead Code Sweeps**: Search for unreachable code branches, obsolete private helper functions, unused configuration options, or dead exports.
   - For **Docs Audits**: Check for drift between implementation and documentation (`README.md`, `docs/ARCHITECTURE.md`, docstrings, CLI arguments, configuration schemas).

3. **Deduplication Check**:
   - Before filing a new issue, compare proposed finding titles and topics against existing open issues.
   - If an open issue already addresses the finding, skip filing to prevent duplicates.

4. **Automated Issue Filing**:
   - File new issues using `gh issue create`:
     - Bug Sweep: `gh issue create --title "[Bug Sweep] <summary>" --body "<details & repro>\n\n<!-- antigravity-auto-reply -->\n<!-- graviton:codebase_auditor -->" --label "bug"`
     - Quality Sweep: `gh issue create --title "[Quality Sweep] <scope>: <recommendation>" --body "<rationale & code snippet>\n\n<!-- antigravity-auto-reply -->\n<!-- graviton:codebase_auditor -->" --label "enhancement"`
     - Security Sweep: `gh issue create --title "[Security Sweep] <summary>" --body "<details & remediation>\n\n<!-- antigravity-auto-reply -->\n<!-- graviton:codebase_auditor -->" --label "bug"`
     - Test Sweep: `gh issue create --title "[Test Sweep] Add unit test coverage for <module>" --body "<rationale & proposed test cases>\n\n<!-- antigravity-auto-reply -->\n<!-- graviton:codebase_auditor -->" --label "enhancement"`
     - Typing Sweep: `gh issue create --title "[Typing Sweep] Add strict type annotations to <module>" --body "<rationale & code snippet>\n\n<!-- antigravity-auto-reply -->\n<!-- graviton:codebase_auditor -->" --label "enhancement"`
     - Dead Code Sweep: `gh issue create --title "[Dead Code Sweep] Remove obsolete <symbol/module>" --body "<location and evidence of non-usage>\n\n<!-- antigravity-auto-reply -->\n<!-- graviton:codebase_auditor -->" --label "enhancement"`
     - Docs Audit: `gh issue create --title "[Docs Audit] <scope>: <discrepancy>" --body "<discrepancy details & suggested fix>\n\n<!-- antigravity-auto-reply -->\n<!-- graviton:codebase_auditor -->" --label "documentation"`

5. **Safety Guardrails & Bot Marker**:
   - Always append `<!-- antigravity-auto-reply -->` and `<!-- graviton:codebase_auditor -->` to all filed issue bodies to maintain loop protection.
