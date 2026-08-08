---
name: code-review-guidelines
description: >-
  Use this skill when performing PR code reviews to verify security, error handling,
  and code quality standards.
---

# Code Review Guidelines

When reviewing pull requests:

1. **Security & Secrets**: Check for exposed API keys, credentials, or unsafe dynamic commands.
2. **Error Handling**: Verify that exceptions are caught and handled gracefully without silent failures.
3. **Test Coverage**: Ensure new code components have corresponding unit tests.
