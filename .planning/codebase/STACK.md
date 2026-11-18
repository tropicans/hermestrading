---
last_mapped_commit: d1a9d2cdaa03771ac60583cfd0a95a8769237e3b
last_mapped_at: 2026-11-18
---
# Technology Stack

**Analysis Date:** 2026-11-18

## Languages

**Primary:**
- Python 3.10 - All runtime services (`services/orchestrator/src/*.py`, `services/workers/src/*.py`, `services/guardian/src/*.py`, `services/backup_vault/backup_daemon.py`). Python 3.10 is pinned in `Dockerfile` (`FROM python:3.10-slim`). Type hints use builtin generics (`list[dict]`, `tuple[bool, str]`), which require 3.9+, consistent with 3.10.

**Secondary:**
- SQL - Schema DDL and queries in `services/orchestrator/src/database.py` (SQLite dialect with `INSERT OR REPLACE`, `DATE('now')`, `PRAGMA table_info`).
- YAML - Container orchestration in `docker-compose.yml`.
- Dockerfile - Image build in `Dockerfile`.

## Runtime

**Environment:**
- Python 3.10 runtime in Debian slim image (`Dockerfile`).
- Four long-running Python processes, each with its own working directory:
  - `orchestrator` → `python -u src/main.py` (cwd `/app/services/orchestrator`)
  - `workers` → `python -u src/main.py` (cwd `/app/services/workers`)
  - `guardian` → `python -u src/main.py` (cwd `/app/services/guardian`)
  - `backup_vault` → `python -u backup_daemon.py` (cwd `/app/services/backup_vault`)
- Concurrency model: threads inside each process (`threading.Thread(..., daemon=True)` in `services/orchestrator/src/main.py:325`, `services/workers/src/main.py:80`, `services/guardian/src/main.py:595`). No async/await, no multiprocessing.

**Package Manager:**
- pip, driven by `requirements.txt` at repo root plus one per service.
- Lockfile: **missing**. No `pip freeze` output, no `poetry.lock`, no `Pipfile.lock`, no `uv.lock`.
- Root `requirements.txt` is **unpinned**; service requirement files pin some versions.

## Frameworks

**Core:**
- No web framework. Services communicate over Redis pub/sub + Redis lists, not HTTP.
- `redis` 5.0.3 (`services/orchestrator/requirements.txt`, `services/workers/requirements.txt`, `services/guardian/requirements.txt`) - event bus, kill-switch state, telemetry cache, log ring buffer.
- `ccxt` (unpinned, root `requirements.txt` + `services/workers/requirements.txt`) - exchange connectivity and market data. Used in `services/orchestrator/src/broker_gateway.py`, `services/orchestrator/src/strategy_engine.py`, `services/workers/src/exchange.py`, `services/guardian/src/chart_generator.py`.
- `pyTelegramBotAPI` 4.16.1 - Telegram bot control plane in `services/guardian/src/main.py`.
- `pandas` 2.2.2 - chart data frames in `services/guardian/src/chart_generator.py`.
- `matplotlib` 3.8.4 (headless `Agg` backend) - chart PNG rendering in `services/guardian/src/chart_generator.py`.
- `requests` 2.31.0 - declared dependency; no direct import found in source (market data uses `urllib.request`).
- `python-dotenv` 1.0.1 - declared dependency; no `load_dotenv()` call found. Containers get env vars via `env_file: .env` in `docker-compose.yml`.
- `websockets` and `numpy` - declared in root `requirements.txt` only; not imported anywhere in `services/`.

**Testing:**
- Not detected. No test framework, no test files, no pytest/tox/unittest config.

**Build/Dev:**
- Docker / Docker Compose v3-style schema (`docker-compose.yml`, no `version:` key).
- Base image `python:3.10-slim`; `build-essential` and `curl` installed for native builds (`Dockerfile:5-8`).
- No linter/formatter config (no `ruff.toml`, `setup.cfg`, `.flake8`, `pyproject.toml`, `.pre-commit-config.yaml`).

## Key Dependencies

**Critical:**
- `ccxt` - every market-data read and every live order goes through CCXT. Default exchange id `indodax` (`EXCHANGE_ID` env var, default `"indodax"`). `services/orchestrator/src/broker_gateway.py:13` does `getattr(ccxt, exchange_id)`; `services/orchestrator/src/strategy_engine.py:17` falls back to `ccxt.indodax`.
- `redis` - hard startup dependency. Orchestrator, workers, and guardian `sys.exit(1)` if `r.ping()` fails (`services/orchestrator/src/main.py:35-46`, `services/workers/src/main.py:13-24`, `services/guardian/src/main.py:77-88`).
- `pyTelegramBotAPI` - the only operator interface; if `TELEGRAM_BOT_TOKEN` is unset the guardian sleeps forever (`services/guardian/src/main.py:66-69`).
- `matplotlib` + `pandas` - chart images sent to Telegram (`services/guardian/src/main.py:270-279`).

**Infrastructure:**
- `sqlite3` (stdlib) - local persistence in `services/orchestrator/src/database.py` and direct reads from guardian (`services/guardian/src/main.py:117`, `services/guardian/src/chart_generator.py:45`).
- `urllib.request` (stdlib) - direct Indodax REST calls in `services/orchestrator/src/strategy_engine.py:4,30,50`.
- `threading`, `uuid`, `json`, `datetime`, `shutil`, `io` (stdlib).

## Configuration

**Environment:**
- Runtime config is entirely environment-variable based; no config file is parsed by the app.
- `docker-compose.yml` injects `env_file: .env` into all four Python services. `.env` is gitignored (`.gitignore:2`).
- `.env.production` exists at repo root - contains environment configuration (contents not inspected).
- Env vars read by code (with defaults):
  - Redis: `REDIS_HOST` (`redis`), `REDIS_PORT` (`6379`), `REDIS_PASSWORD` (`""`)
  - Exchange/broker: `EXCHANGE_ID` (`indodax`), `EXCHANGE_API_KEY`, `EXCHANGE_SECRET_KEY`, plus fallbacks `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` (`services/orchestrator/src/broker_gateway.py:165-167`)
  - Trading: `TRADING_SYMBOL` (`PAXG/IDR`), `TRADING_MODE` (`PAPER`), `PAPER_INITIAL_BALANCE` (`150000000.0` for IDR pairs, else `10000.0`)
  - Risk: `MAX_CAPITAL_RISK_PER_TRADE` (`0.05`), `DAILY_MAX_DRAWDOWN` (`-2500000.0` for IDR pairs, `-150.0` otherwise), `MAX_CONCURRENT_POSITIONS` (`1`)
  - Syariah: `SHARIAH_STRICT_MODE` (`true`), `MAX_LEVERAGE_RATIO` (`1.0`)
  - News filter: `NEWS_BLACKOUT_MINUTES_BEFORE` (`30`), `NEWS_BLACKOUT_MINUTES_AFTER` (`30`)
  - Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
  - Storage: `DB_PATH` (`/app/data/trading.db`)

**Build:**
- `Dockerfile` - single shared image for all four services.
- `docker-compose.yml` - Redis 7-alpine + four app containers, named volumes `redis_data`, bind mounts `./data` and `./backups`.
- No `pyproject.toml`, `setup.py`, `Makefile`, or CI config.

## Platform Requirements

**Development:**
- Docker + Docker Compose.
- Access to Indodax public endpoints for market data.
- Telegram bot token and chat id for the control panel.
- `.env` file at repo root (not committed).

**Production:**
- Docker host with persistent volumes for `/app/data`, `/app/backups`, and Redis AOF data.
- Outbound HTTPS to `indodax.com` (market data) and `api.telegram.org` (bot polling).
- Outbound TCP to the exchange API for live order placement when `TRADING_MODE=LIVE`.

---

*Stack analysis: 2026-11-18*
