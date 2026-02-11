# ProcClaw Skill

Central process/job manager with web UI, scheduling, queues, and OpenClaw integration.

## When to Use

- User asks about running jobs, processes, or services
- User wants to start/stop/restart/enable/disable a job
- User asks about job status, logs, runs, or health
- User mentions "procclaw", "jobs", or specific job names
- User wants to schedule or manage cron-like tasks
- User asks about OpenClaw job prompts or AI sessions
- Proactive: check for pending alerts in heartbeat

## Quick Reference

| Action | API | CLI |
|--------|-----|-----|
| List jobs | `GET /api/v1/jobs` | `procclaw list` |
| Job status | `GET /api/v1/jobs/{id}` | `procclaw status {id}` |
| Start | `POST /api/v1/jobs/{id}/start` | `procclaw start {id}` |
| Stop | `POST /api/v1/jobs/{id}/stop` | `procclaw stop {id}` |
| Restart | `POST /api/v1/jobs/{id}/restart` | - |
| Enable | `POST /api/v1/jobs/{id}/enable` | - |
| Disable | `POST /api/v1/jobs/{id}/disable` | - |
| Logs | `GET /api/v1/runs/{run_id}/logs` | `procclaw logs {id}` |
| Health | `GET /health` | `procclaw daemon status` |

## Authentication

API requires Bearer token (stored in macOS Keychain):

```bash
TOKEN=$(security find-generic-password -a procclaw -s procclaw-api -w)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:9876/api/v1/jobs | jq
```

Web UI at http://localhost:9876 doesn't need token (same-origin bypass).

## API Endpoints

Base URL: `http://localhost:9876`

### Jobs

```bash
# List all jobs
GET /api/v1/jobs

# Get job details
GET /api/v1/jobs/{job_id}

# Start/Stop/Restart
POST /api/v1/jobs/{job_id}/start
POST /api/v1/jobs/{job_id}/stop
POST /api/v1/jobs/{job_id}/restart

# Enable/Disable (modifies jobs.yaml)
POST /api/v1/jobs/{job_id}/enable
POST /api/v1/jobs/{job_id}/disable

# Force run (ignores schedule, runs now)
POST /api/v1/jobs/{job_id}/force-start

# Delete job
DELETE /api/v1/jobs/{job_id}
```

### Runs (Job History)

```bash
# List runs
GET /api/v1/runs
GET /api/v1/runs?job_id={job_id}&status=failed&limit=50

# Get run logs
GET /api/v1/runs/{run_id}/logs

# Get AI session (for OpenClaw jobs)
GET /api/v1/runs/{run_id}/session

# Delete run
DELETE /api/v1/runs/{run_id}

# Bulk delete
POST /api/v1/runs/bulk-delete
  {"run_ids": [1, 2, 3]}
```

### OpenClaw Jobs

```bash
# Get prompt file content
GET /api/v1/jobs/{job_id}/prompt

# Update prompt file
PUT /api/v1/jobs/{job_id}/prompt
  {"content": "# New prompt content..."}

# Backfill session messages (migrate to SQLite)
POST /api/v1/admin/backfill-sessions
```

### Queues

```bash
# List queues
GET /api/v1/queues

# Get queue details
GET /api/v1/queues/{queue_name}

# List jobs in queue
GET /api/v1/queues/{queue_name}/jobs

# Clear queue
DELETE /api/v1/queues/{queue_name}
```

### Workflows

```bash
# List workflows
GET /api/v1/workflows

# Run workflow
POST /api/v1/workflows/{workflow_id}/run

# Get workflow runs
GET /api/v1/workflows/{workflow_id}/runs
```

### Admin

```bash
# Reload configuration
POST /api/v1/reload

# Daemon health
GET /health

# Daemon logs
GET /api/v1/daemon/logs
```

## Job Types

| Type | Description |
|------|-------------|
| `continuous` | Runs 24/7, auto-restarts on crash |
| `scheduled` | Cron-based schedule |
| `oneshot` | Runs once at specific time |
| `manual` | Only runs when triggered |
| `openclaw` | OpenClaw AI agent task |

## OpenClaw Integration

Jobs with `type: openclaw` run AI agent tasks:

```yaml
oc-idea-hunter:
  name: '[OC] Idea Hunter'
  type: openclaw
  cmd: |
    python3 ~/.procclaw/scripts/oc-runner-v3.py \
      ~/.procclaw/prompts/idea-hunter.md \
      --job-id idea-hunter \
      --timeout 600
  schedule: "0 17 * * *"
  queue: openclaw  # Jobs in same queue run sequentially
```

### Prompt Editor

Web UI has a markdown editor for OpenClaw prompts:
- Click 📝 on job detail (only for type=openclaw)
- Split view: Edit | Preview
- Syntax highlighting
- Auto-backup on save

### Session Viewer

AI session transcripts are persisted in SQLite:
- Click 🤖 on run to view AI conversation
- Messages searchable in UI
- Independent of OpenClaw JSONL files

## Queue System

Jobs can be assigned to queues for sequential execution:

```yaml
my-job:
  queue: openclaw  # All openclaw jobs share this queue
```

- Jobs in the same queue run one at a time
- Prevents resource conflicts
- Status shows position in queue

## Self-Healing

Jobs can auto-diagnose and fix failures:

```yaml
my-job:
  self_healing:
    enabled: true
    remediation:
      max_attempts: 3
      allowed_actions:
        - restart_job
        - edit_script
        - edit_config
```

## Web UI

Access at: http://localhost:9876

Features:
- **Jobs tab**: List, filter, start/stop/enable/disable
- **Runs tab**: History, logs, AI sessions, bulk delete
- **Dependencies**: Visual dependency graph
- **Workflows**: Chain/group/chord job orchestration
- **Logs**: Real-time daemon logs
- **DLQ**: Dead letter queue for failed jobs
- **Config**: Auth settings, YAML editor

## Memory Files

ProcClaw writes state for OpenClaw context:

| File | Purpose |
|------|---------|
| `memory/procclaw-pending-alerts.md` | Alerts waiting for delivery |
| `memory/procclaw-session-triggers.md` | Session notifications |
| `memory/procclaw-state.md` | Recent job events |
| `memory/procclaw-jobs.md` | Job status summary |

### Heartbeat: Check Alerts

```bash
# Check for pending alerts
cat ~/.openclaw/workspace/memory/procclaw-pending-alerts.md 2>/dev/null

# After delivering, clear
rm ~/.openclaw/workspace/memory/procclaw-pending-alerts.md
```

## Configuration

### jobs.yaml

Location: `~/.procclaw/jobs.yaml`

```yaml
jobs:
  my-job:
    name: My Job
    description: "Does something useful"
    cmd: python3 script.py
    type: scheduled
    schedule: "0 * * * *"  # Every hour
    enabled: true
    tags:
      - category
    alerts:
      on_failure: true
    retry:
      enabled: true
      max_attempts: 3
```

### Prompts (OpenClaw)

Location: `~/.procclaw/prompts/`

Markdown files with AI agent instructions.

## CLI Commands

```bash
cd ~/.openclaw/workspace/projects/procclaw

# Daemon
uv run procclaw daemon start
uv run procclaw daemon stop
uv run procclaw daemon status

# Jobs
uv run procclaw list
uv run procclaw status <job_id>
uv run procclaw start <job_id>
uv run procclaw stop <job_id>
uv run procclaw logs <job_id> -f  # Follow logs
```

## Response Format

When reporting job status:

| Status | Icon | Example |
|--------|------|---------|
| Running | ✅ | `✅ scanner (uptime: 2h 15m)` |
| Stopped | ⏹️ | `⏹️ backup (last: 10:30, exit 0)` |
| Failed | ❌ | `❌ importer (exit 1: connection refused)` |
| Disabled | 🚫 | `🚫 calendar-poa (disabled)` |
| Planned | ⏰ | `⏰ report (next: 14:00)` |
| Queued | 🔄 | `🔄 idea-hunter (#2 in openclaw queue)` |

## Troubleshooting

### Daemon not responding

```bash
# Check process
pgrep -f "procclaw daemon"

# Force restart
pkill -f "procclaw daemon"
cd ~/.openclaw/workspace/projects/procclaw && uv run procclaw daemon start
```

### Job keeps failing

```bash
# Check logs
TOKEN=$(security find-generic-password -a procclaw -s procclaw-api -w)
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:9876/api/v1/runs?job_id=my-job&limit=5" | jq '.runs[] | {exit_code, error}'

# Check DLQ
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:9876/api/v1/dlq | jq
```

### Session not showing

```bash
# Backfill session messages to SQLite
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:9876/api/v1/admin/backfill-sessions | jq
```

## Files

| Path | Purpose |
|------|---------|
| `~/.procclaw/jobs.yaml` | Job definitions |
| `~/.procclaw/procclaw.yaml` | Daemon config |
| `~/.procclaw/procclaw.db` | SQLite (runs, logs, state) |
| `~/.procclaw/prompts/` | OpenClaw prompt files |
| `~/.procclaw/scripts/` | Job scripts |
| `~/.procclaw/logs/` | Log files |
