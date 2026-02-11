# Self-Healing v2 - Queue & Prioridade

## Problema

1. Healing usa AI (OpenClaw) para análise e sugestões
2. Múltiplos healings rodando em paralelo = competição por tokens
3. Healing deveria ter **menor prioridade** que jobs normais
4. Queue "openclaw" existe mas não é obrigatória/default

## Objetivo

- Healings rodam em sequência (não paralelo)
- Healings esperam jobs openclaw terminarem antes de rodar
- Healings têm prioridade mais baixa que jobs normais

## Solução Proposta

### Usar ConcurrencyLimiter existente

O ProcClaw já tem `ConcurrencyLimiter` que gerencia queues. Vamos usar ele para healing.

### Mudanças

#### 1. Expor ConcurrencyLimiter para HealingEngine

```python
# supervisor.py
class Supervisor:
    def __init__(self, ...):
        self._concurrency_limiter = ConcurrencyLimiter(...)
        self._healing_engine = HealingEngine(
            db=self._db,
            supervisor=self,
            concurrency_limiter=self._concurrency_limiter,  # NOVO
        )
```

#### 2. HealingEngine usa queue antes de rodar

```python
# healing_engine.py
class HealingEngine:
    HEALING_QUEUE = "openclaw"  # Usa mesma queue dos jobs openclaw
    HEALING_PRIORITY = -10      # Prioridade baixa (jobs normais = 0)
    
    async def run_review(self, job_id, ...):
        # Adquirir slot na queue antes de rodar
        await self._concurrency_limiter.acquire(
            queue=self.HEALING_QUEUE,
            job_id=f"healing:{job_id}",
            priority=self.HEALING_PRIORITY,
        )
        try:
            # ... lógica existente do review ...
        finally:
            self._concurrency_limiter.release(
                queue=self.HEALING_QUEUE,
                job_id=f"healing:{job_id}",
            )
```

#### 3. ProactiveScheduler também usa queue

```python
# healing_engine.py - ProactiveScheduler
async def _run_scheduled_reviews(self):
    for job_id, job_config in jobs_to_review:
        # Não roda direto, usa queue
        await self.engine.run_review(job_id, job_config)
        # run_review já cuida da queue internamente
```

### Comportamento Esperado

```
Timeline:
├─ Job OpenClaw A inicia (queue=openclaw, priority=0)
├─ Healing para Job X quer rodar (queue=openclaw, priority=-10)
│   └─ ESPERA - job A tem prioridade maior
├─ Job A termina
├─ Job OpenClaw B inicia (queue=openclaw, priority=0)
│   └─ PASSA NA FRENTE do healing (prioridade maior)
├─ Job B termina
├─ Healing para Job X finalmente roda
├─ Healing para Job Y quer rodar
│   └─ ESPERA - healing X ainda rodando (mesma queue)
└─ Healing X termina, Healing Y roda
```

### Configuração (opcional, para depois)

Podemos adicionar config global se precisar customizar:

```yaml
# procclaw.yaml
healing:
  queue: "openclaw"     # Queue a usar (default: openclaw)
  priority: -10         # Prioridade (default: -10)
  max_concurrent: 1     # Máximo paralelo (default: 1)
```

Por agora, valores hardcoded são suficientes.

## Descobertas

### ConcurrencyLimiter atual
- ✅ Já suporta prioridade (`priority: int`, lower = higher priority)
- Usa `_db.enqueue_job()` para persistir fila no SQLite
- Jobs normais usam priority=2 por default
- Método `queue_job(job_id, priority, trigger, params)`

### Problema
O ConcurrencyLimiter é feito para **jobs do ProcClaw**, não operações internas.
- Espera que `job_id` seja um job real
- Dequeue chama `supervisor.start_job()` para executar

### Opções Refinadas

**Opção A: Semáforo interno + check jobs rodando**
```python
class HealingEngine:
    def __init__(self, ...):
        self._semaphore = asyncio.Semaphore(1)  # 1 healing por vez
    
    async def run_review(self, ...):
        async with self._semaphore:
            # Esperar jobs openclaw terminarem
            while self._has_openclaw_jobs_running():
                await asyncio.sleep(5)
            # Rodar healing
```
**Pros:** Simples, não precisa modificar ConcurrencyLimiter
**Cons:** Polling (não ideal), healing pode esperar muito

**Opção B: Evento de "slot livre"**
```python
# Supervisor emite evento quando job openclaw termina
self._healing_engine.notify_slot_available()

# HealingEngine espera evento
await self._slot_available.wait()
```
**Pros:** Sem polling
**Cons:** Mais complexo, precisa coordenar eventos

**Opção C: Queue separada para healing no DB** (recomendada)
Criar uma "virtual queue" para healing que usa mesma lógica mas IDs separados:
```python
# healing usa job_id = "healing:original-job-id"
self._concurrency_limiter.queue_job(
    job_id=f"healing:{job_id}",
    priority=100,  # muito baixa
    trigger="healing",
)
```
E modificar o dequeue para chamar healing engine quando é `healing:*`

## Recomendação Final

**Opção A** para MVP - simples e funciona:
1. Semáforo interno no HealingEngine (1 por vez)
2. Antes de rodar, checar se há jobs openclaw running
3. Se sim, esperar com backoff

Depois podemos evoluir para Opção C se precisar.

## TODO (MVP - Opção A)

- [x] Verificar ConcurrencyLimiter API atual (já tem priority!)
- [ ] Adicionar `asyncio.Semaphore(1)` no HealingEngine
- [ ] Adicionar método `_has_openclaw_jobs_running()` no HealingEngine
- [ ] Modificar `run_review()` para:
  - Adquirir semáforo
  - Esperar jobs openclaw terminarem
  - Rodar review
  - Liberar semáforo
- [ ] Passar referência ao supervisor para HealingEngine checar jobs
- [ ] Testes

## Riscos

1. **Deadlock**: Se healing espera job que espera healing
   - Mitigação: Healing nunca bloqueia jobs, só espera
   
2. **Starvation**: Healing nunca roda se sempre tem jobs
   - Mitigação: Aceitar isso por design (healing é low priority)
   - Alternativa: Timeout máximo de espera

3. **Queue não existe**: Se queue "openclaw" não foi criada
   - Mitigação: ConcurrencyLimiter cria queue sob demanda
