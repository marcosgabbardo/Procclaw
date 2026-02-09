# Self-Healing Jobs - Design Plan

**Status:** Planning
**Created:** 2026-02-09
**Author:** C3PO + Marcos

## Overview

Quando um job falha, OpenClaw/Claude automaticamente:
1. **Analisa** o erro (logs, contexto, histórico)
2. **Investiga** a causa raiz (pode envolver múltiplos sistemas)
3. **Tenta corrigir** o problema (ProcClaw, OpenClaw, scripts, configs)
4. **Valida** se o fix funcionou (re-run do job)

---

## ⛔ RESTRIÇÕES CRÍTICAS (HARDCODED)

**O Self-Healing NUNCA pode:**

```python
FORBIDDEN_PATHS_ALWAYS = [
    # ProcClaw source - NEVER MODIFY
    "~/.openclaw/workspace/projects/procclaw/",
    "**/projects/procclaw/**",
    
    # OpenClaw source - NEVER MODIFY  
    "**/node_modules/openclaw/**",
    "/opt/homebrew/lib/node_modules/openclaw/",
    "/usr/local/lib/node_modules/openclaw/",
    
    # System critical
    "~/.ssh/",
    "~/.gnupg/",
    "~/.openclaw/openclaw.json",
    "/etc/",
]

FORBIDDEN_ACTIONS_ALWAYS = [
    "git_push",           # Nunca push automático
    "delete_recursive",   # Nunca rm -rf
    "modify_system",      # Nunca /etc, /usr
]
```

**Estas restrições são hardcoded no código e NÃO podem ser bypassadas por config.**

---

## Permissões

### O que PODE fazer:

| Ação | Descrição |
|------|-----------|
| `edit_script` | Editar scripts em `~/.procclaw/scripts/` |
| `edit_config` | Editar `jobs.yaml`, `.env` files |
| `restart_job` | Re-executar job via API |
| `restart_service` | Reiniciar daemon ProcClaw |
| `edit_openclaw_cron` | Editar crons do OpenClaw (via CLI) |
| `run_command` | Rodar comandos de diagnóstico |
| `commit_local` | Commit local (SEM push) |

### O que NÃO pode fazer:

| Ação | Motivo |
|------|--------|
| Editar source ProcClaw | Proteção de integridade |
| Editar source OpenClaw | Proteção de integridade |
| `git push` | Requer autorização humana |
| Editar `~/.ssh/` | Segurança |
| Editar `/etc/` | Sistema |
| `rm -rf` | Destruição de dados |

---

## Configuração

```yaml
# jobs.yaml
oc-stock-hunter:
  name: Stock Hunter
  cmd: python3 ~/.procclaw/scripts/oc-runner.py xxx 600000
  type: openclaw
  
  # Self-Healing config
  self_healing:
    enabled: true
    
    # Stage 1: Analysis
    analysis:
      include_logs: true
      log_lines: 200
      include_stderr: true
      include_history: 5        # Últimas N execuções
      include_config: true      # Incluir config do job
      
    # Stage 2: Remediation
    remediation:
      enabled: true
      max_attempts: 3           # Máx tentativas de fix
      allowed_actions:
        - edit_script           # Editar scripts
        - edit_config           # Editar configs
        - restart_job           # Re-executar o job
        - restart_service       # Reiniciar daemon
        - edit_openclaw_cron    # Editar cron do OpenClaw
        - run_command           # Rodar comando arbitrário
      forbidden_paths:          # Adicional aos hardcoded
        - ~/secrets/
      require_approval: false   # Se true, pede OK antes de agir
      
    # Notificação
    notify:
      on_analysis: false        # Notificar análise?
      on_fix_attempt: true      # Notificar tentativa de fix?
      on_success: true          # Notificar quando resolver?
      on_give_up: true          # Notificar quando desistir?
      session: main
```

### Níveis de Permissão (Presets)

```yaml
# Conservador (padrão) - só restart
self_healing:
  remediation:
    allowed_actions:
      - restart_job
    require_approval: true

# Moderado - pode editar scripts/configs
self_healing:
  remediation:
    allowed_actions:
      - restart_job
      - edit_config
      - edit_script
    require_approval: false

# Agressivo - pode rodar comandos
self_healing:
  remediation:
    allowed_actions:
      - restart_job
      - edit_config
      - edit_script
      - run_command
      - edit_openclaw_cron
    require_approval: false
```

---

## Fluxo de Execução

```
┌─────────────────────────────────────────────────────────────────┐
│                      JOB FALHA (exit != 0)                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │ self_healing    │
                    │   enabled?      │
                    └────────┬────────┘
                             │
              ┌──────────────┴──────────────┐
              │ NO                          │ YES
              ▼                             ▼
      [Normal alert flow]     ┌─────────────────────────┐
                              │ STAGE 1: COLLECT DATA   │
                              │                         │
                              │ • Logs (stdout/stderr)  │
                              │ • Exit code             │
                              │ • Run history           │
                              │ • Job config            │
                              │ • Session transcript    │
                              │   (if openclaw type)    │
                              └───────────┬─────────────┘
                                          │
                                          ▼
                              ┌─────────────────────────┐
                              │ STAGE 2: SPAWN HEALER   │
                              │                         │
                              │ sessions_spawn(         │
                              │   task=healing_prompt,  │
                              │   label="healer:job_id" │
                              │ )                       │
                              └───────────┬─────────────┘
                                          │
                                          ▼
                              ┌─────────────────────────┐
                              │ CLAUDE ANALYZES         │
                              │                         │
                              │ • Identifies root cause │
                              │ • Checks if fixable     │
                              │ • Plans remediation     │
                              └───────────┬─────────────┘
                                          │
                              ┌───────────┴───────────┐
                              │ FIXABLE?              │
                              └───────────┬───────────┘
                                          │
                    ┌─────────────────────┴─────────────────────┐
                    │ NO                                        │ YES
                    ▼                                           ▼
          ┌─────────────────┐                     ┌─────────────────────────┐
          │ Report analysis │                     │ STAGE 3: REMEDIATE      │
          │ Request human   │                     │                         │
          │ intervention    │                     │ • Apply fix             │
          └─────────────────┘                     │ • Validate path safety  │
                                                  │ • Log all changes       │
                                                  └───────────┬─────────────┘
                                                              │
                                                              ▼
                                                  ┌─────────────────────────┐
                                                  │ STAGE 4: VALIDATE       │
                                                  │                         │
                                                  │ • Re-run the job        │
                                                  │ • Check exit code       │
                                                  └───────────┬─────────────┘
                                                              │
                                                ┌─────────────┴─────────────┐
                                                │ SUCCESS?                  │
                                                └─────────────┬─────────────┘
                                                              │
                              ┌────────────────────────────────┴────────────┐
                              │ YES                                         │ NO
                              ▼                                             ▼
                    ┌─────────────────┐                         ┌───────────────────┐
                    │ ✅ Notify       │                         │ attempt <         │
                    │ success         │                         │ max_attempts?     │
                    │                 │                         └─────────┬─────────┘
                    │ Commit local    │                                   │
                    │ (no push)       │                    ┌──────────────┴──────────┐
                    └─────────────────┘                    │ YES                     │ NO
                                                           ▼                         ▼
                                                  [Loop back to          ┌─────────────────┐
                                                   Stage 2]              │ ❌ Notify       │
                                                                         │ give up         │
                                                                         │                 │
                                                                         │ Request human   │
                                                                         └─────────────────┘
```

---

## Prompt Template

```markdown
🔧 **Self-Healing Request**

## Job Info
- **ID:** {{job_id}}
- **Name:** {{job_name}}
- **Type:** {{job_type}}
- **Exit Code:** {{exit_code}}
- **Duration:** {{duration}}s
- **Attempt:** {{attempt}}/{{max_attempts}}

## Logs (last {{log_lines}} lines)
```
{{logs}}
```

## Stderr
```
{{stderr}}
```

## Run History (last {{history_count}})
| Time | Exit Code | Duration | Error |
|------|-----------|----------|-------|
{{history_table}}

## Job Config
```yaml
{{job_config}}
```

{{#if session_transcript}}
## OpenClaw Session Transcript
{{session_transcript}}
{{/if}}

---

## Allowed Actions
{{allowed_actions}}

## ⛔ FORBIDDEN - NEVER MODIFY:
- ProcClaw source code (~/.openclaw/workspace/projects/procclaw/)
- OpenClaw source code (node_modules/openclaw/)
- SSH keys (~/.ssh/)
- GPG keys (~/.gnupg/)
- System config (/etc/)
- OpenClaw config (~/.openclaw/openclaw.json)

**These restrictions are HARDCODED and cannot be bypassed.**

---

## Instructions

1. **Analyze** the failure - identify root cause
2. **Determine** if it's fixable with allowed actions
3. **If fixable:**
   - Apply the fix
   - Report what you changed
   - The job will be re-run automatically
4. **If NOT fixable:**
   - Explain why
   - Suggest what human intervention is needed

**NEVER:**
- Push to GitHub (commits are local only)
- Modify forbidden paths
- Take actions outside the allowed list
- Delete data without explicit approval
```

---

## Response Schema

Claude responds with structured JSON:

```json
{
  "analysis": {
    "root_cause": "string - description of the problem",
    "confidence": "high | medium | low",
    "category": "script_bug | config_error | dependency | api_external | permission | unknown",
    "details": "string - detailed explanation"
  },
  "fixable": true,
  "actions_taken": [
    {
      "type": "edit_script | edit_config | restart_job | run_command | ...",
      "target": "path or job_id",
      "description": "what was changed",
      "diff": "optional - unified diff of changes"
    }
  ],
  "actions_blocked": [
    {
      "type": "string",
      "reason": "why it was blocked (forbidden path, etc)"
    }
  ],
  "should_retry": true,
  "human_intervention_needed": false,
  "human_intervention_reason": "string - if human needed, why",
  "summary": "string - one-line summary for notification"
}
```

---

## Implementation Phases

### Phase 1: Models & Config (~1h)
- [ ] Add `SelfHealingConfig` to models.py
- [ ] Add `AnalysisConfig` sub-model
- [ ] Add `RemediationConfig` sub-model
- [ ] Update JobConfig to include self_healing field
- [ ] Add FORBIDDEN_PATHS_ALWAYS constant

### Phase 2: Context Collector (~2h)
- [ ] Create `self_healing.py` module
- [ ] Implement `collect_failure_context()`
- [ ] Gather logs, stderr, history
- [ ] Gather job config
- [ ] Gather session transcript (for openclaw jobs)
- [ ] Format context as prompt

### Phase 3: Healer Spawner (~2h)
- [ ] Implement `spawn_healer_session()`
- [ ] Use `sessions_spawn` or similar mechanism
- [ ] Pass context and allowed actions
- [ ] Handle response parsing

### Phase 4: Action Executor (~3h)
- [ ] Implement path validation (forbidden paths check)
- [ ] Implement `edit_script` action
- [ ] Implement `edit_config` action
- [ ] Implement `restart_job` action
- [ ] Implement `run_command` action
- [ ] Implement `edit_openclaw_cron` action
- [ ] All actions log what they do

### Phase 5: Validation Loop (~2h)
- [ ] After fix, trigger job re-run
- [ ] Check result
- [ ] Loop if failed (up to max_attempts)
- [ ] Notify on success/give-up

### Phase 6: Integration (~2h)
- [ ] Hook into supervisor's job failure handler
- [ ] Add CLI commands for manual healing trigger
- [ ] Add API endpoint to trigger healing
- [ ] Add UI indicator for healing in progress

### Phase 7: Testing (~2h)
- [ ] Test with script syntax error
- [ ] Test with config error
- [ ] Test with API key expired
- [ ] Test forbidden path protection
- [ ] Test max_attempts limit

---

## Example Scenarios

### Scenario 1: Script Syntax Error

**Failure:**
```
SyntaxError: unexpected EOF while parsing
  File "scanner.py", line 42
```

**Analysis:**
```json
{
  "root_cause": "Missing closing parenthesis on line 42",
  "confidence": "high",
  "category": "script_bug"
}
```

**Fix:**
```python
# Added missing )
data = fetch_data(url)  # was: fetch_data(url
```

**Result:** ✅ Job passes on retry

---

### Scenario 2: API Key Expired

**Failure:**
```
HTTPError: 401 Unauthorized
```

**Analysis:**
```json
{
  "root_cause": "API key expired or invalid",
  "confidence": "high",
  "category": "config_error"
}
```

**Fix:** Check for backup key in .env.backup, rotate if found

**Result:** ✅ Job passes with new key

---

### Scenario 3: External API Down

**Failure:**
```
ConnectionError: Failed to connect to api.example.com
```

**Analysis:**
```json
{
  "root_cause": "External API unreachable",
  "confidence": "high",
  "category": "api_external",
  "fixable": false,
  "human_intervention_needed": true,
  "human_intervention_reason": "External service down, need to wait or use alternative"
}
```

**Result:** ❌ Notifies human, doesn't attempt fix

---

### Scenario 4: Forbidden Path Attempt

**Failure:** Job tries to fix by editing ProcClaw source

**Analysis:** Identifies fix would require editing supervisor.py

**Blocked:**
```json
{
  "actions_blocked": [
    {
      "type": "edit_script",
      "target": "~/.openclaw/workspace/projects/procclaw/src/procclaw/core/supervisor.py",
      "reason": "FORBIDDEN: Cannot modify ProcClaw source code"
    }
  ],
  "human_intervention_needed": true,
  "human_intervention_reason": "Fix requires modifying ProcClaw source - protected path"
}
```

**Result:** ❌ Reports to human, does not modify

---

## Estimated Total: ~14h

| Phase | Time |
|-------|------|
| Models & Config | 1h |
| Context Collector | 2h |
| Healer Spawner | 2h |
| Action Executor | 3h |
| Validation Loop | 2h |
| Integration | 2h |
| Testing | 2h |
| **Total** | **14h** |

---

## Future Enhancements

- [ ] Learning from fixes (store successful fixes for similar errors)
- [ ] Multi-job correlation (detect related failures)
- [ ] Proactive healing (detect issues before failure)
- [ ] Cost tracking (tokens used per healing attempt)
- [ ] Approval workflow (Slack/Discord integration)
