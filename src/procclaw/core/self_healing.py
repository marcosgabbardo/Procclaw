"""Self-healing module for ProcClaw.

Provides AI-powered failure analysis and auto-remediation for jobs.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from procclaw.models import (
    HealingAction,
    HealingStatus,
    JobConfig,
    JobRun,
    JobType,
    SelfHealingConfig,
    HEALING_FORBIDDEN_PATHS_ALWAYS,
)

if TYPE_CHECKING:
    from procclaw.db import Database


class ForbiddenPathError(Exception):
    """Raised when attempting to modify a forbidden path."""
    pass


class HealingContext:
    """Context for a healing session."""
    
    def __init__(
        self,
        job_id: str,
        job: JobConfig,
        run: JobRun,
        logs: str,
        stderr: str,
        history: list[dict],
        session_transcript: str | None = None,
    ):
        self.job_id = job_id
        self.job = job
        self.run = run
        self.logs = logs
        self.stderr = stderr
        self.history = history
        self.session_transcript = session_transcript
        self.attempt = 0
        self.max_attempts = job.self_healing.remediation.max_attempts


class HealingResult:
    """Result of a healing attempt."""
    
    def __init__(
        self,
        status: HealingStatus,
        root_cause: str | None = None,
        confidence: str = "low",
        category: str = "unknown",
        details: str | None = None,
        fixable: bool = False,
        actions_taken: list[dict] | None = None,
        actions_blocked: list[dict] | None = None,
        should_retry: bool = False,
        human_intervention_needed: bool = False,
        human_intervention_reason: str | None = None,
        summary: str | None = None,
    ):
        self.status = status
        self.root_cause = root_cause
        self.confidence = confidence
        self.category = category
        self.details = details
        self.fixable = fixable
        self.actions_taken = actions_taken or []
        self.actions_blocked = actions_blocked or []
        self.should_retry = should_retry
        self.human_intervention_needed = human_intervention_needed
        self.human_intervention_reason = human_intervention_reason
        self.summary = summary
    
    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "status": self.status.value,
            "analysis": {
                "root_cause": self.root_cause,
                "confidence": self.confidence,
                "category": self.category,
                "details": self.details,
            },
            "fixable": self.fixable,
            "actions_taken": self.actions_taken,
            "actions_blocked": self.actions_blocked,
            "should_retry": self.should_retry,
            "human_intervention_needed": self.human_intervention_needed,
            "human_intervention_reason": self.human_intervention_reason,
            "summary": self.summary,
        }


def is_path_forbidden(path: str, additional_forbidden: list[str] | None = None) -> bool:
    """Check if a path is forbidden for modification.
    
    Args:
        path: The path to check
        additional_forbidden: Additional patterns from job config
        
    Returns:
        True if the path is forbidden
    """
    # Expand user home
    expanded = os.path.expanduser(path)
    
    # Check hardcoded forbidden paths
    for pattern in HEALING_FORBIDDEN_PATHS_ALWAYS:
        expanded_pattern = os.path.expanduser(pattern)
        if fnmatch.fnmatch(expanded, expanded_pattern):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
        # Also check if the expanded path starts with the pattern (for directories)
        if expanded_pattern.endswith('/') and expanded.startswith(expanded_pattern.rstrip('/')):
            return True
    
    # Check additional forbidden paths from config
    if additional_forbidden:
        for pattern in additional_forbidden:
            expanded_pattern = os.path.expanduser(pattern)
            if fnmatch.fnmatch(expanded, expanded_pattern):
                return True
            if fnmatch.fnmatch(path, pattern):
                return True
    
    return False


def validate_action(
    action: HealingAction,
    allowed_actions: list[HealingAction],
    target_path: str | None = None,
    forbidden_paths: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Validate if an action is allowed.
    
    Returns:
        Tuple of (is_allowed, error_message)
    """
    # Check if action type is allowed
    if action not in allowed_actions:
        return False, f"Action '{action.value}' not in allowed_actions"
    
    # Check if target path is forbidden
    if target_path and is_path_forbidden(target_path, forbidden_paths):
        return False, f"Path '{target_path}' is forbidden"
    
    return True, None


def collect_context(
    job_id: str,
    job: JobConfig,
    run: JobRun,
    db: "Database",
    logs_dir: Path,
) -> HealingContext:
    """Collect all context needed for healing analysis.
    
    Args:
        job_id: The job identifier
        job: Job configuration
        run: The failed run
        db: Database instance
        logs_dir: Directory where logs are stored
        
    Returns:
        HealingContext with all collected data
    """
    config = job.self_healing.analysis
    
    # Collect stdout logs
    logs = ""
    if config.include_logs:
        log_lines = db.get_logs(run_id=run.id, level="stdout", limit=config.log_lines)
        logs = "\n".join(line.get("line", "") for line in log_lines)
    
    # Collect stderr logs
    stderr = ""
    if config.include_stderr:
        stderr_lines = db.get_logs(run_id=run.id, level="stderr", limit=config.log_lines)
        stderr = "\n".join(line.get("line", "") for line in stderr_lines)
    
    # Collect run history
    history = []
    if config.include_history > 0:
        recent_runs = db.get_runs(job_id=job_id, limit=config.include_history + 1)
        for r in recent_runs:
            if r.id != run.id:  # Exclude current run
                history.append({
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "exit_code": r.exit_code,
                    "duration_seconds": r.duration_seconds,
                    "error": r.error,
                })
    
    # Get session transcript for openclaw jobs
    session_transcript = None
    if job.type == JobType.OPENCLAW and run.session_transcript:
        transcript_path = Path(run.session_transcript)
        if transcript_path.exists():
            try:
                session_transcript = transcript_path.read_text()[:50000]  # Limit size
            except Exception as e:
                logger.warning(f"Could not read session transcript: {e}")
    
    return HealingContext(
        job_id=job_id,
        job=job,
        run=run,
        logs=logs,
        stderr=stderr,
        history=history,
        session_transcript=session_transcript,
    )


def build_healing_prompt(context: HealingContext) -> str:
    """Build the prompt for the healing session.
    
    Args:
        context: The healing context
        
    Returns:
        Formatted prompt string
    """
    job = context.job
    run = context.run
    config = job.self_healing
    
    # Build allowed actions list
    allowed_actions_str = "\n".join(
        f"- {action.value}" for action in config.remediation.allowed_actions
    )
    
    # Build history table
    history_rows = []
    for h in context.history:
        history_rows.append(
            f"| {h['started_at']} | {h['exit_code']} | {h['duration_seconds']:.1f}s | {h['error'] or '-'} |"
        )
    history_table = "\n".join(history_rows) if history_rows else "No recent runs"
    
    # Build job config YAML
    job_config_yaml = json.dumps(job.model_dump(exclude={"self_healing"}), indent=2, default=str)
    
    prompt = f"""🔧 **Self-Healing Request**

## Job Info
- **ID:** {context.job_id}
- **Name:** {job.name}
- **Type:** {job.type.value}
- **Exit Code:** {run.exit_code}
- **Duration:** {run.duration_seconds:.1f}s
- **Attempt:** {context.attempt + 1}/{context.max_attempts}

## Logs (last {config.analysis.log_lines} lines)
```
{context.logs}
```

## Stderr
```
{context.stderr}
```

## Run History (last {config.analysis.include_history})
| Time | Exit Code | Duration | Error |
|------|-----------|----------|-------|
{history_table}

## Job Config
```json
{job_config_yaml}
```
"""

    if context.session_transcript:
        prompt += f"""
## OpenClaw Session Transcript (truncated)
```
{context.session_transcript[:10000]}
```
"""

    prompt += f"""
---

## Allowed Actions
{allowed_actions_str}

## ⛔ FORBIDDEN - NEVER MODIFY:
- ProcClaw source code (~/.openclaw/workspace/projects/procclaw/)
- OpenClaw source code (node_modules/openclaw/)
- SSH keys (~/.ssh/)
- GPG keys (~/.gnupg/)
- System config (/etc/, /usr/, /bin/)
- OpenClaw config (~/.openclaw/openclaw.json)

**These restrictions are HARDCODED and cannot be bypassed.**

---

## Instructions

1. **Analyze** the failure - identify root cause
2. **Determine** if it's fixable with allowed actions
3. **If fixable:**
   - Apply the fix using the tools available
   - Report what you changed
   - The job will be re-run automatically to validate
4. **If NOT fixable:**
   - Explain why
   - Suggest what human intervention is needed

**NEVER:**
- Push to GitHub (commits are local only)
- Modify forbidden paths listed above
- Take actions outside the allowed list
- Delete data without explicit need

After analyzing and attempting fixes, respond with a summary of:
- What you found (root cause)
- What you did (actions taken)
- Whether to retry the job
"""
    
    return prompt


async def spawn_healer_session(
    context: HealingContext,
    prompt: str,
) -> HealingResult:
    """Spawn an OpenClaw session to analyze and fix the failure.
    
    Writes a healing request to a trigger file for the main session to pick up.
    This avoids long-running subprocess issues.
    
    Args:
        context: The healing context
        prompt: The healing prompt
        
    Returns:
        HealingResult with the outcome
    """
    job_id = context.job_id
    run_id = context.run.id
    
    logger.info(f"Spawning healer session for job '{job_id}' run {run_id}")
    
    try:
        # Write healing request to session triggers file for main session pickup
        from procclaw.openclaw import send_to_session
        
        healing_msg = f"""🔧 **Self-Healing Request for job '{job_id}'**

{prompt}

**Instructions:**
1. Analyze the logs and error above
2. Attempt to fix the issue if possible
3. Report your findings

When done, respond with one of:
- "HEALING_FIXED: <summary>" if you applied a fix
- "HEALING_MANUAL: <reason>" if human intervention is needed
- "HEALING_GAVE_UP: <reason>" if you couldn't determine the issue
"""
        
        # Send to main session (no immediate wake to avoid API overload)
        # The request will be picked up on next heartbeat
        await send_to_session("main", healing_msg, immediate=False)
        
        # For now, we return "in_progress" and let the human/agent handle it
        # In a more sophisticated implementation, we'd poll for a response
        logger.info(f"Healing request sent to main session for job '{job_id}'")
        
        return HealingResult(
            status=HealingStatus.GAVE_UP,  # Mark as gave_up since we can't wait for response
            root_cause="Healing request sent to main session",
            fixable=True,
            human_intervention_needed=True,
            human_intervention_reason="Review the healing request in main session and take action",
            summary="Healing request sent - awaiting human/agent review",
        )
        
    except Exception as e:
        logger.error(f"Healer session error: {e}")
        return HealingResult(
            status=HealingStatus.GAVE_UP,
            root_cause=f"Healer error: {str(e)}",
            summary="Healer encountered an error",
        )


class SelfHealer:
    """Self-healing manager for jobs."""
    
    # Cooldown period between healing attempts for the same job (seconds)
    HEALING_COOLDOWN_SECONDS = 300  # 5 minutes
    
    def __init__(self, db: "Database", logs_dir: Path):
        self.db = db
        self.logs_dir = logs_dir
        self._healing_in_progress: set[str] = set()
        self._last_healing_time: dict[str, datetime] = {}
        self._cancelled: set[str] = set()  # Jobs with cancelled healing
    
    def is_healing_enabled(self, job: JobConfig) -> bool:
        """Check if self-healing is enabled for a job."""
        return (
            job.self_healing.enabled and 
            job.self_healing.remediation.enabled
        )
    
    def is_healing_in_progress(self, job_id: str) -> bool:
        """Check if healing is already in progress for a job."""
        return job_id in self._healing_in_progress
    
    def is_in_cooldown(self, job_id: str) -> bool:
        """Check if job is in healing cooldown period."""
        if job_id not in self._last_healing_time:
            return False
        elapsed = (datetime.now() - self._last_healing_time[job_id]).total_seconds()
        return elapsed < self.HEALING_COOLDOWN_SECONDS
    
    def cancel_healing(self, job_id: str) -> bool:
        """Cancel healing for a job.
        
        Returns True if there was healing to cancel.
        """
        was_in_progress = job_id in self._healing_in_progress
        self._cancelled.add(job_id)
        self._healing_in_progress.discard(job_id)
        if was_in_progress:
            logger.info(f"Cancelled healing for job '{job_id}'")
        return was_in_progress
    
    def is_cancelled(self, job_id: str) -> bool:
        """Check if healing was cancelled for a job."""
        return job_id in self._cancelled
    
    def clear_cancelled(self, job_id: str) -> None:
        """Clear cancelled state for a job."""
        self._cancelled.discard(job_id)
    
    async def trigger_healing(
        self,
        job_id: str,
        job: JobConfig,
        run: JobRun,
        on_retry: callable = None,
    ) -> HealingResult:
        """Trigger self-healing for a failed job.
        
        Args:
            job_id: The job identifier
            job: Job configuration
            run: The failed run
            on_retry: Callback to re-run the job if fix is applied
            
        Returns:
            HealingResult with the outcome
        """
        if not self.is_healing_enabled(job):
            return HealingResult(
                status=HealingStatus.GAVE_UP,
                summary="Self-healing not enabled",
            )
        
        if self.is_healing_in_progress(job_id):
            logger.warning(f"Healing already in progress for job '{job_id}'")
            return HealingResult(
                status=HealingStatus.IN_PROGRESS,
                summary="Healing already in progress",
            )
        
        # Check cooldown to prevent API overload
        if self.is_in_cooldown(job_id):
            cooldown_remaining = self.HEALING_COOLDOWN_SECONDS - (datetime.now() - self._last_healing_time[job_id]).total_seconds()
            logger.info(f"Job '{job_id}' in healing cooldown ({cooldown_remaining:.0f}s remaining)")
            return HealingResult(
                status=HealingStatus.GAVE_UP,
                summary=f"In cooldown - wait {cooldown_remaining:.0f}s",
            )
        
        # Clear any previous cancelled state
        self.clear_cancelled(job_id)
        
        config = job.self_healing
        max_attempts = config.remediation.max_attempts
        
        self._healing_in_progress.add(job_id)
        
        try:
            # Store original exit code
            original_exit_code = run.exit_code
            run.original_exit_code = original_exit_code
            run.healing_status = HealingStatus.IN_PROGRESS.value
            self.db.update_run(run)
            
            # Notify if configured
            if config.notify.on_analysis:
                await self._notify(
                    config.notify.session,
                    f"🔍 Analyzing failure for job '{job_id}'..."
                )
            
            for attempt in range(max_attempts):
                logger.info(f"Healing attempt {attempt + 1}/{max_attempts} for job '{job_id}'")
                
                # Collect context
                context = collect_context(job_id, job, run, self.db, self.logs_dir)
                context.attempt = attempt
                
                # Build prompt
                prompt = build_healing_prompt(context)
                
                # Notify fix attempt if configured
                if config.notify.on_fix_attempt:
                    await self._notify(
                        config.notify.session,
                        f"🔧 Healing attempt {attempt + 1}/{max_attempts} for job '{job_id}'..."
                    )
                
                # Spawn healer session
                result = await spawn_healer_session(context, prompt)
                
                # Update run with result
                run.healing_attempts = attempt + 1
                run.healing_result = result.to_dict()
                
                if result.status == HealingStatus.FIXED and result.should_retry:
                    # Try to re-run the job
                    if on_retry:
                        logger.info(f"Retrying job '{job_id}' after healing fix")
                        retry_success = await on_retry(job_id)
                        
                        if retry_success:
                            # Job passed after fix!
                            run.healing_status = HealingStatus.FIXED.value
                            run.exit_code = 0  # Mark as successful
                            self.db.update_run(run)
                            
                            if config.notify.on_success:
                                await self._notify(
                                    config.notify.session,
                                    f"✅ Self-healing SUCCESS for job '{job_id}'\n\n"
                                    f"Root cause: {result.root_cause}\n"
                                    f"Fix: {result.summary}"
                                )
                            
                            return result
                        else:
                            # Fix didn't work, continue to next attempt
                            logger.info(f"Fix didn't work for job '{job_id}', will try again")
                            continue
                    else:
                        # No retry callback, just mark as fixed
                        run.healing_status = HealingStatus.FIXED.value
                        self.db.update_run(run)
                        return result
                
                elif result.status == HealingStatus.GAVE_UP:
                    # Healer gave up on this attempt
                    if result.human_intervention_needed:
                        # Stop trying, need human
                        break
                    # Otherwise continue to next attempt
                    continue
            
            # Exhausted all attempts
            run.healing_status = HealingStatus.GAVE_UP.value
            self.db.update_run(run)
            
            if config.notify.on_give_up:
                await self._notify(
                    config.notify.session,
                    f"❌ Self-healing GAVE UP for job '{job_id}' after {max_attempts} attempts\n\n"
                    f"Last analysis: {result.root_cause}\n"
                    f"Human intervention may be needed."
                )
            
            return result
            
        finally:
            self._healing_in_progress.discard(job_id)
            self._last_healing_time[job_id] = datetime.now()  # Record for cooldown
    
    async def _notify(self, session: str, message: str) -> None:
        """Send notification to OpenClaw session."""
        from procclaw.openclaw import send_to_session
        try:
            await send_to_session(session, message)
        except Exception as e:
            logger.warning(f"Failed to send healing notification: {e}")
