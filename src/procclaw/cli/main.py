"""ProcClaw CLI application."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from procclaw import __version__
from procclaw.config import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_JOBS_FILE,
    DEFAULT_LOGS_DIR,
    DEFAULT_PID_FILE,
    create_default_config,
    ensure_config_dir,
    load_config,
    load_jobs,
)
from procclaw.core.supervisor import Supervisor
from procclaw.db import Database

# Initialize
app = typer.Typer(
    name="procclaw",
    help="ProcClaw - Process Manager for OpenClaw",
    no_args_is_help=True,
)
console = Console()

# Sub-commands
daemon_app = typer.Typer(help="Daemon management commands")
app.add_typer(daemon_app, name="daemon")

secret_app = typer.Typer(help="Secret management commands")
app.add_typer(secret_app, name="secret")

service_app = typer.Typer(help="System service management commands")
app.add_typer(service_app, name="service")


def setup_logging(verbose: bool = False) -> None:
    """Setup logging configuration."""
    logger.remove()

    level = "DEBUG" if verbose else "INFO"

    # Console logging
    logger.add(
        sys.stderr,
        format="<level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level=level,
        colorize=True,
    )


def get_daemon_pid() -> int | None:
    """Get the PID of the running daemon."""
    if not DEFAULT_PID_FILE.exists():
        return None

    try:
        pid = int(DEFAULT_PID_FILE.read_text().strip())
        # Check if process is actually running
        if Supervisor.check_pid(pid):
            return pid
        else:
            # Stale PID file
            DEFAULT_PID_FILE.unlink()
            return None
    except (ValueError, FileNotFoundError):
        return None


def write_daemon_pid(pid: int) -> None:
    """Write the daemon PID to file."""
    ensure_config_dir()
    DEFAULT_PID_FILE.write_text(str(pid))


def remove_daemon_pid() -> None:
    """Remove the daemon PID file."""
    if DEFAULT_PID_FILE.exists():
        DEFAULT_PID_FILE.unlink()


# ============================================================================
# Daemon Commands
# ============================================================================


@daemon_app.command("start")
def daemon_start(
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in foreground"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
) -> None:
    """Start the ProcClaw daemon."""
    setup_logging(verbose)

    # Check if already running
    pid = get_daemon_pid()
    if pid:
        console.print(f"[yellow]Daemon is already running (PID: {pid})[/yellow]")
        raise typer.Exit(1)

    # Ensure config exists
    create_default_config()

    if foreground:
        # Run in foreground
        _run_daemon()
    else:
        # Fork and run in background
        console.print("[blue]Starting daemon in background...[/blue]")

        pid = os.fork()
        if pid > 0:
            # Parent process
            # Wait a moment to check if it started successfully
            import time

            time.sleep(0.5)

            daemon_pid = get_daemon_pid()
            if daemon_pid:
                console.print(f"[green]✓ Daemon started (PID: {daemon_pid})[/green]")
            else:
                console.print("[red]✗ Daemon failed to start. Check logs.[/red]")
                raise typer.Exit(1)
        else:
            # Child process - become daemon
            os.setsid()

            # Fork again to prevent zombie processes
            pid = os.fork()
            if pid > 0:
                os._exit(0)

            # Set up daemon environment
            os.chdir("/")
            os.umask(0)

            # Redirect standard file descriptors
            log_file = DEFAULT_LOGS_DIR / "daemon.log"
            ensure_config_dir()

            sys.stdout.flush()
            sys.stderr.flush()

            with open("/dev/null", "r") as f:
                os.dup2(f.fileno(), sys.stdin.fileno())

            with open(log_file, "a") as f:
                os.dup2(f.fileno(), sys.stdout.fileno())
                os.dup2(f.fileno(), sys.stderr.fileno())

            # Run daemon
            _run_daemon()


def _run_daemon() -> None:
    """Run the daemon process."""
    # Write PID file
    write_daemon_pid(os.getpid())

    try:
        config = load_config()
        jobs = load_jobs()
        db = Database()

        supervisor = Supervisor(config=config, jobs=jobs, db=db)

        # Setup signal handlers
        def handle_signal(signum: int, frame: object) -> None:
            logger.info(f"Received signal {signum}")
            supervisor.shutdown()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGHUP, lambda s, f: supervisor.reload_jobs())

        # Run supervisor
        asyncio.run(supervisor.run())

    except Exception as e:
        logger.error(f"Daemon error: {e}")
        raise
    finally:
        remove_daemon_pid()


@daemon_app.command("stop")
def daemon_stop(
    timeout: int = typer.Option(60, "--timeout", "-t", help="Shutdown timeout in seconds"),
) -> None:
    """Stop the ProcClaw daemon."""
    pid = get_daemon_pid()
    if not pid:
        console.print("[yellow]Daemon is not running[/yellow]")
        raise typer.Exit(1)

    console.print(f"[blue]Stopping daemon (PID: {pid})...[/blue]")

    # Send SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        console.print("[yellow]Daemon process not found[/yellow]")
        remove_daemon_pid()
        return

    # Wait for shutdown
    import time

    start = time.time()
    while time.time() - start < timeout:
        if not Supervisor.check_pid(pid):
            console.print("[green]✓ Daemon stopped[/green]")
            remove_daemon_pid()
            return
        time.sleep(0.5)

    # Force kill
    console.print("[yellow]Daemon did not stop gracefully, force killing...[/yellow]")
    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
        console.print("[green]✓ Daemon killed[/green]")
    except ProcessLookupError:
        pass
    finally:
        remove_daemon_pid()


@daemon_app.command("status")
def daemon_status() -> None:
    """Show daemon status."""
    pid = get_daemon_pid()

    if pid:
        console.print(f"[green]● Daemon is running (PID: {pid})[/green]")

        # Show some stats
        try:
            import psutil

            proc = psutil.Process(pid)
            create_time = datetime.fromtimestamp(proc.create_time())
            uptime = datetime.now() - create_time
            mem = proc.memory_info().rss / (1024 * 1024)

            console.print(f"  Uptime: {uptime}")
            console.print(f"  Memory: {mem:.1f} MB")
        except Exception:
            pass

        # Show running jobs
        try:
            db = Database()
            states = db.get_all_states()
            running = [s for s in states.values() if s.status.value == "running"]
            console.print(f"  Running jobs: {len(running)}")
        except Exception:
            pass
    else:
        console.print("[red]○ Daemon is not running[/red]")


@daemon_app.command("logs")
def daemon_logs(
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    lines: int = typer.Option(50, "--lines", "-n", help="Number of lines to show"),
) -> None:
    """Show daemon logs."""
    log_file = DEFAULT_LOGS_DIR / "daemon.log"

    if not log_file.exists():
        console.print("[yellow]No daemon logs found[/yellow]")
        return

    if follow:
        # Use tail -f
        os.execvp("tail", ["tail", "-f", str(log_file)])
    else:
        # Show last N lines
        os.execvp("tail", ["tail", "-n", str(lines), str(log_file)])


# ============================================================================
# Job Commands
# ============================================================================


@app.command("list")
def list_jobs(
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status"),
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Filter by tag"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show more details"),
) -> None:
    """List all jobs."""
    setup_logging(verbose)

    # Try API first for live data
    daemon_pid = get_daemon_pid()
    jobs_data = None

    if daemon_pid:
        from procclaw.cli.client import APIClient

        try:
            with APIClient() as client:
                if client.is_daemon_running():
                    jobs_data = client.list_jobs(status=status, tag=tag)
        except Exception:
            pass  # Fall back to direct DB access

    if jobs_data is not None:
        # Use API data
        if not jobs_data:
            console.print("[yellow]No jobs match the filter[/yellow]")
            return

        table = Table(title="Jobs")
        table.add_column("Job", style="cyan")
        table.add_column("Type")
        table.add_column("Status")
        table.add_column("Last Run")
        table.add_column("Restarts", justify="right")

        for job in jobs_data:
            job_status = job["status"]
            status_str = job_status
            if job_status == "running":
                status_str = f"[green]{job_status}[/green]"
            elif job_status == "failed":
                status_str = f"[red]{job_status}[/red]"
            elif job_status == "stopped":
                status_str = f"[dim]{job_status}[/dim]"

            if not job["enabled"]:
                status_str = "[dim]disabled[/dim]"

            last_run_str = "-"
            if job.get("started_at"):
                last_run_str = job["started_at"][:16].replace("T", " ")

            table.add_row(
                job["id"],
                job["type"],
                status_str,
                last_run_str,
                str(job.get("restart_count", 0)),
            )

        console.print(table)
        return

    # Fallback to direct DB access
    try:
        jobs = load_jobs()
        db = Database()
        states = db.get_all_states()
    except Exception as e:
        console.print(f"[red]Error loading jobs: {e}[/red]")
        raise typer.Exit(1)

    if not jobs.jobs:
        console.print("[yellow]No jobs configured[/yellow]")
        console.print(f"Add jobs to: {DEFAULT_JOBS_FILE}")
        return

    # Create table
    table = Table(title="Jobs")
    table.add_column("Job", style="cyan")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Last Run")
    table.add_column("Restarts", justify="right")

    for job_id, job in jobs.jobs.items():
        # Apply filters
        if tag and tag not in job.tags:
            continue

        state = states.get(job_id)
        job_status = state.status.value if state else "unknown"

        if status and job_status != status:
            continue

        # Format status with color
        status_str = job_status
        if job_status == "running":
            status_str = f"[green]{job_status}[/green]"
        elif job_status == "failed":
            status_str = f"[red]{job_status}[/red]"
        elif job_status == "stopped":
            status_str = f"[dim]{job_status}[/dim]"

        if not job.enabled:
            status_str = f"[dim]disabled[/dim]"

        # Format last run
        last_run_str = "-"
        if state and state.started_at:
            last_run_str = state.started_at.strftime("%Y-%m-%d %H:%M")

        table.add_row(
            job_id,
            job.type.value,
            status_str,
            last_run_str,
            str(state.restart_count if state else 0),
        )

    console.print(table)


def sync_job_state(db: Database, job_id: str, exit_code: int | None = None) -> None:
    """Sync job state with actual process status."""
    state = db.get_state(job_id)
    if not state or state.status.value != "running" or not state.pid:
        return

    # Check if process is actually running
    if not Supervisor.check_pid(state.pid):
        # Process died, update state
        from procclaw.models import JobStatus

        now = datetime.now()
        duration = (now - state.started_at).total_seconds() if state.started_at else None

        # Determine status based on exit code
        if exit_code is not None:
            state.status = JobStatus.STOPPED if exit_code == 0 else JobStatus.FAILED
            state.last_exit_code = exit_code
        else:
            state.status = JobStatus.STOPPED

        state.stopped_at = now
        db.save_state(state)

        # Update last run record
        last_run = db.get_last_run(job_id)
        if last_run and last_run.finished_at is None:
            last_run.finished_at = now
            last_run.duration_seconds = duration
            last_run.exit_code = exit_code
            db.update_run(last_run)


@app.command("status")
def job_status(
    job_id: str = typer.Argument(..., help="Job ID"),
) -> None:
    """Show detailed status of a job."""
    try:
        jobs = load_jobs()
        db = Database()
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    job = jobs.get_job(job_id)
    if not job:
        console.print(f"[red]Job '{job_id}' not found[/red]")
        raise typer.Exit(1)

    # Sync state with actual process
    sync_job_state(db, job_id)

    state = db.get_state(job_id)
    last_run = db.get_last_run(job_id)
    stats = db.get_job_stats(job_id)

    # Build status display
    table = Table(title=job_id, show_header=False, box=None)
    table.add_column("Key", style="dim")
    table.add_column("Value")

    table.add_row("Name", job.name)
    table.add_row("Description", job.description or "-")
    table.add_row("Type", job.type.value)
    table.add_row("Enabled", "✓" if job.enabled else "✗")

    if state:
        status_str = state.status.value
        if state.status.value == "running":
            status_str = f"[green]{status_str}[/green]"
        elif state.status.value == "failed":
            status_str = f"[red]{status_str}[/red]"
        table.add_row("Status", status_str)

        if state.pid:
            table.add_row("PID", str(state.pid))

        if state.started_at:
            uptime = datetime.now() - state.started_at
            table.add_row("Started", state.started_at.strftime("%Y-%m-%d %H:%M:%S"))
            if state.status.value == "running":
                table.add_row("Uptime", str(uptime).split(".")[0])

        table.add_row("Restarts", str(state.restart_count))

        if state.last_exit_code is not None:
            table.add_row("Last Exit Code", str(state.last_exit_code))

        if state.last_error:
            table.add_row("Last Error", f"[red]{state.last_error}[/red]")
    else:
        table.add_row("Status", "[dim]never run[/dim]")

    # Stats
    if stats["total_runs"] > 0:
        table.add_row("", "")  # Separator
        table.add_row("Total Runs", str(stats["total_runs"]))
        table.add_row("Successful", str(stats["successful_runs"]))
        table.add_row("Failed", str(stats["failed_runs"]))
        if stats["avg_duration"]:
            table.add_row("Avg Duration", f"{stats['avg_duration']:.1f}s")

    console.print(table)


@app.command("start")
def start_job(
    job_id: str = typer.Argument(..., help="Job ID"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for job to complete (manual jobs)"),
) -> None:
    """Start a job."""
    setup_logging(verbose)

    # Check if daemon is running and use API
    pid = get_daemon_pid()
    if pid:
        from procclaw.cli.client import APIClient

        try:
            with APIClient() as client:
                if client.is_daemon_running():
                    result = client.start_job(job_id)
                    console.print(f"[green]✓ {result['message']}[/green]")
                    if result.get("pid"):
                        console.print(f"[dim]PID: {result['pid']}[/dim]")
                    return
        except Exception as e:
            console.print(f"[yellow]Warning: Could not connect to daemon API: {e}[/yellow]")
            console.print("[dim]Starting job directly...[/dim]")

    # Fallback to direct execution
    try:
        config = load_config()
        jobs = load_jobs()
        db = Database()

        supervisor = Supervisor(config=config, jobs=jobs, db=db)

        if not supervisor.start_job(job_id):
            console.print(f"[red]✗ Failed to start job '{job_id}'[/red]")
            raise typer.Exit(1)

        console.print(f"[green]✓ Started job '{job_id}'[/green]")

        # Get the process handle
        job = jobs.get_job(job_id)
        if not job:
            return

        # For continuous jobs, don't wait
        if job.type.value == "continuous" or not wait:
            state = db.get_state(job_id)
            if state and state.pid:
                console.print(f"[dim]PID: {state.pid}[/dim]")
            return

        # Wait for job to complete (manual/scheduled)
        console.print("[dim]Waiting for job to complete...[/dim]")

        # Wait using supervisor's wait method to capture exit code
        exit_code = supervisor.wait_for_job(job_id)

        if exit_code is None:
            # Fallback: check state from DB
            state = db.get_state(job_id)
            if state and state.last_exit_code is not None:
                exit_code = state.last_exit_code

        # Get duration from last run
        last_run = db.get_last_run(job_id)
        duration_str = ""
        if last_run and last_run.duration_seconds:
            duration_str = f" ({last_run.duration_seconds:.1f}s)"

        if exit_code == 0:
            console.print(f"[green]✓ Job completed successfully{duration_str}[/green]")
        elif exit_code is not None:
            console.print(f"[red]✗ Job failed (exit code: {exit_code})[/red]")
            raise typer.Exit(1)
        else:
            console.print("[green]✓ Job completed[/green]")

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command("stop")
def stop_job(
    job_id: str = typer.Argument(..., help="Job ID"),
    force: bool = typer.Option(False, "--force", "-f", help="Force kill"),
) -> None:
    """Stop a running job."""
    # Try API first
    pid = get_daemon_pid()
    if pid:
        from procclaw.cli.client import APIClient

        try:
            with APIClient() as client:
                if client.is_daemon_running():
                    result = client.stop_job(job_id, force=force)
                    console.print(f"[green]✓ {result['message']}[/green]")
                    return
        except Exception as e:
            if "409" in str(e):
                console.print(f"[yellow]Job '{job_id}' is not running[/yellow]")
                return
            console.print(f"[yellow]Warning: Could not connect to daemon API: {e}[/yellow]")

    # Fallback to direct stop
    try:
        config = load_config()
        jobs = load_jobs()
        db = Database()

        # Check if job is running from state
        state = db.get_state(job_id)
        if not state or state.status.value != "running" or not state.pid:
            console.print(f"[yellow]Job '{job_id}' is not running[/yellow]")
            return

        # Stop via PID directly
        import signal as sig

        job_pid = state.pid
        try:
            if force:
                os.kill(job_pid, sig.SIGKILL)
            else:
                os.kill(job_pid, sig.SIGTERM)
            console.print(f"[green]✓ Stopped job '{job_id}' (PID: {job_pid})[/green]")
        except ProcessLookupError:
            console.print(f"[yellow]Process not found (PID: {job_pid})[/yellow]")

        # Update state
        from procclaw.models import JobStatus

        state.status = JobStatus.STOPPED
        state.stopped_at = datetime.now()
        db.save_state(state)

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command("restart")
def restart_job(
    job_id: str = typer.Argument(..., help="Job ID"),
) -> None:
    """Restart a job."""
    stop_job(job_id, force=False)
    import time

    time.sleep(1)
    start_job(job_id)


@app.command("logs")
def job_logs(
    job_id: str = typer.Argument(..., help="Job ID"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    lines: int = typer.Option(100, "--lines", "-n", help="Number of lines to show"),
    error: bool = typer.Option(False, "--error", "-e", help="Show error log instead"),
) -> None:
    """Show job logs."""
    jobs = load_jobs()
    job = jobs.get_job(job_id)

    if not job:
        console.print(f"[red]Job '{job_id}' not found[/red]")
        raise typer.Exit(1)

    if error:
        log_file = job.get_log_stderr_path(DEFAULT_CONFIG_DIR, job_id)
    else:
        log_file = job.get_log_stdout_path(DEFAULT_CONFIG_DIR, job_id)

    if not log_file.exists():
        console.print(f"[yellow]No logs found at {log_file}[/yellow]")
        return

    if follow:
        os.execvp("tail", ["tail", "-f", str(log_file)])
    else:
        os.execvp("tail", ["tail", "-n", str(lines), str(log_file)])


# ============================================================================
# Config Commands
# ============================================================================


@app.command("config")
def show_config(
    validate: bool = typer.Option(False, "--validate", help="Validate configuration"),
) -> None:
    """Show or validate configuration."""
    if validate:
        try:
            load_config()
            jobs = load_jobs()
            console.print("[green]✓ Configuration is valid[/green]")
            console.print(f"  {len(jobs.jobs)} jobs configured")
        except Exception as e:
            console.print(f"[red]✗ Configuration error: {e}[/red]")
            raise typer.Exit(1)
    else:
        console.print(f"[dim]Config directory:[/dim] {DEFAULT_CONFIG_DIR}")
        console.print(f"[dim]Jobs file:[/dim] {DEFAULT_JOBS_FILE}")

        if DEFAULT_JOBS_FILE.exists():
            console.print("\n[dim]Jobs:[/dim]")
            jobs = load_jobs()
            for job_id in jobs.jobs:
                console.print(f"  - {job_id}")


@app.command("init")
def init_config() -> None:
    """Initialize ProcClaw configuration."""
    create_default_config()
    console.print(f"[green]✓ Created configuration at {DEFAULT_CONFIG_DIR}[/green]")
    console.print(f"\nEdit {DEFAULT_JOBS_FILE} to add your jobs.")


@app.command("version")
def version() -> None:
    """Show version."""
    console.print(f"ProcClaw v{__version__}")


# ============================================================================
# Secret Commands
# ============================================================================


@secret_app.command("set")
def secret_set(
    name: str = typer.Argument(..., help="Secret name"),
    value: str = typer.Argument(..., help="Secret value"),
) -> None:
    """Store a secret in the system keychain."""
    from procclaw.secrets import set_secret

    if set_secret(name, value):
        console.print(f"[green]✓ Secret '{name}' stored[/green]")
    else:
        console.print(f"[red]✗ Failed to store secret '{name}'[/red]")
        raise typer.Exit(1)


@secret_app.command("get")
def secret_get(
    name: str = typer.Argument(..., help="Secret name"),
    show: bool = typer.Option(False, "--show", help="Show the actual value"),
) -> None:
    """Get a secret from the system keychain."""
    from procclaw.secrets import get_secret

    value = get_secret(name)
    if value is None:
        console.print(f"[yellow]Secret '{name}' not found[/yellow]")
        raise typer.Exit(1)

    if show:
        console.print(value)
    else:
        masked = value[:2] + "*" * (len(value) - 4) + value[-2:] if len(value) > 4 else "****"
        console.print(f"[dim]{name}[/dim] = {masked}")


@secret_app.command("list")
def secret_list() -> None:
    """List all stored secrets."""
    from procclaw.secrets import list_secrets

    secrets = list_secrets()
    if not secrets:
        console.print("[dim]No secrets stored[/dim]")
        return

    console.print("[bold]Stored secrets:[/bold]")
    for name in secrets:
        console.print(f"  • {name}")


@secret_app.command("delete")
def secret_delete(
    name: str = typer.Argument(..., help="Secret name"),
) -> None:
    """Delete a secret from the system keychain."""
    from procclaw.secrets import delete_secret

    if delete_secret(name):
        console.print(f"[green]✓ Secret '{name}' deleted[/green]")
    else:
        console.print(f"[yellow]Secret '{name}' not found or could not be deleted[/yellow]")


# ============================================================================
# Service Commands
# ============================================================================


@service_app.command("install")
def service_install_cmd() -> None:
    """Install ProcClaw as a system service (launchd on macOS, systemd on Linux)."""
    from procclaw.service import service_install

    if service_install():
        console.print("[green]✓ Service installed[/green]")
        console.print("[dim]ProcClaw will start automatically on login.[/dim]")
    else:
        console.print("[red]✗ Failed to install service[/red]")
        raise typer.Exit(1)


@service_app.command("uninstall")
def service_uninstall_cmd() -> None:
    """Uninstall the ProcClaw system service."""
    from procclaw.service import service_uninstall

    if service_uninstall():
        console.print("[green]✓ Service uninstalled[/green]")
    else:
        console.print("[red]✗ Failed to uninstall service[/red]")
        raise typer.Exit(1)


@service_app.command("status")
def service_status_cmd() -> None:
    """Show system service status."""
    from procclaw.service import service_status

    status = service_status()

    if not status.get("installed"):
        if "error" in status:
            console.print(f"[yellow]{status['error']}[/yellow]")
        else:
            console.print("[dim]Service is not installed[/dim]")
            console.print("Run [bold]procclaw service install[/bold] to install.")
        return

    if status.get("running"):
        pid = status.get("pid", "unknown")
        console.print(f"[green]● Service is running (PID: {pid})[/green]")
    else:
        console.print("[yellow]○ Service is installed but not running[/yellow]")


# ============================================================================
# Entry Point
# ============================================================================


def main() -> None:
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
