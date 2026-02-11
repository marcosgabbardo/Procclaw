"""Tests for Self-Healing v2 functionality."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from procclaw.models import (
    # New enums
    HealingMode,
    ReviewFrequency,
    SuggestionCategory,
    SuggestionSeverity,
    SuggestionStatus,
    ActionStatus,
    ReviewStatus,
    # New config models
    ReviewScheduleConfig,
    ReviewScopeConfig,
    SuggestionBehaviorConfig,
    # Updated config
    SelfHealingConfig,
    # Data models
    HealingReview,
    HealingSuggestion,
    HealingActionRecord,
)
from procclaw.db import Database


class TestHealingEnums:
    """Tests for Self-Healing v2 enums."""
    
    def test_healing_mode_values(self):
        """Test HealingMode enum values."""
        assert HealingMode.REACTIVE == "reactive"
        assert HealingMode.PROACTIVE == "proactive"
    
    def test_review_frequency_values(self):
        """Test ReviewFrequency enum values."""
        assert ReviewFrequency.HOURLY == "hourly"
        assert ReviewFrequency.DAILY == "daily"
        assert ReviewFrequency.WEEKLY == "weekly"
        assert ReviewFrequency.ON_FAILURE == "on_failure"
        assert ReviewFrequency.ON_SLA_BREACH == "on_sla_breach"
        assert ReviewFrequency.MANUAL == "manual"
    
    def test_suggestion_category_values(self):
        """Test SuggestionCategory enum values."""
        assert SuggestionCategory.PERFORMANCE == "performance"
        assert SuggestionCategory.COST == "cost"
        assert SuggestionCategory.RELIABILITY == "reliability"
        assert SuggestionCategory.SECURITY == "security"
        assert SuggestionCategory.CONFIG == "config"
        assert SuggestionCategory.PROMPT == "prompt"
        assert SuggestionCategory.SCRIPT == "script"
    
    def test_suggestion_severity_values(self):
        """Test SuggestionSeverity enum values."""
        assert SuggestionSeverity.LOW == "low"
        assert SuggestionSeverity.MEDIUM == "medium"
        assert SuggestionSeverity.HIGH == "high"
        assert SuggestionSeverity.CRITICAL == "critical"
    
    def test_suggestion_status_values(self):
        """Test SuggestionStatus enum values."""
        assert SuggestionStatus.PENDING == "pending"
        assert SuggestionStatus.APPROVED == "approved"
        assert SuggestionStatus.REJECTED == "rejected"
        assert SuggestionStatus.APPLIED == "applied"
        assert SuggestionStatus.FAILED == "failed"
    
    def test_action_status_values(self):
        """Test ActionStatus enum values."""
        assert ActionStatus.SUCCESS == "success"
        assert ActionStatus.FAILED == "failed"
        assert ActionStatus.ROLLED_BACK == "rolled_back"
    
    def test_review_status_values(self):
        """Test ReviewStatus enum values."""
        assert ReviewStatus.RUNNING == "running"
        assert ReviewStatus.COMPLETED == "completed"
        assert ReviewStatus.FAILED == "failed"


class TestHealingConfigModels:
    """Tests for Self-Healing v2 config models."""
    
    def test_review_schedule_config_defaults(self):
        """Test ReviewScheduleConfig default values."""
        config = ReviewScheduleConfig()
        assert config.frequency == ReviewFrequency.DAILY
        assert config.time == "03:00"
        assert config.day == 1
        assert config.min_runs == 5
    
    def test_review_schedule_config_custom(self):
        """Test ReviewScheduleConfig with custom values."""
        config = ReviewScheduleConfig(
            frequency=ReviewFrequency.WEEKLY,
            time="09:00",
            day=5,  # Friday
            min_runs=10
        )
        assert config.frequency == ReviewFrequency.WEEKLY
        assert config.time == "09:00"
        assert config.day == 5
        assert config.min_runs == 10
    
    def test_review_scope_config_defaults(self):
        """Test ReviewScopeConfig default values (all enabled)."""
        config = ReviewScopeConfig()
        assert config.analyze_logs is True
        assert config.analyze_runs is True
        assert config.analyze_ai_sessions is True
        assert config.analyze_sla is True
        assert config.analyze_workflows is True
        assert config.analyze_script is True
        assert config.analyze_prompt is True
        assert config.analyze_config is True
    
    def test_review_scope_config_selective(self):
        """Test ReviewScopeConfig with selective analysis."""
        config = ReviewScopeConfig(
            analyze_ai_sessions=False,
            analyze_prompt=False
        )
        assert config.analyze_logs is True
        assert config.analyze_ai_sessions is False
        assert config.analyze_prompt is False
    
    def test_suggestion_behavior_config_defaults(self):
        """Test SuggestionBehaviorConfig default values."""
        config = SuggestionBehaviorConfig()
        assert config.auto_apply is False
        assert config.auto_apply_categories == []
        assert config.min_severity_for_approval == "medium"
        assert config.notify_on_suggestion is True
        assert config.notify_channel == "whatsapp"
    
    def test_suggestion_behavior_config_auto_apply(self):
        """Test SuggestionBehaviorConfig with auto-apply."""
        config = SuggestionBehaviorConfig(
            auto_apply=True,
            auto_apply_categories=["config", "prompt"],
            min_severity_for_approval="high"
        )
        assert config.auto_apply is True
        assert "config" in config.auto_apply_categories
        assert config.min_severity_for_approval == "high"


class TestSelfHealingConfigV2:
    """Tests for updated SelfHealingConfig with v2 fields."""
    
    def test_backwards_compatibility(self):
        """Test that old config format still works."""
        # Old format (v1)
        config = SelfHealingConfig(enabled=True)
        assert config.enabled is True
        assert config.mode == HealingMode.REACTIVE  # Default
        assert config.analysis is not None
        assert config.remediation is not None
        assert config.notify is not None
    
    def test_new_v2_fields(self):
        """Test new v2 fields are available."""
        config = SelfHealingConfig(
            enabled=True,
            mode=HealingMode.PROACTIVE,
            review_schedule=ReviewScheduleConfig(frequency=ReviewFrequency.HOURLY),
            review_scope=ReviewScopeConfig(analyze_prompt=False),
            suggestions=SuggestionBehaviorConfig(auto_apply=True)
        )
        assert config.mode == HealingMode.PROACTIVE
        assert config.review_schedule.frequency == ReviewFrequency.HOURLY
        assert config.review_scope.analyze_prompt is False
        assert config.suggestions.auto_apply is True
    
    def test_json_serialization(self):
        """Test JSON serialization/deserialization."""
        config = SelfHealingConfig(
            enabled=True,
            mode=HealingMode.PROACTIVE
        )
        json_str = config.model_dump_json()
        restored = SelfHealingConfig.model_validate_json(json_str)
        assert restored.enabled == config.enabled
        assert restored.mode == config.mode


class TestHealingDataModels:
    """Tests for Self-Healing v2 data models."""
    
    def test_healing_review_model(self):
        """Test HealingReview data model."""
        now = datetime.now()
        review = HealingReview(
            id=1,
            job_id="test-job",
            started_at=now,
            finished_at=now + timedelta(minutes=5),
            status=ReviewStatus.COMPLETED,
            runs_analyzed=10,
            suggestions_count=2,
            created_at=now
        )
        assert review.id == 1
        assert review.job_id == "test-job"
        assert review.status == ReviewStatus.COMPLETED
        assert review.runs_analyzed == 10
    
    def test_healing_suggestion_model(self):
        """Test HealingSuggestion data model."""
        now = datetime.now()
        suggestion = HealingSuggestion(
            id=1,
            review_id=1,
            job_id="test-job",
            category=SuggestionCategory.PROMPT,
            severity=SuggestionSeverity.HIGH,
            title="Update deprecated API",
            description="The prompt uses deprecated Twitter API v1.1",
            current_state="old code",
            suggested_change="new code",
            expected_impact="Better reliability",
            affected_files=["~/.procclaw/prompts/test.md"],
            status=SuggestionStatus.PENDING,
            created_at=now
        )
        assert suggestion.category == SuggestionCategory.PROMPT
        assert suggestion.severity == SuggestionSeverity.HIGH
        assert len(suggestion.affected_files) == 1
    
    def test_healing_action_model(self):
        """Test HealingActionRecord data model."""
        now = datetime.now()
        action = HealingActionRecord(
            id=1,
            suggestion_id=1,
            job_id="test-job",
            action_type="edit_prompt",
            file_path="~/.procclaw/prompts/test.md",
            original_content="old content",
            new_content="new content",
            status=ActionStatus.SUCCESS,
            can_rollback=True,
            execution_duration_ms=1500,
            created_at=now
        )
        assert action.action_type == "edit_prompt"
        assert action.status == ActionStatus.SUCCESS
        assert action.can_rollback is True


class TestHealingDatabaseMigration:
    """Tests for Self-Healing v2 database migration."""
    
    @pytest.fixture
    def temp_db(self):
        """Create a temporary database."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        yield db
        # Cleanup
        if db_path.exists():
            db_path.unlink()
    
    def test_healing_reviews_table_exists(self, temp_db):
        """Test that healing_reviews table is created."""
        with temp_db._connect() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='healing_reviews'"
            )
            assert cursor.fetchone() is not None
    
    def test_healing_suggestions_table_exists(self, temp_db):
        """Test that healing_suggestions table is created."""
        with temp_db._connect() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='healing_suggestions'"
            )
            assert cursor.fetchone() is not None
    
    def test_healing_actions_table_exists(self, temp_db):
        """Test that healing_actions table is created."""
        with temp_db._connect() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='healing_actions'"
            )
            assert cursor.fetchone() is not None
    
    def test_insert_healing_review(self, temp_db):
        """Test inserting a healing review."""
        with temp_db._connect() as conn:
            conn.execute("""
                INSERT INTO healing_reviews (job_id, started_at, status)
                VALUES ('test-job', datetime('now'), 'running')
            """)
            cursor = conn.execute("SELECT * FROM healing_reviews WHERE job_id = 'test-job'")
            row = cursor.fetchone()
            assert row is not None
            assert row['job_id'] == 'test-job'
            assert row['status'] == 'running'
    
    def test_insert_healing_suggestion(self, temp_db):
        """Test inserting a healing suggestion."""
        with temp_db._connect() as conn:
            # First insert a review
            conn.execute("""
                INSERT INTO healing_reviews (job_id, started_at, status)
                VALUES ('test-job', datetime('now'), 'completed')
            """)
            review_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            
            # Then insert a suggestion
            conn.execute("""
                INSERT INTO healing_suggestions 
                (review_id, job_id, category, severity, title, description, status)
                VALUES (?, 'test-job', 'prompt', 'high', 'Test Title', 'Test Description', 'pending')
            """, (review_id,))
            
            cursor = conn.execute("SELECT * FROM healing_suggestions WHERE job_id = 'test-job'")
            row = cursor.fetchone()
            assert row is not None
            assert row['category'] == 'prompt'
            assert row['severity'] == 'high'
    
    def test_insert_healing_action(self, temp_db):
        """Test inserting a healing action."""
        with temp_db._connect() as conn:
            # First insert review and suggestion
            conn.execute("""
                INSERT INTO healing_reviews (job_id, started_at, status)
                VALUES ('test-job', datetime('now'), 'completed')
            """)
            review_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            
            conn.execute("""
                INSERT INTO healing_suggestions 
                (review_id, job_id, category, severity, title, description, status)
                VALUES (?, 'test-job', 'script', 'medium', 'Test', 'Test', 'approved')
            """, (review_id,))
            suggestion_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            
            # Then insert an action
            conn.execute("""
                INSERT INTO healing_actions 
                (suggestion_id, job_id, action_type, status)
                VALUES (?, 'test-job', 'edit_script', 'success')
            """, (suggestion_id,))
            
            cursor = conn.execute("SELECT * FROM healing_actions WHERE job_id = 'test-job'")
            row = cursor.fetchone()
            assert row is not None
            assert row['action_type'] == 'edit_script'
            assert row['status'] == 'success'
    
    def test_indexes_created(self, temp_db):
        """Test that indexes are created."""
        with temp_db._connect() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_healing_%'"
            )
            indexes = [row['name'] for row in cursor.fetchall()]
            
            expected_indexes = [
                'idx_healing_reviews_job',
                'idx_healing_reviews_started',
                'idx_healing_suggestions_job',
                'idx_healing_suggestions_status',
                'idx_healing_suggestions_created',
                'idx_healing_actions_job',
                'idx_healing_actions_suggestion',
            ]
            
            for idx in expected_indexes:
                assert idx in indexes, f"Index {idx} not found"


class TestHealingConfigIntegration:
    """Integration tests for Self-Healing v2 config parsing."""
    
    def test_yaml_parsing_backwards_compatible(self):
        """Test that old YAML format still works."""
        # Simulate old format
        old_config = {
            "enabled": True,
            "analysis": {
                "include_logs": True,
                "log_lines": 100
            },
            "remediation": {
                "enabled": True,
                "max_attempts": 2
            }
        }
        config = SelfHealingConfig(**old_config)
        assert config.enabled is True
        assert config.analysis.log_lines == 100
        assert config.mode == HealingMode.REACTIVE
    
    def test_yaml_parsing_new_format(self):
        """Test that new YAML format works."""
        new_config = {
            "enabled": True,
            "mode": "proactive",
            "review_schedule": {
                "frequency": "weekly",
                "time": "02:00",
                "day": 0  # Sunday
            },
            "suggestions": {
                "auto_apply": False,
                "notify_on_suggestion": True
            }
        }
        config = SelfHealingConfig(**new_config)
        assert config.mode == HealingMode.PROACTIVE
        assert config.review_schedule.frequency == ReviewFrequency.WEEKLY
        assert config.review_schedule.day == 0
