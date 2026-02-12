# PLAN: Trade Jobs — ProcClaw Trading Analytics

## Visão

Novo tipo de job `trade` no ProcClaw que transforma o process manager em um **trading dashboard**. Jobs de trade são continuous mas com um contrato de dados: o script emite eventos estruturados (JSON lines) que o ProcClaw captura, parseia, armazena e exibe com analytics completos.

**Princípio central:** ProcClaw não sabe o que é Polymarket, cripto ou ações. Ele entende um **protocolo genérico de eventos de trade**. Qualquer bot que emita esse protocolo ganha analytics de graça.

---

## Arquitetura

```
┌─────────────────┐     stdout (JSON lines)     ┌──────────────────┐
│  Bot Script     │ ─────────────────────────── │  ProcClaw        │
│  (any language) │                              │  Trade Parser    │
│                 │  Emite eventos:               │                  │
│  - scan         │  {"event":"trade_open",...}   │  Parseia → DB    │
│  - decision     │  {"event":"decision",...}     │  Calcula metrics │
│  - trade_open   │  {"event":"portfolio",...}    │  Serve API       │
│  - trade_close  │                              │  Renderiza UI    │
│  - portfolio    │                              │                  │
└─────────────────┘                              └──────────────────┘
```

### Separação de responsabilidades

| Componente | Responsabilidade |
|-----------|-----------------|
| **Bot script** | Emite eventos JSON no stdout. Não sabe que ProcClaw existe. |
| **Trade Event Parser** | Intercepta stdout, detecta linhas JSON com `"event"`, salva no DB |
| **Trade Analytics Engine** | Calcula métricas agregadas a partir dos eventos |
| **Trade API** | Endpoints REST para consultar eventos, métricas, histórico |
| **Trade UI** | Aba "📈 Trading" na Web UI com dashboard visual |

---

## Protocolo de Eventos (Trade Event Protocol v1)

O bot emite linhas JSON no stdout. Linhas que não são JSON são tratadas como log normal.

### Eventos obrigatórios

```jsonc
// 1. SCAN — início de um ciclo de varredura
{
  "event": "scan",
  "ts": "2026-02-12T12:00:00Z",      // ISO-8601
  "markets_scanned": 150,              // quantos mercados avaliou
  "opportunities_found": 3,            // quantos passaram no filtro
  "duration_ms": 2340                  // tempo do scan
}

// 2. DECISION — raciocínio de cada oportunidade avaliada
{
  "event": "decision",
  "ts": "2026-02-12T12:00:01Z",
  "market": "Will Bitcoin hit $100k by March?",
  "market_id": "0x123abc",            // opcional, ID externo
  "action": "buy" | "skip" | "sell",
  "reason": "Volume 120k > threshold 50k, odds 0.15 in range [0.01-0.30], score 85",
  "details": {                         // opcional, dados extras
    "price": 0.15,
    "volume": 120000,
    "score": 85
  }
}

// 3. TRADE_OPEN — posição aberta
{
  "event": "trade_open",
  "ts": "2026-02-12T12:00:02Z",
  "trade_id": "uuid-or-sequential",   // ID único do trade
  "market": "Will Bitcoin hit $100k by March?",
  "market_id": "0x123abc",
  "side": "Yes" | "No" | "Long" | "Short" | "Both",
  "entry_price": 0.15,
  "size": 150.00,                      // valor em $ investido
  "quantity": 1000,                    // opcional, quantidade de contratos/shares
  "target_price": 0.30,               // opcional, preço alvo
  "stop_loss": 0.075,                 // opcional
  "strategy": "underdog_hunter",      // nome da estratégia
  "notes": "Dumbguy88 pattern"        // opcional
}

// 4. TRADE_CLOSE — posição fechada
{
  "event": "trade_close",
  "ts": "2026-02-12T15:30:00Z",
  "trade_id": "uuid-or-sequential",
  "market": "Will Bitcoin hit $100k by March?",
  "exit_price": 0.30,
  "pnl": 150.00,                      // lucro/prejuízo em $
  "pnl_pct": 100.0,                   // lucro/prejuízo em %
  "reason": "target_hit_2x" | "stop_loss" | "time_limit" | "market_resolved" | "manual",
  "hold_duration_hours": 3.5           // tempo que ficou na posição
}

// 5. PORTFOLIO — snapshot periódico do estado
{
  "event": "portfolio",
  "ts": "2026-02-12T12:00:03Z",
  "initial_capital": 1000.00,
  "current_capital": 1150.00,
  "cash": 700.00,                      // capital disponível
  "invested": 450.00,                  // capital em posições abertas
  "unrealized_pnl": 23.50,            // PnL de posições abertas
  "realized_pnl": 150.00,             // PnL acumulado de trades fechados
  "open_positions": 3,
  "total_trades": 15,                  // trades fechados total
  "win_count": 10,
  "loss_count": 5
}
```

### Regras do protocolo

1. Cada linha é um JSON independente (JSON Lines / NDJSON)
2. Campo `event` é obrigatório — determina o tipo
3. Campo `ts` é obrigatório — ISO-8601 timestamp
4. Linhas sem `"event"` são tratadas como log normal (ignoradas pelo parser de trades)
5. Eventos desconhecidos são armazenados como `custom` (extensibilidade)
6. O bot DEVE emitir `portfolio` pelo menos 1x por ciclo de scan
7. `decision` com `action: "skip"` é opcional mas recomendado (pra entender o raciocínio)

---

## Database Schema

### Novas tabelas

```sql
-- Eventos brutos (append-only, fonte de verdade)
CREATE TABLE trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    run_id INTEGER,                    -- FK job_runs.id (ciclo atual)
    event_type TEXT NOT NULL,           -- scan, decision, trade_open, trade_close, portfolio
    ts TEXT NOT NULL,                   -- timestamp do evento
    data TEXT NOT NULL,                 -- JSON completo do evento
    ingested_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES job_runs(id)
);

CREATE INDEX idx_trade_events_job ON trade_events(job_id, event_type, ts);
CREATE INDEX idx_trade_events_run ON trade_events(run_id, event_type);

-- Trades (posições abertas/fechadas, derivado de trade_open + trade_close)
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    trade_id TEXT NOT NULL,            -- ID do bot
    market TEXT NOT NULL,
    market_id TEXT,
    side TEXT,
    entry_price REAL,
    exit_price REAL,
    size REAL,
    quantity REAL,
    target_price REAL,
    stop_loss REAL,
    pnl REAL,
    pnl_pct REAL,
    status TEXT DEFAULT 'open',        -- open, closed
    reason TEXT,                        -- motivo do close
    strategy TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    hold_duration_hours REAL,
    open_event_id INTEGER,             -- FK trade_events.id
    close_event_id INTEGER,            -- FK trade_events.id
    UNIQUE(job_id, trade_id)
);

CREATE INDEX idx_trades_job_status ON trades(job_id, status);

-- Snapshots de portfolio (série temporal)
CREATE TABLE portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    initial_capital REAL,
    current_capital REAL,
    cash REAL,
    invested REAL,
    unrealized_pnl REAL,
    realized_pnl REAL,
    open_positions INTEGER,
    total_trades INTEGER,
    win_count INTEGER,
    loss_count INTEGER,
    event_id INTEGER,                  -- FK trade_events.id
    FOREIGN KEY (event_id) REFERENCES trade_events(id)
);

CREATE INDEX idx_portfolio_job_ts ON portfolio_snapshots(job_id, ts);

-- Métricas agregadas (calculadas periodicamente)
CREATE TABLE trade_job_stats (
    job_id TEXT PRIMARY KEY,
    initial_capital REAL,
    current_capital REAL,
    total_pnl REAL,
    total_pnl_pct REAL,
    win_rate REAL,
    total_trades INTEGER,
    open_trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    avg_win REAL,
    avg_loss REAL,
    best_trade_pnl REAL,
    best_trade_market TEXT,
    worst_trade_pnl REAL,
    worst_trade_market TEXT,
    avg_hold_hours REAL,
    max_drawdown_pct REAL,
    profit_factor REAL,                -- gross_wins / gross_losses
    sharpe_ratio REAL,                 -- retorno / volatilidade (simplified)
    scans_total INTEGER,
    decisions_total INTEGER,
    opportunities_found INTEGER,
    last_scan_at TEXT,
    last_trade_at TEXT,
    updated_at TEXT
);

-- Decisions log (pra análise de raciocínio)
CREATE TABLE trade_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    market TEXT NOT NULL,
    market_id TEXT,
    action TEXT NOT NULL,               -- buy, skip, sell
    reason TEXT,
    details TEXT,                        -- JSON
    event_id INTEGER,
    FOREIGN KEY (event_id) REFERENCES trade_events(id)
);

CREATE INDEX idx_decisions_job_ts ON trade_decisions(job_id, ts);
CREATE INDEX idx_decisions_action ON trade_decisions(job_id, action);
```

---

## Métricas Calculadas (Trade Analytics Engine)

### Por Job (trade_job_stats)

| Métrica | Cálculo |
|---------|---------|
| **PnL ($)** | Soma de todos trade_close.pnl |
| **PnL (%)** | (current_capital - initial_capital) / initial_capital * 100 |
| **Win Rate** | wins / total_trades * 100 |
| **Avg Win** | Média de pnl onde pnl > 0 |
| **Avg Loss** | Média de pnl onde pnl < 0 |
| **Best Trade** | Max pnl + nome do mercado |
| **Worst Trade** | Min pnl + nome do mercado |
| **Profit Factor** | Total gains / Total losses |
| **Avg Hold Time** | Média de hold_duration_hours |
| **Max Drawdown** | Maior queda peak-to-trough da equity curve |
| **Sharpe Ratio** | Avg return per trade / StdDev return per trade |
| **Trades/Day** | total_trades / dias desde primeiro trade |
| **Scans/Day** | scans_total / dias |
| **Hit Rate** | opportunities_found / markets_scanned (do scan events) |
| **Skip Rate** | decisions skip / decisions total |

### Por Run (ciclo de scan)

| Métrica | Cálculo |
|---------|---------|
| Markets scanned | Do evento scan |
| Opportunities found | Do evento scan |
| Decisions made | Count de decision events no run |
| Trades opened | Count de trade_open no run |
| Trades closed | Count de trade_close no run |
| PnL do ciclo | Soma de pnl dos trades fechados no run |
| Duration | Tempo entre primeiro e último evento do run |

---

## API Endpoints

```
# Trade jobs overview
GET /api/v1/trading/overview
→ Lista todos os jobs type=trade com stats resumidos

# Stats de um job
GET /api/v1/trading/{job_id}/stats
→ trade_job_stats completo

# Trades de um job
GET /api/v1/trading/{job_id}/trades?status=open|closed|all&limit=50&offset=0
→ Lista de trades com filtros

# Decisions de um job
GET /api/v1/trading/{job_id}/decisions?action=buy|skip|sell&limit=100&offset=0
→ Log de decisões com filtros

# Portfolio history (equity curve)
GET /api/v1/trading/{job_id}/equity?from=ISO&to=ISO&granularity=1h|1d
→ Série temporal de portfolio snapshots

# Events brutos
GET /api/v1/trading/{job_id}/events?type=scan|decision|trade_open|trade_close|portfolio&limit=100
→ Eventos brutos com filtros

# Comparação entre jobs
GET /api/v1/trading/compare?jobs=pm-arbitrage,pm-underdog,pm-bonds
→ Stats side-by-side de múltiplos jobs
```

---

## Web UI — Aba "📈 Trading"

### Layout principal

```
┌──────────────────────────────────────────────────────────────────┐
│  📈 Trading                                                      │
│                                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │ Total PnL │ │ Win Rate │ │ Open     │ │ Best     │           │
│  │ +$423.50  │ │ 67.3%    │ │ 12 pos   │ │ +$89.20  │           │
│  │ +8.47%    │ │ 34W/17L  │ │ 8 strats │ │ underdog │           │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │
│                                                                  │
│  ┌─ Strategy Cards ─────────────────────────────────────────────┐│
│  │                                                               ││
│  │  ⚡ Arbitrage    🎯 Underdog     🔄 Contrarian    💎 Bonds  ││
│  │  +$42 (+4.2%)   +$180 (+18%)   -$12 (-1.2%)    +$85 (+8.5%)││
│  │  WR: 89%        WR: 72%        WR: 45%          WR: 90%    ││
│  │  ██████████░░    ████████░░░░   ████░░░░░░░░     █████████░ ││
│  │  8s scan         2h scan        1h scan           6h scan   ││
│  │  12 trades       8 trades       5 trades          3 trades  ││
│  │                                                               ││
│  │  🔬 Domain      🎰 Concentrated 🏀 Sports        🔁 Grinder ││
│  │  +$56 (+5.6%)   +$90 (+9%)     -$18 (-1.8%)    +$0.50     ││
│  │  WR: 96%        WR: 100%       WR: 33%          WR: 50.4% ││
│  │  ...             ...            ...               ...       ││
│  └───────────────────────────────────────────────────────────────┘│
│                                                                  │
│  [Click card → detail view]                                      │
│                                                                  │
│  ┌─ Detail View (when card clicked) ────────────────────────────┐│
│  │                                                               ││
│  │  Sub-tabs: [Overview] [Trades] [Decisions] [Equity] [Events] ││
│  │                                                               ││
│  │  Overview: KPIs + equity curve chart                          ││
│  │  Trades: tabela de trades abertos/fechados                    ││
│  │  Decisions: log de decisões com filtro buy/skip/sell          ││
│  │  Equity: gráfico da equity curve ao longo do tempo            ││
│  │  Events: eventos brutos (debug)                               ││
│  └───────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘
```

### Strategy Card (componente)

Cada card mostra:
- Emoji + nome da estratégia
- PnL em $ e %
- Win rate com barra visual
- Número de trades (abertos / total)
- Status (🟢 running / 🔴 stopped)
- Intervalo de scan
- Último scan (relative time)
- Mini sparkline da equity (últimas 24h)

### Detail View — sub-tabs

**Overview:**
- KPI cards: PnL, Win Rate, Avg Win, Avg Loss, Profit Factor, Sharpe, Max Drawdown
- Equity curve (canvas chart, portfolio snapshots ao longo do tempo)
- Trades por dia (bar chart)

**Trades:**
- Tabela com: Market, Side, Entry, Exit, PnL, PnL%, Hold Time, Reason, Status
- Filtro: Open / Closed / All
- Sort por qualquer coluna
- Color-coded: verde pra win, vermelho pra loss

**Decisions:**
- Timeline de decisões
- Filtro: Buy / Skip / Sell
- Cada decisão mostra: timestamp, mercado, ação, razão completa
- Ratio buy/skip como indicador de seletividade

**Equity:**
- Gráfico grande da equity curve
- Drawdown overlay
- Benchmark line (capital inicial)
- Zoom: 1h, 6h, 24h, 7d, 30d, all

**Events:**
- Eventos brutos em JSON
- Filtro por tipo
- Útil pra debug

---

## Fases de Implementação

### Fase 1 — Protocolo + Storage (MVP)
**Esforço: ~3-4h**

1. **DB Migration** — Criar tabelas (trade_events, trades, portfolio_snapshots, trade_job_stats, trade_decisions)
2. **Trade Event Parser** — Módulo `core/trade_parser.py` que:
   - Recebe linha de stdout
   - Tenta parsear como JSON
   - Se tem campo `event`, classifica e salva no DB
   - Se é `trade_open`, cria registro em `trades`
   - Se é `trade_close`, atualiza registro em `trades`
   - Se é `portfolio`, salva snapshot
   - Se é `decision`, salva em trade_decisions
3. **Integrar no job_wrapper.py** — Quando job.type == "trade", passar stdout pelo parser antes de logar
4. **Config do job** — Novo campo no YAML:
   ```yaml
   type: trade
   trade_config:
     initial_capital: 1000
     currency: USD
   ```
5. **Atualizar bots** — Modificar `run_strategy.py` e `BaseStrategy` pra emitir eventos no protocolo

**Entregável:** Bots emitem eventos → ProcClaw captura e salva no SQLite.

### Fase 2 — Analytics Engine + API
**Esforço: ~3-4h**

1. **Trade Analytics Engine** — Módulo `core/trade_analytics.py`:
   - Calcula trade_job_stats a partir dos eventos
   - Recalcula após cada trade_close e portfolio event
   - Calcula drawdown, sharpe, profit factor
2. **API Endpoints** — Adicionar em `api/server.py`:
   - `/api/v1/trading/overview`
   - `/api/v1/trading/{job_id}/stats`
   - `/api/v1/trading/{job_id}/trades`
   - `/api/v1/trading/{job_id}/decisions`
   - `/api/v1/trading/{job_id}/equity`
   - `/api/v1/trading/{job_id}/events`
   - `/api/v1/trading/compare`
3. **Stats Aggregation** — Background task que recalcula stats periodicamente (ou event-driven)

**Entregável:** API funcional, pode consultar via curl/browser.

### Fase 3 — Web UI
**Esforço: ~4-6h**

1. **Nova aba "📈 Trading"** no nav
2. **Summary cards** no topo (total PnL, win rate, positions, best trade)
3. **Strategy cards** — grid de cards com KPIs de cada job trade
4. **Detail view** — click no card abre detalhe com sub-tabs
5. **Equity curve chart** — Canvas-based chart (sem lib externa, ou usar Chart.js CDN)
6. **Decisions timeline** — visualização do log de decisões
7. **Auto-refresh** — polling a cada 10-30s pra atualizar dados (ajustável)

**Entregável:** Dashboard visual completo na Web UI.

### Fase 4 — Polish + Alertas (opcional)
**Esforço: ~2-3h**

1. **Alertas de trade** — WhatsApp notification quando:
   - Trade aberto com size > threshold
   - Trade fechado com PnL significativo
   - Win rate caiu abaixo de threshold
   - Drawdown excedeu limite
2. **Export** — CSV/JSON dos trades e decisions
3. **Comparação visual** — side-by-side de estratégias com gráficos sobrepostos
4. **Performance optimization** — índices, pagination, data retention policy

---

## Migração dos Jobs Existentes

Os 8 jobs `pm-*` precisam mudar:
1. `type: continuous` → `type: trade`
2. Adicionar `trade_config.initial_capital: 1000`
3. Modificar `BaseStrategy.run_cycle()` pra emitir eventos JSON no stdout

### Mudanças no bot (BaseStrategy)

```python
import json
from datetime import datetime, timezone

def emit_event(self, event_type: str, data: dict):
    """Emit a trade protocol event to stdout"""
    event = {
        "event": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        **data
    }
    # Print as single JSON line — ProcClaw intercepts this
    print(json.dumps(event), flush=True)

def run_cycle(self):
    # ... existing logic ...
    
    # Emit scan event
    self.emit_event("scan", {
        "markets_scanned": len(all_markets),
        "opportunities_found": len(opportunities),
        "duration_ms": scan_duration_ms
    })
    
    # For each market evaluated
    for market in evaluated:
        self.emit_event("decision", {
            "market": market.question,
            "market_id": market.id,
            "action": "buy" if market in opportunities else "skip",
            "reason": market.skip_reason or market.buy_reason,
            "details": {"price": market.price, "volume": market.volume}
        })
    
    # For each new trade
    for trade in new_trades:
        self.emit_event("trade_open", {
            "trade_id": trade["id"],
            "market": trade["market"],
            "side": trade["outcome"],
            "entry_price": trade["entry_price"],
            "size": trade["size"],
            "strategy": self.get_strategy_id()
        })
    
    # For each closed trade
    for trade, reason in closed_trades:
        self.emit_event("trade_close", {
            "trade_id": trade["id"],
            "market": trade["market"],
            "exit_price": trade["exit_price"],
            "pnl": trade["pnl"],
            "pnl_pct": trade["pnl_pct"],
            "reason": reason
        })
    
    # Portfolio snapshot
    self.emit_event("portfolio", {
        "initial_capital": self.portfolio.initial_capital,
        "current_capital": self.portfolio.current_value(),
        "cash": self.portfolio.cash,
        "invested": self.portfolio.invested_value(),
        "unrealized_pnl": self.portfolio.unrealized_pnl(),
        "realized_pnl": self.portfolio.realized_pnl,
        "open_positions": len(self.portfolio.positions),
        "total_trades": self.portfolio.total_closed,
        "win_count": self.portfolio.wins,
        "loss_count": self.portfolio.losses
    })
```

---

## Decisões de Design

### Por que protocolo stdout e não API/webhook?
- **Simplicidade**: bot não precisa saber de ProcClaw, HTTP, auth
- **Linguagem-agnostica**: qualquer linguagem que faz `print(json)` funciona
- **Zero config no bot**: não precisa URL, porta, token
- **Já temos**: ProcClaw já captura stdout — só precisa parsear

### Por que tabelas separadas e não só trade_events?
- **Performance**: queries em trades/portfolio são frequentes
- **Indexação**: campos tipados vs JSON genérico
- **Joins**: relacionar trade com decisões que levaram a ele
- **Backup**: trade_events é a fonte de verdade, as outras são derivadas

### Por que recalcular stats e não calcular on-the-fly?
- **Performance**: dashboard carrega stats pre-calculados
- **Consistência**: mesmo resultado em múltiplos clientes
- **Histórico**: pode comparar stats em diferentes pontos no tempo

### Compatibilidade com self_healing
- Jobs `trade` herdam de `continuous` — restart automático se crashar
- Self-healing pode analisar trade metrics (win rate caindo = problema?)
- Decision log é input perfeito pra healing engine avaliar qualidade

---

## Riscos e Mitigações

| Risco | Probabilidade | Impacto | Mitigação |
|-------|--------------|---------|-----------|
| Volume de eventos (arbitrage 8s = ~10k events/day) | Alta | Médio | Data retention policy, cleanup após 30d |
| Parser overhead no hot path | Baixa | Alto | JSON parse é rápido, async se necessário |
| DB lock contention com múltiplos writers | Média | Médio | WAL mode, batch inserts |
| Bots antigos quebram com novo type | Baixa | Baixo | `trade` herda behavior de `continuous` |
| Chart.js bundle size | Baixa | Baixo | CDN, lazy load na aba trading |

---

## Acceptance Criteria

- [ ] Job type `trade` funciona como `continuous` + trade parser
- [ ] 5 tipos de evento são capturados e armazenados
- [ ] Métricas são calculadas automaticamente após cada ciclo
- [ ] API retorna stats, trades, decisions, equity data
- [ ] Web UI tem aba Trading com cards de estratégia
- [ ] Click no card mostra detalhe com sub-tabs
- [ ] Equity curve renderizada como chart
- [ ] Decisions log visível e filtrável
- [ ] 8 bots Polymarket migrados para type=trade e emitindo eventos
- [ ] Alertas de trade significativo via WhatsApp (fase 4)

---

## Estimativa Total

| Fase | Esforço | Prioridade |
|------|---------|-----------|
| Fase 1 — Protocolo + Storage | 3-4h | P0 |
| Fase 2 — Analytics + API | 3-4h | P0 |
| Fase 3 — Web UI | 4-6h | P1 |
| Fase 4 — Polish + Alertas | 2-3h | P2 |
| **Total** | **12-17h** | |

---

*Criado: 2026-02-12*
*Status: PLANNING*
