"""Event triggers for ProcClaw.

Allows jobs to be triggered by external events:
- Webhooks (HTTP POST)
- File creation
- (Future: Message queues)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent

from loguru import logger

if TYPE_CHECKING:
    from procclaw.models import TriggerConfig


@dataclass
class TriggerEvent:
    """An event that triggers a job."""
    
    job_id: str
    trigger_type: str  # "webhook", "file", "queue"
    timestamp: datetime
    payload: dict | None = None
    idempotency_key: str | None = None
    source: str | None = None  # IP, filename, queue name


@dataclass
class WebhookRequest:
    """An incoming webhook request."""
    
    job_id: str
    payload: dict | None
    idempotency_key: str | None
    auth_token: str | None
    source_ip: str | None


class WebhookValidator:
    """Validates incoming webhook requests."""
    
    def __init__(self, configs: dict[str, "TriggerConfig"]):
        """Initialize with trigger configurations.
        
        Args:
            configs: Map of job_id -> TriggerConfig
        """
        self._configs = configs
    
    def validate(self, request: WebhookRequest) -> tuple[bool, str]:
        """Validate a webhook request.
        
        Args:
            request: The incoming request
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        config = self._configs.get(request.job_id)
        
        if config is None:
            return False, f"Job '{request.job_id}' not found"
        
        if not config.enabled:
            return False, f"Trigger not enabled for '{request.job_id}'"
        
        if config.type.value != "webhook":
            return False, f"Job '{request.job_id}' is not webhook-triggered"
        
        # Check auth token if configured
        if config.auth_token:
            if request.auth_token != config.auth_token:
                return False, "Invalid auth token"
        
        return True, ""
    
    def update_config(self, job_id: str, config: "TriggerConfig") -> None:
        """Update a job's trigger configuration."""
        self._configs[job_id] = config
    
    def remove_config(self, job_id: str) -> None:
        """Remove a job's trigger configuration."""
        self._configs.pop(job_id, None)


class FileWatchHandler(FileSystemEventHandler):
    """Handles file creation events for triggers."""
    
    def __init__(
        self,
        on_file_created: Callable[[str, Path], None],
        patterns: dict[str, str],  # job_id -> glob pattern
        delete_after: dict[str, bool],  # job_id -> should delete
    ):
        """Initialize the file watch handler.
        
        Args:
            on_file_created: Callback when matching file is created
            patterns: Glob patterns per job
            delete_after: Whether to delete files after triggering
        """
        super().__init__()
        self._on_file_created = on_file_created
        self._patterns = patterns
        self._delete_after = delete_after
    
    def on_created(self, event):
        """Handle file creation event."""
        if isinstance(event, FileCreatedEvent):
            file_path = Path(event.src_path)
            
            for job_id, pattern in self._patterns.items():
                if file_path.match(pattern):
                    logger.info(f"File trigger: {file_path} -> job '{job_id}'")
                    
                    try:
                        self._on_file_created(job_id, file_path)
                    except Exception as e:
                        logger.error(f"File trigger callback error: {e}")
                    
                    # Delete file if configured
                    if self._delete_after.get(job_id, False):
                        try:
                            file_path.unlink()
                            logger.debug(f"Deleted trigger file: {file_path}")
                        except Exception as e:
                            logger.warning(f"Failed to delete trigger file: {e}")
                    
                    break


class FileTriggerWatcher:
    """Watches directories for file creation triggers."""
    
    def __init__(self):
        """Initialize the file trigger watcher."""
        self._observer = Observer()
        self._handlers: dict[str, FileWatchHandler] = {}  # path -> handler
        self._running = False
    
    def add_watch(
        self,
        job_id: str,
        watch_path: str,
        pattern: str,
        delete_after: bool,
        on_trigger: Callable[[str, Path], None],
    ) -> None:
        """Add a file watch for a job.
        
        Args:
            job_id: The job ID
            watch_path: Directory to watch
            pattern: Glob pattern for matching files
            delete_after: Whether to delete files after triggering
            on_trigger: Callback when file is created
        """
        path = Path(watch_path).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        
        path_str = str(path)
        
        if path_str not in self._handlers:
            handler = FileWatchHandler(
                on_file_created=on_trigger,
                patterns={},
                delete_after={},
            )
            self._handlers[path_str] = handler
            self._observer.schedule(handler, path_str, recursive=False)
        
        handler = self._handlers[path_str]
        handler._patterns[job_id] = pattern
        handler._delete_after[job_id] = delete_after
        
        logger.info(f"Added file watch: {path} ({pattern}) -> job '{job_id}'")
    
    def remove_watch(self, job_id: str) -> None:
        """Remove file watches for a job."""
        for handler in self._handlers.values():
            handler._patterns.pop(job_id, None)
            handler._delete_after.pop(job_id, None)
    
    def start(self) -> None:
        """Start the file watcher."""
        if not self._running:
            self._observer.start()
            self._running = True
            logger.info("File trigger watcher started")
    
    def stop(self) -> None:
        """Stop the file watcher."""
        if self._running:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._running = False
            logger.info("File trigger watcher stopped")
    
    @property
    def is_running(self) -> bool:
        """Check if watcher is running."""
        return self._running


class TriggerManager:
    """Manages all event triggers for jobs.
    
    Provides a unified interface for:
    - Webhook triggers via HTTP API
    - File triggers via filesystem watching
    - (Future) Queue triggers
    """
    
    def __init__(
        self,
        on_trigger: Callable[[TriggerEvent], bool],
    ):
        """Initialize the trigger manager.
        
        Args:
            on_trigger: Callback when a trigger fires, returns success
        """
        self._on_trigger = on_trigger
        self._configs: dict[str, "TriggerConfig"] = {}
        
        # Components
        self._webhook_validator = WebhookValidator(self._configs)
        self._file_watcher = FileTriggerWatcher()
    
    def register_job(self, job_id: str, config: "TriggerConfig") -> None:
        """Register a job's trigger configuration.
        
        Args:
            job_id: The job ID
            config: Trigger configuration
        """
        self._configs[job_id] = config
        self._webhook_validator.update_config(job_id, config)
        
        # Set up file watch if needed
        if config.enabled and config.type.value == "file" and config.watch_path:
            self._file_watcher.add_watch(
                job_id=job_id,
                watch_path=config.watch_path,
                pattern=config.pattern,
                delete_after=config.delete_after,
                on_trigger=self._handle_file_trigger,
            )
    
    def unregister_job(self, job_id: str) -> None:
        """Unregister a job's triggers."""
        self._configs.pop(job_id, None)
        self._webhook_validator.remove_config(job_id)
        self._file_watcher.remove_watch(job_id)
    
    def handle_webhook(
        self,
        job_id: str,
        payload: dict | None = None,
        idempotency_key: str | None = None,
        auth_token: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[bool, str]:
        """Handle an incoming webhook trigger.
        
        Args:
            job_id: The job ID
            payload: Request payload
            idempotency_key: Idempotency key
            auth_token: Authorization token
            source_ip: Source IP address
            
        Returns:
            Tuple of (success, message)
        """
        request = WebhookRequest(
            job_id=job_id,
            payload=payload,
            idempotency_key=idempotency_key,
            auth_token=auth_token,
            source_ip=source_ip,
        )
        
        # Validate
        is_valid, error = self._webhook_validator.validate(request)
        if not is_valid:
            logger.warning(f"Webhook validation failed for '{job_id}': {error}")
            return False, error
        
        # Create trigger event
        event = TriggerEvent(
            job_id=job_id,
            trigger_type="webhook",
            timestamp=datetime.now(),
            payload=payload,
            idempotency_key=idempotency_key,
            source=source_ip,
        )
        
        # Fire trigger
        try:
            success = self._on_trigger(event)
            if success:
                logger.info(f"Webhook trigger fired for '{job_id}'")
                return True, "Triggered"
            else:
                return False, "Trigger rejected"
        except Exception as e:
            logger.error(f"Webhook trigger error for '{job_id}': {e}")
            return False, str(e)
    
    def _handle_file_trigger(self, job_id: str, file_path: Path) -> None:
        """Handle a file trigger event."""
        # Read file content as payload
        payload = None
        try:
            if file_path.suffix == ".json":
                payload = json.loads(file_path.read_text())
            else:
                payload = {"filename": file_path.name, "path": str(file_path)}
        except:
            payload = {"filename": file_path.name}
        
        event = TriggerEvent(
            job_id=job_id,
            trigger_type="file",
            timestamp=datetime.now(),
            payload=payload,
            source=str(file_path),
        )
        
        try:
            self._on_trigger(event)
        except Exception as e:
            logger.error(f"File trigger error for '{job_id}': {e}")
    
    def start(self) -> None:
        """Start all trigger watchers."""
        self._file_watcher.start()
        self._running = True
    
    def stop(self) -> None:
        """Stop all trigger watchers."""
        self._running = False
        self._file_watcher.stop()
    
    async def run(self) -> None:
        """Run the trigger manager (async loop).
        
        This method runs until stop() is called, allowing
        the file watcher to process events in the background.
        """
        self.start()
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            self.stop()
    
    def get_registered_jobs(self) -> list[str]:
        """Get list of jobs with triggers."""
        return list(self._configs.keys())
    
    def get_config(self, job_id: str) -> "TriggerConfig" | None:
        """Get a job's trigger configuration."""
        return self._configs.get(job_id)
