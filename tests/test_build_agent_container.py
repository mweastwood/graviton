"""
Unit tests for bin/build_agent_container.sh
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_AGENT_CONTAINER_PATH = REPO_ROOT / "bin" / "build_agent_container.sh"


class TestBuildAgentContainer(unittest.TestCase):

    def setUp(self):
        self.assertTrue(BUILD_AGENT_CONTAINER_PATH.exists(), f"{BUILD_AGENT_CONTAINER_PATH} does not exist")
        self.assertTrue(
            os.access(BUILD_AGENT_CONTAINER_PATH, os.X_OK),
            f"{BUILD_AGENT_CONTAINER_PATH} is not executable",
        )

    def _create_mock_binary(
        self,
        bin_dir: Path,
        name: str,
        body: str = '#!/usr/bin/env bash\necho "MOCK $0: $@"\n',
    ) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        binary_path = bin_dir / name
        binary_path.write_text(body)
        binary_path.chmod(binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return binary_path

    def _setup_minimal_bin(self, target_dir: Path) -> Path:
        """Create a minimal bin directory with symlinks to host utilities so isolated subshells function cleanly."""
        min_bin = target_dir / "min_bin"
        min_bin.mkdir(parents=True, exist_ok=True)
        for tool in ["bash", "env", "dirname", "pwd"]:
            resolved = shutil.which(tool)
            if resolved:
                tool_path = Path(resolved)
                symlink_path = min_bin / tool
                if not symlink_path.exists():
                    symlink_path.symlink_to(tool_path)
        return min_bin

    def test_default_image_tagging(self):
        """Verify running bin/build_agent_container.sh without arguments invokes docker build with default tag and root context."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            min_bin = self._setup_minimal_bin(tmp_path)
            mock_bin = tmp_path / "mock_bin"
            self._create_mock_binary(mock_bin, "docker")

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{min_bin}"

            res = subprocess.run(
                [str(BUILD_AGENT_CONTAINER_PATH)],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(res.returncode, 0)
            self.assertIn("Building Antigravity Agent Docker container image: antigravity-agent:latest...", res.stdout)
            self.assertIn(
                f"build -t antigravity-agent:latest -f {REPO_ROOT}/Dockerfile {REPO_ROOT}",
                res.stdout,
            )
            self.assertIn("Build complete!", res.stdout)
            self.assertIn("Run an agent container with: bin/run_agent_container.sh [AGENT_NAME] <PROMPT>", res.stdout)

    def test_custom_image_tagging(self):
        """Verify passing a custom image name/tag propagates -t <tag> to docker build."""
        custom_tags = [
            "custom-agent:v1.2.3",
            "my-registry.io/org/agent:beta",
        ]
        for tag in custom_tags:
            with self.subTest(tag=tag):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_path = Path(tmp_dir)
                    min_bin = self._setup_minimal_bin(tmp_path)
                    mock_bin = tmp_path / "mock_bin"
                    self._create_mock_binary(mock_bin, "docker")

                    env = os.environ.copy()
                    env["PATH"] = f"{mock_bin}:{min_bin}"

                    res = subprocess.run(
                        [str(BUILD_AGENT_CONTAINER_PATH), tag],
                        capture_output=True,
                        text=True,
                        env=env,
                    )

                    self.assertEqual(res.returncode, 0)
                    self.assertIn(f"Building Antigravity Agent Docker container image: {tag}...", res.stdout)
                    self.assertIn(
                        f"build -t {tag} -f {REPO_ROOT}/Dockerfile {REPO_ROOT}",
                        res.stdout,
                    )
                    self.assertIn("Build complete!", res.stdout)

    def test_docker_build_failure_propagation(self):
        """Verify non-zero exit codes from docker build fail the script immediately due to set -euo pipefail."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            min_bin = self._setup_minimal_bin(tmp_path)
            mock_bin = tmp_path / "mock_bin"
            failing_docker = (
                '#!/usr/bin/env bash\n'
                'echo "Error: docker daemon not reachable" >&2\n'
                'exit 42\n'
            )
            self._create_mock_binary(mock_bin, "docker", body=failing_docker)

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{min_bin}"

            res = subprocess.run(
                [str(BUILD_AGENT_CONTAINER_PATH)],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(res.returncode, 42)
            self.assertIn("Error: docker daemon not reachable", res.stderr)
            self.assertNotIn("Build complete!", res.stdout)

    def test_script_directory_resolution_independent_of_cwd(self):
        """Verify SCRIPT_DIR and REPO_ROOT resolve accurately when invoked from a different current working directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            min_bin = self._setup_minimal_bin(tmp_path)
            mock_bin = tmp_path / "mock_bin"
            self._create_mock_binary(mock_bin, "docker")

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{min_bin}"

            sub_cwd = tmp_path / "nested" / "working" / "dir"
            sub_cwd.mkdir(parents=True, exist_ok=True)

            res = subprocess.run(
                [str(BUILD_AGENT_CONTAINER_PATH), "isolated-tag:test"],
                capture_output=True,
                text=True,
                cwd=str(sub_cwd),
                env=env,
            )

            self.assertEqual(res.returncode, 0)
            self.assertIn(
                f"build -t isolated-tag:test -f {REPO_ROOT}/Dockerfile {REPO_ROOT}",
                res.stdout,
            )
            self.assertIn("Build complete!", res.stdout)


if __name__ == "__main__":
    unittest.main()
