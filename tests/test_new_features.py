"""Tests for the 6 new gap features.

1. Deduplication
2. Max Concurrent (Concurrency Control)
3. Priority Queues
4. Dead Letter Queue
5. Distributed Locks
6. Event Triggers
"""

import asyncio
import json
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, patch
import pytest

from procclaw.models import (
    JobConfig,
    JobType,
    RetryConfig,
    Priority,
    DedupConfig,
    ConcurrencyConfig,
    LockConfig,
    TriggerConfig,
    TriggerType,
)
from procclaw.db import Database


# ============================================================================
# Feature 1: Deduplication
# ============================================================================

class TestDeduplication:
    """Tests for deduplication feature."""

    def test_dedup_blocks_duplicate_within_window(self, tmp_path):
        """Same job should be blocked within dedup window."""
        from procclaw.core.dedup import DeduplicationManager
        
        db = Database(tmp_path / "dedup.db")
        manager = DeduplicationManager(db, default_window=60)
        
        # Record first execution
        manager.record("test-job", run_id=1)
        
        # Check for duplicate - should be blocked
        result = manager.check("test-job")
        assert result.is_duplicate is True
        assert result.original_run_id == 1
    
    def test_dedup_allows_after_window(self, tmp_path):
        """Same job should be allowed after window expires."""
        from procclaw.core.dedup import DeduplicationManager
        
        db = Database(tmp_path / "dedup.db")
        manager = DeduplicationManager(db, default_window=1)  # 1 second
        
        # Record execution
        manager.record("test-job", run_id=1)
        
        # Wait for window to expire
        time.sleep(1.1)
        
        # Should now be allowed
        result = manager.check("test-job")
        assert result.is_duplicate is False
    
    def test_idempotency_key_blocks_forever(self, tmp_path):
        """Idempotency key should block regardless of window."""
        from procclaw.core.dedup import DeduplicationManager
        
        db = Database(tmp_path / "dedup.db")
        manager = DeduplicationManager(db, default_window=1)
        
        # Record with idempotency key
        manager.record("test-job", run_id=1, idempotency_key="key-123")
        
        # Wait past window
        time.sleep(1.1)
        
        # Should still be blocked by idempotency key
        result = manager.check("test-job", idempotency_key="key-123")
        assert result.is_duplicate is True
        assert "Idempotency key" in result.reason
    
    def test_different_params_not_blocked(self, tmp_path):
        """Different params should not be blocked."""
        from procclaw.core.dedup import DeduplicationManager
        
        db = Database(tmp_path / "dedup.db")
        manager = DeduplicationManager(db, default_window=60)
        
        # Record with params
        manager.record("test-job", run_id=1, params={"key": "value1"})
        
        # Different params should be allowed
        result = manager.check("test-job", params={"key": "value2"})
        assert result.is_duplicate is False
    
    def test_clear_job_dedup(self, tmp_path):
        """Should be able to clear dedup for a job."""
        from procclaw.core.dedup import DeduplicationManager
        
        db = Database(tmp_path / "dedup.db")
        manager = DeduplicationManager(db, default_window=60)
        
        manager.record("test-job", run_id=1)
        
        # Clear
        count = manager.clear_job("test-job")
        assert count >= 1
        
        # Should now be allowed
        result = manager.check("test-job")
        assert result.is_duplicate is False


# ============================================================================
# Feature 2: Concurrency Control
# ============================================================================

class TestConcurrencyControl:
    """Tests for concurrency limiting."""

    def test_max_instances_enforced(self, tmp_path):
        """Max instances should be enforced."""
        from procclaw.core.concurrency import ConcurrencyLimiter
        
        db = Database(tmp_path / "conc.db")
        limiter = ConcurrencyLimiter(db, lambda x: 0)
        
        config = ConcurrencyConfig(max_instances=2)
        
        # First two should succeed
        assert limiter.can_start("job-1", config)[0] is True
        limiter.record_start("job-1")
        
        assert limiter.can_start("job-1", config)[0] is True
        limiter.record_start("job-1")
        
        # Third should fail
        can_start, reason = limiter.can_start("job-1", config)
        assert can_start is False
        assert "max instances" in reason.lower()
    
    def test_release_allows_new_start(self, tmp_path):
        """Releasing should allow new starts."""
        from procclaw.core.concurrency import ConcurrencyLimiter
        
        db = Database(tmp_path / "conc.db")
        limiter = ConcurrencyLimiter(db, lambda x: 0)
        
        config = ConcurrencyConfig(max_instances=1)
        
        # Start one
        limiter.record_start("job-1")
        
        # Can't start another
        assert limiter.can_start("job-1", config)[0] is False
        
        # Release
        limiter.record_stop("job-1")
        
        # Now can start
        assert limiter.can_start("job-1", config)[0] is True
    
    def test_global_max_enforced(self, tmp_path):
        """Global max should be enforced."""
        from procclaw.core.concurrency import ConcurrencyLimiter
        
        db = Database(tmp_path / "conc.db")
        limiter = ConcurrencyLimiter(db, lambda x: 0, global_max=3)
        
        config = ConcurrencyConfig(max_instances=10)
        
        # Start 3 different jobs
        for i in range(3):
            limiter.record_start(f"job-{i}")
        
        # Fourth should fail due to global max
        can_start, reason = limiter.can_start("job-3", config)
        assert can_start is False
        assert "global" in reason.lower()
    
    def test_queue_job(self, tmp_path):
        """Jobs should be queueable."""
        from procclaw.core.concurrency import ConcurrencyLimiter
        
        db = Database(tmp_path / "conc.db")
        limiter = ConcurrencyLimiter(db, lambda x: 0)
        
        # Queue a job
        queue_id = limiter.queue_job("test-job", priority=1)
        assert queue_id > 0
        
        # Check queue length
        assert limiter.get_queue_length() == 1
        
        # Dequeue
        entry = limiter.get_next_queued()
        assert entry is not None
        assert entry["job_id"] == "test-job"


# ============================================================================
# Feature 3: Priority Queues
# ============================================================================

class TestPriorityQueues:
    """Tests for priority queue ordering."""

    def test_priority_ordering(self):
        """Higher priority jobs should come first."""
        from procclaw.core.priority import PriorityQueue
        
        queue = PriorityQueue()
        
        # Add in reverse order
        queue.enqueue("low-job", priority=3)
        queue.enqueue("normal-job", priority=2)
        queue.enqueue("critical-job", priority=0)
        
        # Should come out in priority order
        assert queue.dequeue().job_id == "critical-job"
        assert queue.dequeue().job_id == "normal-job"
        assert queue.dequeue().job_id == "low-job"
    
    def test_fifo_within_priority(self):
        """Same priority should be FIFO."""
        from procclaw.core.priority import PriorityQueue
        
        queue = PriorityQueue()
        
        # Add same priority
        queue.enqueue("first", priority=2)
        time.sleep(0.01)  # Ensure different timestamps
        queue.enqueue("second", priority=2)
        time.sleep(0.01)
        queue.enqueue("third", priority=2)
        
        # Should be FIFO
        assert queue.dequeue().job_id == "first"
        assert queue.dequeue().job_id == "second"
        assert queue.dequeue().job_id == "third"
    
    def test_queue_stats(self):
        """Should provide accurate stats."""
        from procclaw.core.priority import PriorityQueue
        
        queue = PriorityQueue()
        
        queue.enqueue("c1", priority=0)
        queue.enqueue("c2", priority=0)
        queue.enqueue("h1", priority=1)
        queue.enqueue("n1", priority=2)
        queue.enqueue("l1", priority=3)
        
        stats = queue.get_stats()
        assert stats["total"] == 5
        assert stats["by_priority"][0] == 2
        assert stats["by_priority"][1] == 1
        assert stats["by_priority"][2] == 1
        assert stats["by_priority"][3] == 1
    
    def test_remove_job(self):
        """Should be able to remove all instances of a job."""
        from procclaw.core.priority import PriorityQueue
        
        queue = PriorityQueue()
        
        queue.enqueue("job-1", priority=2)
        queue.enqueue("job-2", priority=2)
        queue.enqueue("job-1", priority=2)  # Second instance
        
        removed = queue.remove("job-1")
        assert removed == 2
        assert queue.get_count() == 1
    
    def test_preemption_check(self):
        """Should detect when preemption is needed."""
        from procclaw.core.priority import PriorityQueue
        
        queue = PriorityQueue()
        
        # Empty queue - no preemption
        assert queue.should_preempt(current_priority=2) is False
        
        # Lower priority waiting - no preemption
        queue.enqueue("low", priority=3)
        assert queue.should_preempt(current_priority=2) is False
        
        # Higher priority waiting - preemption
        queue.enqueue("critical", priority=0)
        assert queue.should_preempt(current_priority=2) is True


# ============================================================================
# Feature 4: Dead Letter Queue
# ============================================================================

class TestDeadLetterQueue:
    """Tests for dead letter queue."""

    def test_add_to_dlq(self, tmp_path):
        """Failed jobs should be added to DLQ."""
        from procclaw.core.dlq import DeadLetterQueue
        
        db = Database(tmp_path / "dlq.db")
        dlq = DeadLetterQueue(db)
        
        job_config = JobConfig(name="test", cmd="echo test")
        
        entry = dlq.add(
            job_id="test-job",
            run_id=123,
            error="Max retries exceeded",
            attempts=5,
            job_config=job_config,
        )
        
        assert entry.id > 0
        assert entry.job_id == "test-job"
        assert entry.attempts == 5
    
    def test_list_dlq(self, tmp_path):
        """Should list DLQ entries."""
        from procclaw.core.dlq import DeadLetterQueue
        
        db = Database(tmp_path / "dlq.db")
        dlq = DeadLetterQueue(db)
        
        # Add entries
        dlq.add("job-1", 1, "Error 1", 3)
        dlq.add("job-2", 2, "Error 2", 5)
        
        entries = dlq.list()
        assert len(entries) == 2
    
    def test_reinject_from_dlq(self, tmp_path):
        """Should be able to reinject from DLQ."""
        from procclaw.core.dlq import DeadLetterQueue
        
        db = Database(tmp_path / "dlq.db")
        dlq = DeadLetterQueue(db)
        
        entry = dlq.add("test-job", 123, "Error", 5)
        
        # Reinject
        def mock_reinject(job_id, params):
            return 456  # New run ID
        
        new_run_id = dlq.reinject(entry.id, mock_reinject)
        assert new_run_id == 456
        
        # Should be marked as reinjected
        updated = dlq.get(entry.id)
        assert updated.is_reinjected is True
        assert updated.reinjected_run_id == 456
    
    def test_dlq_stats(self, tmp_path):
        """Should provide accurate stats."""
        from procclaw.core.dlq import DeadLetterQueue
        
        db = Database(tmp_path / "dlq.db")
        dlq = DeadLetterQueue(db)
        
        dlq.add("job-1", 1, "Error 1", 3)
        dlq.add("job-1", 2, "Error 2", 3)
        dlq.add("job-2", 3, "Error 3", 5)
        
        stats = dlq.get_stats()
        assert stats.total_entries == 3
        assert stats.pending_entries == 3
        assert stats.by_job["job-1"] == 2
        assert stats.by_job["job-2"] == 1
    
    def test_delete_dlq_entry(self, tmp_path):
        """Should be able to delete DLQ entries."""
        from procclaw.core.dlq import DeadLetterQueue
        
        db = Database(tmp_path / "dlq.db")
        dlq = DeadLetterQueue(db)
        
        entry = dlq.add("test-job", 123, "Error", 5)
        
        result = dlq.delete(entry.id)
        assert result is True
        
        assert dlq.get(entry.id) is None


# ============================================================================
# Feature 5: Distributed Locks
# ============================================================================

class TestDistributedLocks:
    """Tests for distributed locking."""

    def test_acquire_and_release(self, tmp_path):
        """Should acquire and release locks."""
        from procclaw.core.locks import SQLiteLockProvider, LockManager
        
        db = Database(tmp_path / "locks.db")
        provider = SQLiteLockProvider(db)
        manager = LockManager(provider)
        
        # Acquire
        acquired = asyncio.run(manager.acquire("test-job"))
        assert acquired is True
        
        # Is locked
        is_locked = asyncio.run(manager.is_locked("test-job"))
        assert is_locked is True
        
        # Release
        released = asyncio.run(manager.release("test-job"))
        assert released is True
        
        # No longer locked
        is_locked = asyncio.run(manager.is_locked("test-job"))
        assert is_locked is False
    
    def test_lock_prevents_duplicate(self, tmp_path):
        """Lock should prevent duplicate acquisition."""
        from procclaw.core.locks import SQLiteLockProvider, LockManager
        
        db = Database(tmp_path / "locks.db")
        provider = SQLiteLockProvider(db)
        
        manager1 = LockManager(provider)
        manager2 = LockManager(provider)
        
        # First manager acquires
        asyncio.run(manager1.acquire("test-job"))
        
        # Second manager should fail (no wait)
        acquired = asyncio.run(manager2.acquire("test-job", wait=False))
        assert acquired is False
    
    def test_file_lock_provider(self, tmp_path):
        """File-based locks should work."""
        from procclaw.core.locks import FileLockProvider, LockManager
        
        lock_dir = tmp_path / "locks"
        provider = FileLockProvider(lock_dir)
        manager = LockManager(provider)
        
        # Acquire
        acquired = asyncio.run(manager.acquire("test-job", timeout_seconds=60))
        assert acquired is True
        
        # Lock file should exist
        lock_file = lock_dir / "test-job.lock"
        assert lock_file.exists()
        
        # Release
        released = asyncio.run(manager.release("test-job"))
        assert released is True
    
    def test_lock_context_manager(self, tmp_path):
        """Lock context manager should work."""
        from procclaw.core.locks import SQLiteLockProvider, LockManager
        
        db = Database(tmp_path / "locks.db")
        provider = SQLiteLockProvider(db)
        manager = LockManager(provider)
        
        async def use_lock():
            async with manager.lock("test-job"):
                # Lock is held here
                assert await manager.is_locked("test-job")
            # Lock is released here
            assert not await manager.is_locked("test-job")
        
        asyncio.run(use_lock())
    
    def test_lock_expiry(self, tmp_path):
        """Expired locks should be acquirable."""
        from procclaw.core.locks import FileLockProvider, LockManager
        
        lock_dir = tmp_path / "locks"
        provider = FileLockProvider(lock_dir)
        
        manager1 = LockManager(provider)
        manager2 = LockManager(provider)
        
        # Acquire with 1 second timeout
        asyncio.run(manager1.acquire("test-job", timeout_seconds=1))
        
        # Wait for expiry
        time.sleep(1.5)
        
        # Second manager should be able to acquire
        acquired = asyncio.run(manager2.acquire("test-job", wait=False))
        assert acquired is True


# ============================================================================
# Feature 6: Event Triggers
# ============================================================================

class TestEventTriggers:
    """Tests for event triggers."""

    def test_webhook_validation(self):
        """Webhook validation should work."""
        from procclaw.core.triggers import WebhookValidator, WebhookRequest
        
        config = TriggerConfig(
            enabled=True,
            type=TriggerType.WEBHOOK,
            auth_token="secret123",
        )
        
        validator = WebhookValidator({"test-job": config})
        
        # Valid request
        request = WebhookRequest(
            job_id="test-job",
            payload={"key": "value"},
            idempotency_key=None,
            auth_token="secret123",
            source_ip="127.0.0.1",
        )
        is_valid, error = validator.validate(request)
        assert is_valid is True
        
        # Invalid token
        request.auth_token = "wrong"
        is_valid, error = validator.validate(request)
        assert is_valid is False
        assert "token" in error.lower()
    
    def test_trigger_manager_webhook(self):
        """Trigger manager should handle webhooks."""
        from procclaw.core.triggers import TriggerManager, TriggerEvent
        
        triggered_events = []
        
        def on_trigger(event: TriggerEvent) -> bool:
            triggered_events.append(event)
            return True
        
        manager = TriggerManager(on_trigger)
        
        config = TriggerConfig(
            enabled=True,
            type=TriggerType.WEBHOOK,
        )
        manager.register_job("test-job", config)
        
        # Trigger
        success, msg = manager.handle_webhook(
            job_id="test-job",
            payload={"data": "test"},
        )
        
        assert success is True
        assert len(triggered_events) == 1
        assert triggered_events[0].job_id == "test-job"
        assert triggered_events[0].trigger_type == "webhook"
    
    def test_disabled_trigger_rejected(self):
        """Disabled triggers should be rejected."""
        from procclaw.core.triggers import TriggerManager
        
        manager = TriggerManager(lambda e: True)
        
        config = TriggerConfig(enabled=False)
        manager.register_job("test-job", config)
        
        success, msg = manager.handle_webhook("test-job")
        assert success is False
        assert "not enabled" in msg.lower()
    
    def test_file_trigger_watcher(self, tmp_path):
        """File trigger watcher should detect file creation."""
        from procclaw.core.triggers import FileTriggerWatcher
        
        triggered = []
        
        def on_trigger(job_id, path):
            triggered.append((job_id, path))
        
        watcher = FileTriggerWatcher()
        watcher.add_watch(
            job_id="test-job",
            watch_path=str(tmp_path),
            pattern="*.trigger",
            delete_after=False,
            on_trigger=on_trigger,
        )
        watcher.start()
        
        try:
            # Create a trigger file
            trigger_file = tmp_path / "test.trigger"
            trigger_file.write_text("trigger content")
            
            # Wait for detection (watchdog is async)
            time.sleep(0.5)
            
            # Should have triggered
            assert len(triggered) >= 1
            assert triggered[0][0] == "test-job"
        finally:
            watcher.stop()


# ============================================================================
# Integration Tests
# ============================================================================

class TestFeatureIntegration:
    """Integration tests for all new features."""

    def test_dedup_with_concurrency(self, tmp_path):
        """Dedup and concurrency should work together."""
        from procclaw.core.dedup import DeduplicationManager
        from procclaw.core.concurrency import ConcurrencyLimiter
        
        db = Database(tmp_path / "integration.db")
        
        dedup = DeduplicationManager(db, default_window=60)
        limiter = ConcurrencyLimiter(db, lambda x: 0)
        
        config = ConcurrencyConfig(max_instances=1)
        
        # First execution
        if not dedup.check("job-1").is_duplicate:
            if limiter.can_start("job-1", config)[0]:
                limiter.record_start("job-1")
                dedup.record("job-1", run_id=1)
        
        # Second should be blocked by dedup
        assert dedup.check("job-1").is_duplicate is True
        
        # Even if we could start, dedup blocks
        limiter.record_stop("job-1")
        assert limiter.can_start("job-1", config)[0] is True
        assert dedup.check("job-1").is_duplicate is True
    
    def test_priority_with_dlq(self, tmp_path):
        """Priority queue failures should go to DLQ."""
        from procclaw.core.priority import PriorityQueue
        from procclaw.core.dlq import DeadLetterQueue
        
        db = Database(tmp_path / "integration.db")
        
        queue = PriorityQueue()
        dlq = DeadLetterQueue(db)
        
        # Simulate job going through queue and failing
        queue.enqueue("failing-job", priority=0)
        job = queue.dequeue()
        
        # Job fails, add to DLQ
        dlq.add(
            job_id=job.job_id,
            run_id=1,
            error="Test failure",
            attempts=5,
        )
        
        # DLQ should have the entry
        entries = dlq.list()
        assert len(entries) == 1
        assert entries[0].job_id == "failing-job"


# Run with: pytest tests/test_new_features.py -v
