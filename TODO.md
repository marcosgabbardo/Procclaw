# ProcClaw TODO

## Backlog de Melhorias

### 1. [UI] YAML Editor no Modal de Edição
**Status:** Pendente  
**Prioridade:** Alta

Assim como o modal de criar job tem toggle para ver/editar YAML, o modal de edição também deveria ter.

**Implementação:**
- Adicionar toggle "📝 YAML" no modal de edição
- Reutilizar lógica do modal de criação
- Sincronizar campos ↔ YAML bidirecionalmente
- Usar endpoint PATCH existente para salvar

**Arquivos:**
- `src/procclaw/web/static/index.html` - adicionar toggle e textarea no edit modal

---

### 2. [Core] Tasks Compostas (Chain, Group, Chord)
**Status:** Pendente  
**Prioridade:** Média

Permitir criar workflows complexos com múltiplos jobs:

#### Chain (A → B → C) - Sequencial
```yaml
composite-job:
  type: chain
  steps:
    - job_id: step-a
    - job_id: step-b
    - job_id: step-c
```
Cada step só executa após o anterior terminar com sucesso.

#### Group (A + B + C) - Paralelo
```yaml
composite-job:
  type: group
  jobs:
    - job_id: task-a
    - job_id: task-b
    - job_id: task-c
```
Todos executam simultaneamente.

#### Chord (Group + Callback)
```yaml
composite-job:
  type: chord
  parallel:
    - job_id: task-a
    - job_id: task-b
  callback: job_id: final-task
```
Executa grupo em paralelo, depois executa callback quando todos terminarem.

**Implementação:**
- Novo `JobType`: CHAIN, GROUP, CHORD
- Novo modelo `CompositeJob` com lista de steps
- `CompositeExecutor` no supervisor para orquestrar
- Tracking de estado: qual step está rodando, quais completaram
- UI: visualização de progresso do composite

**Arquivos:**
- `src/procclaw/models.py` - novos tipos e modelos
- `src/procclaw/core/supervisor.py` - lógica de execução
- `src/procclaw/core/composite.py` - novo executor de composites
- `src/procclaw/web/static/index.html` - UI para criar/visualizar

---

### 3. [CLI] CLI Completo sem Web
**Status:** Pendente  
**Prioridade:** Média

Todas as funcionalidades da web disponíveis via CLI, sem precisar do daemon web.

**Comandos propostos:**
```bash
# Jobs
procclaw jobs list [--status running|stopped|failed]
procclaw jobs show <job_id>
procclaw jobs create <yaml_file>
procclaw jobs edit <job_id> [--yaml]
procclaw jobs delete <job_id>
procclaw jobs enable <job_id>
procclaw jobs disable <job_id>

# Execução
procclaw run <job_id> [--wait] [--timeout 60]
procclaw stop <job_id>
procclaw restart <job_id>

# Logs
procclaw logs <job_id> [-f|--follow] [--lines 100] [--source file|sqlite]
procclaw logs daemon [-f]

# Runs (histórico)
procclaw runs list [--job <job_id>] [--status success|failed]
procclaw runs show <run_id>
procclaw runs logs <run_id>

# Tags
procclaw tags list
procclaw tags add <job_id> <tag>
procclaw tags remove <job_id> <tag>

# Composites
procclaw chain <job_a> <job_b> <job_c> [--wait]
procclaw group <job_a> <job_b> <job_c> [--wait]

# Config
procclaw config show
procclaw config edit
procclaw config validate
```

**Implementação:**
- Modo "CLI-only" que não inicia servidor web
- Operações diretas no YAML + SQLite (sem API)
- Ou: CLI como cliente da API (quando daemon roda)
- Flag `--no-web` no daemon start

**Arquivos:**
- `src/procclaw/cli/` - reorganizar comandos
- `src/procclaw/cli/jobs.py` - comandos de jobs
- `src/procclaw/cli/runs.py` - comandos de histórico
- `src/procclaw/cli/composite.py` - comandos de chain/group

---

## Concluídos

- [x] Auto-start continuous jobs on daemon startup
- [x] SQLite fallback for logs when files are empty
- [x] Log source indicator in UI (File/SQLite)
- [x] ONESHOT job type
- [x] Missed run catchup on daemon restart
- [x] file_log_mode config (keep/delete)
- [x] Runs tab with execution history
- [x] Column sorting in jobs table
- [x] Force run button for scheduled jobs
- [x] PLANNED status for scheduled jobs with future runs
