# ProcClaw Queue System - Feature Plan

## Problema
Jobs que chamam OpenClaw/Claude rodam em paralelo → rate limit.
Precisamos serializar execução de jobs que competem pelo mesmo recurso.

---

## Conceito

```
┌─────────────────────────────────────────────────────────┐
│                    PROCCLAW                             │
│                                                         │
│  Queue: "openclaw"          Queue: "default"            │
│  ┌─────────────────┐        ┌─────────────────┐        │
│  │ idea-hunter ⏳   │        │ backup ▶️        │        │
│  │ stock-hunter 🔜 │        │ cleanup ▶️       │        │
│  │ skill-scout 🔜  │        └─────────────────┘        │
│  └─────────────────┘                                    │
│                             Sem queue (paralelo)        │
│  max_concurrent: 1          ┌─────────────────┐        │
│  (um de cada vez)           │ email-watcher ▶️ │        │
│                             │ web-scraper ▶️   │        │
│                             └─────────────────┘        │
└─────────────────────────────────────────────────────────┘
```

**Regras:**
- Jobs na mesma queue: executam sequencialmente
- Jobs em queues diferentes: executam em paralelo
- Jobs sem queue: comportamento atual (paralelo)

---

## Data Model

### Option A: Queues Implícitas (Simples)
Queues são criadas automaticamente quando referenciadas no job.

```yaml
# jobs.yaml
jobs:
  idea-hunter:
    queue: openclaw      # ← queue criada implicitamente
    cmd: python3 oc-runner.py idea-hunter
    
  stock-hunter:
    queue: openclaw      # ← mesma queue = serializado
    cmd: python3 oc-runner.py stock-hunter
    
  email-watcher:
    # sem queue = paralelo (comportamento atual)
    cmd: python3 email-watcher.py
```

**Pros:** Simples, zero config extra
**Cons:** Sem settings por queue (max_concurrent sempre 1)

### Option B: Queues Explícitas (Flexível)
Queues definidas separadamente com settings.

```yaml
# jobs.yaml
queues:
  openclaw:
    max_concurrent: 1      # um job por vez
    priority: 10           # maior = mais prioritário
    
  scraping:
    max_concurrent: 3      # até 3 jobs simultâneos
    priority: 5

jobs:
  idea-hunter:
    queue: openclaw
    queue_priority: 1      # prioridade dentro da queue
```

**Pros:** Flexível, suporta concorrência > 1
**Cons:** Mais complexo, mais config

### Recomendação: **Option A primeiro, evoluir pra B se precisar**

---

## Campos Novos

### JobConfig
```python
class JobConfig(BaseModel):
    # ... existing fields ...
    queue: str | None = None           # Nome da queue (None = sem queue)
    queue_priority: int = 0            # Prioridade na queue (maior = primeiro)
    queue_timeout_seconds: int | None = None  # Max tempo esperando na queue
```

### Novo: QueueState (em memória, não persistido)
```python
@dataclass
class QueueState:
    name: str
    running_jobs: set[str]             # Jobs atualmente executando
    pending_jobs: list[str]            # Jobs aguardando (ordenado por prioridade)
    max_concurrent: int = 1            # Quantos podem rodar juntos
```

---

## Fluxo de Execução

### Ao iniciar um job:
```
1. Job quer iniciar (trigger: scheduled, manual, retry)
2. job.queue existe?
   ├─ NÃO → iniciar imediatamente (comportamento atual)
   └─ SIM → verificar queue
            ├─ Queue tem slot livre? (running < max_concurrent)
            │   ├─ SIM → adicionar aos running, iniciar job
            │   └─ NÃO → adicionar aos pending (ordenado por prioridade)
            └─ Se adicionado aos pending:
                - Logar "Job X queued, position Y"
                - Se queue_timeout configurado, agendar timeout
```

### Ao finalizar um job:
```
1. Job terminou (sucesso, falha, ou stop manual)
2. job.queue existe?
   ├─ NÃO → nada a fazer
   └─ SIM → remover dos running
            └─ pending não vazio?
                ├─ SIM → pegar próximo (maior prioridade), iniciar
                └─ NÃO → nada a fazer
```

### Edge Cases:
| Situação | Comportamento |
|----------|---------------|
| Job pausado enquanto na queue | Remove da queue pending |
| Job pausado enquanto running | Remove de running, próximo inicia |
| Manual start de job em queue ocupada | Entra na queue (não bypassa) |
| Force start (admin) | Bypassa queue, roda imediatamente |
| Queue timeout | Remove da pending, marca como "queue_timeout" |
| Job disabled enquanto pending | Remove da queue |

---

## API Endpoints

### Novos Endpoints:
```
GET  /api/v1/queues                    # Lista todas as queues ativas
GET  /api/v1/queues/{name}             # Detalhes de uma queue
GET  /api/v1/queues/{name}/jobs        # Jobs na queue (running + pending)
POST /api/v1/jobs/{id}/force-start     # Bypass queue (admin)
```

### Endpoints Modificados:
```
GET  /api/v1/jobs/{id}                 # Adiciona: queue, queue_position
POST /api/v1/jobs/{id}/start           # Respeita queue
```

### Response Examples:

**GET /api/v1/queues**
```json
{
  "queues": [
    {
      "name": "openclaw",
      "max_concurrent": 1,
      "running": ["idea-hunter"],
      "running_count": 1,
      "pending": ["stock-hunter", "skill-scout"],
      "pending_count": 2
    }
  ]
}
```

**GET /api/v1/jobs/stock-hunter**
```json
{
  "id": "stock-hunter",
  "status": "queued",           // novo status!
  "queue": "openclaw",
  "queue_position": 1,          // 0-indexed, posição na fila
  "queue_wait_since": "2026-02-10T12:30:00Z"
}
```

---

## Web UI

### Jobs List
```
┌──────────────────────────────────────────────────────────┐
│ Jobs                                         [+ Add Job] │
├──────────────────────────────────────────────────────────┤
│ 🟢 idea-hunter      running    queue: openclaw          │
│ 🟡 stock-hunter     queued #1  queue: openclaw          │
│ 🟡 skill-scout      queued #2  queue: openclaw          │
│ 🟢 email-watcher    running    (no queue)               │
│ ⚫ backup           stopped    (no queue)               │
└──────────────────────────────────────────────────────────┘
```

### Job Edit/Create Form
```
┌──────────────────────────────────────────────────────────┐
│ Edit Job: stock-hunter                                   │
├──────────────────────────────────────────────────────────┤
│ Name:     [stock-hunter        ]                         │
│ Command:  [python3 oc-runner.py stock-hunter]            │
│ Type:     (•) Scheduled  ( ) Continuous  ( ) Manual      │
│ Schedule: [0 12,18 * * *       ]                         │
│                                                          │
│ ── Queue Settings ──────────────────────────────         │
│ Queue:    [openclaw           ▼]  [+ New Queue]          │
│           □ No queue (run in parallel)                   │
│ Priority: [0    ] (higher = runs first)                  │
│ Timeout:  [     ] seconds (blank = no timeout)           │
│                                                          │
│                              [Cancel]  [Save]            │
└──────────────────────────────────────────────────────────┘
```

### Queues Tab (nova)
```
┌──────────────────────────────────────────────────────────┐
│ Queues                                                   │
├──────────────────────────────────────────────────────────┤
│ openclaw                                                 │
│   Running: idea-hunter (started 5m ago)                  │
│   Pending: stock-hunter (#1), skill-scout (#2)           │
│   ────────────────────────────────────────────           │
│ scraping                                                 │
│   Running: (none)                                        │
│   Pending: (none)                                        │
└──────────────────────────────────────────────────────────┘
```

---

## Novo Status: "queued"

Adicionar ao enum JobStatus:
```python
class JobStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    COMPLETED = "completed"
    QUEUED = "queued"        # ← NOVO
    PAUSED = "paused"
```

---

## Implementação - Fases

### Fase 1: Core (MVP)
- [ ] Adicionar `queue` field ao JobConfig
- [ ] Criar QueueManager class
- [ ] Modificar Supervisor.start_job() pra checar queue
- [ ] Modificar _on_process_exit() pra processar próximo da queue
- [ ] Adicionar status "queued"
- [ ] Testes unitários

### Fase 2: API
- [ ] GET /api/v1/queues
- [ ] GET /api/v1/queues/{name}
- [ ] Modificar job endpoints pra incluir queue info
- [ ] POST /api/v1/jobs/{id}/force-start

### Fase 3: Web UI
- [ ] Queue selector no job form
- [ ] Mostrar queue status na lista de jobs
- [ ] Nova tab "Queues"

### Fase 4: Extras (se precisar)
- [ ] queue_priority por job
- [ ] queue_timeout
- [ ] max_concurrent > 1 (queues explícitas)
- [ ] Persistir queue state (sobreviver restart)

---

## Decisões Pendentes

1. **Queues implícitas vs explícitas?**
   - Recomendo: implícitas primeiro (simples)

2. **Queue state persiste no restart?**
   - Recomendo: não inicialmente (jobs pending viram "stopped", re-triggar no próximo schedule)

3. **Manual start bypassa queue?**
   - Recomendo: não, entra na queue. Ter `force-start` separado pra admin.

4. **Mostrar tempo estimado na queue?**
   - Recomendo: não (complexo, impreciso)

---

## Arquivos a Modificar

```
src/procclaw/
├── models.py                 # JobConfig.queue, JobStatus.QUEUED
├── core/
│   ├── queue_manager.py      # NOVO - lógica de filas
│   └── supervisor.py         # integrar QueueManager
├── api/
│   └── server.py             # novos endpoints
└── web/
    └── templates/            # UI updates
```

---

*Criado: 2026-02-10*
