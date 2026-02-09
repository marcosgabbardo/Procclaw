"""Secrets management using system keychain."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger

from procclaw.config import DEFAULT_CONFIG_DIR

# Service name for keychain entries
KEYCHAIN_SERVICE = "procclaw"

# Fallback secrets file
SECRETS_FILE = DEFAULT_CONFIG_DIR / ".secrets"


def _is_macos() -> bool:
    """Check if running on macOS."""
    return sys.platform == "darwin"


def _is_linux() -> bool:
    """Check if running on Linux."""
    return sys.platform == "linux"


def _run_command(cmd: list[str], input_data: str | None = None) -> tuple[bool, str]:
    """Run a shell command and return (success, output)."""
    try:
        result = subprocess.run(
            cmd,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except Exception as e:
        return False, str(e)


# macOS Keychain functions

def _keychain_set(name: str, value: str) -> bool:
    """Store a secret in macOS Keychain."""
    # Delete existing entry first (ignore errors)
    subprocess.run(
        ["security", "delete-generic-password", "-a", KEYCHAIN_SERVICE, "-s", name],
        capture_output=True,
    )

    # Add new entry
    success, _ = _run_command([
        "security", "add-generic-password",
        "-a", KEYCHAIN_SERVICE,
        "-s", name,
        "-w", value,
    ])
    return success


def _keychain_get(name: str) -> str | None:
    """Get a secret from macOS Keychain."""
    success, output = _run_command([
        "security", "find-generic-password",
        "-a", KEYCHAIN_SERVICE,
        "-s", name,
        "-w",
    ])
    return output if success else None


def _keychain_delete(name: str) -> bool:
    """Delete a secret from macOS Keychain."""
    success, _ = _run_command([
        "security", "delete-generic-password",
        "-a", KEYCHAIN_SERVICE,
        "-s", name,
    ])
    return success


def _keychain_list() -> list[str]:
    """List all secrets in macOS Keychain for this service."""
    success, output = _run_command([
        "security", "dump-keychain",
    ])

    if not success:
        return []

    # Parse output to find our entries
    # This is a bit hacky but works
    secrets = []
    lines = output.split("\n")
    current_service = None

    for line in lines:
        if '"svce"<blob>=' in line:
            # Extract service name
            try:
                start = line.index('"', line.index("=")) + 1
                end = line.index('"', start)
                current_service = line[start:end]
            except ValueError:
                current_service = None
        elif '"acct"<blob>=' in line and current_service == KEYCHAIN_SERVICE:
            # Extract account (secret name)
            try:
                start = line.index('"', line.index("=")) + 1
                end = line.index('"', start)
                secrets.append(line[start:end])
            except ValueError:
                pass

    return secrets


# Linux secret-tool functions (libsecret)

def _secret_tool_set(name: str, value: str) -> bool:
    """Store a secret using secret-tool (Linux)."""
    success, _ = _run_command(
        ["secret-tool", "store", "--label", f"procclaw:{name}", "service", KEYCHAIN_SERVICE, "key", name],
        input_data=value,
    )
    return success


def _secret_tool_get(name: str) -> str | None:
    """Get a secret using secret-tool (Linux)."""
    success, output = _run_command([
        "secret-tool", "lookup", "service", KEYCHAIN_SERVICE, "key", name,
    ])
    return output if success else None


def _secret_tool_delete(name: str) -> bool:
    """Delete a secret using secret-tool (Linux)."""
    success, _ = _run_command([
        "secret-tool", "clear", "service", KEYCHAIN_SERVICE, "key", name,
    ])
    return success


def _secret_tool_list() -> list[str]:
    """List secrets using secret-tool (Linux)."""
    # secret-tool doesn't have a list command, so we use a fallback
    # For now, return empty list - would need dbus to enumerate
    return []


# Fallback file-based secrets

def _file_set(name: str, value: str) -> bool:
    """Store a secret in the fallback file."""
    secrets = _file_load()
    secrets[name] = value
    return _file_save(secrets)


def _file_get(name: str) -> str | None:
    """Get a secret from the fallback file."""
    secrets = _file_load()
    return secrets.get(name)


def _file_delete(name: str) -> bool:
    """Delete a secret from the fallback file."""
    secrets = _file_load()
    if name in secrets:
        del secrets[name]
        return _file_save(secrets)
    return True


def _file_list() -> list[str]:
    """List secrets in the fallback file."""
    return list(_file_load().keys())


def _file_load() -> dict[str, str]:
    """Load secrets from the fallback file."""
    if not SECRETS_FILE.exists():
        return {}

    secrets = {}
    try:
        with open(SECRETS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line and "=" in line:
                    name, value = line.split("=", 1)
                    secrets[name] = value
    except Exception as e:
        logger.error(f"Failed to load secrets file: {e}")

    return secrets


def _file_save(secrets: dict[str, str]) -> bool:
    """Save secrets to the fallback file."""
    try:
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SECRETS_FILE, "w") as f:
            for name, value in secrets.items():
                f.write(f"{name}={value}\n")
        # Set restrictive permissions
        os.chmod(SECRETS_FILE, 0o600)
        return True
    except Exception as e:
        logger.error(f"Failed to save secrets file: {e}")
        return False


# Public API

def set_secret(name: str, value: str) -> bool:
    """Store a secret.

    Args:
        name: Secret name
        value: Secret value

    Returns:
        True if stored successfully
    """
    if _is_macos():
        if _keychain_set(name, value):
            logger.debug(f"Stored secret '{name}' in Keychain")
            return True
        logger.warning(f"Keychain failed, using fallback file")

    if _is_linux():
        if _secret_tool_set(name, value):
            logger.debug(f"Stored secret '{name}' in secret-tool")
            return True
        logger.warning(f"secret-tool failed, using fallback file")

    # Fallback to file
    if _file_set(name, value):
        logger.debug(f"Stored secret '{name}' in fallback file")
        return True

    return False


def get_secret(name: str) -> str | None:
    """Get a secret.

    Args:
        name: Secret name

    Returns:
        Secret value or None if not found
    """
    if _is_macos():
        value = _keychain_get(name)
        if value is not None:
            return value

    if _is_linux():
        value = _secret_tool_get(name)
        if value is not None:
            return value

    # Fallback to file
    return _file_get(name)


def delete_secret(name: str) -> bool:
    """Delete a secret.

    Args:
        name: Secret name

    Returns:
        True if deleted successfully
    """
    success = False

    if _is_macos():
        success = _keychain_delete(name) or success

    if _is_linux():
        success = _secret_tool_delete(name) or success

    # Also delete from fallback file
    success = _file_delete(name) or success

    return success


def list_secrets() -> list[str]:
    """List all secret names.

    Returns:
        List of secret names
    """
    secrets = set()

    if _is_macos():
        secrets.update(_keychain_list())

    if _is_linux():
        secrets.update(_secret_tool_list())

    # Also include fallback file secrets
    secrets.update(_file_list())

    return sorted(secrets)


def resolve_secret_ref(value: str) -> str:
    """Resolve a secret reference in a config value.

    Handles ${secret:NAME} syntax.

    Args:
        value: Config value that may contain secret references

    Returns:
        Resolved value with secrets substituted
    """
    import re

    def replace_secret(match: re.Match[str]) -> str:
        secret_name = match.group(1)
        secret_value = get_secret(secret_name)
        if secret_value is None:
            logger.warning(f"Secret '{secret_name}' not found")
            return match.group(0)  # Return original if not found
        return secret_value

    return re.sub(r"\$\{secret:([^}]+)\}", replace_secret, value)
