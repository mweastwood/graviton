#!/usr/bin/env python3
"""
Graviton Webhook Server & Event Router.

Listens for GitHub webhook events (pull_request, pull_request_review,
pull_request_review_comment, issues, issue_comment) and triggers sandboxed
Antigravity agent containers in response.

Uses standard Python library only (0 external dependencies).
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Any

# Add REPO_ROOT to sys.path to allow importing lib
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.security import verify_signature, is_valid_repo_name
from lib.router import route_webhook_event, format_event_summary, get_server_repo_name
from lib.runner import run_agent_async
from lib.updater import sync_repo_and_reload, stop_smee_listener, set_hot_reload_state
from lib.scheduler import TaskScheduler
from lib.tasks import TaskManager
from lib.tui import TerminalDashboard, run_graceful_shutdown
from lib.pr_tracker import PRTracker
from lib.quota import QuotaTracker, QuotaState
from lib.reactions import post_emoji_reaction_async
from lib.release import (
    DEFAULT_BRANCH,
    execute_release_async,
    post_issue_comment,
    post_release_help_async,
    post_release_init_async,
    post_release_unrecognized_async,
    resolve_repo_dir,
)
from lib.dashboard import DashboardUpdater, format_dashboard_markdown, render_dashboard_html


_is_shutting_down = False
_shutdown_thread: Optional[threading.Thread] = None
_shutdown_lock = threading.Lock()


def graceful_shutdown(
    task_manager: Optional[TaskManager] = None,
    scheduler: Optional[TaskScheduler] = None,
    dashboard: Optional[TerminalDashboard] = None,
    httpd: Optional[HTTPServer] = None,
    quota_tracker: Optional[QuotaTracker] = None,
    grace_period: float = 3.0,
    timeout: Optional[float] = None,
) -> threading.Thread:
    """
    Execute 4-step graceful shutdown sequence in a background thread:
    1. Drain Active Tasks (task_manager.drain_active_tasks)
    2. Webhook Grace Buffer (sleep grace_period seconds)
    3. Shutdown HTTP Listener (httpd.shutdown) & Persist Task Queue and Model Selection (task_manager.dump_queue_state, quota_tracker.dump_model_selection)
    4. Clean Abort & Termination (stop scheduler, dashboard, task_manager, server_close)
    """
    if dashboard:
        dashboard.httpd = httpd or getattr(dashboard, "httpd", None)
        return dashboard.graceful_shutdown(timeout=timeout, grace_period=grace_period)

    global _is_shutting_down, _shutdown_thread
    with _shutdown_lock:
        if _is_shutting_down and _shutdown_thread is not None:
            return _shutdown_thread
        _is_shutting_down = True

        def _shutdown_worker():
            run_graceful_shutdown(
                task_manager=task_manager,
                scheduler=scheduler,
                dashboard=dashboard,
                httpd=httpd,
                quota_tracker=quota_tracker,
                grace_period=grace_period,
                timeout=timeout,
                logger=logger,
            )

        t = threading.Thread(target=_shutdown_worker, daemon=True, name="GracefulShutdownThread")
        _shutdown_thread = t
        t.start()
        return t


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("graviton")

RUN_CONTAINER_SCRIPT = REPO_ROOT / "bin" / "run_agent_container.sh"
RUN_LISTENER_SCRIPT = REPO_ROOT / "bin" / "run_listener.sh"


def start_smee_listener(smee_url: str, port: int) -> Optional[subprocess.Popen]:
    """Launch bin/run_listener.sh as a background subprocess if smee_url is provided."""
    if not smee_url:
        return None
    if not RUN_LISTENER_SCRIPT.exists() or not os.access(RUN_LISTENER_SCRIPT, os.X_OK):
        logger.error(f"Smee listener script not found or not executable: {RUN_LISTENER_SCRIPT}")
        return None
    logger.info(f"Starting background smee listener for {smee_url} -> http://localhost:{port}/...")
    try:
        return subprocess.Popen(
            [str(RUN_LISTENER_SCRIPT), smee_url, str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.error(f"Failed to start smee listener process: {e}")
        return None


def is_supervisor_active(handler: Any) -> bool:
    """Return True only if supervisor mode is explicitly enabled on handler, server, or class."""
    val = getattr(handler, "use_supervisor", None)
    if val is True:
        return True
    if val is False:
        return False
    server = getattr(handler, "server", None)
    if server is not None:
        s_val = getattr(server, "use_supervisor", None)
        if s_val is True:
            return True
        if s_val is False:
            return False
    return getattr(GravitonHandler, "use_supervisor", False) is True


def resolve_server_host_and_port(handler: Any) -> tuple[str, int]:
    """Resolve the effective server host and port safely."""
    updater = getattr(handler, "dashboard_updater", None)
    if updater and not isinstance(updater, (type, type(None))):
        h = getattr(updater, "host", None)
        p = getattr(updater, "port", None)
        if isinstance(h, str) and isinstance(p, int):
            return h, p
        if isinstance(h, str):
            try:
                return h, int(p)
            except (ValueError, TypeError):
                return h, 8000

    server = getattr(handler, "server", None)
    if server and not isinstance(server, (type, type(None))):
        s_addr = getattr(server, "server_address", None)
        if isinstance(s_addr, tuple) and len(s_addr) == 2:
            h, p = s_addr[0], s_addr[1]
            if isinstance(h, str) and isinstance(p, int):
                return h, p

    h = getattr(handler, "server_host", None)
    if not isinstance(h, str):
        h = getattr(GravitonHandler, "server_host", "localhost")
        if not isinstance(h, str):
            h = "localhost"

    p = getattr(handler, "server_port", None)
    if not isinstance(p, int):
        p = getattr(GravitonHandler, "server_port", 8000)
        try:
            p = int(p)
        except (ValueError, TypeError):
            p = 8000

    return h, p


class GravitonHandler(BaseHTTPRequestHandler):
    secret: str = ""
    default_reviewer: str = "code_reviewer"
    default_fixer: str = "code_fixer"
    default_triager: str = "issue_triager"
    default_drafter: str = "pr_drafter"
    server_repo_name: str = get_server_repo_name(REPO_ROOT)
    scheduler: Optional[TaskScheduler] = None
    task_manager: Optional[TaskManager] = None
    pr_tracker: Optional[PRTracker] = None
    quota_tracker: Optional[QuotaTracker] = None
    dashboard_updater: Optional[DashboardUpdater] = None
    listener_proc: Optional[subprocess.Popen] = None
    use_supervisor: bool = False
    server_host: str = "localhost"
    server_port: int = 8000

    @property
    def is_supervisor_active(self) -> bool:
        return is_supervisor_active(self)

    def _get_host_and_port(self) -> tuple[str, int]:
        """Resolve the effective server host and port."""
        return resolve_server_host_and_port(self)

    def do_GET(self):
        """Health check and task query endpoints."""
        raw_path = getattr(self, "path", "/") or "/"
        path_clean = raw_path.split("?")[0].rstrip("/")
        if path_clean in ("", "/health"):
            sched = GravitonHandler.scheduler
            tasks_info = self.task_manager.get_stats() if self.task_manager else {}
            quota_info = self.quota_tracker.get_info().to_dict() if self.quota_tracker else {}
            active_rc_urls = {}
            if self.task_manager:
                active_rc_urls = {
                    t.id: t.remote_control_url
                    for t in self.task_manager.get_active_tasks()
                    if getattr(t, "remote_control_url", None)
                }
            self._send_json(200, {
                "status": "ok",
                "service": "graviton-server",
                "reviewer_agent": self.default_reviewer,
                "fixer_agent": self.default_fixer,
                "triager_agent": self.default_triager,
                "drafter_agent": self.default_drafter,
                "scheduler_enabled": sched is not None,
                "scheduler_running": sched.is_running() if sched else False,
                "active_jobs": len(sched.jobs) if sched else 0,
                "tasks": tasks_info,
                "active_remote_control_urls": active_rc_urls,
                "quota": quota_info,
            })
        elif path_clean == "/tasks":
            if not self.task_manager:
                self._send_json(200, {"active": [], "queued": [], "history": [], "stats": {}})
                return
            active = [t.to_dict() for t in self.task_manager.get_active_tasks()]
            queued = [t.to_dict() for t in self.task_manager.get_queued_tasks()]
            history = [t.to_dict() for t in self.task_manager.get_task_history(limit=50)]
            self._send_json(200, {
                "stats": self.task_manager.get_stats(),
                "active": active,
                "queued": queued,
                "history": history,
            })
        elif path_clean.startswith("/tasks/"):
            task_id = path_clean[len("/tasks/"):]
            if not self.task_manager:
                self._send_json(503, {"error": "TaskManager not enabled"})
                return
            task = self.task_manager.get_task(task_id)
            if not task:
                self._send_json(404, {"error": f"Task '{task_id}' not found"})
                return
            task_dict = task.to_dict()
            task_dict["logs"] = task.get_logs(limit=200)
            self._send_json(200, task_dict)
        elif path_clean == "/dashboard":
            host, port = resolve_server_host_and_port(self)
            markdown_content = (
                self.dashboard_updater.get_markdown()
                if self.dashboard_updater
                else format_dashboard_markdown(
                    task_manager=self.task_manager,
                    quota_tracker=self.quota_tracker,
                    scheduler=self.scheduler,
                    host=host,
                    port=port,
                )
            )
            html_page = render_dashboard_html(markdown_content, host=host, port=port)
            self._send_html(200, html_page)
        elif path_clean in ("/dashboard/content", "/dashboard/markdown"):
            host, port = resolve_server_host_and_port(self)
            markdown_content = (
                self.dashboard_updater.get_markdown()
                if self.dashboard_updater
                else format_dashboard_markdown(
                    task_manager=self.task_manager,
                    quota_tracker=self.quota_tracker,
                    scheduler=self.scheduler,
                    host=host,
                    port=port,
                )
            )
            targets = self.dashboard_updater.get_targets() if self.dashboard_updater else []
            self._send_json(200, {"markdown": markdown_content, "targets": targets})
        else:
            self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        """Handle incoming GitHub Webhook POST request or local task control."""
        content_length = int(self.headers.get("Content-Length", 0))
        payload_bytes = self.rfile.read(content_length)

        raw_path = getattr(self, "path", "/") or "/"
        path_clean = raw_path.split("?")[0].rstrip("/")
        # Local control endpoints
        if path_clean == "/tasks/submit":
            try:
                data = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
            except Exception:
                self._send_json(400, {"error": "Invalid JSON payload"})
                return
            if not self.task_manager:
                self._send_json(503, {"error": "TaskManager not enabled"})
                return
            agent = data.get("agent", self.default_reviewer)
            prompt = data.get("prompt", "")
            if not prompt:
                self._send_json(400, {"error": "Missing required 'prompt' field"})
                return
            try:
                task = self.task_manager.submit_task(
                    agent=agent,
                    prompt=prompt,
                    goal_prompt=data.get("goal_prompt"),
                    use_goal=data.get("use_goal", True),
                    target_id=data.get("target_id"),
                    repo_full_name=data.get("repo_full_name"),
                    repo_name=data.get("repo_name"),
                    clone_url=data.get("clone_url"),
                )
                if self.dashboard_updater:
                    self.dashboard_updater.trigger_update()
                self._send_json(200, {"status": "submitted", "task_id": task.id})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        if path_clean.startswith("/tasks/") and path_clean.endswith("/abort"):
            task_id = path_clean[len("/tasks/"):-len("/abort")].rstrip("/")
            if not task_id:
                self._send_json(400, {"error": "Missing task ID in path"})
                return
            if not self.task_manager:
                self._send_json(503, {"error": "TaskManager not enabled"})
                return
            success = self.task_manager.abort_task(task_id)
            if success:
                if self.dashboard_updater:
                    self.dashboard_updater.trigger_update()
                self._send_json(200, {"status": "aborted", "task_id": task_id})
            else:
                self._send_json(404, {"error": f"Task '{task_id}' could not be aborted"})
            return

        if path_clean == "/dashboard/register":
            try:
                data = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
            except Exception:
                self._send_json(400, {"error": "Invalid JSON payload"})
                return
            target_path = data.get("path")
            if not target_path:
                self._send_json(400, {"error": "Missing required 'path' field"})
                return
            if not self.dashboard_updater:
                self._send_json(503, {"error": "DashboardUpdater not initialized"})
                return
            self.dashboard_updater.register_target(target_path)
            self._send_json(200, {
                "status": "ok",
                "registered": str(Path(target_path).resolve()),
                "targets": self.dashboard_updater.get_targets(),
            })
            return

        if path_clean == "/dashboard/unregister":
            try:
                data = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
            except Exception:
                self._send_json(400, {"error": "Invalid JSON payload"})
                return
            target_path = data.get("path")
            if not target_path:
                self._send_json(400, {"error": "Missing required 'path' field"})
                return
            if not self.dashboard_updater:
                self._send_json(503, {"error": "DashboardUpdater not initialized"})
                return
            self.dashboard_updater.unregister_target(target_path)
            self._send_json(200, {
                "status": "ok",
                "unregistered": str(Path(target_path).resolve()),
                "targets": self.dashboard_updater.get_targets(),
            })
            return

        # Verify HMAC signature if secret is configured
        if self.secret:
            sig_header = self.headers.get("X-Hub-Signature-256", "")
            if not verify_signature(payload_bytes, self.secret, sig_header):
                logger.warning("Invalid or missing HMAC signature received.")
                self._send_json(401, {"error": "Invalid signature"})
                return

        event_type = self.headers.get("X-GitHub-Event", "unknown")

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            logger.error("Failed to parse JSON payload.")
            self._send_json(400, {"error": "Invalid JSON payload"})
            return

        target_summary = format_event_summary(event_type, payload)
        logger.info(f"Received GitHub webhook event: {event_type} ({target_summary})")

        # Route event using lib.router
        decision = route_webhook_event(
            event_type=event_type,
            payload=payload,
            default_reviewer=self.default_reviewer,
            default_fixer=self.default_fixer,
            default_triager=self.default_triager,
            default_drafter=self.default_drafter,
            pr_tracker=self.pr_tracker,
            server_repo_name=getattr(self, "server_repo_name", get_server_repo_name(REPO_ROOT)),
            repo_root=REPO_ROOT,
            repos_dir=getattr(self, "repos_dir", None),
        )

        status = decision.get("status", "unknown")
        agent = decision.get("agent")
        reason = decision.get("reason")
        action = decision.get("action")

        if status == "accepted":
            if agent:
                logger.info(f"Routed webhook event '{event_type}' ({target_summary}): status=accepted, agent={agent}")
            elif action:
                logger.info(f"Routed webhook event '{event_type}' ({target_summary}): status=accepted, action={action}")
            else:
                logger.info(f"Routed webhook event '{event_type}' ({target_summary}): status=accepted")
        else:
            if reason:
                logger.info(f"Routed webhook event '{event_type}' ({target_summary}): status=ignored, reason={reason}")
            else:
                logger.info(f"Routed webhook event '{event_type}' ({target_summary}): status=ignored")

        if decision.get("status") == "accepted":
            if decision.get("action") == "ping":
                self._send_json(200, {"message": "pong", "zen": decision.get("zen", "")})
                return

            if decision.get("action") == "self_update":
                ref = decision.get("ref", "refs/heads/main")
                self._send_json(200, {
                    "status": "accepted",
                    "action": "self_update",
                    "ref": ref,
                    "message": "Self-update triggered. Syncing repository and reloading server...",
                })
                threading.Thread(
                    target=sync_repo_and_reload,
                    args=(REPO_ROOT, ref, self.server, self.task_manager, getattr(self, "listener_proc", None), getattr(self, "quota_tracker", None)),
                    daemon=True,
                ).start()
                return

            if decision.get("action") == "release":
                release_type = decision.get("release_type", "release")
                repo_name = decision.get("repo_name")
                repo_full_name = decision.get("repo_full_name") or repo_name
                issue_number = decision.get("issue_number")
                command = decision.get("command")
                branch = decision.get("branch") or DEFAULT_BRANCH
                pre_flight = decision.get("pre_flight")
                repo_dir = decision.get("repo_dir")

                if not repo_dir:
                    repo_dir = resolve_repo_dir(repo_name, repo_root=REPO_ROOT, repos_dir=getattr(self, "repos_dir", None))

                self._send_json(200, {
                    "status": "accepted",
                    "action": "release",
                    "release_type": release_type,
                    "repo_name": repo_name,
                    "message": f"Release '{release_type}' triggered.",
                })

                post_emoji_reaction_async(event_type, payload, reaction="rocket")

                if repo_dir and Path(repo_dir).exists():
                    execute_release_async(
                        repo_dir=Path(repo_dir),
                        repo_full_name=repo_full_name,
                        issue_number=issue_number,
                        release_type=release_type,
                        command=command,
                        target_branch=branch,
                        pre_flight_checks=pre_flight,
                    )
                else:
                    logger.error(f"Cannot execute release: directory '{repo_dir}' not found for repo '{repo_name}'")
                    if repo_full_name and issue_number:
                        threading.Thread(
                            target=post_issue_comment,
                            args=(
                                repo_full_name,
                                issue_number,
                                f"❌ **Release Failed**: Repository directory not found on Graviton server for `{repo_name}`.",
                            ),
                            daemon=True,
                            name="ReleaseRepoDirNotFoundThread",
                        ).start()
                return

            if decision.get("action") == "release_init":
                repo_full_name = decision.get("repo_full_name") or decision.get("repo_name")
                issue_number = decision.get("issue_number")
                release_config = decision.get("release_config")

                self._send_json(200, {
                    "status": "accepted",
                    "action": "release_init",
                    "message": "Release issue initialized.",
                })
                post_emoji_reaction_async(event_type, payload, reaction="rocket")
                if repo_full_name and issue_number:
                    post_release_init_async(repo_full_name, issue_number, release_config)
                return

            if decision.get("action") == "release_help":
                repo_full_name = decision.get("repo_full_name") or decision.get("repo_name")
                issue_number = decision.get("issue_number")
                release_config = decision.get("release_config")

                self._send_json(200, {
                    "status": "accepted",
                    "action": "release_help",
                    "message": "Release help requested.",
                })
                post_emoji_reaction_async(event_type, payload, reaction="eyes")
                if repo_full_name and issue_number:
                    post_release_help_async(repo_full_name, issue_number, release_config)
                return

            if decision.get("action") == "release_unrecognized":
                repo_full_name = decision.get("repo_full_name") or decision.get("repo_name")
                issue_number = decision.get("issue_number")
                comment_body = decision.get("comment_body", "")
                release_config = decision.get("release_config")

                self._send_json(200, {
                    "status": "accepted",
                    "action": "release_unrecognized",
                    "message": "Unrecognized release command.",
                })
                if repo_full_name and issue_number:
                    post_release_unrecognized_async(repo_full_name, issue_number, comment_body, release_config)
                return

            agent = decision.get("agent")
            prompt = decision.get("prompt")
            goal_prompt = decision.get("goal_prompt")
            if agent and prompt:
                target_num = decision.get("pr_number") or decision.get("issue_number")
                target_id = f"#{target_num}" if target_num is not None else None
                repo_full_name = decision.get("repo_full_name")
                repo_name = decision.get("repo_name")
                clone_url = decision.get("clone_url")

                if self.task_manager:
                    try:
                        submit_kwargs = {
                            "agent": agent,
                            "prompt": prompt,
                            "target_id": target_id,
                            "repo_full_name": repo_full_name,
                            "repo_name": repo_name,
                            "clone_url": clone_url,
                        }
                        tm_use_sup = getattr(self.task_manager, "use_supervisor", None)
                        is_sup = (
                            is_supervisor_active(self)
                            or tm_use_sup is True
                            or (tm_use_sup is None and getattr(self.task_manager, "script_path", "unset") is None)
                        )
                        if is_sup:
                            if goal_prompt:
                                submit_kwargs["goal_prompt"] = goal_prompt
                            if event_type:
                                submit_kwargs["webhook_event_type"] = event_type
                            if payload:
                                submit_kwargs["webhook_payload"] = payload

                        self.task_manager.submit_task(**submit_kwargs)
                        post_emoji_reaction_async(event_type, payload)
                    except RuntimeError as e:
                        logger.warning(f"Could not submit task: {e}")
                        if "pacing" in str(e).lower():
                            self._send_json(200, {"status": "ignored", "reason": "behind_quota_pacing"})
                        else:
                            self._send_json(503, {"error": str(e)})
                        return
                else:
                    qt = getattr(self, "quota_tracker", None)
                    if qt:
                        if hasattr(qt, "is_behind_pacing") and callable(getattr(qt, "is_behind_pacing", None)):
                            res = qt.is_behind_pacing()
                            if res is True:
                                logger.warning("Task acceptance suspended due to quota pacing deficit. Skipping task submission.")
                                self._send_json(200, {"status": "ignored", "reason": "behind_quota_pacing"})
                                return
                        if getattr(qt, "state", None) == QuotaState.EXHAUSTED:
                            logger.warning("Task acceptance suspended due to quota exhaustion. Skipping task submission.")
                            self._send_json(503, {"error": "Cannot accept new task: quota is exhausted"})
                            return

                    exec_cwd = REPO_ROOT
                    if repo_name and hasattr(self, "repos_dir") and self.repos_dir:
                        if not is_valid_repo_name(repo_name):
                            logger.warning(f"Unsafe or invalid repo_name '{repo_name}' attempting path traversal out of {self.repos_dir}")
                            self._send_json(400, {"error": f"Unsafe or invalid repo_name '{repo_name}' attempting path traversal out of {self.repos_dir}"})
                            return
                        candidate_cwd = (self.repos_dir / repo_name).resolve()
                        repos_dir_resolved = self.repos_dir.resolve()
                        if candidate_cwd != repos_dir_resolved and repos_dir_resolved in candidate_cwd.parents:
                            exec_cwd = candidate_cwd
                        else:
                            logger.warning(f"Unsafe or invalid repo_name '{repo_name}' attempting path traversal out of {self.repos_dir}")
                            self._send_json(400, {"error": f"Unsafe or invalid repo_name '{repo_name}' attempting path traversal out of {self.repos_dir}"})
                            return

                    if exec_cwd and not exec_cwd.exists() and clone_url:
                        logger.info(f"Repository directory '{exec_cwd}' does not exist in direct execution mode. Auto-cloning from {clone_url}...")
                        try:
                            exec_cwd.parent.mkdir(parents=True, exist_ok=True)
                            subprocess.run(
                                ["git", "clone", "--", clone_url, str(exec_cwd)],
                                check=True,
                                capture_output=True,
                                text=True,
                            )
                            logger.info(f"Successfully auto-cloned repository to '{exec_cwd}'.")
                        except Exception as clone_err:
                            logger.error(f"Failed to auto-clone repository '{clone_url}' into '{exec_cwd}': {clone_err}")
                            self._send_json(500, {"error": f"Failed to auto-clone repository '{clone_url}': {clone_err}"})
                            return

                    if exec_cwd and not exec_cwd.exists():
                        logger.error(f"Repository directory '{exec_cwd}' does not exist.")
                        self._send_json(400, {"error": f"Repository directory '{exec_cwd}' does not exist"})
                        return

                    post_emoji_reaction_async(event_type, payload)
                    if is_supervisor_active(self):
                        from lib.supervisor import run_container_goal, run_container_turn
                        target_fn = run_container_goal if goal_prompt else run_container_turn
                        target_p = goal_prompt or prompt
                        threading.Thread(
                            target=target_fn,
                            args=(target_p, exec_cwd),
                            kwargs={"agent_name": agent},
                            daemon=True,
                        ).start()
                    else:
                        run_agent_async(agent, prompt, RUN_CONTAINER_SCRIPT, exec_cwd)

            # Omit internal prompt from HTTP response output
            response_payload = {k: v for k, v in decision.items() if k != "prompt"}
            self._send_json(200, response_payload)
            return
        else:
            self._send_json(200, decision)
            return

    def _send_json(self, status_code: int, data: dict):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))

    def _send_html(self, status_code: int, html_content: str):
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_content.encode("utf-8"))

    def log_message(self, format, *args):
        """Suppress default HTTP log formatting to use our logger."""
        logger.debug("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format % args))


def main():
    parser = argparse.ArgumentParser(description="Graviton Webhook Server & Event Router")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"), help="Host IP to bind (default: 0.0.0.0)")
    parser.add_argument("--port", "-p", type=int, default=int(os.getenv("PORT", "8000")), help="Port to bind (default: 8000)")
    parser.add_argument("--secret", "-s", default=os.getenv("WEBHOOK_SECRET", os.getenv("GITHUB_WEBHOOK_SECRET", "")), help="GitHub webhook secret for HMAC verification")
    parser.add_argument("--repos-dir", "--projects-dir", default=os.getenv("REPOS_DIR", os.getenv("PROJECTS_DIR", "~/graviton-repos")), help="Base directory for managed repository checkouts (env: REPOS_DIR or PROJECTS_DIR, default: ~/graviton-repos)")
    parser.add_argument("--reviewer", default=os.getenv("DEFAULT_REVIEWER", "code_reviewer"), help="Reviewer agent name (default: code_reviewer)")
    parser.add_argument("--fixer", default=os.getenv("DEFAULT_FIXER", "code_fixer"), help="Fixer agent name (default: code_fixer)")
    parser.add_argument("--triager", default=os.getenv("DEFAULT_TRIAGER", "issue_triager"), help="Triager agent name (default: issue_triager)")
    parser.add_argument("--drafter", default=os.getenv("DEFAULT_DRAFTER", "pr_drafter"), help="Drafter agent name (default: pr_drafter)")
    parser.add_argument("--schedules-config", default=os.getenv("SCHEDULES_CONFIG", str(REPO_ROOT / "config" / "schedules.json")), help="Path to schedule JSON configuration file")
    parser.add_argument("--schedules-state", default=os.getenv("SCHEDULES_STATE", str(REPO_ROOT / ".graviton_scheduler_state.json")), help="Path to schedule execution state JSON file")
    parser.add_argument("--smee-url", default=os.getenv("SMEE_URL", ""), help="Smee.io channel URL for launching local webhook proxy listener (env: SMEE_URL)")
    parser.add_argument("--max-workers", "-w", type=int, default=int(os.getenv("MAX_WORKERS", "2")), help="Max concurrent agent worker threads (default: 2)")
    parser.add_argument("--max-tasks", type=int, default=int(os.getenv("MAX_TASKS", "1000")), help="Max tasks retained in memory (default: 1000)")
    parser.add_argument("--quota-pool", default=os.getenv("ANTIGRAVITY_QUOTA_POOL", "gemini"), help="Target quota pool to track (e.g., gemini, claude_gpt) (default: gemini)")
    parser.add_argument(
        "--quota-endpoint",
        default=os.getenv("ANTIGRAVITY_QUOTA_ENDPOINT", ""),
        help="Antigravity RPC endpoint URL for quota retrieval (default: auto-detected or env ANTIGRAVITY_QUOTA_ENDPOINT)",
    )
    parser.add_argument(
        "--model-state",
        "--model-selection-state",
        default=os.getenv("MODEL_SELECTION_STATE", str(REPO_ROOT / ".graviton_model_selection.json")),
        help="Path to persisted model selection state JSON file (default: REPO_ROOT/.graviton_model_selection.json)",
    )
    parser.add_argument("--quit-grace-period", type=float, default=float(os.getenv("QUIT_GRACE_PERIOD", "3.0")), help="Grace period (seconds) to accept webhooks after draining active tasks during shutdown (default: 3.0)")
    parser.add_argument(
        "--supervisor",
        dest="use_supervisor",
        action="store_true",
        default=os.getenv("GRAVITON_USE_SUPERVISOR", "true").lower() in ("1", "true", "yes"),
        help="Use Antigravity ContainerSupervisor pipeline (default: True, env: GRAVITON_USE_SUPERVISOR)",
    )
    parser.add_argument(
        "--no-supervisor",
        dest="use_supervisor",
        action="store_false",
        help="[DEPRECATED] Use legacy bash container runner (bin/run_agent_container.sh)",
    )
    parser.add_argument(
        "--post-completion-comment",
        action="store_true",
        default=os.getenv("GRAVITON_POST_COMPLETION_COMMENT", "").lower() in ("1", "true", "yes"),
        help="Post a completion comment to GitHub issue/PR upon supervisor task finish",
    )
    parser.add_argument(
        "--post-start-comment",
        action="store_true",
        default=os.getenv("GRAVITON_POST_START_COMMENT", "").lower() in ("1", "true", "yes"),
        help="Post an initial progress comment with live remote control link to GitHub issue/PR upon supervisor task start",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        default=os.getenv("GRAVITON_ENABLE_TUI", "false").lower() in ("1", "true", "yes"),
        help="[DEPRECATED] Run interactive curses Terminal UI dashboard (default: False, env: GRAVITON_ENABLE_TUI)",
    )
    parser.add_argument(
        "--dashboard-target",
        action="append",
        default=[],
        help="Optional file path to continuously update with live dashboard markdown (can specify multiple times)",
    )
    args = parser.parse_args()

    # Strip console StreamHandler ONLY if running interactive curses TUI
    if getattr(args, "tui", False):
        _root = logging.getLogger()
        for _h in list(_root.handlers):
            if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
                _root.removeHandler(_h)

    repos_dir = Path(args.repos_dir).expanduser().resolve()
    GravitonHandler.secret = args.secret
    GravitonHandler.default_reviewer = args.reviewer
    GravitonHandler.default_fixer = args.fixer
    GravitonHandler.default_triager = args.triager
    GravitonHandler.default_drafter = args.drafter
    GravitonHandler.repos_dir = repos_dir

    if not args.use_supervisor:
        logger.warning("[DEPRECATED] --no-supervisor is deprecated. ContainerSupervisor is the standard execution runtime.")
        if not RUN_CONTAINER_SCRIPT.exists():
            logger.error(f"Run agent container script not found at: {RUN_CONTAINER_SCRIPT}")
            sys.exit(1)

    if not args.secret:
        logger.warning("No WEBHOOK_SECRET specified. HMAC signature verification is DISABLED.")
    else:
        logger.info("HMAC signature verification ENABLED.")

    listener_proc = start_smee_listener(args.smee_url, args.port) if args.smee_url else None
    GravitonHandler.listener_proc = listener_proc

    scheduler = None
    dashboard = None
    dashboard_updater = None
    task_manager = None
    httpd = None
    shutdown_thread: Optional[threading.Thread] = None

    try:
        quota_kwargs = {
            "quota_pool": args.quota_pool,
            "state_path": Path(args.model_state),
        }
        if args.quota_endpoint:
            quota_kwargs["api_url"] = args.quota_endpoint
        quota_tracker = QuotaTracker(**quota_kwargs)
        GravitonHandler.quota_tracker = quota_tracker
        quota_tracker.restore_model_selection()
        try:
            quota_tracker.poll_all_pools()
        except Exception as e:
            logger.warning(f"Initial live quota poll for all pools failed: {e}")

        task_manager = TaskManager(
            max_workers=args.max_workers,
            max_tasks=args.max_tasks,
            script_path=RUN_CONTAINER_SCRIPT,
            cwd=REPO_ROOT,
            quota_tracker=quota_tracker,
            repos_dir=repos_dir,
            use_supervisor=args.use_supervisor,
            post_completion_comment=args.post_completion_comment,
            post_start_comment=args.post_start_comment,
        )
        restored_count = task_manager.restore_queue_state()
        if restored_count > 0:
            logger.info(f"Restored {restored_count} queued task(s) from persisted state.")
        task_manager.start()
        GravitonHandler.task_manager = task_manager

        config_path = Path(args.schedules_config)
        state_path = Path(args.schedules_state)
        logger.info(f"Initializing Periodic TaskScheduler using config: {config_path}, state: {state_path}")
        scheduler = TaskScheduler(
            config_path=config_path,
            state_path=state_path,
            runner=run_agent_async,
            script_path=RUN_CONTAINER_SCRIPT,
            cwd=REPO_ROOT,
            task_manager=task_manager,
            quota_tracker=quota_tracker,
            repos_dir=repos_dir,
        )
        scheduler.start()
        GravitonHandler.scheduler = scheduler

        pr_tracker = PRTracker()
        pr_tracker.sync_in_background(repo_root=REPO_ROOT, repos_dir=repos_dir)
        GravitonHandler.pr_tracker = pr_tracker
        GravitonHandler.use_supervisor = args.use_supervisor

        server_address = (args.host, args.port)
        GravitonHandler.server_host = args.host
        GravitonHandler.server_port = args.port
        httpd = HTTPServer(server_address, GravitonHandler)
        httpd.use_supervisor = args.use_supervisor

        def shutdown_signal_handler(signum, frame):
            nonlocal shutdown_thread
            logger.info(f"Received signal {signum}, starting graceful Graviton Webhook Server shutdown...")
            shutdown_thread = graceful_shutdown(
                task_manager=task_manager,
                scheduler=scheduler,
                dashboard=dashboard,
                httpd=httpd,
                quota_tracker=quota_tracker,
                grace_period=args.quit_grace_period,
            )

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, shutdown_signal_handler)
            except (ValueError, TypeError, AttributeError):
                pass

        # Always initialize and start the background DashboardUpdater for live artifacts/web
        dashboard_updater = DashboardUpdater(
            task_manager=task_manager,
            quota_tracker=quota_tracker,
            scheduler=scheduler,
            host=args.host,
            port=args.port,
        )
        for target_path in args.dashboard_target:
            dashboard_updater.register_target(target_path)
        dashboard_updater.start()
        GravitonHandler.dashboard_updater = dashboard_updater

        # Hook task lifecycle events into dashboard_updater
        orig_on_init = getattr(task_manager, "on_task_init", None)
        orig_on_result = getattr(task_manager, "on_task_result", None)
        orig_on_thought = getattr(task_manager, "on_task_thought", None)
        orig_on_tool = getattr(task_manager, "on_task_tool_call", None)

        def _hook_init(task):
            if orig_on_init:
                try:
                    orig_on_init(task)
                except Exception:
                    pass
            dashboard_updater.trigger_update()

        def _hook_result(task):
            if orig_on_result:
                try:
                    orig_on_result(task)
                except Exception:
                    pass
            dashboard_updater.trigger_update()

        def _hook_thought(task, thought):
            if orig_on_thought:
                try:
                    orig_on_thought(task, thought)
                except Exception:
                    pass
            dashboard_updater.trigger_update()

        def _hook_tool(task, tool):
            if orig_on_tool:
                try:
                    orig_on_tool(task, tool)
                except Exception:
                    pass
            dashboard_updater.trigger_update()

        task_manager.on_task_init = _hook_init
        task_manager.on_task_result = _hook_result
        task_manager.on_task_thought = _hook_thought
        task_manager.on_task_tool_call = _hook_tool

        if args.tui:
            logger.warning("[DEPRECATED] Terminal UI (--tui) is deprecated. Use the Graviton Antigravity plugin and live dashboard.")
            # Right before dashboard.start(), strip root StreamHandler
            _root = logging.getLogger()
            for _h in list(_root.handlers):
                if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
                    _root.removeHandler(_h)

            dashboard = TerminalDashboard(
                task_manager=task_manager,
                host=args.host,
                port=args.port,
                repo_root=REPO_ROOT,
                scheduler=scheduler,
                pr_tracker=pr_tracker,
                quota_tracker=quota_tracker,
                quit_grace_period=args.quit_grace_period,
                httpd=httpd,
            )
            dashboard.start()
            logger.info("Live Terminal UI Dashboard ENABLED.")
        else:
            dashboard = None
            logger.info("Graviton Webhook Server running in headless daemon mode.")

        logger.info(f"Starting Graviton Webhook Server on {args.host}:{args.port}...")
        logger.info(f"Agents: Reviewer='{args.reviewer}', Fixer='{args.fixer}', Triager='{args.triager}', Drafter='{args.drafter}'")

        try:
            httpd.serve_forever()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Stopping Graviton Webhook Server...")
    finally:
        stop_smee_listener(listener_proc)
        if dashboard_updater:
            try:
                dashboard_updater.stop()
            except Exception:
                pass
        st = shutdown_thread or (getattr(dashboard, "_shutdown_thread", None) if dashboard else None) or _shutdown_thread
        if st and st.is_alive() and st != threading.current_thread():
            st.join()
        if scheduler:
            try:
                scheduler.stop()
            except Exception:
                pass
        if dashboard:
            try:
                dashboard.stop()
            except Exception:
                pass
        if task_manager:
            try:
                task_manager.stop()
            except Exception:
                pass
        if httpd:
            try:
                httpd.server_close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
