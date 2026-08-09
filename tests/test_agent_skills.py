"""
Unit tests for agent specs and dedicated skills validation.
"""

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
SKILLS_DIR = REPO_ROOT / "skills"


class TestAgentSkillsMapping(unittest.TestCase):

    def test_agent_specs_exist_and_valid_json(self):
        agent_files = list(AGENTS_DIR.glob("*.json"))
        self.assertGreater(len(agent_files), 0, "No agent spec files found in agents/")
        for spec_file in agent_files:
            with open(spec_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("name", data)
            self.assertIn("system_prompt", data)

    def test_every_agent_has_dedicated_skill(self):
        agent_files = list(AGENTS_DIR.glob("*.json"))
        for spec_file in agent_files:
            with open(spec_file, "r", encoding="utf-8") as f:
                data = json.load(f)
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
        agent_files = list(AGENTS_DIR.glob("*.json"))
        for spec_file in agent_files:
            with open(spec_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            system_prompt = data["system_prompt"]
            # Verify system prompt references a skill guidelines document
            self.assertIn(
                "skill",
                system_prompt.lower(),
                f"Agent '{data['name']}' system prompt should reference its dedicated skill",
            )
            # Lightweight check: system prompt length under 300 characters
            self.assertLess(
                len(system_prompt),
                300,
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
        for agent_name in ["code_fixer", "code_reviewer", "issue_triager", "pr_drafter"]:
            spec_file = AGENTS_DIR / f"{agent_name}.json"
            with open(spec_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            system_prompt = data["system_prompt"]
            self.assertIn(
                "<!-- antigravity-auto-reply -->",
                system_prompt,
                f"Agent '{agent_name}' system prompt must require bot signature tag",
            )

    def test_agent_skills_require_bot_marker_signature(self):
        skill_dirs = ["code-fixer-guidelines", "code-review-guidelines", "issue-triager-guidelines", "pr-drafter-guidelines"]
        for skill_dir_name in skill_dirs:
            skill_path = SKILLS_DIR / skill_dir_name / "SKILL.md"
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn(
                "<!-- antigravity-auto-reply -->",
                content,
                f"Skill file {skill_path} must contain bot signature tag",
            )
            self.assertIn("gh pr create", content, f"Skill file {skill_path} must reference gh pr create")
            self.assertIn("gh pr review", content, f"Skill file {skill_path} must reference gh pr review")

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
        spec_file = AGENTS_DIR / "code_reviewer.json"
        with open(spec_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        system_prompt = data["system_prompt"]
        self.assertIn("--request-changes", system_prompt)
        self.assertIn("never use --comment for actionable findings", system_prompt)

    def test_code_review_guidelines_includes_templates_for_changes_requested_and_approved(self):
        skill_path = SKILLS_DIR / "code-review-guidelines" / "SKILL.md"
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Review Body Templates", content)
        self.assertIn("Template for CHANGES_REQUESTED", content)
        self.assertIn("Template for APPROVE / NO_CHANGES_NEEDED", content)
        self.assertIn("Code Review Summary: Changes Requested", content)
        self.assertIn("Code Review Summary: Approved", content)


if __name__ == "__main__":
    unittest.main()


