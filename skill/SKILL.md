# ProcClaw Skill

Control and monitor ProcClaw jobs via chat.

## When to Use

- User asks about running jobs, processes, or services
- User wants to start/stop/restart a job
- User asks about job status, logs, or health
- User mentions "procclaw", "jobs", or specific job names
- Proactive: check for pending alerts in heartbeat

## API Endpoint

ProcClaw daemon runs on `http://localhost:9876`.

## Commands

### List Jobs

```bash
curl -s http://localhost:9876/api/v1/jobs | jq
```

Or use CLI:
```bash
cd ~/.openclaw/workspace/projects/procclaw && uv run procclaw list
```

### Get Job Status

```bash
curl -s http://localhost:9876/api/v1/jobs/<job_id> | jq
```

Or CLI:
```bash
cd ~/.openclaw/workspace/projects/procclaw && uv run procclaw status <job_id>
```

### Start Job

```bash
curl -s -X POST http://localhost:9876/api/v1/jobs/<job_id>/start | jq
```

Or CLI:
```bash
cd ~/.openclaw/workspace/projects/procclaw && uv run procclaw start <job_id>
```

### Stop Job

```bash
curl -s -X POST http://localhost:9876/api/v1/jobs/<job_id>/stop | jq
```

Or CLI:
```bash
cd ~/.openclaw/workspace/projects/procclaw && uv run procclaw stop <job_id>
```

### Restart Job

```bash
curl -s -X POST http://localhost:9876/api/v1/jobs/<job_id>/restart | jq
```

### View Logs

```bash
curl -s "http://localhost:9876/api/v1/jobs/<job_id>/logs?lines=50" | jq -r '.logs'
```

Or CLI:
```bash
cd ~/.openclaw/workspace/projects/procclaw && uv run procclaw logs <job_id> -n 50
```

### Check Daemon Health

```bash
curl -s http://localhost:9876/health | jq
```

### Reload Configuration

```bash
curl -s -X POST http://localhost:9876/api/v1/reload | jq
```

## Pending Alerts

Check for pending alerts during heartbeat:

```bash
cat ~/.openclaw/workspace/memory/procclaw-pending-alerts.md 2>/dev/null
```

If alerts exist, deliver them to the user and clear:

```python
# After delivering alerts
from procclaw.openclaw import clear_pending_alerts
clear_pending_alerts()
```

Or manually:
```bash
rm ~/.openclaw/workspace/memory/procclaw-pending-alerts.md
```

## Memory Files

ProcClaw writes state to memory for context:

- `memory/procclaw-state.md` - Recent job events (last 100)
- `memory/procclaw-jobs.md` - Current job status summary
- `memory/procclaw-pending-alerts.md` - Alerts waiting for delivery

## Response Format

When user asks about jobs, respond with:

**Running:**
- ✅ job-name (uptime: 2h 15m)

**Stopped:**
- ⏹️ job-name (last run: 10:30, exit 0)

**Failed:**
- ❌ job-name (exit code 1, error: ...)

**Scheduled:**
- ⏰ job-name (next: 14:00)

## Example Interactions

**User:** "quais jobs estão rodando?"
**Response:** List all jobs with status icons

**User:** "reinicia o stock-scanner"
**Response:** Restart job, report previous uptime and new PID

**User:** "para todos os jobs de trading"
**Response:** Stop jobs matching tag, confirm each

**User:** "logs do algo-traders"
**Response:** Show last 20 lines of logs

**User:** "o que aconteceu com o accumulation?"
**Response:** Check status, show last error if failed, suggest restart

## Daemon Management

### Start Daemon

```bash
cd ~/.openclaw/workspace/projects/procclaw && uv run procclaw daemon start
```

### Stop Daemon

```bash
cd ~/.openclaw/workspace/projects/procclaw && uv run procclaw daemon stop
```

### Check if Running

```bash
curl -s http://localhost:9876/health 2>/dev/null && echo "Running" || echo "Stopped"
```

## Configuration

Jobs are defined in `~/.procclaw/jobs.yaml`.

To add a new job:
1. Edit the file
2. Call reload: `curl -X POST http://localhost:9876/api/v1/reload`

## Troubleshooting

### Daemon not responding
```bash
# Check if process exists
pgrep -f "procclaw daemon"

# Check logs
tail -50 ~/.procclaw/logs/daemon.log
```

### Job keeps failing
```bash
# Check job logs
cat ~/.procclaw/logs/<job_id>.error.log | tail -50

# Check retry status
curl -s http://localhost:9876/api/v1/jobs/<job_id> | jq '.retry_attempt, .next_retry'
```
