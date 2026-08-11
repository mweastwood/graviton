"""
Unit tests for lib/pr_tracker.py (PRTracker).
"""

import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.pr_tracker import PRTracker


class TestPRTracker(unittest.TestCase):

    def test_add_and_remove_approved_pr(self):
        tracker = PRTracker()
        self.assertEqual(tracker.get_approved_prs(), [])

        # Add PR #42 with string author and int number
        tracker.add_approved_pr(
            number=42,
            title="Add feature X",
            author="mweastwood",
            url="https://github.com/mweastwood/graviton/pull/42",
        )

        # Add PR #10 with dict author and string number
        tracker.add_approved_pr(
            number="10",
            title="Fix bug Y",
            author={"login": "contributor_bob"},
            url="https://github.com/mweastwood/graviton/pull/10",
        )

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 2)
        # Should be sorted by PR number ascending
        self.assertEqual(approved[0]["number"], 10)
        self.assertEqual(approved[0]["author"], "contributor_bob")
        self.assertEqual(approved[1]["number"], 42)
        self.assertEqual(approved[1]["author"], "mweastwood")

        # Remove PR #10
        tracker.remove_approved_pr(10)
        approved_after = tracker.get_approved_prs()
        self.assertEqual(len(approved_after), 1)
        self.assertEqual(approved_after[0]["number"], 42)

        # Remove invalid PR (non-existent or invalid type)
        tracker.remove_approved_pr(999)
        tracker.remove_approved_pr("invalid")
        self.assertEqual(len(tracker.get_approved_prs()), 1)

        # Add invalid PR number (should be ignored safely)
        tracker.add_approved_pr(number="invalid_num", title="Bad number PR")
        self.assertEqual(len(tracker.get_approved_prs()), 1)

    def test_thread_safety(self):
        tracker = PRTracker()
        threads = []

        def add_prs(start_num):
            for i in range(start_num, start_num + 50):
                tracker.add_approved_pr(
                    number=i,
                    title=f"PR #{i}",
                    author="user",
                    url=f"https://github.com/org/repo/pull/{i}",
                )

        def remove_prs(start_num):
            for i in range(start_num, start_num + 25):
                tracker.remove_approved_pr(i)

        for s in [100, 200, 300]:
            t1 = threading.Thread(target=add_prs, args=(s,))
            t2 = threading.Thread(target=remove_prs, args=(s,))
            threads.extend([t1, t2])

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        approved = tracker.get_approved_prs()
        self.assertTrue(all(isinstance(p["number"], int) for p in approved))

    @patch("subprocess.run")
    def test_sync_github_prs_success(self, mock_run):
        mock_output = [
            {
                "number": 42,
                "title": "Approved PR",
                "url": "https://github.com/org/repo/pull/42",
                "author": {"login": "alice"},
                "reviewDecision": "APPROVED",
                "isDraft": False,
            },
            {
                "number": 43,
                "title": "Draft Approved PR",
                "url": "https://github.com/org/repo/pull/43",
                "author": {"login": "bob"},
                "reviewDecision": "APPROVED",
                "isDraft": True,
            },
            {
                "number": 44,
                "title": "Pending Review PR",
                "url": "https://github.com/org/repo/pull/44",
                "author": {"login": "charlie"},
                "reviewDecision": "REVIEW_REQUIRED",
                "isDraft": False,
            },
        ]
        mock_res = MagicMock()
        mock_res.stdout = json.dumps(mock_output)
        mock_res.returncode = 0
        mock_run.return_value = mock_res

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 42)
        self.assertEqual(approved[0]["author"], "alice")

    @patch("subprocess.run")
    def test_sync_github_prs_with_latest_reviews_approval(self, mock_run):
        mock_output = [
            {
                "number": 59,
                "title": "Approved PR via latestReviews",
                "url": "https://github.com/org/repo/pull/59",
                "author": {"login": "dev"},
                "reviewDecision": "",
                "isDraft": False,
                "latestReviews": [
                    {"state": "APPROVED", "author": {"login": "reviewer_alice"}}
                ],
            },
        ]
        mock_res = MagicMock()
        mock_res.stdout = json.dumps(mock_output)
        mock_res.returncode = 0
        mock_run.return_value = mock_res

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 59)
        self.assertEqual(approved[0]["author"], "dev")

        cmd = mock_run.call_args_list[-1][0][0]
        self.assertIn("--limit", cmd)
        self.assertIn("300", cmd)
        self.assertIn("number,title,url,author,reviewDecision,isDraft,latestReviews,comments,reviews", cmd)

    @patch("subprocess.run")
    def test_sync_github_prs_with_latest_reviews_changes_requested_ignored(self, mock_run):
        mock_output = [
            {
                "number": 60,
                "title": "PR with mixed reviews",
                "url": "https://github.com/org/repo/pull/60",
                "author": {"login": "dev"},
                "reviewDecision": "",
                "isDraft": False,
                "latestReviews": [
                    {"state": "APPROVED", "author": {"login": "reviewer1"}},
                    {"state": "CHANGES_REQUESTED", "author": {"login": "reviewer2"}},
                ],
            },
        ]
        mock_res = MagicMock()
        mock_res.stdout = json.dumps(mock_output)
        mock_res.returncode = 0
        mock_run.return_value = mock_res

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 0)

    @patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh"))
    def test_sync_github_prs_handles_exception(self, mock_run):
        tracker = PRTracker()
        tracker.add_approved_pr(1, "Existing PR", "dev", "https://example.com/1")
        
        # Sync fails, existing state should not crash
        tracker.sync_github_prs()
        self.assertEqual(len(tracker.get_approved_prs()), 1)

    def test_multi_repo_approved_prs_tracking(self):
        tracker = PRTracker()
        tracker.add_approved_pr(42, "Feature Alpha", "alice", "https://github.com/owner/repo-alpha/pull/42", repo_full_name="owner/repo-alpha")
        tracker.add_approved_pr(42, "Feature Beta", "bob", "https://github.com/owner/repo-beta/pull/42", repo_full_name="owner/repo-beta")

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 2)
        self.assertEqual(approved[0]["repo_full_name"], "owner/repo-alpha")
        self.assertEqual(approved[0]["number"], 42)
        self.assertEqual(approved[1]["repo_full_name"], "owner/repo-beta")
        self.assertEqual(approved[1]["number"], 42)

        # Remove PR #42 specifically for owner/repo-alpha
        tracker.remove_approved_pr(42, repo_full_name="owner/repo-alpha")
        remaining = tracker.get_approved_prs()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["repo_full_name"], "owner/repo-beta")

    @patch("subprocess.run")
    def test_sync_github_prs_removesuffix_git(self, mock_run):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "reddit"
            repo_dir.mkdir()
            (repo_dir / ".git").mkdir()

            mock_git_res = MagicMock()
            mock_git_res.returncode = 0
            mock_git_res.stdout = "https://github.com/owner/reddit.git\n"

            mock_gh_res = MagicMock()
            mock_gh_res.returncode = 0
            mock_gh_res.stdout = json.dumps([{
                "number": 1,
                "title": "PR 1",
                "url": "https://github.com/owner/reddit/pull/1",
                "author": {"login": "dev"},
                "reviewDecision": "APPROVED",
                "isDraft": False,
            }])

            mock_run.side_effect = [mock_git_res, mock_gh_res]

            tracker = PRTracker()
            tracker.sync_github_prs(repos_dir=Path(tmpdir))

            approved = tracker.get_approved_prs()
            self.assertEqual(len(approved), 1)
            self.assertEqual(approved[0]["repo_full_name"], "owner/reddit")
            self.assertEqual(approved[0]["number"], 1)

    @patch("subprocess.run")
    def test_sync_github_prs_trailing_slash_urls(self, mock_run):
        import tempfile
        for origin_url in ["https://github.com/owner/reddit/", "git@github.com:owner/reddit.git/"]:
            with tempfile.TemporaryDirectory() as tmpdir:
                repo_dir = Path(tmpdir) / "reddit"
                repo_dir.mkdir()
                (repo_dir / ".git").mkdir()

                mock_git_res = MagicMock()
                mock_git_res.returncode = 0
                mock_git_res.stdout = f"{origin_url}\n"

                mock_gh_res = MagicMock()
                mock_gh_res.returncode = 0
                mock_gh_res.stdout = json.dumps([{
                    "number": 1,
                    "title": "PR 1",
                    "url": "https://github.com/owner/reddit/pull/1",
                    "author": {"login": "dev"},
                    "reviewDecision": "APPROVED",
                    "isDraft": False,
                }])

                mock_run.side_effect = [mock_git_res, mock_gh_res]

                tracker = PRTracker()
                tracker.sync_github_prs(repos_dir=Path(tmpdir))

                approved = tracker.get_approved_prs()
                self.assertEqual(len(approved), 1)
                self.assertEqual(approved[0]["repo_full_name"], "owner/reddit")
                self.assertEqual(approved[0]["number"], 1)

    def test_remove_approved_pr_cleans_up_empty_repo_name(self):
        tracker = PRTracker()
        # Add PR with empty repo_full_name
        tracker.add_approved_pr(99, "Legacy PR", "charlie", "https://github.com/owner/repo/pull/99", repo_full_name="")
        self.assertEqual(len(tracker.get_approved_prs()), 1)

        # Removing PR #99 specifying a repo name should clean up the empty repo name entry too
        tracker.remove_approved_pr(99, repo_full_name="owner/repo")
        self.assertEqual(len(tracker.get_approved_prs()), 0)

    @patch("subprocess.run")
    def test_sync_github_prs_resilient_to_single_repo_failure(self, mock_run):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dir1 = Path(tmpdir) / "repo1"
            dir2 = Path(tmpdir) / "repo2"
            dir1.mkdir()
            dir2.mkdir()
            (dir1 / ".git").mkdir()
            (dir2 / ".git").mkdir()

            # repo1 git returns origin, gh raises CalledProcessError
            mock_git1 = MagicMock(returncode=0, stdout="https://github.com/owner/repo1.git\n")
            # repo2 git returns origin, gh succeeds
            mock_git2 = MagicMock(returncode=0, stdout="https://github.com/owner/repo2.git\n")
            mock_gh2 = MagicMock(returncode=0, stdout=json.dumps([{
                "number": 10,
                "title": "Repo2 PR",
                "url": "https://github.com/owner/repo2/pull/10",
                "author": {"login": "dev2"},
                "reviewDecision": "APPROVED",
                "isDraft": False,
            }]))

            # We mock subprocess.run calls in sequence
            def side_effect(cmd, cwd=None, **kwargs):
                cmd_str = " ".join(cmd)
                if "git remote get-url" in cmd_str:
                    if "repo1" in str(cwd):
                        return mock_git1
                    return mock_git2
                if "gh pr list" in cmd_str:
                    if "repo1" in str(cwd):
                        raise subprocess.CalledProcessError(1, "gh", output="", stderr="Failed")
                    return mock_gh2
                return MagicMock(returncode=0, stdout="")

            mock_run.side_effect = side_effect

            tracker = PRTracker()
            tracker.sync_github_prs(repos_dir=Path(tmpdir))

            approved = tracker.get_approved_prs()
            self.assertEqual(len(approved), 1)
            self.assertEqual(approved[0]["repo_full_name"], "owner/repo2")
            self.assertEqual(approved[0]["number"], 10)

    @patch("subprocess.run")
    def test_sync_directory_helper(self, mock_run):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "myrepo"
            d.mkdir()
            (d / ".git").mkdir()

            mock_git_res = MagicMock(returncode=0, stdout="git@github.com:org/myrepo.git\n")
            mock_gh_res = MagicMock(returncode=0, stdout=json.dumps([{
                "number": 101,
                "title": "Helper Test PR",
                "url": "https://github.com/org/myrepo/pull/101",
                "author": {"login": "alice"},
                "reviewDecision": "APPROVED",
                "isDraft": False,
            }]))
            mock_run.side_effect = [mock_git_res, mock_gh_res]

            tracker = PRTracker()
            repo_name, prs = tracker._sync_directory(d)
            self.assertEqual(repo_name, "org/myrepo")
            self.assertEqual(len(prs), 1)
            self.assertEqual(prs[0]["number"], 101)

    @patch("subprocess.run")
    def test_concurrent_multi_repo_sync(self, mock_run):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            num_repos = 5
            dirs = []
            for i in range(1, num_repos + 1):
                r_dir = Path(tmpdir) / f"repo{i}"
                r_dir.mkdir()
                (r_dir / ".git").mkdir()
                dirs.append(r_dir)

            thread_ids = set()
            lock = threading.Lock()
            barrier = threading.Barrier(num_repos)

            def side_effect(cmd, cwd=None, **kwargs):
                cmd_str = " ".join(cmd)
                cwd_str = str(cwd or "")
                if "git remote get-url" in cmd_str:
                    with lock:
                        thread_ids.add(threading.get_ident())
                    barrier.wait(timeout=5.0)
                for i in range(1, num_repos + 1):
                    if f"repo{i}" in cwd_str:
                        if "git remote get-url" in cmd_str:
                            return MagicMock(returncode=0, stdout=f"https://github.com/org/repo{i}.git\n")
                        if "gh pr list" in cmd_str:
                            return MagicMock(returncode=0, stdout=json.dumps([{
                                "number": i * 10,
                                "title": f"PR Repo {i}",
                                "url": f"https://github.com/org/repo{i}/pull/{i*10}",
                                "author": {"login": f"user{i}"},
                                "reviewDecision": "APPROVED",
                                "isDraft": False,
                            }]))
                return MagicMock(returncode=0, stdout="")

            mock_run.side_effect = side_effect

            tracker = PRTracker()
            tracker.sync_github_prs(repos_dir=Path(tmpdir))

            approved = tracker.get_approved_prs()
            self.assertEqual(len(approved), num_repos)
            for i in range(1, num_repos + 1):
                matching = [p for p in approved if p["repo_full_name"] == f"org/repo{i}"]
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0]["number"], i * 10)
            self.assertEqual(len(thread_ids), num_repos)

    @patch("lib.pr_tracker.ThreadPoolExecutor")
    @patch("subprocess.run")
    def test_thread_pool_worker_bounds(self, mock_run, mock_executor_cls):
        import tempfile
        from concurrent.futures import ThreadPoolExecutor
        
        # Test max_workers calculation for 15 repositories (bounded to min(10, len(target_dirs)))
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(15):
                r_dir = Path(tmpdir) / f"repo{i}"
                r_dir.mkdir()
                (r_dir / ".git").mkdir()

            mock_run.return_value = MagicMock(returncode=0, stdout="[]")
            
            # Use real ThreadPoolExecutor context manager inside mock
            real_executor_instances = []
            def executor_factory(max_workers=None):
                executor = ThreadPoolExecutor(max_workers=max_workers)
                real_executor_instances.append((max_workers, executor))
                return executor

            mock_executor_cls.side_effect = executor_factory

            tracker = PRTracker()
            tracker.sync_github_prs(repos_dir=Path(tmpdir))

            self.assertTrue(len(real_executor_instances) > 0)
            self.assertEqual(real_executor_instances[0][0], 10)  # Bound capped at 10 for 15 repos

    @patch("subprocess.run")
    def test_sync_github_prs_with_bot_approval(self, mock_run):
        from lib.security import BOT_MARKER
        mock_output = [
            {
                "number": 107,
                "title": "PR with bot approval summary comment",
                "url": "https://github.com/org/repo/pull/107",
                "author": {"login": "dev"},
                "reviewDecision": "",
                "isDraft": False,
                "comments": [
                    {
                        "body": f"Code Review Summary: Approved ✅\n\n{BOT_MARKER}",
                        "createdAt": "2026-08-10T01:00:00Z",
                        "author": {"login": "code_reviewer"},
                    },
                ],
                "reviews": [
                    {
                        "state": "COMMENTED",
                        "body": f"Code Review Summary: Approved ✅\n\n{BOT_MARKER}",
                        "submittedAt": "2026-08-10T02:00:00Z",
                        "author": {"login": "code_reviewer"},
                    }
                ],
            },
        ]
        mock_git_res = MagicMock(returncode=0, stdout="")
        mock_gh_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.side_effect = [mock_git_res, mock_gh_res]

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 107)

    def test_has_approval_marker_glad_to_see_approved(self):
        from lib.pr_tracker import has_approval_marker
        self.assertTrue(has_approval_marker("glad to see this approved"))
        self.assertTrue(has_approval_marker("happy to see this approved"))

    def test_is_bot_event_rest_api_type_bot(self):
        from lib.pr_tracker import is_bot_event
        self.assertTrue(is_bot_event("", {"login": "dependabot", "type": "Bot"}))
        self.assertFalse(is_bot_event("", {"login": "human_dev", "type": "User"}))

    @patch("subprocess.run")
    def test_sync_github_prs_review_required_with_approval(self, mock_run):
        mock_output = [
            {
                "number": 130,
                "title": "PR with reviewDecision REVIEW_REQUIRED despite historical approval",
                "url": "https://github.com/org/repo/pull/130",
                "author": {"login": "dev"},
                "reviewDecision": "REVIEW_REQUIRED",
                "isDraft": False,
                "reviews": [
                    {
                        "state": "APPROVED",
                        "body": "LGTM",
                        "author": {"login": "alice"},
                        "submittedAt": "2026-08-10T01:00:00Z",
                    }
                ],
            },
        ]
        mock_git_res = MagicMock(returncode=0, stdout="")
        mock_gh_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.side_effect = [mock_git_res, mock_gh_res]

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)

    @patch("subprocess.run")
    def test_sync_github_prs_dismissed_review_clears_approval(self, mock_run):
        mock_output = [
            {
                "number": 131,
                "title": "PR with dismissed approval review",
                "url": "https://github.com/org/repo/pull/131",
                "author": {"login": "dev"},
                "reviewDecision": "",
                "isDraft": False,
                "reviews": [
                    {
                        "state": "APPROVED",
                        "body": "LGTM",
                        "author": {"login": "alice"},
                        "submittedAt": "2026-08-10T01:00:00Z",
                    },
                    {
                        "state": "DISMISSED",
                        "body": "",
                        "author": {"login": "alice"},
                        "submittedAt": "2026-08-10T02:00:00Z",
                    },
                ],
            },
        ]
        mock_git_res = MagicMock(returncode=0, stdout="")
        mock_gh_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.side_effect = [mock_git_res, mock_gh_res]

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 0)

    @patch("subprocess.run")
    def test_sync_github_prs_timestamp_sorting_fallback(self, mock_run):
        mock_output = [
            {
                "number": 132,
                "title": "PR with undated comment and dated review",
                "url": "https://github.com/org/repo/pull/132",
                "author": {"login": "dev"},
                "reviewDecision": "",
                "isDraft": False,
                "comments": [
                    {
                        "body": "LGTM! Approved for merge.",
                        "createdAt": "",
                        "author": {"login": "alice"},
                    }
                ],
                "reviews": [
                    {
                        "state": "CHANGES_REQUESTED",
                        "body": "Please fix lint",
                        "author": {"login": "bob"},
                        "submittedAt": "2026-08-10T01:00:00Z",
                    }
                ],
            },
        ]
        mock_git_res = MagicMock(returncode=0, stdout="")
        mock_gh_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.side_effect = [mock_git_res, mock_gh_res]

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 0)

    def test_has_approval_marker_with_modal_verbs(self):
        from lib.pr_tracker import has_approval_marker
        self.assertTrue(has_approval_marker("Changes look good and will work as expected. Approved!"))
        self.assertTrue(has_approval_marker("This patch should fix the issue. LGTM!"))
        self.assertTrue(has_approval_marker("The solution would be fine. Approved."))
        self.assertTrue(has_approval_marker("Changes could work nicely. Approved!"))
        self.assertTrue(has_approval_marker("This must work. Approved!"))
        self.assertTrue(has_approval_marker("It may resolve the problem. LGTM!"))

        # Explicit negation / future phrasing should still be rejected
        self.assertFalse(has_approval_marker("It will be approved after testing."))
        self.assertFalse(has_approval_marker("It should be approved soon."))
        self.assertFalse(has_approval_marker("It could be approved later."))
        self.assertFalse(has_approval_marker("It would be approved if tests pass."))

    def test_has_approval_marker_prefix_negations(self):
        from lib.pr_tracker import has_approval_marker
        self.assertFalse(has_approval_marker("This PR is disapproved."))
        self.assertFalse(has_approval_marker("This PR is unapproved."))
        self.assertFalse(has_approval_marker("This PR is dis-approved."))
        self.assertFalse(has_approval_marker("This PR is non-approved."))

    def test_parse_event_timestamp_and_sorting(self):
        from lib.pr_tracker import _parse_event_timestamp
        ts1 = _parse_event_timestamp("2026-08-10T01:00:00Z")
        ts2 = _parse_event_timestamp("2026-08-10T03:00:00+02:00")
        ts3 = _parse_event_timestamp("2026-08-10T01:30:00.123456Z")
        self.assertEqual(ts1, ts2)
        self.assertLess(ts1, ts3)
        self.assertEqual(_parse_event_timestamp(None), 0.0)
        self.assertEqual(_parse_event_timestamp(""), 0.0)
        self.assertEqual(_parse_event_timestamp(True), 0.0)
        self.assertEqual(_parse_event_timestamp(False), 0.0)

    @patch("subprocess.run")
    def test_sync_github_prs_comment_with_user_schema_format(self, mock_run):
        mock_output = [
            {
                "number": 150,
                "title": "PR with REST user schema comment",
                "url": "https://github.com/org/repo/pull/150",
                "author": {"login": "dev"},
                "reviewDecision": "",
                "isDraft": False,
                "comments": [
                    {
                        "body": "LGTM!",
                        "createdAt": "2026-08-10T01:00:00Z",
                        "user": {"login": "reviewer_bob"},
                    },
                ],
            },
        ]
        mock_git_res = MagicMock(returncode=0, stdout="")
        mock_gh_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.side_effect = [mock_git_res, mock_gh_res]

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 150)

    @patch("subprocess.run")
    def test_sync_github_prs_with_modal_verb_approval_comment(self, mock_run):
        mock_output = [
            {
                "number": 140,
                "title": "PR with positive modal verb approval phrase",
                "url": "https://github.com/org/repo/pull/140",
                "author": {"login": "dev"},
                "reviewDecision": "",
                "isDraft": False,
                "comments": [
                    {
                        "body": "Changes look good and will work as expected. Approved!",
                        "createdAt": "2026-08-10T01:00:00Z",
                        "author": {"login": "reviewer_alice"},
                    },
                ],
            },
        ]
        mock_git_res = MagicMock(returncode=0, stdout="")
        mock_gh_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.side_effect = [mock_git_res, mock_gh_res]

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 140)

    @patch("subprocess.run")
    def test_sync_github_prs_comment_approval_with_review_required(self, mock_run):
        mock_output = [
            {
                "number": 145,
                "title": "Branch protected PR with comment approval",
                "url": "https://github.com/org/repo/pull/145",
                "author": {"login": "dev"},
                "reviewDecision": "REVIEW_REQUIRED",
                "isDraft": False,
                "comments": [
                    {
                        "body": "LGTM! Approved for merge.",
                        "createdAt": "2026-08-10T02:00:00Z",
                        "author": {"login": "reviewer_bob"},
                    },
                ],
            },
        ]
        mock_git_res = MagicMock(returncode=0, stdout="")
        mock_gh_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.side_effect = [mock_git_res, mock_gh_res]

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 145)
        self.assertEqual(approved[0]["author"], "dev")

    def test_has_approval_marker_rejects_questions_and_discussion(self):
        from lib.pr_tracker import has_approval_marker
        self.assertFalse(has_approval_marker("Is this PR approved?"))
        self.assertFalse(has_approval_marker("Has this been approved?"))
        self.assertFalse(has_approval_marker("Was this approved?"))
        self.assertFalse(has_approval_marker("Is this approved"))
        self.assertFalse(has_approval_marker("Why was this approved?"))
        self.assertFalse(has_approval_marker("Fixed issue in approved pull requests panel"))

    def test_has_approval_marker_perfect_tense_approvals(self):
        from lib.pr_tracker import has_approval_marker
        self.assertTrue(has_approval_marker("This PR has been tested and approved"))
        self.assertTrue(has_approval_marker("I have reviewed and approved"))
        self.assertTrue(has_approval_marker("The team has approved this PR"))
        self.assertTrue(has_approval_marker("I have approved"))

    def test_is_bot_event_agent_logins(self):
        from lib.pr_tracker import is_bot_event
        self.assertTrue(is_bot_event("", {"login": "code_reviewer"}))
        self.assertTrue(is_bot_event("", {"login": "code_fixer"}))
        self.assertTrue(is_bot_event("", "code_reviewer"))
        self.assertTrue(is_bot_event("", "code_fixer"))
        self.assertFalse(is_bot_event("", {"login": "human_dev"}))

    def test_is_bot_event_human_usernames_with_bot_substring(self):
        from lib.pr_tracker import is_bot_event
        human_logins = ["chabot", "talbot", "bottomley", "robotics", "Abott", "botany"]
        for login in human_logins:
            self.assertFalse(is_bot_event("", login), f"Failed for string login '{login}'")
            self.assertFalse(is_bot_event("", {"login": login, "type": "User"}), f"Failed for dict login '{login}'")

        bot_logins = ["github-actions[bot]", "my-bot", "bot_helper", "bot"]
        for login in bot_logins:
            self.assertTrue(is_bot_event("", login), f"Failed for bot login '{login}'")

    @patch("subprocess.run")
    def test_sync_github_prs_comment_approval_overrides_changes_requested_decision(self, mock_run):
        mock_output = [
            {
                "number": 124,
                "title": "PR with comment approval following CHANGES_REQUESTED review state",
                "url": "https://github.com/mweastwood/graviton/pull/124",
                "author": {"login": "dev_author"},
                "reviewDecision": "CHANGES_REQUESTED",
                "isDraft": False,
                "reviews": [
                    {
                        "state": "CHANGES_REQUESTED",
                        "body": "/fix Action items required",
                        "author": {"login": "chabot"},
                        "submittedAt": "2026-08-10T01:00:00Z",
                    }
                ],
                "comments": [
                    {
                        "body": "LGTM! Approved for merge.",
                        "createdAt": "2026-08-10T02:00:00Z",
                        "author": {"login": "chabot"},
                    }
                ],
            },
        ]
        mock_git_res = MagicMock(returncode=0, stdout="")
        mock_gh_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.side_effect = [mock_git_res, mock_gh_res]

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 124)

    @patch("subprocess.run")
    def test_sync_github_prs_subsequent_review_approval_overrides_changes_requested_decision(self, mock_run):
        mock_output = [
            {
                "number": 125,
                "title": "PR with review approval following CHANGES_REQUESTED state",
                "url": "https://github.com/mweastwood/graviton/pull/125",
                "author": {"login": "dev_author"},
                "reviewDecision": "CHANGES_REQUESTED",
                "isDraft": False,
                "reviews": [
                    {
                        "state": "CHANGES_REQUESTED",
                        "body": "Please fix issues",
                        "author": {"login": "reviewer_alice"},
                        "submittedAt": "2026-08-10T01:00:00Z",
                    },
                    {
                        "state": "APPROVED",
                        "body": "Looks good now",
                        "author": {"login": "reviewer_alice"},
                        "submittedAt": "2026-08-10T02:00:00Z",
                    },
                ],
            },
        ]
        mock_git_res = MagicMock(returncode=0, stdout="")
        mock_gh_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.side_effect = [mock_git_res, mock_gh_res]

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 125)

    @patch("subprocess.run")
    def test_sync_directory_custom_and_ssh_git_remote_urls(self, mock_run):
        import tempfile
        remote_urls = [
            ("git@git.internal.com:custom-org/custom-repo.git", "custom-org/custom-repo"),
            ("https://github.enterprise.corp/enterprise-org/ent-repo.git", "enterprise-org/ent-repo"),
            ("ssh://git@git.internal.com:2222/team/project.git", "team/project"),
        ]
        for url, expected_repo in remote_urls:
            with tempfile.TemporaryDirectory() as tmpdir:
                d = Path(tmpdir) / "repo"
                d.mkdir()
                (d / ".git").mkdir()

                mock_git_res = MagicMock(returncode=0, stdout=f"{url}\n")
                mock_gh_res = MagicMock(returncode=0, stdout=json.dumps([{
                    "number": 1,
                    "title": "Remote test PR",
                    "url": f"https://example.com/pr/1",
                    "author": {"login": "dev"},
                    "reviewDecision": "APPROVED",
                    "isDraft": False,
                }]))
                mock_run.side_effect = [mock_git_res, mock_gh_res]

                tracker = PRTracker()
                repo_name, prs = tracker._sync_directory(d)
                self.assertEqual(repo_name, expected_repo)

    @patch("subprocess.run")
    def test_sync_github_prs_review_id_numeric_type_safety(self, mock_run):
        mock_output = [
            {
                "number": 200,
                "title": "PR with numeric review IDs across review lists",
                "url": "https://github.com/org/repo/pull/200",
                "author": {"login": "dev"},
                "reviewDecision": "",
                "isDraft": False,
                "latestReviews": [
                    {
                        "id": 99999,
                        "state": "APPROVED",
                        "body": "LGTM",
                        "author": {"login": "reviewer1"},
                        "submittedAt": "2026-08-10T01:00:00Z",
                    }
                ],
                "reviews": [
                    {
                        "id": "99999",
                        "state": "APPROVED",
                        "body": "LGTM",
                        "author": {"login": "reviewer1"},
                        "submittedAt": "2026-08-10T01:00:00Z",
                    }
                ],
            },
        ]
        mock_git_res = MagicMock(returncode=0, stdout="")
        mock_gh_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.side_effect = [mock_git_res, mock_gh_res]

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 200)

    @patch("subprocess.run")
    def test_sync_single_repo_author_dict_lacking_login_key(self, mock_run):
        mock_output = [
            {
                "number": 301,
                "title": "PR with dict author lacking login key",
                "url": "https://github.com/org/repo/pull/301",
                "author": {"id": 12345},
                "reviewDecision": "APPROVED",
                "isDraft": False,
            },
            {
                "number": 302,
                "title": "PR with dict author containing None login",
                "url": "https://github.com/org/repo/pull/302",
                "author": {"login": None},
                "reviewDecision": "APPROVED",
                "isDraft": False,
            },
        ]
        mock_res = MagicMock(returncode=0, stdout=json.dumps(mock_output))
        mock_run.return_value = mock_res

        tracker = PRTracker()
        results = tracker._sync_single_repo(cwd="/tmp")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["number"], 301)
        self.assertEqual(results[0]["author"], "")
        self.assertEqual(results[1]["number"], 302)
        self.assertEqual(results[1]["author"], "")


if __name__ == "__main__":
    unittest.main()



