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
from typing import Optional

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


_is_shutting_down = False
_shutdown_thread: Optional[threading.Thread] = None
_shutdown_lock = threading.Lock()


def graceful_shutdown(
    task_manager: Optional[TaskManager] = None,
    scheduler: Optional[TaskScheduler] = None,
    dashboard: Optional[TerminalDashboard] = None,
    httpd: Optional[HTTPServer] = None,
    grace_period: float = 3.0,
    timeout: Optional[float] = None,
) -> threading.Thread:
    """
    Execute 4-step graceful shutdown sequence in a background thread:
    1. Drain Active Tasks (task_manager.drain_active_tasks)
    2. Webhook Grace Buffer (sleep grace_period seconds)
    3. Shutdown HTTP Listener (httpd.shutdown) & Persist Task Queue (task_manager.dump_queue_state)
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
        return subprocess.Popen([str(RUN_LISTENER_SCRIPT), smee_url, str(port)])
    except Exception as e:
        logger.error(f"Failed to start smee listener process: {e}")
        return None


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
    listener_proc: Optional[subprocess.Popen] = None

    def do_GET(self):
        """Health check endpoint."""
        if self.path in ("/", "/health"):
            sched = GravitonHandler.scheduler
            tasks_info = self.task_manager.get_stats() if self.task_manager else {}
            quota_info = self.quota_tracker.get_info().to_dict() if self.quota_tracker else {}
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
                "quota": quota_info,
            })
        else:
            self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        """Handle incoming GitHub Webhook POST request."""
        content_length = int(self.headers.get("Content-Length", 0))
        payload_bytes = self.rfile.read(content_length)

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
                    args=(REPO_ROOT, ref, self.server, self.task_manager, getattr(self, "listener_proc", None)),
                    daemon=True,
                ).start()
                return

            agent = decision.get("agent")
            prompt = decision.get("prompt")
            if agent and prompt:
                target_num = decision.get("pr_number") or decision.get("issue_number")
                target_id = f"#{target_num}" if target_num is not None else None
                repo_full_name = decision.get("repo_full_name")
                repo_name = decision.get("repo_name")
                clone_url = decision.get("clone_url")

                if self.task_manager:
                    try:
                        self.task_manager.submit_task(
                            agent=agent,
                            prompt=prompt,
                            target_id=target_id,
                            repo_full_name=repo_full_name,
                            repo_name=repo_name,
                            clone_url=clone_url,
                        )
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
                            import subprocess
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
    parser.add_argument("--quit-grace-period", type=float, default=float(os.getenv("QUIT_GRACE_PERIOD", "3.0")), help="Grace period (seconds) to accept webhooks after draining active tasks during shutdown (default: 3.0)")
    args = parser.parse_args()

    repos_dir = Path(args.repos_dir).expanduser().resolve()
    GravitonHandler.secret = args.secret
    GravitonHandler.default_reviewer = args.reviewer
    GravitonHandler.default_fixer = args.fixer
    GravitonHandler.default_triager = args.triager
    GravitonHandler.default_drafter = args.drafter
    GravitonHandler.repos_dir = repos_dir

    if not args.secret:
        logger.warning("No WEBHOOK_SECRET specified. HMAC signature verification is DISABLED.")
    else:
        logger.info("HMAC signature verification ENABLED.")

    if not RUN_CONTAINER_SCRIPT.exists():
        logger.error(f"Run agent container script not found at: {RUN_CONTAINER_SCRIPT}")
        sys.exit(1)

    listener_proc = start_smee_listener(args.smee_url, args.port) if args.smee_url else None
    GravitonHandler.listener_proc = listener_proc

    quota_tracker = QuotaTracker(quota_pool=args.quota_pool)
    GravitonHandler.quota_tracker = quota_tracker
    try:
        quota_tracker.poll_live_quota()
    except Exception as e:
        logger.warning(f"Initial live quota poll failed: {e}")

    task_manager = TaskManager(
        max_workers=args.max_workers,
        max_tasks=args.max_tasks,
        script_path=RUN_CONTAINER_SCRIPT,
        cwd=REPO_ROOT,
        quota_tracker=quota_tracker,
        repos_dir=repos_dir,
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
    )
    scheduler.start()
    GravitonHandler.scheduler = scheduler

    pr_tracker = PRTracker()
    pr_tracker.sync_in_background(repo_root=REPO_ROOT, repos_dir=repos_dir)
    GravitonHandler.pr_tracker = pr_tracker

    server_address = (args.host, args.port)
    httpd = HTTPServer(server_address, GravitonHandler)

    shutdown_thread: Optional[threading.Thread] = None

    def shutdown_signal_handler(signum, frame):
        nonlocal shutdown_thread
        logger.info(f"Received signal {signum}, starting graceful Graviton Webhook Server shutdown...")
        shutdown_thread = graceful_shutdown(
            task_manager=task_manager,
            scheduler=scheduler,
            dashboard=dashboard,
            httpd=httpd,
            grace_period=args.quit_grace_period,
        )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown_signal_handler)
        except (ValueError, TypeError, AttributeError):
            pass

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

    logger.info(f"Starting Graviton Webhook Server on {args.host}:{args.port}...")
    logger.info(f"Agents: Reviewer='{args.reviewer}', Fixer='{args.fixer}', Triager='{args.triager}', Drafter='{args.drafter}'")
    logger.info("Live Terminal UI Dashboard ENABLED.")

    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping Graviton Webhook Server...")
    finally:
        stop_smee_listener(listener_proc)
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
        try:
            httpd.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
