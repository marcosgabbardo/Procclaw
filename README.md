# ProcClaw 🦞

**Process Manager for OpenClaw** - A robust, lightweight process manager with CLI, API, and deep OpenClaw integration.

## Features

- **Job Types**: Manual, Continuous, Scheduled (cron)
- **Dependencies**: Jobs can depend on other jobs (after_start, after_complete)
- **Health Checks**: Process, HTTP endpoint, File heartbeat
- **Retry Policy**: Webhook-style backoff (0s, 30s, 5m, 15m, 1h)
- **Graceful Shutdown**: 60s grace period with SIGTERM → SIGKILL
- **Secrets Management**: macOS Keychain / Linux libsecret integration
- **System Service**: launchd (macOS) / systemd (Linux) support
- **HTTP API**: Full REST API with Prometheus metrics
- **OpenClaw Integration**: Skill control, memory logging, alerts

## Installation

```bash
# From source with uv
cd procclaw
uv venv && uv pip install -e .

# Or with pip
pip install -e .
```

## Quick Start

```bash
# Initialize configuration
procclaw init

# Edit jobs configuration
nano ~/.procclaw/jobs.yaml

# Start daemon
procclaw daemon start

# List jobs
procclaw list

# Start a job
procclaw start my-job

# View logs
procclaw logs my-job -f

# Stop daemon
procclaw daemon stop
```

## Configuration

### Main Config: `~/.procclaw/procclaw.yaml`

```yaml
daemon:
  host: 127.0.0.1
  port: 9876
  log_level: INFO

api:
  auth:
    enabled: false
    # token: "your-secret-token"

defaults:
  retry:
    preset: webhook  # 0s, 30s, 5m, 15m, 1h
  shutdown:
    grace_period: 60
  health_check:
    type: process
    interval: 60

openclaw:
  enabled: true
  memory_logging: true
  alerts:
    enabled: true
    channels: [whatsapp]
```

### Jobs: `~/.procclaw/jobs.yaml`

```yaml
jobs:
  # Scheduled job (cron)
  stock-scanner:
    name: Stock Scanner
    cmd: python scanner.py
    cwd: ~/scripts/scanner
    type: scheduled
    schedule: "0 */12 * * *"
    timezone: America/Sao_Paulo
    on_overlap: skip  # skip | queue | kill_restart

  # Continuous service
  api-server:
    name: API Server
    cmd: uvicorn main:app --port 8000
    cwd: ~/projects/api
    type: continuous
    health_check:
      type: http
      url: http://localhost:8000/health
      interval: 30

  # Job with dependencies
  data-processor:
    name: Data Processor
    cmd: python process.py
    type: manual
    depends_on:
      - job: api-server
        condition: after_start
        timeout: 60

  # Job with secrets
  notifier:
    name: Notifier
    cmd: python notify.py
    env:
      API_KEY: ${secret:NOTIFY_API_KEY}
    type: manual
```

## CLI Commands

### Daemon Management
```bash
procclaw daemon start          # Start in background
procclaw daemon start -f       # Start in foreground
procclaw daemon stop           # Stop daemon
procclaw daemon status         # Show status
procclaw daemon logs [-f]      # View daemon logs
```

### Job Control
```bash
procclaw list                  # List all jobs
procclaw list --status running # Filter by status
procclaw list --tag trading    # Filter by tag
procclaw status <job>          # Detailed job status
procclaw start <job>           # Start job
procclaw stop <job> [--force]  # Stop job
procclaw restart <job>         # Restart job
procclaw logs <job> [-f] [-n]  # View job logs
```

### Secrets
```bash
procclaw secret set NAME VALUE  # Store secret
procclaw secret get NAME        # Get secret (masked)
procclaw secret get NAME --show # Get secret (visible)
procclaw secret list            # List secrets
procclaw secret delete NAME     # Delete secret
```

### System Service
```bash
procclaw service install    # Install as system service
procclaw service uninstall  # Remove system service
procclaw service status     # Check service status
```

### Configuration
```bash
procclaw init               # Create default config
procclaw config             # Show config paths
procclaw config --validate  # Validate configuration
procclaw version            # Show version
```

## HTTP API

Base URL: `http://localhost:9876`

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Daemon health check |
| GET | `/api/v1/jobs` | List all jobs |
| GET | `/api/v1/jobs/{id}` | Get job details |
| POST | `/api/v1/jobs/{id}/start` | Start a job |
| POST | `/api/v1/jobs/{id}/stop` | Stop a job |
| POST | `/api/v1/jobs/{id}/restart` | Restart a job |
| GET | `/api/v1/jobs/{id}/logs` | Get job logs |
| GET | `/metrics` | Prometheus metrics |
| POST | `/api/v1/reload` | Reload configuration |

### Authentication

Enable token auth in config:
```yaml
api:
  auth:
    enabled: true
    token: "your-secret-token"
```

Then include header: `Authorization: Bearer your-secret-token`

## Prometheus Metrics

```
procclaw_jobs_total                    # Total configured jobs
procclaw_job_status{job="..."}         # 1=running, 0=stopped
procclaw_job_uptime_seconds{job="..."}  # Current uptime
procclaw_job_restart_count_total{job="..."} # Total restarts
```

## Job Types

### Manual
- Runs only when explicitly triggered
- Exits when complete

### Continuous  
- Runs indefinitely
- Auto-restarts on failure (with retry policy)
- Health checks monitor status

### Scheduled
- Cron-based scheduling
- Configurable overlap behavior
- Timezone support

## Retry Policy

Default "webhook" preset:
| Attempt | Delay |
|---------|-------|
| 1 | Immediate |
| 2 | 30 seconds |
| 3 | 5 minutes |
| 4 | 15 minutes |
| 5 | 1 hour |

After 5 failures, job is marked as failed and alerts are sent.

## Dependencies

Jobs can depend on other jobs:

```yaml
depends_on:
  - job: database
    condition: after_start    # Wait for DB to start
    timeout: 60
  - job: migrations
    condition: after_complete # Wait for migrations to finish
```

Conditions:
- `after_start`: Dependency must have started
- `after_complete`: Dependency must have completed successfully
- `before_complete`: This job must complete before dependency

## Health Checks

### Process (default)
```yaml
health_check:
  type: process
  interval: 60
```

### HTTP
```yaml
health_check:
  type: http
  url: http://localhost:8000/health
  expected_status: 200
  expected_body: "ok"
  timeout: 10
  interval: 30
```

### File
```yaml
health_check:
  type: file
  path: /tmp/heartbeat
  max_age: 120  # seconds
  interval: 60
```

## Directory Structure

```
~/.procclaw/
├── procclaw.yaml     # Main configuration
├── jobs.yaml         # Jobs configuration
├── procclaw.db       # SQLite state database
├── procclaw.pid      # Daemon PID file
├── .secrets          # Fallback secrets file
└── logs/
    ├── daemon.log
    ├── daemon.audit.log
    ├── <job>.log
    └── <job>.error.log
```

## License

MIT

## Author

Marcos Gabbardo (@marcosgabbardo)
