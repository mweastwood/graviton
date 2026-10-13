#!/usr/bin/env python3
"""
Comprehensive unit tests for autonomous agent management and enable/disable controls.
Tests AgentManager, router integration, scheduler integration, server endpoints, and dashboard.
"""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.agents import (
    AGENT_CODEBASE_AUDITOR,
    AGENT_CODE_FIXER,
    AGENT_CODE_REVIEWER,
    AGENT_ISSUE_TRIAGER,
    AGENT_PR_DRAFTER,
    AGENT_TO_ROLE,
    CANONICAL_AGENTS,
    ROLE_TO_AGENT,
    AgentManager,
    agent_to_role,
    canonicalize_agent_name,
)
from lib.dashboard.markdown import format_dashboard_markdown
from lib.router import is_agent_enabled, route_webhook_event
from lib.scheduler import ScheduledJob, TaskScheduler


class TestAgentManagerBasics(unittest.TestCase):
    def test_canonicalize_agent_name(self):
        self.assertEqual(canonicalize_agent_name("reviewer"), AGENT_CODE_REVIEWER)
        self.assertEqual(canonicalize_agent_name("code_reviewer"), AGENT_CODE_REVIEWER)
        self.assertEqual(canonicalize_agent_name("code-reviewer"), AGENT_CODE_REVIEWER)
        self.assertEqual(canonicalize_agent_name("fixer"), AGENT_CODE_FIXER)
        self.assertEqual(canonicalize_agent_name("code_fixer"), AGENT_CODE_FIXER)
        self.assertEqual(canonicalize_agent_name("code-fixer"), AGENT_CODE_FIXER)
        self.assertEqual(canonicalize_agent_name("triager"), AGENT_ISSUE_TRIAGER)
        self.assertEqual(canonicalize_agent_name("issue_triager"), AGENT_ISSUE_TRIAGER)
        self.assertEqual(canonicalize_agent_name("drafter"), AGENT_PR_DRAFTER)
        self.assertEqual(canonicalize_agent_name("pr_drafter"), AGENT_PR_DRAFTER)
        self.assertEqual(canonicalize_agent_name("auditor"), AGENT_CODEBASE_AUDITOR)
        self.assertEqual(canonicalize_agent_name("codebase_auditor"), AGENT_CODEBASE_AUDITOR)
        self.assertEqual(canonicalize_agent_name("unknown"), "unknown")
        self.assertIsNone(canonicalize_agent_name(None))
        self.assertIsNone(canonicalize_agent_name(""))

    def test_agent_to_role(self):
        self.assertEqual(agent_to_role("code_reviewer"), "reviewer")
        self.assertEqual(agent_to_role("code_fixer"), "fixer")
        self.assertEqual(agent_to_role("issue_triager"), "triager")
        self.assertEqual(agent_to_role("pr_drafter"), "drafter")
        self.assertEqual(agent_to_role("codebase_auditor"), "auditor")
        self.assertEqual(agent_to_role("reviewer"), "reviewer")
        self.assertIsNone(agent_to_role(None))

    def test_default_all_enabled(self):
        am = AgentManager(auto_load_env=False)
        for agent in CANONICAL_AGENTS:
            self.assertTrue(am.is_enabled(agent))
            role = AGENT_TO_ROLE[agent]
            self.assertTrue(am.is_enabled(role))
        self.assertTrue(am.is_enabled("unknown_custom_agent"))

    def test_enable_disable_toggle(self):
        am = AgentManager(auto_load_env=False)
        self.assertTrue(am.is_enabled("reviewer"))

        # Disable using role alias
        res = am.disable("reviewer")
        self.assertTrue(res)
        self.assertFalse(am.is_enabled("reviewer"))
        self.assertFalse(am.is_enabled(AGENT_CODE_REVIEWER))

        # Enable using canonical name
        res = am.enable(AGENT_CODE_REVIEWER)
        self.assertTrue(res)
        self.assertTrue(am.is_enabled("reviewer"))
        self.assertTrue(am.is_enabled(AGENT_CODE_REVIEWER))

        # Toggle
        new_val = am.toggle("reviewer")
        self.assertFalse(new_val)
        self.assertFalse(am.is_enabled("reviewer"))

        new_val = am.toggle("reviewer")
        self.assertTrue(new_val)
        self.assertTrue(am.is_enabled("reviewer"))

        # Unrecognized agent rejection
        self.assertFalse(am.set_enabled("unknown_agent", True))
        self.assertFalse(am.set_enabled("unknown_agent", False))
        self.assertFalse(am.enable("unknown_agent"))
        self.assertFalse(am.disable("unknown_agent"))
        self.assertIsNone(am.toggle("unknown_agent"))
        self.assertIsNone(am.toggle(""))

        # String boolean parsing in set_enabled
        am.set_enabled("reviewer", "false")
        self.assertFalse(am.is_enabled("reviewer"))
        am.set_enabled("reviewer", "true")
        self.assertTrue(am.is_enabled("reviewer"))
        am.set_enabled("reviewer", "0")
        self.assertFalse(am.is_enabled("reviewer"))
        am.set_enabled("reviewer", "1")
        self.assertTrue(am.is_enabled("reviewer"))

    def test_get_status_and_summary(self):
        am = AgentManager(auto_load_env=False)
        am.disable("auditor")

        status = am.get_status()
        self.assertEqual(len(status), len(CANONICAL_AGENTS))
        self.assertTrue(status[AGENT_CODE_REVIEWER])
        self.assertFalse(status[AGENT_CODEBASE_AUDITOR])

        roles_status = am.get_roles_status()
        self.assertTrue(roles_status["reviewer"])
        self.assertFalse(roles_status["auditor"])

        summary = am.get_summary()
        self.assertFalse(summary["all_enabled"])
        self.assertTrue(summary["any_disabled"])
        self.assertEqual(summary["disabled_count"], 1)
        self.assertIn(AGENT_CODEBASE_AUDITOR, summary["disabled_agents"])
        self.assertNotIn(AGENT_CODEBASE_AUDITOR, summary["enabled_agents"])

    def test_update_from_dict(self):
        am = AgentManager(auto_load_env=False)
        am.update_from_dict({
            "reviewer": False,
            "code_fixer": False,
            "drafter": "0",
            "auditor": True,
        })
        self.assertFalse(am.is_enabled("reviewer"))
        self.assertFalse(am.is_enabled("fixer"))
        self.assertFalse(am.is_enabled("drafter"))
        self.assertTrue(am.is_enabled("auditor"))
        self.assertTrue(am.is_enabled("triager"))


class TestAgentManagerEnvAndPersistence(unittest.TestCase):
    def test_env_vars_individual(self):
        env_patch = {
            "GRAVITON_ENABLE_REVIEWER": "false",
            "GRAVITON_DISABLE_FIXER": "true",
            "GRAVITON_ENABLE_ISSUE_TRIAGER": "1",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            am = AgentManager(auto_load_env=True)
            self.assertFalse(am.is_enabled("reviewer"))
            self.assertFalse(am.is_enabled("fixer"))
            self.assertTrue(am.is_enabled("triager"))

    def test_env_vars_batch_lists(self):
        env_patch = {
            "GRAVITON_DISABLED_AGENTS": "reviewer, codebase_auditor",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            am = AgentManager(auto_load_env=True)
            self.assertFalse(am.is_enabled("reviewer"))
            self.assertFalse(am.is_enabled("auditor"))
            self.assertTrue(am.is_enabled("fixer"))
            self.assertTrue(am.is_enabled("triager"))
            self.assertTrue(am.is_enabled("drafter"))

    def test_persistence_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "agent_state.json"
            am1 = AgentManager(state_path=state_file, auto_load_env=False)
            am1.disable("reviewer")
            am1.disable("drafter")
            self.assertTrue(state_file.exists())

            # Load into new manager
            am2 = AgentManager(state_path=state_file, auto_load_env=False)
            self.assertFalse(am2.is_enabled("reviewer"))
            self.assertFalse(am2.is_enabled("drafter"))
            self.assertTrue(am2.is_enabled("fixer"))
            self.assertTrue(am2.is_enabled("triager"))
            self.assertTrue(am2.is_enabled("auditor"))


class TestAgentManagerConcurrency(unittest.TestCase):
    def test_concurrent_toggles(self):
        am = AgentManager(auto_load_env=False)
        iterations = 50

        def toggle_worker():
            for _ in range(iterations):
                am.toggle("reviewer")
                am.toggle("fixer")
                am.get_summary()

        threads = [threading.Thread(target=toggle_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Both agents must still have a valid boolean state
        self.assertIsInstance(am.is_enabled("reviewer"), bool)
        self.assertIsInstance(am.is_enabled("fixer"), bool)

    def test_concurrent_save_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "concurrent_state.json"
            am = AgentManager(state_path=state_file, auto_load_env=False)
            iterations = 30

            def save_worker(worker_id):
                for i in range(iterations):
                    if i % 2 == 0:
                        am.disable("reviewer")
                    else:
                        am.enable("reviewer")
                    am.save_state()

            threads = [threading.Thread(target=save_worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertTrue(state_file.exists())
            with open(state_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertIn("code_reviewer", saved)
            self.assertIsInstance(saved["code_reviewer"], bool)


class TestRouterAgentManagerIntegration(unittest.TestCase):
    def setUp(self):
        self.am = AgentManager(auto_load_env=False)

    def test_is_agent_enabled_helper(self):
        self.assertTrue(is_agent_enabled("code_reviewer", None))
        self.assertTrue(is_agent_enabled("code_reviewer", self.am))
        self.am.disable("reviewer")
        self.assertFalse(is_agent_enabled("code_reviewer", self.am))

        # Dict support
        self.assertTrue(is_agent_enabled("code_fixer", {"code_fixer": True}))
        self.assertFalse(is_agent_enabled("code_fixer", {"code_fixer": False}))
        self.assertFalse(is_agent_enabled("code_fixer", {"fixer": False}))

        # Verify definition in lib.agents and re-export in lib.routers.base
        from lib.agents import is_agent_enabled as is_agent_enabled_from_agents
        from lib.routers.base import is_agent_enabled as is_agent_enabled_from_base
        self.assertIs(is_agent_enabled_from_agents, is_agent_enabled_from_base)

        # Set support
        self.assertTrue(is_agent_enabled("code_fixer", {"code_fixer"}))
        self.assertFalse(is_agent_enabled("code_fixer", {"code_reviewer"}))

    def test_pull_request_event_disabled_reviewer(self):
        payload = {
            "action": "opened",
            "number": 42,
            "pull_request": {"number": 42, "body": "Initial PR description"},
            "repository": {"full_name": "owner/repo", "name": "repo"},
        }
        # Enabled
        decision = route_webhook_event("pull_request", payload, agent_manager=self.am)
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["agent"], "code_reviewer")

        # Disabled
        self.am.disable("reviewer")
        decision = route_webhook_event("pull_request", payload, agent_manager=self.am)
        self.assertEqual(decision["status"], "ignored")
        self.assertIn("disabled", decision["reason"])

    def test_pull_request_review_event_disabled_fixer(self):
        payload = {
            "action": "submitted",
            "number": 42,
            "review": {"state": "CHANGES_REQUESTED", "body": "Please fix this logic."},
            "pull_request": {"number": 42, "user": {"login": "antigravity"}},
            "repository": {"full_name": "owner/repo", "name": "repo"},
        }
        # Enabled
        decision = route_webhook_event("pull_request_review", payload, agent_manager=self.am)
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["agent"], "code_fixer")

        # Disabled
        self.am.disable("fixer")
        decision = route_webhook_event("pull_request_review", payload, agent_manager=self.am)
        self.assertEqual(decision["status"], "ignored")
        self.assertIn("disabled", decision["reason"])

    def test_pull_request_review_comment_event_disabled_fixer(self):
        payload = {
            "action": "created",
            "comment": {"body": "Off by one error here.", "path": "lib/foo.py", "line": 10},
            "pull_request": {"number": 42, "user": {"login": "antigravity"}},
            "repository": {"full_name": "owner/repo", "name": "repo"},
        }
        # Enabled
        decision = route_webhook_event("pull_request_review_comment", payload, agent_manager=self.am)
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["agent"], "code_fixer")

        # Disabled
        self.am.disable("fixer")
        decision = route_webhook_event("pull_request_review_comment", payload, agent_manager=self.am)
        self.assertEqual(decision["status"], "ignored")
        self.assertIn("disabled", decision["reason"])

    def test_issues_event_opened_disabled_triager(self):
        payload = {
            "action": "opened",
            "issue": {"number": 10, "title": "Crash on startup", "body": "Traceback attached."},
            "repository": {"full_name": "owner/repo", "name": "repo"},
        }
        # Enabled
        decision = route_webhook_event("issues", payload, agent_manager=self.am)
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["agent"], "issue_triager")

        # Disabled
        self.am.disable("triager")
        decision = route_webhook_event("issues", payload, agent_manager=self.am)
        self.assertEqual(decision["status"], "ignored")
        self.assertIn("disabled", decision["reason"])

    def test_issues_event_labeled_ready_for_pr_disabled_drafter(self):
        payload = {
            "action": "labeled",
            "label": {"name": "ready-for-pr"},
            "issue": {"number": 10, "title": "Spec ready", "body": "Details."},
            "repository": {"full_name": "owner/repo", "name": "repo"},
        }
        # Enabled
        decision = route_webhook_event("issues", payload, agent_manager=self.am)
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["agent"], "pr_drafter")

        # Disabled
        self.am.disable("drafter")
        decision = route_webhook_event("issues", payload, agent_manager=self.am)
        self.assertEqual(decision["status"], "ignored")
        self.assertIn("disabled", decision["reason"])

    def test_issue_comment_on_pr_explicit_commands(self):
        # /review command
        payload_rev = {
            "action": "created",
            "comment": {"body": "/review please"},
            "issue": {"number": 5, "pull_request": {"url": "https://github.com/owner/repo/pull/5"}},
            "repository": {"full_name": "owner/repo", "name": "repo"},
        }
        self.am.disable("reviewer")
        dec_rev = route_webhook_event("issue_comment", payload_rev, agent_manager=self.am)
        self.assertEqual(dec_rev["status"], "ignored")
        self.assertIn("disabled", dec_rev["reason"])

        # /fix command
        payload_fix = {
            "action": "created",
            "comment": {"body": "/fix typos"},
            "issue": {"number": 5, "pull_request": {"url": "https://github.com/owner/repo/pull/5"}},
            "repository": {"full_name": "owner/repo", "name": "repo"},
        }
        self.am.disable("fixer")
        dec_fix = route_webhook_event("issue_comment", payload_fix, agent_manager=self.am)
        self.assertEqual(dec_fix["status"], "ignored")
        self.assertIn("disabled", dec_fix["reason"])


class TestSchedulerAgentManagerIntegration(unittest.TestCase):
    def test_scheduler_skips_disabled_agent_jobs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "schedules.json"
            state = Path(tmpdir) / "scheduler_state.json"
            am = AgentManager(auto_load_env=False)
            am.disable("auditor")

            executed_jobs = []

            def mock_runner(agent, prompt, *args):
                executed_jobs.append((agent, prompt))

            scheduler = TaskScheduler(
                config_path=cfg,
                state_path=state,
                runner=mock_runner,
                agent_manager=am,
                check_interval_seconds=1.0,
            )

            job = ScheduledJob(
                job_id="test_sweep",
                name="Test Sweep",
                interval_seconds=60,
                agent="codebase_auditor",
                prompt="Sweep code",
                enabled=True,
            )
            scheduler.add_job(job)

            # Manual trigger should be refused when disabled
            triggered = scheduler.trigger_job("test_sweep")
            self.assertFalse(triggered)
            self.assertEqual(len(executed_jobs), 0)

            # Manual trigger with force=True bypasses agent_manager and executes the job
            forced = scheduler.trigger_job("test_sweep", force=True)
            self.assertTrue(forced)
            self.assertEqual(len(executed_jobs), 1)
            self.assertEqual(executed_jobs[-1][0], "codebase_auditor")

            # Re-enable and trigger normally
            am.enable("auditor")
            triggered = scheduler.trigger_job("test_sweep")
            self.assertTrue(triggered)
            self.assertEqual(len(executed_jobs), 2)

            # _check_and_run_jobs tick
            am.disable("auditor")
            job.last_run = None
            job.next_run = None
            scheduler._check_and_run_jobs()
            # Still 2 executions, tick did not run it because agent is disabled
            self.assertEqual(len(executed_jobs), 2)


class TestDashboardAgentStatus(unittest.TestCase):
    def test_markdown_dashboard_with_agent_manager(self):
        am = AgentManager(auto_load_env=False)
        am.disable("reviewer")
        am.disable("auditor")

        md = format_dashboard_markdown(agent_manager=am)
        self.assertIn("## 🤖 Autonomous Agents", md)
        self.assertIn("🔴 `DISABLED`", md)
        self.assertIn("🟢 `ENABLED`", md)
        self.assertIn("code_reviewer", md)
        self.assertIn("codebase_auditor", md)
        self.assertIn("code_fixer", md)

    def test_markdown_dashboard_without_agent_manager_no_extra_section(self):
        md = format_dashboard_markdown(agent_manager=None)
        self.assertNotIn("## 🤖 Autonomous Agents", md)

    def test_markdown_dashboard_with_custom_agents(self):
        am = AgentManager(auto_load_env=False)
        extra_status = {"security_auditor": True, "custom_fixer": False}
        full_status = {**am.get_status(), **extra_status}
        md = format_dashboard_markdown(agent_manager=am, extra_info={"agent_status": full_status})
        self.assertIn("## 🤖 Autonomous Agents", md)
        self.assertIn("security_auditor", md)
        self.assertIn("custom_fixer", md)
        self.assertIn("code_reviewer", md)
        # Verify canonical agents appear before custom agents
        pos_reviewer = md.find("`code_reviewer`")
        pos_auditor = md.find("`codebase_auditor`")
        pos_custom = md.find("`security_auditor`")
        self.assertTrue(pos_reviewer < pos_custom)
        self.assertTrue(pos_auditor < pos_custom)


class TestServerAgentEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        server_path = Path(__file__).resolve().parent.parent / "bin" / "graviton-server.py"
        spec = importlib.util.spec_from_file_location("graviton_server_test", server_path)
        cls.server_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server_mod)
        cls.GravitonHandler = cls.server_mod.GravitonHandler

    def setUp(self):
        self.am = AgentManager(auto_load_env=False)
        self.GravitonHandler.agent_manager = self.am
        self.GravitonHandler.secret = ""

    def test_health_check_includes_agents(self):
        handler = MagicMock(spec=self.GravitonHandler)
        handler.task_manager = None
        handler.quota_tracker = None
        handler.path = "/health"
        handler.default_reviewer = "code_reviewer"
        handler.default_fixer = "code_fixer"
        handler.default_triager = "issue_triager"
        handler.default_drafter = "pr_drafter"
        handler.agent_manager = self.am

        self.GravitonHandler.do_GET(handler)
        handler._send_json.assert_called_once()
        code, body = handler._send_json.call_args[0]
        self.assertEqual(code, 200)
        self.assertIn("agents", body)
        self.assertIn("roles", body)
        self.assertTrue(body["agents"]["code_reviewer"])
        self.assertTrue(body["roles"]["reviewer"])

    def test_get_api_agents(self):
        handler = MagicMock(spec=self.GravitonHandler)
        handler.path = "/api/agents"
        handler.agent_manager = self.am

        self.GravitonHandler.do_GET(handler)
        handler._send_json.assert_called_once()
        code, body = handler._send_json.call_args[0]
        self.assertEqual(code, 200)
        self.assertTrue(body["all_enabled"])
        self.assertEqual(body["disabled_count"], 0)
        self.assertIn("code_reviewer", body["agents"])

    def test_post_api_agents_toggle(self):
        from io import BytesIO

        # 1. Toggle via path
        handler = MagicMock(spec=self.GravitonHandler)
        handler.path = "/api/agents/toggle"
        handler.agent_manager = self.am
        handler.dashboard_updater = MagicMock()
        payload = json.dumps({"agent": "reviewer"}).encode("utf-8")
        handler.rfile = BytesIO(payload)
        handler.headers = {"Content-Length": str(len(payload))}

        self.GravitonHandler.do_POST(handler)
        handler._send_json.assert_called_once()
        code, body = handler._send_json.call_args[0]
        self.assertEqual(code, 200)
        self.assertFalse(body["agents"]["code_reviewer"])
        self.assertFalse(body["roles"]["reviewer"])
        handler.dashboard_updater.trigger_update.assert_called_once()

    def test_post_api_agents_explicit_enable_disable(self):
        from io import BytesIO

        # Disable
        handler = MagicMock(spec=self.GravitonHandler)
        handler.path = "/api/agents"
        handler.agent_manager = self.am
        handler.dashboard_updater = None
        payload = json.dumps({"agent": "fixer", "enabled": False}).encode("utf-8")
        handler.rfile = BytesIO(payload)
        handler.headers = {"Content-Length": str(len(payload))}

        self.GravitonHandler.do_POST(handler)
        handler._send_json.assert_called_once()
        code, body = handler._send_json.call_args[0]
        self.assertEqual(code, 200)
        self.assertFalse(body["agents"]["code_fixer"])

        # Enable via action="enable"
        handler2 = MagicMock(spec=self.GravitonHandler)
        handler2.path = "/api/agents"
        handler2.agent_manager = self.am
        handler2.dashboard_updater = None
        payload2 = json.dumps({"agent": "fixer", "action": "enable"}).encode("utf-8")
        handler2.rfile = BytesIO(payload2)
        handler2.headers = {"Content-Length": str(len(payload2))}

        self.GravitonHandler.do_POST(handler2)
        handler2._send_json.assert_called_once()
        code2, body2 = handler2._send_json.call_args[0]
        self.assertEqual(code2, 200)
        self.assertTrue(body2["agents"]["code_fixer"])

        # String boolean parsing ("false" -> False)
        handler3 = MagicMock(spec=self.GravitonHandler)
        handler3.path = "/api/agents"
        handler3.agent_manager = self.am
        handler3.dashboard_updater = None
        payload3 = json.dumps({"agent": "reviewer", "enabled": "false"}).encode("utf-8")
        handler3.rfile = BytesIO(payload3)
        handler3.headers = {"Content-Length": str(len(payload3))}

        self.GravitonHandler.do_POST(handler3)
        handler3._send_json.assert_called_once()
        code3, body3 = handler3._send_json.call_args[0]
        self.assertEqual(code3, 200)
        self.assertFalse(body3["agents"]["code_reviewer"])

        # String boolean parsing ("true" -> True)
        handler4 = MagicMock(spec=self.GravitonHandler)
        handler4.path = "/api/agents"
        handler4.agent_manager = self.am
        handler4.dashboard_updater = None
        payload4 = json.dumps({"agent": "reviewer", "enabled": "true"}).encode("utf-8")
        handler4.rfile = BytesIO(payload4)
        handler4.headers = {"Content-Length": str(len(payload4))}

        self.GravitonHandler.do_POST(handler4)
        handler4._send_json.assert_called_once()
        code4, body4 = handler4._send_json.call_args[0]
        self.assertEqual(code4, 200)
        self.assertTrue(body4["agents"]["code_reviewer"])

    def test_post_api_agents_batch_and_direct(self):
        from io import BytesIO

        handler = MagicMock(spec=self.GravitonHandler)
        handler.path = "/api/agents"
        handler.agent_manager = self.am
        handler.dashboard_updater = None
        payload = json.dumps({"reviewer": False, "auditor": False}).encode("utf-8")
        handler.rfile = BytesIO(payload)
        handler.headers = {"Content-Length": str(len(payload))}

        self.GravitonHandler.do_POST(handler)
        handler._send_json.assert_called_once()
        code, body = handler._send_json.call_args[0]
        self.assertEqual(code, 200)
        self.assertFalse(body["agents"]["code_reviewer"])
        self.assertFalse(body["agents"]["codebase_auditor"])
        self.assertTrue(body["agents"]["code_fixer"])

        # Wrapped agents dictionary payload
        handler_wrapped = MagicMock(spec=self.GravitonHandler)
        handler_wrapped.path = "/api/agents"
        handler_wrapped.agent_manager = self.am
        handler_wrapped.dashboard_updater = None
        payload_wrapped = json.dumps({"agents": {"reviewer": True, "auditor": True}}).encode("utf-8")
        handler_wrapped.rfile = BytesIO(payload_wrapped)
        handler_wrapped.headers = {"Content-Length": str(len(payload_wrapped))}

        self.GravitonHandler.do_POST(handler_wrapped)
        handler_wrapped._send_json.assert_called_once()
        code_w, body_w = handler_wrapped._send_json.call_args[0]
        self.assertEqual(code_w, 200)
        self.assertTrue(body_w["agents"]["code_reviewer"])
        self.assertTrue(body_w["agents"]["codebase_auditor"])

    def test_post_api_agents_errors(self):
        from io import BytesIO

        # Invalid JSON
        handler = MagicMock(spec=self.GravitonHandler)
        handler.path = "/api/agents"
        handler.agent_manager = self.am
        handler.rfile = BytesIO(b"not json")
        handler.headers = {"Content-Length": "8"}
        self.GravitonHandler.do_POST(handler)
        code, body = handler._send_json.call_args[0]
        self.assertEqual(code, 400)

        # Empty/Unknown agent in toggle
        handler2 = MagicMock(spec=self.GravitonHandler)
        handler2.path = "/api/agents/toggle"
        handler2.agent_manager = self.am
        payload2 = json.dumps({"agent": ""}).encode("utf-8")
        handler2.rfile = BytesIO(payload2)
        handler2.headers = {"Content-Length": str(len(payload2))}
        self.GravitonHandler.do_POST(handler2)
        code2, body2 = handler2._send_json.call_args[0]
        self.assertEqual(code2, 400)
        self.assertIn("error", body2)

        # Unknown agent in toggle
        handler3 = MagicMock(spec=self.GravitonHandler)
        handler3.path = "/api/agents/toggle"
        handler3.agent_manager = self.am
        payload3 = json.dumps({"agent": "nonexistent_agent"}).encode("utf-8")
        handler3.rfile = BytesIO(payload3)
        handler3.headers = {"Content-Length": str(len(payload3))}
        self.GravitonHandler.do_POST(handler3)
        code3, body3 = handler3._send_json.call_args[0]
        self.assertEqual(code3, 400)
        self.assertIn("Unknown agent", body3["error"])

        # Unknown agent in enable action
        handler4 = MagicMock(spec=self.GravitonHandler)
        handler4.path = "/api/agents"
        handler4.agent_manager = self.am
        payload4 = json.dumps({"agent": "nonexistent_agent", "action": "enable"}).encode("utf-8")
        handler4.rfile = BytesIO(payload4)
        handler4.headers = {"Content-Length": str(len(payload4))}
        self.GravitonHandler.do_POST(handler4)
        code4, body4 = handler4._send_json.call_args[0]
        self.assertEqual(code4, 400)
        self.assertIn("Unknown agent", body4["error"])

        # Unknown agent in disable action
        handler5 = MagicMock(spec=self.GravitonHandler)
        handler5.path = "/api/agents"
        handler5.agent_manager = self.am
        payload5 = json.dumps({"agent": "nonexistent_agent", "action": "disable"}).encode("utf-8")
        handler5.rfile = BytesIO(payload5)
        handler5.headers = {"Content-Length": str(len(payload5))}
        self.GravitonHandler.do_POST(handler5)
        code5, body5 = handler5._send_json.call_args[0]
        self.assertEqual(code5, 400)
        self.assertIn("Unknown agent", body5["error"])

        # Unknown agent with enabled field
        handler6 = MagicMock(spec=self.GravitonHandler)
        handler6.path = "/api/agents"
        handler6.agent_manager = self.am
        payload6 = json.dumps({"agent": "nonexistent_agent", "enabled": True}).encode("utf-8")
        handler6.rfile = BytesIO(payload6)
        handler6.headers = {"Content-Length": str(len(payload6))}
        self.GravitonHandler.do_POST(handler6)
        code6, body6 = handler6._send_json.call_args[0]
        self.assertEqual(code6, 400)
        self.assertIn("Unknown agent", body6["error"])

        # Invalid boolean string
        handler7 = MagicMock(spec=self.GravitonHandler)
        handler7.path = "/api/agents"
        handler7.agent_manager = self.am
        payload7 = json.dumps({"agent": "reviewer", "enabled": "not_a_bool"}).encode("utf-8")
        handler7.rfile = BytesIO(payload7)
        handler7.headers = {"Content-Length": str(len(payload7))}
        self.GravitonHandler.do_POST(handler7)
        code7, body7 = handler7._send_json.call_args[0]
        self.assertEqual(code7, 400)
        self.assertIn("Invalid boolean value", body7["error"])

        # Wrapped payload with no recognized agent keys
        handler8 = MagicMock(spec=self.GravitonHandler)
        handler8.path = "/api/agents"
        handler8.agent_manager = self.am
        payload8 = json.dumps({"agents": {"invalid_agent": True}}).encode("utf-8")
        handler8.rfile = BytesIO(payload8)
        handler8.headers = {"Content-Length": str(len(payload8))}
        self.GravitonHandler.do_POST(handler8)
        code8, body8 = handler8._send_json.call_args[0]
        self.assertEqual(code8, 400)
        self.assertEqual(body8["error"], "No valid agent configuration provided")

        # Empty wrapped agents payload
        handler9 = MagicMock(spec=self.GravitonHandler)
        handler9.path = "/api/agents"
        handler9.agent_manager = self.am
        payload9 = json.dumps({"agents": {}}).encode("utf-8")
        handler9.rfile = BytesIO(payload9)
        handler9.headers = {"Content-Length": str(len(payload9))}
        self.GravitonHandler.do_POST(handler9)
        code9, body9 = handler9._send_json.call_args[0]
        self.assertEqual(code9, 400)
        self.assertEqual(body9["error"], "No valid agent configuration provided")

        # Direct payload with no recognized agent keys
        handler10 = MagicMock(spec=self.GravitonHandler)
        handler10.path = "/api/agents"
        handler10.agent_manager = self.am
        payload10 = json.dumps({"invalid_name": True}).encode("utf-8")
        handler10.rfile = BytesIO(payload10)
        handler10.headers = {"Content-Length": str(len(payload10))}
        self.GravitonHandler.do_POST(handler10)
        code10, body10 = handler10._send_json.call_args[0]
        self.assertEqual(code10, 400)
        self.assertEqual(body10["error"], "No valid agent configuration provided")


class TestServerAgentCLI(unittest.TestCase):
    def test_server_cli_flags(self):
        import subprocess
        res = subprocess.run(
            ["python3", "bin/graviton-server.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--enable-reviewer", res.stdout)
        self.assertIn("--disable-reviewer", res.stdout)
        self.assertIn("--enable-fixer", res.stdout)
        self.assertIn("--disable-fixer", res.stdout)
        self.assertIn("--enable-triager", res.stdout)
        self.assertIn("--disable-triager", res.stdout)
        self.assertIn("--enable-drafter", res.stdout)
        self.assertIn("--disable-drafter", res.stdout)
        self.assertIn("--enable-auditor", res.stdout)
        self.assertIn("--disable-auditor", res.stdout)
        self.assertIn("--enabled-agents", res.stdout)
        self.assertIn("--disabled-agents", res.stdout)


if __name__ == "__main__":
    unittest.main()
