"""Tests for SLA (Service Level Agreement) functionality."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from procclaw.models import (
    JobConfig,
    JobRun,
    JobType,
    SLAConfig,
    SLAStatus,
    ErrorBudgetConfig,
    ErrorBudgetPeriod,
)
from procclaw.sla import (
    check_run_sla,
    calculate_sla_metrics,
    calculate_job_sla_score,
    get_effective_sla,
    get_expected_start_time,
    parse_period,
    create_config_hash,
    SLACheckResult,
    SLAMetrics,
    DEFAULT_SLA_BY_TYPE,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def basic_job():
    """Create a basic scheduled job."""
    return JobConfig(
        name="Test Job",
        cmd="echo test",
        type=JobType.SCHEDULED,
        schedule="0 9 * * *",  # 9 AM daily
        sla=SLAConfig(
            enabled=True,
            success_rate=95.0,
            schedule_tolerance=300,
            max_duration=600,
        ),
    )


@pytest.fixture
def openclaw_job():
    """Create an OpenClaw job."""
    return JobConfig(
        name="OpenClaw Job",
        cmd="python runner.py",
        type=JobType.OPENCLAW,
        schedule="0 17 * * *",  # 5 PM daily
        sla=SLAConfig(
            enabled=True,
            success_rate=90.0,
            schedule_tolerance=600,
            max_duration=900,
        ),
    )


@pytest.fixture
def continuous_job():
    """Create a continuous job."""
    return JobConfig(
        name="Continuous Job",
        cmd="python daemon.py",
        type=JobType.CONTINUOUS,
        sla=SLAConfig(
            enabled=True,
            success_rate=99.0,
        ),
    )


@pytest.fixture
def successful_run():
    """Create a successful job run that started on time (9:00 AM)."""
    # Use a fixed time that matches the schedule (9 AM)
    started = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    return JobRun(
        id=1,
        job_id="test-job",
        started_at=started,
        finished_at=started + timedelta(minutes=3),
        exit_code=0,
        duration_seconds=180,
        trigger="scheduled",
    )


@pytest.fixture
def failed_run():
    """Create a failed job run."""
    started = datetime.now() - timedelta(minutes=5)
    return JobRun(
        id=2,
        job_id="test-job",
        started_at=started,
        finished_at=started + timedelta(minutes=2),
        exit_code=1,
        duration_seconds=120,
        trigger="scheduled",
        error="Process failed",
    )


@pytest.fixture
def late_run():
    """Create a run that started late."""
    # Started 10 minutes after expected
    started = datetime.now().replace(hour=9, minute=10, second=0, microsecond=0)
    return JobRun(
        id=3,
        job_id="test-job",
        started_at=started,
        finished_at=started + timedelta(minutes=3),
        exit_code=0,
        duration_seconds=180,
        trigger="scheduled",
    )


@pytest.fixture
def over_duration_run():
    """Create a run that exceeded max duration."""
    started = datetime.now() - timedelta(minutes=15)
    return JobRun(
        id=4,
        job_id="test-job",
        started_at=started,
        finished_at=started + timedelta(minutes=12),
        exit_code=0,
        duration_seconds=720,  # 12 minutes (over 10 min limit)
        trigger="scheduled",
    )


# ============================================================
# Test parse_period
# ============================================================

class TestParsePeriod:
    """Tests for parse_period function."""
    
    def test_parse_hours(self):
        """Test parsing hours."""
        assert parse_period("24h") == timedelta(hours=24)
        assert parse_period("1h") == timedelta(hours=1)
        assert parse_period("48h") == timedelta(hours=48)
    
    def test_parse_days(self):
        """Test parsing days."""
        assert parse_period("7d") == timedelta(days=7)
        assert parse_period("1d") == timedelta(days=1)
        assert parse_period("30d") == timedelta(days=30)
    
    def test_parse_weeks(self):
        """Test parsing weeks."""
        assert parse_period("1w") == timedelta(weeks=1)
        assert parse_period("2w") == timedelta(weeks=2)
    
    def test_parse_months(self):
        """Test parsing months (approximate)."""
        assert parse_period("1m") == timedelta(days=30)
        assert parse_period("3m") == timedelta(days=90)
    
    def test_parse_invalid(self):
        """Test parsing invalid period returns default."""
        assert parse_period("invalid") == timedelta(days=7)
        assert parse_period("") == timedelta(days=7)
        assert parse_period("123") == timedelta(days=7)


# ============================================================
# Test get_effective_sla
# ============================================================

class TestGetEffectiveSLA:
    """Tests for get_effective_sla function."""
    
    def test_enabled_sla(self, basic_job):
        """Test getting effective SLA when enabled."""
        sla = get_effective_sla(basic_job)
        assert sla["enabled"] is True
        assert sla["success_rate"] == 95.0
        assert sla["schedule_tolerance"] == 300
        assert sla["max_duration"] == 600
    
    def test_disabled_sla(self, basic_job):
        """Test getting effective SLA when disabled."""
        basic_job.sla.enabled = False
        sla = get_effective_sla(basic_job)
        assert sla["enabled"] is False
        # Should use defaults for scheduled type
        assert sla["success_rate"] == 95.0
        assert sla["schedule_tolerance"] == 300
    
    def test_defaults_by_type_scheduled(self):
        """Test default SLA for scheduled jobs."""
        job = JobConfig(name="Test", cmd="echo", type=JobType.SCHEDULED)
        sla = get_effective_sla(job)
        defaults = DEFAULT_SLA_BY_TYPE["scheduled"]
        assert sla["success_rate"] == defaults["success_rate"]
    
    def test_defaults_by_type_continuous(self):
        """Test default SLA for continuous jobs."""
        job = JobConfig(name="Test", cmd="echo", type=JobType.CONTINUOUS)
        sla = get_effective_sla(job)
        defaults = DEFAULT_SLA_BY_TYPE["continuous"]
        assert sla["success_rate"] == defaults["success_rate"]
    
    def test_defaults_by_type_openclaw(self):
        """Test default SLA for OpenClaw jobs."""
        job = JobConfig(name="Test", cmd="echo", type=JobType.OPENCLAW)
        sla = get_effective_sla(job)
        defaults = DEFAULT_SLA_BY_TYPE["openclaw"]
        assert sla["success_rate"] == defaults["success_rate"]


# ============================================================
# Test check_run_sla
# ============================================================

class TestCheckRunSLA:
    """Tests for check_run_sla function."""
    
    def test_successful_run_passes(self, basic_job, successful_run):
        """Test that a successful run passes SLA."""
        result = check_run_sla(successful_run, basic_job)
        assert result.status == "pass"
        assert result.success_check is True
        assert result.exit_code == 0
    
    def test_failed_run_fails_success_check(self, basic_job, failed_run):
        """Test that a failed run fails SLA."""
        result = check_run_sla(failed_run, basic_job)
        assert result.success_check is False
        assert result.exit_code == 1
    
    def test_over_duration_fails(self, basic_job, over_duration_run):
        """Test that over-duration run fails duration check."""
        result = check_run_sla(over_duration_run, basic_job)
        assert result.duration_check is False
        assert result.max_duration == 600
        assert result.duration_seconds == 720
    
    def test_partial_status(self, basic_job, over_duration_run):
        """Test partial status when some checks pass."""
        result = check_run_sla(over_duration_run, basic_job)
        # Success check passes (exit_code=0), but duration fails
        assert result.success_check is True
        assert result.duration_check is False
        assert result.status in ("fail", "partial")
    
    def test_no_sla_status(self):
        """Test no_sla status when no checks applicable."""
        job = JobConfig(
            name="Test",
            cmd="echo",
            type=JobType.MANUAL,
            sla=SLAConfig(enabled=False),
        )
        run = JobRun(
            id=1,
            job_id="test",
            started_at=datetime.now(),
            # No finished_at means run is still running
        )
        result = check_run_sla(run, job)
        # With no finished_at and no SLA, should be no_sla
        assert result.status == "no_sla"
    
    def test_result_to_json(self, basic_job, successful_run):
        """Test SLACheckResult serializes to JSON."""
        result = check_run_sla(successful_run, basic_job)
        json_str = result.to_json()
        assert '"status": "pass"' in json_str
        assert '"success_check": true' in json_str


# ============================================================
# Test calculate_sla_metrics
# ============================================================

class TestCalculateSLAMetrics:
    """Tests for calculate_sla_metrics function."""
    
    def test_empty_runs(self, basic_job):
        """Test metrics with no runs."""
        metrics = calculate_sla_metrics(
            "test-job", basic_job, [],
            datetime.now() - timedelta(days=7),
            datetime.now(),
        )
        assert metrics.total_runs == 0
        assert metrics.status == "no_data"
    
    def test_all_successful_runs(self, basic_job):
        """Test metrics with all successful runs."""
        # Use fixed times at 9:00 AM to match schedule
        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        runs = [
            JobRun(
                id=i,
                job_id="test-job",
                started_at=base - timedelta(days=i),
                finished_at=base - timedelta(days=i) + timedelta(minutes=5),
                exit_code=0,
                duration_seconds=300,
                trigger="scheduled",
            )
            for i in range(1, 8)
        ]
        
        metrics = calculate_sla_metrics(
            "test-job", basic_job, runs,
            base - timedelta(days=7),
            base,
        )
        
        assert metrics.total_runs == 7
        assert metrics.successful_runs == 7
        assert metrics.failed_runs == 0
        assert metrics.success_rate == 100.0
        assert metrics.status == "healthy"
    
    def test_mixed_runs(self, basic_job):
        """Test metrics with mixed success/failure."""
        now = datetime.now()
        runs = []
        
        # 8 successful, 2 failed = 80% success rate
        for i in range(8):
            runs.append(JobRun(
                id=i,
                job_id="test-job",
                started_at=now - timedelta(days=i),
                finished_at=now - timedelta(days=i) + timedelta(minutes=5),
                exit_code=0,
                duration_seconds=300,
                trigger="scheduled",
            ))
        
        for i in range(2):
            runs.append(JobRun(
                id=10 + i,
                job_id="test-job",
                started_at=now - timedelta(days=i, hours=12),
                finished_at=now - timedelta(days=i, hours=12) + timedelta(minutes=5),
                exit_code=1,
                duration_seconds=300,
                trigger="scheduled",
            ))
        
        metrics = calculate_sla_metrics(
            "test-job", basic_job, runs,
            now - timedelta(days=7),
            now,
        )
        
        assert metrics.total_runs == 10
        assert metrics.successful_runs == 8
        assert metrics.failed_runs == 2
        assert metrics.success_rate == 80.0
    
    def test_duration_statistics(self, basic_job):
        """Test duration statistics calculation."""
        now = datetime.now()
        durations = [100, 200, 300, 400, 500]
        runs = [
            JobRun(
                id=i,
                job_id="test-job",
                started_at=now - timedelta(days=i),
                finished_at=now - timedelta(days=i) + timedelta(seconds=d),
                exit_code=0,
                duration_seconds=d,
                trigger="scheduled",
            )
            for i, d in enumerate(durations)
        ]
        
        metrics = calculate_sla_metrics(
            "test-job", basic_job, runs,
            now - timedelta(days=7),
            now,
        )
        
        assert metrics.avg_duration == 300.0  # mean of 100-500
        assert metrics.max_duration_actual == 500.0
    
    def test_overall_score_calculation(self, basic_job):
        """Test overall score is weighted average."""
        # Use fixed times at 9:00 AM to match schedule
        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        runs = [
            JobRun(
                id=i,
                job_id="test-job",
                started_at=base - timedelta(days=i),
                finished_at=base - timedelta(days=i) + timedelta(minutes=5),
                exit_code=0,
                duration_seconds=300,
                trigger="scheduled",
            )
            for i in range(10)
        ]
        
        metrics = calculate_sla_metrics(
            "test-job", basic_job, runs,
            base - timedelta(days=10),
            base,
        )
        
        # With 100% success, adherence, and duration
        # Score = 100*0.5 + 100*0.3 + 100*0.2 = 100
        assert metrics.overall_score == 100.0
    
    def test_to_dict(self, basic_job):
        """Test metrics convert to dictionary."""
        metrics = calculate_sla_metrics(
            "test-job", basic_job, [],
            datetime.now() - timedelta(days=7),
            datetime.now(),
        )
        
        d = metrics.to_dict()
        assert "job_id" in d
        assert "period" in d
        assert "total_runs" in d
        assert "success_rate" in d
        assert "overall_score" in d


# ============================================================
# Test Models
# ============================================================

class TestSLAModels:
    """Tests for SLA-related models."""
    
    def test_sla_config_defaults(self):
        """Test SLAConfig default values."""
        config = SLAConfig()
        assert config.enabled is False
        assert config.success_rate == 95.0
        assert config.schedule_tolerance == 300
        assert config.max_duration is None
        assert config.evaluation_period == "7d"
        assert config.alert_on_breach is True
        assert config.alert_threshold == 90.0
    
    def test_sla_config_custom(self):
        """Test SLAConfig with custom values."""
        config = SLAConfig(
            enabled=True,
            success_rate=99.0,
            schedule_tolerance=60,
            max_duration=3600,
            max_consecutive_failures=5,
        )
        assert config.enabled is True
        assert config.success_rate == 99.0
        assert config.schedule_tolerance == 60
        assert config.max_duration == 3600
        assert config.max_consecutive_failures == 5
    
    def test_sla_status_enum(self):
        """Test SLAStatus enum values."""
        assert SLAStatus.PASS.value == "pass"
        assert SLAStatus.FAIL.value == "fail"
        assert SLAStatus.PARTIAL.value == "partial"
        assert SLAStatus.NO_SLA.value == "no_sla"
    
    def test_error_budget_config(self):
        """Test ErrorBudgetConfig."""
        config = ErrorBudgetConfig()
        assert config.period == ErrorBudgetPeriod.WEEK
        assert config.max_failures == 3
        
        config = ErrorBudgetConfig(period=ErrorBudgetPeriod.MONTH, max_failures=10)
        assert config.period == ErrorBudgetPeriod.MONTH
        assert config.max_failures == 10
    
    def test_job_run_sla_fields(self):
        """Test JobRun SLA fields."""
        run = JobRun(
            job_id="test",
            started_at=datetime.now(),
            sla_snapshot_id=1,
            sla_status="pass",
            sla_details='{"success_check": true}',
        )
        assert run.sla_snapshot_id == 1
        assert run.sla_status == "pass"
        assert run.sla_details is not None


# ============================================================
# Test create_config_hash
# ============================================================

class TestConfigHash:
    """Tests for config hash functionality."""
    
    def test_same_config_same_hash(self, basic_job):
        """Test same config produces same hash."""
        hash1 = create_config_hash(basic_job)
        hash2 = create_config_hash(basic_job)
        assert hash1 == hash2
    
    def test_different_cmd_different_hash(self, basic_job):
        """Test different command produces different hash."""
        hash1 = create_config_hash(basic_job)
        basic_job.cmd = "echo different"
        hash2 = create_config_hash(basic_job)
        assert hash1 != hash2
    
    def test_different_schedule_different_hash(self, basic_job):
        """Test different schedule produces different hash."""
        hash1 = create_config_hash(basic_job)
        basic_job.schedule = "0 10 * * *"
        hash2 = create_config_hash(basic_job)
        assert hash1 != hash2
    
    def test_different_sla_different_hash(self, basic_job):
        """Test different SLA config produces different hash."""
        hash1 = create_config_hash(basic_job)
        basic_job.sla.success_rate = 99.0
        hash2 = create_config_hash(basic_job)
        assert hash1 != hash2
    
    def test_hash_length(self, basic_job):
        """Test hash has expected length."""
        hash_value = create_config_hash(basic_job)
        assert len(hash_value) == 16  # First 16 chars of SHA256


# ============================================================
# Test Edge Cases
# ============================================================

class TestEdgeCases:
    """Tests for edge cases."""
    
    def test_run_without_finished_at(self, basic_job):
        """Test checking run that hasn't finished."""
        run = JobRun(
            id=1,
            job_id="test",
            started_at=datetime.now(),
            # No finished_at
        )
        result = check_run_sla(run, basic_job)
        # Success check should be None since run hasn't finished
        assert result.success_check is None
    
    def test_run_without_duration(self, basic_job):
        """Test checking run without duration."""
        run = JobRun(
            id=1,
            job_id="test",
            started_at=datetime.now() - timedelta(minutes=5),
            finished_at=datetime.now(),
            exit_code=0,
            duration_seconds=None,
        )
        result = check_run_sla(run, basic_job)
        # Duration check should be None since no duration
        assert result.duration_check is None
    
    def test_job_without_schedule(self):
        """Test job without schedule (manual)."""
        job = JobConfig(
            name="Manual Job",
            cmd="echo test",
            type=JobType.MANUAL,
            sla=SLAConfig(enabled=True),
        )
        run = JobRun(
            id=1,
            job_id="test",
            started_at=datetime.now(),
            finished_at=datetime.now() + timedelta(minutes=5),
            exit_code=0,
            duration_seconds=300,
        )
        result = check_run_sla(run, job)
        # Should skip schedule adherence check
        assert result.on_time_check is None
    
    def test_metrics_with_single_run(self, basic_job):
        """Test metrics calculation with single run."""
        now = datetime.now()
        runs = [
            JobRun(
                id=1,
                job_id="test",
                started_at=now - timedelta(hours=1),
                finished_at=now - timedelta(hours=1) + timedelta(minutes=5),
                exit_code=0,
                duration_seconds=300,
                trigger="scheduled",
            )
        ]
        
        metrics = calculate_sla_metrics(
            "test", basic_job, runs,
            now - timedelta(days=1),
            now,
        )
        
        assert metrics.total_runs == 1
        assert metrics.success_rate == 100.0
        assert metrics.p50_duration == 300.0
        assert metrics.p95_duration == 300.0  # Same as p50 with single run


# ============================================================
# Test Integration
# ============================================================

class TestIntegration:
    """Integration tests."""
    
    def test_full_sla_workflow(self, basic_job):
        """Test full SLA calculation workflow."""
        now = datetime.now()
        
        # Create a week of runs
        runs = []
        for day in range(7):
            for hour in [9, 21]:  # Two runs per day
                started = now - timedelta(days=day, hours=now.hour - hour)
                # Vary success (85% success rate)
                exit_code = 0 if (day * 2 + (0 if hour == 9 else 1)) % 7 != 0 else 1
                runs.append(JobRun(
                    id=len(runs) + 1,
                    job_id="test-job",
                    started_at=started,
                    finished_at=started + timedelta(minutes=5),
                    exit_code=exit_code,
                    duration_seconds=300,
                    trigger="scheduled",
                ))
        
        metrics = calculate_sla_metrics(
            "test-job", basic_job, runs,
            now - timedelta(days=7),
            now,
        )
        
        # Should have calculated metrics
        assert metrics.total_runs == 14
        assert 0 <= metrics.success_rate <= 100
        assert 0 <= metrics.overall_score <= 100
        assert metrics.status in ("healthy", "warning", "critical", "no_data")


class TestTrendCalculation:
    """Tests for trend calculation."""
    
    def test_trend_improving(self, basic_job):
        """Test trend is improving when second half is better."""
        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        runs = []
        
        # First half: 50% success (2 fail, 2 pass)
        for i in range(4):
            runs.append(JobRun(
                id=i,
                job_id="test",
                started_at=base - timedelta(days=6-i),
                finished_at=base - timedelta(days=6-i) + timedelta(minutes=5),
                exit_code=1 if i < 2 else 0,
                duration_seconds=300,
            ))
        
        # Second half: 100% success (4 pass)
        for i in range(4):
            runs.append(JobRun(
                id=i+4,
                job_id="test",
                started_at=base - timedelta(days=2-i),
                finished_at=base - timedelta(days=2-i) + timedelta(minutes=5),
                exit_code=0,
                duration_seconds=300,
            ))
        
        metrics = calculate_sla_metrics(
            "test", basic_job, runs,
            base - timedelta(days=7),
            base,
        )
        
        assert metrics.trend == "improving"
    
    def test_trend_degrading(self, basic_job):
        """Test trend is degrading when second half is worse."""
        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        runs = []
        
        # First half: 100% success
        for i in range(4):
            runs.append(JobRun(
                id=i,
                job_id="test",
                started_at=base - timedelta(days=6-i),
                finished_at=base - timedelta(days=6-i) + timedelta(minutes=5),
                exit_code=0,
                duration_seconds=300,
            ))
        
        # Second half: 50% success
        for i in range(4):
            runs.append(JobRun(
                id=i+4,
                job_id="test",
                started_at=base - timedelta(days=2-i),
                finished_at=base - timedelta(days=2-i) + timedelta(minutes=5),
                exit_code=1 if i < 2 else 0,
                duration_seconds=300,
            ))
        
        metrics = calculate_sla_metrics(
            "test", basic_job, runs,
            base - timedelta(days=7),
            base,
        )
        
        assert metrics.trend == "degrading"
    
    def test_trend_stable(self, basic_job):
        """Test trend is stable when both halves similar."""
        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        runs = []
        
        # Both halves: 100% success
        for i in range(8):
            runs.append(JobRun(
                id=i,
                job_id="test",
                started_at=base - timedelta(days=7-i),
                finished_at=base - timedelta(days=7-i) + timedelta(minutes=5),
                exit_code=0,
                duration_seconds=300,
            ))
        
        metrics = calculate_sla_metrics(
            "test", basic_job, runs,
            base - timedelta(days=7),
            base,
        )
        
        assert metrics.trend == "stable"
    
    def test_trend_few_runs(self, basic_job):
        """Test trend is stable with few runs."""
        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        runs = [
            JobRun(
                id=0,
                job_id="test",
                started_at=base - timedelta(days=1),
                finished_at=base - timedelta(days=1) + timedelta(minutes=5),
                exit_code=0,
                duration_seconds=300,
            )
        ]
        
        metrics = calculate_sla_metrics(
            "test", basic_job, runs,
            base - timedelta(days=7),
            base,
        )
        
        assert metrics.trend == "stable"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
