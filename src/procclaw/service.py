"""System service management for ProcClaw."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger

# Service configuration
SERVICE_NAME = "com.openclaw.procclaw"
LAUNCHD_PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCHD_PLIST_PATH = LAUNCHD_PLIST_DIR / f"{SERVICE_NAME}.plist"

# Systemd user service dir (Linux)
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_SERVICE_PATH = SYSTEMD_USER_DIR / "procclaw.service"


def _get_procclaw_path() -> str:
    """Get the path to the procclaw executable."""
    # Try to find procclaw in PATH
    import shutil
    procclaw_path = shutil.which("procclaw")
    if procclaw_path:
        return procclaw_path

    # Fall back to Python module execution
    return f"{sys.executable} -m procclaw.cli.main"


def _is_macos() -> bool:
    """Check if running on macOS."""
    return sys.platform == "darwin"


def _is_linux() -> bool:
    """Check if running on Linux."""
    return sys.platform == "linux"


# macOS launchd functions

def _generate_launchd_plist() -> str:
    """Generate launchd plist content."""
    from procclaw.config import DEFAULT_LOGS_DIR

    procclaw_path = _get_procclaw_path()
    logs_dir = DEFAULT_LOGS_DIR

    # Handle both direct executable and python module execution
    if " " in procclaw_path:
        # Python module execution
        parts = procclaw_path.split()
        program_args = "\n".join(f"        <string>{p}</string>" for p in parts)
        program_args += "\n        <string>daemon</string>\n        <string>run</string>"
    else:
        program_args = f"""        <string>{procclaw_path}</string>
        <string>daemon</string>
        <string>run</string>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{SERVICE_NAME}</string>

    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{logs_dir}/daemon.log</string>

    <key>StandardErrorPath</key>
    <string>{logs_dir}/daemon.error.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


def launchd_install() -> bool:
    """Install the launchd service on macOS."""
    if not _is_macos():
        logger.error("launchd is only available on macOS")
        return False

    # Create directory if needed
    LAUNCHD_PLIST_DIR.mkdir(parents=True, exist_ok=True)

    # Generate and write plist
    plist_content = _generate_launchd_plist()
    LAUNCHD_PLIST_PATH.write_text(plist_content)

    logger.info(f"Created plist at {LAUNCHD_PLIST_PATH}")

    # Load the service
    result = subprocess.run(
        ["launchctl", "load", str(LAUNCHD_PLIST_PATH)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error(f"Failed to load service: {result.stderr}")
        return False

    logger.info("Service installed and loaded")
    return True


def launchd_uninstall() -> bool:
    """Uninstall the launchd service on macOS."""
    if not _is_macos():
        logger.error("launchd is only available on macOS")
        return False

    if not LAUNCHD_PLIST_PATH.exists():
        logger.warning("Service is not installed")
        return True

    # Unload the service
    result = subprocess.run(
        ["launchctl", "unload", str(LAUNCHD_PLIST_PATH)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(f"Failed to unload service: {result.stderr}")

    # Remove plist
    LAUNCHD_PLIST_PATH.unlink()
    logger.info("Service uninstalled")
    return True


def launchd_status() -> dict:
    """Get launchd service status."""
    if not _is_macos():
        return {"installed": False, "error": "Not on macOS"}

    if not LAUNCHD_PLIST_PATH.exists():
        return {"installed": False}

    # Check if service is running
    result = subprocess.run(
        ["launchctl", "list", SERVICE_NAME],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return {"installed": True, "running": False}

    # Parse output
    lines = result.stdout.strip().split("\n")
    if len(lines) >= 2:
        parts = lines[1].split("\t")
        if len(parts) >= 3:
            pid = parts[0] if parts[0] != "-" else None
            status = parts[1]
            return {
                "installed": True,
                "running": pid is not None,
                "pid": int(pid) if pid else None,
                "status": status,
            }

    return {"installed": True, "running": False}


# Linux systemd functions

def _generate_systemd_unit() -> str:
    """Generate systemd unit file content."""
    procclaw_path = _get_procclaw_path()

    return f"""[Unit]
Description=ProcClaw Process Manager
After=network.target

[Service]
ExecStart={procclaw_path} daemon run
Restart=always
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
"""


def systemd_install() -> bool:
    """Install the systemd user service on Linux."""
    if not _is_linux():
        logger.error("systemd user service is only available on Linux")
        return False

    # Create directory if needed
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    # Generate and write unit file
    unit_content = _generate_systemd_unit()
    SYSTEMD_SERVICE_PATH.write_text(unit_content)

    logger.info(f"Created service at {SYSTEMD_SERVICE_PATH}")

    # Reload systemd
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    # Enable the service
    result = subprocess.run(
        ["systemctl", "--user", "enable", "procclaw.service"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error(f"Failed to enable service: {result.stderr}")
        return False

    # Start the service
    result = subprocess.run(
        ["systemctl", "--user", "start", "procclaw.service"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error(f"Failed to start service: {result.stderr}")
        return False

    logger.info("Service installed, enabled, and started")
    return True


def systemd_uninstall() -> bool:
    """Uninstall the systemd user service on Linux."""
    if not _is_linux():
        logger.error("systemd user service is only available on Linux")
        return False

    if not SYSTEMD_SERVICE_PATH.exists():
        logger.warning("Service is not installed")
        return True

    # Stop the service
    subprocess.run(
        ["systemctl", "--user", "stop", "procclaw.service"],
        capture_output=True,
    )

    # Disable the service
    subprocess.run(
        ["systemctl", "--user", "disable", "procclaw.service"],
        capture_output=True,
    )

    # Remove unit file
    SYSTEMD_SERVICE_PATH.unlink()

    # Reload systemd
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    logger.info("Service uninstalled")
    return True


def systemd_status() -> dict:
    """Get systemd service status."""
    if not _is_linux():
        return {"installed": False, "error": "Not on Linux"}

    if not SYSTEMD_SERVICE_PATH.exists():
        return {"installed": False}

    # Check status
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "procclaw.service"],
        capture_output=True,
        text=True,
    )

    is_active = result.stdout.strip() == "active"

    # Get more details
    result = subprocess.run(
        ["systemctl", "--user", "show", "procclaw.service", "--property=MainPID"],
        capture_output=True,
        text=True,
    )

    pid = None
    if "MainPID=" in result.stdout:
        try:
            pid = int(result.stdout.split("=")[1].strip())
            if pid == 0:
                pid = None
        except ValueError:
            pass

    return {
        "installed": True,
        "running": is_active,
        "pid": pid,
    }


# Public API

def service_install() -> bool:
    """Install the system service."""
    if _is_macos():
        return launchd_install()
    elif _is_linux():
        return systemd_install()
    else:
        logger.error(f"Unsupported platform: {sys.platform}")
        return False


def service_uninstall() -> bool:
    """Uninstall the system service."""
    if _is_macos():
        return launchd_uninstall()
    elif _is_linux():
        return systemd_uninstall()
    else:
        logger.error(f"Unsupported platform: {sys.platform}")
        return False


def service_status() -> dict:
    """Get the system service status."""
    if _is_macos():
        return launchd_status()
    elif _is_linux():
        return systemd_status()
    else:
        return {"installed": False, "error": f"Unsupported platform: {sys.platform}"}
