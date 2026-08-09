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
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

# Add REPO_ROOT to sys.path to allow importing lib
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.security import verify_signature
from lib.router import route_webhook_event
from lib.runner import run_agent_async
from lib.updater import sync_repo_and_reload
from lib.scheduler import TaskScheduler
from lib.tasks import TaskManager
from lib.tui import TerminalDashboard

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("graviton")

RUN_CONTAINER_SCRIPT = REPO_ROOT / "bin" / "run_agent_container.sh"


class GravitonHandler(BaseHTTPRequestHandler):
    secret: str = ""
    default_reviewer: str = "code_reviewer"
    default_fixer: str = "code_fixer"
    default_triager: str = "issue_triager"
    scheduler: Optional[TaskScheduler] = None
    task_manager: Optional[TaskManager] = None

    def do_GET(self):
        """Health check endpoint."""
        if self.path in ("/", "/health"):
            sched = GravitonHandler.scheduler
            tasks_info = self.task_manager.get_stats() if self.task_manager else {}
            self._send_json(200, {
                "status": "ok",
                "service": "graviton-server",
                "reviewer_agent": self.default_reviewer,
                "fixer_agent": self.default_fixer,
                "triager_agent": self.default_triager,
                "scheduler_enabled": sched is not None,
                "scheduler_running": sched.is_running() if sched else False,
                "active_jobs": len(sched.jobs) if sched else 0,
                "tasks": tasks_info,
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
        logger.info(f"Received GitHub webhook event: {event_type}")

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            logger.error("Failed to parse JSON payload.")
            self._send_json(400, {"error": "Invalid JSON payload"})
            return

        # Route event using lib.router
        decision = route_webhook_event(
            event_type=event_type,
            payload=payload,
            default_reviewer=self.default_reviewer,
            default_fixer=self.default_fixer,
            default_triager=self.default_triager,
        )

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
                    args=(REPO_ROOT, ref, self.server, self.task_manager),
                    daemon=True,
                ).start()
                return

            agent = decision.get("agent")
            prompt = decision.get("prompt")
            if agent and prompt:
                target_num = decision.get("pr_number") or decision.get("issue_number")
                target_id = f"#{target_num}" if target_num is not None else None
                if self.task_manager:
                    try:
                        self.task_manager.submit_task(agent=agent, prompt=prompt, target_id=target_id)
                    except RuntimeError as e:
                        logger.warning(f"Could not submit task: {e}")
                        self._send_json(503, {"error": "Server is draining tasks for update"})
                        return
                else:
                    run_agent_async(agent, prompt, RUN_CONTAINER_SCRIPT, REPO_ROOT)

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
    parser.add_argument("--reviewer", default=os.getenv("DEFAULT_REVIEWER", "code_reviewer"), help="Reviewer agent name (default: code_reviewer)")
    parser.add_argument("--fixer", default=os.getenv("DEFAULT_FIXER", "code_fixer"), help="Fixer agent name (default: code_fixer)")
    parser.add_argument("--triager", default=os.getenv("DEFAULT_TRIAGER", "issue_triager"), help="Triager agent name (default: issue_triager)")
    parser.add_argument("--enable-scheduler", action="store_true", default=os.getenv("ENABLE_SCHEDULER", "false").lower() in ("true", "1", "yes"), help="Enable periodic task scheduler on server startup")
    parser.add_argument("--schedules-config", default=os.getenv("SCHEDULES_CONFIG", str(REPO_ROOT / "config" / "schedules.json")), help="Path to schedule JSON configuration file")
    parser.add_argument("--dashboard", "-d", action="store_true", help="Enable live terminal UI dashboard")
    parser.add_argument("--max-workers", "-w", type=int, default=int(os.getenv("MAX_WORKERS", "2")), help="Max concurrent agent worker threads (default: 2)")
    parser.add_argument("--max-tasks", type=int, default=int(os.getenv("MAX_TASKS", "1000")), help="Max tasks retained in memory (default: 1000)")
    args = parser.parse_args()

    GravitonHandler.secret = args.secret
    GravitonHandler.default_reviewer = args.reviewer
    GravitonHandler.default_fixer = args.fixer
    GravitonHandler.default_triager = args.triager

    if not args.secret:
        logger.warning("No WEBHOOK_SECRET specified. HMAC signature verification is DISABLED.")
    else:
        logger.info("HMAC signature verification ENABLED.")

    if not RUN_CONTAINER_SCRIPT.exists():
        logger.error(f"Run agent container script not found at: {RUN_CONTAINER_SCRIPT}")
        sys.exit(1)

    scheduler: Optional[TaskScheduler] = None
    if args.enable_scheduler:
        config_path = Path(args.schedules_config)
        logger.info(f"Initializing Periodic TaskScheduler using config: {config_path}")
        scheduler = TaskScheduler(
            config_path=config_path,
            runner=run_agent_async,
            script_path=RUN_CONTAINER_SCRIPT,
            cwd=REPO_ROOT,
        )
        scheduler.start()
        GravitonHandler.scheduler = scheduler

    task_manager = TaskManager(
        max_workers=args.max_workers,
        max_tasks=args.max_tasks,
        script_path=RUN_CONTAINER_SCRIPT,
        cwd=REPO_ROOT,
    )
    restored_count = task_manager.restore_queue_state()
    if restored_count > 0:
        logger.info(f"Restored {restored_count} queued task(s) from persisted state.")
    task_manager.start()
    GravitonHandler.task_manager = task_manager

    dashboard = None
    if args.dashboard:
        dashboard = TerminalDashboard(
            task_manager=task_manager,
            host=args.host,
            port=args.port,
            repo_root=REPO_ROOT,
            scheduler=scheduler,
        )
        dashboard.start()

    server_address = (args.host, args.port)
    httpd = HTTPServer(server_address, GravitonHandler)
    logger.info(f"Starting Graviton Webhook Server on {args.host}:{args.port}...")
    logger.info(f"Agents: Reviewer='{args.reviewer}', Fixer='{args.fixer}', Triager='{args.triager}'")
    logger.info(f"Scheduler enabled: {args.enable_scheduler}")
    if args.dashboard:
        logger.info("Live Terminal UI Dashboard ENABLED.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Graviton Webhook Server...")
    finally:
        if scheduler:
            scheduler.stop()
        if dashboard:
            dashboard.stop()
        task_manager.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
