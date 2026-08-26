"""
Unit tests for bin/run_listener.sh
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_LISTENER_PATH = REPO_ROOT / "bin" / "run_listener.sh"


class TestRunListener(unittest.TestCase):

    def setUp(self):
        self.assertTrue(RUN_LISTENER_PATH.exists(), f"{RUN_LISTENER_PATH} does not exist")

    def _create_mock_binary(self, bin_dir: Path, name: str, body: str = '#!/usr/bin/env bash\necho "MOCK $0: $@"\n') -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        binary_path = bin_dir / name
        binary_path.write_text(body)
        binary_path.chmod(binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return binary_path

    def _setup_minimal_bin(self, target_dir: Path) -> Path:
        """Create a minimal bin directory with symlinks to bash and env so #!/usr/bin/env bash works cleanly in isolated PATH tests."""
        min_bin = target_dir / "min_bin"
        min_bin.mkdir(parents=True, exist_ok=True)
        for tool in ["bash", "env"]:
            tool_path = Path(f"/usr/bin/{tool}")
            if not tool_path.exists():
                tool_path = Path(f"/bin/{tool}")
            symlink_path = min_bin / tool
            if not symlink_path.exists():
                symlink_path.symlink_to(tool_path)
        return min_bin

    def test_missing_smee_url_argument(self):
        """Test execution when no arguments are provided to ensure it prints usage instructions and exits with exit code 1."""
        res = subprocess.run(
            [str(RUN_LISTENER_PATH)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 1)
        output = res.stdout + res.stderr
        self.assertIn("Usage:", output)
        self.assertIn("<SMEE_URL> [TARGET_PORT]", output)

    def test_standard_smee_cli_in_path(self):
        """Test execution when smee command is available on system PATH."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            min_bin = self._setup_minimal_bin(tmp_path)
            mock_bin = tmp_path / "mock_bin"
            self._create_mock_binary(mock_bin, "smee")

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{min_bin}"

            res = subprocess.run(
                [str(RUN_LISTENER_PATH), "https://smee.io/channel", "8080"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0)
            output = res.stdout
            self.assertIn("--url https://smee.io/channel", output)
            self.assertIn("--path /", output)
            self.assertIn("--port 8080", output)

    def test_npm_global_fallback(self):
        """Test fallback to ${HOME}/.npm-global/bin/smee when smee is not in PATH."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_home = tmp_path / "home"
            fake_home.mkdir()
            npm_bin = fake_home / ".npm-global" / "bin"
            mock_smee = self._create_mock_binary(npm_bin, "smee")

            min_bin = self._setup_minimal_bin(tmp_path)

            env = os.environ.copy()
            env["HOME"] = str(fake_home)
            env["PATH"] = str(min_bin)

            res = subprocess.run(
                [str(RUN_LISTENER_PATH), "https://smee.io/channel", "9000"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0)
            output = res.stdout
            self.assertIn("--url https://smee.io/channel", output)
            self.assertIn("--path /", output)
            self.assertIn("--port 9000", output)
            self.assertIn(str(mock_smee), output)

    def test_npx_fallback(self):
        """Test fallback to npx smee when smee binary is not installed directly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_home = tmp_path / "home"
            fake_home.mkdir()

            min_bin = self._setup_minimal_bin(tmp_path)
            mock_bin = tmp_path / "mock_bin"
            self._create_mock_binary(mock_bin, "npx")

            env = os.environ.copy()
            env["HOME"] = str(fake_home)
            env["PATH"] = f"{mock_bin}:{min_bin}"

            res = subprocess.run(
                [str(RUN_LISTENER_PATH), "https://smee.io/channel"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0)
            output = res.stdout
            self.assertIn("smee --url https://smee.io/channel --path / --port 8000", output)

    def test_missing_cli_error(self):
        """Test exit code 1 and error logging when neither smee nor npx is installed on the system."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_home = tmp_path / "home"
            fake_home.mkdir()

            min_bin = self._setup_minimal_bin(tmp_path)

            env = os.environ.copy()
            env["HOME"] = str(fake_home)
            env["PATH"] = str(min_bin)

            res = subprocess.run(
                [str(RUN_LISTENER_PATH), "https://smee.io/channel"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 1)
            output = res.stdout + res.stderr
            self.assertIn("Error: 'smee' CLI tool not found", output)

    def test_default_target_port(self):
        """Test that target port defaults to 8000 when omitted."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            min_bin = self._setup_minimal_bin(tmp_path)
            mock_bin = tmp_path / "mock_bin"
            self._create_mock_binary(mock_bin, "smee")

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{min_bin}"

            res = subprocess.run(
                [str(RUN_LISTENER_PATH), "https://smee.io/channel"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0)
            output = res.stdout
            self.assertIn("--port 8000", output)


if __name__ == "__main__":
    unittest.main()
