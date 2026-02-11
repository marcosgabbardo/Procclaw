# Self-Healing: Business Log Analysis

## Objetivo

Permitir que o self-healing analise **resultados de negócio** (não apenas métricas técnicas) de jobs continuous, gerando sugestões de melhoria na lógica/estratégia do script.

## Exemplo de Uso

Um bot de trading loga decisões e resultados. O self-healing analisa 30 dias de dados e sugere: "win rate em crypto é 40%, em politics é 8% → focar em crypto".

---

## Fase 1: Modelo de Dados

### 1.1 Config do Job (`models.py`)

Adicionar campo `business_log` ao `SelfHealingConfig`:

```python
class BusinessLogConfig(BaseModel):
    """Configuration for business-level log analysis."""
    path: str  # Path to JSONL file with business decisions/outcomes
    summary_path: str | None = None  # Optional separate summary file
    max_lines: int = 500  # Max lines to feed to AI (most recent)
    context_prompt: str | None = None  # Optional domain-specific instructions for AI
    # e.g. "This is a trading bot. Focus on win rate, risk management, entry criteria"
```

No `SelfHealingConfig`:
```python
class SelfHealingConfig(BaseModel):
    ...
    business_log: BusinessLogConfig | None = None
```

### 1.2 YAML do Job

```yaml
polymarket-bot:
  type: continuous
  self_healing:
    enabled: true
    business_log:
      path: ~/.procclaw/logs/polymarket-bot/decisions.jsonl
      max_lines: 500
      context_prompt: |
        This is a Polymarket paper trading bot that buys underdog positions.
        Focus on: win rate by category, entry criteria effectiveness,
        exit timing, capital utilization, and risk/reward ratio.
    review_schedule:
      frequency: weekly
      time: "09:00"
      day: 1  # Monday
      min_runs: 1  # Continuous = 1 run is enough
```

---

## Fase 2: Coleta de Dados

### 2.1 `AnalysisContext` (healing_engine.py)

Adicionar campo:

```python
@dataclass
class AnalysisContext:
    ...
    business_logs: str | None = None  # Raw JSONL content (last N lines)
    business_context: str | None = None  # Domain-specific prompt from config
```

### 2.2 Coletar Business Logs

Em `_build_analysis_context()`, quando `business_log` está configurado:

```python
if healing_config.business_log:
    log_path = Path(healing_config.business_log.path).expanduser()
    if log_path.exists():
        # Read last N lines (most recent data)
        lines = log_path.read_text().splitlines()
        max_lines = healing_config.business_log.max_lines or 500
        recent = lines[-max_lines:]
        context.business_logs = "\n".join(recent)
        context.business_context = healing_config.business_log.context_prompt
```

---

## Fase 3: Prompt de Análise

### 3.1 Prompt Bifurcado

Quando `business_logs` existe, o prompt muda de modo **técnico** para modo **business analyst**:

```python
def _build_analysis_prompt(self, context: AnalysisContext) -> str:
    if context.business_logs:
        return self._build_business_analysis_prompt(context)
    else:
        return self._build_technical_analysis_prompt(context)  # atual
```

### 3.2 Business Analysis Prompt

```markdown
## Role
You are a business strategy analyst reviewing operational data from an automated system.
Your goal is to find patterns in decisions and outcomes that can improve results.

## Domain Context
{business_context or "Analyze the business logic and suggest improvements."}

## Job: {job_id}
### Configuration
{job_config}

### Script
{script_content}

### Business Decision Log (last {N} entries)
{business_logs}

### Technical Context (supplementary)
- Restarts: {restart_count} in last 30 days
- Current uptime: {uptime}
- Health check failures: {health_fail_count}

## Analysis Instructions

Analyze the business decision log and identify:

1. **Performance Patterns**
   - What decisions lead to good outcomes vs bad outcomes?
   - Are there categories/types that perform consistently better or worse?
   - What's the overall success rate and trend?

2. **Decision Quality**
   - Are the entry/exit criteria effective?
   - Are there missed opportunities (too many skips)?
   - Are there bad patterns (repeated mistakes)?

3. **Parameter Optimization**
   - Which thresholds should be tightened or relaxed?
   - What filters are too aggressive or too loose?
   - What timing parameters could improve?

4. **Risk Management**
   - Is capital being used efficiently?
   - Are losses concentrated in specific scenarios?
   - Is there adequate diversification?

5. **Strategy Improvements**
   - What new rules would improve outcomes based on data?
   - What existing rules should be removed or modified?
   - Are there edge cases not being handled?

For each suggestion, provide:
- Data-backed evidence (cite specific entries/patterns from the log)
- Quantified expected impact when possible
- The exact code/config change via proposed_content
```

### 3.3 Manter Formato de Output Igual

O JSON de suggestions é o mesmo. `proposed_content` contém o script modificado ou config modificada. Aprovação e apply funcionam igual.

---

## Fase 4: Business Log Contract (documentação)

### 4.1 JSONL Schema Recomendado

Documentar o formato recomendado para scripts que querem business-level healing:

```jsonl
// Decisão tomada
{"ts":"ISO-8601","event":"<action>","...campos específicos","reasoning":"por que"}

// Resultado de decisão anterior  
{"ts":"ISO-8601","event":"<outcome>","ref":"<id da decisão>","result":"success|failure","...métricas"}

// Resumo periódico
{"ts":"ISO-8601","event":"summary","period":"daily|weekly","...métricas agregadas"}
```

**Campos obrigatórios:**
- `ts` - timestamp ISO-8601
- `event` - tipo do evento

**Campos recomendados:**
- `reasoning` - por que tomou a decisão (crucial pro AI entender)
- `result` / `outcome` - o que aconteceu
- `ref` - referência a decisão anterior (pra correlacionar)

### 4.2 Exemplos por Domínio

**Trading bot:**
```jsonl
{"ts":"...","event":"scan","markets":142,"passed_filter":8}
{"ts":"...","event":"buy","market":"X","odds":0.02,"size":25,"reasoning":"volume spike"}
{"ts":"...","event":"exit","market":"X","pnl":-25,"exit_reason":"time_limit"}
{"ts":"...","event":"summary","period":"daily","trades":3,"pnl":-42,"win_rate":0.33}
```

**Lead scraper:**
```jsonl
{"ts":"...","event":"scrape","source":"linkedin","found":50,"new":12}
{"ts":"...","event":"qualify","lead":"Company X","score":85,"reasoning":"matches ICP, 50+ employees"}
{"ts":"...","event":"skip","lead":"Company Y","score":20,"reasoning":"too small"}
{"ts":"...","event":"outcome","lead":"Company X","result":"replied","days_to_reply":3}
{"ts":"...","event":"summary","period":"weekly","scraped":200,"qualified":30,"replied":5,"conversion":0.17}
```

**Alert monitor:**
```jsonl
{"ts":"...","event":"check","target":"api.example.com","latency_ms":250,"status":200}
{"ts":"...","event":"alert","target":"api.example.com","reason":"latency > 500ms","severity":"warning"}
{"ts":"...","event":"resolved","target":"api.example.com","duration_min":12,"was_real":true}
{"ts":"...","event":"summary","period":"daily","checks":1440,"alerts":3,"false_positives":1}
```

---

## Fase 5: UI

### 5.1 Job Edit Modal

Na seção Self-Healing, quando `type=continuous`:
- Mostrar campo **Business Log Path** (input text)
- Mostrar campo **Max Lines** (number, default 500)
- Mostrar textarea **Context Prompt** (domain-specific instructions)

### 5.2 Suggestion Cards

Nenhuma mudança. As suggestions de business analysis usam o mesmo formato. A diferença está no conteúdo (strategy vs infra), não na estrutura.

---

## Fase 6: Testes

### 6.1 Unit Tests

1. **Config parsing** - `business_log` no YAML parseia corretamente
2. **Log reading** - últimas N linhas, arquivo não existe, arquivo vazio
3. **Prompt building** - modo business vs modo técnico
4. **Integration** - flow completo: config → collect → analyze → suggestions

### 6.2 Test com dados reais

Usar o `polymarket-bot` como cobaia:
1. Adicionar business_log config
2. Apontar para decisions.jsonl existente (ou criar mock)
3. Trigger manual de review
4. Verificar se suggestions fazem sentido

---

## Ordem de Implementação

| Step | O que | Estimativa |
|------|-------|------------|
| 1 | `BusinessLogConfig` em models.py | 5 min |
| 2 | `business_logs` em `AnalysisContext` | 5 min |
| 3 | Coleta de business logs no engine | 10 min |
| 4 | Business analysis prompt | 15 min |
| 5 | Bifurcação do prompt (business vs technical) | 10 min |
| 6 | UI: campos business_log no job edit modal | 15 min |
| 7 | Testes unitários | 20 min |
| 8 | Documentação (README + examples) | 10 min |
| **Total** | | **~90 min** |

---

## Decisões de Design

1. **JSONL, não DB** - Business logs ficam em arquivo, não no SQLite do ProcClaw. O script escreve, ProcClaw só lê. Zero acoplamento.

2. **Prompt bifurcado, não misturado** - Quando tem business_log, o foco é business. Métricas técnicas viram contexto secundário. Evita confundir o AI.

3. **context_prompt é livre** - Cada domínio é diferente. O usuário explica pro AI o que o script faz e no que focar. Sem tentar generalizar demais.

4. **proposed_content funciona igual** - O AI gera o script/config novo completo. Aprovação, apply e rollback são idênticos. Não precisa de novo fluxo.

5. **max_lines = 500 default** - Suficiente pra ~1 mês de um bot que faz 15-20 decisões/dia. Não estoura contexto do AI.
