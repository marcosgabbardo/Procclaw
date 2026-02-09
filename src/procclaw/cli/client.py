"""HTTP client for communicating with ProcClaw daemon API."""

from __future__ import annotations

import httpx
from loguru import logger

from procclaw.config import load_config


class APIClient:
    """Client for ProcClaw daemon API."""

    def __init__(self, base_url: str | None = None, token: str | None = None):
        """Initialize the API client.

        Args:
            base_url: Base URL for the API (default: from config)
            token: Authentication token (default: from config)
        """
        if base_url is None:
            config = load_config()
            base_url = f"http://{config.daemon.host}:{config.daemon.port}"
            if config.api.auth.enabled and token is None:
                token = config.api.auth.token

        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        """Get or create the HTTP client."""
        if self._client is None:
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "APIClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def is_daemon_running(self) -> bool:
        """Check if the daemon is running and responding."""
        try:
            response = self._get_client().get("/health")
            return response.status_code == 200
        except httpx.RequestError:
            return False

    def health(self) -> dict:
        """Get daemon health status."""
        response = self._get_client().get("/health")
        response.raise_for_status()
        return response.json()

    def list_jobs(
        self, 
        status: str | None = None, 
        tag: str | None = None,
        job_type: str | None = None,
        query: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict]:
        """List all jobs with optional filtering."""
        params = {}
        if status:
            params["status"] = status
        if tag:
            params["tag"] = tag
        if job_type:
            params["type"] = job_type
        if query:
            params["q"] = query
        if enabled is not None:
            params["enabled"] = str(enabled).lower()

        response = self._get_client().get("/api/v1/jobs", params=params)
        response.raise_for_status()
        return response.json()["jobs"]
    
    def search_jobs(self, query: str) -> list[dict]:
        """Search jobs by name, description, or tags."""
        return self.list_jobs(query=query)

    def get_job(self, job_id: str) -> dict:
        """Get job status."""
        response = self._get_client().get(f"/api/v1/jobs/{job_id}")
        response.raise_for_status()
        return response.json()

    def start_job(self, job_id: str) -> dict:
        """Start a job."""
        response = self._get_client().post(f"/api/v1/jobs/{job_id}/start")
        response.raise_for_status()
        return response.json()

    def stop_job(self, job_id: str, force: bool = False) -> dict:
        """Stop a job."""
        params = {"force": force} if force else {}
        response = self._get_client().post(f"/api/v1/jobs/{job_id}/stop", params=params)
        response.raise_for_status()
        return response.json()

    def restart_job(self, job_id: str) -> dict:
        """Restart a job."""
        response = self._get_client().post(f"/api/v1/jobs/{job_id}/restart")
        response.raise_for_status()
        return response.json()

    def run_job(self, job_id: str) -> dict:
        """Trigger a manual run of a job."""
        response = self._get_client().post(f"/api/v1/jobs/{job_id}/run")
        response.raise_for_status()
        return response.json()

    def get_logs(self, job_id: str, lines: int = 100, error: bool = False) -> dict:
        """Get job logs."""
        params = {"lines": lines, "error": error}
        response = self._get_client().get(f"/api/v1/jobs/{job_id}/logs", params=params)
        response.raise_for_status()
        return response.json()

    def reload_config(self) -> dict:
        """Reload jobs configuration."""
        response = self._get_client().post("/api/v1/reload")
        response.raise_for_status()
        return response.json()

    def metrics(self) -> str:
        """Get Prometheus metrics."""
        response = self._get_client().get("/metrics")
        response.raise_for_status()
        return response.json()["text"]
    
    # Generic HTTP methods for new endpoints
    
    def get(self, path: str, params: dict | None = None) -> dict:
        """Generic GET request."""
        response = self._get_client().get(f"/api/v1{path}", params=params)
        response.raise_for_status()
        return response.json()
    
    def post(self, path: str, json: dict | None = None) -> dict:
        """Generic POST request."""
        response = self._get_client().post(f"/api/v1{path}", json=json)
        response.raise_for_status()
        return response.json()
