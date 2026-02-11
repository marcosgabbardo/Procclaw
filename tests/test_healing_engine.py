"""Tests for Self-Healing v2 Engine."""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from procclaw.core.healing_engine import (
    HealingEngine,
    ProactiveScheduler,
    AnalysisContext,
    SuggestionData,
)
from procclaw.db import Database
from procclaw.models import (
    HealingMode,
    ReviewFrequency,
    ReviewScheduleConfig,
    ReviewScopeConfig,
    SuggestionBehaviorConfig,
    SelfHealingConfig,
    JobConfig,
    JobType,
)


class MockJobConfig:
    """Mock job config for testing."""
    
    def __init__(self, job_id: str, **kwargs):
        self.id = job_id
        self.name = kwargs.get("name", job_id)
        self.cmd = kwargs.get("cmd", "echo test")
        self.type = MagicMock()
        self.type.value = kwargs.get("type", "manual")
        
        # Self-healing config - use min_runs=0 for tests to avoid SkipReviewError
        default_schedule = ReviewScheduleConfig(min_runs=0)
        self.self_healing = SelfHealingConfig(
            enabled=kwargs.get("healing_enabled", True),
            mode=kwargs.get("healing_mode", HealingMode.PROACTIVE),
            review_schedule=kwargs.get("review_schedule", default_schedule),
            review_scope=kwargs.get("review_scope", ReviewScopeConfig()),
            suggestions=kwargs.get("suggestions", SuggestionBehaviorConfig()),
        )
    
    def model_dump(self):
        return {
            "id": self.id,
            "name": self.name,
            "cmd": self.cmd,
            "type": self.type.value,
        }


class MockJobs:
    """Mock jobs config."""
    
    def __init__(self):
        self.jobs = {}
    
    def get_job(self, job_id: str):
        return self.jobs.get(job_id)


class MockSupervisor:
    """Mock supervisor for testing."""
    
    def __init__(self, db: Database):
        self.db = db
        self.jobs = MockJobs()


@pytest.fixture
def temp_db():
    """Create a temporary database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    db = Database(db_path)
    yield db
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def mock_supervisor(temp_db):
    """Create mock supervisor."""
    return MockSupervisor(temp_db)


@pytest.fixture
def engine(mock_supervisor):
    """Create healing engine."""
    return HealingEngine(mock_supervisor.db, mock_supervisor)


class TestAnalysisContext:
    """Tests for AnalysisContext."""
    
    def test_context_creation(self):
        """Test creating analysis context."""
        context = AnalysisContext(
            job_id="test-job",
            job_config={"cmd": "echo test"},
            recent_runs=[{"id": 1, "exit_code": 0}],
            log_samples={"1": "test log"},
            ai_sessions=[],
            sla_metrics=None,
            sla_violations=[],
            prompt_content="test prompt",
            script_content="echo test",
        )
        assert context.job_id == "test-job"
        assert len(context.recent_runs) == 1
        assert context.prompt_content == "test prompt"


class TestSuggestionData:
    """Tests for SuggestionData."""
    
    def test_suggestion_creation(self):
        """Test creating suggestion data."""
        suggestion = SuggestionData(
            category="performance",
            severity="high",
            title="Optimize loop",
            description="The loop is inefficient",
            suggested_change="Use list comprehension",
            auto_apply=False,
        )
        assert suggestion.category == "performance"
        assert suggestion.severity == "high"
        assert suggestion.auto_apply is False


class TestHealingEngine:
    """Tests for HealingEngine."""
    
    def test_engine_creation(self, engine):
        """Test creating healing engine."""
        assert engine is not None
        assert engine._running_reviews == {}
    
    def test_is_review_running_false(self, engine):
        """Test is_review_running when no review."""
        assert engine.is_review_running("test-job") is False
    
    @pytest.mark.asyncio
    async def test_run_review_job_not_found(self, engine):
        """Test run_review with non-existent job."""
        with pytest.raises(ValueError, match="not found"):
            await engine.run_review("non-existent-job")
    
    @pytest.mark.asyncio
    async def test_run_review_creates_record(self, engine, mock_supervisor, temp_db):
        """Test that run_review creates a review record."""
        # Setup job
        job_config = MockJobConfig("test-job")
        mock_supervisor.jobs.jobs["test-job"] = job_config
        
        # Mock AI analysis
        with patch.object(engine, '_analyze_with_ai', new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = []
            
            review_id = await engine.run_review("test-job")
        
        assert review_id > 0
        
        # Verify review record
        review = temp_db.get_healing_review(review_id)
        assert review is not None
        assert review["job_id"] == "test-job"
        assert review["status"] == "completed"
    
    @pytest.mark.asyncio
    async def test_run_review_with_suggestions(self, engine, mock_supervisor, temp_db):
        """Test run_review generates suggestions."""
        # Setup job
        job_config = MockJobConfig("test-job")
        mock_supervisor.jobs.jobs["test-job"] = job_config
        
        # Mock AI analysis with suggestions
        with patch.object(engine, '_analyze_with_ai', new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = [
                SuggestionData(
                    category="performance",
                    severity="medium",
                    title="Test suggestion",
                    description="Test description",
                ),
            ]
            
            review_id = await engine.run_review("test-job")
        
        # Verify suggestions created
        suggestions = temp_db.get_healing_suggestions(review_id=review_id)
        assert len(suggestions) == 1
        assert suggestions[0]["title"] == "Test suggestion"
        
        # Verify review has suggestion count
        review = temp_db.get_healing_review(review_id)
        assert review["suggestions_count"] == 1
    
    @pytest.mark.asyncio
    async def test_concurrent_review_blocked(self, engine, mock_supervisor):
        """Test that concurrent reviews for same job are blocked."""
        job_config = MockJobConfig("test-job")
        mock_supervisor.jobs.jobs["test-job"] = job_config
        
        # Simulate running review
        engine._running_reviews["test-job"] = 123
        
        # Should return existing review ID
        review_id = await engine.run_review("test-job")
        assert review_id == 123
    
    def test_get_prompt_path(self, engine):
        """Test extracting prompt path from command."""
        job_config = MockJobConfig(
            "test-job",
            cmd="python script.py --prompt ~/.procclaw/prompts/test.md"
        )
        
        # Mock path exists
        with patch.object(Path, 'exists', return_value=True):
            path = engine._get_prompt_path(job_config)
        
        assert path is not None
        assert "test.md" in str(path)
    
    def test_get_script_path(self, engine):
        """Test extracting script path from command."""
        job_config = MockJobConfig(
            "test-job",
            cmd="python3 ~/.procclaw/scripts/test.py"
        )
        
        # Mock path exists
        with patch.object(Path, 'exists', return_value=True):
            path = engine._get_script_path(job_config)
        
        assert path is not None
        assert "test.py" in str(path)
    
    def test_parse_ai_response_valid_json(self, engine, mock_supervisor):
        """Test parsing valid AI response."""
        job_config = MockJobConfig("test-job")
        
        response = '''Here's my analysis:
        
```json
{
  "suggestions": [
    {
      "category": "performance",
      "severity": "high",
      "title": "Optimize database queries",
      "description": "Multiple N+1 queries detected",
      "auto_apply": false
    }
  ]
}
```
'''
        
        suggestions = engine._parse_ai_response(response, job_config.self_healing)
        assert len(suggestions) == 1
        assert suggestions[0].category == "performance"
        assert suggestions[0].severity == "high"
    
    def test_parse_ai_response_invalid_category(self, engine, mock_supervisor):
        """Test parsing response with invalid category defaults to config."""
        job_config = MockJobConfig("test-job")
        
        response = '''```json
{
  "suggestions": [
    {
      "category": "invalid_category",
      "severity": "medium",
      "title": "Test",
      "description": "Test"
    }
  ]
}
```'''
        
        suggestions = engine._parse_ai_response(response, job_config.self_healing)
        assert len(suggestions) == 1
        assert suggestions[0].category == "config"  # Default
    
    def test_parse_ai_response_no_json(self, engine, mock_supervisor):
        """Test parsing response without JSON returns empty."""
        job_config = MockJobConfig("test-job")
        
        response = "No JSON here, just plain text analysis."
        
        suggestions = engine._parse_ai_response(response, job_config.self_healing)
        assert suggestions == []
    
    def test_build_analysis_prompt(self, engine):
        """Test building analysis prompt."""
        context = AnalysisContext(
            job_id="test-job",
            job_config={"cmd": "echo test", "type": "manual"},
            recent_runs=[
                {"id": 1, "exit_code": 0, "duration_seconds": 5.0, "trigger": "manual"}
            ],
            log_samples={"1": "test output"},
            ai_sessions=[],
            sla_metrics=None,
            sla_violations=[],
            prompt_content=None,
            script_content=None,
        )
        
        prompt = engine._build_analysis_prompt(context)
        
        assert "test-job" in prompt
        assert "echo test" in prompt
        assert "Run 1" in prompt
        assert "suggestions" in prompt


class TestProactiveScheduler:
    """Tests for ProactiveScheduler."""
    
    def test_scheduler_creation(self, engine, mock_supervisor):
        """Test creating scheduler."""
        scheduler = ProactiveScheduler(engine, mock_supervisor)
        assert scheduler is not None
        assert scheduler._running is False
    
    @pytest.mark.asyncio
    async def test_scheduler_start_stop(self, engine, mock_supervisor):
        """Test starting and stopping scheduler."""
        scheduler = ProactiveScheduler(engine, mock_supervisor)
        
        await scheduler.start()
        assert scheduler._running is True
        assert scheduler._task is not None
        
        await scheduler.stop()
        assert scheduler._running is False
    
    def test_is_due_manual_frequency(self, engine, mock_supervisor):
        """Test is_due returns False for manual frequency."""
        scheduler = ProactiveScheduler(engine, mock_supervisor)
        
        job_config = MockJobConfig(
            "test-job",
            review_schedule=ReviewScheduleConfig(frequency=ReviewFrequency.MANUAL)
        )
        
        assert scheduler._is_due("test-job", job_config, datetime.now()) is False
    
    def test_is_due_first_check_not_enough_runs(self, engine, mock_supervisor, temp_db):
        """Test is_due returns False when not enough runs."""
        scheduler = ProactiveScheduler(engine, mock_supervisor)
        
        job_config = MockJobConfig(
            "test-job",
            review_schedule=ReviewScheduleConfig(
                frequency=ReviewFrequency.DAILY,
                min_runs=5
            )
        )
        
        # No runs in DB
        assert scheduler._is_due("test-job", job_config, datetime.now()) is False
    
    def test_is_due_hourly_interval(self, engine, mock_supervisor, temp_db):
        """Test is_due for hourly frequency."""
        scheduler = ProactiveScheduler(engine, mock_supervisor)
        
        job_config = MockJobConfig(
            "test-job",
            review_schedule=ReviewScheduleConfig(
                frequency=ReviewFrequency.HOURLY,
                min_runs=0
            )
        )
        
        now = datetime.now()
        
        # First check with enough time passed
        scheduler._last_check["test-job"] = now - timedelta(hours=2)
        assert scheduler._is_due("test-job", job_config, now) is True
        
        # Not enough time passed
        scheduler._last_check["test-job"] = now - timedelta(minutes=30)
        assert scheduler._is_due("test-job", job_config, now) is False
    
    @pytest.mark.asyncio
    async def test_trigger_on_failure_event(self, engine, mock_supervisor):
        """Test trigger_on_event for failure."""
        scheduler = ProactiveScheduler(engine, mock_supervisor)
        
        job_config = MockJobConfig(
            "test-job",
            review_schedule=ReviewScheduleConfig(
                frequency=ReviewFrequency.ON_FAILURE
            )
        )
        mock_supervisor.jobs.jobs["test-job"] = job_config
        
        # Mock run_review
        with patch.object(engine, 'run_review', new_callable=AsyncMock) as mock_run:
            mock_run.return_value = 1
            
            await scheduler.trigger_on_event("test-job", "failure")
            
            mock_run.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_trigger_on_sla_breach_event(self, engine, mock_supervisor):
        """Test trigger_on_event for SLA breach."""
        scheduler = ProactiveScheduler(engine, mock_supervisor)
        
        job_config = MockJobConfig(
            "test-job",
            review_schedule=ReviewScheduleConfig(
                frequency=ReviewFrequency.ON_SLA_BREACH
            )
        )
        mock_supervisor.jobs.jobs["test-job"] = job_config
        
        # Mock run_review
        with patch.object(engine, 'run_review', new_callable=AsyncMock) as mock_run:
            mock_run.return_value = 1
            
            await scheduler.trigger_on_event("test-job", "sla_breach")
            
            mock_run.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_trigger_on_event_wrong_frequency(self, engine, mock_supervisor):
        """Test trigger_on_event doesn't trigger for wrong frequency."""
        scheduler = ProactiveScheduler(engine, mock_supervisor)
        
        job_config = MockJobConfig(
            "test-job",
            review_schedule=ReviewScheduleConfig(
                frequency=ReviewFrequency.DAILY  # Not on_failure
            )
        )
        mock_supervisor.jobs.jobs["test-job"] = job_config
        
        # Mock run_review
        with patch.object(engine, 'run_review', new_callable=AsyncMock) as mock_run:
            await scheduler.trigger_on_event("test-job", "failure")
            
            mock_run.assert_not_called()


class TestAutoApply:
    """Tests for auto-apply functionality."""
    
    @pytest.mark.asyncio
    async def test_auto_apply_suggestion(self, engine, mock_supervisor, temp_db):
        """Test auto-applying a suggestion."""
        job_config = MockJobConfig(
            "test-job",
            suggestions=SuggestionBehaviorConfig(auto_apply=True)
        )
        mock_supervisor.jobs.jobs["test-job"] = job_config
        
        # Create a review manually
        review_id = temp_db.create_healing_review("test-job")
        
        # Create suggestion
        suggestion = SuggestionData(
            category="config",
            severity="low",
            title="Auto-apply test",
            description="This should be auto-applied",
            auto_apply=True,
        )
        
        result = await engine._apply_suggestion(1, suggestion)
        assert result is True
    
    def test_auto_apply_blocked_by_severity(self, engine, mock_supervisor):
        """Test auto-apply blocked for high severity."""
        job_config = MockJobConfig(
            "test-job",
            suggestions=SuggestionBehaviorConfig(
                auto_apply=True,
                min_severity_for_approval="medium"
            )
        )
        
        response = '''```json
{
  "suggestions": [
    {
      "category": "config",
      "severity": "high",
      "title": "High severity",
      "description": "Should not auto-apply",
      "auto_apply": true
    }
  ]
}
```'''
        
        suggestions = engine._parse_ai_response(response, job_config.self_healing)
        assert len(suggestions) == 1
        assert suggestions[0].auto_apply is False  # Blocked by severity


class TestHealingQueue:
    """Tests for healing queue behavior."""
    
    def test_semaphore_initialized(self, engine):
        """Test that semaphore is initialized."""
        assert hasattr(engine, '_semaphore')
        assert engine._semaphore._value == 1
    
    def test_get_running_openclaw_jobs_empty(self, engine, mock_supervisor):
        """Test no running jobs returns empty list."""
        result = engine._get_running_openclaw_jobs()
        assert result == []
    
    def test_get_running_openclaw_jobs_with_running(self, engine, mock_supervisor):
        """Test detection of running openclaw jobs."""
        # Add an openclaw job
        job_config = MockJobConfig("oc-job", type="openclaw")
        mock_supervisor.jobs.jobs["oc-job"] = job_config
        
        # Simulate running process
        mock_supervisor._processes = {"oc-job": "mock_process"}
        
        result = engine._get_running_openclaw_jobs()
        assert "oc-job" in result
    
    def test_get_running_openclaw_jobs_ignores_non_openclaw(self, engine, mock_supervisor):
        """Test that non-openclaw jobs are ignored."""
        # Add a manual job
        job_config = MockJobConfig("manual-job", type="manual")
        mock_supervisor.jobs.jobs["manual-job"] = job_config
        
        # Simulate running process
        mock_supervisor._processes = {"manual-job": "mock_process"}
        
        result = engine._get_running_openclaw_jobs()
        assert result == []
    
    @pytest.mark.asyncio
    async def test_wait_for_openclaw_slot_immediate(self, engine, mock_supervisor):
        """Test immediate slot available."""
        # No running jobs
        result = await engine._wait_for_openclaw_slot()
        assert result is True
    
    @pytest.mark.asyncio
    async def test_semaphore_prevents_parallel(self, engine, mock_supervisor, temp_db):
        """Test that semaphore prevents parallel reviews."""
        job_config = MockJobConfig("test-job")
        mock_supervisor.jobs.jobs["test-job"] = job_config
        
        # Track execution order
        execution_log = []
        
        async def mock_review(job_id, delay):
            execution_log.append(f"start:{job_id}")
            await asyncio.sleep(delay)
            execution_log.append(f"end:{job_id}")
        
        # Patch the internal method
        original = engine._run_review_internal
        
        async def patched(*args, **kwargs):
            job_id = args[0]
            await mock_review(job_id, 0.1)
            return 1
        
        engine._run_review_internal = patched
        
        try:
            # Start two reviews "simultaneously"
            mock_supervisor.jobs.jobs["job-a"] = MockJobConfig("job-a")
            mock_supervisor.jobs.jobs["job-b"] = MockJobConfig("job-b")
            
            task1 = asyncio.create_task(engine.run_review("job-a"))
            task2 = asyncio.create_task(engine.run_review("job-b"))
            
            await asyncio.gather(task1, task2)
            
            # Verify sequential execution (not interleaved)
            # Should be: start:a, end:a, start:b, end:b (or b then a)
            assert len(execution_log) == 4
            # First job should complete before second starts
            first_end_idx = next(i for i, x in enumerate(execution_log) if x.startswith("end:"))
            second_start_idx = next(i for i, x in enumerate(execution_log[first_end_idx:]) if x.startswith("start:")) + first_end_idx
            assert first_end_idx < second_start_idx
        finally:
            engine._run_review_internal = original


class TestJobScopeValidation:
    """Tests for job scope validation and isolation."""

    @pytest.fixture
    def engine_with_job(self, temp_db):
        """Engine with a specific job configured."""
        mock_supervisor = MagicMock()
        mock_supervisor.jobs = MagicMock()
        mock_supervisor.jobs.jobs = {}
        mock_supervisor.get_job = lambda job_id: mock_supervisor.jobs.jobs.get(job_id, {}).get("_raw_config")
        engine = HealingEngine(temp_db, mock_supervisor)
        
        # Add a job
        mock_supervisor.jobs.jobs["my-job"] = {
            "_raw_config": {
                "id": "my-job",
                "cmd": "python3 /path/to/my-job-script.py",
            }
        }
        return engine, mock_supervisor

    def test_validate_jobs_yaml_allowed(self, engine_with_job):
        """jobs.yaml is always allowed (will be isolated)."""
        engine, _ = engine_with_job
        is_valid, error = engine._validate_file_for_job("my-job", "~/.procclaw/jobs.yaml")
        assert is_valid is True
        assert error is None

    def test_validate_job_script_allowed(self, engine_with_job, tmp_path):
        """Job's own script is allowed."""
        engine, mock_supervisor = engine_with_job
        
        # Create a script file
        script = tmp_path / "my-job-script.py"
        script.write_text("print('hello')")
        
        # Update job config to use this script
        mock_supervisor.jobs.jobs["my-job"]["_raw_config"]["cmd"] = f"python3 {script}"
        
        is_valid, error = engine._validate_file_for_job("my-job", str(script))
        assert is_valid is True

    def test_validate_other_job_script_blocked(self, engine_with_job, tmp_path):
        """Another job's script is blocked."""
        engine, mock_supervisor = engine_with_job
        
        # Script that doesn't belong to my-job
        other_script = tmp_path / "other-job-script.py"
        other_script.write_text("print('other')")
        
        is_valid, error = engine._validate_file_for_job("my-job", str(other_script))
        assert is_valid is False
        assert "not associated" in error

    def test_validate_file_with_job_reference_allowed(self, engine_with_job, tmp_path):
        """Files that reference the job_id in content are allowed."""
        engine, _ = engine_with_job
        
        # Create a file that mentions the job
        config_file = tmp_path / "some-config.yaml"
        config_file.write_text("# Config for my-job\njob: my-job\nvalue: 123")
        
        is_valid, error = engine._validate_file_for_job("my-job", str(config_file))
        # Should be valid because "my-job" is in the content
        assert is_valid is True

    def test_validate_job_not_found(self, engine_with_job):
        """Validation fails if job doesn't exist."""
        engine, _ = engine_with_job
        is_valid, error = engine._validate_file_for_job("nonexistent-job", "/some/file.yaml")
        assert is_valid is False
        assert "not found" in error

    def test_extract_job_section_from_yaml(self, engine_with_job):
        """Extract only the specific job's section from jobs.yaml."""
        engine, _ = engine_with_job
        
        yaml_content = """job-a:
  name: Job A
  cmd: echo a
  schedule: "0 * * * *"

my-job:
  name: My Job
  cmd: python3 script.py
  type: scheduled

job-b:
  name: Job B
  cmd: echo b
"""
        section, start, end = engine._extract_job_section_from_yaml(yaml_content, "my-job")
        
        assert section is not None
        assert "my-job:" in section
        assert "My Job" in section
        assert "job-a" not in section
        assert "job-b" not in section

    def test_extract_job_section_not_found(self, engine_with_job):
        """Returns None if job not in YAML."""
        engine, _ = engine_with_job
        
        yaml_content = """other-job:
  name: Other
  cmd: echo other
"""
        section, _, _ = engine._extract_job_section_from_yaml(yaml_content, "my-job")
        assert section is None

    def test_merge_job_section_back(self, engine_with_job):
        """Merge modified job section back without affecting other jobs."""
        engine, _ = engine_with_job
        
        original = """job-a:
  name: Job A
  cmd: echo a

my-job:
  name: My Job
  cmd: python3 script.py

job-b:
  name: Job B
  cmd: echo b
"""
        modified_section = """my-job:
  name: My Job (Updated)
  cmd: python3 script.py
  timeout: 60
"""
        result = engine._merge_job_section_to_yaml(original, "my-job", modified_section)
        
        import yaml
        parsed = yaml.safe_load(result)
        
        # my-job should be updated
        assert parsed["my-job"]["name"] == "My Job (Updated)"
        assert parsed["my-job"]["timeout"] == 60
        
        # Other jobs should be unchanged
        assert parsed["job-a"]["name"] == "Job A"
        assert parsed["job-b"]["name"] == "Job B"
        assert "timeout" not in parsed["job-a"]
        assert "timeout" not in parsed["job-b"]

    def test_merge_preserves_all_jobs(self, engine_with_job):
        """Merge doesn't remove any existing jobs."""
        engine, _ = engine_with_job
        
        original = """first:
  cmd: echo 1
second:
  cmd: echo 2
third:
  cmd: echo 3
"""
        modified = """second:
  cmd: echo 2
  new_field: value
"""
        result = engine._merge_job_section_to_yaml(original, "second", modified)
        
        import yaml
        parsed = yaml.safe_load(result)
        
        assert len(parsed) == 3
        assert "first" in parsed
        assert "second" in parsed
        assert "third" in parsed
        assert parsed["second"]["new_field"] == "value"
