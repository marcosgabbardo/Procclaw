# ProcClaw

Process Manager for OpenClaw.

## Features

- **Job Types**: scheduled (cron), continuous, manual
- **Health Checks**: process, HTTP, file-based
- **Retry Policy**: webhook-style backoff (0, 30s, 5m, 15m, 1h)
- **Dependencies**: after_start, after_complete, before_complete
- **Graceful Shutdown**: 60s grace period with SIGTERM → SIGKILL
- **CLI & API**: Full control via command line and HTTP API
- **OpenClaw Integration**: Alerts, memory logging, skill control

## Installation

```bash
# From source
cd procclaw
pip install -e .

# Or with uv
uv pip install -e .
```

## Quick Start

```bash
# Initialize configuration
procclaw init

# Edit jobs
nano ~/.procclaw/jobs.yaml

# Start daemon
procclaw daemon start

# List jobs
procclaw list

# Start a job
procclaw start <job-id>

# Check status
procclaw status <job-id>

# View logs
procclaw logs <job-id> -f

# Stop daemon
procclaw daemon stop
```

## Configuration

### Main config: `~/.procclaw/procclaw.yaml`

```yaml
daemon:
  host: 127.0.0.1
  port: 9876
  log_level: INFO

api:
  auth:
    enabled: false

defaults:
  retry:
    preset: webhook
  shutdown:
    grace_period: 60
```

### Jobs: `~/.procclaw/jobs.yaml`

```yaml
jobs:
  my-scanner:
    name: Stock Scanner
    cmd: python scanner.py
    cwd: ~/scripts/stock-scanner
    type: scheduled
    schedule: "0 */12 * * *"
    timezone: America/Sao_Paulo

  my-service:
    name: API Service
    cmd: uvicorn main:app
    cwd: ~/projects/api
    type: continuous
    health_check:
      type: http
      url: http://localhost:8000/health
```

## CLI Commands

```bash
# Daemon
procclaw daemon start|stop|status|logs

# Jobs
procclaw list [--status running] [--tag trading]
procclaw status <job>
procclaw start <job>
procclaw stop <job> [--force]
procclaw restart <job>
procclaw logs <job> [-f] [-n 100]

# Config
procclaw config [--validate]
procclaw init
```

## License

MIT
