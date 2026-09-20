"""
Unit tests for agent specs and dedicated skills validation.
"""

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "plugin" / "agents"
SKILLS_DIR = REPO_ROOT / "plugin" / "skills"


def parse_agent_md(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if not match:
        raise ValueError(f"Invalid agent.md format in {path}")
    frontmatter_raw, body = match.groups()
    data = {}
    for line in frontmatter_raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            data[k.strip()] = v.strip()
    data["system_prompt"] = body.strip()
    return data


class TestAgentSkillsMapping(unittest.TestCase):

    def test_agent_specs_exist_and_valid_format(self):
        agent_files = list(AGENTS_DIR.glob("*/agent.md"))
        self.assertGreater(len(agent_files), 0, "No agent spec files found in plugin/agents/")
        for spec_file in agent_files:
            data = parse_agent_md(spec_file)
            self.assertIn("name", data)
            self.assertIn("description", data)
            self.assertIn("system_prompt", data)

    def test_every_agent_has_dedicated_skill(self):
        agent_files = list(AGENTS_DIR.glob("*/agent.md"))
        for spec_file in agent_files:
            data = parse_agent_md(spec_file)
            agent_name = data["name"]
            # Derive skill directory name from agent name (e.g. code_reviewer -> code-review-guidelines)
            normalized_name = agent_name.replace("_", "-")
            if normalized_name.endswith("-reviewer"):
                skill_dir_name = normalized_name.replace("-reviewer", "-review-guidelines")
            elif not normalized_name.endswith("-guidelines"):
                skill_dir_name = f"{normalized_name}-guidelines"
            else:
                skill_dir_name = normalized_name

            skill_path = SKILLS_DIR / skill_dir_name / "SKILL.md"
            self.assertTrue(
                skill_path.exists(),
                f"Agent '{agent_name}' is missing dedicated skill file at {skill_path}",
            )

    def test_agent_system_prompts_are_lightweight_and_reference_skill(self):
        agent_files = list(AGENTS_DIR.glob("*/agent.md"))
        for spec_file in agent_files:
            data = parse_agent_md(spec_file)
            system_prompt = data["system_prompt"]
            # Verify system prompt references a skill guidelines document
            self.assertIn(
                "skill",
                system_prompt.lower(),
                f"Agent '{data['name']}' system prompt should reference its dedicated skill",
            )
            # Lightweight check: system prompt length under 350 characters
            self.assertLess(
                len(system_prompt),
                350,
                f"Agent '{data['name']}' system prompt is too long ({len(system_prompt)} chars)",
            )

    def test_code_review_guidelines_includes_presubmit_and_mergeability_checks(self):
        skill_path = SKILLS_DIR / "code-review-guidelines" / "SKILL.md"
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Presubmit & CI Status Verification", content)
        self.assertIn("Mergeability Verification", content)
        self.assertIn("gh pr checks", content)
        self.assertIn("gh pr view", content)
        self.assertIn("mergeable", content.lower())

    def test_agent_system_prompts_enforce_bot_marker_signature(self):
        for agent_name in ["code_fixer", "code_reviewer", "issue_triager", "pr_drafter", "codebase_auditor"]:
            spec_file = AGENTS_DIR / agent_name / "agent.md"
            data = parse_agent_md(spec_file)
            system_prompt = data["system_prompt"]
            self.assertIn(
                "<!-- antigravity-auto-reply -->",
                system_prompt,
                f"Agent '{agent_name}' system prompt must require bot signature tag",
            )
            self.assertIn(
                f"<!-- graviton:{agent_name} -->",
                system_prompt,
                f"Agent '{agent_name}' system prompt must require agent signature tag",
            )

    def test_agent_skills_require_bot_marker_signature(self):
        skill_mappings = [
            ("code-fixer-guidelines", "code_fixer"),
            ("code-review-guidelines", "code_reviewer"),
            ("issue-triager-guidelines", "issue_triager"),
            ("pr-drafter-guidelines", "pr_drafter"),
            ("codebase-auditor-guidelines", "codebase_auditor"),
        ]
        for skill_dir_name, agent_name in skill_mappings:
            skill_path = SKILLS_DIR / skill_dir_name / "SKILL.md"
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn(
                "<!-- antigravity-auto-reply -->",
                content,
                f"Skill file {skill_path} must contain bot signature tag",
            )
            self.assertIn(
                f"<!-- graviton:{agent_name} -->",
                content,
                f"Skill file {skill_path} must contain agent signature tag <!-- graviton:{agent_name} -->",
            )

    def test_code_review_guidelines_enforces_formal_pr_reviews(self):
        skill_path = SKILLS_DIR / "code-review-guidelines" / "SKILL.md"
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("gh pr review <pr_number> --request-changes", content)
        self.assertIn("gh pr review <pr_number> --approve", content)
        self.assertIn("gh pr review <pr_number> --comment", content)
        self.assertIn("Do **NOT** use `gh pr comment` or `gh issue comment`", content)

    def test_code_review_guidelines_requires_changes_requested_for_any_findings(self):
        skill_path = SKILLS_DIR / "code-review-guidelines" / "SKILL.md"
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Any Changes Needed", content)
        self.assertIn("minor fixes", content)
        self.assertIn("style tweaks", content)
        self.assertIn("docstrings", content)
        self.assertIn("CHANGES_REQUESTED", content)
        self.assertIn("no code changes at all", content.lower())
        self.assertIn("ignored by the webhook router", content)
        self.assertIn("never** use `--comment`", content)

    def test_code_reviewer_system_prompt_directs_request_changes(self):
        spec_file = AGENTS_DIR / "code_reviewer" / "agent.md"
        data = parse_agent_md(spec_file)
        system_prompt = data["system_prompt"]
        self.assertIn("--request-changes", system_prompt)
        self.assertIn("/fix", system_prompt)
        self.assertIn("gh pr comment", system_prompt)

    def test_code_review_guidelines_includes_author_verification_and_own_pr_rules(self):
        skill_path = SKILLS_DIR / "code-review-guidelines" / "SKILL.md"
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Author Verification & Submission Strategy", content)
        self.assertIn("gh pr view <number> --json author", content)
        self.assertIn("External Author PRs", content)
        self.assertIn("Own PRs (Author is Graviton/Bot)", content)
        self.assertIn("/fix", content)
        self.assertIn("Can't submit review on your own pull request", content)
        self.assertIn("Template for Own PR Review with Changes Needed (`/fix` Trigger)", content)

    def test_code_review_guidelines_includes_templates_for_changes_requested_and_approved(self):
        skill_path = SKILLS_DIR / "code-review-guidelines" / "SKILL.md"
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Review Body Templates", content)
        self.assertIn("Template for CHANGES_REQUESTED", content)
        self.assertIn("Template for APPROVE / NO_CHANGES_NEEDED", content)
        self.assertIn("Code Review Summary: Changes Requested", content)
        self.assertIn("Code Review Summary: Approved", content)
        self.assertIn("Presubmit & CI Status", content)
        self.assertIn("Mergeability Verification", content)
        self.assertIn("Action Items & Required Changes", content)
        self.assertIn("Verification Checklist", content)
        self.assertIn("<!-- antigravity-auto-reply -->", content)

    def test_run_agent_container_mounts_server_level_skills(self):
        script_path = REPO_ROOT / "bin" / "run_agent_container.sh"
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("GRAVITON_ROOT=", content)
        self.assertIn('SKILLS_MOUNT=(-v "${GRAVITON_ROOT}/plugin/skills:/root/.gemini/config/skills:ro")', content)
        self.assertIn('AGENTS_MOUNT=(-v "${GRAVITON_ROOT}/plugin/agents:/root/.gemini/config/agents:ro")', content)
        self.assertNotIn("${TEMP_WORKSPACE}/skills:/root/.gemini/config/skills:ro", content)

    def test_codebase_auditor_guidelines_includes_flaky_and_low_quality_test_checks(self):
        skill_path = SKILLS_DIR / "codebase-auditor-guidelines" / "SKILL.md"
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Flaky Tests", content)
        self.assertIn("Low-Quality Tests", content)
        self.assertIn("Root-Cause Analysis & Resolution Suggestions", content)
        self.assertIn("time.sleep()", content)
        self.assertIn("assertTrue(True)", content)
        self.assertIn("Fix flaky/low-quality test in <module>: <test_name>", content)


if __name__ == "__main__":
    unittest.main()



