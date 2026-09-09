"""
Unit tests for lib/reactions.py
"""

import json
import os
import subprocess
import threading
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

from lib.reactions import (
    get_reaction_endpoint,
    post_emoji_reaction,
    post_emoji_reaction_async,
)


class TestReactions(unittest.TestCase):

    def test_get_reaction_endpoint_issue_comment(self):
        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "comment": {"id": 101},
            "issue": {"number": 99},
        }
        endpoint = get_reaction_endpoint("issue_comment", payload)
        self.assertEqual(endpoint, "/repos/mweastwood/graviton/issues/comments/101/reactions")

    def test_get_reaction_endpoint_pull_request_review_comment(self):
        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "comment": {"id": 202},
            "pull_request": {"number": 15},
        }
        endpoint = get_reaction_endpoint("pull_request_review_comment", payload)
        self.assertEqual(endpoint, "/repos/mweastwood/graviton/pulls/comments/202/reactions")

    def test_get_reaction_endpoint_pull_request_review_with_comment_id(self):
        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "review": {"id": 303, "comment_id": 404},
            "pull_request": {"number": 20},
        }
        endpoint = get_reaction_endpoint("pull_request_review", payload)
        self.assertEqual(endpoint, "/repos/mweastwood/graviton/pulls/comments/404/reactions")

    def test_get_reaction_endpoint_pull_request_review_without_comment_id(self):
        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "review": {"id": 303},
            "pull_request": {"number": 20},
        }
        endpoint = get_reaction_endpoint("pull_request_review", payload)
        self.assertEqual(endpoint, "/repos/mweastwood/graviton/issues/20/reactions")

    def test_get_reaction_endpoint_issues(self):
        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        endpoint = get_reaction_endpoint("issues", payload)
        self.assertEqual(endpoint, "/repos/mweastwood/graviton/issues/99/reactions")

    def test_get_reaction_endpoint_pull_request(self):
        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "pull_request": {"number": 42},
        }
        endpoint = get_reaction_endpoint("pull_request", payload)
        self.assertEqual(endpoint, "/repos/mweastwood/graviton/issues/42/reactions")

    def test_get_reaction_endpoint_repo_owner_name_fallback(self):
        payload = {
            "repository": {"owner": {"login": "owner1"}, "name": "repo1"},
            "issue": {"number": 5},
        }
        endpoint = get_reaction_endpoint("issues", payload)
        self.assertEqual(endpoint, "/repos/owner1/repo1/issues/5/reactions")

    def test_get_reaction_endpoint_unsupported_event(self):
        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
        }
        self.assertIsNone(get_reaction_endpoint("push", payload))
        self.assertIsNone(get_reaction_endpoint("ping", payload))

    def test_get_reaction_endpoint_missing_repo_or_payload(self):
        self.assertIsNone(get_reaction_endpoint("issues", {}))
        self.assertIsNone(get_reaction_endpoint("issues", None))
        self.assertIsNone(get_reaction_endpoint("issues", {"issue": {"number": 1}}))

    @patch("subprocess.run")
    def test_post_emoji_reaction_via_gh_cli_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        res = post_emoji_reaction("issues", payload, reaction="eyes")
        self.assertTrue(res)
        mock_run.assert_called_once_with(
            ["gh", "api", "-X", "POST", "/repos/mweastwood/graviton/issues/99/reactions", "-f", "content=eyes"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_post_emoji_reaction_fallback_to_urllib_success(self, mock_run, mock_urlopen):
        mock_run.side_effect = FileNotFoundError("gh cli not found")
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}):
            res = post_emoji_reaction("issues", payload, reaction="eyes")

        self.assertTrue(res)
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.github.com/repos/mweastwood/graviton/issues/99/reactions")
        self.assertEqual(req.headers["Authorization"], "Bearer test_token")

    @patch("subprocess.run")
    def test_post_emoji_reaction_graceful_failure_no_token(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="Not found")
        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        with patch.dict(os.environ, {}, clear=True):
            res = post_emoji_reaction("issues", payload)

        self.assertFalse(res)

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_post_emoji_reaction_urllib_network_url_error(self, mock_run, mock_urlopen):
        mock_run.side_effect = FileNotFoundError("gh cli not found")
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}):
            with self.assertLogs("graviton.reactions", level="WARNING") as cm:
                res = post_emoji_reaction("issues", payload, reaction="+1")

        self.assertFalse(res)
        mock_urlopen.assert_called_once()
        self.assertTrue(any("urllib request failed for reaction" in line and "connection refused" in line for line in cm.output))

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_post_emoji_reaction_urllib_network_error(self, mock_run, mock_urlopen):
        mock_run.return_value = MagicMock(returncode=1, stderr="Not found")
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        with patch.dict(os.environ, {"GH_TOKEN": "test_token"}):
            with self.assertLogs("graviton.reactions", level="WARNING") as cm:
                res = post_emoji_reaction("issues", payload, reaction="+1")

        self.assertFalse(res)
        mock_urlopen.assert_called_once()
        self.assertTrue(any("urllib request failed for reaction" in line and "connection refused" in line for line in cm.output))

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_post_emoji_reaction_urllib_http_error(self, mock_run, mock_urlopen):
        mock_run.side_effect = FileNotFoundError("gh cli not found")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.github.com/repos/mweastwood/graviton/issues/99/reactions",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=None,
        )

        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}):
            with self.assertLogs("graviton.reactions", level="WARNING") as cm:
                res = post_emoji_reaction("issues", payload, reaction="eyes")

        self.assertFalse(res)
        mock_urlopen.assert_called_once()
        self.assertTrue(any("urllib request failed for reaction" in line and "Internal Server Error" in line for line in cm.output))

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_post_emoji_reaction_urllib_http_403(self, mock_run, mock_urlopen):
        mock_run.return_value = MagicMock(returncode=1, stderr="gh failed")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.github.com/repos/mweastwood/graviton/issues/99/reactions",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )

        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}):
            with self.assertLogs("graviton.reactions", level="WARNING") as cm:
                res = post_emoji_reaction("issues", payload, reaction="eyes")

        self.assertFalse(res)
        mock_urlopen.assert_called_once()
        self.assertTrue(any("urllib request failed for reaction" in line and "Forbidden" in line for line in cm.output))

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_post_emoji_reaction_urllib_non_2xx_status(self, mock_run, mock_urlopen):
        mock_run.side_effect = FileNotFoundError("gh cli not found")
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}):
            with self.assertLogs("graviton.reactions", level="WARNING") as cm:
                res = post_emoji_reaction("issues", payload, reaction="eyes")

        self.assertFalse(res)
        mock_urlopen.assert_called_once()
        self.assertTrue(any("urllib returned HTTP status 404" in line for line in cm.output))

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_post_emoji_reaction_urllib_generic_exception(self, mock_run, mock_urlopen):
        mock_run.side_effect = FileNotFoundError("gh cli not found")
        mock_urlopen.side_effect = ConnectionResetError("connection reset by peer")

        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}):
            with self.assertLogs("graviton.reactions", level="WARNING") as cm:
                res = post_emoji_reaction("issues", payload, reaction="rocket")

        self.assertFalse(res)
        mock_urlopen.assert_called_once()
        self.assertTrue(any("urllib request failed for reaction" in line and "connection reset by peer" in line for line in cm.output))

    @patch("lib.reactions.post_emoji_reaction")
    def test_post_emoji_reaction_async(self, mock_post):
        called = threading.Event()
        mock_post.side_effect = lambda *args, **kwargs: called.set()

        payload = {
            "repository": {"full_name": "mweastwood/graviton"},
            "issue": {"number": 99},
        }
        thread = post_emoji_reaction_async("issues", payload, reaction="eyes")
        self.assertTrue(called.wait(timeout=10.0), "Async reaction thread did not execute target function")
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "Async reaction thread did not terminate")
        mock_post.assert_called_once_with("issues", payload, "eyes")


if __name__ == "__main__":
    unittest.main()
