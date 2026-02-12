"""Tests for Self-Healing v2 API endpoints."""

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from procclaw.api.server import create_app, set_supervisor
from procclaw.db import Database


class MockJobsConfig:
    """Mock jobs config."""
    
    def __init__(self):
        self.jobs = {}
    
    def get_job(self, job_id: str):
        return self.jobs.get(job_id)


class MockConfig:
    """Mock config."""
    
    class Api:
        class Auth:
            enabled = False
            token = None
        auth = Auth()
    
    api = Api()


class MockHealingEngine:
    """Mock healing engine for API tests."""
    
    def __init__(self, db: Database):
        self.db = db
        self._running_reviews = {}
    
    def is_review_running(self, job_id: str) -> bool:
        return job_id in self._running_reviews
    
    def get_review_status(self, job_id: str):
        review_id = self._running_reviews.get(job_id)
        if review_id:
            return self.db.get_healing_review(review_id)
        return None
    
    async def run_review(self, job_id: str, job_config=None):
        review_id = self.db.create_healing_review(job_id)
        self.db.update_healing_review(review_id, status="completed")
        return review_id
    
    async def apply_approved_suggestion(self, suggestion_id: int):
        suggestion = self.db.get_healing_suggestion(suggestion_id)
        if not suggestion:
            return {"success": False, "error": "Suggestion not found"}
        if suggestion["status"] != "approved":
            return {"success": False, "error": "Not approved"}
        self.db.update_healing_suggestion(suggestion_id, status="applied")
        return {"success": True, "message": "Applied", "action_id": None}


class MockSupervisor:
    """Mock supervisor for API tests."""
    
    def __init__(self, db: Database):
        self.db = db
        self.jobs = MockJobsConfig()
        self.config = MockConfig()
        self._self_healer = MagicMock()
        self._healing_engine = MockHealingEngine(db)
    
    def is_job_running(self, job_id: str) -> bool:
        """Mock: no jobs are running."""
        return False


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
def client(temp_db):
    """Create a test client with mock supervisor."""
    supervisor = MockSupervisor(temp_db)
    supervisor.jobs.jobs["test-job"] = MagicMock()
    set_supervisor(supervisor)
    app = create_app()
    return TestClient(app)


class TestHealingReviewsAPI:
    """Tests for healing reviews API."""
    
    def test_list_reviews_empty(self, client):
        """Test listing reviews when empty."""
        response = client.get("/api/v1/healing/reviews")
        assert response.status_code == 200
        data = response.json()
        assert data["reviews"] == []
        assert data["total"] == 0
    
    def test_list_reviews(self, client, temp_db):
        """Test listing reviews."""
        # Create some reviews
        temp_db.create_healing_review("job-1")
        temp_db.create_healing_review("job-2")
        
        response = client.get("/api/v1/healing/reviews")
        assert response.status_code == 200
        data = response.json()
        assert len(data["reviews"]) == 2
        assert data["total"] == 2
    
    def test_list_reviews_filter_by_job(self, client, temp_db):
        """Test filtering reviews by job_id."""
        temp_db.create_healing_review("job-1")
        temp_db.create_healing_review("job-2")
        temp_db.create_healing_review("job-1")
        
        response = client.get("/api/v1/healing/reviews?job_id=job-1")
        assert response.status_code == 200
        data = response.json()
        assert len(data["reviews"]) == 2
        assert all(r["job_id"] == "job-1" for r in data["reviews"])
    
    def test_list_reviews_filter_by_status(self, client, temp_db):
        """Test filtering reviews by status."""
        review_id = temp_db.create_healing_review("job-1")
        temp_db.update_healing_review(review_id, status="completed")
        temp_db.create_healing_review("job-2")
        
        response = client.get("/api/v1/healing/reviews?status=running")
        assert response.status_code == 200
        data = response.json()
        assert len(data["reviews"]) == 1
        assert data["reviews"][0]["status"] == "running"
    
    def test_get_review(self, client, temp_db):
        """Test getting a single review."""
        review_id = temp_db.create_healing_review("test-job")
        
        response = client.get(f"/api/v1/healing/reviews/{review_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == review_id
        assert data["job_id"] == "test-job"
        assert data["status"] == "running"
    
    def test_get_review_not_found(self, client):
        """Test getting non-existent review."""
        response = client.get("/api/v1/healing/reviews/9999")
        assert response.status_code == 404


class TestHealingSuggestionsAPI:
    """Tests for healing suggestions API."""
    
    def test_list_suggestions_empty(self, client):
        """Test listing suggestions when empty."""
        response = client.get("/api/v1/healing/suggestions")
        assert response.status_code == 200
        data = response.json()
        assert data["suggestions"] == []
        assert data["total"] == 0
        assert data["pending_count"] == 0
    
    def test_list_suggestions(self, client, temp_db):
        """Test listing suggestions."""
        review_id = temp_db.create_healing_review("test-job")
        temp_db.create_healing_suggestion(
            review_id, "test-job", "prompt", "high", "Title 1", "Desc 1"
        )
        temp_db.create_healing_suggestion(
            review_id, "test-job", "config", "medium", "Title 2", "Desc 2"
        )
        
        response = client.get("/api/v1/healing/suggestions")
        assert response.status_code == 200
        data = response.json()
        assert len(data["suggestions"]) == 2
        assert data["pending_count"] == 2
    
    def test_list_suggestions_filter_by_category(self, client, temp_db):
        """Test filtering suggestions by category."""
        review_id = temp_db.create_healing_review("test-job")
        temp_db.create_healing_suggestion(
            review_id, "test-job", "prompt", "high", "Title 1", "Desc 1"
        )
        temp_db.create_healing_suggestion(
            review_id, "test-job", "config", "medium", "Title 2", "Desc 2"
        )
        
        response = client.get("/api/v1/healing/suggestions?category=prompt")
        assert response.status_code == 200
        data = response.json()
        assert len(data["suggestions"]) == 1
        assert data["suggestions"][0]["category"] == "prompt"
    
    def test_get_suggestion(self, client, temp_db):
        """Test getting a single suggestion."""
        review_id = temp_db.create_healing_review("test-job")
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "script", "high", "Test Title", "Test Description",
            affected_files=["test.sh"]
        )
        
        response = client.get(f"/api/v1/healing/suggestions/{suggestion_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == suggestion_id
        assert data["category"] == "script"
        assert data["severity"] == "high"
        assert "test.sh" in data["affected_files"]
    
    def test_approve_suggestion(self, client, temp_db):
        """Test approving a suggestion."""
        review_id = temp_db.create_healing_review("test-job")
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "config", "medium", "Title", "Desc"
        )
        
        response = client.post(
            f"/api/v1/healing/suggestions/{suggestion_id}/approve",
            json={"reviewed_by": "human"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        
        # Verify status changed
        suggestion = temp_db.get_healing_suggestion(suggestion_id)
        assert suggestion["status"] == "approved"
        assert suggestion["reviewed_by"] == "human"
    
    def test_approve_non_pending_fails(self, client, temp_db):
        """Test approving non-pending suggestion fails."""
        review_id = temp_db.create_healing_review("test-job")
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "config", "medium", "Title", "Desc"
        )
        temp_db.update_healing_suggestion(suggestion_id, status="applied")
        
        response = client.post(
            f"/api/v1/healing/suggestions/{suggestion_id}/approve",
            json={}
        )
        assert response.status_code == 400
        assert "not pending" in response.json()["detail"]
    
    def test_reject_suggestion(self, client, temp_db):
        """Test rejecting a suggestion."""
        review_id = temp_db.create_healing_review("test-job")
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "prompt", "low", "Title", "Desc"
        )
        
        response = client.post(
            f"/api/v1/healing/suggestions/{suggestion_id}/reject",
            json={"reason": "Not applicable", "reviewed_by": "human"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        
        # Verify status changed
        suggestion = temp_db.get_healing_suggestion(suggestion_id)
        assert suggestion["status"] == "rejected"
        assert suggestion["rejection_reason"] == "Not applicable"


class TestHealingActionsAPI:
    """Tests for healing actions API."""
    
    def test_list_actions_empty(self, client):
        """Test listing actions when empty."""
        response = client.get("/api/v1/healing/actions")
        assert response.status_code == 200
        data = response.json()
        assert data["actions"] == []
        assert data["total"] == 0
    
    def test_list_actions(self, client, temp_db):
        """Test listing actions."""
        review_id = temp_db.create_healing_review("test-job")
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "script", "medium", "Title", "Desc"
        )
        temp_db.create_healing_action(
            suggestion_id, "test-job", "edit_script",
            file_path="test.sh", original_content="old", new_content="new"
        )
        
        response = client.get("/api/v1/healing/actions")
        assert response.status_code == 200
        data = response.json()
        assert len(data["actions"]) == 1
        assert data["actions"][0]["action_type"] == "edit_script"
        assert data["actions"][0]["can_rollback"] is True
    
    def test_get_action(self, client, temp_db):
        """Test getting a single action."""
        review_id = temp_db.create_healing_review("test-job")
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "config", "high", "Title", "Desc"
        )
        action_id = temp_db.create_healing_action(
            suggestion_id, "test-job", "edit_config",
            file_path="config.yaml",
            original_content="old: value",
            new_content="new: value",
            execution_duration_ms=500
        )
        
        response = client.get(f"/api/v1/healing/actions/{action_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == action_id
        assert data["action_type"] == "edit_config"
        assert data["original_content"] == "old: value"
        assert data["new_content"] == "new: value"
        assert data["execution_duration_ms"] == 500
    
    def test_rollback_action(self, client, temp_db):
        """Test rolling back an action."""
        review_id = temp_db.create_healing_review("test-job")
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "script", "medium", "Title", "Desc"
        )
        
        # Create a temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write("new content")
            file_path = f.name
        
        try:
            action_id = temp_db.create_healing_action(
                suggestion_id, "test-job", "edit_script",
                file_path=file_path,
                original_content="original content",
                new_content="new content"
            )
            
            response = client.post(f"/api/v1/healing/actions/{action_id}/rollback")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            
            # Verify file was restored
            assert Path(file_path).read_text() == "original content"
            
            # Verify action status
            action = temp_db.get_healing_action(action_id)
            assert action["status"] == "rolled_back"
            assert action["rolled_back_at"] is not None
        finally:
            Path(file_path).unlink(missing_ok=True)
    
    def test_rollback_non_rollbackable_fails(self, client, temp_db):
        """Test rolling back non-rollbackable action fails."""
        review_id = temp_db.create_healing_review("test-job")
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "script", "medium", "Title", "Desc"
        )
        action_id = temp_db.create_healing_action(
            suggestion_id, "test-job", "run_command",
            command_executed="echo hello"
        )
        
        response = client.post(f"/api/v1/healing/actions/{action_id}/rollback")
        assert response.status_code == 400
        assert "cannot be rolled back" in response.json()["detail"]


class TestJobHealingSummaryAPI:
    """Tests for job healing summary API."""
    
    def test_get_job_healing_summary_empty(self, client):
        """Test getting summary when no reviews exist."""
        response = client.get("/api/v1/jobs/test-job/healing/summary")
        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "test-job"
        assert data["last_review"] is None
        assert data["pending_suggestions"] == 0
        assert data["rollbackable_actions_count"] == 0
    
    def test_get_job_healing_summary(self, client, temp_db):
        """Test getting summary with data."""
        review_id = temp_db.create_healing_review("test-job")
        temp_db.update_healing_review(review_id, suggestions_count=3)
        
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "prompt", "high", "Title", "Desc"
        )
        temp_db.create_healing_action(
            suggestion_id, "test-job", "edit_prompt",
            file_path="test.md", original_content="old", new_content="new"
        )
        
        response = client.get("/api/v1/jobs/test-job/healing/summary")
        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "test-job"
        assert data["last_review"] is not None
        assert data["last_review"]["id"] == review_id
        assert data["pending_suggestions"] == 1
        assert data["rollbackable_actions_count"] == 1


class TestTriggerReviewAPI:
    """Tests for trigger review API."""
    
    def test_trigger_review(self, client, temp_db):
        """Test triggering a review."""
        response = client.post(
            "/api/v1/healing/trigger",
            json={"job_id": "test-job"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["job_id"] == "test-job"
        assert data["status"] == "pending"  # Review runs async
    
    def test_trigger_review_job_not_found(self, client):
        """Test triggering review for non-existent job."""
        response = client.post(
            "/api/v1/healing/trigger",
            json={"job_id": "non-existent-job"}
        )
        assert response.status_code == 404


class TestApplySuggestionAPI:
    """Tests for apply suggestion API."""
    
    def test_apply_approved_suggestion(self, client, temp_db):
        """Test applying an approved suggestion."""
        review_id = temp_db.create_healing_review("test-job")
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "config", "low", "Title", "Desc"
        )
        # Approve first
        temp_db.update_healing_suggestion(suggestion_id, status="approved")
        
        response = client.post(f"/api/v1/healing/suggestions/{suggestion_id}/apply")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
    
    def test_apply_non_approved_fails(self, client, temp_db):
        """Test that applying non-approved suggestion fails."""
        review_id = temp_db.create_healing_review("test-job")
        suggestion_id = temp_db.create_healing_suggestion(
            review_id, "test-job", "config", "low", "Title", "Desc"
        )
        # Don't approve
        
        response = client.post(f"/api/v1/healing/suggestions/{suggestion_id}/apply")
        assert response.status_code == 400


class TestHealingStatsAPI:
    """Tests for healing stats API."""
    
    def test_get_stats_empty(self, client):
        """Test getting stats when empty."""
        response = client.get("/api/v1/healing/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["suggestions"]["total"] == 0
        assert data["reviews"]["total"] == 0
        assert data["actions"]["total"] == 0
    
    def test_get_stats_with_data(self, client, temp_db):
        """Test getting stats with data."""
        # Create some data
        review_id = temp_db.create_healing_review("job-1")
        temp_db.update_healing_review(review_id, status="completed")
        
        s1 = temp_db.create_healing_suggestion(
            review_id, "job-1", "prompt", "high", "Title 1", "Desc 1"
        )
        s2 = temp_db.create_healing_suggestion(
            review_id, "job-1", "config", "low", "Title 2", "Desc 2"
        )
        temp_db.update_healing_suggestion(s1, status="applied")
        temp_db.update_healing_suggestion(s2, status="rejected")
        
        response = client.get("/api/v1/healing/stats")
        assert response.status_code == 200
        data = response.json()
        
        assert data["suggestions"]["total"] == 2
        assert data["suggestions"]["applied"] == 1
        assert data["suggestions"]["rejected"] == 1
        assert data["reviews"]["total"] == 1
        assert data["reviews"]["completed"] == 1
        assert "prompt" in data["by_category"]
        assert "config" in data["by_category"]
        assert "high" in data["by_severity"]
        assert "low" in data["by_severity"]
