# Feature: OpenClaw Session Logs in ProcClaw

## Objetivo
Ver os logs completos da sessão OpenClaw que rodou um job `type: openclaw`, não apenas o stdout do `oc-runner.py`.

## Problema Atual
1. Jobs `openclaw` rodam via `oc-runner.py` → `openclaw cron run <id>`
2. OpenClaw cria sessão **isolated** (ephemeral)
3. Sessão é destruída após execução
4. Só temos stdout do runner, não o conteúdo da conversa AI

## Solução Proposta

### Fase 1: Captura no oc-runner.py

**Modificar `oc-runner.py` para:**
1. Usar `--expect-final` para capturar resposta completa
2. Parsear output JSON do OpenClaw
3. Salvar session transcript em arquivo separado
4. Registrar path do transcript no stdout para ProcClaw capturar

**Output esperado:**
```
[2026-02-09T10:00:00] Starting OpenClaw cron job: 626f7b9e-...
[2026-02-09T10:00:05] Session started
[2026-02-09T10:02:30] Session completed
[2026-02-09T10:02:30] Transcript saved: ~/.procclaw/transcripts/626f7b9e-2026-02-09T10-00-00.json
[2026-02-09T10:02:30] Exit code: 0
SESSION_TRANSCRIPT=/Users/makiavel/.procclaw/transcripts/626f7b9e-2026-02-09T10-00-00.json
```

### Fase 2: Storage no ProcClaw

**Novo campo em JobRun:**
```python
class JobRun(BaseModel):
    ...
    session_transcript_path: str | None = None  # path to OpenClaw session log
```

**Migração DB:**
```sql
ALTER TABLE job_runs ADD COLUMN session_transcript_path TEXT;
```

**Supervisor:** Parsear stdout procurando `SESSION_TRANSCRIPT=` e salvar path.

### Fase 3: API Endpoint

**GET /api/v1/runs/{run_id}/session**
```json
{
  "run_id": 123,
  "job_id": "oc-idea-hunter",
  "has_transcript": true,
  "transcript": {
    "started_at": "2026-02-09T10:00:00",
    "finished_at": "2026-02-09T10:02:30",
    "model": "claude-sonnet-4-5",
    "messages": [
      {"role": "system", "content": "..."},
      {"role": "user", "content": "# Idea Hunter\n..."},
      {"role": "assistant", "content": "Encontrei 5 ideias...", "tool_calls": [...]},
      ...
    ],
    "token_usage": {
      "input": 15000,
      "output": 3500,
      "total": 18500
    }
  }
}
```

### Fase 4: UI

**Na Runs tab:**
- Nova coluna "Session" com ícone 🧠 se tem transcript
- Clicar abre modal com a conversa formatada

**No Run Detail Modal:**
- Seção "OpenClaw Session" 
- Mostra mensagens formatadas (user/assistant)
- Tool calls expandíveis
- Token usage

**Session Viewer Modal:**
- Estilo chat (balões de mensagem)
- Syntax highlighting para código
- Tool calls como cards colapsáveis
- Filtro: All | User | Assistant | Tools

## Formato do Transcript

```json
{
  "version": 1,
  "cron_job_id": "626f7b9e-67a8-4fe4-8379-1700fb4e4a0c",
  "cron_job_name": "Idea Hunter",
  "procclaw_job_id": "oc-idea-hunter",
  "procclaw_run_id": 123,
  "started_at": "2026-02-09T10:00:00Z",
  "finished_at": "2026-02-09T10:02:30Z",
  "duration_seconds": 150,
  "model": "anthropic/claude-sonnet-4-5",
  "status": "completed",
  "messages": [
    {
      "role": "user",
      "content": "# Idea Hunter\n\nBusca ideias de apps iOS..."
    },
    {
      "role": "assistant",
      "content": "Vou buscar ideias nas fontes configuradas...",
      "tool_calls": [
        {
          "id": "call_123",
          "name": "web_search",
          "arguments": {"query": "iOS app ideas 2026"},
          "result": "..."
        }
      ]
    },
    {
      "role": "assistant", 
      "content": "Encontrei 5 ideias interessantes:\n\n1. **AppName**..."
    }
  ],
  "token_usage": {
    "input_tokens": 15000,
    "output_tokens": 3500,
    "total_tokens": 18500,
    "estimated_cost_usd": 0.08
  },
  "delivery": {
    "mode": "announce",
    "channel": "whatsapp",
    "sent": true
  }
}
```

## Storage & Cleanup

**Localização:** `~/.procclaw/transcripts/`

**Naming:** `{cron_job_id}-{ISO_timestamp}.json`

**Cleanup policy (config):**
```yaml
transcripts:
  retention_days: 30
  max_size_mb: 500
```

**Cleanup job:** ProcClaw scheduled job que limpa transcripts antigos

## Dependências

1. **OpenClaw CLI** deve suportar output estruturado do `cron run`
   - Verificar se `--expect-final` retorna JSON parseável
   - Pode precisar de feature request no OpenClaw

2. **oc-runner.py** precisa ser refatorado significativamente

## Estimativa

| Fase | Esforço | Dependência |
|------|---------|-------------|
| 1. oc-runner.py | 2h | OpenClaw CLI output |
| 2. DB + Model | 30min | - |
| 3. API | 1h | Fase 2 |
| 4. UI | 3h | Fase 3 |
| 5. Cleanup job | 30min | - |
| **Total** | **~7h** | |

## Próximos Passos

1. [ ] Testar `openclaw cron run --expect-final` e verificar output
2. [ ] Definir formato exato do transcript
3. [ ] Implementar Fase 1 (oc-runner.py)
4. [ ] Implementar Fases 2-4 incrementalmente
5. [ ] Adicionar cleanup job

## Alternativas Consideradas

**A. Persistir sessions no OpenClaw**
- Pró: Menos modificação no oc-runner
- Contra: Depende de mudança no OpenClaw, mais complexo

**B. Usar delivery/announce como fonte**
- Pró: Já existe
- Contra: Só tem o resultado final, não a conversa completa

**C. Webhook do OpenClaw → ProcClaw**
- Pró: Real-time
- Contra: Complexidade de integração, nova infra
