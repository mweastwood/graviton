#!/usr/bin/env python3
"""
Unit tests for bin/graviton-plugin-install
"""

import argparse
import os
import shutil
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


if __name__ == "__main__":
    unittest.main()
