"""Unit tests for lib/projects.py (Project discovery and workspace trust helpers)."""

import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from lib.projects import (
    DEFAULT_PROJECT_NAME,
    _extract_uri,
    ensure_default_project,
    ensure_workspace_trusted,
    find_project_for_repo,
)


class TestProjectDiscovery(unittest.TestCase):
    """Test project discovery and creation helpers in lib/projects."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp_dir.name)
        self.projects_dir = self.base / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.repo_dir = self.base / "my_repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_extract_uri(self):
        self.assertIsNone(_extract_uri(None))
        self.assertIsNone(_extract_uri("not-a-dict"))
        self.assertIsNone(_extract_uri({}))
        self.assertEqual(_extract_uri({"folderUri": "file:///path"}), "file:///path")
        self.assertEqual(_extract_uri({"gitFolder": {"folderUri": "file:///git/path"}}), "file:///git/path")
        self.assertIsNone(_extract_uri({"gitFolder": None}))
        self.assertIsNone(_extract_uri({"gitFolder": {"folderUri": 123}}))

    def test_ensure_default_project_creates_and_reuses(self):
        # 1. Create project file
        pid, pname = ensure_default_project(self.projects_dir, self.repo_dir, name="Test Project")
        self.assertEqual(pname, "Test Project")
        proj_file = self.projects_dir / f"{pid}.json"
        self.assertTrue(proj_file.exists())

        data = json.loads(proj_file.read_text(encoding="utf-8"))
        self.assertEqual(data["id"], pid)
        self.assertEqual(data["name"], "Test Project")
        uris = [_extract_uri(r) for r in data["projectResources"]["resources"]]
        self.assertIn("file:///workspace", uris)
        self.assertIn(f"file://{self.repo_dir.resolve()}", uris)

        # 2. Reuse project file
        pid2, pname2 = ensure_default_project(self.projects_dir, self.repo_dir, name="Test Project")
        self.assertEqual(pid2, pid)
        self.assertEqual(pname2, pname)

        # 3. Add new repo to existing project
        new_repo = self.base / "another_repo"
        new_repo.mkdir()
        pid3, pname3 = ensure_default_project(self.projects_dir, new_repo, name="Test Project")
        self.assertEqual(pid3, pid)
        updated_data = json.loads(proj_file.read_text(encoding="utf-8"))
        updated_uris = [_extract_uri(r) for r in updated_data["projectResources"]["resources"]]
        self.assertIn(f"file://{new_repo.resolve()}", updated_uris)

    def test_ensure_default_project_str_paths(self):
        str_projects_dir = str(self.projects_dir / "str_projects")
        str_repo_dir = str(self.repo_dir)
        pid, pname = ensure_default_project(str_projects_dir, str_repo_dir, name="String Path Project")
        self.assertEqual(pname, "String Path Project")
        proj_file = Path(str_projects_dir) / f"{pid}.json"
        self.assertTrue(proj_file.exists())

        data = json.loads(proj_file.read_text(encoding="utf-8"))
        self.assertEqual(data["id"], pid)
        self.assertEqual(data["name"], "String Path Project")
        uris = [_extract_uri(r) for r in data["projectResources"]["resources"]]
        self.assertIn("file:///workspace", uris)
        self.assertIn(f"file://{self.repo_dir.resolve()}", uris)

        # Verify reuse with string paths
        pid2, pname2 = ensure_default_project(str_projects_dir, str_repo_dir, name="String Path Project")
        self.assertEqual(pid2, pid)
        self.assertEqual(pname2, pname)

        # Verify string path with repo_path=None
        pid_none, pname_none = ensure_default_project(str_projects_dir, None, name="None Repo Project")
        self.assertEqual(pname_none, "None Repo Project")
        proj_none_file = Path(str_projects_dir) / f"{pid_none}.json"
        self.assertTrue(proj_none_file.exists())

    def test_find_project_for_repo_matching_and_non_matching(self):
        proj_file = self.projects_dir / "alpha.json"
        proj_file.write_text(json.dumps({
            "id": "project-alpha",
            "name": "Project Alpha",
            "projectResources": {
                "resources": [
                    {"gitFolder": {"folderUri": f"file://{self.repo_dir.resolve()}"}}
                ]
            }
        }))

        # Matching repo
        res = find_project_for_repo(self.repo_dir, config_dir=self.projects_dir)
        self.assertEqual(res, ("project-alpha", "Project Alpha"))

        # Matching subdirectory of repo
        subdir = self.repo_dir / "subdir"
        subdir.mkdir()
        self.assertEqual(find_project_for_repo(subdir, config_dir=self.projects_dir), ("project-alpha", "Project Alpha"))

        # Non-matching repo
        unrelated = self.base / "unrelated_repo"
        unrelated.mkdir()
        self.assertIsNone(find_project_for_repo(unrelated, config_dir=self.projects_dir))

    def test_find_project_for_repo_preferences(self):
        proj_file = self.projects_dir / "alpha.json"
        proj_file.write_text(json.dumps({
            "id": "project-alpha",
            "name": "Project Alpha",
            "projectResources": {
                "resources": [
                    {"gitFolder": {"folderUri": f"file://{self.repo_dir.resolve()}"}}
                ]
            }
        }))

        unrelated = self.base / "unrelated_repo"
        unrelated.mkdir()

        # Explicit preferred_name_or_id
        self.assertEqual(
            find_project_for_repo(unrelated, config_dir=self.projects_dir, preferred_name_or_id="project-alpha"),
            ("project-alpha", "Project Alpha"),
        )
        self.assertEqual(
            find_project_for_repo(unrelated, config_dir=self.projects_dir, preferred_name_or_id="Project Alpha"),
            ("project-alpha", "Project Alpha"),
        )

        # ANTIGRAVITY_PROJECT env var
        with patch.dict(os.environ, {"ANTIGRAVITY_PROJECT": "project-alpha"}):
            self.assertEqual(
                find_project_for_repo(unrelated, config_dir=self.projects_dir),
                ("project-alpha", "Project Alpha"),
            )

        # GRAVITON_PROJECT_ID env var
        with patch.dict(os.environ, {"GRAVITON_PROJECT_ID": "project-alpha"}):
            self.assertEqual(
                find_project_for_repo(unrelated, config_dir=self.projects_dir),
                ("project-alpha", "Project Alpha"),
            )

    def test_find_project_for_repo_percent_encoded(self):
        spaced_dir = self.base / "spaced repo dir"
        spaced_dir.mkdir()
        proj_file = self.projects_dir / "spaced.json"
        proj_file.write_text(json.dumps({
            "id": "project-spaced",
            "name": "Spaced Project",
            "projectResources": {
                "resources": [
                    {"gitFolder": {"folderUri": f"file://{urllib.parse.quote(str(spaced_dir.resolve()))}"}}
                ]
            }
        }))
        self.assertEqual(
            find_project_for_repo(spaced_dir, config_dir=self.projects_dir),
            ("project-spaced", "Spaced Project"),
        )


class TestEnsureWorkspaceTrusted(unittest.TestCase):
    """Test ensure_workspace_trusted in lib/projects."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cli_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_settings_json_when_missing(self):
        settings_path = self.cli_dir / "settings.json"
        self.assertFalse(settings_path.exists())
        ensure_workspace_trusted(self.cli_dir)
        self.assertTrue(settings_path.exists())
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertIn("/workspace", data.get("trustedWorkspaces", []))

    def test_preserves_existing_trusted_workspaces(self):
        settings_path = self.cli_dir / "settings.json"
        settings_path.write_text(json.dumps({
            "theme": "dark",
            "trustedWorkspaces": ["/some/other/path"],
        }))
        ensure_workspace_trusted(self.cli_dir)
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("theme"), "dark")
        self.assertIn("/some/other/path", data.get("trustedWorkspaces", []))
        self.assertIn("/workspace", data.get("trustedWorkspaces", []))

    def test_handles_corrupt_or_non_dict_json(self):
        settings_path = self.cli_dir / "settings.json"
        settings_path.write_text("invalid json")
        ensure_workspace_trusted(self.cli_dir)
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertIn("/workspace", data.get("trustedWorkspaces", []))


if __name__ == "__main__":
    unittest.main()
