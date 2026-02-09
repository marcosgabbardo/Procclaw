"""Tests for enterprise features integration."""

from __future__ import annotations

import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from procclaw.models import (
    ConcurrencyConfig,
    Priority,
    TriggerConfig,
    TriggerType,
)
from procclaw.db import Database
from procclaw.core.dedup import ExecutionFingerprint
from procclaw.core.priority import PriorityQueue
from procclaw.core.dlq import DLQEntry


class TestExecutionFingerprint:
    """Tests for execution fingerprint generation."""

    def test_fingerprint_creation(self):
        """Test execution fingerprint generation."""
        fp1 = ExecutionFingerprint.create("job1")
        fp2 = ExecutionFingerprint.create("job1")
        fp3 = ExecutionFingerprint.create("job2")

        # Same job, same fingerprint
        assert fp1.fingerprint == fp2.fingerprint
        # Different job, different fingerprint
        assert fp1.fingerprint != fp3.fingerprint

    def test_fingerprint_with_params(self):
        """Test fingerprint includes params when specified."""
        fp1 = ExecutionFingerprint.create("job1", params={"key": "value1"})
        fp2 = ExecutionFingerprint.create("job1", params={"key": "value2"})
        fp3 = ExecutionFingerprint.create("job1", params={"key": "value1"})

        assert fp1.fingerprint != fp2.fingerprint
        assert fp1.fingerprint == fp3.fingerprint

    def test_idempotency_key_override(self):
        """Test idempotency key takes precedence."""
        fp1 = ExecutionFingerprint.create("job1", idempotency_key="unique-key-1")
        fp2 = ExecutionFingerprint.create("job1", idempotency_key="unique-key-1")
        fp3 = ExecutionFingerprint.create("job1", idempotency_key="unique-key-2")

        assert fp1.idempotency_key == fp2.idempotency_key
        assert fp1.idempotency_key != fp3.idempotency_key


class TestPriorityQueue:
    """Tests for priority queue."""

    def test_priority_ordering(self):
        """Test jobs are dequeued in priority order."""
        queue = PriorityQueue()

        queue.enqueue("low-job", Priority.LOW)
        queue.enqueue("critical-job", Priority.CRITICAL)
        queue.enqueue("normal-job", Priority.NORMAL)
        queue.enqueue("high-job", Priority.HIGH)

        # Should come out in priority order
        assert queue.dequeue().job_id == "critical-job"
        assert queue.dequeue().job_id == "high-job"
        assert queue.dequeue().job_id == "normal-job"
        assert queue.dequeue().job_id == "low-job"

    def test_fifo_within_priority(self):
        """Test FIFO ordering within same priority."""
        queue = PriorityQueue()

        queue.enqueue("job1", Priority.NORMAL)
        time.sleep(0.01)  # Ensure different timestamps
        queue.enqueue("job2", Priority.NORMAL)
        time.sleep(0.01)
        queue.enqueue("job3", Priority.NORMAL)

        assert queue.dequeue().job_id == "job1"
        assert queue.dequeue().job_id == "job2"
        assert queue.dequeue().job_id == "job3"

    def test_queue_stats(self):
        """Test queue statistics."""
        queue = PriorityQueue()

        queue.enqueue("job1", Priority.CRITICAL)
        queue.enqueue("job2", Priority.HIGH)
        queue.enqueue("job3", Priority.NORMAL)
        queue.enqueue("job4", Priority.NORMAL)

        stats = queue.get_stats()
        assert stats["total"] == 4
        assert stats["by_priority"][Priority.CRITICAL.value] == 1
        assert stats["by_priority"][Priority.HIGH.value] == 1
        assert stats["by_priority"][Priority.NORMAL.value] == 2

    def test_empty_queue(self):
        """Test behavior of empty queue."""
        queue = PriorityQueue()

        assert queue.dequeue() is None
        
        queue.enqueue("job1", Priority.CRITICAL)
        assert queue.dequeue().job_id == "job1"
        assert queue.dequeue() is None

    def test_with_params_and_trigger(self):
        """Test enqueue with params and trigger."""
        queue = PriorityQueue()

        queue.enqueue(
            "job1",
            Priority.HIGH,
            trigger="webhook",
            params={"key": "value"},
            idempotency_key="key123",
        )

        job = queue.dequeue()
        assert job.job_id == "job1"
        assert job.trigger == "webhook"
        assert job.params == {"key": "value"}
        assert job.idempotency_key == "key123"


class TestDLQEntry:
    """Tests for DLQ entry model."""

    def test_is_reinjected_false(self):
        """Test is_reinjected when not reinjected."""
        entry = DLQEntry(
            id=1,
            job_id="job1",
            original_run_id=100,
            failed_at=datetime.now(),
            attempts=5,
            last_error="Error",
            job_config=None,
            trigger_params=None,
        )
        
        assert not entry.is_reinjected

    def test_is_reinjected_true(self):
        """Test is_reinjected when reinjected."""
        entry = DLQEntry(
            id=1,
            job_id="job1",
            original_run_id=100,
            failed_at=datetime.now(),
            attempts=5,
            last_error="Error",
            job_config=None,
            trigger_params=None,
            reinjected_at=datetime.now(),
            reinjected_run_id=200,
        )
        
        assert entry.is_reinjected


class TestConcurrencyConfig:
    """Tests for concurrency configuration."""

    def test_default_config(self):
        """Test default concurrency config."""
        config = ConcurrencyConfig()
        
        assert config.max_instances == 1
        assert config.queue_excess == True
        assert config.queue_timeout == 300

    def test_custom_config(self):
        """Test custom concurrency config."""
        config = ConcurrencyConfig(
            max_instances=5,
            queue_excess=False,
            queue_timeout=60,
        )
        
        assert config.max_instances == 5
        assert config.queue_excess == False
        assert config.queue_timeout == 60


class TestTriggerConfig:
    """Tests for trigger configuration."""

    def test_webhook_trigger(self):
        """Test webhook trigger config."""
        config = TriggerConfig(
            enabled=True,
            type=TriggerType.WEBHOOK,
            auth_token="secret123",
        )
        
        assert config.enabled
        assert config.type == TriggerType.WEBHOOK
        assert config.auth_token == "secret123"

    def test_file_trigger(self):
        """Test file trigger config."""
        config = TriggerConfig(
            enabled=True,
            type=TriggerType.FILE,
            watch_path="/var/data/inbox",
            pattern="*.json",
            delete_after=True,
        )
        
        assert config.type == TriggerType.FILE
        assert config.watch_path == "/var/data/inbox"
        assert config.pattern == "*.json"
        assert config.delete_after


class TestAPIEndpoints:
    """Tests for new API endpoints."""

    @pytest.fixture
    def test_client(self):
        """Create test client."""
        from fastapi.testclient import TestClient
        from procclaw.api.server import create_app, set_supervisor
        
        # Create mock supervisor
        supervisor = MagicMock()
        supervisor.config.api.auth.enabled = False
        supervisor.jobs.get_job.return_value = None
        
        set_supervisor(supervisor)
        app = create_app()
        
        return TestClient(app), supervisor

    def test_dlq_list_endpoint(self, test_client):
        """Test DLQ list endpoint."""
        client, supervisor = test_client
        
        supervisor.get_dlq_entries.return_value = [
            DLQEntry(
                id=1,
                job_id="job1",
                original_run_id=100,
                failed_at=datetime.now(),
                attempts=5,
                last_error="Error",
                job_config=None,
                trigger_params=None,
            )
        ]

        response = client.get("/api/v1/dlq")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["entries"][0]["job_id"] == "job1"

    def test_dlq_stats_endpoint(self, test_client):
        """Test DLQ stats endpoint."""
        client, supervisor = test_client
        
        supervisor.get_dlq_stats.return_value = {
            "total_entries": 10,
            "pending_entries": 7,
            "reinjected_entries": 3,
            "by_job": {"job1": 5, "job2": 5},
        }

        response = client.get("/api/v1/dlq/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total_entries"] == 10

    def test_dlq_reinject_endpoint(self, test_client):
        """Test DLQ reinject endpoint."""
        client, supervisor = test_client
        supervisor.reinject_dlq_entry.return_value = True

        response = client.post("/api/v1/dlq/1/reinject")
        assert response.status_code == 200
        assert response.json()["success"]

    def test_dlq_reinject_failure(self, test_client):
        """Test DLQ reinject failure."""
        client, supervisor = test_client
        supervisor.reinject_dlq_entry.return_value = False

        response = client.post("/api/v1/dlq/999/reinject")
        assert response.status_code == 500

    def test_dlq_purge_endpoint(self, test_client):
        """Test DLQ purge endpoint."""
        client, supervisor = test_client
        supervisor.purge_dlq.return_value = 5

        response = client.delete("/api/v1/dlq?older_than_days=7")
        assert response.status_code == 200
        data = response.json()
        assert data["purged"] == 5

    def test_concurrency_endpoint(self, test_client):
        """Test concurrency stats endpoint."""
        client, supervisor = test_client
        
        mock_job = MagicMock()
        supervisor.jobs.get_job.return_value = mock_job
        supervisor.get_concurrency_stats.return_value = {
            "job_id": "job1",
            "running_count": 2,
            "max_instances": 5,
            "queued_count": 1,
        }

        response = client.get("/api/v1/jobs/job1/concurrency")
        assert response.status_code == 200
        data = response.json()
        assert data["running_count"] == 2
        assert data["max_instances"] == 5
        assert data["queued_count"] == 1

    def test_concurrency_endpoint_not_found(self, test_client):
        """Test concurrency endpoint for non-existent job."""
        client, supervisor = test_client
        supervisor.jobs.get_job.return_value = None

        response = client.get("/api/v1/jobs/nonexistent/concurrency")
        assert response.status_code == 404

    def test_webhook_trigger_endpoint(self, test_client):
        """Test webhook trigger endpoint."""
        client, supervisor = test_client
        
        mock_job = MagicMock()
        mock_job.trigger.enabled = True
        mock_job.trigger.type.value = "webhook"
        supervisor.jobs.get_job.return_value = mock_job
        supervisor.trigger_job_webhook.return_value = True
        supervisor.db.get_state.return_value = MagicMock(pid=12345)

        response = client.post(
            "/api/v1/trigger/test-job",
            json={"key": "value"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"]
        assert data["job_id"] == "test-job"

    def test_webhook_trigger_not_enabled(self, test_client):
        """Test webhook trigger when not enabled."""
        client, supervisor = test_client
        
        mock_job = MagicMock()
        mock_job.trigger.enabled = False
        supervisor.jobs.get_job.return_value = mock_job

        response = client.post("/api/v1/trigger/test-job")
        assert response.status_code == 400


class TestPriorityModel:
    """Tests for Priority enum."""

    def test_priority_ordering(self):
        """Test priority values are ordered correctly."""
        assert Priority.CRITICAL.value < Priority.HIGH.value
        assert Priority.HIGH.value < Priority.NORMAL.value
        assert Priority.NORMAL.value < Priority.LOW.value

    def test_priority_values(self):
        """Test priority enum values."""
        assert Priority.CRITICAL.value == 0
        assert Priority.HIGH.value == 1
        assert Priority.NORMAL.value == 2
        assert Priority.LOW.value == 3


class TestIntegrationScenarios:
    """Tests for common integration scenarios."""

    def test_priority_queue_with_many_jobs(self):
        """Test priority queue handles many jobs correctly."""
        queue = PriorityQueue()

        # Add 100 jobs at various priorities
        for i in range(25):
            queue.enqueue(f"low-{i}", Priority.LOW)
            queue.enqueue(f"normal-{i}", Priority.NORMAL)
            queue.enqueue(f"high-{i}", Priority.HIGH)
            queue.enqueue(f"critical-{i}", Priority.CRITICAL)

        stats = queue.get_stats()
        assert stats["total"] == 100
        assert stats["by_priority"][Priority.CRITICAL.value] == 25
        assert stats["by_priority"][Priority.LOW.value] == 25

        # All critical should come out first
        for i in range(25):
            job = queue.dequeue()
            assert job.priority == Priority.CRITICAL.value

        # Then high
        for i in range(25):
            job = queue.dequeue()
            assert job.priority == Priority.HIGH.value

    def test_fingerprint_deterministic(self):
        """Test fingerprints are deterministic."""
        # Same inputs should always produce same fingerprint
        for _ in range(10):
            fp = ExecutionFingerprint.create(
                "job1",
                params={"a": 1, "b": 2},
            )
            assert fp.fingerprint == ExecutionFingerprint.create(
                "job1",
                params={"a": 1, "b": 2},
            ).fingerprint
