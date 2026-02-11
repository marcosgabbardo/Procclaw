"""Self-Healing v2 Engine - Proactive behavior analysis.

This module provides proactive job analysis and optimization suggestions.
Unlike the reactive self_healing.py (triggered on failures), this engine
runs periodically to analyze job behavior and suggest improvements.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import yaml
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
    # Pre-generated content for review before apply
    proposed_content: str | None = None  # The new file content
    target_file: str | None = None  # Which file to modify


class SkipReviewError(Exception):
    """Raised when a review should be skipped (e.g., not enough runs)."""
    pass


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
        force: bool = False,
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
            force: If True, analyze all runs ignoring min_runs and last review time
            
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
            
            return await self._run_review_internal(job_id, job_config, trigger, failed_run_id, force)
    
    async def _run_review_internal(
        self,
        job_id: str,
        job_config: JobConfig | None = None,
        trigger: str = "scheduled",
        failed_run_id: int | None = None,
        force: bool = False,
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
                job_id, job_config, scope, trigger=trigger, failed_run_id=failed_run_id, force=force
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
                    proposed_content=suggestion.proposed_content,
                    target_file=suggestion.target_file,
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
        force: bool = False,
    ) -> AnalysisContext:
        """Collect all context data for analysis.
        
        Args:
            job_id: Job to analyze
            job_config: Job configuration
            scope: What to analyze (logs, runs, ai_sessions, sla)
            trigger: What triggered this review ('scheduled', 'failure', 'sla_breach', 'manual')
            failed_run_id: Specific run ID when triggered by failure (reactive mode)
            force: If True, analyze all recent runs ignoring time scope and min_runs
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
                # PROACTIVE: Runs since last completed review (or all recent if force)
                if force:
                    # Force mode: analyze all runs from last 30 days
                    since = datetime.now() - timedelta(days=30)
                    logger.debug(f"Proactive mode (FORCE): analyzing all runs since {since}")
                else:
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
                
                # Check min_runs threshold (skip if force)
                if not force:
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
        logger.info(f"Built analysis prompt: {len(prompt)} chars")
        
        # Call OpenClaw for analysis
        try:
            result = await self._call_openclaw_analysis(prompt)
            logger.info(f"OpenClaw returned: {len(result)} chars")
            
            # Save raw response for debugging
            try:
                debug_path = Path.home() / ".procclaw" / "logs" / "last_healing_response.txt"
                debug_path.write_text(result)
                logger.debug(f"Saved AI response to {debug_path}")
            except Exception:
                pass
            
            suggestions = self._parse_ai_response(result, config)
            logger.info(f"Parsed {len(suggestions)} suggestions from AI response")
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
            duration = run.get('duration_seconds') or 0
            prompt += f"- {status} Run {run['id']}: exit={run.get('exit_code')}, "
            prompt += f"duration={duration:.1f}s, "
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

You MUST analyze the job and provide at least 1-3 suggestions for improvement.
Even if the job is working, there's ALWAYS room for improvement.

**CRITICAL**: For each suggestion that involves modifying a file (prompt, script, config), 
you MUST include the COMPLETE proposed file content. The user needs to see EXACTLY what 
will change BEFORE approving. This is mandatory for transparency.

Output your analysis in JSON format:

```json
{
  "suggestions": [
    {
      "category": "performance|cost|reliability|security|config|prompt|script",
      "severity": "low|medium|high|critical",
      "title": "Short title",
      "description": "Detailed description of the issue or improvement opportunity",
      "current_state": "What's currently happening",
      "suggested_change": "What should be changed",
      "expected_impact": "Expected improvement",
      "affected_files": ["list of files to modify, if any"],
      "target_file": "/path/to/file.ext",
      "proposed_content": "The COMPLETE new file content that will replace the current content. Include the ENTIRE file, not just changes.",
      "auto_apply": false
    }
  ]
}
```

### Rules for proposed_content:
1. **MUST be complete** - the entire file content, not just a snippet
2. **MUST be ready to apply** - no placeholders, no "..." or "[rest of file]"
3. For config changes to jobs.yaml, include ONLY the affected job section
4. For prompts (.md), include the full prompt text
5. For scripts (.py, .sh), include the full script
6. If no file change needed (just a config suggestion), set target_file and proposed_content to null

Look for improvements in these areas:
- **Performance**: Slow runs? High resource usage? Could be faster?
- **Reliability**: Any failures? Flaky behavior? Missing error handling? Retries?
- **Cost**: Too many API calls? Verbose prompts? Unnecessary runs?
- **Security**: Hardcoded secrets? Unsafe practices?
- **Config**: Better timeouts? Schedule optimization? Resource limits?
- **Prompt**: Clearer instructions? Better examples? Reduce tokens?
- **Script**: Code improvements? Edge cases? Better logging?

IMPORTANT:
- You MUST provide at least one suggestion
- Even successful jobs can be improved (faster, cheaper, more reliable)
- Look at run duration - could it be faster?
- Look at schedule - is it optimal?
- Look at logs - any warnings or inefficiencies?
- If truly perfect, suggest monitoring or documentation improvements

Set auto_apply=true only for trivial, low-risk config changes.
"""
        
        return prompt
    
    async def _call_openclaw_analysis(self, prompt: str) -> str:
        """Call OpenClaw CLI for analysis."""
        
        try:
            # Use openclaw agent --local for one-shot analysis
            # Generate unique session id to avoid conflicts
            import uuid
            session_id = f"procclaw-healing-{uuid.uuid4().hex[:8]}"
            
            proc = await asyncio.create_subprocess_exec(
                "openclaw", "agent",
                "--local",
                "--session-id", session_id,
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
                logger.warning(f"stderr: {stderr.decode()[:500]}")
            
            result = stdout.decode()
            logger.info(f"OpenClaw stdout: {len(result)} chars, stderr: {len(stderr)} chars")
            
            return result
            
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
        
        # Log raw response for debugging
        logger.debug(f"AI response length: {len(response)}")
        logger.debug(f"AI response preview: {response[:500]}...")
        
        # Try to find JSON in response
        import re
        
        # Strategy: Find all ```json ... ``` blocks using greedy matching,
        # then use JSON-aware brace counting (skipping string contents)
        # to find the valid JSON object boundary.
        json_match = re.search(r'```json\s*(.*)\s*```', response, re.DOTALL)
        
        if json_match:
            json_str = json_match.group(1).strip()
            
            # JSON-aware brace counting: skip characters inside strings
            brace_count = 0
            json_end = 0
            in_string = False
            escape_next = False
            
            for i, char in enumerate(json_str):
                if escape_next:
                    escape_next = False
                    continue
                if char == '\\' and in_string:
                    escape_next = True
                    continue
                if char == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = i + 1
                        break
            
            if json_end > 0:
                json_str = json_str[:json_end]
                class _FakeMatch:
                    def group(self, n):
                        return json_str if n == 1 else json_str
                json_match = _FakeMatch()
            else:
                # Brace counting failed, try json.loads on the whole thing
                try:
                    json.loads(json_str)
                    class _FakeMatch2:
                        def group(self, n):
                            return json_str if n == 1 else json_str
                    json_match = _FakeMatch2()
                except json.JSONDecodeError:
                    json_match = None
        
        if not json_match:
            # Try without code block
            json_match = re.search(r'\{[\s\S]*"suggestions"[\s\S]*\}', response)
        
        if not json_match:
            logger.warning(f"No JSON found in AI response. Full response: {response[:1000]}")
            return []
        
        try:
            json_str = json_match.group(1) if '```' in response else json_match.group(0)
            data = json.loads(json_str)
            
            raw_suggestions = data.get("suggestions", [])
            logger.info(f"AI returned {len(raw_suggestions)} raw suggestions")
            if not raw_suggestions:
                logger.warning(f"AI returned empty suggestions array. Parsed data: {data}")
            
            for s in raw_suggestions:
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
                    proposed_content=s.get("proposed_content"),
                    target_file=s.get("target_file"),
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
        proposed_content = suggestion.get("proposed_content")
        target_file = suggestion.get("target_file")
        
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
            
            # Check if we have pre-generated content (from analysis phase)
            if proposed_content and target_file:
                # Use pre-generated content - no AI call needed!
                logger.info(f"Using pre-generated content for {target_file} ({len(proposed_content)} chars)")
                result = await self._apply_pregenerated_content(
                    job_id=job_id,
                    file_path=target_file,
                    new_content=proposed_content,
                )
            elif not affected_files and not suggested_change:
                # No file changes needed
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
            else:
                # Fall back to AI-generated changes (old behavior)
                logger.info("No pre-generated content, calling AI to apply change")
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
            
            # Record failed action even for exceptions
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            action_id = self.db.create_healing_action(
                suggestion_id=suggestion_id,
                job_id=job_id,
                action_type=action_type,
                file_path=affected_files[0] if affected_files else None,
                status="failed",
                error_message=str(e),
                execution_duration_ms=duration_ms,
            )
            
            self.db.update_healing_suggestion(
                suggestion_id,
                status="failed",
                action_id=action_id,
            )
            
            return {"success": False, "action_id": action_id, "error": str(e)}
    
    def _validate_file_for_job(self, job_id: str, file_path: str) -> tuple[bool, str | None]:
        """Validate that a file belongs to a specific job.
        
        Returns:
            (is_valid, error_message)
        """
        file_path_obj = Path(file_path).expanduser()
        file_name = file_path_obj.name
        
        # Get job config to know allowed paths
        job = self.supervisor.jobs.get_job(job_id)
        if not job:
            return False, f"Job {job_id} not found"
        
        # jobs.yaml is always allowed (we'll isolate the section)
        if file_name == "jobs.yaml":
            return True, None
        
        # Check if it's the job's script
        if job.cmd:
            # Extract script path from cmd (e.g., "python3 /path/to/script.py")
            try:
                parts = shlex.split(job.cmd)
                for part in parts:
                    if part.endswith(('.py', '.sh', '.bash', '.js')):
                        if Path(part).expanduser().resolve() == file_path_obj.resolve():
                            return True, None
            except:
                pass
        
        # Check if it's in prompts directory
        prompts_dir = Path("~/.procclaw/prompts").expanduser()
        if file_path_obj.is_relative_to(prompts_dir):
            file_str = str(file_path_obj)
            # Check if job_id or variations are in the path
            # e.g., job_id="oc-twitter-trends" should match "twitter-trends.md"
            if job_id in file_str:
                return True, None
            # Try without common prefixes
            job_base = job_id.removeprefix("oc-").removeprefix("job-")
            if job_base in file_str:
                return True, None
            # Check if file content references this job
            try:
                content = file_path_obj.read_text()
                if job_id in content:
                    return True, None
            except:
                pass
            return False, f"Prompt file does not belong to job {job_id}"
        
        # Check if file contains job_id reference (loose check for other files)
        try:
            content = file_path_obj.read_text()
            if job_id in content:
                return True, None
        except:
            pass
        
        return False, f"File {file_path} is not associated with job {job_id}"

    def _extract_job_section_from_yaml(self, content: str, job_id: str) -> tuple[str | None, int, int]:
        """Extract just the job section from jobs.yaml.
        
        Returns:
            (job_section_yaml, start_line, end_line)
        """
        try:
            # Parse YAML
            data = yaml.safe_load(content)
            if not isinstance(data, dict):
                return None, 0, 0
            
            # Handle both formats: {jobs: {id: config}} and {id: config}
            jobs_dict = data.get("jobs", data)
            if not isinstance(jobs_dict, dict):
                return None, 0, 0
            
            if job_id not in jobs_dict:
                return None, 0, 0
            
            # Serialize just this job's config
            job_section = {job_id: jobs_dict[job_id]}
            job_yaml = yaml.dump(job_section, default_flow_style=False, sort_keys=False)
            
            # Find line numbers (approximate)
            lines = content.split('\n')
            start_line = 0
            end_line = len(lines)
            
            for i, line in enumerate(lines):
                if line.startswith(f"{job_id}:"):
                    start_line = i
                    # Find next top-level key
                    for j in range(i + 1, len(lines)):
                        if lines[j] and not lines[j][0].isspace() and not lines[j].startswith('#'):
                            end_line = j
                            break
                    break
            
            return job_yaml, start_line, end_line
            
        except Exception as e:
            logger.warning(f"Failed to extract job section: {e}")
            return None, 0, 0

    def _merge_job_section_to_yaml(self, original_content: str, job_id: str, new_job_yaml: str) -> str:
        """Merge the modified job section back into the full jobs.yaml."""
        try:
            # Parse both
            original_data = yaml.safe_load(original_content)
            new_job_data = yaml.safe_load(new_job_yaml)
            
            if not isinstance(original_data, dict) or not isinstance(new_job_data, dict):
                raise ValueError("Invalid YAML structure")
            
            # Get the new job config
            if job_id in new_job_data:
                new_config = new_job_data[job_id]
            else:
                # AI might have returned without the key
                new_config = new_job_data
            
            # Handle both formats: {jobs: {id: config}} and {id: config}
            if "jobs" in original_data and isinstance(original_data["jobs"], dict):
                # Format: jobs: { id: config }
                original_data["jobs"][job_id] = new_config
            else:
                # Format: { id: config }
                original_data[job_id] = new_config
            
            # Serialize back
            return yaml.dump(original_data, default_flow_style=False, sort_keys=False, allow_unicode=True)
            
        except Exception as e:
            logger.error(f"Failed to merge job section: {e}")
            raise

    async def _apply_pregenerated_content(
        self,
        job_id: str,
        file_path: str,
        new_content: str,
    ) -> dict:
        """Apply pre-generated content directly (no AI call needed).
        
        This is used when the AI already generated the proposed content during
        the analysis phase. The content was shown to the user for review,
        and now we just apply it directly.
        
        SECURITY: Same file validation as _apply_change_with_ai.
        """
        file_path_obj = Path(file_path).expanduser()
        file_name = file_path_obj.name
        
        if not file_path_obj.exists():
            return {"success": False, "error": f"File not found: {file_path}"}
        
        # SECURITY: Validate file belongs to this job
        is_valid, error = self._validate_file_for_job(job_id, file_path)
        if not is_valid:
            logger.warning(f"Security: blocked access to {file_path} for job {job_id}: {error}")
            return {"success": False, "error": f"Access denied: {error}"}
        
        # Read original content for backup/rollback
        try:
            original_content = file_path_obj.read_text()
        except Exception as e:
            return {"success": False, "error": f"Cannot read file: {e}"}
        
        # SPECIAL HANDLING: For jobs.yaml, merge the section back FIRST
        is_jobs_yaml = file_name == "jobs.yaml"
        final_content = new_content
        
        if is_jobs_yaml:
            try:
                final_content = self._merge_job_section_to_yaml(original_content, job_id, new_content)
                logger.info(f"Merged job section: {len(new_content)} chars -> {len(final_content)} chars")
            except Exception as e:
                return {"success": False, "error": f"Failed to merge job section: {e}"}
        
        # SAFETY: Check for suspicious content reduction (AFTER merge for jobs.yaml)
        if len(final_content.strip()) < len(original_content.strip()) * 0.5:
            return {
                "success": False,
                "error": f"Content reduced by more than 50% ({len(original_content)} -> {len(final_content)}). Blocked for safety."
            }
        
        # Write the new content
        try:
            file_path_obj.write_text(final_content)
            logger.info(f"Applied pre-generated content to {file_path}")
        except Exception as e:
            return {"success": False, "error": f"Failed to write file: {e}"}
        
        return {
            "success": True,
            "file_path": str(file_path_obj),
            "original_content": original_content,
            "new_content": final_content,
        }

    async def _apply_change_with_ai(
        self,
        job_id: str,
        file_path: str | None,
        suggested_change: str,
    ) -> dict:
        """Use AI to apply a suggested change to a file.
        
        SECURITY: This method validates that the file belongs to the specified job
        and only allows modifications within the job's scope.
        """
        
        if not file_path:
            return {"success": True}  # No file to change
        
        file_path_obj = Path(file_path).expanduser()
        file_name = file_path_obj.name
        
        if not file_path_obj.exists():
            return {"success": False, "error": f"File not found: {file_path}"}
        
        # SECURITY: Validate file belongs to this job
        is_valid, error = self._validate_file_for_job(job_id, file_path)
        if not is_valid:
            logger.warning(f"Security: blocked access to {file_path} for job {job_id}: {error}")
            return {"success": False, "error": f"Access denied: {error}"}
        
        # Read original content
        try:
            original_content = file_path_obj.read_text()
        except Exception as e:
            return {"success": False, "error": f"Cannot read file: {e}"}
        
        # SPECIAL HANDLING: For jobs.yaml, only send the job's section
        is_jobs_yaml = file_name == "jobs.yaml"
        content_for_ai = original_content
        
        if is_jobs_yaml:
            job_section, _, _ = self._extract_job_section_from_yaml(original_content, job_id)
            if job_section:
                content_for_ai = job_section
                logger.info(f"Isolated job section for {job_id} ({len(job_section)} chars)")
            else:
                return {"success": False, "error": f"Job {job_id} not found in jobs.yaml"}
        
        # Determine file type for appropriate prompt
        file_ext = file_path_obj.suffix.lower()
        is_yaml_file = file_ext in ('.yaml', '.yml')
        is_markdown_file = file_ext in ('.md', '.markdown')
        
        # Use larger limit for content (10KB should cover most files)
        content_limit = 10000
        truncated = len(content_for_ai) > content_limit
        content_to_send = content_for_ai[:content_limit]
        
        if is_yaml_file:
            file_type_instruction = "YAML configuration"
            code_block_type = "yaml"
        elif is_markdown_file:
            file_type_instruction = "Markdown document"
            code_block_type = "markdown"
        else:
            file_type_instruction = "file"
            code_block_type = ""
        
        # Build prompt for AI to make the change - with STRICT scope restrictions
        prompt = f"""Apply this change to the {file_type_instruction}.

## CRITICAL CONSTRAINTS
- This file belongs to job `{job_id}`
- Output the COMPLETE modified file content
- DO NOT truncate or omit any sections
- Keep ALL existing content that is not being changed
- Only modify what is specifically requested

## File: {file_path}
## Type: {file_type_instruction}

### Current Content
```{code_block_type}
{content_to_send}
```
{"[TRUNCATED - file continues beyond this point, preserve any content after this]" if truncated else ""}

### Requested Change
{suggested_change}

### Instructions
1. Apply ONLY the requested change
2. Output the COMPLETE file content (not just the changed parts)
3. DO NOT remove or omit any existing sections
4. Start your response with ```{code_block_type}
5. End with ```
"""
        
        try:
            # Call OpenClaw for the change
            import uuid
            session_id = f"procclaw-apply-{uuid.uuid4().hex[:8]}"
            
            proc = await asyncio.create_subprocess_exec(
                "openclaw", "agent",
                "--local",
                "--session-id", session_id,
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
            logger.info(f"AI apply response: {len(response)} chars")
            logger.debug(f"AI apply response preview: {response[:500]}")
            
            # Extract new content from response
            import re
            # Find ALL code blocks and use the LARGEST one (most likely to be complete file)
            code_blocks = re.findall(r'```(?:yaml|json|python|sh|bash|markdown|md|text)?\s*(.*?)\s*```', response, re.DOTALL)
            if code_blocks:
                # Use the largest code block (most complete content)
                new_content = max(code_blocks, key=len)
                logger.info(f"Extracted content from code block ({len(code_blocks)} blocks found, using largest: {len(new_content)} chars)")
            else:
                # Try to use the whole response if it looks like file content
                new_content = response.strip()
                logger.info("Using raw response as content")
            
            if not new_content:
                logger.warning(f"AI returned empty content. Response: {response[:500]}")
                return {"success": False, "error": f"AI returned empty content. Response preview: {response[:200]}"}
            
            # SAFETY CHECK: Prevent significant content loss (truncation)
            # Allow up to 20% reduction for cleanup, but flag anything more
            original_len = len(content_for_ai)
            new_len = len(new_content)
            if new_len < original_len * 0.5:  # More than 50% reduction
                reduction_pct = round((1 - new_len / original_len) * 100)
                logger.error(f"Content reduction too large: {original_len} -> {new_len} ({reduction_pct}% loss)")
                return {
                    "success": False, 
                    "error": f"Content reduced by {reduction_pct}% ({original_len} -> {new_len} chars). This looks like truncation. Aborting to prevent data loss."
                }
            
            # For jobs.yaml, merge the job section back into the full file
            final_content = new_content
            if is_jobs_yaml:
                try:
                    final_content = self._merge_job_section_to_yaml(
                        original_content, job_id, new_content
                    )
                    logger.info(f"Merged job {job_id} section back to jobs.yaml")
                except Exception as e:
                    return {"success": False, "error": f"Failed to merge job section: {e}"}
            
            if final_content == original_content:
                logger.warning("AI returned unchanged content")
                return {"success": False, "error": "AI returned unchanged content (no modifications made)"}
            
            # Write new content
            file_path_obj.write_text(final_content)
            
            return {
                "success": True,
                "file_path": str(file_path_obj),
                "original_content": original_content,
                "new_content": final_content,
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
        
        # Event-based frequencies are handled by trigger_on_event, not scheduler
        if frequency in (ReviewFrequency.ON_FAILURE, ReviewFrequency.ON_SLA_BREACH, ReviewFrequency.MANUAL):
            return False
        
        # First verify we're in the correct time window for this frequency
        # This prevents triggering on wrong day/time even on first check
        if frequency == ReviewFrequency.DAILY:
            review_time = datetime.strptime(schedule.time, "%H:%M").time()
            # Only trigger if we're past the scheduled time
            if now.time() < review_time:
                return False
        elif frequency == ReviewFrequency.WEEKLY:
            # Check day of week (0=Monday, 6=Sunday)
            if now.weekday() != schedule.day:
                return False
            # Also check time on the correct day
            review_time = datetime.strptime(schedule.time, "%H:%M").time()
            if now.time() < review_time:
                return False
        
        # Verify min_runs
        runs = self.engine.db.get_runs(job_id=job_id, limit=schedule.min_runs + 1)
        if len(runs) < schedule.min_runs:
            return False
        
        last_check = self._last_check.get(job_id)
        if last_check is None:
            # First check - we're in the right window and have enough runs
            return True
        
        # Calculate interval based on frequency
        if frequency == ReviewFrequency.HOURLY:
            interval = timedelta(hours=1)
        elif frequency == ReviewFrequency.DAILY:
            interval = timedelta(days=1)
        elif frequency == ReviewFrequency.WEEKLY:
            interval = timedelta(weeks=1)
        else:
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
