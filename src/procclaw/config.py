"""Configuration loading and management for ProcClaw."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from loguru import logger

from procclaw.models import (
    JobConfig,
    JobsConfig,
    JobType,
    ProcClawConfig,
)

# Default paths
DEFAULT_CONFIG_DIR = Path.home() / ".procclaw"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "procclaw.yaml"
DEFAULT_JOBS_FILE = DEFAULT_CONFIG_DIR / "jobs.yaml"  # Legacy, for migration
DEFAULT_JOBS_DIR = DEFAULT_CONFIG_DIR / "jobs"  # New: one file per job
DEFAULT_DB_FILE = DEFAULT_CONFIG_DIR / "procclaw.db"
DEFAULT_PID_FILE = DEFAULT_CONFIG_DIR / "procclaw.pid"
DEFAULT_LOGS_DIR = DEFAULT_CONFIG_DIR / "logs"


class ConfigError(Exception):
    """Configuration error."""

    pass


def ensure_config_dir() -> Path:
    """Ensure the config directory exists."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Auto-migrate from jobs.yaml to jobs/ directory
    migrate_jobs_yaml_to_dir()
    
    return DEFAULT_CONFIG_DIR


def migrate_jobs_yaml_to_dir() -> bool:
    """Migrate from single jobs.yaml to individual job files in jobs/ directory.
    
    This function is idempotent - it only runs if:
    - jobs.yaml exists
    - jobs/ directory is empty or doesn't exist
    
    After migration, renames jobs.yaml to jobs.yaml.bak
    
    Returns:
        True if migration was performed, False if skipped
    """
    jobs_file = DEFAULT_JOBS_FILE
    jobs_dir = DEFAULT_JOBS_DIR
    
    # Only migrate if jobs.yaml exists
    if not jobs_file.exists():
        return False
    
    # Only migrate if jobs/ directory is empty or doesn't exist
    jobs_dir.mkdir(parents=True, exist_ok=True)
    existing_job_files = list(jobs_dir.glob("*.yaml"))
    if existing_job_files:
        logger.debug(f"Jobs directory already has {len(existing_job_files)} files, skipping migration")
        return False
    
    logger.info(f"Migrating jobs.yaml to individual files in {jobs_dir}")
    
    try:
        with open(jobs_file) as f:
            data = yaml.safe_load(f)
        
        if not isinstance(data, dict):
            logger.warning(f"Invalid jobs.yaml format: expected dict, got {type(data)}")
            return False
        
        # Handle both formats: {jobs: {id: config}} and {id: config}
        jobs_dict = data.get("jobs", data)
        if not isinstance(jobs_dict, dict):
            logger.warning(f"Invalid jobs structure in jobs.yaml")
            return False
        
        migrated = 0
        for job_id, config in jobs_dict.items():
            if job_id.startswith("_"):  # Skip metadata keys
                continue
            if not isinstance(config, dict):
                logger.warning(f"Skipping job '{job_id}': config is not a dict")
                continue
            
            job_file = jobs_dir / f"{job_id}.yaml"
            with open(job_file, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            migrated += 1
            logger.debug(f"Migrated job '{job_id}' to {job_file}")
        
        # Backup original jobs.yaml
        backup_file = jobs_file.with_suffix(".yaml.bak")
        jobs_file.rename(backup_file)
        logger.info(f"Migration complete: {migrated} jobs migrated, original backed up to {backup_file}")
        
        return True
        
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False


def expand_env_vars(value: str) -> str:
    """Expand environment variables in a string.

    Supports:
    - ${env:VAR_NAME} - environment variable
    - ${secret:SECRET_NAME} - secret from keychain (TODO)
    - $VAR_NAME or ${VAR_NAME} - standard env var expansion
    """
    # Handle ${env:VAR_NAME} syntax
    def replace_env(match: re.Match[str]) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, "")

    value = re.sub(r"\$\{env:([^}]+)\}", replace_env, value)

    # Handle ${secret:SECRET_NAME} syntax (placeholder for now)
    def replace_secret(match: re.Match[str]) -> str:
        secret_name = match.group(1)
        # TODO: Implement keychain lookup
        logger.warning(f"Secret lookup not implemented: {secret_name}")
        return f"${{secret:{secret_name}}}"

    value = re.sub(r"\$\{secret:([^}]+)\}", replace_secret, value)

    # Standard environment variable expansion
    value = os.path.expandvars(value)

    return value


def expand_path(path: str | None) -> str | None:
    """Expand a path with ~ and environment variables."""
    if path is None:
        return None
    expanded = os.path.expanduser(path)
    expanded = expand_env_vars(expanded)
    return expanded


def load_yaml_file(path: Path) -> dict:
    """Load a YAML file."""
    if not path.exists():
        return {}

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    return data


def load_config(config_path: Path | None = None) -> ProcClawConfig:
    """Load the main ProcClaw configuration."""
    path = config_path or DEFAULT_CONFIG_FILE

    if not path.exists():
        logger.info(f"Config file not found at {path}, using defaults")
        return ProcClawConfig()

    data = load_yaml_file(path)

    try:
        config = ProcClawConfig.model_validate(data)
        logger.debug(f"Loaded config from {path}")
        return config
    except Exception as e:
        raise ConfigError(f"Invalid config file {path}: {e}") from e


def load_jobs(jobs_path: Path | None = None) -> JobsConfig:
    """Load the jobs configuration from jobs/ directory.
    
    Each job is stored in its own file: jobs/{job_id}.yaml
    The job_id is derived from the filename (without .yaml extension).
    
    Args:
        jobs_path: Optional path to jobs directory (defaults to ~/.procclaw/jobs/)
        
    Returns:
        JobsConfig with all jobs loaded
    """
    jobs_dir = jobs_path or DEFAULT_JOBS_DIR
    
    # Ensure migration has happened
    if not jobs_dir.exists():
        # Try to ensure config dir which triggers migration
        ensure_config_dir()
    
    if not jobs_dir.exists() or not jobs_dir.is_dir():
        logger.warning(f"Jobs directory not found at {jobs_dir}")
        return JobsConfig()
    
    jobs_data: dict[str, dict] = {}
    
    # Load each .yaml file in the jobs directory
    for job_file in sorted(jobs_dir.glob("*.yaml")):
        job_id = job_file.stem  # filename without extension
        
        try:
            with open(job_file) as f:
                job_data = yaml.safe_load(f)
            
            if not isinstance(job_data, dict):
                logger.warning(f"Invalid job file {job_file}: expected dict, got {type(job_data)}")
                continue
            
            # Expand paths and env vars
            if "cwd" in job_data:
                job_data["cwd"] = expand_path(job_data["cwd"])
            if "cmd" in job_data:
                job_data["cmd"] = expand_env_vars(job_data["cmd"])
            if "env" in job_data:
                job_data["env"] = {
                    k: expand_env_vars(str(v)) for k, v in job_data["env"].items()
                }
            
            jobs_data[job_id] = job_data
            
        except Exception as e:
            logger.error(f"Failed to load job file {job_file}: {e}")
            continue
    
    try:
        jobs = JobsConfig.model_validate({"jobs": jobs_data})
        logger.debug(f"Loaded {len(jobs.jobs)} jobs from {jobs_dir}")

        # Validate jobs
        _validate_jobs(jobs)

        return jobs
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Invalid jobs in {jobs_dir}: {e}") from e


def _validate_jobs(jobs: JobsConfig) -> None:
    """Validate job configurations."""
    job_ids = set(jobs.jobs.keys())

    for job_id, job in jobs.jobs.items():
        # Scheduled jobs must have a schedule
        if job.type == JobType.SCHEDULED and not job.schedule:
            raise ConfigError(f"Job '{job_id}' is scheduled but has no schedule defined")

        # Validate cron expression
        if job.schedule:
            try:
                from croniter import croniter

                croniter(job.schedule)
            except Exception as e:
                raise ConfigError(
                    f"Job '{job_id}' has invalid cron expression '{job.schedule}': {e}"
                ) from e

        # Validate dependencies exist
        for dep in job.depends_on:
            if dep.job not in job_ids:
                raise ConfigError(
                    f"Job '{job_id}' depends on unknown job '{dep.job}'"
                )

        # Check for circular dependencies
        _check_circular_deps(job_id, jobs, set())


def _check_circular_deps(
    job_id: str, jobs: JobsConfig, visited: set[str], path: list[str] | None = None
) -> None:
    """Check for circular dependencies."""
    if path is None:
        path = []

    if job_id in visited:
        cycle = " -> ".join(path + [job_id])
        raise ConfigError(f"Circular dependency detected: {cycle}")

    job = jobs.jobs.get(job_id)
    if not job:
        return

    visited.add(job_id)
    path.append(job_id)

    for dep in job.depends_on:
        _check_circular_deps(dep.job, jobs, visited.copy(), path.copy())


def save_jobs(jobs: JobsConfig, jobs_path: Path | None = None) -> None:
    """Save all jobs to individual files in jobs/ directory.
    
    Each job is saved to: jobs/{job_id}.yaml
    
    Args:
        jobs: JobsConfig with all jobs
        jobs_path: Optional path to jobs directory (defaults to ~/.procclaw/jobs/)
    """
    jobs_dir = jobs_path or DEFAULT_JOBS_DIR
    ensure_config_dir()

    for job_id, job in jobs.jobs.items():
        job_dict = job.model_dump(mode="json", exclude_none=True)
        job_file = jobs_dir / f"{job_id}.yaml"
        
        with open(job_file, "w") as f:
            yaml.dump(job_dict, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    logger.info(f"Saved {len(jobs.jobs)} jobs to {jobs_dir}")


def save_job(job_id: str, job_config: dict | JobConfig, jobs_path: Path | None = None) -> None:
    """Save a single job to its own file.
    
    Args:
        job_id: The job ID (used as filename)
        job_config: Job configuration (dict or JobConfig model)
        jobs_path: Optional path to jobs directory
    """
    jobs_dir = jobs_path or DEFAULT_JOBS_DIR
    ensure_config_dir()
    
    # Convert JobConfig to dict if needed (mode="json" ensures enums are serialized as strings)
    if hasattr(job_config, 'model_dump'):
        job_dict = job_config.model_dump(mode="json", exclude_none=True)
    else:
        job_dict = dict(job_config)
    
    job_file = jobs_dir / f"{job_id}.yaml"
    
    with open(job_file, "w") as f:
        yaml.dump(job_dict, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    
    logger.debug(f"Saved job '{job_id}' to {job_file}")


def delete_job_file(job_id: str, jobs_path: Path | None = None) -> bool:
    """Delete a job's config file.
    
    Args:
        job_id: The job ID to delete
        jobs_path: Optional path to jobs directory
        
    Returns:
        True if file was deleted, False if not found
    """
    jobs_dir = jobs_path or DEFAULT_JOBS_DIR
    job_file = jobs_dir / f"{job_id}.yaml"
    
    if job_file.exists():
        job_file.unlink()
        logger.info(f"Deleted job file {job_file}")
        return True
    else:
        logger.warning(f"Job file not found: {job_file}")
        return False


def get_job_file_path(job_id: str, jobs_path: Path | None = None) -> Path:
    """Get the file path for a job's config file.
    
    Args:
        job_id: The job ID
        jobs_path: Optional path to jobs directory
        
    Returns:
        Path to the job file
    """
    jobs_dir = jobs_path or DEFAULT_JOBS_DIR
    return jobs_dir / f"{job_id}.yaml"


def create_default_config() -> None:
    """Create default configuration files if they don't exist."""
    ensure_config_dir()

    if not DEFAULT_CONFIG_FILE.exists():
        config = ProcClawConfig()
        with open(DEFAULT_CONFIG_FILE, "w") as f:
            yaml.safe_dump(
                config.model_dump(exclude_defaults=True),
                f,
                default_flow_style=False,
                sort_keys=False,
            )
        logger.info(f"Created default config at {DEFAULT_CONFIG_FILE}")

    # Create example job in jobs/ directory if no jobs exist
    existing_jobs = list(DEFAULT_JOBS_DIR.glob("*.yaml"))
    if not existing_jobs:
        example_job = {
            "name": "Example Job",
            "description": "An example job to get started",
            "cmd": "echo 'Hello from ProcClaw!'",
            "type": "manual",
            "enabled": False,
        }
        example_file = DEFAULT_JOBS_DIR / "example-job.yaml"
        with open(example_file, "w") as f:
            yaml.dump(example_job, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        logger.info(f"Created example job at {example_file}")
