# ProcClaw 🦞

![Dashboard](https://img.shields.io/badge/Web%20UI-Dark%20Mode-1f2937?style=flat-square)
![Jobs](https://img.shields.io/badge/Jobs-24%20managed-22c55e?style=flat-square)

**Process Manager for OpenClaw** - A robust, lightweight process manager with CLI, HTTP API, and deep OpenClaw integration.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Web UI

Access the dashboard at **http://localhost:9876** when the daemon is running.

### Features
- 📊 **Stats Header**: Running/failed/total jobs at a glance
- ⚙️ **Jobs Tab**: Full list with filtering (search, type, status, tags), sortable columns
- 📜 **Logs Tab**: Real-time log viewer with source indicator (📄 file / 💾 SQLite)
- 🔄 **Runs Tab**: Execution history with filters and details
- 💀 **DLQ Tab**: Dead Letter Queue management (view, reinject, purge)
- 🔧 **Config Tab**: View/edit jobs.yaml with YAML editor

### Job Management
- **Inline tag editing** (GitHub-style: click + to add, × to remove)
- **Edit modal** with form and YAML modes
- **Create jobs** via form or raw YAML
- **Force run** scheduled jobs with ⚡ button
- **Column customization** (persisted in localStorage)

### Keyboard Shortcuts
| Key | Action |
|-----|--------|
| `r` | Refresh |
| `1-5` | Switch tabs |
| `/` | Focus search |
| `Esc` | Close modal |

## Features

- 🔄 **Job Types**: Manual, Continuous, Scheduled (cron), Oneshot (run once at datetime)
- 🔗 **Dependencies**: Jobs can depend on other jobs (after_start, after_complete)
- 🏥 **Health Checks**: Process, HTTP endpoint, File heartbeat monitoring
- 🔁 **Retry Policy**: Webhook-style exponential backoff (0s → 30s → 5m → 15m → 1h)
- 🛑 **Graceful Shutdown**: Configurable grace period with SIGTERM → SIGKILL
- 🔐 **Secrets Management**: macOS Keychain / Linux libsecret integration
- 🚀 **System Service**: launchd (macOS) / systemd (Linux) auto-install
- 🌐 **HTTP API**: Full REST API with Prometheus metrics
- 📊 **Prometheus Metrics**: Built-in `/metrics` endpoint
- 🦞 **OpenClaw Integration**: Skill control, memory logging, alerts
- 🚀 **Auto-start**: Continuous jobs start automatically on daemon startup
- 💾 **SQLite Logs**: Logs saved to SQLite after completion (configurable: keep/delete files)
- ⏰ **Missed Run Catchup**: Scheduled jobs catch up if daemon was down (60 min grace period)
- 🛡️ **Crash Recovery**: Jobs complete even if daemon dies (heartbeat + completion markers)

### Enterprise Features

- 🚫 **Deduplication**: Prevent duplicate job runs within time window
- 🔒 **Distributed Locks**: Singleton jobs across instances (SQLite/file-based)
- 📥 **Priority Queue**: 4 priority levels (critical, high, normal, low)
- 💀 **Dead Letter Queue**: Failed jobs go to DLQ for inspection/reinjection
- 🎯 **Concurrency Control**: Max instances per job with overflow queuing
- 🔔 **Event Triggers**: Webhook and file watcher triggers

### Workflow Features

- ⏰ **ETA Scheduling**: Schedule jobs at specific datetime or delay
- ⛔ **Task Revocation**: Cancel queued/scheduled jobs, terminate running
- 📊 **Result Collection**: Capture stdout/stderr, exit codes, custom data
- 🔗 **Task Composition**: Chain (A→B→C), Group (A+B+C parallel), Chord (group + callback)

## Installation

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

### Install from source

```bash
# Clone the repository
git clone https://github.com/marcosgabbardo/Procclaw.git
cd Procclaw

# Install with uv (recommended)
uv venv
uv pip install -e .

# Or with pip
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Verify installation

```bash
procclaw version
# ProcClaw v0.1.0
```

### CLI-only (no Web UI)

If you only need the CLI without the web interface, you can run the daemon in headless mode:

```bash
# Start daemon without binding HTTP port
procclaw daemon start --no-api

# Or configure in procclaw.yaml
daemon:
  api_enabled: false
```

All CLI commands work without the web UI:

```bash
procclaw list              # List all jobs
procclaw start <job>       # Start a job
procclaw stop <job>        # Stop a job
procclaw logs <job> -f     # Follow logs
procclaw status <job>      # Check status
procclaw search "query"    # Search jobs
```

## Quick Start

### 1. Initialize configuration

```bash
procclaw init
```

This creates:
- `~/.procclaw/procclaw.yaml` - Main daemon configuration
- `~/.procclaw/jobs.yaml` - Job definitions

### 2. Define your first job

Edit `~/.procclaw/jobs.yaml`:

```yaml
jobs:
  hello-world:
    name: Hello World
    cmd: echo "Hello from ProcClaw!"
    type: manual

  my-service:
    name: My Service
    cmd: python -m http.server 8080
    cwd: ~/projects/myapp
    type: continuous
    health_check:
      type: http
      url: http://localhost:8080/
      interval: 30
```

### 3. Start the daemon

```bash
# Start in background
procclaw daemon start

# Or start in foreground (for debugging)
procclaw daemon start -f
```

### 4. Control your jobs

```bash
# List all jobs
procclaw list

# Start a job
procclaw start hello-world

# View logs
procclaw logs hello-world

# Follow logs in real-time
procclaw logs my-service -f

# Stop a job
procclaw stop my-service

# Check status
procclaw status my-service
```

## Configuration

### Main Config: `~/.procclaw/procclaw.yaml`

```yaml
daemon:
  host: 127.0.0.1      # API listen address
  port: 9876           # API port
  log_level: INFO      # DEBUG, INFO, WARNING, ERROR

api:
  auth:
    enabled: false     # Enable token authentication
    # token: "your-secret-token"

defaults:
  retry:
    preset: webhook    # 0s, 30s, 5m, 15m, 1h
  shutdown:
    grace_period: 60   # Seconds before SIGKILL
  health_check:
    type: process
    interval: 60

timezone: America/Sao_Paulo  # Default timezone for schedules

openclaw:
  enabled: true
  memory_logging: true
  alerts:
    enabled: true
    channels: [whatsapp]
```

### Jobs Config: `~/.procclaw/jobs.yaml`

```yaml
jobs:
  # ═══════════════════════════════════════════════════════════
  # SCHEDULED JOB - Runs on a cron schedule
  # ═══════════════════════════════════════════════════════════
  stock-scanner:
    name: Stock Scanner
    description: Scans for unusual volume patterns in small cap stocks
    cmd: python scanner.py
    cwd: ~/scripts/scanner
    type: scheduled
    schedule: "0 */12 * * *"      # Every 12 hours
    timezone: America/Sao_Paulo
    on_overlap: skip              # skip | queue | kill_restart
    env:
      SCAN_MODE: production
    tags: [trading, scanner, stocks]

  # ═══════════════════════════════════════════════════════════
  # CONTINUOUS JOB - Runs forever, auto-restarts on failure
  # ═══════════════════════════════════════════════════════════
  api-server:
    name: API Server
    cmd: uvicorn main:app --host 0.0.0.0 --port 8000
    cwd: ~/projects/api
    type: continuous
    health_check:
      type: http
      url: http://localhost:8000/health
      expected_status: 200
      timeout: 10
      interval: 30
    retry:
      preset: webhook             # Use default retry policy

  # ═══════════════════════════════════════════════════════════
  # MANUAL JOB - Runs only when explicitly triggered
  # ═══════════════════════════════════════════════════════════
  data-export:
    name: Data Export
    cmd: python export.py --format csv
    cwd: ~/projects/data
    type: manual
    timeout: 3600                 # Max 1 hour runtime

  # ═══════════════════════════════════════════════════════════
  # JOB WITH DEPENDENCIES
  # ═══════════════════════════════════════════════════════════
  data-processor:
    name: Data Processor
    cmd: python process.py
    type: manual
    depends_on:
      - job: api-server
        condition: after_start    # Wait for API to start
        timeout: 60
      - job: database
        condition: after_complete # Wait for DB migration

  # ═══════════════════════════════════════════════════════════
  # JOB WITH SECRETS
  # ═══════════════════════════════════════════════════════════
  notifier:
    name: Slack Notifier
    cmd: python notify.py
    type: manual
    env:
      SLACK_TOKEN: ${secret:SLACK_TOKEN}
      API_KEY: ${secret:NOTIFY_API_KEY}

  # ═══════════════════════════════════════════════════════════
  # DISABLED JOB (won't start automatically)
  # ═══════════════════════════════════════════════════════════
  deprecated-job:
    name: Old Job
    cmd: python old_script.py
    type: scheduled
    schedule: "0 0 * * *"
    enabled: false
```

## CLI Reference

### Daemon Management

```bash
# Start daemon in background
procclaw daemon start

# Start in foreground (see all output)
procclaw daemon start -f

# Stop daemon gracefully
procclaw daemon stop

# Check daemon status
procclaw daemon status

# View daemon logs
procclaw daemon logs

# Follow daemon logs
procclaw daemon logs -f
```

### Job Control

```bash
# List all jobs
procclaw list

# List only running jobs
procclaw list --status running

# List jobs by tag
procclaw list --tag trading

# Get detailed job status
procclaw status my-job

# Start a job
procclaw start my-job

# Stop a job (graceful)
procclaw stop my-job

# Force stop (immediate SIGKILL)
procclaw stop my-job --force

# Restart a job
procclaw restart my-job

# View job logs (last 100 lines)
procclaw logs my-job

# Follow logs in real-time
procclaw logs my-job -f

# Show last N lines
procclaw logs my-job -n 50
```

### Secrets Management

ProcClaw stores secrets in the system keychain (macOS Keychain / Linux libsecret).

```bash
# Store a secret
procclaw secret set SLACK_TOKEN "xoxb-your-token-here"

# Get a secret (masked by default)
procclaw secret get SLACK_TOKEN
# Output: SLACK_TOKEN = xoxb-****-here

# Get secret value (visible)
procclaw secret get SLACK_TOKEN --show
# Output: SLACK_TOKEN = xoxb-your-token-here

# List all secrets
procclaw secret list

# Delete a secret
procclaw secret delete SLACK_TOKEN
```

Use secrets in jobs:
```yaml
jobs:
  my-job:
    cmd: python script.py
    env:
      API_KEY: ${secret:MY_API_KEY}
```

### System Service

Install ProcClaw as a system service to start automatically on boot.

```bash
# Install as system service
procclaw service install
# Creates: ~/Library/LaunchAgents/com.procclaw.daemon.plist (macOS)
# Creates: ~/.config/systemd/user/procclaw.service (Linux)

# Check service status
procclaw service status

# Uninstall service
procclaw service uninstall
```

### Search and Filtering

ProcClaw provides powerful search and filtering capabilities to find jobs quickly.

```bash
# Search by name, description, id, or tags
procclaw search "trading"
procclaw search "polymarket"
procclaw search "scanner"

# List with filters
procclaw list --status running
procclaw list --type scheduled
procclaw list --tag openclaw
procclaw list --enabled

# Combine filters
procclaw list --type continuous --tag trading
procclaw list -q "report" --status stopped
```

**Search matches:**
- Job ID (e.g., `stock-scanner`)
- Job name (e.g., `Stock Scanner`)
- Job description
- Tags

### Configuration

```bash
# Initialize default configuration
procclaw init

# Show config file paths
procclaw config

# Validate configuration files
procclaw config --validate

# Show version
procclaw version
```

## HTTP API

The daemon exposes a REST API at `http://localhost:9876` (configurable).

### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Daemon health check |
| `GET` | `/api/v1/jobs` | List all jobs (with filtering) |
| `GET` | `/api/v1/jobs/{id}` | Get job details |
| `POST` | `/api/v1/jobs` | Create a new job |
| `PATCH` | `/api/v1/jobs/{id}` | Update job properties |
| `DELETE` | `/api/v1/jobs/{id}` | Delete a job |
| `POST` | `/api/v1/jobs/{id}/start` | Start a job |
| `POST` | `/api/v1/jobs/{id}/stop` | Stop a job |
| `POST` | `/api/v1/jobs/{id}/restart` | Restart a job |
| `POST` | `/api/v1/jobs/{id}/run` | Force run (for scheduled/oneshot) |
| `POST` | `/api/v1/jobs/{id}/tags` | Add tags to a job |
| `DELETE` | `/api/v1/jobs/{id}/tags/{tag}` | Remove a tag |
| `GET` | `/api/v1/jobs/{id}/logs` | Get job logs |
| `GET` | `/api/v1/runs` | List execution history |
| `GET` | `/metrics` | Prometheus metrics |
| `POST` | `/api/v1/reload` | Reload configuration |

### Query Parameters for `/api/v1/jobs`

| Parameter | Description | Example |
|-----------|-------------|---------|
| `q` | Search in name, description, id, tags | `?q=trading` |
| `status` | Filter by status | `?status=running` |
| `type` | Filter by job type | `?type=scheduled` |
| `tag` | Filter by single tag | `?tag=openclaw` |
| `tags` | Filter by multiple tags (comma-separated) | `?tags=ai,trading` |
| `enabled` | Filter by enabled status | `?enabled=true` |

### Examples

```bash
# Health check
curl http://localhost:9876/health
# {"status": "healthy", "version": "0.1.0", "jobs_running": 3, "jobs_total": 10}

# List all jobs
curl http://localhost:9876/api/v1/jobs
# {"jobs": [...], "total": 10}

# Search jobs
curl "http://localhost:9876/api/v1/jobs?q=trading"
# {"jobs": [...], "total": 5}

# Filter by type and status
curl "http://localhost:9876/api/v1/jobs?type=scheduled&status=running"

# Filter by multiple tags
curl "http://localhost:9876/api/v1/jobs?tags=openclaw,ai"

# Combine search with filters
curl "http://localhost:9876/api/v1/jobs?q=report&type=scheduled&enabled=true"

# Start a job
curl -X POST http://localhost:9876/api/v1/jobs/my-job/start
# {"success": true, "message": "Job started", "job_id": "my-job", "pid": 12345}

# Get logs
curl "http://localhost:9876/api/v1/jobs/my-job/logs?lines=50"
# {"job_id": "my-job", "lines": [...], "total_lines": 50}

# Prometheus metrics
curl http://localhost:9876/metrics
```

### Authentication

Enable token authentication in `procclaw.yaml`:

```yaml
api:
  auth:
    enabled: true
    token: "your-secret-token"
```

Then include the token in requests:

```bash
curl -H "Authorization: Bearer your-secret-token" \
  http://localhost:9876/api/v1/jobs
```

## Prometheus Metrics

ProcClaw exposes metrics at `/metrics` in Prometheus format:

```
# HELP procclaw_jobs_total Total number of configured jobs
# TYPE procclaw_jobs_total gauge
procclaw_jobs_total 8

# HELP procclaw_job_status Job status (1=running, 0=stopped)
# TYPE procclaw_job_status gauge
procclaw_job_status{job="api-server"} 1
procclaw_job_status{job="scanner"} 0

# HELP procclaw_job_uptime_seconds Current uptime in seconds
# TYPE procclaw_job_uptime_seconds gauge
procclaw_job_uptime_seconds{job="api-server"} 3600.5

# HELP procclaw_job_restart_count_total Total restart count
# TYPE procclaw_job_restart_count_total counter
procclaw_job_restart_count_total{job="api-server"} 2
```

## Job Types

### Manual
- Runs only when explicitly triggered via CLI or API
- Exits when the command completes
- Good for: one-off tasks, data exports, maintenance scripts

### Continuous
- Runs indefinitely until stopped
- Auto-restarts on failure (respects retry policy)
- Health checks monitor status
- Good for: web servers, API services, background workers

### Scheduled
- Runs on a cron schedule
- Configurable overlap behavior (skip, queue, or kill/restart)
- Timezone-aware
- Good for: periodic tasks, scanners, reports

### Oneshot
- Runs once at a specific datetime
- Auto-disables after successful execution
- Shows as "Planned" status until run time
- Good for: one-time migrations, scheduled deployments, reminders

```yaml
jobs:
  switch-calendar:
    name: Switch Calendar Mode
    cmd: python switch_mode.py
    type: oneshot
    run_at: "2026-02-13T09:00:00"
    timezone: America/Sao_Paulo
```

## Retry Policy

When a job fails, ProcClaw uses exponential backoff:

| Attempt | Delay |
|---------|-------|
| 1 | Immediate |
| 2 | 30 seconds |
| 3 | 5 minutes |
| 4 | 15 minutes |
| 5 | 1 hour |

After 5 failures, the job is marked as `failed` and alerts are sent (if configured).

Custom retry policy:
```yaml
jobs:
  my-job:
    retry:
      max_attempts: 3
      delays: [0, 60, 300]  # 0s, 1m, 5m
```

## Dependencies

Jobs can depend on other jobs:

```yaml
jobs:
  database:
    cmd: docker start postgres
    type: manual

  migrations:
    cmd: python manage.py migrate
    type: manual
    depends_on:
      - job: database
        condition: after_start
        timeout: 30

  web-server:
    cmd: python manage.py runserver
    type: continuous
    depends_on:
      - job: migrations
        condition: after_complete
```

**Conditions:**
- `after_start`: Dependency must have started (running)
- `after_complete`: Dependency must have completed successfully (exit 0)
- `before_complete`: This job must complete before dependency starts

## Health Checks

### Process Check (default)
Monitors if the process is still alive.

```yaml
health_check:
  type: process
  interval: 60
```

### HTTP Check
Calls an HTTP endpoint and checks the response.

```yaml
health_check:
  type: http
  url: http://localhost:8000/health
  expected_status: 200
  expected_body: "ok"    # Optional: check response body
  timeout: 10            # Request timeout in seconds
  interval: 30           # Check interval in seconds
```

### File Heartbeat Check
Checks if a file was recently modified (useful for long-running scripts that write heartbeat files).

```yaml
health_check:
  type: file
  path: /tmp/myapp-heartbeat
  max_age: 120          # Max seconds since last modification
  interval: 60
```

## Log Storage

ProcClaw stores logs in files during execution and optionally saves them to SQLite after completion.

### Configuration

In `procclaw.yaml`:

```yaml
daemon:
  file_log_mode: delete  # keep | delete | disabled
```

| Mode | Behavior |
|------|----------|
| `keep` | Keep log files after saving to SQLite (default) |
| `delete` | Delete log files after saving to SQLite |
| `disabled` | Don't save to SQLite, keep files only |

### Log Source

The API returns a `source` field indicating where logs came from:
- `file` - Logs read from active log file (running jobs)
- `sqlite` - Logs retrieved from database (completed jobs)
- `none` - No logs available

The Web UI shows a visual indicator: 📄 File (green) or 💾 SQLite (yellow).

## Crash Recovery & Completion Tracking

ProcClaw uses a **wrapper script** to reliably track job completion, even if the daemon crashes while a job is running.

### How It Works

Every job runs inside a transparent wrapper that provides:

1. **Heartbeat**: A file updated every 5 seconds while the job runs
2. **Completion Marker**: A file with the exit code, written when the job finishes

```
/tmp/procclaw-markers/
├── procclaw-wrapper.sh        # The wrapper script (auto-generated)
├── {job_id}-{run_id}.done     # Completion markers (exit code inside)

/tmp/procclaw-heartbeats/
└── {job_id}-{run_id}          # Heartbeat files (timestamp inside)
```

### Crash Recovery Scenarios

| Scenario | Before | After |
|----------|--------|-------|
| Daemon dies, job completes | ❌ Lost (marked as killed) | ✅ Recovered from marker |
| Daemon dies, job fails | ❌ Lost | ✅ Exit code recovered |
| Daemon dies, job still running | ✅ Adopted (PID check) | ✅ Adopted + heartbeat |
| Job killed (SIGKILL) | ❌ Unknown state | ⚠️ Detected via stale heartbeat |

### On Daemon Restart

When ProcClaw starts, it checks for incomplete jobs:

1. **Completion Marker exists?** → Job finished while daemon was down, recover exit code
2. **Heartbeat recent (<15s)?** → Job still running, adopt the process
3. **Heartbeat stale (>15s)?** → Job died mid-execution (SIGKILL or crash)
4. **No marker, no heartbeat?** → Job killed, mark as failed

### Wrapper Behavior

The wrapper handles signals gracefully:

- **SIGTERM/SIGINT**: Cleanup runs, marker is written
- **SIGKILL**: No cleanup possible, detected via stale heartbeat

### Transparency

Jobs don't need any modifications - the wrapper is completely transparent:

```bash
# What you define
cmd: python my_script.py --arg value

# What actually runs (automatic)
/tmp/procclaw-markers/procclaw-wrapper.sh bash -c 'python my_script.py --arg value'
```

### Configuration

The wrapper is enabled by default. Directories can be customized in code if needed:

```python
from procclaw.core.job_wrapper import init_wrapper_manager

init_wrapper_manager(
    marker_dir=Path("/custom/markers"),
    heartbeat_dir=Path("/custom/heartbeats"),
    heartbeat_interval=5,      # seconds
    stale_threshold=15,        # seconds before considered dead
)
```

## Directory Structure

```
~/.procclaw/
├── procclaw.yaml     # Main daemon configuration
├── jobs.yaml         # Job definitions
├── procclaw.db       # SQLite state database (jobs, runs, logs)
├── procclaw.pid      # Daemon PID file
├── .secrets          # Fallback secrets file (if keychain unavailable)
├── scripts/          # Custom scripts for jobs
└── logs/
    ├── daemon.log           # Daemon output
    ├── daemon.audit.log     # Audit log (start/stop/config changes)
    ├── <job>.log            # Job stdout (if file_log_mode != disabled)
    └── <job>.error.log      # Job stderr

/tmp/procclaw-markers/        # Crash recovery (auto-created)
├── procclaw-wrapper.sh       # Job wrapper script
└── <job>-<run_id>.done       # Completion markers

/tmp/procclaw-heartbeats/     # Job heartbeats (auto-created)
└── <job>-<run_id>            # Heartbeat files (timestamp)
```

## Comparison with Alternatives

| Feature | ProcClaw | Celery | PM2 | Supervisor | systemd |
|---------|----------|--------|-----|------------|---------|
| Cron scheduling | ✅ | ✅ (beat) | ✅ | ❌ | ❌ |
| Job dependencies | ✅ | ✅ (chains) | ❌ | ❌ | ✅ |
| HTTP API | ✅ | ✅ (flower) | ✅ | ✅ | ❌ |
| Secrets management | ✅ | ❌ | ❌ | ❌ | ❌ |
| Prometheus metrics | ✅ | ✅ | ✅ | ❌ | ❌ |
| OpenClaw integration | ✅ | ❌ | ❌ | ❌ | ❌ |
| No broker required | ✅ | ❌ | ✅ | ✅ | ✅ |
| Web UI built-in | ✅ | ❌ (flower) | ✅ | ✅ | ❌ |
| Single binary | ❌ | ❌ | ❌ | ❌ | ✅ |
| Python native | ✅ | ✅ | ❌ | ✅ | ❌ |
| Setup complexity | Low | High | Low | Medium | Low |

## Troubleshooting

### Daemon won't start

```bash
# Check if another instance is running
procclaw daemon status

# Check for port conflicts
lsof -i :9876

# Start in foreground to see errors
procclaw daemon start -f
```

### Job keeps restarting

```bash
# Check job logs
procclaw logs my-job -n 100

# Check if health check is failing
procclaw status my-job

# Validate configuration
procclaw config --validate
```

### Secrets not working

```bash
# Verify secret is stored
procclaw secret list

# Check keychain access (macOS)
security find-generic-password -s "procclaw" -a "MY_SECRET"
```

## Development

```bash
# Clone and install dev dependencies
git clone https://github.com/marcosgabbardo/Procclaw.git
cd Procclaw
uv venv
uv pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=procclaw

# Lint
ruff check src/
```

## License

MIT License - see [LICENSE](LICENSE) for details.

## Author

**Marcos Gabbardo** ([@marcosgabbardo](https://github.com/marcosgabbardo))

---

*Part of the [OpenClaw](https://github.com/openclaw/openclaw) ecosystem* 🦞
