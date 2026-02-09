"""Task revocation for ProcClaw.

Allows cancelling jobs that are queued or scheduled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database


@dataclass
class Revocation:
    """A job revocation record."""
    
    job_id: str
    revoked_at: datetime
    reason: str | None = None
    expires_at: datetime | None = None
    terminate: bool = False  # Also kill if running
    
    @property
    def is_expired(self) -> bool:
        """Check if the revocation has expired."""
        if self.expires_at is None:
            return False
        return datetime.now(ZoneInfo("UTC")) >= self.expires_at


class RevocationManager:
    """Manages job revocations.
    
    Features:
    - Revoke queued/scheduled jobs
    - Optional terminate running jobs
    - Expiring revocations
    - Persistence across restarts
    """
    
    def __init__(
        self,
        db: "Database",
        default_expiry_seconds: int = 3600,  # 1 hour
    ):
        """Initialize the revocation manager.
        
        Args:
            db: Database for persistence
            default_expiry_seconds: Default revocation expiry
        """
        self._db = db
        self._default_expiry = default_expiry_seconds
        self._lock = Lock()
        self._revocations: dict[str, Revocation] = {}
        
        # Load persisted revocations
        self._load_from_db()
    
    def _load_from_db(self) -> None:
        """Load revocations from database."""
        try:
            rows = self._db.query(
                "SELECT job_id, revoked_at, reason, expires_at, terminate "
                "FROM revocations"
            )
            now = datetime.now(ZoneInfo("UTC"))
            loaded = 0
            for row in rows:
                expires_at = None
                if row["expires_at"]:
                    expires_at = datetime.fromisoformat(row["expires_at"]).replace(tzinfo=ZoneInfo("UTC"))
                    if expires_at <= now:
                        continue  # Skip expired
                
                revocation = Revocation(
                    job_id=row["job_id"],
                    revoked_at=datetime.fromisoformat(row["revoked_at"]).replace(tzinfo=ZoneInfo("UTC")),
                    reason=row["reason"],
                    expires_at=expires_at,
                    terminate=bool(row["terminate"]),
                )
                self._revocations[row["job_id"]] = revocation
                loaded += 1
            logger.debug(f"Loaded {loaded} active revocations from database")
        except Exception as e:
            logger.debug(f"No revocations table yet: {e}")
    
    def revoke(
        self,
        job_id: str,
        reason: str | None = None,
        terminate: bool = False,
        expires_in: int | None = None,
    ) -> Revocation:
        """Revoke a job.
        
        Args:
            job_id: The job to revoke
            reason: Optional reason for revocation
            terminate: If True, also kill if running
            expires_in: Seconds until revocation expires (None = default)
            
        Returns:
            The Revocation record
        """
        now = datetime.now(ZoneInfo("UTC"))
        
        if expires_in is None:
            expires_in = self._default_expiry
        
        expires_at = now + timedelta(seconds=expires_in) if expires_in > 0 else None
        
        revocation = Revocation(
            job_id=job_id,
            revoked_at=now,
            reason=reason,
            expires_at=expires_at,
            terminate=terminate,
        )
        
        with self._lock:
            self._revocations[job_id] = revocation
            self._persist_to_db(revocation)
        
        logger.info(f"Revoked job '{job_id}' (terminate={terminate}, reason={reason})")
        return revocation
    
    def unrevoke(self, job_id: str) -> bool:
        """Remove a revocation.
        
        Args:
            job_id: The job to unrevoke
            
        Returns:
            True if revocation was removed, False if not found
        """
        with self._lock:
            if job_id in self._revocations:
                del self._revocations[job_id]
                self._remove_from_db(job_id)
                logger.info(f"Unrevoked job '{job_id}'")
                return True
        return False
    
    def is_revoked(self, job_id: str) -> bool:
        """Check if a job is revoked.
        
        Args:
            job_id: The job to check
            
        Returns:
            True if revoked and not expired
        """
        with self._lock:
            revocation = self._revocations.get(job_id)
            if revocation is None:
                return False
            
            if revocation.is_expired:
                # Clean up expired
                del self._revocations[job_id]
                self._remove_from_db(job_id)
                return False
            
            return True
    
    def get_revocation(self, job_id: str) -> Revocation | None:
        """Get the revocation record for a job."""
        revocation = self._revocations.get(job_id)
        if revocation and revocation.is_expired:
            with self._lock:
                del self._revocations[job_id]
                self._remove_from_db(job_id)
            return None
        return revocation
    
    def should_terminate(self, job_id: str) -> bool:
        """Check if a running job should be terminated.
        
        Args:
            job_id: The job to check
            
        Returns:
            True if revoked with terminate=True
        """
        revocation = self.get_revocation(job_id)
        return revocation is not None and revocation.terminate
    
    def get_all_active(self) -> list[Revocation]:
        """Get all active (non-expired) revocations."""
        self.cleanup_expired()
        return list(self._revocations.values())
    
    def cleanup_expired(self) -> int:
        """Remove expired revocations.
        
        Returns:
            Number of expired revocations removed
        """
        expired = []
        now = datetime.now(ZoneInfo("UTC"))
        
        with self._lock:
            for job_id, revocation in list(self._revocations.items()):
                if revocation.expires_at and revocation.expires_at <= now:
                    expired.append(job_id)
            
            for job_id in expired:
                del self._revocations[job_id]
                self._remove_from_db(job_id)
        
        if expired:
            logger.debug(f"Cleaned up {len(expired)} expired revocations")
        
        return len(expired)
    
    def consume_revocation(self, job_id: str) -> Revocation | None:
        """Get and remove a revocation (for one-time use).
        
        Args:
            job_id: The job to check
            
        Returns:
            The Revocation if found, None otherwise
        """
        with self._lock:
            revocation = self._revocations.pop(job_id, None)
            if revocation:
                self._remove_from_db(job_id)
            return revocation
    
    def _persist_to_db(self, revocation: Revocation) -> None:
        """Persist a revocation to database."""
        try:
            self._db.execute(
                """
                INSERT OR REPLACE INTO revocations 
                (job_id, revoked_at, reason, expires_at, terminate)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    revocation.job_id,
                    revocation.revoked_at.isoformat(),
                    revocation.reason,
                    revocation.expires_at.isoformat() if revocation.expires_at else None,
                    1 if revocation.terminate else 0,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to persist revocation: {e}")
    
    def _remove_from_db(self, job_id: str) -> None:
        """Remove a revocation from database."""
        try:
            self._db.execute("DELETE FROM revocations WHERE job_id = ?", (job_id,))
        except Exception as e:
            logger.debug(f"Failed to remove revocation from db: {e}")
