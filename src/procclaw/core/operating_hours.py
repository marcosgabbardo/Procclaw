"""Operating hours management for ProcClaw.

Allows jobs to be configured to run only during specific hours/days.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING

import pytz
from loguru import logger

if TYPE_CHECKING:
    from procclaw.models import JobConfig, OperatingHoursConfig, OperatingHoursAction


class OperatingHoursChecker:
    """Checks if jobs are within their operating hours.
    
    This can be used by:
    - Scheduler: to skip scheduled runs outside hours
    - Supervisor: to pause continuous jobs outside hours
    """
    
    def __init__(self, default_timezone: str = "America/Sao_Paulo"):
        """Initialize the operating hours checker.
        
        Args:
            default_timezone: Default timezone for jobs without one
        """
        self._default_tz = default_timezone
    
    def is_within_hours(
        self,
        config: "OperatingHoursConfig",
        check_time: datetime | None = None,
    ) -> bool:
        """Check if the current time is within operating hours.
        
        Args:
            config: Operating hours configuration
            check_time: Time to check (default: now)
            
        Returns:
            True if within operating hours
        """
        if not config.enabled:
            return True  # No restrictions
        
        tz = pytz.timezone(config.timezone or self._default_tz)
        
        if check_time is None:
            now = datetime.now(tz)
        else:
            if check_time.tzinfo is None:
                now = tz.localize(check_time)
            else:
                now = check_time.astimezone(tz)
        
        # Check day of week (1=Monday, 7=Sunday)
        weekday = now.isoweekday()
        if weekday not in config.days:
            return False
        
        # Parse start and end times
        try:
            start_parts = config.start.split(":")
            if len(start_parts) != 2:
                raise ValueError(f"Invalid start time format: {config.start}")
            start_time = time(int(start_parts[0]), int(start_parts[1]))
            
            end_parts = config.end.split(":")
            if len(end_parts) != 2:
                raise ValueError(f"Invalid end time format: {config.end}")
            end_time = time(int(end_parts[0]), int(end_parts[1]))
        except (ValueError, IndexError) as e:
            logger.warning(f"Invalid operating hours format: {e}")
            return True  # Fail open - allow job to run
        
        current_time = now.time()
        
        # Handle overnight hours (e.g., 22:00 - 06:00)
        if start_time <= end_time:
            # Normal case: start before end
            return start_time <= current_time <= end_time
        else:
            # Overnight case: end is next day
            return current_time >= start_time or current_time <= end_time
    
    def get_next_operating_start(
        self,
        config: "OperatingHoursConfig",
    ) -> datetime | None:
        """Get the next time operating hours begin.
        
        Args:
            config: Operating hours configuration
            
        Returns:
            Next start time, or None if always operating
        """
        if not config.enabled:
            return None
        
        tz = pytz.timezone(config.timezone or self._default_tz)
        now = datetime.now(tz)
        
        # If currently within hours, return None
        if self.is_within_hours(config, now):
            return None
        
        # Parse start time
        try:
            start_parts = config.start.split(":")
            start_time = time(int(start_parts[0]), int(start_parts[1]))
        except (ValueError, IndexError):
            return None
        
        # Find the next valid day
        for days_ahead in range(8):  # Check up to a week ahead
            check_date = now.date()
            from datetime import timedelta
            check_date = check_date + timedelta(days=days_ahead)
            check_dt = datetime.combine(check_date, start_time)
            check_dt = tz.localize(check_dt)
            
            if check_dt.isoweekday() in config.days and check_dt > now:
                return check_dt
        
        return None
    
    def should_run_job(self, job: "JobConfig") -> tuple[bool, str]:
        """Check if a job should run based on operating hours.
        
        Args:
            job: The job configuration
            
        Returns:
            Tuple of (should_run, reason)
        """
        from procclaw.models import OperatingHoursAction
        
        config = job.operating_hours
        
        if not config.enabled:
            return True, "No operating hours configured"
        
        within_hours = self.is_within_hours(config)
        
        if within_hours:
            return True, "Within operating hours"
        
        # Outside operating hours - check action
        if config.action == OperatingHoursAction.SKIP:
            return False, "Outside operating hours (skip)"
        elif config.action == OperatingHoursAction.PAUSE:
            return False, "Outside operating hours (pause)"
        elif config.action == OperatingHoursAction.ALERT:
            # Run but also log/alert
            logger.warning(f"Job running outside operating hours")
            return True, "Outside operating hours (alert)"
        
        return True, "Unknown action"
    
    def get_status_message(
        self,
        job: "JobConfig",
    ) -> str:
        """Get a human-readable status message for operating hours.
        
        Args:
            job: The job configuration
            
        Returns:
            Status message
        """
        config = job.operating_hours
        
        if not config.enabled:
            return "No restrictions"
        
        if self.is_within_hours(config):
            return "Within operating hours"
        
        next_start = self.get_next_operating_start(config)
        if next_start:
            return f"Outside hours, next start: {next_start.strftime('%a %H:%M')}"
        
        return "Outside operating hours"
