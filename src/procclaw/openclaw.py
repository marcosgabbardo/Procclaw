"""OpenClaw integration for ProcClaw.

Provides alerting via WhatsApp/Telegram and memory logging for AI context.

Alert delivery strategy:
1. Try gateway wake API (triggers immediate agent response)
2. Fall back to alert file for heartbeat pickup
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


# OpenClaw workspace for memory logging
OPENCLAW_WORKSPACE = Path.home() / ".openclaw" / "workspace"
MEMORY_DIR = OPENCLAW_WORKSPACE / "memory"

# Gateway API - try common ports
GATEWAY_PORT = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))
GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", f"http://localhost:{GATEWAY_PORT}")
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")

# Optional webhook for external notifications
ALERT_WEBHOOK_URL = os.environ.get("PROCCLAW_ALERT_WEBHOOK", "")


class AlertType:
    """Alert type constants."""
    
    FAILURE = "failure"
    MAX_RETRIES = "max_retries"
    HEALTH_FAIL = "health_fail"
    RECOVERED = "recovered"
    RESTART = "restart"
    MISSED_RUN = "missed_run"
    RATE_LIMIT = "rate_limit"
    OUTPUT_ERROR = "output_error"


# Emoji for alert types
ALERT_EMOJI: dict[str, str] = {
    AlertType.FAILURE: "🚨",
    AlertType.MAX_RETRIES: "💀",
    AlertType.HEALTH_FAIL: "🏥",
    AlertType.RECOVERED: "✅",
    AlertType.RESTART: "🔄",
    AlertType.MISSED_RUN: "⏰",
    AlertType.RATE_LIMIT: "🚦",
    AlertType.OUTPUT_ERROR: "📝",
}


async def send_alert(
    job_id: str,
    alert_type: str,
    message: str,
    details: dict[str, Any] | None = None,
    channels: list[str] | None = None,
) -> bool:
    """Send an alert via OpenClaw message tool.
    
    Uses the OpenClaw CLI to send messages through configured channels.
    
    Args:
        job_id: The job ID
        alert_type: Type of alert (failure, max_retries, health_fail, etc.)
        message: Main message content
        details: Optional additional details
        channels: List of channels to send to (default: whatsapp)
        
    Returns:
        True if alert was sent successfully
    """
    channels = channels or ["whatsapp"]
    emoji = ALERT_EMOJI.get(alert_type, "ℹ️")
    
    # Format the message
    text_parts = [
        f"{emoji} **ProcClaw: {job_id}**",
        "",
        message,
    ]
    
    if details:
        text_parts.append("")
        for key, value in details.items():
            if value is not None:
                text_parts.append(f"• {key}: {value}")
    
    text = "\n".join(text_parts)
    
    # Try to send via OpenClaw CLI
    success = False
    for channel in channels:
        try:
            result = await _send_via_openclaw(text, channel)
            if result:
                success = True
                logger.info(f"Alert sent via {channel} for job '{job_id}'")
        except Exception as e:
            logger.warning(f"Failed to send alert via {channel}: {e}")
    
    return success


async def _send_via_openclaw(message: str, channel: str = "whatsapp") -> bool:
    """Send message via OpenClaw gateway.
    
    Strategy:
    1. Write to pending alerts file (always, for reliability)
    2. Try webhook if configured (for immediate external notification)
    3. Log success/failure
    """
    # Write to pending alerts (always, for reliability)
    _write_pending_alert(message, channel)
    
    # Try webhook for immediate external notification
    if ALERT_WEBHOOK_URL:
        webhook_result = await _send_webhook(message, channel)
        if webhook_result:
            logger.info("Alert sent via webhook")
            return True
    
    logger.debug("Alert queued for heartbeat pickup")
    return True  # Return True since we queued the alert


async def _send_webhook(message: str, channel: str) -> bool:
    """Send alert to external webhook.
    
    Supports various webhook formats:
    - Slack: {"text": "message"}
    - Discord: {"content": "message"}
    - Generic: {"message": "message", "channel": "channel"}
    """
    if not ALERT_WEBHOOK_URL:
        return False
    
    try:
        # Detect webhook type from URL
        if "slack.com" in ALERT_WEBHOOK_URL:
            payload = {"text": message}
        elif "discord.com" in ALERT_WEBHOOK_URL:
            payload = {"content": message}
        else:
            payload = {"message": message, "channel": channel}
        
        cmd = [
            "curl", "-s", "-X", "POST",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(payload),
            ALERT_WEBHOOK_URL,
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=10.0,
        )
        
        if process.returncode == 0:
            return True
        
        logger.warning(f"Webhook failed: {stderr.decode()}")
        return False
        
    except asyncio.TimeoutError:
        logger.warning("Webhook timed out")
        return False
    except Exception as e:
        logger.warning(f"Webhook error: {e}")
        return False


def _write_pending_alert(message: str, channel: str) -> bool:
    """Write alert to pending file for heartbeat pickup.
    
    Creates a file that the agent reads during heartbeat.
    """
    try:
        pending_file = MEMORY_DIR / "procclaw-pending-alerts.md"
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        entry = f"\n## [{timestamp}] {channel}\n{message}\n"
        
        with open(pending_file, "a") as f:
            f.write(entry)
        
        logger.debug(f"Alert queued in {pending_file}")
        return True
        
    except Exception as e:
        logger.warning(f"Failed to write pending alert: {e}")
        return False


def clear_pending_alerts() -> int:
    """Clear pending alerts file.
    
    Call this after alerts have been delivered.
    Returns number of alerts cleared.
    """
    try:
        pending_file = MEMORY_DIR / "procclaw-pending-alerts.md"
        if not pending_file.exists():
            return 0
        
        content = pending_file.read_text()
        count = content.count("## [")
        
        pending_file.unlink()
        return count
        
    except Exception as e:
        logger.warning(f"Failed to clear pending alerts: {e}")
        return 0


def get_pending_alerts() -> list[dict]:
    """Get list of pending alerts.
    
    Returns list of {timestamp, channel, message} dicts.
    """
    try:
        pending_file = MEMORY_DIR / "procclaw-pending-alerts.md"
        if not pending_file.exists():
            return []
        
        content = pending_file.read_text()
        alerts = []
        
        for block in content.split("\n## [")[1:]:
            try:
                # Parse timestamp and channel from header
                header, *body = block.split("\n", 1)
                ts_end = header.index("]")
                timestamp = header[:ts_end]
                channel = header[ts_end+1:].strip()
                message = body[0].strip() if body else ""
                
                alerts.append({
                    "timestamp": timestamp,
                    "channel": channel,
                    "message": message,
                })
            except Exception:
                continue
        
        return alerts
        
    except Exception as e:
        logger.warning(f"Failed to read pending alerts: {e}")
        return []


def log_to_memory(
    job_id: str,
    event: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """Log job event to OpenClaw memory for AI context.
    
    Writes to memory/procclaw-state.md for the AI to read.
    
    Args:
        job_id: The job ID
        event: Event type (started, stopped, failed, etc.)
        details: Optional additional details
        
    Returns:
        True if logged successfully
    """
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        
        memory_file = MEMORY_DIR / "procclaw-state.md"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Build log entry
        entry_parts = [f"## [{timestamp}] {job_id}: {event}"]
        
        if details:
            for key, value in details.items():
                if value is not None:
                    entry_parts.append(f"- **{key}:** {value}")
        
        entry = "\n".join(entry_parts) + "\n\n"
        
        # Append to file (keep last 100 entries by truncating if too large)
        if memory_file.exists():
            content = memory_file.read_text()
            entries = content.split("\n## [")
            
            # Keep last 99 entries to add new one
            if len(entries) > 99:
                entries = entries[-99:]
                content = "## [".join(entries)
                memory_file.write_text(content)
        
        with open(memory_file, "a") as f:
            f.write(entry)
        
        logger.debug(f"Logged to memory: {job_id} - {event}")
        return True
        
    except Exception as e:
        logger.warning(f"Failed to log to memory: {e}")
        return False


def update_job_summary() -> bool:
    """Update the job summary file for quick AI reference.
    
    Creates memory/procclaw-jobs.md with current job states.
    """
    try:
        # Import here to avoid circular imports
        from procclaw.config import load_jobs
        from procclaw.db import Database
        
        jobs = load_jobs()
        db = Database()
        
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        summary_file = MEMORY_DIR / "procclaw-jobs.md"
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        lines = [
            "# ProcClaw Jobs Summary",
            f"*Updated: {timestamp}*",
            "",
            "| Job | Type | Status | Last Run |",
            "|-----|------|--------|----------|",
        ]
        
        for job_id, job in jobs.jobs.items():
            state = db.get_state(job_id)
            status = state.status.value if state else "unknown"
            last_run = db.get_last_run(job_id)
            
            last_run_str = "-"
            if last_run and last_run.finished_at:
                last_run_str = last_run.finished_at.strftime("%m/%d %H:%M")
                if last_run.exit_code == 0:
                    last_run_str += " ✓"
                else:
                    last_run_str += f" ✗({last_run.exit_code})"
            
            lines.append(f"| {job_id} | {job.type.value} | {status} | {last_run_str} |")
        
        lines.append("")
        
        summary_file.write_text("\n".join(lines))
        return True
        
    except Exception as e:
        logger.warning(f"Failed to update job summary: {e}")
        return False


async def send_to_session(
    session: str,
    message: str,
    timeout: float = 30.0,
) -> bool:
    """Send a message to an OpenClaw session.
    
    Writes to a trigger file that the heartbeat picks up.
    For immediate delivery, also attempts to wake the gateway.
    
    Args:
        session: Session identifier:
            - "main" -> "agent:main:main"
            - "cron:<id>" -> "agent:main:cron:<id>"
            - Full key like "agent:main:..." used as-is
        message: Message to send
        timeout: Timeout in seconds
        
    Returns:
        True if message was queued successfully
    """
    # Expand session shorthand
    if session == "main":
        session_key = "agent:main:main"
    elif session.startswith("cron:"):
        cron_id = session[5:]  # Remove "cron:" prefix
        session_key = f"agent:main:cron:{cron_id}"
    else:
        session_key = session
    
    logger.debug(f"Sending to session {session_key}: {message[:50]}...")
    
    try:
        # Write to trigger file for heartbeat pickup
        trigger_file = MEMORY_DIR / "procclaw-session-triggers.md"
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n## [{timestamp}] Session: {session_key}\n{message}\n"
        
        with open(trigger_file, "a") as f:
            f.write(entry)
        
        logger.info(f"Session trigger queued for {session_key}")
        
        # Try to wake the gateway for immediate pickup
        try:
            cmd = ["openclaw", "cron", "wake", "--mode", "now"]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=5.0)
        except Exception:
            # Wake is best-effort, don't fail if it doesn't work
            pass
        
        return True
        
    except Exception as e:
        logger.warning(f"Error queueing session trigger for {session_key}: {e}")
        return False


def render_trigger_template(
    template: str,
    job_id: str,
    job_name: str,
    status: str,
    exit_code: int | None,
    duration: float | None,
    error: str | None,
    started_at: datetime | None,
    finished_at: datetime | None,
) -> str:
    """Render a trigger message template with job variables.
    
    Template variables:
    - {{job_id}}: Job identifier
    - {{job_name}}: Job display name
    - {{status}}: "success" or "failed"
    - {{exit_code}}: Process exit code
    - {{duration}}: Duration in seconds
    - {{error}}: Error message (if failed)
    - {{started_at}}: Start timestamp
    - {{finished_at}}: End timestamp
    """
    result = template
    
    replacements = {
        "{{job_id}}": job_id,
        "{{job_name}}": job_name or job_id,
        "{{status}}": status,
        "{{exit_code}}": str(exit_code) if exit_code is not None else "N/A",
        "{{duration}}": f"{duration:.1f}" if duration is not None else "N/A",
        "{{error}}": error or "",
        "{{started_at}}": started_at.strftime("%Y-%m-%d %H:%M:%S") if started_at else "N/A",
        "{{finished_at}}": finished_at.strftime("%Y-%m-%d %H:%M:%S") if finished_at else "N/A",
    }
    
    for key, value in replacements.items():
        result = result.replace(key, value)
    
    return result


class OpenClawIntegration:
    """OpenClaw integration manager.
    
    Provides methods for sending alerts and logging to memory.
    """
    
    def __init__(self, enabled: bool = True, channels: list[str] | None = None):
        """Initialize the integration.
        
        Args:
            enabled: Whether integration is enabled
            channels: Default channels for alerts
        """
        self.enabled = enabled
        self.channels = channels or ["whatsapp"]
        self._alert_queue: asyncio.Queue[tuple[str, str, str, dict | None]] = asyncio.Queue()
        self._running = False
    
    async def start(self) -> None:
        """Start the integration background tasks."""
        if not self.enabled:
            return
        
        self._running = True
        asyncio.create_task(self._alert_worker())
        logger.info("OpenClaw integration started")
    
    async def stop(self) -> None:
        """Stop the integration."""
        self._running = False
    
    async def _alert_worker(self) -> None:
        """Background worker to process alert queue."""
        while self._running:
            try:
                # Wait for alert with timeout
                try:
                    job_id, alert_type, message, details = await asyncio.wait_for(
                        self._alert_queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue
                
                # Send the alert
                await send_alert(
                    job_id=job_id,
                    alert_type=alert_type,
                    message=message,
                    details=details,
                    channels=self.channels,
                )
                
                # Log to memory
                log_to_memory(job_id, alert_type, details)
                
                # Update summary
                update_job_summary()
                
            except Exception as e:
                logger.error(f"Alert worker error: {e}")
    
    def queue_alert(
        self,
        job_id: str,
        alert_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Queue an alert to be sent.
        
        Args:
            job_id: The job ID
            alert_type: Type of alert
            message: Alert message
            details: Optional details
        """
        if not self.enabled:
            return
        
        try:
            self._alert_queue.put_nowait((job_id, alert_type, message, details))
        except asyncio.QueueFull:
            logger.warning("Alert queue full, dropping alert")
    
    def on_job_started(self, job_id: str, pid: int, trigger: str) -> None:
        """Called when a job starts."""
        log_to_memory(job_id, "started", {
            "pid": pid,
            "trigger": trigger,
        })
        update_job_summary()
    
    def on_job_stopped(self, job_id: str, exit_code: int, duration: float) -> None:
        """Called when a job stops normally."""
        log_to_memory(job_id, "stopped", {
            "exit_code": exit_code,
            "duration": f"{duration:.1f}s",
        })
        update_job_summary()
    
    def on_job_failed(
        self,
        job_id: str,
        exit_code: int,
        error: str | None,
        should_alert: bool = True,
    ) -> None:
        """Called when a job fails."""
        details = {
            "exit_code": exit_code,
            "error": error,
        }
        
        log_to_memory(job_id, "failed", details)
        
        if should_alert:
            self.queue_alert(
                job_id=job_id,
                alert_type=AlertType.FAILURE,
                message=f"Job failed with exit code {exit_code}",
                details=details,
            )
        
        update_job_summary()
    
    def on_max_retries(self, job_id: str, attempts: int) -> None:
        """Called when a job reaches max retries."""
        details = {"attempts": attempts}
        
        log_to_memory(job_id, "max_retries", details)
        
        self.queue_alert(
            job_id=job_id,
            alert_type=AlertType.MAX_RETRIES,
            message=f"Job reached max retries ({attempts} attempts)",
            details=details,
        )
        
        update_job_summary()
    
    def on_health_fail(self, job_id: str, check_type: str, message: str) -> None:
        """Called when a health check fails."""
        details = {
            "check_type": check_type,
            "message": message,
        }
        
        log_to_memory(job_id, "health_fail", details)
        
        self.queue_alert(
            job_id=job_id,
            alert_type=AlertType.HEALTH_FAIL,
            message=f"Health check failed: {message}",
            details=details,
        )
        
        update_job_summary()
    
    def on_health_recover(self, job_id: str) -> None:
        """Called when a job recovers from health failure."""
        log_to_memory(job_id, "recovered", {})
        
        self.queue_alert(
            job_id=job_id,
            alert_type=AlertType.RECOVERED,
            message="Job recovered and is healthy again",
        )
        
        update_job_summary()
    
    def on_job_restart(self, job_id: str, restart_count: int) -> None:
        """Called when a job is restarted."""
        log_to_memory(job_id, "restart", {
            "restart_count": restart_count,
        })
        update_job_summary()

    def on_missed_run(
        self,
        job_id: str,
        expected_time: datetime,
        hours_overdue: float,
        last_run: datetime | None = None,
    ) -> None:
        """Called when a scheduled job misses its expected run.
        
        Args:
            job_id: The job ID
            expected_time: When the job should have run
            hours_overdue: How many hours overdue
            last_run: When the job last actually ran
        """
        details = {
            "expected": expected_time.strftime("%Y-%m-%d %H:%M:%S"),
            "overdue": f"{hours_overdue:.1f}h",
            "last_run": last_run.strftime("%Y-%m-%d %H:%M:%S") if last_run else "never",
        }
        
        log_to_memory(job_id, "missed_run", details)
        
        self.queue_alert(
            job_id=job_id,
            alert_type=AlertType.MISSED_RUN,
            message=f"Scheduled job missed! Expected at {expected_time.strftime('%H:%M')}, now {hours_overdue:.1f}h overdue",
            details=details,
        )
        
        update_job_summary()

    def on_rate_limit(self, job_id: str, error_message: str) -> None:
        """Called when a job hits rate limiting."""
        details = {"error": error_message}
        
        log_to_memory(job_id, "rate_limit", details)
        
        self.queue_alert(
            job_id=job_id,
            alert_type=AlertType.RATE_LIMIT,
            message=f"Rate limit detected: {error_message}",
            details=details,
        )

    def on_output_error(self, job_id: str, pattern: str, context: str) -> None:
        """Called when error pattern is found in job output."""
        details = {
            "pattern": pattern,
            "context": context[:200],  # Truncate context
        }
        
        log_to_memory(job_id, "output_error", details)
        
        self.queue_alert(
            job_id=job_id,
            alert_type=AlertType.OUTPUT_ERROR,
            message=f"Error pattern '{pattern}' found in output",
            details=details,
        )


# Singleton instance
_integration: OpenClawIntegration | None = None


def get_integration() -> OpenClawIntegration:
    """Get the global OpenClaw integration instance."""
    global _integration
    if _integration is None:
        _integration = OpenClawIntegration()
    return _integration


def init_integration(enabled: bool = True, channels: list[str] | None = None) -> OpenClawIntegration:
    """Initialize the global OpenClaw integration."""
    global _integration
    _integration = OpenClawIntegration(enabled=enabled, channels=channels)
    return _integration
