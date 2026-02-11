"""Self-Healing v2 Engine - Proactive behavior analysis.

This module provides proactive job analysis and optimization suggestions.
Unlike the reactive self_healing.py (triggered on failures), this engine
runs periodically to analyze job behavior and suggest improvements.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from procclaw.models import (
    HealingMode,
    ReviewFrequency,
    ReviewStatus,
    SuggestionCategory,
    SuggestionSeverity,
    SuggestionStatus,
    ActionStatus,
    JobConfig,
    JobRun,
    SelfHealingConfig,
)

if TYPE_CHECKING:
    from procclaw.db import Database
    from procclaw.core.supervisor import Supervisor


@dataclass
class AnalysisContext:
    """Context data collected for analysis."""
    job_id: str
    job_config: dict
    recent_runs: list[dict]
    log_samples: dict[str, str]  # run_id -> logs
    ai_sessions: list[dict]
    sla_metrics: dict | None
    sla_violations: list[dict]
    prompt_content: str | None
    script_content: str | None


@dataclass
class SuggestionData:
    """Data for a generated suggestion."""
    category: str
    severity: str
    title: str
    description: str
    current_state: str | None = None
    suggested_change: str | None = None
    expected_impact: str | None = None
    affected_files: list[str] | None = None
    auto_apply: bool = False


class SkipReviewError(Exception):
    """Raised when a review should be skipped (e.g., not enough runs)."""
    pass
    expected_impact: str | None = None
    affected_files: list[str] | None = None
    auto_apply: bool = False


class HealingEngine:
    """Proactive healing engine for job behavior analysis.
    
    This engine:
    1. Collects comprehensive data about job behavior
    2. Sends data to AI for analysis
    3. Generates improvement suggestions
    4. Optionally auto-applies low-risk changes
    
    Queue behavior:
    - Only one healing review runs at a time (semaphore)
    - Healing waits for OpenClaw jobs to finish before running
    - This prevents token competition with real jobs
    """
    
    # How long to wait between checks for openclaw jobs (seconds)
    OPENCLAW_CHECK_INTERVAL = 5
    # Maximum time to wait for openclaw jobs before giving up (seconds)
    OPENCLAW_MAX_WAIT = 300  # 5 minutes
    
    def __init__(self, db: "Database", supervisor: "Supervisor"):
        self.db = db
        self.supervisor = supervisor
        self._running_reviews: dict[str, int] = {}  # job_id -> review_id
        self._semaphore = asyncio.Semaphore(1)  # Only 1 healing at a time
    
    def _get_running_openclaw_jobs(self) -> list[str]:
        """Get list of currently running OpenClaw jobs.
        
        Returns:
            List of job IDs that are type=openclaw and currently running
        """
        running = []
        
        # Get all jobs - handle both real supervisor and mocks
        jobs_dict = {}
        if hasattr(self.supervisor, 'jobs'):
            if hasattr(self.supervisor.jobs, 'jobs'):
                # Real supervisor: self.supervisor.jobs.jobs
                jobs_dict = self.supervisor.jobs.jobs
            elif hasattr(self.supervisor.jobs, 'items'):
                # Dict-like mock
                jobs_dict = dict(self.supervisor.jobs.items())
            elif isinstance(self.supervisor.jobs, dict):
                jobs_dict = self.supervisor.jobs
        
        # Check each openclaw job
        for job_id, job_config in jobs_dict.items():
            job_type = getattr(job_config.type, 'value', str(job_config.type))
            if job_type == "openclaw":
                # Check if this job has a running process
                if hasattr(self.supervisor, '_processes'):
                    if job_id in self.supervisor._processes:
                        running.append(job_id)
        
        return running
    
    async def _wait_for_openclaw_slot(self) -> bool:
        """Wait until no OpenClaw jobs are running.
        
        Returns:
            True if slot became available, False if timed out
        """
        start_time = asyncio.get_event_loop().time()
        
        while True:
            running = self._get_running_openclaw_jobs()
            
            if not running:
                return True
            
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= self.OPENCLAW_MAX_WAIT:
                logger.warning(
                    f"Timed out waiting for OpenClaw jobs: {running} "
                    f"(waited {elapsed:.0f}s)"
                )
                return False
            
            logger.debug(
                f"Waiting for OpenClaw jobs to finish: {running} "
                f"(waited {elapsed:.0f}s)"
            )
            await asyncio.sleep(self.OPENCLAW_CHECK_INTERVAL)
    
    async def run_review(
        self,
        job_id: str,
        job_config: JobConfig | None = None,
        trigger: str = "scheduled",
        failed_run_id: int | None = None,
    ) -> int:
        """Run a healing review for a job.
        
        This method:
        1. Acquires semaphore (only 1 healing at a time)
        2. Waits for OpenClaw jobs to finish (lower priority than real jobs)
        3. Runs the actual review
        
        Args:
            job_id: Job to analyze
            job_config: Optional job config (fetched if not provided)
            trigger: What triggered this review ('scheduled', 'failure', 'sla_breach', 'manual')
            failed_run_id: Specific run ID when triggered by failure (reactive mode)
            
        Returns:
            Review ID
        """
        # Acquire semaphore - only 1 healing at a time
        async with self._semaphore:
            logger.debug(f"Acquired healing semaphore for {job_id}")
            
            # Wait for OpenClaw jobs to finish
            if not await self._wait_for_openclaw_slot():
                logger.warning(f"Skipping healing for {job_id}: OpenClaw jobs still running")
                # Create a skipped review record
                review_id = self.db.create_healing_review(job_id)
                self.db.update_healing_review(
                    review_id,
                    status="skipped",
                    finished_at=datetime.now(),
                    error_message="Timed out waiting for OpenClaw jobs",
                )
                return review_id
            
            return await self._run_review_internal(job_id, job_config, trigger, failed_run_id)
    
    async def _run_review_internal(
        self,
        job_id: str,
        job_config: JobConfig | None = None,
        trigger: str = "scheduled",
        failed_run_id: int | None = None,
    ) -> int:
        """Internal review logic (called after acquiring semaphore).
        
        Args:
            job_id: Job to analyze
            job_config: Optional job config (fetched if not provided)
            trigger: What triggered this review
            failed_run_id: Specific run ID when triggered by failure
            
        Returns:
            Review ID
        """
        # Check if review is already running
        if job_id in self._running_reviews:
            logger.warning(f"Review already running for {job_id}")
            return self._running_reviews[job_id]
        
        # Create review record
        review_id = self.db.create_healing_review(job_id)
        self._running_reviews[job_id] = review_id
        
        start_time = datetime.now()
        
        try:
            # Get job config
            if job_config is None:
                job_config = self.supervisor.jobs.get_job(job_id)
            
            if job_config is None:
                raise ValueError(f"Job '{job_id}' not found")
            
            healing_config = job_config.self_healing
            scope = healing_config.review_scope
            
            # Collect context
            context = await self._collect_context(
                job_id, job_config, scope, trigger=trigger, failed_run_id=failed_run_id
            )
            
            # Run AI analysis
            suggestions = await self._analyze_with_ai(context, healing_config)
            
            # Store suggestions
            auto_applied = 0
            for suggestion in suggestions:
                suggestion_id = self.db.create_healing_suggestion(
                    review_id=review_id,
                    job_id=job_id,
                    category=suggestion.category,
                    severity=suggestion.severity,
                    title=suggestion.title,
                    description=suggestion.description,
                    current_state=suggestion.current_state,
                    suggested_change=suggestion.suggested_change,
                    expected_impact=suggestion.expected_impact,
                    affected_files=suggestion.affected_files,
                )
                
                # Auto-apply if configured
                if suggestion.auto_apply and healing_config.suggestions.auto_apply:
                    applied = await self._apply_suggestion(suggestion_id, suggestion)
                    if applied:
                        auto_applied += 1
            
            # Update review
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            self.db.update_healing_review(
                review_id,
                status="completed",
                finished_at=datetime.now(),
                runs_analyzed=len(context.recent_runs),
                logs_lines=sum(len(log.split('\n')) for log in context.log_samples.values()),
                ai_sessions_count=len(context.ai_sessions),
                sla_violations_count=len(context.sla_violations),
                suggestions_count=len(suggestions),
                auto_applied_count=auto_applied,
                analysis_duration_ms=duration_ms,
            )
            
            logger.info(
                f"Review completed for {job_id}: "
                f"{len(suggestions)} suggestions, {auto_applied} auto-applied"
            )
            
            # Send notification if configured and has pending suggestions
            pending_count = len(suggestions) - auto_applied
            if pending_count > 0 and healing_config.suggestions.notify_on_suggestion:
                await self._send_notification(
                    job_id=job_id,
                    review_id=review_id,
                    suggestions=suggestions,
                    pending_count=pending_count,
                    channel=healing_config.suggestions.notify_channel,
                )
            
            return review_id
        
        except SkipReviewError as e:
            # Review skipped (e.g., not enough runs) - not a failure
            logger.info(f"Review skipped for {job_id}: {e}")
            self.db.update_healing_review(
                review_id,
                status="skipped",
                finished_at=datetime.now(),
                error_message=str(e),
            )
            return review_id
            
        except Exception as e:
            logger.error(f"Review failed for {job_id}: {e}")
            self.db.update_healing_review(
                review_id,
                status="failed",
                finished_at=datetime.now(),
                error_message=str(e),
            )
            raise
        finally:
            self._running_reviews.pop(job_id, None)
    
    async def _collect_context(
        self,
        job_id: str,
        job_config: JobConfig,
        scope: Any,
        trigger: str = "scheduled",
        failed_run_id: int | None = None,
    ) -> AnalysisContext:
        """Collect all context data for analysis.
        
        Args:
            job_id: Job to analyze
            job_config: Job configuration
            scope: What to analyze (logs, runs, ai_sessions, sla)
            trigger: What triggered this review ('scheduled', 'failure', 'sla_breach', 'manual')
            failed_run_id: Specific run ID when triggered by failure (reactive mode)
        """
        healing_config = job_config.self_healing
        
        # Get runs based on mode
        recent_runs = []
        if scope.analyze_runs:
            if healing_config.mode == HealingMode.REACTIVE:
                # REACTIVE: Only the failed run
                if failed_run_id:
                    run = self.db.get_run(failed_run_id)
                    runs = [run] if run else []
                else:
                    # Fallback: last failed run
                    runs = self.db.get_runs(job_id=job_id, status="failed", limit=1)
                logger.debug(f"Reactive mode: analyzing {len(runs)} failed run(s)")
            else:
                # PROACTIVE: Runs since last completed review
                last_review = self.db.get_last_completed_review(job_id)
                
                if last_review and last_review.get("finished_at"):
                    # Parse the finished_at timestamp
                    finished_at = last_review["finished_at"]
                    if isinstance(finished_at, str):
                        since = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                    else:
                        since = finished_at
                    logger.debug(f"Proactive mode: analyzing runs since {since}")
                else:
                    # No previous review: last 7 days
                    since = datetime.now() - timedelta(days=7)
                    logger.debug(f"Proactive mode (no prior review): analyzing runs since {since}")
                
                runs = self.db.get_runs(job_id=job_id, since=since, limit=100)
                
                # Check min_runs threshold
                min_runs = healing_config.review_schedule.min_runs
                if len(runs) < min_runs:
                    logger.info(
                        f"Skipping review for {job_id}: only {len(runs)} runs "
                        f"since last review, need {min_runs}"
                    )
                    raise SkipReviewError(
                        f"Insufficient runs: {len(runs)} < {min_runs} required"
                    )
            
            recent_runs = [
                {
                    "id": r.id,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "exit_code": r.exit_code,
                    "duration_seconds": r.duration_seconds,
                    "trigger": r.trigger,
                    "error": r.error,
                    "sla_status": r.sla_status,
                }
                for r in runs
            ]
        
        # Get log samples
        log_samples = {}
        if scope.analyze_logs and recent_runs:
            # Get logs from last 3 runs
            for run in recent_runs[:3]:
                logs = self.db.get_logs(run_id=run["id"], limit=100)
                if logs:
                    log_samples[str(run["id"])] = "\n".join(
                        log.get("line", "") for log in logs
                    )
        
        # Get AI sessions
        ai_sessions = []
        if scope.analyze_ai_sessions and job_config.type.value == "openclaw":
            for run in recent_runs[:5]:
                run_obj = self.db.get_run(run["id"]) if hasattr(self.db, 'get_run') else None
                if run_obj and run_obj.session_messages:
                    try:
                        messages = json.loads(run_obj.session_messages)
                        ai_sessions.append({
                            "run_id": run["id"],
                            "messages": messages[:20],  # Limit messages
                        })
                    except json.JSONDecodeError:
                        pass
        
        # Get SLA metrics
        sla_metrics = None
        sla_violations = []
        if scope.analyze_sla:
            # Get last 7 days of runs with SLA data
            sla_runs = [r for r in recent_runs if r.get("sla_status")]
            sla_violations = [r for r in sla_runs if r.get("sla_status") == "fail"]
        
        # Get prompt content
        prompt_content = None
        if scope.analyze_prompt and job_config.type.value == "openclaw":
            prompt_path = self._get_prompt_path(job_config)
            if prompt_path and prompt_path.exists():
                try:
                    prompt_content = prompt_path.read_text()[:5000]  # Limit size
                except Exception as e:
                    logger.warning(f"Failed to read prompt: {e}")
        
        # Get script content
        script_content = None
        if scope.analyze_script:
            script_path = self._get_script_path(job_config)
            if script_path and script_path.exists():
                try:
                    script_content = script_path.read_text()[:5000]  # Limit size
                except Exception as e:
                    logger.warning(f"Failed to read script: {e}")
        
        return AnalysisContext(
            job_id=job_id,
            job_config=job_config.model_dump(),
            recent_runs=recent_runs,
            log_samples=log_samples,
            ai_sessions=ai_sessions,
            sla_metrics=sla_metrics,
            sla_violations=sla_violations,
            prompt_content=prompt_content,
            script_content=script_content,
        )
    
    def _get_prompt_path(self, job_config: JobConfig) -> Path | None:
        """Extract prompt path from job config."""
        cmd = job_config.cmd
        if not cmd:
            return None
        
        # Look for common prompt file patterns
        import re
        patterns = [
            r'--prompt\s+["\']?([^"\'>\s]+)["\']?',
            r'-p\s+["\']?([^"\'>\s]+)["\']?',
            r'prompts?/([^\s"\']+\.md)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, cmd)
            if match:
                path = Path(match.group(1)).expanduser()
                if path.exists():
                    return path
        
        return None
    
    def _get_script_path(self, job_config: JobConfig) -> Path | None:
        """Extract script path from job config."""
        cmd = job_config.cmd
        if not cmd:
            return None
        
        # Look for script files in command
        import re
        patterns = [
            r'python3?\s+["\']?([^"\'>\s]+\.py)["\']?',
            r'bash\s+["\']?([^"\'>\s]+\.sh)["\']?',
            r'sh\s+["\']?([^"\'>\s]+\.sh)["\']?',
            r'node\s+["\']?([^"\'>\s]+\.js)["\']?',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, cmd)
            if match:
                path = Path(match.group(1)).expanduser()
                if path.exists():
                    return path
        
        return None
    
    async def _analyze_with_ai(
        self,
        context: AnalysisContext,
        config: SelfHealingConfig,
    ) -> list[SuggestionData]:
        """Send context to AI for analysis and get suggestions."""
        
        # Build analysis prompt
        prompt = self._build_analysis_prompt(context)
        
        # Call OpenClaw for analysis
        try:
            result = await self._call_openclaw_analysis(prompt)
            suggestions = self._parse_ai_response(result, config)
            return suggestions
        except Exception as e:
            logger.error(f"AI analysis failed: {e}")
            return []
    
    def _build_analysis_prompt(self, context: AnalysisContext) -> str:
        """Build the analysis prompt for AI."""
        
        prompt = f"""Analyze this ProcClaw job and suggest improvements.

## Job: {context.job_id}

### Configuration
```json
{json.dumps(context.job_config, indent=2, default=str)[:2000]}
```

### Recent Runs ({len(context.recent_runs)} total)
"""
        
        for run in context.recent_runs[:5]:
            status = "✓" if run.get("exit_code") == 0 else "✗"
            prompt += f"- {status} Run {run['id']}: exit={run.get('exit_code')}, "
            prompt += f"duration={run.get('duration_seconds', 0):.1f}s, "
            prompt += f"trigger={run.get('trigger')}\n"
        
        if context.log_samples:
            prompt += "\n### Log Samples\n"
            for run_id, logs in list(context.log_samples.items())[:2]:
                prompt += f"\n#### Run {run_id}\n```\n{logs[:1000]}\n```\n"
        
        if context.sla_violations:
            prompt += f"\n### SLA Violations: {len(context.sla_violations)} in recent runs\n"
        
        if context.prompt_content:
            prompt += f"\n### Prompt Content\n```markdown\n{context.prompt_content[:2000]}\n```\n"
        
        if context.script_content:
            prompt += f"\n### Script Content\n```\n{context.script_content[:2000]}\n```\n"
        
        prompt += """
## Instructions

Analyze the job and output suggestions in JSON format:

```json
{
  "suggestions": [
    {
      "category": "performance|cost|reliability|security|config|prompt|script",
      "severity": "low|medium|high|critical",
      "title": "Short title",
      "description": "Detailed description of the issue",
      "current_state": "What's currently happening",
      "suggested_change": "What should be changed",
      "expected_impact": "Expected improvement",
      "affected_files": ["list of files to modify"],
      "auto_apply": false
    }
  ]
}
```

Focus on:
- Performance issues (slow runs, high resource usage)
- Reliability problems (frequent failures, flaky behavior)
- Cost optimization (reduce API calls, optimize prompts)
- Security concerns (exposed credentials, unsafe practices)
- Configuration improvements (better timeouts, retries)
- Prompt improvements (clearer instructions, better examples)
- Script bugs or improvements

Only suggest changes with clear evidence from the data.
Set auto_apply=true only for trivial, low-risk changes.
"""
        
        return prompt
    
    async def _call_openclaw_analysis(self, prompt: str) -> str:
        """Call OpenClaw CLI for analysis."""
        
        try:
            # Use openclaw one-shot for analysis
            proc = await asyncio.create_subprocess_exec(
                "openclaw", "run",
                "--model", "sonnet",
                "--message", prompt,
                "--timeout", "120",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=150
            )
            
            if proc.returncode != 0:
                logger.warning(f"OpenClaw analysis returned {proc.returncode}")
                logger.debug(f"stderr: {stderr.decode()[:500]}")
            
            return stdout.decode()
            
        except asyncio.TimeoutError:
            logger.error("OpenClaw analysis timed out")
            return ""
        except FileNotFoundError:
            logger.error("OpenClaw CLI not found")
            return ""
    
    def _parse_ai_response(
        self,
        response: str,
        config: SelfHealingConfig,
    ) -> list[SuggestionData]:
        """Parse AI response into suggestions."""
        
        suggestions = []
        
        # Try to find JSON in response
        import re
        json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
        if not json_match:
            # Try without code block
            json_match = re.search(r'\{[\s\S]*"suggestions"[\s\S]*\}', response)
        
        if not json_match:
            logger.warning("No JSON found in AI response")
            return []
        
        try:
            json_str = json_match.group(1) if '```' in response else json_match.group(0)
            data = json.loads(json_str)
            
            for s in data.get("suggestions", []):
                # Validate category
                category = s.get("category", "config")
                if category not in [c.value for c in SuggestionCategory]:
                    category = "config"
                
                # Validate severity
                severity = s.get("severity", "medium")
                if severity not in [sv.value for sv in SuggestionSeverity]:
                    severity = "medium"
                
                # Check auto-apply eligibility
                auto_apply = s.get("auto_apply", False)
                if auto_apply:
                    # Only auto-apply low severity or allowed categories
                    min_severity = config.suggestions.min_severity_for_approval
                    severity_order = ["low", "medium", "high", "critical"]
                    if severity_order.index(severity) >= severity_order.index(min_severity):
                        auto_apply = False
                    if category not in config.suggestions.auto_apply_categories:
                        if not config.suggestions.auto_apply:
                            auto_apply = False
                
                suggestions.append(SuggestionData(
                    category=category,
                    severity=severity,
                    title=s.get("title", "Untitled suggestion"),
                    description=s.get("description", ""),
                    current_state=s.get("current_state"),
                    suggested_change=s.get("suggested_change"),
                    expected_impact=s.get("expected_impact"),
                    affected_files=s.get("affected_files"),
                    auto_apply=auto_apply,
                ))
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response: {e}")
        
        return suggestions
    
    async def _apply_suggestion(
        self,
        suggestion_id: int,
        suggestion: SuggestionData,
    ) -> bool:
        """Apply a suggestion automatically (internal)."""
        
        logger.info(f"Auto-applying suggestion {suggestion_id}: {suggestion.title}")
        
        # For auto-apply, we just mark as applied without actual changes
        # Real changes require human review
        self.db.update_healing_suggestion(
            suggestion_id,
            status="applied",
            reviewed_at=datetime.now(),
            reviewed_by="auto",
        )
        
        return True
    
    async def apply_approved_suggestion(
        self,
        suggestion_id: int,
    ) -> dict:
        """Apply an approved suggestion with actual changes.
        
        Args:
            suggestion_id: The suggestion to apply
            
        Returns:
            Result dict with action_id and status
        """
        suggestion = self.db.get_healing_suggestion(suggestion_id)
        if not suggestion:
            return {"success": False, "error": "Suggestion not found"}
        
        if suggestion["status"] != "approved":
            return {"success": False, "error": f"Suggestion is not approved (status: {suggestion['status']})"}
        
        job_id = suggestion["job_id"]
        affected_files = suggestion.get("affected_files") or []
        suggested_change = suggestion.get("suggested_change")
        
        start_time = datetime.now()
        
        try:
            # Determine action type from category
            category = suggestion["category"]
            if category == "prompt":
                action_type = "edit_prompt"
            elif category == "script":
                action_type = "edit_script"
            elif category == "config":
                action_type = "edit_config"
            else:
                action_type = "manual"
            
            # For now, we need the AI to actually make the change
            # This requires calling OpenClaw with the specific change request
            if not affected_files or not suggested_change:
                # Mark as applied but no actual change
                self.db.update_healing_suggestion(
                    suggestion_id,
                    status="applied",
                    applied_at=datetime.now(),
                )
                return {
                    "success": True,
                    "action_id": None,
                    "message": "Suggestion applied (no file changes needed)",
                }
            
            # Apply the change via AI
            result = await self._apply_change_with_ai(
                job_id=job_id,
                file_path=affected_files[0] if affected_files else None,
                suggested_change=suggested_change,
            )
            
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            
            if result["success"]:
                # Record the action
                action_id = self.db.create_healing_action(
                    suggestion_id=suggestion_id,
                    job_id=job_id,
                    action_type=action_type,
                    file_path=result.get("file_path"),
                    original_content=result.get("original_content"),
                    new_content=result.get("new_content"),
                    status="success",
                    execution_duration_ms=duration_ms,
                    ai_session_key=result.get("session_key"),
                )
                
                # Update suggestion
                self.db.update_healing_suggestion(
                    suggestion_id,
                    status="applied",
                    applied_at=datetime.now(),
                    action_id=action_id,
                )
                
                return {
                    "success": True,
                    "action_id": action_id,
                    "message": "Suggestion applied successfully",
                }
            else:
                # Record failed action
                action_id = self.db.create_healing_action(
                    suggestion_id=suggestion_id,
                    job_id=job_id,
                    action_type=action_type,
                    file_path=affected_files[0] if affected_files else None,
                    status="failed",
                    error_message=result.get("error"),
                    execution_duration_ms=duration_ms,
                )
                
                self.db.update_healing_suggestion(
                    suggestion_id,
                    status="failed",
                )
                
                return {
                    "success": False,
                    "action_id": action_id,
                    "error": result.get("error", "Apply failed"),
                }
                
        except Exception as e:
            logger.error(f"Failed to apply suggestion {suggestion_id}: {e}")
            
            self.db.update_healing_suggestion(
                suggestion_id,
                status="failed",
            )
            
            return {"success": False, "error": str(e)}
    
    async def _apply_change_with_ai(
        self,
        job_id: str,
        file_path: str | None,
        suggested_change: str,
    ) -> dict:
        """Use AI to apply a suggested change to a file."""
        
        if not file_path:
            return {"success": True}  # No file to change
        
        file_path_obj = Path(file_path).expanduser()
        
        if not file_path_obj.exists():
            return {"success": False, "error": f"File not found: {file_path}"}
        
        # Read original content
        try:
            original_content = file_path_obj.read_text()
        except Exception as e:
            return {"success": False, "error": f"Cannot read file: {e}"}
        
        # Build prompt for AI to make the change
        prompt = f"""Apply this change to the file.

## File: {file_path}

### Current Content
```
{original_content[:3000]}
```

### Requested Change
{suggested_change}

### Instructions
1. Apply the requested change to the file content
2. Output ONLY the new file content, no explanations
3. Keep the rest of the file unchanged
4. Start your response with ```
"""
        
        try:
            # Call OpenClaw for the change
            proc = await asyncio.create_subprocess_exec(
                "openclaw", "run",
                "--model", "sonnet",
                "--message", prompt,
                "--timeout", "60",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=90
            )
            
            response = stdout.decode()
            
            # Extract new content from response
            import re
            code_match = re.search(r'```(?:\w+)?\s*(.*?)\s*```', response, re.DOTALL)
            if code_match:
                new_content = code_match.group(1)
            else:
                # Try to use the whole response
                new_content = response.strip()
            
            if not new_content or new_content == original_content:
                return {"success": False, "error": "AI returned empty or unchanged content"}
            
            # Write new content
            file_path_obj.write_text(new_content)
            
            return {
                "success": True,
                "file_path": str(file_path_obj),
                "original_content": original_content,
                "new_content": new_content,
            }
            
        except asyncio.TimeoutError:
            return {"success": False, "error": "AI timed out"}
        except FileNotFoundError:
            return {"success": False, "error": "OpenClaw CLI not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _send_notification(
        self,
        job_id: str,
        review_id: int,
        suggestions: list[SuggestionData],
        pending_count: int,
        channel: str = "whatsapp",
    ) -> None:
        """Send notification about new suggestions."""
        try:
            from procclaw.openclaw import _write_pending_alert
            
            # Build notification message
            severity_emoji = {
                "critical": "🔴",
                "high": "🟠",
                "medium": "🟡",
                "low": "🔵",
            }
            
            lines = [
                f"🧬 **Self-Healing Review** for `{job_id}`",
                f"",
                f"Found **{pending_count}** suggestions requiring review:",
                "",
            ]
            
            for s in suggestions[:5]:  # Limit to 5 in notification
                emoji = severity_emoji.get(s.severity, "⚪")
                lines.append(f"{emoji} [{s.severity.upper()}] {s.title}")
            
            if len(suggestions) > 5:
                lines.append(f"... and {len(suggestions) - 5} more")
            
            lines.extend([
                "",
                f"Review in ProcClaw UI → Self-Healing tab",
            ])
            
            message = "\n".join(lines)
            
            # Write to pending alerts file for OpenClaw to pick up
            _write_pending_alert(message, channel=channel)
            
            logger.debug(f"Notification sent for {job_id} review")
            
        except Exception as e:
            logger.warning(f"Failed to send notification: {e}")
    
    def get_review_status(self, job_id: str) -> dict | None:
        """Get status of running review for a job."""
        review_id = self._running_reviews.get(job_id)
        if review_id:
            return self.db.get_healing_review(review_id)
        return None
    
    def is_review_running(self, job_id: str) -> bool:
        """Check if a review is running for a job."""
        return job_id in self._running_reviews


class ProactiveScheduler:
    """Scheduler for proactive healing reviews.
    
    Runs reviews based on job configuration:
    - hourly, daily, weekly schedules
    - on_failure, on_sla_breach triggers
    """
    
    def __init__(self, engine: HealingEngine, supervisor: "Supervisor"):
        self.engine = engine
        self.supervisor = supervisor
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_check: dict[str, datetime] = {}
    
    async def start(self):
        """Start the scheduler."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Proactive healing scheduler started")
    
    async def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Proactive healing scheduler stopped")
    
    async def _run_loop(self):
        """Main scheduler loop."""
        while self._running:
            try:
                await self._check_jobs()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            
            # Check every minute
            await asyncio.sleep(60)
    
    async def _check_jobs(self):
        """Check all jobs for scheduled reviews."""
        now = datetime.now()
        
        for job_id, job_config in self.supervisor.jobs.jobs.items():
            healing = job_config.self_healing
            
            # Skip if not proactive mode
            if not healing.enabled or healing.mode != HealingMode.PROACTIVE:
                continue
            
            # Skip if review is already running
            if self.engine.is_review_running(job_id):
                continue
            
            # Check if due for review
            if self._is_due(job_id, job_config, now):
                logger.info(f"Triggering scheduled review for {job_id}")
                try:
                    await self.engine.run_review(job_id, job_config, trigger="scheduled")
                except SkipReviewError:
                    pass  # Already logged in run_review
                except Exception as e:
                    logger.error(f"Scheduled review failed for {job_id}: {e}")
                
                self._last_check[job_id] = now
    
    def _is_due(
        self,
        job_id: str,
        job_config: JobConfig,
        now: datetime,
    ) -> bool:
        """Check if a job is due for review."""
        
        schedule = job_config.self_healing.review_schedule
        frequency = schedule.frequency
        
        last_check = self._last_check.get(job_id)
        if last_check is None:
            # First check - also verify min_runs
            runs = self.engine.db.get_runs(job_id=job_id, limit=schedule.min_runs + 1)
            if len(runs) < schedule.min_runs:
                return False
            return True
        
        # Calculate interval based on frequency
        if frequency == ReviewFrequency.HOURLY:
            interval = timedelta(hours=1)
        elif frequency == ReviewFrequency.DAILY:
            interval = timedelta(days=1)
            # Check time of day
            review_time = datetime.strptime(schedule.time, "%H:%M").time()
            if now.time() < review_time:
                return False
        elif frequency == ReviewFrequency.WEEKLY:
            interval = timedelta(weeks=1)
            # Check day of week
            if now.weekday() != schedule.day:
                return False
        elif frequency == ReviewFrequency.MANUAL:
            return False
        else:
            # on_failure, on_sla_breach handled elsewhere
            return False
        
        return now - last_check >= interval
    
    async def trigger_on_event(
        self,
        job_id: str,
        event: str,
        run_id: int | None = None,
    ):
        """Trigger review based on event.
        
        Args:
            job_id: Job to review
            event: "failure" or "sla_breach"
            run_id: Run ID that triggered the event (for failure events)
        """
        job_config = self.supervisor.jobs.get_job(job_id)
        if not job_config:
            return
        
        healing = job_config.self_healing
        if not healing.enabled:
            return
        
        frequency = healing.review_schedule.frequency
        
        should_trigger = False
        if event == "failure" and frequency == ReviewFrequency.ON_FAILURE:
            should_trigger = True
        elif event == "sla_breach" and frequency == ReviewFrequency.ON_SLA_BREACH:
            should_trigger = True
        
        if should_trigger:
            logger.info(f"Triggering {event} review for {job_id}")
            try:
                await self.engine.run_review(
                    job_id, 
                    job_config, 
                    trigger=event,
                    failed_run_id=run_id if event == "failure" else None,
                )
            except SkipReviewError:
                pass  # Already logged
            except Exception as e:
                logger.error(f"Event-triggered review failed for {job_id}: {e}")
