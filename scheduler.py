"""
scheduler.py — Automated daily scheduler with retry logic.

Features:
  - Runs pipeline at configured times daily
  - Handles failures gracefully (retries, logs, continues)
  - Sends failure alerts (optional webhook/email)
  - Health check endpoint (optional)
  - Graceful shutdown on SIGTERM
"""

import sys
import time
import signal
import threading
import traceback
from datetime import datetime, date
from loguru import logger

import config
import database as db
from pipeline import Pipeline


class Scheduler:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.running = True
        self.last_run_date: str = ""
        self.pipeline = Pipeline(dry_run=dry_run)

        # Graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        logger.info("📛 Shutdown signal received — stopping after current job")
        self.running = False

    def _should_run_now(self) -> bool:
        """Check if it's time to run based on configured upload times."""
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        today = date.today().isoformat()

        # Don't run twice in the same day
        if self.last_run_date == today:
            return False

        # Check if current time matches any scheduled time (within 1 minute window)
        for scheduled_time in config.UPLOAD_TIMES:
            scheduled_time = scheduled_time.strip()
            # Compare HH:MM strings
            if current_time == scheduled_time:
                return True

        return False

    def _run_with_retry(self):
        """Execute daily run with retry on failure."""
        max_retries = config.MAX_RETRIES
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"🏁 Starting daily run (attempt {attempt}/{max_retries})")
                self.pipeline.run_daily()
                self.last_run_date = date.today().isoformat()
                logger.success("✅ Daily run completed successfully")
                self._send_alert("success", "Daily run completed successfully")
                return
            except Exception as e:
                logger.error(f"Daily run failed (attempt {attempt}): {e}")
                logger.debug(traceback.format_exc())
                db.log_run(date.today().isoformat(), "scheduler",
                           "failure", str(e)[:500])

                if attempt < max_retries:
                    wait_time = config.RETRY_DELAY * attempt
                    logger.info(f"⏳ Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    # Recreate pipeline for fresh state
                    self.pipeline = Pipeline(dry_run=self.dry_run)
                else:
                    logger.critical("All retry attempts exhausted!")
                    self._send_alert("failure", str(e))

    def _send_alert(self, status: str, message: str):
        """Send webhook notification (optional). Configure ALERT_WEBHOOK_URL in .env"""
        import os
        webhook_url = os.getenv("ALERT_WEBHOOK_URL")
        if not webhook_url:
            return

        try:
            import requests
            emoji = "✅" if status == "success" else "🚨"
            payload = {
                "text": f"{emoji} YouTube Shorts Bot — {status.upper()}\n{message}",
                "username": "ShortsBot",
            }
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception as e:
            logger.warning(f"Alert webhook failed: {e}")

    def _health_check_thread(self):
        """Simple HTTP health check endpoint on port 8888."""
        try:
            from http.server import HTTPServer, BaseHTTPRequestHandler
            import json

            class HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self_handler):
                    status = {
                        "status": "running" if self.running else "stopping",
                        "last_run": self.last_run_date,
                        "today_uploads": db.videos_uploaded_today(),
                        "quota": config.VIDEOS_PER_DAY,
                        "scheduled_times": config.UPLOAD_TIMES,
                        "niche": config.NICHE,
                    }
                    body = json.dumps(status).encode()
                    self_handler.send_response(200)
                    self_handler.send_header("Content-Type", "application/json")
                    self_handler.end_headers()
                    self_handler.wfile.write(body)

                def log_message(self, *args):
                    pass  # suppress access logs

            server = HTTPServer(("0.0.0.0", 8888), HealthHandler)
            logger.info("🏥 Health check: http://localhost:8888")
            while self.running:
                server.handle_request()
        except Exception as e:
            logger.warning(f"Health check server failed: {e}")

    def run_forever(self):
        """Main loop — checks time every 30 seconds and runs when scheduled."""
        logger.info("🤖 YouTube Shorts Bot scheduler started")
        logger.info(f"   Niche: {config.NICHE}")
        logger.info(f"   Videos/day: {config.VIDEOS_PER_DAY}")
        logger.info(f"   Scheduled: {', '.join(config.UPLOAD_TIMES)}")
        logger.info(f"   Dry run: {self.dry_run}")

        # Start health check in background
        health_thread = threading.Thread(
            target=self._health_check_thread,
            daemon=True
        )
        health_thread.start()

        while self.running:
            try:
                if self._should_run_now():
                    logger.info(f"⏰ Scheduled time hit — starting pipeline")
                    self._run_with_retry()

                time.sleep(30)  # check every 30 seconds

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
                time.sleep(60)

        logger.info("👋 Scheduler stopped cleanly")

    def run_now(self):
        """Run immediately (for testing/manual trigger)."""
        logger.info("⚡ Manual trigger — running immediately")
        self._run_with_retry()


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run_now = "--now" in sys.argv

    scheduler = Scheduler(dry_run=dry_run)

    if run_now:
        scheduler.run_now()
    else:
        scheduler.run_forever()
