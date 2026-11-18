---
last_mapped_commit: d1a9d2cdaa03771ac60583cfd0a95a8769237e3b
last_mapped_at: 2026-11-18
---
# External Integrations

**Analysis Date:** 2026-11-18

## APIs & External Services

**Crypto/Commodity Exchange:**
- Indodax (default exchange, `EXCHANGE_ID=indodax`) - spot market data and live order execution
  - Public ticker REST: `https://indodax.com/api/ticker/{base}_{quote}` and `https://indodax.com/api/ticker/{base}{quote}` — consumed with `urllib.request` in `services/orchestrator/src/strategy_engine.py:50`
  - Chart history REST: `https://indodax.com/tradingview/history_v2?symbol={base}{quote}&tf=1&from=...&to=...` — 1-minute closes in `services/orchestrator/src/strategy_engine.py:30`
  - Private trading API via CCXT `ccxt.indodax` — `fetch_balance`, `load_markets`, `create_order` in `services/orchestrator/src/broker_gateway.py:49-122`
  - Auth: `EXCHANGE_API_KEY` / `EXCHANGE_SECRET_KEY` (env). Legacy Kraken fallbacks `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` are read at `services/orchestrator/src/broker_gateway.py:166-167`.
  - Indodax specifics: market metadata patched from `info.ticker_id` (`_ensure_indodax_markets`, `broker_gateway.py:26-37`); IDR orders are sent as `limit` orders with ±0.1%/±0.2% price offsets (`broker_gateway.py:73,107`); non-IDR exchanges use `market` orders.
  - Rate limiting: `enableRateLimit: True` on every CCXT client.

- CCXT multi-exchange adapter - ANY exchange id resolvable as `ccxt.<id>` can be selected via `EXCHANGE_ID` (`services/orchestrator/src/broker_gateway.py:13`, `services/orchestrator/src/strategy_engine.py:17`, `services/guardian/src/chart_generator.py:22`). Code paths beyond Indodax are generic, not tuned.
  - Secondary client in `services/workers/src/exchange.py:4-13` (`get_exchange_client`) - defined but not imported by any worker module.

**Messaging / Operator Interface:**
- Telegram Bot API via `pyTelegramBotAPI` (`telebot`) - primary human control plane
  - Client: `telebot.TeleBot(BOT_TOKEN, threaded=True)` at `services/guardian/src/main.py:75`; `apihelper.CONNECT_TIMEOUT=15`, `READ_TIMEOUT=25`, `RETRY_ON_ERROR=True` (`main.py:71-73`)
  - Auth: `TELEGRAM_BOT_TOKEN`, admin gating by `TELEGRAM_CHAT_ID` comparison in `is_admin()` (`main.py:90-95`)
  - Inbound: long polling `bot.infinity_polling(timeout=10, long_polling_timeout=5)` with reconnect loop (`main.py:603-608`)
  - Commands: `/start`, `/menu`, `/status`, `/summary`, `/positions`, `/logs`, `/chart`, `/backup`, `/kill`, `/resume` (`main.py:296-413`)
  - Outbound: text alerts (`main.py:485-591`), inline keyboards with `close_{ticket}` callbacks (`main.py:424-428`), chart photos (`main.py:270-279`), DB document uploads (`main.py:459-483`)

**Market Calendar / News:**
- Not integrated. `NewsBlackoutFilter` in `services/workers/src/news_filter.py` holds an empty `scheduled_events` list and has no fetch client; `set_blackout_events()` is called by nothing. The comment at `news_filter.py:8` marks it as pending a calendar API sync.

## Data Storage

**Databases:**
- SQLite - order ledger and audit trail
  - Connection: `DB_PATH` env var, default `/app/data/trading.db` (`services/orchestrator/src/database.py:5`)
  - Client: stdlib `sqlite3`, one connection per call via `get_connection()` (`database.py:7-10`)
  - Tables: `orders` (`database.py:16-37`), `audit_logs` (`database.py:39-48`)
  - Migrations: runtime `ALTER TABLE ... ADD COLUMN` for missing columns (`database.py:51-68`)
  - Additional direct SQLite readers outside the DB module: `services/guardian/src/main.py:117,364,472`, `services/guardian/src/chart_generator.py:45`, `services/backup_vault/backup_daemon.py:23`
  - Host path: `./data` bind mount (`docker-compose.yml:29,43,57,72`)

**File Storage:**
- Local filesystem only. SQLite snapshots written to `/app/backups` (`BACKUP_DIR`, `services/backup_vault/backup_daemon.py:9`) on a 6-hour loop (`backup_daemon.py:53-58`), retention 7 days (`backup_daemon.py:45-50`). Host path `./backups` bind mount (`docker-compose.yml:58,73`).

**Caching:**
- Redis 7-alpine (`docker-compose.yml:2-15`), `--appendonly yes`, named volume `redis_data`, healthcheck `redis-cli ping`.
- Auth: `REDIS_PASSWORD` (default empty string — no password unless the operator sets both `REDIS_PASSWORD` and Redis `requirepass`).
- Services use `decode_responses=True` (`services/orchestrator/src/main.py:36-41`, `services/workers/src/main.py:14-19`, `services/guardian/src/main.py:78-83`).

**Redis keys / channels in use:**

| Key / channel | Type | Writer | Reader |
|---|---|---|---|
| `hermes:trade_queue` | list (RPUSH/BLPOP) | `services/orchestrator/src/main.py:222` | `services/workers/src/main.py:44` |
| `hermes:trade_result:{trade_id}` | pub/sub | `services/workers/src/main.py:56,64,72,75` | `services/orchestrator/src/main.py:215` |
| `hermes:alerts` | pub/sub | `services/orchestrator/src/main.py:61,83,119,170,275,284,366` | `services/guardian/src/main.py:495` |
| `hermes:events` | pub/sub | `services/guardian/src/main.py:285,291` | `services/workers/src/main.py:31` |
| `hermes:manual_close` | pub/sub | `services/guardian/src/main.py:427` | `services/orchestrator/src/main.py:131` |
| `hermes:telemetry` | string (JSON) | `services/orchestrator/src/main.py:316` | `services/guardian/src/main.py:183,239` |
| `hermes:logs` | list (LPUSH/LTRIM 50) | `services/orchestrator/src/main.py:54-55` | `services/guardian/src/main.py:203` |
| `hermes:kill_switch` | string | `services/orchestrator/src/main.py:78`, `services/guardian/src/main.py:290` | orchestrator/workers/guardian |
| `hermes:sl_cooldown` | string with TTL 1800s | `services/orchestrator/src/main.py:122` | `services/orchestrator/src/main.py:201` |
| `hermes:sl_hits:{ticket}` | string with TTL 15s | `services/orchestrator/src/main.py:191` | `services/orchestrator/src/main.py:190` |

**Backups:**
- `backup_vault` copies SQLite via `sqlite3.Connection.backup(pages=1)` and triggers Redis `BGSAVE` (`services/backup_vault/backup_daemon.py:15-41`).

## Authentication & Identity

**Auth Provider:**
- Custom / none for app users. No user accounts, sessions, or OAuth.
- Telegram bot admin is identified by numeric chat id equality: `str(obj.from_user.id) == str(ADMIN_CHAT_ID)` (`services/guardian/src/main.py:90-95`).
- Exchange access is API-key based through CCXT; live mode is only entered when `TRADING_MODE=LIVE` AND both key/secret are set AND the key does not contain `your_` or `ISI_` placeholders (`services/orchestrator/src/broker_gateway.py:169-174`). Otherwise `PaperSpotBroker` is used.

## Monitoring & Observability

**Error Tracking:**
- None (no Sentry/Rollbar/Datadog). Errors go to stdout and, when `notify_telegram=True`, to the `hermes:alerts` channel consumed by the guardian (`services/orchestrator/src/main.py:48-68`).

**Logs:**
- `print()` to container stdout with `flush=True` (`services/orchestrator/src/main.py:52`).
- Redis ring buffer: last 50 entries of `hermes:logs`, surfaced as a 12-entry `<pre>` block in Telegram (`services/guardian/src/main.py:203-217`).
- Healthcheck scripts exist but are NOT wired into `docker-compose.yml`:
  - `services/orchestrator/src/healthcheck.py` - verifies `DB_PATH` exists and `SELECT 1 FROM orders LIMIT 1`
  - `services/guardian/src/healthcheck.py` - Redis `PING` with 3s socket timeout
- Log timestamps are rendered in WIB (UTC+7) (`services/orchestrator/src/main.py:50`), while the EOD summary scheduler fires at 00:00 UTC (`services/guardian/src/main.py:583-592`).

## CI/CD & Deployment

**Hosting:**
- Self-hosted Docker host. `docker-compose.yml` defines `restart: always` for every service.
- Git remote: `https://github.com/tropicans/hermestrading.git`.

**CI Pipeline:**
- None. No `.github/`, no `.gitlab-ci.yml`, no `.circleci/`.

## Environment Configuration

**Required env vars:**
- `TELEGRAM_BOT_TOKEN` - bot will idle forever without it (`services/guardian/src/main.py:66-69`)
- `TELEGRAM_CHAT_ID` - admin gate for every handler
- `REDIS_HOST`, `REDIS_PORT` - default `redis:6379` (Compose network name)
- `REDIS_PASSWORD` - optional; empty by default
- `EXCHANGE_ID` - default `indodax`
- `EXCHANGE_API_KEY`, `EXCHANGE_SECRET_KEY` - required only for `TRADING_MODE=LIVE`
- `TRADING_SYMBOL` - default `PAXG/IDR`
- `TRADING_MODE` - `PAPER` (default) or `LIVE`
- `DB_PATH` - default `/app/data/trading.db`
- Risk/syariah tuning: `MAX_CAPITAL_RISK_PER_TRADE`, `DAILY_MAX_DRAWDOWN`, `MAX_CONCURRENT_POSITIONS`, `SHARIAH_STRICT_MODE`, `MAX_LEVERAGE_RATIO`, `PAPER_INITIAL_BALANCE`, `NEWS_BLACKOUT_MINUTES_BEFORE`, `NEWS_BLACKOUT_MINUTES_AFTER`

**Secrets location:**
- `.env` at repo root, gitignored (`.gitignore:2`), injected via `env_file` in `docker-compose.yml:26-27,40-41,54-55,69-70`.
- `.env.production` exists at repo root - contains environment configuration (contents not inspected).
- No secret manager, no Docker secrets, no vault.

## Webhooks & Callbacks

**Incoming:**
- None. No HTTP server is started by any service; all inbound control is Telegram long polling.

**Outgoing:**
- None (no registered webhooks to third parties).

## Cross-Service Coupling

- `services/guardian/src/main.py:14-19` mutates `sys.path` with the hard-coded absolute container path `/app/services/orchestrator/src` and imports `broker_gateway.get_broker_client`, so the guardian image must contain the orchestrator source. Chart generator similarly depends on CCXT and the shared SQLite file.

---

*Integration audit: 2026-11-18*
