# PLAN: Self-Healing v2 - Job Behavior Reviewer

> **Status:** Planning  
> **Created:** 2026-02-11  
> **Author:** Marcos + Claude  

---

## 1. Visão Geral

### 1.1 Objetivo
Evoluir o Self-Healing de um sistema reativo (corrige falhas) para um **Revisor de Comportamento Proativo** que:
- Analisa periodicamente se jobs estão funcionando da melhor forma
- Sugere otimizações de eficácia, performance e custo
- Detecta quando condições externas mudaram (páginas, APIs, regras)
- Aplica melhorias automaticamente OU aguarda aprovação humana

### 1.2 Escopo
- **Qualquer job** (AI ou não) pode ter Self-Healing v2 habilitado
- **Insumos para análise:**
  - Logs de execução
  - Sessões AI (para jobs OpenClaw)
  - Histórico de runs
  - Workflows e dependências
  - Violações de SLA
  - Código fonte (scripts)
  - Prompts (arquivos .md)
  - Configurações do job

### 1.3 Entregáveis
1. Modelo de dados atualizado (SQLite)
2. Configuração de job expandida
3. Engine de análise periódica
4. Nova aba "Self-Healing" na Web UI
5. Sistema de aprovação de sugestões
6. Logging de execuções de melhorias

---

## 2. Arquitetura

### 2.1 Componentes

```
┌─────────────────────────────────────────────────────────────────┐
│                        ProcClaw Daemon                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────┐    ┌─────────────────┐                    │
│  │   Scheduler     │───▶│ Healing Analyzer │                    │
│  │  (periodic)     │    │    Engine        │                    │
│  └─────────────────┘    └────────┬─────────┘                    │
│                                  │                              │
│                                  ▼                              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                   Insumos Collector                      │   │
│  │  ┌─────┐ ┌──────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌──────────┐  │   │
│  │  │Logs │ │Runs  │ │SLA  │ │AI   │ │Work │ │Scripts/  │  │   │
│  │  │     │ │History│ │Viols│ │Sess │ │flows│ │Prompts   │  │   │
│  │  └─────┘ └──────┘ └─────┘ └─────┘ └─────┘ └──────────┘  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                  │                              │
│                                  ▼                              │
│  ┌─────────────────┐    ┌─────────────────┐                    │
│  │  AI Analyzer    │───▶│  Suggestion     │                    │
│  │  (OpenClaw)     │    │  Generator      │                    │
│  └─────────────────┘    └────────┬────────┘                    │
│                                  │                              │
│                    ┌─────────────┴─────────────┐               │
│                    ▼                           ▼               │
│  ┌─────────────────────┐     ┌─────────────────────┐          │
│  │  Auto-Apply Mode    │     │  Approval Queue     │          │
│  │  (execute changes)  │     │  (wait for human)   │          │
│  └─────────────────────┘     └─────────────────────┘          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                        SQLite Database                          │
├─────────────────────────────────────────────────────────────────┤
│  healing_reviews    │  healing_suggestions  │  healing_actions  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Fluxo de Dados

```
1. Scheduler dispara análise (periodicidade configurada)
        │
        ▼
2. Collector reúne insumos do job
   - Últimos N runs
   - Logs recentes
   - Sessões AI (se aplicável)
   - SLA score e violações
   - Código/prompt atual
        │
        ▼
3. AI Analyzer processa insumos
   - Detecta problemas/ineficiências
   - Gera sugestões de melhoria
   - Estima impacto (performance, custo, eficácia)
        │
        ▼
4. Decisão baseada na config do job:
   ├─▶ auto_apply=true  → Executa mudanças automaticamente
   │                      → Registra em healing_actions
   │
   └─▶ auto_apply=false → Cria sugestão pendente
                          → Aguarda aprovação na UI
                          → Notifica usuário (opcional)
        │
        ▼
5. Usuário revisa na aba Self-Healing
   - Visualiza sugestão completa
   - Aprova → executa mudanças
   - Rejeita → arquiva sugestão
```

---

## 3. Modelo de Dados (SQLite)

### 3.1 Nova Tabela: `healing_reviews`
```sql
CREATE TABLE healing_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    status TEXT NOT NULL,  -- 'running', 'completed', 'failed'
    
    -- Insumos coletados
    runs_analyzed INTEGER DEFAULT 0,
    logs_lines INTEGER DEFAULT 0,
    ai_sessions_count INTEGER DEFAULT 0,
    sla_violations_count INTEGER DEFAULT 0,
    
    -- Resultado
    suggestions_count INTEGER DEFAULT 0,
    auto_applied_count INTEGER DEFAULT 0,
    error_message TEXT,
    
    -- Metadata
    analysis_duration_ms INTEGER,
    ai_tokens_used INTEGER,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_healing_reviews_job ON healing_reviews(job_id);
CREATE INDEX idx_healing_reviews_started ON healing_reviews(started_at DESC);
```

### 3.2 Nova Tabela: `healing_suggestions`
```sql
CREATE TABLE healing_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    
    -- Classificação
    category TEXT NOT NULL,  -- 'performance', 'cost', 'reliability', 'security', 'config', 'prompt', 'script'
    severity TEXT NOT NULL,  -- 'low', 'medium', 'high', 'critical'
    
    -- Conteúdo
    title TEXT NOT NULL,
    description TEXT NOT NULL,  -- Explicação detalhada (pode ser longa)
    current_state TEXT,         -- Estado atual (código/config)
    suggested_change TEXT,      -- Mudança sugerida
    expected_impact TEXT,       -- Impacto esperado
    
    -- Arquivos afetados
    affected_files TEXT,  -- JSON array de paths
    
    -- Status
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'approved', 'rejected', 'applied', 'failed'
    
    -- Aprovação
    reviewed_at TIMESTAMP,
    reviewed_by TEXT,  -- 'auto' ou 'human'
    rejection_reason TEXT,
    
    -- Execução
    applied_at TIMESTAMP,
    action_id INTEGER,  -- FK para healing_actions
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (review_id) REFERENCES healing_reviews(id),
    FOREIGN KEY (action_id) REFERENCES healing_actions(id)
);

CREATE INDEX idx_healing_suggestions_job ON healing_suggestions(job_id);
CREATE INDEX idx_healing_suggestions_status ON healing_suggestions(status);
CREATE INDEX idx_healing_suggestions_created ON healing_suggestions(created_at DESC);
```

### 3.3 Nova Tabela: `healing_actions`
```sql
CREATE TABLE healing_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion_id INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    
    -- Tipo de ação
    action_type TEXT NOT NULL,  -- 'edit_script', 'edit_prompt', 'edit_config', 'run_command', 'restart_job'
    
    -- Detalhes
    file_path TEXT,
    original_content TEXT,   -- Backup do conteúdo original
    new_content TEXT,        -- Novo conteúdo aplicado
    command_executed TEXT,   -- Se foi comando
    
    -- Resultado
    status TEXT NOT NULL,  -- 'success', 'failed', 'rolled_back'
    error_message TEXT,
    
    -- Rollback
    can_rollback BOOLEAN DEFAULT TRUE,
    rolled_back_at TIMESTAMP,
    
    -- Metadata
    execution_duration_ms INTEGER,
    ai_session_key TEXT,  -- Se usou AI para executar
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (suggestion_id) REFERENCES healing_suggestions(id)
);

CREATE INDEX idx_healing_actions_job ON healing_actions(job_id);
CREATE INDEX idx_healing_actions_suggestion ON healing_actions(suggestion_id);
```

---

## 4. Configuração do Job

### 4.1 Estrutura Atual (manter compatibilidade)
```yaml
self_healing:
  enabled: true
  analysis:
    include_logs: true
    log_lines: 200
    include_stderr: true
    include_history: 5
    include_config: true
  remediation:
    enabled: true
    max_attempts: 3
    allowed_actions:
      - restart_job
      - edit_script
      - edit_config
    forbidden_paths: []
    require_approval: false
  notify:
    on_analysis: false
    on_fix_attempt: true
    on_success: true
    on_give_up: true
    session: main
```

### 4.2 Novos Campos (v2)
```yaml
self_healing:
  enabled: true
  
  # === NOVO: Modo de Operação ===
  mode: proactive  # 'reactive' (só em falha) | 'proactive' (análise periódica)
  
  # === NOVO: Periodicidade de Análise ===
  review_schedule:
    frequency: daily  # 'hourly', 'daily', 'weekly', 'on_failure', 'on_sla_breach', 'manual'
    time: "03:00"     # Horário preferencial (para daily/weekly)
    day: 1            # Dia da semana para weekly (0=domingo)
    min_runs: 5       # Mínimo de runs desde última análise para disparar
  
  # === NOVO: Escopo da Análise ===
  review_scope:
    analyze_logs: true
    analyze_runs: true
    analyze_ai_sessions: true  # Se job tipo OpenClaw
    analyze_sla: true
    analyze_workflows: true
    analyze_script: true       # Analisar código do script
    analyze_prompt: true       # Analisar prompt (jobs OpenClaw)
    analyze_config: true       # Analisar configuração do job
  
  # === NOVO: Comportamento de Sugestões ===
  suggestions:
    auto_apply: false          # false = aguarda aprovação, true = aplica automaticamente
    auto_apply_categories:     # Se auto_apply=false, pode auto-aplicar certas categorias
      - config                 # Ex: só auto-aplica mudanças de config
    min_severity_for_approval: medium  # 'low' auto-aplica, 'medium'+ precisa aprovação
    notify_on_suggestion: true
    notify_channel: whatsapp
  
  # === Campos existentes (mantidos) ===
  analysis:
    include_logs: true
    log_lines: 500  # Aumentado para análise mais completa
    include_stderr: true
    include_history: 10  # Mais histórico para padrões
    include_config: true
  
  remediation:
    enabled: true
    max_attempts: 3
    allowed_actions:
      - restart_job
      - edit_script
      - edit_prompt   # NOVO
      - edit_config
      - run_command
    forbidden_paths: []
    require_approval: false  # Para ações reativas (falhas)
  
  notify:
    on_analysis: true  # NOVO: notifica quando análise é feita
    on_suggestion: true  # NOVO: notifica quando há sugestão
    on_fix_attempt: true
    on_success: true
    on_give_up: true
    session: main
```

### 4.3 Modelo Pydantic Atualizado
```python
class ReviewFrequency(str, Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    ON_FAILURE = "on_failure"
    ON_SLA_BREACH = "on_sla_breach"
    MANUAL = "manual"

class HealingMode(str, Enum):
    REACTIVE = "reactive"      # Só quando job falha
    PROACTIVE = "proactive"    # Análise periódica

class ReviewScheduleConfig(BaseModel):
    frequency: ReviewFrequency = ReviewFrequency.DAILY
    time: str = "03:00"        # HH:MM
    day: int = 1               # 0-6 para weekly
    min_runs: int = 5          # Mínimo de runs para disparar análise

class ReviewScopeConfig(BaseModel):
    analyze_logs: bool = True
    analyze_runs: bool = True
    analyze_ai_sessions: bool = True
    analyze_sla: bool = True
    analyze_workflows: bool = True
    analyze_script: bool = True
    analyze_prompt: bool = True
    analyze_config: bool = True

class SuggestionBehaviorConfig(BaseModel):
    auto_apply: bool = False
    auto_apply_categories: list[str] = []
    min_severity_for_approval: str = "medium"
    notify_on_suggestion: bool = True
    notify_channel: str = "whatsapp"

class SelfHealingConfig(BaseModel):
    enabled: bool = False
    mode: HealingMode = HealingMode.REACTIVE
    review_schedule: ReviewScheduleConfig = Field(default_factory=ReviewScheduleConfig)
    review_scope: ReviewScopeConfig = Field(default_factory=ReviewScopeConfig)
    suggestions: SuggestionBehaviorConfig = Field(default_factory=SuggestionBehaviorConfig)
    analysis: HealingAnalysisConfig = Field(default_factory=HealingAnalysisConfig)
    remediation: HealingRemediationConfig = Field(default_factory=HealingRemediationConfig)
    notify: HealingNotifyConfig = Field(default_factory=HealingNotifyConfig)
```

---

## 5. Web UI - Nova Aba "Self-Healing"

### 5.1 Layout da Aba

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Jobs  │  Runs  │  Dependencies  │  Workflows  │  Self-Healing  │  ... │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  📊 Overview                                                     │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │   │
│  │  │ Pending  │  │ Approved │  │ Applied  │  │ Rejected │        │   │
│  │  │    12    │  │     3    │  │    47    │  │     8    │        │   │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘        │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  🔧 Pending Suggestions                              [Refresh]   │   │
│  ├─────────────────────────────────────────────────────────────────┤   │
│  │  Filter: [All Jobs ▼] [All Categories ▼] [All Severities ▼]    │   │
│  ├─────────────────────────────────────────────────────────────────┤   │
│  │                                                                  │   │
│  │  ┌─ 🔴 HIGH ──────────────────────────────────────────────────┐ │   │
│  │  │  oc-idea-hunter                          2026-02-11 08:30  │ │   │
│  │  │  "Prompt usando API deprecated do Twitter"                 │ │   │
│  │  │  Category: prompt  │  [View] [✓ Approve] [✗ Reject]       │ │   │
│  │  └────────────────────────────────────────────────────────────┘ │   │
│  │                                                                  │   │
│  │  ┌─ 🟡 MEDIUM ────────────────────────────────────────────────┐ │   │
│  │  │  backup-procclaw                         2026-02-11 05:00  │ │   │
│  │  │  "Schedule pode ser otimizado para menor custo"            │ │   │
│  │  │  Category: config  │  [View] [✓ Approve] [✗ Reject]       │ │   │
│  │  └────────────────────────────────────────────────────────────┘ │   │
│  │                                                                  │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  📜 Recent Actions                                   [View All]  │   │
│  ├─────────────────────────────────────────────────────────────────┤   │
│  │  ✅ oc-stock-hunter  │  edit_prompt  │  success  │  10min ago   │   │
│  │  ✅ backup-openclaw  │  edit_config  │  success  │  2h ago      │   │
│  │  ❌ email-watcher    │  edit_script  │  failed   │  1d ago      │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Modal de Sugestão

```
┌─────────────────────────────────────────────────────────────────────────┐
│  🔧 Suggestion Details                                        [✕]      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Job: oc-idea-hunter                                                    │
│  Category: prompt  │  Severity: 🔴 HIGH  │  Created: 2026-02-11 08:30  │
│                                                                         │
│  ───────────────────────────────────────────────────────────────────── │
│                                                                         │
│  📋 Title                                                               │
│  Prompt usando API deprecated do Twitter                                │
│                                                                         │
│  📝 Description                                                         │
│  O prompt atual referencia endpoints da API v1.1 do Twitter que foram  │
│  descontinuados. A análise dos últimos 10 runs mostra 60% de falhas    │
│  relacionadas a "endpoint not found". Recomenda-se atualizar para a    │
│  API v2 ou usar alternativas como Nitter.                              │
│                                                                         │
│  📄 Current State                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  ... buscar tweets via api.twitter.com/1.1/search/tweets ...    │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ✨ Suggested Change                                                    │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  ... buscar tweets via api.twitter.com/2/tweets/search/recent   │   │
│  │  ... ou usar nitter.net como fallback ...                       │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  📈 Expected Impact                                                     │
│  - Success rate: 40% → 95% (estimado)                                  │
│  - Custo: sem alteração                                                │
│  - Performance: +20% (menos retries)                                   │
│                                                                         │
│  📁 Affected Files                                                      │
│  - ~/.procclaw/prompts/idea-hunter.md                                  │
│                                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  [Cancel]                              [✗ Reject]  [✓ Approve & Apply] │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.3 Modal de Ação Executada

```
┌─────────────────────────────────────────────────────────────────────────┐
│  📜 Action Details                                            [✕]      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Job: oc-stock-hunter                                                   │
│  Action: edit_prompt  │  Status: ✅ Success  │  2026-02-11 08:45       │
│                                                                         │
│  ───────────────────────────────────────────────────────────────────── │
│                                                                         │
│  📁 File Modified                                                       │
│  ~/.procclaw/prompts/stock-hunter.md                                   │
│                                                                         │
│  📝 Changes (Diff)                                                      │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  - Buscar ações com volume 2x acima da média                    │   │
│  │  + Buscar ações com volume 1.5x acima da média (ajustado para   │   │
│  │  +   reduzir falsos positivos baseado nos últimos 30 dias)      │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ⏱️ Execution                                                           │
│  - Duration: 1.2s                                                       │
│  - AI Session: healing-stock-hunter-20260211-084523                    │
│  - Tokens used: 1,247                                                  │
│                                                                         │
│  ↩️ Rollback Available                                                  │
│  Original content backed up. Click to restore.                         │
│                                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  [Close]                                              [↩️ Rollback]     │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 6. API Endpoints

### 6.1 Novos Endpoints

```
# Reviews
GET    /api/v1/healing/reviews                    # Lista reviews
GET    /api/v1/healing/reviews/{id}               # Detalhes de um review
POST   /api/v1/healing/reviews/{job_id}/trigger   # Dispara análise manual

# Suggestions
GET    /api/v1/healing/suggestions                # Lista sugestões (filtros: status, job_id, category, severity)
GET    /api/v1/healing/suggestions/{id}           # Detalhes de uma sugestão
POST   /api/v1/healing/suggestions/{id}/approve   # Aprova e executa
POST   /api/v1/healing/suggestions/{id}/reject    # Rejeita com motivo
GET    /api/v1/healing/suggestions/pending/count  # Contagem de pendentes

# Actions
GET    /api/v1/healing/actions                    # Lista ações executadas
GET    /api/v1/healing/actions/{id}               # Detalhes de uma ação
POST   /api/v1/healing/actions/{id}/rollback      # Desfaz uma ação

# Stats
GET    /api/v1/healing/stats                      # Estatísticas gerais
GET    /api/v1/healing/stats/{job_id}             # Estatísticas de um job
```

---

## 7. Fases de Implementação

### Fase 1: Fundação (3-4 dias)
- [ ] Criar novas tabelas no SQLite (migração v8)
- [ ] Atualizar modelos Pydantic
- [ ] Atualizar parser de jobs.yaml
- [ ] Criar endpoints básicos de API (CRUD)
- [ ] Testes unitários

### Fase 2: Engine de Análise (4-5 dias)
- [ ] Implementar HealingReviewScheduler
- [ ] Implementar InsumoCollector
- [ ] Implementar AIAnalyzer (integração OpenClaw)
- [ ] Implementar SuggestionGenerator
- [ ] Sistema de filas para análises
- [ ] Testes de integração

### Fase 3: Execução e Rollback (3-4 dias)
- [ ] Implementar ActionExecutor
- [ ] Backup automático antes de mudanças
- [ ] Sistema de rollback
- [ ] Logging detalhado
- [ ] Notificações

### Fase 4: Web UI (4-5 dias)
- [ ] Nova aba Self-Healing
- [ ] Cards de estatísticas
- [ ] Lista de sugestões pendentes
- [ ] Modal de detalhes de sugestão
- [ ] Botões de aprovação/rejeição
- [ ] Lista de ações executadas
- [ ] Modal de detalhes de ação
- [ ] Botão de rollback

### Fase 5: Configuração na UI (2-3 dias)
- [ ] Atualizar modal de edição de job
- [ ] Seção expandida de Self-Healing
- [ ] Campos de periodicidade
- [ ] Campos de escopo
- [ ] Campos de comportamento de sugestões
- [ ] Preview de próxima análise

### Fase 6: Testes e Refinamentos (2-3 dias)
- [ ] Testes end-to-end
- [ ] Testes com jobs reais
- [ ] Ajustes de prompts do analyzer
- [ ] Documentação
- [ ] Release notes

**Total estimado: 18-24 dias**

---

## 8. Considerações de Segurança

### 8.1 Paths Proibidos (mantido)
```python
HEALING_FORBIDDEN_PATHS_ALWAYS = [
    "~/.openclaw/workspace/projects/procclaw/",
    "**/projects/procclaw/**",
    "**/node_modules/openclaw/**",
    "~/.ssh/",
    "~/.gnupg/",
    "~/.openclaw/openclaw.json",
    "/etc/", "/usr/", "/bin/", "/sbin/",
]
```

### 8.2 Novas Restrições
- Scripts de sistema (backup-*.sh) só com aprovação explícita
- Prompts críticos podem ter flag `protected: true`
- Rate limit de ações por dia (configurável)
- Rollback obrigatório para ações de alto impacto

### 8.3 Auditoria
- Todas as ações logadas com timestamp, usuário, motivo
- Backup completo antes de qualquer mudança
- Histórico de rollbacks preservado

---

## 9. Integração com OpenClaw

### 9.1 Prompt do Analyzer
O analyzer usará OpenClaw para analisar os insumos. Prompt base:

```markdown
# Job Behavior Analyzer

Você é um especialista em análise de jobs automatizados. Analise os seguintes dados
e identifique oportunidades de melhoria.

## Job: {job_id}
- Type: {job_type}
- Schedule: {schedule}
- Last {n} runs analyzed

## Insumos
### Runs History
{runs_summary}

### Recent Logs
{logs_excerpt}

### SLA Status
{sla_summary}

### Current Script/Prompt
{code_content}

## Análise Solicitada
1. Identificar problemas de eficácia (job não faz o que deveria)
2. Identificar problemas de performance (demora mais que necessário)
3. Identificar oportunidades de redução de custo
4. Detectar mudanças externas que afetam o job
5. Sugerir melhorias concretas e acionáveis

## Output Format
Retorne um JSON com array de sugestões:
{output_schema}
```

---

## 10. Métricas de Sucesso

- **Adoption:** % de jobs com Self-Healing v2 habilitado
- **Suggestion Quality:** % de sugestões aprovadas vs rejeitadas
- **Impact:** Melhoria média de SLA após aplicar sugestões
- **Time Saved:** Tempo economizado em debugging manual
- **Cost Reduction:** Redução de custo em jobs OpenClaw otimizados

---

## Changelog

| Data | Versão | Descrição |
|------|--------|-----------|
| 2026-02-11 | 0.1 | Documento inicial |
