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


class HealingEngine:
    """Proactive healing engine for job behavior analysis.
    
    This engine:
    1. Collects comprehensive data about job behavior
    2. Sends data to AI for analysis
    3. Generates improvement suggestions
    4. Optionally auto-applies low-risk changes
    """
    
    def __init__(self, db: "Database", supervisor: "Supervisor"):
        self.db = db
        self.supervisor = supervisor
        self._running_reviews: dict[str, int] = {}  # job_id -> review_id
    
    async def run_review(
        self,
        job_id: str,
        job_config: JobConfig | None = None,
    ) -> int:
        """Run a healing review for a job.
        
        Args:
            job_id: Job to analyze
            job_config: Optional job config (fetched if not provided)
            
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
            context = await self._collect_context(job_id, job_config, scope)
            
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
    ) -> AnalysisContext:
        """Collect all context data for analysis."""
        
        # Get recent runs
        recent_runs = []
        if scope.analyze_runs:
            runs = self.db.get_runs(job_id=job_id, limit=20)
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
        """Apply a suggestion automatically."""
        
        # For now, just mark as applied
        # TODO: Implement actual file modifications
        
        logger.info(f"Auto-applying suggestion {suggestion_id}: {suggestion.title}")
        
        self.db.update_healing_suggestion(
            suggestion_id,
            status="applied",
            reviewed_at=datetime.now(),
            reviewed_by="auto",
        )
        
        return True
    
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
                    await self.engine.run_review(job_id, job_config)
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
    ):
        """Trigger review based on event.
        
        Args:
            job_id: Job to review
            event: "failure" or "sla_breach"
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
                await self.engine.run_review(job_id, job_config)
            except Exception as e:
                logger.error(f"Event-triggered review failed for {job_id}: {e}")
