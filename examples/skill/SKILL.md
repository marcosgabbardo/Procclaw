# ProcClaw Skill

Control and monitor ProcClaw jobs via chat.

## When to Use

- User asks about running jobs, processes, or services
- User wants to start/stop/restart a job
- User asks about job status, logs, or health
- User mentions "procclaw", "jobs", or specific job names
- User asks about failed jobs or dead letter queue (DLQ)
- User wants to trigger a job via webhook
- User asks about concurrency or queued jobs
- Proactive: check for pending alerts in heartbeat

## API Endpoint

ProcClaw daemon runs on `http://localhost:9876`.

## Basic Commands

### List Jobs

```bash
curl -s http://localhost:9876/api/v1/jobs | jq
```

Or CLI:
```bash
cd ~/.openclaw/workspace/projects/procclaw && uv run procclaw list
```

### Get Job Status

```bash
curl -s http://localhost:9876/api/v1/jobs/<job_id> | jq
```

### Start Job

```bash
curl -s -X POST http://localhost:9876/api/v1/jobs/<job_id>/start | jq
```

### Stop Job

```bash
curl -s -X POST http://localhost:9876/api/v1/jobs/<job_id>/stop | jq
```

### Restart Job

```bash
curl -s -X POST http://localhost:9876/api/v1/jobs/<job_id>/restart | jq
```

### View Logs

```bash
curl -s "http://localhost:9876/api/v1/jobs/<job_id>/logs?lines=50" | jq -r '.lines[]'
```

### Check Daemon Health

```bash
curl -s http://localhost:9876/health | jq
```

### Reload Configuration

```bash
curl -s -X POST http://localhost:9876/api/v1/reload | jq
```

## Enterprise Features

### Dead Letter Queue (DLQ)

Jobs that fail after max retries go to the DLQ for inspection and reinjection.

**List DLQ entries:**
```bash
curl -s http://localhost:9876/api/v1/dlq | jq
```

**Get DLQ stats:**
```bash
curl -s http://localhost:9876/api/v1/dlq/stats | jq
```

**Reinject a DLQ entry:**
```bash
curl -s -X POST http://localhost:9876/api/v1/dlq/<entry_id>/reinject | jq
```

**Purge old DLQ entries:**
```bash
# Purge entries older than 7 days
curl -s -X DELETE "http://localhost:9876/api/v1/dlq?older_than_days=7" | jq

# Purge all entries for a specific job
curl -s -X DELETE "http://localhost:9876/api/v1/dlq?job_id=failing-job" | jq
```

### Concurrency Control

Check how many instances of a job are running and queued.

**Get concurrency stats:**
```bash
curl -s http://localhost:9876/api/v1/jobs/<job_id>/concurrency | jq
```

Response:
```json
{
  "job_id": "my-job",
  "running_count": 2,
  "max_instances": 5,
  "queued_count": 3
}
```

### Webhook Triggers

Trigger jobs via HTTP POST with optional payload.

**Trigger a job:**
```bash
curl -s -X POST http://localhost:9876/api/v1/trigger/<job_id> \
  -H "Content-Type: application/json" \
  -d '{"key": "value"}' | jq
```

**With idempotency key (prevents duplicate triggers):**
```bash
curl -s -X POST "http://localhost:9876/api/v1/trigger/<job_id>?idempotency_key=unique-123" \
  -H "Content-Type: application/json" \
  -d '{"event": "deploy"}' | jq
```

**With auth token (if job requires it):**
```bash
curl -s -X POST http://localhost:9876/api/v1/trigger/<job_id> \
  -H "Authorization: Bearer secret-token" \
  -d '{}' | jq
```

### Priority Levels

Jobs can have priority levels that affect execution order:

| Priority | Value | Description |
|----------|-------|-------------|
| CRITICAL | 0 | Highest, may preempt others |
| HIGH | 1 | Important jobs |
| NORMAL | 2 | Default |
| LOW | 3 | Background tasks |

Configure in `jobs.yaml`:
```yaml
jobs:
  important-job:
    priority: high  # or critical, normal, low
```

### Deduplication

Prevents running the same job multiple times within a time window.

Configure in `jobs.yaml`:
```yaml
jobs:
  my-job:
    dedup:
      enabled: true
      window_seconds: 60  # Don't run again within 60s
      use_params: true    # Include params in fingerprint
```

### Distributed Locks

Prevents same job from running on multiple instances.

Configure in `jobs.yaml`:
```yaml
jobs:
  singleton-job:
    lock:
      enabled: true
      timeout_seconds: 300
```

### Event Triggers

#### Webhook Trigger
```yaml
jobs:
  deploy-job:
    trigger:
      enabled: true
      type: webhook
      auth_token: "optional-secret"
```

#### File Watcher Trigger
```yaml
jobs:
  process-files:
    trigger:
      enabled: true
      type: file
      watch_path: /var/data/inbox
      pattern: "*.json"
      delete_after: true
```

## Pending Alerts

Check for pending alerts during heartbeat:

```bash
cat ~/.openclaw/workspace/memory/procclaw-pending-alerts.md 2>/dev/null
```

If alerts exist, deliver them to the user and clear:
```bash
rm ~/.openclaw/workspace/memory/procclaw-pending-alerts.md
```

### Alert Types

| Emoji | Type | Description |
|-------|------|-------------|
| 🚨 | failure | Job failed |
| 💀 | max_retries | Job moved to DLQ |
| 🏥 | health_fail | Health check failed |
| ✅ | recovered | Job recovered |
| 🔄 | restart | Job restarted |
| ⏰ | missed_run | Scheduled job missed |
| 📥 | queued | Job queued (max instances) |
| ⏱️ | queue_timeout | Job timed out in queue |

## Memory Files

- `memory/procclaw-state.md` - Recent job events
- `memory/procclaw-jobs.md` - Current job status summary
- `memory/procclaw-pending-alerts.md` - Alerts waiting for delivery

## Response Format

When user asks about jobs:

**Running:**
- ✅ job-name (uptime: 2h 15m, instances: 2/5)

**Stopped:**
- ⏹️ job-name (last run: 10:30, exit 0)

**Failed:**
- ❌ job-name (exit code 1, in DLQ: yes)

**Scheduled:**
- ⏰ job-name (next: 14:00, priority: high)

**Queued:**
- 📥 job-name (waiting, position: 3)

## Example Interactions

**User:** "quais jobs estão rodando?"
→ List all jobs with status icons and concurrency info

**User:** "reinicia o stock-scanner"
→ Restart job, report previous uptime and new PID

**User:** "o que tem no DLQ?"
→ List DLQ entries with errors and attempt counts

**User:** "reinjeta o job 123 do DLQ"
→ Reinject entry, confirm new run started

**User:** "trigger o deploy-job com payload X"
→ Call webhook endpoint, confirm trigger accepted

**User:** "quantas instâncias do worker estão rodando?"
→ Show concurrency stats

**User:** "limpa o DLQ de jobs antigos"
→ Purge entries older than 7 days

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

### Full Job Config Example

```yaml
jobs:
  example-job:
    name: "Example Job"
    cmd: "python script.py"
    type: scheduled  # scheduled, continuous, manual
    schedule: "0 */2 * * *"  # Every 2 hours
    enabled: true
    
    # Priority
    priority: normal  # critical, high, normal, low
    
    # Retry
    retry:
      enabled: true
      max_attempts: 5
      preset: webhook  # webhook, exponential, fixed
    
    # Concurrency
    concurrency:
      max_instances: 3
      queue_excess: true
      queue_timeout: 300
    
    # Deduplication
    dedup:
      enabled: true
      window_seconds: 60
    
    # Distributed Lock
    lock:
      enabled: false
      timeout_seconds: 300
    
    # Event Trigger
    trigger:
      enabled: false
      type: webhook
      auth_token: null
    
    # Operating Hours
    operating_hours:
      enabled: false
      days: [1, 2, 3, 4, 5]  # Mon-Fri
      start: "08:00"
      end: "18:00"
      timezone: "America/Sao_Paulo"
    
    # Resource Limits
    resources:
      enabled: false
      max_memory_mb: 512
      max_cpu_percent: 80
      timeout_seconds: 3600
      action: warn  # warn, throttle, kill
    
    # Alerts
    alerts:
      on_failure: true
      on_max_retries: true
      on_health_fail: false
      on_recovered: false
    
    # Tags for grouping
    tags: ["production", "critical"]
```

## Troubleshooting & Diagnostics

### 🔍 Diagnóstico Rápido

**1. Checklist inicial:**
```bash
# Daemon está rodando?
curl -s http://localhost:9876/health | jq

# Quantos jobs running?
curl -s http://localhost:9876/api/v1/jobs?status=running | jq '.total'

# Tem algo no DLQ?
curl -s http://localhost:9876/api/v1/dlq/stats | jq

# Logs do daemon
tail -50 ~/.procclaw/logs/daemon.log
```

**2. Status de um job específico:**
```bash
# Status completo
curl -s http://localhost:9876/api/v1/jobs/<job_id> | jq

# Logs stdout (últimas 100 linhas)
cat ~/.procclaw/logs/<job_id>.log | tail -100

# Logs stderr (erros)
cat ~/.procclaw/logs/<job_id>.error.log | tail -50

# Últimas runs
curl -s http://localhost:9876/api/v1/jobs/<job_id>/runs | jq '.[-5:]'
```

### ❌ Job Keeps Failing

**Passo 1: Ver o erro**
```bash
# Stderr do job
cat ~/.procclaw/logs/<job_id>.error.log | tail -100

# Exit code da última run
curl -s http://localhost:9876/api/v1/jobs/<job_id> | jq '.last_exit_code'
```

**Passo 2: Checar DLQ**
```bash
# Ver se está no DLQ
curl -s http://localhost:9876/api/v1/dlq | jq '.entries[] | select(.job_id == "<job_id>")'

# Ver erro que causou DLQ
curl -s http://localhost:9876/api/v1/dlq | jq '.entries[] | select(.job_id == "<job_id>") | .error'
```

**Passo 3: Testar manualmente**
```bash
# Rodar o comando do job manualmente pra ver o erro
cd <cwd_do_job>
<comando_do_job> 2>&1
```

**Passo 4: Corrigir e reinjetar**
```bash
# Depois de corrigir, reinjetar do DLQ
curl -s -X POST http://localhost:9876/api/v1/dlq/<entry_id>/reinject | jq
```

### 🔄 Job Not Starting

**Causa 1: Desabilitado**
```bash
curl -s http://localhost:9876/api/v1/jobs/<job_id> | jq '.enabled'
# Fix: Editar jobs.yaml e reload
```

**Causa 2: Concurrency limit**
```bash
curl -s http://localhost:9876/api/v1/jobs/<job_id>/concurrency | jq
# Se running_count >= max_instances, esperar ou aumentar limite
```

**Causa 3: Lock held**
```bash
curl -s http://localhost:9876/api/v1/jobs/<job_id> | jq '.lock'
# Esperar lock expirar ou forçar release (restart daemon)
```

**Causa 4: Dedup window**
```bash
# Checar se rodou recentemente
curl -s http://localhost:9876/api/v1/jobs/<job_id>/runs | jq '.[-1].started_at'
# Esperar window expirar (default 60s)
```

**Causa 5: Revoked**
```bash
curl -s http://localhost:9876/api/v1/revocations | jq '.[] | select(.job_id == "<job_id>")'
# Remove revocation
curl -s -X DELETE http://localhost:9876/api/v1/jobs/<job_id>/revoke
```

### 🔴 Daemon Not Responding

**Passo 1: Checar processo**
```bash
pgrep -f "procclaw daemon"
ps aux | grep procclaw
```

**Passo 2: Ver logs**
```bash
tail -100 ~/.procclaw/logs/daemon.log
tail -50 ~/.procclaw/logs/daemon.error.log
```

**Passo 3: Checar porta**
```bash
lsof -i :9876
```

**Passo 4: Reiniciar**
```bash
cd ~/.openclaw/workspace/projects/procclaw
uv run procclaw daemon stop
uv run procclaw daemon start

# Ou via launchd (se instalado como serviço)
launchctl stop com.openclaw.procclaw
launchctl start com.openclaw.procclaw
```

### 📊 Análise de Logs

**Formato dos logs:**
```
~/.procclaw/logs/
├── daemon.log           # Output do daemon
├── daemon.error.log     # Erros do daemon
├── daemon.audit.log     # Auditoria (start/stop/config)
├── <job_id>.log         # Stdout do job
└── <job_id>.error.log   # Stderr do job
```

**Buscar erros em todos os logs:**
```bash
grep -r "ERROR\|FAIL\|Exception" ~/.procclaw/logs/ | tail -20
```

**Ver jobs que falharam hoje:**
```bash
grep "exit code" ~/.procclaw/logs/*.log | grep -v "exit code: 0" | grep "$(date +%Y-%m-%d)"
```

**Timeline de um job:**
```bash
grep "<job_id>" ~/.procclaw/logs/daemon.log | tail -50
```

### 🛠️ Correções Comuns

**Job com timeout:**
```yaml
# Aumentar timeout em jobs.yaml
jobs:
  slow-job:
    resources:
      timeout_seconds: 7200  # 2 horas
```

**Job usando muita memória:**
```yaml
# Adicionar limite
jobs:
  memory-hog:
    resources:
      max_memory_mb: 1024
      action: kill  # kill quando exceder
```

**Job overlapping (scheduled):**
```yaml
# Pular se ainda está rodando
jobs:
  long-job:
    on_overlap: skip  # ou queue, ou kill_restart
```

**Muitos retries:**
```yaml
# Ajustar retry policy
jobs:
  flaky-job:
    retry:
      max_attempts: 3
      delays: [30, 60, 120]  # 30s, 1m, 2m
```

### 🔧 Recovery Actions

**Limpar DLQ antigo:**
```bash
curl -s -X DELETE "http://localhost:9876/api/v1/dlq?older_than_days=7" | jq
```

**Reinjetar todos do DLQ:**
```bash
for id in $(curl -s http://localhost:9876/api/v1/dlq | jq -r '.entries[].id'); do
  curl -s -X POST "http://localhost:9876/api/v1/dlq/$id/reinject"
done
```

**Forçar run de job scheduled:**
```bash
curl -s -X POST http://localhost:9876/api/v1/jobs/<job_id>/start | jq
```

**Reset state de um job:**
```bash
# Parar, limpar, reiniciar
curl -s -X POST http://localhost:9876/api/v1/jobs/<job_id>/stop
# Esperar parar
curl -s -X POST http://localhost:9876/api/v1/jobs/<job_id>/start
```

### 📋 Checklist Diário

```bash
# Health check
curl -s http://localhost:9876/health | jq

# Jobs failed nas últimas 24h
curl -s http://localhost:9876/api/v1/jobs?status=failed | jq '.jobs[].id'

# DLQ pendente
curl -s http://localhost:9876/api/v1/dlq/stats | jq

# Continuous jobs running
curl -s "http://localhost:9876/api/v1/jobs?type=continuous&status=running" | jq '.total'

# Scheduled jobs próximos
curl -s "http://localhost:9876/api/v1/jobs?type=scheduled" | jq '.jobs[] | {id, next_run}'
```

## OpenClaw Jobs (oc-*)

Jobs que executam tarefas AI do OpenClaw usam um wrapper especial.

### ⚠️ REGRA OBRIGATÓRIA: Sync de Timeout

**Ao criar um job OpenClaw que roda via ProcClaw:**

1. **OpenClaw cron:** Definir `timeoutSeconds` no payload
2. **ProcClaw job:** Usar MESMO timeout + 60s buffer
3. **ProcClaw job:** SEMPRE `retry: enabled: false`

**Por quê:**
- OpenClaw pode completar a tarefa (ex: enviar WhatsApp) mas o `openclaw cron run` timeout antes de receber confirmação
- Isso retorna exit code 1 (timeout), não falha real
- Se retry estiver ON, o job roda de novo → mensagem duplicada

**Exemplo correto:**

```yaml
# 1. OpenClaw cron (via openclaw cron add):
payload:
  kind: agentTurn
  message: "..."
  timeoutSeconds: 600  # 10 min

# 2. ProcClaw job (jobs.yaml):
oc-meu-job:
  cmd: python3 ~/.procclaw/scripts/oc-runner.py <job_id> 660000  # 600s + 60s = 660000ms
  retry:
    enabled: false  # SEMPRE false pra jobs OpenClaw
```

**Checklist ao criar job OpenClaw:**
- [ ] Definir `timeoutSeconds` no OpenClaw cron
- [ ] Usar mesmo valor + 60s no oc-runner.py
- [ ] Adicionar `retry: enabled: false`
- [ ] Testar manualmente antes de agendar

### Estrutura
```
~/.procclaw/
├── scripts/
│   └── oc-runner.py          # Wrapper que captura logs do OpenClaw
└── jobs.yaml
    ├── oc-idea-hunter        # Usa: python3 oc-runner.py <job_id> <timeout>
    ├── oc-stock-hunter
    ├── oc-twitter-trends
    └── ...
```

### Ver logs de job OpenClaw
```bash
# Log completo (inclui output do agente)
cat ~/.procclaw/logs/oc-idea-hunter.log | tail -100

# Só erros
cat ~/.procclaw/logs/oc-idea-hunter.error.log
```

### Formato do log
```
[2026-02-09 17:00:01] 🚀 Starting OpenClaw job: idea-hunter
   Timeout: 600000ms (600s)
============================================================
📤 OpenClaw Output:
============================================================
[output do agente - tool calls, respostas, etc]
============================================================
[2026-02-09 17:08:23] ✅ Job completed successfully
```

### Troubleshooting OpenClaw Jobs

**Job falhou com timeout:**
```bash
# Aumentar timeout no jobs.yaml (em ms)
cmd: python3 ~/.procclaw/scripts/oc-runner.py <id> 900000  # 15 min
```

**Verificar se OpenClaw está respondendo:**
```bash
openclaw status
```

**Ver sessão do cron no OpenClaw:**
```bash
openclaw sessions list --limit 5
```

**Rodar job manualmente pra debug:**
```bash
python3 ~/.procclaw/scripts/oc-runner.py <openclaw_cron_id> 300000
```

## Workflow Features

### ETA Scheduling

Schedule jobs to run at a specific time or after a delay.

**Schedule at specific time:**
```bash
curl -s -X POST "http://localhost:9876/api/v1/jobs/<job_id>/schedule?run_at=2026-02-09T10:00:00" | jq
```

**Schedule in N seconds:**
```bash
curl -s -X POST "http://localhost:9876/api/v1/jobs/<job_id>/schedule?run_in=3600" | jq
```

**With timezone:**
```bash
curl -s -X POST "http://localhost:9876/api/v1/jobs/<job_id>/schedule?run_at=2026-02-09T10:00:00&timezone=America/Sao_Paulo" | jq
```

**Cancel scheduled job:**
```bash
curl -s -X DELETE "http://localhost:9876/api/v1/jobs/<job_id>/schedule" | jq
```

**List all ETA jobs:**
```bash
curl -s http://localhost:9876/api/v1/eta | jq
```

### Task Revocation

Cancel queued/scheduled jobs or terminate running jobs.

**Revoke a job:**
```bash
curl -s -X POST "http://localhost:9876/api/v1/jobs/<job_id>/revoke?reason=maintenance" | jq
```

**Revoke and terminate if running:**
```bash
curl -s -X POST "http://localhost:9876/api/v1/jobs/<job_id>/revoke?terminate=true" | jq
```

**Revoke with expiry (auto-unrevoke after N seconds):**
```bash
curl -s -X POST "http://localhost:9876/api/v1/jobs/<job_id>/revoke?expires_in=3600" | jq
```

**Remove revocation:**
```bash
curl -s -X DELETE "http://localhost:9876/api/v1/jobs/<job_id>/revoke" | jq
```

**List all revocations:**
```bash
curl -s http://localhost:9876/api/v1/revocations | jq
```

### Job Results

Get captured output from job runs.

**Get last result:**
```bash
curl -s http://localhost:9876/api/v1/jobs/<job_id>/results | jq
```

**Get specific run result:**
```bash
curl -s "http://localhost:9876/api/v1/jobs/<job_id>/results?run_id=123" | jq
```

Response includes:
- `exit_code` - Process exit code
- `success` - True if exit_code == 0
- `stdout_tail` - Last N chars of stdout
- `stderr_tail` - Last N chars of stderr
- `duration_seconds` - How long it ran
- `custom_data` - Any JSON written to result file

### Workflows (Task Composition)

Compose multiple jobs into workflows: chains, groups, or chords.

**Workflow Types:**

| Type | Description | Example |
|------|-------------|---------|
| chain | Sequential A→B→C | Build → Test → Deploy |
| group | Parallel A+B+C | Run tests concurrently |
| chord | Group + Callback | Process files → Notify |

**List workflows:**
```bash
curl -s http://localhost:9876/api/v1/workflows | jq
```

**Get workflow details:**
```bash
curl -s http://localhost:9876/api/v1/workflows/<workflow_id> | jq
```

**Start a workflow:**
```bash
curl -s -X POST http://localhost:9876/api/v1/workflows/<workflow_id>/run | jq
```

**List workflow runs:**
```bash
curl -s http://localhost:9876/api/v1/workflows/<workflow_id>/runs | jq
```

**Get run details:**
```bash
curl -s http://localhost:9876/api/v1/workflow-runs/<run_id> | jq
```

**Cancel a running workflow:**
```bash
curl -s -X POST http://localhost:9876/api/v1/workflow-runs/<run_id>/cancel | jq
```

### Workflow Configuration

Define workflows in `~/.procclaw/workflows.yaml`:

```yaml
workflows:
  deploy-pipeline:
    name: "Deploy Pipeline"
    type: chain  # chain, group, chord
    jobs:
      - build
      - test
      - deploy
    on_failure: stop  # stop or continue

  parallel-tests:
    name: "Parallel Tests"
    type: group
    jobs:
      - test-unit
      - test-integration
      - test-e2e
    max_parallel: 5

  process-and-notify:
    name: "Process and Notify"
    type: chord
    jobs:
      - process-a
      - process-b
      - process-c
    callback: send-notification
```

### Result Passing Between Jobs

In chain workflows, each job receives info about the previous job via env vars:

| Variable | Description |
|----------|-------------|
| `PROCCLAW_PREV_JOB_ID` | ID of previous job |
| `PROCCLAW_PREV_EXIT_CODE` | Exit code (0 = success) |
| `PROCCLAW_PREV_SUCCESS` | "1" or "0" |
| `PROCCLAW_PREV_STDOUT` | Tail of stdout |
| `PROCCLAW_PREV_DURATION` | Duration in seconds |

In chord callbacks, aggregated results are available:

| Variable | Description |
|----------|-------------|
| `PROCCLAW_WORKFLOW_ID` | Workflow ID |
| `PROCCLAW_WORKFLOW_SUCCESS` | "1" if all succeeded |
| `PROCCLAW_WORKFLOW_JOB_IDS` | Comma-separated job IDs |
| `PROCCLAW_WORKFLOW_RESULTS_FILE` | Path to JSON with all results |

## Example Workflow Interactions

**User:** "agenda o backup pra meia-noite"
→ `POST /api/v1/jobs/backup/schedule?run_at=2026-02-09T00:00:00`

**User:** "cancela todos os jobs agendados do scanner"
→ `DELETE /api/v1/jobs/scanner/schedule`

**User:** "pausa o deploy por 1 hora"
→ `POST /api/v1/jobs/deploy/revoke?expires_in=3600&reason=maintenance`

**User:** "roda o pipeline de deploy"
→ `POST /api/v1/workflows/deploy-pipeline/run`

**User:** "qual foi o resultado do último build?"
→ `GET /api/v1/jobs/build/results`

**User:** "cancela o workflow 42"
→ `POST /api/v1/workflow-runs/42/cancel`
