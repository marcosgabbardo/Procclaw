# Self-Healing v2 - Run Scope Specification

## Problema Atual
O código atual pega os **últimos 20 runs** fixo (`limit=20`), sem considerar:
- Modo (reactive vs proactive)
- Quando foi a última review
- Correções manuais feitas entre reviews

## Comportamento Esperado

### Modo REACTIVE (on failure)
- **Trigger**: Quando job falha
- **Runs analisados**: **Apenas o run que falhou** (último run com `exit_code != 0`)
- **Contexto adicional**: Pode incluir últimos N runs para comparação de padrões, mas foco é no run que falhou
- **Logs**: Apenas do run que falhou

### Modo PROACTIVE (periodic review)
- **Trigger**: Conforme schedule configurado (hourly/daily/weekly/etc)
- **Runs analisados**: **Todos os runs desde a última review completada**
- **Se não teve review anterior**: Últimos N dias (configurável, default 7 dias?)
- **Min runs**: Respeitar `review_schedule.min_runs` - pula review se não atingiu mínimo
- **Logs**: Dos runs no período

## Implementação

### 1. Modificar `_collect_context()` em `healing_engine.py`

```python
async def _collect_context(
    self,
    job_id: str,
    job_config: JobConfig,
    scope: Any,
    trigger: str = "scheduled",  # ou "failure"
    failed_run_id: int | None = None,  # para reactive
) -> AnalysisContext:
    
    healing_config = job_config.self_healing
    
    if healing_config.mode == HealingMode.REACTIVE:
        # Modo REACTIVE: só o run que falhou
        if failed_run_id:
            runs = [self.db.get_run(failed_run_id)]
        else:
            # Fallback: último run failed
            runs = self.db.get_runs(job_id=job_id, status="failed", limit=1)
        
    else:
        # Modo PROACTIVE: runs desde última review
        last_review = self.db.get_last_completed_review(job_id)
        
        if last_review:
            since = last_review.finished_at
        else:
            # Sem review anterior: últimos 7 dias
            since = datetime.now() - timedelta(days=7)
        
        runs = self.db.get_runs(job_id=job_id, since=since)
        
        # Checar min_runs
        min_runs = healing_config.review_schedule.min_runs
        if len(runs) < min_runs:
            raise SkipReviewError(f"Only {len(runs)} runs since last review, need {min_runs}")
```

### 2. Adicionar método `get_last_completed_review()` no DB

```python
def get_last_completed_review(self, job_id: str) -> HealingReview | None:
    """Get the most recent completed review for a job."""
    cur = self.conn.execute("""
        SELECT * FROM healing_reviews 
        WHERE job_id = ? AND status = 'completed'
        ORDER BY finished_at DESC 
        LIMIT 1
    """, (job_id,))
    row = cur.fetchone()
    return HealingReview(**dict(row)) if row else None
```

### 3. Adicionar filtro `since` em `get_runs()`

```python
def get_runs(
    self,
    job_id: str | None = None,
    since: datetime | None = None,  # NOVO
    status: str | None = None,
    limit: int = 100,
) -> list[JobRun]:
    ...
    if since:
        query += " AND started_at >= ?"
        params.append(since.isoformat())
```

## Configuração Adicional (opcional)

Considerar adicionar em `ReviewScheduleConfig`:
- `max_lookback_days: int = 7` - máximo de dias pra trás se não teve review anterior
- Já existe: `min_runs: int = 5` - mínimo de runs pra disparar review

## Edge Cases

1. **Job nunca rodou**: Pula review
2. **Job rodou mas menos que min_runs**: Pula review  
3. **Última review foi há muito tempo**: Limitar a max_lookback_days
4. **Correção manual entre reviews**: Os runs antes da correção serão analisados, mas a AI deveria perceber que runs recentes estão OK

## TODO

- [ ] Implementar `get_last_completed_review()` no DB
- [ ] Adicionar parâmetro `since` em `get_runs()`
- [ ] Modificar `_collect_context()` para diferenciar REACTIVE vs PROACTIVE
- [ ] Adicionar `failed_run_id` como parâmetro quando trigger é failure
- [ ] Testes para ambos os modos
- [ ] Considerar adicionar `max_lookback_days` na config
