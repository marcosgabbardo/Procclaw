"""Output parsing for ProcClaw.

Detects errors and warnings in job output even when exit code is 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from procclaw.models import JobConfig


@dataclass
class OutputMatch:
    """A match found in job output."""
    
    pattern: str
    level: str  # "error", "warning", "rate_limit"
    line: str
    line_number: int
    
    def __repr__(self) -> str:
        return f"OutputMatch({self.level}: '{self.pattern}' at line {self.line_number})"


@dataclass
class OutputAlertConfig:
    """Configuration for output-based alerts."""
    
    error_patterns: list[str]
    warning_patterns: list[str]
    rate_limit_patterns: list[str]
    
    @classmethod
    def default(cls) -> "OutputAlertConfig":
        """Get default output alert configuration."""
        return cls(
            error_patterns=[
                r"ERROR",
                r"Exception",
                r"Traceback",
                r"FATAL",
                r"CRITICAL",
                r"failed",
                r"failure",
                r"\bpanic\b",
                r"segmentation fault",
                r"core dumped",
            ],
            warning_patterns=[
                r"WARNING",
                r"WARN",
                r"deprecated",
            ],
            rate_limit_patterns=[
                r"429",
                r"rate.?limit",
                r"too.?many.?requests",
                r"quota.?exceeded",
                r"throttl",
            ],
        )


class OutputParser:
    """Parses job output for errors and warnings.
    
    Can be configured with custom patterns per job or use defaults.
    """
    
    def __init__(
        self,
        config: OutputAlertConfig | None = None,
        max_context_lines: int = 3,
    ):
        """Initialize the output parser.
        
        Args:
            config: Output alert configuration
            max_context_lines: Number of context lines around matches
        """
        self._config = config or OutputAlertConfig.default()
        self._max_context = max_context_lines
        
        # Compile patterns
        self._error_regex = self._compile_patterns(self._config.error_patterns)
        self._warning_regex = self._compile_patterns(self._config.warning_patterns)
        self._rate_limit_regex = self._compile_patterns(self._config.rate_limit_patterns)
    
    def _compile_patterns(self, patterns: list[str]) -> re.Pattern | None:
        """Compile a list of patterns into a single regex."""
        if not patterns:
            return None
        
        combined = "|".join(f"({p})" for p in patterns)
        return re.compile(combined, re.IGNORECASE)
    
    def parse_output(
        self,
        content: str,
        check_errors: bool = True,
        check_warnings: bool = True,
        check_rate_limits: bool = True,
    ) -> list[OutputMatch]:
        """Parse output content for matches.
        
        Args:
            content: The output content to parse
            check_errors: Check for error patterns
            check_warnings: Check for warning patterns
            check_rate_limits: Check for rate limit patterns
            
        Returns:
            List of matches found
        """
        matches = []
        lines = content.split("\n")
        
        for i, line in enumerate(lines, start=1):
            # Check for errors
            if check_errors and self._error_regex and self._error_regex.search(line):
                match = self._error_regex.search(line)
                matches.append(OutputMatch(
                    pattern=match.group(0),
                    level="error",
                    line=line.strip()[:200],
                    line_number=i,
                ))
            
            # Check for warnings
            elif check_warnings and self._warning_regex and self._warning_regex.search(line):
                match = self._warning_regex.search(line)
                matches.append(OutputMatch(
                    pattern=match.group(0),
                    level="warning",
                    line=line.strip()[:200],
                    line_number=i,
                ))
            
            # Check for rate limits
            elif check_rate_limits and self._rate_limit_regex and self._rate_limit_regex.search(line):
                match = self._rate_limit_regex.search(line)
                matches.append(OutputMatch(
                    pattern=match.group(0),
                    level="rate_limit",
                    line=line.strip()[:200],
                    line_number=i,
                ))
        
        return matches
    
    def parse_file(
        self,
        file_path: Path,
        max_size: int = 1024 * 1024,  # 1MB
        tail_only: bool = True,
    ) -> list[OutputMatch]:
        """Parse a log file for matches.
        
        Args:
            file_path: Path to the log file
            max_size: Maximum bytes to read
            tail_only: Only read the last max_size bytes
            
        Returns:
            List of matches found
        """
        if not file_path.exists():
            return []
        
        try:
            file_size = file_path.stat().st_size
            
            with open(file_path, "r", errors="ignore") as f:
                if tail_only and file_size > max_size:
                    f.seek(file_size - max_size)
                    # Skip partial line
                    f.readline()
                
                content = f.read(max_size)
            
            return self.parse_output(content)
            
        except Exception as e:
            logger.warning(f"Failed to parse output file {file_path}: {e}")
            return []
    
    def get_context(
        self,
        content: str,
        line_number: int,
    ) -> str:
        """Get context lines around a match.
        
        Args:
            content: The full content
            line_number: The line number of the match
            
        Returns:
            Context lines as a string
        """
        lines = content.split("\n")
        start = max(0, line_number - 1 - self._max_context)
        end = min(len(lines), line_number + self._max_context)
        
        context_lines = []
        for i in range(start, end):
            marker = ">>> " if i == line_number - 1 else "    "
            context_lines.append(f"{marker}{lines[i]}")
        
        return "\n".join(context_lines)


class JobOutputChecker:
    """Checks job output after completion.
    
    Integrates with the supervisor to check job output for errors
    even when exit code is 0.
    """
    
    def __init__(
        self,
        logs_dir: Path,
        on_error: callable | None = None,
        on_warning: callable | None = None,
        on_rate_limit: callable | None = None,
    ):
        """Initialize the job output checker.
        
        Args:
            logs_dir: Directory containing job logs
            on_error: Callback for errors (job_id, match)
            on_warning: Callback for warnings (job_id, match)
            on_rate_limit: Callback for rate limits (job_id, match)
        """
        self._logs_dir = logs_dir
        self._on_error = on_error
        self._on_warning = on_warning
        self._on_rate_limit = on_rate_limit
        self._parser = OutputParser()
    
    def check_job_output(
        self,
        job_id: str,
        job: "JobConfig",
        exit_code: int,
    ) -> list[OutputMatch]:
        """Check a job's output after completion.
        
        Args:
            job_id: The job ID
            job: The job configuration
            exit_code: The job's exit code
            
        Returns:
            List of matches found
        """
        # Only check if job succeeded (exit 0) - failures are already handled
        # Or always check if we want to detect rate limits for retry
        
        stdout_path = self._logs_dir / f"{job_id}.log"
        stderr_path = self._logs_dir / f"{job_id}.error.log"
        
        all_matches = []
        
        # Check stderr first (more likely to have errors)
        if stderr_path.exists():
            matches = self._parser.parse_file(stderr_path)
            all_matches.extend(matches)
        
        # Also check stdout
        if stdout_path.exists():
            matches = self._parser.parse_file(stdout_path, tail_only=True)
            all_matches.extend(matches)
        
        # Trigger callbacks
        for match in all_matches:
            if match.level == "error" and self._on_error:
                self._on_error(job_id, match)
            elif match.level == "warning" and self._on_warning:
                self._on_warning(job_id, match)
            elif match.level == "rate_limit" and self._on_rate_limit:
                self._on_rate_limit(job_id, match)
        
        if all_matches:
            logger.info(
                f"Found {len(all_matches)} patterns in output for '{job_id}': "
                f"{[m.level for m in all_matches]}"
            )
        
        return all_matches
    
    def has_errors(self, job_id: str) -> bool:
        """Check if a job's output contains errors."""
        stderr_path = self._logs_dir / f"{job_id}.error.log"
        
        if not stderr_path.exists():
            return False
        
        matches = self._parser.parse_file(stderr_path)
        return any(m.level == "error" for m in matches)
    
    def has_rate_limit(self, job_id: str) -> bool:
        """Check if a job's output indicates rate limiting."""
        stderr_path = self._logs_dir / f"{job_id}.error.log"
        stdout_path = self._logs_dir / f"{job_id}.log"
        
        for path in [stderr_path, stdout_path]:
            if path.exists():
                matches = self._parser.parse_file(path)
                if any(m.level == "rate_limit" for m in matches):
                    return True
        
        return False
