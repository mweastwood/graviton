#!/usr/bin/env python3
"""
Unit tests for bin/graviton-plugin-install
"""

import argparse
import os
import shutil
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_INSTALL_SCRIPT = REPO_ROOT / "bin" / "graviton-plugin-install"

plugin_installer = SourceFileLoader(
    "graviton_plugin_install", str(PLUGIN_INSTALL_SCRIPT)
).load_module()


class TestPluginInstall(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.target_dir = self.tmp_path / "plugins" / "graviton"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_argparse_mutual_exclusion(self):
        parser = argparse.ArgumentParser()
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--workspace", action="store_true")
        group.add_argument("--global", dest="is_global", action="store_true")

        # Workspace only
        args = parser.parse_args(["--workspace"])
        self.assertTrue(args.workspace)
        self.assertFalse(args.is_global)

        # Global only
        args = parser.parse_args(["--global"])
        self.assertTrue(args.is_global)
        self.assertFalse(args.workspace)

        # Both flags raise SystemExit
        with self.assertRaises(SystemExit):
            with patch("sys.stderr"):
                parser.parse_args(["--workspace", "--global"])

    def test_install_workspace_symlink(self):
        success = plugin_installer.install_plugin(
            self.target_dir, use_symlink=True, is_global=False
        )
        self.assertTrue(success)
        self.assertTrue(self.target_dir.is_symlink())
        link_dest = os.readlink(self.target_dir)
        self.assertFalse(os.path.isabs(link_dest))
        self.assertTrue((self.target_dir / "bin" / "graviton-sidecar").exists())
        self.assertTrue((self.target_dir / "bin" / "graviton-mcp").exists())

    def test_install_global_symlink(self):
        success = plugin_installer.install_plugin(
            self.target_dir, use_symlink=True, is_global=True
        )
        self.assertTrue(success)
        self.assertTrue(self.target_dir.is_symlink())
        link_dest = os.readlink(self.target_dir)
        # Global symlink must be absolute to avoid fragile multi-level relative links
        self.assertTrue(os.path.isabs(link_dest))
        self.assertEqual(Path(link_dest), plugin_installer.PLUGIN_SRC.resolve())
        self.assertTrue((self.target_dir / "bin" / "graviton-sidecar").exists())
        self.assertTrue((self.target_dir / "bin" / "graviton-mcp").exists())

    def test_install_copy_includes_executables(self):
        success = plugin_installer.install_plugin(
            self.target_dir, use_symlink=False, is_global=False
        )
        self.assertTrue(success)
        self.assertFalse(self.target_dir.is_symlink())
        self.assertTrue(self.target_dir.is_dir())
        sidecar_exe = self.target_dir / "bin" / "graviton-sidecar"
        mcp_exe = self.target_dir / "bin" / "graviton-mcp"
        self.assertTrue(sidecar_exe.exists())
        self.assertTrue(mcp_exe.exists())
        self.assertTrue(os.access(sidecar_exe, os.X_OK))
        self.assertTrue(os.access(mcp_exe, os.X_OK))

    def test_main_cli_workspace_default(self):
        with patch.object(
            plugin_installer, "install_plugin", return_value=True
        ) as mock_install:
            with patch("sys.stdout"):
                with patch.object(sys, "argv", ["graviton-plugin-install"]):
                    plugin_installer.main()
                mock_install.assert_called_once_with(
                    plugin_installer.REPO_ROOT / ".agents" / "plugins" / "graviton",
                    use_symlink=True,
                    is_global=False,
                )

                mock_install.reset_mock()
                with patch.object(
                    sys, "argv", ["graviton-plugin-install", "--workspace"]
                ):
                    plugin_installer.main()
                mock_install.assert_called_once_with(
                    plugin_installer.REPO_ROOT / ".agents" / "plugins" / "graviton",
                    use_symlink=True,
                    is_global=False,
                )

    def test_main_cli_global(self):
        with patch.object(
            plugin_installer, "install_plugin", return_value=True
        ) as mock_install:
            with patch("sys.stdout"):
                with patch.object(
                    sys, "argv", ["graviton-plugin-install", "--global"]
                ):
                    plugin_installer.main()
                mock_install.assert_called_once_with(
                    Path.home() / ".gemini" / "config" / "plugins" / "graviton",
                    use_symlink=True,
                    is_global=True,
                )

    def test_main_cli_copy_flag(self):
        with patch.object(
            plugin_installer, "install_plugin", return_value=True
        ) as mock_install:
            with patch("sys.stdout"):
                with patch.object(
                    sys, "argv", ["graviton-plugin-install", "--copy"]
                ):
                    plugin_installer.main()
                mock_install.assert_called_once_with(
                    plugin_installer.REPO_ROOT / ".agents" / "plugins" / "graviton",
                    use_symlink=False,
                    is_global=False,
                )

    def test_ensure_plugin_executables_generates_wrapper(self):
        clean_target = self.tmp_path / "clean_plugin"
        mock_repo = self.tmp_path / "mock_graviton"
        plugin_installer._ensure_plugin_executables(clean_target, repo_root=mock_repo)

        bin_dir = clean_target / "bin"
        self.assertTrue(bin_dir.is_dir())

        for exe_name in ["graviton-sidecar", "graviton-mcp"]:
            exe_file = bin_dir / exe_name
            self.assertTrue(exe_file.exists())
            self.assertTrue(os.access(exe_file, os.X_OK))

            content = exe_file.read_text()
            self.assertTrue(content.startswith("#!/usr/bin/env python3"))
            self.assertIn('REPO_ROOT = Path(os.environ.get("GRAVITON_ROOT"', content)
            self.assertIn(str(mock_repo.resolve()), content)
            self.assertIn("for parent in [current.parent", content)
            self.assertIn("os.execv", content)

    def test_install_plugin_cleans_existing_symlink(self):
        dummy_file = self.tmp_path / "dummy.txt"
        dummy_file.write_text("dummy")
        self.target_dir.parent.mkdir(parents=True, exist_ok=True)
        self.target_dir.symlink_to(dummy_file)
        self.assertTrue(self.target_dir.is_symlink())

        with patch("sys.stdout"):
            success = plugin_installer.install_plugin(self.target_dir, use_symlink=True)
        self.assertTrue(success)
        self.assertTrue(self.target_dir.is_symlink())
        self.assertNotEqual(os.readlink(self.target_dir), str(dummy_file))

    def test_install_plugin_cleans_existing_directory(self):
        self.target_dir.mkdir(parents=True, exist_ok=True)
        (self.target_dir / "old_file.txt").write_text("old content")
        self.assertTrue(self.target_dir.is_dir())

        with patch("sys.stdout"):
            success = plugin_installer.install_plugin(self.target_dir, use_symlink=True)
        self.assertTrue(success)
        self.assertTrue(self.target_dir.is_symlink())
        self.assertFalse((self.target_dir / "old_file.txt").exists())

    def test_install_plugin_cleans_existing_file(self):
        self.target_dir.parent.mkdir(parents=True, exist_ok=True)
        self.target_dir.write_text("regular file")
        self.assertTrue(self.target_dir.is_file())

        with patch("sys.stdout"):
            success = plugin_installer.install_plugin(self.target_dir, use_symlink=True)
        self.assertTrue(success)
        self.assertTrue(self.target_dir.is_symlink())
        self.assertTrue(self.target_dir.is_dir())

    def test_install_plugin_symlink_failure_falls_back_to_copy(self):
        with patch.object(
            Path, "symlink_to", side_effect=OSError("Cross-device link")
        ):
            with patch("sys.stdout"):
                success = plugin_installer.install_plugin(
                    self.target_dir, use_symlink=True, is_global=False
                )
            self.assertTrue(success)
            self.assertFalse(self.target_dir.is_symlink())
            self.assertTrue(self.target_dir.is_dir())
            self.assertTrue((self.target_dir / "bin" / "graviton-sidecar").exists())
            self.assertTrue((self.target_dir / "bin" / "graviton-mcp").exists())


if __name__ == "__main__":
    unittest.main()
