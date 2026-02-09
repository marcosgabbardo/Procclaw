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
DEFAULT_JOBS_FILE = DEFAULT_CONFIG_DIR / "jobs.yaml"
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
    return DEFAULT_CONFIG_DIR


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
    """Load the jobs configuration."""
    path = jobs_path or DEFAULT_JOBS_FILE

    if not path.exists():
        logger.warning(f"Jobs file not found at {path}")
        return JobsConfig()

    data = load_yaml_file(path)

    # Expand paths and env vars in job configs
    if "jobs" in data:
        for job_id, job_data in data["jobs"].items():
            if "cwd" in job_data:
                job_data["cwd"] = expand_path(job_data["cwd"])
            if "cmd" in job_data:
                job_data["cmd"] = expand_env_vars(job_data["cmd"])
            if "env" in job_data:
                job_data["env"] = {
                    k: expand_env_vars(str(v)) for k, v in job_data["env"].items()
                }

    try:
        jobs = JobsConfig.model_validate(data)
        logger.debug(f"Loaded {len(jobs.jobs)} jobs from {path}")

        # Validate jobs
        _validate_jobs(jobs)

        return jobs
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Invalid jobs file {path}: {e}") from e


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
    """Save jobs configuration to file."""
    path = jobs_path or DEFAULT_JOBS_FILE
    ensure_config_dir()

    data = {"jobs": {}}
    for job_id, job in jobs.jobs.items():
        job_dict = job.model_dump(exclude_defaults=True)
        data["jobs"][job_id] = job_dict

    with open(path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    logger.info(f"Saved {len(jobs.jobs)} jobs to {path}")


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

    if not DEFAULT_JOBS_FILE.exists():
        example_jobs = {
            "jobs": {
                "example-job": {
                    "name": "Example Job",
                    "description": "An example job to get started",
                    "cmd": "echo 'Hello from ProcClaw!'",
                    "type": "manual",
                    "enabled": False,
                }
            }
        }
        with open(DEFAULT_JOBS_FILE, "w") as f:
            yaml.safe_dump(example_jobs, f, default_flow_style=False, sort_keys=False)
        logger.info(f"Created example jobs at {DEFAULT_JOBS_FILE}")
