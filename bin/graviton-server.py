#!/usr/bin/env python3
"""
Graviton Webhook Server & Event Router.

Listens for GitHub webhook events (pull_request, pull_request_review,
pull_request_review_comment, issue_comment) and triggers sandboxed
Antigravity agent containers in response.

Includes loop protection (ignoring comments with '<!-- antigravity-auto-reply -->')
and routes human review feedback directly to the code_fixer agent.

Uses standard Python library only (0 external dependencies).
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("graviton")

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_CONTAINER_SCRIPT = REPO_ROOT / "bin" / "run_agent_container.sh"
BOT_MARKER = "<!-- antigravity-auto-reply -->"


def verify_signature(payload_bytes: bytes, secret: str, signature_header: str) -> bool:
    """Verify HMAC SHA256 signature from GitHub."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected_sig = signature_header.split("sha256=")[1]
    computed_sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_sig, computed_sig)


def run_agent_async(agent_name: str, prompt: str):
    """Run the agent container asynchronously in a separate thread."""
    def worker():
        logger.info(f"Triggering agent '{agent_name}' with prompt: '{prompt}'")
        cmd = [str(RUN_CONTAINER_SCRIPT), agent_name, prompt]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.info(f"Agent '{agent_name}' finished successfully for prompt: '{prompt}'")
                if result.stdout:
                    logger.info(f"Agent stdout:\n{result.stdout.strip()}")
            else:
                logger.error(f"Agent '{agent_name}' failed with exit code {result.returncode}")
                if result.stderr:
                    logger.error(f"Agent stderr:\n{result.stderr.strip()}")
        except Exception as e:
            logger.exception(f"Error executing agent container script: {e}")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


class GravitonHandler(BaseHTTPRequestHandler):
    secret: str = ""
    default_reviewer: str = "code_reviewer"
    default_fixer: str = "code_fixer"

    def do_GET(self):
        """Health check endpoint."""
        if self.path in ("/", "/health"):
            self._send_json(200, {
                "status": "ok",
                "service": "graviton-server",
                "reviewer_agent": self.default_reviewer,
                "fixer_agent": self.default_fixer,
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

        if event_type == "ping":
            self._send_json(200, {"message": "pong", "zen": payload.get("zen", "")})
            return

        # 1. Pull Request Events (Opened / Synchronized -> Code Reviewer)
        elif event_type == "pull_request":
            action = payload.get("action")
            pr_number = payload.get("number") or payload.get("pull_request", {}).get("number")
            logger.info(f"Pull request #{pr_number} action: {action}")

            if action in ("opened", "synchronize", "reopened"):
                prompt = f"Review PR #{pr_number}"
                run_agent_async(self.default_reviewer, prompt)
                self._send_json(200, {
                    "status": "accepted",
                    "action": action,
                    "pr_number": pr_number,
                    "agent": self.default_reviewer,
                })
                return
            else:
                self._send_json(200, {
                    "status": "ignored",
                    "reason": f"Pull request action '{action}' does not trigger review",
                })
                return

        # 2. Pull Request Submitted Review Events (Changes Requested -> Code Fixer)
        elif event_type == "pull_request_review":
            action = payload.get("action")
            review = payload.get("review", {})
            review_state = review.get("state", "").upper()
            review_body = review.get("body", "")
            pr_number = payload.get("pull_request", {}).get("number")

            if BOT_MARKER in review_body:
                logger.info("Dropping pull_request_review event generated by bot.")
                self._send_json(200, {"status": "ignored", "reason": "Bot self-review event dropped"})
                return

            if action == "submitted" and review_state == "CHANGES_REQUESTED":
                prompt = f"Resolve review feedback on PR #{pr_number}: '{review_body}'"
                run_agent_async(self.default_fixer, prompt)
                self._send_json(200, {
                    "status": "accepted",
                    "action": action,
                    "review_state": review_state,
                    "pr_number": pr_number,
                    "agent": self.default_fixer,
                })
                return
            else:
                self._send_json(200, {
                    "status": "ignored",
                    "reason": f"Review state '{review_state}' action '{action}' does not trigger fixer",
                })
                return

        # 3. Inline Line-by-Line Review Comments (Human -> Code Fixer)
        elif event_type == "pull_request_review_comment":
            action = payload.get("action")
            comment = payload.get("comment", {})
            comment_body = comment.get("body", "")
            file_path = comment.get("path", "")
            line = comment.get("line") or comment.get("original_line")
            pr_url = payload.get("pull_request", {}).get("html_url", "")
            pr_number = pr_url.rstrip("/").split("/")[-1] if pr_url else ""

            if BOT_MARKER in comment_body:
                logger.info("Dropping review comment generated by bot.")
                self._send_json(200, {"status": "ignored", "reason": "Bot comment dropped"})
                return

            if action == "created":
                prompt = f"Resolve review comment on PR #{pr_number} in file '{file_path}' (line {line}): '{comment_body}'"
                run_agent_async(self.default_fixer, prompt)
                self._send_json(200, {
                    "status": "accepted",
                    "pr_number": pr_number,
                    "file": file_path,
                    "line": line,
                    "agent": self.default_fixer,
                })
                return

        # 4. General Issue / PR Comments (@antigravity or /fix -> Code Fixer)
        elif event_type == "issue_comment":
            action = payload.get("action")
            comment = payload.get("comment", {})
            comment_body = comment.get("body", "")
            issue = payload.get("issue", {})
            pr = issue.get("pull_request")

            if BOT_MARKER in comment_body:
                logger.info("Dropping issue comment generated by bot.")
                self._send_json(200, {"status": "ignored", "reason": "Bot comment dropped"})
                return

            if pr and action == "created":
                pr_number = issue.get("number")
                if "@antigravity" in comment_body.lower() or "/fix" in comment_body.lower() or "/review" in comment_body.lower():
                    agent = self.default_reviewer if "/review" in comment_body.lower() else self.default_fixer
                    prompt = f"Address comment on PR #{pr_number}: '{comment_body}'"
                    run_agent_async(agent, prompt)
                    self._send_json(200, {
                        "status": "accepted",
                        "pr_number": pr_number,
                        "agent": agent,
                    })
                    return

            self._send_json(200, {
                "status": "ignored",
                "reason": "Comment did not trigger review or fix criteria",
            })
            return

        else:
            self._send_json(200, {
                "status": "ignored",
                "reason": f"Event type '{event_type}' not handled",
            })

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
    args = parser.parse_args()

    GravitonHandler.secret = args.secret
    GravitonHandler.default_reviewer = args.reviewer
    GravitonHandler.default_fixer = args.fixer

    if not args.secret:
        logger.warning("No WEBHOOK_SECRET specified. HMAC signature verification is DISABLED.")
    else:
        logger.info("HMAC signature verification ENABLED.")

    if not RUN_CONTAINER_SCRIPT.exists():
        logger.error(f"Run agent container script not found at: {RUN_CONTAINER_SCRIPT}")
        sys.exit(1)

    server_address = (args.host, args.port)
    httpd = HTTPServer(server_address, GravitonHandler)
    logger.info(f"Starting Graviton Webhook Server on {args.host}:{args.port}...")
    logger.info(f"Agents: Reviewer='{args.reviewer}', Fixer='{args.fixer}'")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Graviton Webhook Server...")
        httpd.shutdown()


if __name__ == "__main__":
    main()
