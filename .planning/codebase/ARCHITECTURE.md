---
last_mapped_commit: d1a9d2cdaa03771ac60583cfd0a95a8769237e3b
last_mapped_at: 2026-11-18
---
<!-- refreshed: 2026-11-18 -->
# Architecture

**Analysis Date:** 2026-11-18

## System Overview

```text
┌──────────────────────────────────────────────────────────────────────┐
│  OPERATOR SURFACE — Telegram (pyTelegramBotAPI, long polling)        │
│  `services/guardian/src/main.py`  ·  `services/guardian/src/chart_generator.py` │
└───────────────┬──────────────────────────────────────────────────────┘
                │ pub/sub: hermes:manual_close, hermes:events
                │ SET/DEL: hermes:kill_switch
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  EVENT BUS — Redis 7 (pub/sub channels + BRPOP list queue + KV keys) │
│  `docker-compose.yml` (service: redis)                               │
└───┬──────────────────────────────┬───────────────────────────┬───────┘
    │ hermes:trade_queue (list)    │ hermes:trade_result:<id>   │ hermes:alerts
    ▼                              ▲                           ▼
┌───────────────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
│  DECISION LAYER           │  │  POLICY LAYER        │  │  PERSISTENCE     │
│  orchestrator             │  │  workers             │  │  SQLite (shared) │
│  `services/orchestrator/  │  │  `services/workers/  │  │  /app/data/      │
│   src/main.py`            │  │   src/main.py`       │  │  trading.db      │
│  strategy_engine.py       │  │  syariah_guard.py    │  │  database.py     │
│  broker_gateway.py        │  │  news_filter.py      │  │                  │
└──────────┬────────────────┘  └──────────────────────┘  └──────────────────┘
           │ REST (urllib) + CCXT
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│  EXTERNAL — Indodax REST ticker/history_v2 + CCXT spot order API     │
│  Optional fallback: Telegram alerts (guardian), CCXT chart data      │
└──────────────────────────────────────────────────────────────────────┘
                              ▲
┌─────────────────────────────┴────────────────────────────────────────┐
│  HOUSEKEEPING — backup_vault (6h loop)                               │
│  `services/backup_vault/backup_daemon.py`  · SQLite hot backup +     │
│  Redis BGSAVE + 7-day rotation                                       │
└──────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| Orchestrator main loop | Poll spot price every 4s, evaluate strategy, manage exits/trailing stops, dispatch proposals | `services/orchestrator/src/main.py` |
| Strategy engine | EMA 5/13 crossover, RSI 14 gate, ATR-based TP/SL, Fibonacci pullback guard | `services/orchestrator/src/strategy_engine.py` |
| Market data feed | Indodax `ticker` mid-price + `history_v2` 1m candles, cached last valid price | `services/orchestrator/src/strategy_engine.py:8-71` |
| Broker gateway | `CCXTSpotBroker` (live spot) / `PaperSpotBroker` (simulator) behind one factory | `services/orchestrator/src/broker_gateway.py` |
| Persistence | SQLite schema init + migrations, order/audit CRUD, daily PnL query | `services/orchestrator/src/database.py` |
| Workers queue processor | `BLPOP` proposals, kill-switch → news blackout → Syariah validation, reply on result channel | `services/workers/src/main.py` |
| Syariah guard | Reject non-SPOT, swap/riba, leverage > 1:1, unlisted pair | `services/workers/src/syariah_guard.py` |
| News blackout filter | Time-window rejection around scheduled events (list is currently empty) | `services/workers/src/news_filter.py` |
| Guardian bot | Telegram control panel — status, PnL recap, positions, logs, chart, backup, kill/resume | `services/guardian/src/main.py` |
| Chart renderer | matplotlib candlestick/line PNG from SQLite orders or CCXT OHLCV fallback | `services/guardian/src/chart_generator.py` |
| Backup daemon | 6-hourly SQLite `Connection.backup` + Redis `BGSAVE` + 7-day retention | `services/backup_vault/backup_daemon.py` |
| Health probes | SQLite probe (orchestrator), Redis ping probe (guardian) | `services/orchestrator/src/healthcheck.py`, `services/guardian/src/healthcheck.py` |

## Pattern Overview

**Overall:** Multi-process event-driven pipeline (orchestrator → queue → worker → callback) with a shared SQLite file and Redis as both message bus and state store.

**Key Characteristics:**
- Four independent Python containers built from one image (`Dockerfile`), differentiated only by `working_dir` and `command` in `docker-compose.yml`.
- No package hierarchy: each service runs flat modules from its own `src/` directory; imports resolve because the working directory is on `sys.path`.
- Coordinator/worker split: orchestrator owns *when* to trade, workers own *whether it is allowed* (Syariah/news/kill-switch).
- All cross-process communication is string/JSON payloads on Redis channels; no HTTP between services.
- State is split: trading records in SQLite (`/app/data/trading.db`), transient/fast state in Redis (`hermes:kill_switch`, `hermes:telemetry`, `hermes:logs`, `hermes:sl_cooldown`, `hermes:sl_hits:<ticket>`).
- Paper/live mode is a runtime branch inside one factory, not a separate deployment (`services/orchestrator/src/broker_gateway.py:163-174`).

## Layers

**Presentation / Control (Guardian):**
- Purpose: Operator UI and manual override surface
- Location: `services/guardian/src/`
- Contains: Telegram handlers, HTML message builders, matplotlib chart rendering, direct SQLite read queries, direct broker balance read
- Depends on: Redis (`hermes:alerts`, `hermes:telemetry`, `hermes:logs`, `hermes:kill_switch`), SQLite `/app/data/trading.db`, `broker_gateway` imported by path injection
- Used by: Human operator only

**Decision (Orchestrator):**
- Purpose: Market observation, signal generation, risk sizing, order execution, position lifecycle
- Location: `services/orchestrator/src/`
- Contains: `main.py` (loop + risk + dispatch), `strategy_engine.py` (indicators), `broker_gateway.py` (exchange adapters), `database.py` (writes)
- Depends on: Indodax REST, CCXT, Redis, SQLite
- Used by: Nothing — it is the top of the automated path

**Policy (Workers):**
- Purpose: Compliance gate before capital is committed
- Location: `services/workers/src/`
- Contains: queue consumer, `SyariahGuard`, `NewsBlackoutFilter`, unused `exchange.py` helper
- Depends on: Redis only for the queue/response channel
- Used by: Orchestrator (synchronously, via 5s pubsub wait)

**Persistence:**
- Purpose: Durable record of orders and audit decisions
- Location: `services/orchestrator/src/database.py` (write-side owner); `services/guardian/src/main.py:112-179`, `services/guardian/src/chart_generator.py:41-58` (read-side consumers)
- Contains: `orders`, `audit_logs` tables, additive `ALTER TABLE` migrations
- Depends on: local filesystem volume `./data:/app/data`

**Infrastructure:**
- Purpose: Message bus and backup
- Location: `docker-compose.yml`, `services/backup_vault/backup_daemon.py`
- Contains: Redis 7 with AOF, healthcheck; 6-hour backup loop
- Depends on: shared volumes `./data`, `./backups`

## Data Flow

### Primary Trade Path (signal → fill → alert)

1. Loop tick fetches mid-price: `MarketDataFeed.get_spot_price(pair)` (`services/orchestrator/src/main.py:353`, implementation `services/orchestrator/src/strategy_engine.py:41-71`).
2. Trailing/exit evaluation runs first each tick: `process_trailing_and_exits` (`services/orchestrator/src/main.py:151-195`) — breakeven lock at +0.65%, trail at 0.18%, SL confirmation requires 2 hits inside 15s (`hermes:sl_hits:<ticket>`).
3. If `len(open_positions) < MAX_CONCURRENT_POSITIONS`, strategy evaluates: `strategy.analyze(pair, current_price)` (`services/orchestrator/src/main.py:359`, implementation `services/orchestrator/src/strategy_engine.py:218-293`).
4. Position size derived from broker cash: `calculate_dynamic_volume` (`services/orchestrator/src/main.py:87-96`).
5. Proposal queued and awaited: `dispatch_trade_proposal` (`services/orchestrator/src/main.py:197-292`) subscribes `hermes:trade_result:<trade_id>`, then `RPUSH hermes:trade_queue`.
6. Worker consumes with `BLPOP hermes:trade_queue` (`services/workers/src/main.py:44`), applies kill-switch (`:54`), news blackout (`:61`), then `SyariahGuard.validate_trade` (`services/workers/src/main.py:70` → `services/workers/src/syariah_guard.py:11-43`).
7. Worker publishes `{"status": "APPROVED"|"REJECTED", ...}` to the response channel (`services/workers/src/main.py:71-77`).
8. Orchestrator receives reply within its 5s window and calls `broker.execute_spot_order(...)` (`services/orchestrator/src/main.py:236-241` → `services/orchestrator/src/broker_gateway.py:65-93`).
9. Order written via `record_order` + `record_audit` (`services/orchestrator/src/main.py:243-257` → `services/orchestrator/src/database.py:71-82`, `:127-135`).
10. `ORDER_EXECUTED` published to `hermes:alerts` (`services/orchestrator/src/main.py:262-275`); guardian renders Telegram message (`services/guardian/src/main.py:550-566`).

### Exit Path

1. Price crosses TP/SL inside `process_trailing_and_exits` (`services/orchestrator/src/main.py:182-195`).
2. `close_single_position` sells via broker and persists (`services/orchestrator/src/main.py:98-126` → `services/orchestrator/src/database.py:104-113`).
3. `POSITION_CLOSED` alert published; `check_risk_circuit_breaker` re-evaluated (`services/orchestrator/src/main.py:120`).
4. Pure ATR stop sets `hermes:sl_cooldown` for 1800s to block re-entry (`services/orchestrator/src/main.py:121-123`).

### Manual Control Path

1. Operator taps Telegram button → callback handler (`services/guardian/src/main.py:415-483`).
2. Close: publishes `hermes:manual_close` with ticket or `ALL` (`services/guardian/src/main.py:427`); orchestrator thread `listen_manual_close_events` consumes (`services/orchestrator/src/main.py:128-149`).
3. Kill: `SET hermes:kill_switch active` + publish `hermes:events` = `KILL_SWITCH_TRIGGERED` (`services/guardian/src/main.py:289-294`); workers log it (`services/workers/src/main.py:29-38`), orchestrator refuses new proposals (`services/orchestrator/src/main.py:198`).
4. Resume: `DEL hermes:kill_switch` + publish `TRADING_RESUMED` (`services/guardian/src/main.py:283-287`).

### Telemetry / Observability Path

1. `update_telemetry` writes a JSON snapshot to `hermes:telemetry` each tick (`services/orchestrator/src/main.py:294-318`).
2. `sys_log` pushes to capped list `hermes:logs` (50 entries, `LTRIM`) and optionally publishes `SYSTEM_ERROR` alerts with a 60s throttle (`services/orchestrator/src/main.py:48-68`).
3. Guardian reads both keys directly for the log/status screens (`services/guardian/src/main.py:181-217`, `:234-268`).

**State Management:**
- Redis = ephemeral operational state + event transport (single shared instance, no cluster).
- SQLite = system of record for orders/audit; opened per call, no connection pool, no WAL pragma.
- In-process memory = strategy sample buffers (`EMAMomentumStrategy.prices/highs/lows`), cached price (`MarketDataFeed._last_valid_price`), module-level `last_known_price`.

## Key Abstractions

**`MarketDataFeed` (classmethods):**
- Purpose: Exchange-agnostic price/candle source with last-known-price caching
- Examples: `services/orchestrator/src/strategy_engine.py:8-71`
- Pattern: Static/class-level singleton holding `_exchange`, `_last_valid_price`, `_last_spread_pct`. Indodax is hardcoded to HTTP endpoints even when `EXCHANGE_ID` differs.

**`EMAMomentumStrategy`:**
- Purpose: Encapsulates indicator math plus the entry gate
- Examples: `services/orchestrator/src/strategy_engine.py:74-293`
- Pattern: Stateful object; `add_price` resamples at 60s buckets, derives synthetic highs/lows from a ±0.02% spread. `analyze` returns `None`, a `BUY` proposal dict, or a `{"action": "SKIPPED"}` dict.

**`CCXTSpotBroker` / `PaperSpotBroker` + `get_broker_client()`:**
- Purpose: Uniform spot-order interface; mode selected by env
- Examples: `services/orchestrator/src/broker_gateway.py`
- Pattern: Adapter + factory. `BrokerExecutionError` is the only domain exception. Indodax path patches market ids from `ticker_id` and rounds limit prices (`:26-37`, `:71-77`).

**Redis channel protocol (implicit interface):**
- Purpose: Cross-service contract
- Channels/keys: `hermes:trade_queue`, `hermes:trade_result:<trade_id>`, `hermes:alerts`, `hermes:events`, `hermes:manual_close`, `hermes:telemetry`, `hermes:logs`, `hermes:kill_switch`, `hermes:sl_cooldown`, `hermes:sl_hits:<ticket>`
- Pattern: Fire-and-forget pub/sub for notifications; list + reply-channel for request/response.

**`SyariahGuard`:**
- Purpose: Sharia compliance policy object
- Examples: `services/workers/src/syariah_guard.py:6-43`
- Pattern: Raises `SyariahComplianceError` on violation; the caller owns the response formatting.

## Entry Points

**Orchestrator:**
- Location: `services/orchestrator/src/main.py` (`if __name__ == "__main__"` at `:396`)
- Triggers: `docker compose` command `python -u src/main.py`
- Responsibilities: Redis connect (exits on failure), `init_db()`, broker/strategy construction, manual-close thread, seed 15 samples from candles, infinite 4s loop

**Workers:**
- Location: `services/workers/src/main.py` (`:79-83`)
- Triggers: container command `python -u src/main.py`
- Responsibilities: Redis connect, daemon thread on `hermes:events`, blocking `BLPOP` validation loop

**Guardian:**
- Location: `services/guardian/src/main.py` (`:594-608`)
- Triggers: container command `python -u src/main.py`
- Responsibilities: Telegram bot setup, alert-listener thread, midnight summary thread, `infinity_polling` in main thread

**Backup Vault:**
- Location: `services/backup_vault/backup_daemon.py` (`:52-58`)
- Triggers: container command `python -u backup_daemon.py`
- Responsibilities: 10s startup delay, immediate backup, then every 21600s

**Healthchecks:**
- Location: `services/orchestrator/src/healthcheck.py`, `services/guardian/src/healthcheck.py`
- Triggers: manual / container orchestrator invocation (not wired into `docker-compose.yml` healthcheck blocks)
- Responsibilities: exit 0/1 based on SQLite table read or Redis ping

## Architectural Constraints

- **Threading:** Each service is a single process. Orchestrator runs 2 threads (main loop + manual-close listener) plus a short-lived pubsub subscriber per trade dispatch. Worker runs 2 threads (event listener + queue consumer). Guardian runs 3 threads (alerts, midnight summary, bot polling with `threaded=True`).
- **Global state:** `r` (Redis client) module-global in all services; `broker`, `strategy`, `last_known_price`, `_last_error_alert_ts`, `_global_broker` in `services/orchestrator/src/main.py:32-72` and `services/guardian/src/main.py:16-19`. `PaperSpotBroker.balance` lives in process memory — the orchestrator and guardian each build their own broker instance, so simulated balances are **not** shared.
- **Synchronous coupling:** `dispatch_trade_proposal` blocks the main loop for up to 5s waiting on the worker reply (`services/orchestrator/src/main.py:225-290`), then `time.sleep(15)` after dispatch (`:379`). Tick cadence is therefore 4s idle / ~19s after a signal.
- **Deployment coupling:** All containers share one image and one root `requirements.txt` (`Dockerfile:10-11`); per-service `requirements.txt` files are not used by the build.
- **Path coupling:** Guardian injects another service's source directory into `sys.path` (`services/guardian/src/main.py:14`) to import `broker_gateway`. Moving orchestrator files breaks guardian at runtime.
- **Filesystem coupling:** `DB_PATH` is env-driven in orchestrator but hardcoded to `/app/data/trading.db` in `services/guardian/src/main.py:54` and `services/guardian/src/chart_generator.py:14`.
- **Host paths:** `BACKUP_DIR = "/app/backups"` hardcoded in guardian and backup daemon; live DB export writes to `/tmp/trading_live_export.db` (`services/guardian/src/main.py:470`).
- **Circular imports:** None. Dependency direction is one-way except guardian → orchestrator via `sys.path`.

## Anti-Patterns

### Business logic duplicated across service boundaries

**What happens:** Guardian recomputes performance stats with raw SQL (`services/guardian/src/main.py:120-131`), opens SQLite directly (`:364-368`), and instantiates its own broker (`:16-19`) instead of consuming orchestrator telemetry.
**Why it's wrong:** Schema or balance-model changes must be applied in two places; paper-mode balances diverge from what the orchestrator reports.
**Do this instead:** Read aggregates from `database.py` helpers (or move them there) and publish balance snapshots into `hermes:telemetry`; keep guardian read-only over Redis + one thin DB accessor module.

### Module-global side effects at import time

**What happens:** Connecting to Redis, `init_db()`, broker construction, and `sys.exit(1)` on Redis failure all run at module import (`services/orchestrator/src/main.py:35-72`, `services/workers/src/main.py:13-27`, `services/guardian/src/main.py:66-88`).
**Why it's wrong:** Any import of the module (including from a test or a healthcheck) triggers live connections and process termination.
**Do this instead:** Move startup into explicit `main()`/`bootstrap()` functions and guard with `if __name__ == "__main__"`.

### Silent exception swallowing

**What happens:** `except Exception: pass` around telemetry, alerts, log writes, and balance reads (`services/orchestrator/src/main.py:67-68`, `:317-318`; `services/guardian/src/main.py:142-143`).
**Why it's wrong:** Broker misconfiguration or Redis outages become invisible; the loop keeps trading on stale values.
**Do this instead:** Log at WARN through `sys_log` with throttling and surface repeated failures as a `SYSTEM_ERROR` alert.

### Mixed-language, hardcoded currency fallbacks

**What happens:** Prices default to `71000000.0` / `2685.0` (`services/orchestrator/src/main.py:32`), ATR defaults to `current_p * 0.0015` (`services/orchestrator/src/strategy_engine.py:162`), chart fallback to `72500000.0` (`services/guardian/src/chart_generator.py:31-38`).
**Why it's wrong:** A populated fallback makes a failed fetch indistinguishable from a real quote, and instruments with different scales silently mis-size.
**Do this instead:** Fail closed — skip the tick and log; keep a single `safety.py` constant module if placeholders are unavoidable.

## Error Handling

**Strategy:** Try/except at loop boundaries with log-and-continue; typed exceptions at domain boundaries.

**Patterns:**
- Outer try/except in the main loop logs and continues (`services/orchestrator/src/main.py:391-392`).
- `BrokerExecutionError` wraps all exchange failures (`services/orchestrator/src/broker_gateway.py:92-93`, `:121-122`) and is caught separately from generic errors at dispatch (`services/orchestrator/src/main.py:276-279`).
- `SyariahComplianceError` drives REJECTED responses (`services/workers/src/main.py:74-77`).
- Fatal startup: Redis unreachable → `sys.exit(1)` in orchestrator, workers, guardian.
- Guardian degrades to a sleep loop when the Telegram token is missing (`services/guardian/src/main.py:66-69`).
- Alert delivery retried 3x with 2s backoff in `safe_send_alert` (`services/guardian/src/main.py:485-491`).

## Cross-Cutting Concerns

**Logging:** Structured-ish text via `print()`; orchestrator additionally writes WIB-timestamped entries to the capped Redis list `hermes:logs` with `[level]` prefix (`services/orchestrator/src/main.py:48-68`). Workers, guardian, and backup use plain `print()`.

**Validation:** Two-stage — strategy filters (RSI 45–68, spread ≤ 0.85%, Fibonacci zone ≤ 76.4%, momentum) in `services/orchestrator/src/strategy_engine.py:235-251`, then policy filters in `services/workers/src/syariah_guard.py` and `services/workers/src/news_filter.py`.

**Authentication:** Telegram operator gate compares `from_user.id`/`chat.id` to `TELEGRAM_CHAT_ID` (`services/guardian/src/main.py:90-95`). Exchange auth is CCXT API key/secret from env; no other auth layer exists between services.

**Risk control:** Daily realized-PnL circuit breaker (`services/orchestrator/src/main.py:74-85`), per-trade capital fraction `MAX_CAPITAL_RISK_PER_TRADE` (`:24`, `:87-96`), max concurrent positions (`:27`, `:358`), post-stop-loss cooldown (`:121-123`), kill-switch flag (`:198`).

---

*Architecture analysis: 2026-11-18*
