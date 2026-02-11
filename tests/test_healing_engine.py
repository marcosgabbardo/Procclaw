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
        
        # Self-healing config
        self.self_healing = SelfHealingConfig(
            enabled=kwargs.get("healing_enabled", True),
            mode=kwargs.get("healing_mode", HealingMode.PROACTIVE),
            review_schedule=kwargs.get("review_schedule", ReviewScheduleConfig()),
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
