---
last_mapped_commit: d1a9d2cdaa03771ac60583cfd0a95a8769237e3b
last_mapped_at: 2026-11-18
---
# Codebase Structure

**Analysis Date:** 2026-11-18

## Directory Layout

```
hermestrading/
├── docker-compose.yml          # Redis + 4 Python services, shared ./data and ./backups volumes
├── Dockerfile                  # Single python:3.10-slim image used by every service
├── requirements.txt            # Root dependency set actually installed into the image
├── .env.production             # Production env template (values not inspected)
├── .gitignore                  # Ignores .env, *.db, data/, backups/, __pycache__
├── .planning/                  # GSD planning artifacts (codebase docs live here)
├── .opencode/                  # GSD command/skill/plugin tooling (not application code)
├── .agents/                    # Agent tooling config (not application code)
└── services/
    ├── orchestrator/
    │   ├── requirements.txt    # Not used by Docker build
    │   └── src/
    │       ├── main.py         # Event loop, risk gates, dispatch, trailing stops
    │       ├── strategy_engine.py  # MarketDataFeed + EMAMomentumStrategy
    │       ├── broker_gateway.py   # CCXTSpotBroker / PaperSpotBroker / factory
    │       ├── database.py         # SQLite schema + order/audit persistence
    │       └── healthcheck.py      # SQLite readiness probe
    ├── workers/
    │   ├── requirements.txt
    │   └── src/
    │       ├── main.py         # BLPOP queue consumer, publishes results
    │       ├── syariah_guard.py    # Sharia compliance policy
    │       ├── news_filter.py      # Time-window blackout filter
    │       └── exchange.py         # Unused CCXT client helper
    ├── guardian/
    │   ├── requirements.txt
    │   └── src/
    │       ├── main.py         # Telegram control panel + alert renderer
    │       ├── chart_generator.py  # matplotlib PNG buffer builder
    │       └── healthcheck.py      # Redis ping probe
    └── backup_vault/
        └── backup_daemon.py    # 6h SQLite hot backup + Redis BGSAVE + rotation
```

## Directory Purposes

**`services/`:**
- Purpose: Holds every runtime component; one directory per container
- Contains: Per-service `src/` directories plus (mostly unused) per-service `requirements.txt`
- Key files: `services/orchestrator/src/main.py`, `services/workers/src/main.py`, `services/guardian/src/main.py`, `services/backup_vault/backup_daemon.py`

**`services/orchestrator/`:**
- Purpose: Decision engine and the only writer of trade records
- Contains: Loop driver, strategy math, broker adapters, SQLite layer, healthcheck
- Key files: `services/orchestrator/src/main.py`, `services/orchestrator/src/strategy_engine.py`, `services/orchestrator/src/broker_gateway.py`, `services/orchestrator/src/database.py`

**`services/workers/`:**
- Purpose: Pre-trade compliance gate
- Contains: Queue consumer, Syariah policy, news blackout policy
- Key files: `services/workers/src/main.py`, `services/workers/src/syariah_guard.py`

**`services/guardian/`:**
- Purpose: Operator-facing Telegram bot and reporting
- Contains: Handler/message code, chart rendering, healthcheck
- Key files: `services/guardian/src/main.py`, `services/guardian/src/chart_generator.py`

**`services/backup_vault/`:**
- Purpose: Data durability
- Contains: Standalone daemon script (no `src/` subdirectory — the only service structured this way)
- Key files: `services/backup_vault/backup_daemon.py`

**`.opencode/`, `.agents/`:**
- Purpose: GSD agent framework (commands, skills, hooks, plugins)
- Contains: Markdown/JS tooling; contains no trading logic
- Key files: none relevant to the application

**Runtime directories (gitignored, created at run time):**
- `data/` → mounted to `/app/data`, holds `trading.db`
- `backups/` → mounted to `/app/backups`, holds `trading_backup_YYYYMMDD_HHMMSS.db`

## Key File Locations

**Entry Points:**
- `services/orchestrator/src/main.py`: `start_automated_loop()` at `:320`, launched at `:397`
- `services/workers/src/main.py`: `evaluate_and_respond()` at `:40`, launched at `:83`
- `services/guardian/src/main.py`: `bot.infinity_polling()` loop at `:603-608`
- `services/backup_vault/backup_daemon.py`: backup loop at `:52-58`

**Configuration:**
- `docker-compose.yml`: service definitions, `working_dir`, commands, volumes, Redis healthcheck
- `Dockerfile`: base image, system deps (`build-essential`, `curl`), dependency install
- `requirements.txt`: single dependency list for all containers (`ccxt`, `redis`, `pandas`, `matplotlib`, `pyTelegramBotAPI`, `numpy`, `websockets`, `requests`, `python-dotenv`)
- `.env.production`: production variable template — loaded by compose via `env_file: .env` (note: compose references `.env`, not `.env.production`)
- Env reads are inline `os.getenv(...)` calls, not a central settings module

**Core Logic:**
- `services/orchestrator/src/strategy_engine.py`: `MarketDataFeed` (`:8`), `EMAMomentumStrategy` (`:74`), entry gate (`:218-293`)
- `services/orchestrator/src/main.py`: risk sizing (`:87`), trailing/exits (`:151`), dispatch (`:197`), telemetry (`:294`)
- `services/orchestrator/src/broker_gateway.py`: adapters (`:9`, `:125`), factory (`:163`)
- `services/orchestrator/src/database.py`: schema + migrations (`:12-69`), CRUD (`:71-135`)
- `services/workers/src/syariah_guard.py`: `validate_trade` (`:11-43`)
- `services/guardian/src/chart_generator.py`: `generate_paxg_chart` (`:41-108`)

**Testing:**
- Not detected — no test files, no `pytest.ini`, no test directory, no CI config

**Healthchecks:**
- `services/orchestrator/src/healthcheck.py` (SQLite probe, exit 0/1)
- `services/guardian/src/healthcheck.py` (Redis ping, exit 0/1)
- Not wired into any `healthcheck:` block in `docker-compose.yml`; only Redis declares one

## Naming Conventions

**Files:**
- `snake_case.py` for all modules: `strategy_engine.py`, `broker_gateway.py`, `syariah_guard.py`, `news_filter.py`, `chart_generator.py`, `backup_daemon.py`
- Service directories are lowercase single words (`orchestrator`, `workers`, `guardian`, `backup_vault`); only non-`src` service is `backup_vault`
- No `__init__.py` anywhere — modules are imported as top-level names, relying on the process working directory being `services/<name>` (see `docker-compose.yml:21,35,49,64`)

**Modules / Classes:**
- Classes `PascalCase`: `MarketDataFeed`, `EMAMomentumStrategy`, `CCXTSpotBroker`, `PaperSpotBroker`, `SyariahGuard`, `NewsBlackoutFilter`, `BrokerExecutionError`, `SyariahComplianceError`
- Functions and methods `snake_case`, verb-first: `get_broker_client`, `calculate_dynamic_volume`, `dispatch_trade_proposal`, `process_trailing_and_exits`, `validate_trade`, `generate_paxg_chart`
- Private helpers prefixed with `_`: `_ensure_indodax_markets`, `_split_pair`, `_fetch_live_candles_fallback`, `_get_public_exchange`

**Variables / Constants:**
- Module constants `UPPER_SNAKE`: `TRADING_SYMBOL`, `MAX_CONCURRENT_POSITIONS`, `BREAKEVEN_THRESHOLD_PCT`, `TRAILING_DISTANCE_PCT`, `DB_PATH`, `BACKUP_DIR`
- Emoji constants use the `EMOJI_*` prefix, all defined at the top of `services/guardian/src/main.py:21-43`
- Locals `snake_case`; a few short loop names (`r`, `c`, `p`, `sl`, `tp`, `b`) are used freely
- Environment variables `UPPER_SNAKE` and read inline: `TRADING_MODE`, `EXCHANGE_ID`, `EXCHANGE_API_KEY`, `EXCHANGE_SECRET_KEY`, `TRADING_SYMBOL`, `DAILY_MAX_DRAWDOWN`, `MAX_CAPITAL_RISK_PER_TRADE`, `MAX_CONCURRENT_POSITIONS`, `SHARIAH_STRICT_MODE`, `MAX_LEVERAGE_RATIO`, `NEWS_BLACKOUT_MINUTES_BEFORE/AFTER`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `DB_PATH`, `PAPER_INITIAL_BALANCE`

**Redis namespace:**
- All keys/channels colon-prefixed with `hermes:` — `hermes:trade_queue`, `hermes:trade_result:<trade_id>`, `hermes:alerts`, `hermes:events`, `hermes:manual_close`, `hermes:telemetry`, `hermes:logs`, `hermes:kill_switch`, `hermes:sl_cooldown`, `hermes:sl_hits:<ticket>`. Follow this prefix for any new key.

**Language:**
- Identifiers, log tags (`[orchestrator]`, `[guardian]`, `[workers]`, `[backup]`, `[database]`, `[broker_gateway]`, `[strategy_engine]`), and Telegram labels are English; comments, docstrings, and user-facing strings are Indonesian. Match this split when adding code in existing files.

## Where to Add New Code

**New Trading Signal / Indicator:**
- Indicator math: `services/orchestrator/src/strategy_engine.py` (add a method on `EMAMomentumStrategy`, keep the `calculate_*` naming)
- Gate logic and reason strings: inside `EMAMomentumStrategy.analyze` (`services/orchestrator/src/strategy_engine.py:218-293`) so `SKIPPED` reasons stay in one place

**New Pre-Trade Policy (compliance, filters):**
- New policy module: `services/workers/src/<policy_name>.py` exporting one class with a `validate_*`/`is_*` method raising a dedicated `*Error`
- Wire it into the ordered checks in `services/workers/src/main.py:53-77` (kill-switch → news → Syariah). Publish `{"status": "REJECTED", "reason": ...}` on `hermes:trade_result:<trade_id>`

**New Exchange / Broker Behavior:**
- Add or adjust adapters in `services/orchestrator/src/broker_gateway.py`; extend `get_broker_client()` (`:163-174`) rather than branching in `main.py`
- Keep the dict contract `{ticket, pair, action, volume, fill_price, total_cost, remaining_balance}` for `execute_spot_order` and `{ticket, pnl, remaining_balance}` for `close_spot_position`

**New Persistence Field or Query:**
- Add the column to the `CREATE TABLE` in `services/orchestrator/src/database.py:16-37` **and** to `required_cols` at `:54-61` so the additive migration applies to existing DBs
- Add a helper function in the same file; do not open `sqlite3.connect` from other services

**New Operator Command (Telegram):**
- Handler + message builder: `services/guardian/src/main.py` (register with `@bot.message_handler`; gate every handler with `is_admin`)
- Read state from Redis first; only query SQLite if no Redis projection exists
- Add matching `Telegram`-published event name under the `hermes:` namespace and handle it in the owning service

**New Background Job:**
- Put it in the service that owns the data; run as a `threading.Thread(daemon=True)` started in that service's `__main__` block (pattern: `services/guardian/src/main.py:594-600`)
- If it must survive independently, create `services/<job_name>/` with a top-level script like `services/backup_vault/backup_daemon.py` and a compose entry using `working_dir: /app/services/<job_name>`

**New Utility Helpers:**
- No shared library exists. Duplicate small helpers rather than adding a shared package — cross-service imports currently require a `sys.path` hack (`services/guardian/src/main.py:14`) and should not be expanded

**New Service Wiring:**
- Add the container to `docker-compose.yml` following the existing shape: `build: .`, `container_name: hermes_<name>`, `working_dir: /app/services/<name>`, `command: ["python", "-u", "<entry>.py"]`, `env_file: .env`, and only the volumes that service needs
- If it needs a new dependency, add it to the root `requirements.txt` (the per-service files are not installed by `Dockerfile`)

## Special Directories

**`.planning/`:**
- Purpose: GSD state and generated codebase documentation
- Generated: Partly (codebase docs are regenerated by mapping)
- Committed: Yes

**`.opencode/`:**
- Purpose: GSD commands, skills, hooks, plugins, core workflows
- Generated: No
- Committed: Yes (vendored agent tooling)

**`.agents/`:**
- Purpose: Agent-side configuration/tooling
- Generated: No
- Committed: Yes

**`data/` / `backups/`:**
- Purpose: SQLite database and hot-backup snapshots at runtime
- Generated: Yes
- Committed: No — both listed in `.gitignore`

**`__pycache__/`:**
- Purpose: Python bytecode
- Generated: Yes
- Committed: No

---

*Structure analysis: 2026-11-18*
