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


if __name__ == "__main__":
    unittest.main()
