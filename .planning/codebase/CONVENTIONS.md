---
last_mapped_commit: d1a9d2cdaa03771ac60583cfd0a95a8769237e3b
last_mapped_at: 2026-11-18
---
# Coding Conventions

**Analysis Date:** 2026-11-18

## Language and Runtime

**Primary:**
- Python 3.10 (`Dockerfile:1` — `python:3.10-slim`)
- Python 3.11 (`services/guardian/Dockerfile:1` — `python:3.11-slim-bookworm`); service-level Dockerfiles exist alongside the root image. Standardize on one base image when adding code.

**Style baseline:**
- No formatter, linter, or type checker configured. No `pyproject.toml`, `setup.cfg`, `tox.ini`, `.flake8`, `ruff.toml`, or `mypy.ini` anywhere in the repo.
- Effective style is PEP 8 with 4-space indentation and ~120-column lines (many lines exceed 100, e.g. `services/guardian/src/main.py:163-177`, `services/orchestrator/src/main.py:260`).
- Write PEP 8 code and keep new lines under ~120 characters. Do not introduce a formatter config in isolation; if adopting one, apply it repo-wide in a separate change.

## Naming Patterns

**Files:**
- `snake_case.py` for all modules: `strategy_engine.py`, `broker_gateway.py`, `syariah_guard.py`, `news_filter.py`, `chart_generator.py`, `backup_daemon.py`, `healthcheck.py`.
- One module per concern inside a service's `src/`; no packages, no `__init__.py`, no nested modules.

**Classes:**
- `PascalCase`, noun-first, sometimes with domain prefix/suffix: `MarketDataFeed`, `EMAMomentumStrategy`, `CCXTSpotBroker`, `PaperSpotBroker`, `SyariahGuard`, `NewsBlackoutFilter`.
- Domain exceptions are `PascalCase` + `Error` and subclass bare `Exception` with `pass` body:
  - `BrokerExecutionError` (`services/orchestrator/src/broker_gateway.py:6-7`)
  - `SyariahComplianceError` (`services/workers/src/syariah_guard.py:3-4`)

**Functions and methods:**
- `snake_case`, verb-first: `get_spot_price`, `calculate_ema`, `process_trailing_and_exits`, `dispatch_trade_proposal`, `validate_trade`, `is_in_blackout`, `run_backup`.
- Private helpers get a single leading underscore: `_get_public_exchange`, `_ensure_indodax_markets`, `_split_pair`, `_fetch_live_candles_fallback`.
- Predicate functions may use `is_` / `check_` prefix: `is_admin`, `check_risk_circuit_breaker`.

**Variables:**
- `snake_case`; abbreviations are common and accepted: `r` (Redis client), `conn`/`cursor`, `tp`, `sl`, `pnl`, `vol`, `pos`, `acc`, `curr`/`curr_p`, `ema_f`/`ema_s`.
- Loop placeholders use `i`, `e` (exception), `msg`, `row`, `p`.

**Constants:**
- Module-level `UPPER_SNAKE_CASE`, defined immediately after imports and env bootstrap: `REDIS_HOST`, `TRADING_SYMBOL`, `DB_PATH`, `BREAKEVEN_THRESHOLD_PCT`, `TRAILING_DISTANCE_PCT`, `MAX_CONCURRENT_POSITIONS` (`services/orchestrator/src/main.py:17-33`; `services/guardian/src/main.py:45-55`).
- Emoji constants are module-level `EMOJI_*` strings written as `\U` escapes (`services/guardian/src/main.py:21-43`). Reuse these; do not inline emoji in new guardian messages.

**Types:**
- Light type hints only on public helper signatures: `-> str`, `-> float`, `-> dict`, `-> list`, `-> tuple[bool, str]`, `price: float`, `pair: str = None`.
- No `typing` imports anywhere. Prefer builtin generics (`dict`, `list`, `tuple[bool, str]`) if adding hints.
- Docstrings exist on public classes/methods in `strategy_engine.py`, `broker_gateway.py`, `chart_generator.py`; private helpers and most `main.py` functions have none.

## Imports

**Order (follow when adding code):**
1. Encoding declaration where present: `# -*- coding: utf-8 -*-` (present in `services/guardian/src/main.py:1`, `services/guardian/src/chart_generator.py:1`, `services/orchestrator/src/main.py:1`, `services/orchestrator/src/strategy_engine.py:1`, `services/orchestrator/src/broker_gateway.py:1`; absent in `database.py`, all of `services/workers/src/`, `backup_daemon.py`).
2. Standard library (`os`, `sys`, `time`, `json`, `sqlite3`, `datetime`, `threading`, `uuid`, `shutil`, `io`, `urllib.request`, `pathlib`).
3. Third-party (`redis`, `telebot`/`telebot.apihelper`, `ccxt`, `matplotlib`, `pandas`).
4. Local flat modules (`from broker_gateway import ...`, `from strategy_engine import ...`, `from database import ...`, `from syariah_guard import ...`).

**Rules:**
- Imports are absolute and flat because each service runs with its `src/` as working-directory-on-`sys.path`. Never use relative imports (`from .x import y`) — there are no packages.
- Do not import across service boundaries except via the existing path-injection hack in `services/guardian/src/main.py:14-19`:
  ```python
  sys.path.insert(0, '/app/services/orchestrator/src')
  try:
      from broker_gateway import get_broker_client
      _global_broker = get_broker_client()
  except Exception:
      _global_broker = None
  ```
  Reuse this pattern only if unavoidable; prefer reading state from Redis or SQLite.
- Grouped imports can be multi-line with parentheses (`services/orchestrator/src/main.py:12-15`); no strict blank-line separation is enforced between groups today.

## Module Layout and Side Effects

**Standard module order:**
1. Encoding header + imports.
2. Module constants and env bootstrap via `os.getenv(NAME, default)`.
3. Process-level singletons created at import time: Redis client `r`, `broker`, `strategy`, Telegram `bot`, SQLite init.
4. Helper functions.
5. Telegram handlers (guardian only), decorated with `@bot.message_handler` / `@bot.callback_query_handler`.
6. `if __name__ == "__main__":` entrypoint that starts daemon threads then blocks in the main loop.

**Known pattern (do not expand):**
- Import-time side effects are pervasive: `r = redis.Redis(...)` then `r.ping()` and `sys.exit(1)` on failure (`services/orchestrator/src/main.py:35-46`, `services/workers/src/main.py:13-24`, `services/guardian/src/main.py:77-88`), `init_db()` and `broker = get_broker_client()` (`services/orchestrator/src/main.py:70-72`), `bot = telebot.TeleBot(...)` (`services/guardian/src/main.py:75`).
- New code should not add more import-time side effects. Put new behavior in functions callable from `__main__` or the existing loop so it stays testable.

**Entrypoint guard:**
- Every executable module ends with `if __name__ == "__main__":` (`services/orchestrator/src/main.py:396`, `services/workers/src/main.py:79`, `services/guardian/src/main.py:594`, `services/backup_vault/backup_daemon.py:52`).
- Pure libraries (`database.py`, `broker_gateway.py`, `strategy_engine.py`, `syariah_guard.py`, `news_filter.py`, `exchange.py`) have no `__main__` block.

## Configuration

**Environment access:**
- Read config inline with `os.getenv("NAME", default)` at module scope; no settings module, no validation layer.
- `.env` is loaded by Docker Compose via `env_file: - .env` (`docker-compose.yml:26-27`, `:40-41`, `:54-55`, `:69-70`). `python-dotenv` is listed in requirements but never imported; do not add `load_dotenv()` to service code — the container injects env vars.
- Naming: `UPPER_SNAKE` env keys, domain-prefixed (`TRADING_MODE`, `TRADING_SYMBOL`, `EXCHANGE_ID`, `EXCHANGE_API_KEY`, `EXCHANGE_SECRET_KEY`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `MAX_CAPITAL_RISK_PER_TRADE`, `DAILY_MAX_DRAWDOWN`, `MAX_CONCURRENT_POSITIONS`, `PAPER_INITIAL_BALANCE`, `SHARIAH_STRICT_MODE`, `MAX_LEVERAGE_RATIO`, `NEWS_BLACKOUT_MINUTES_BEFORE/AFTER`, `DB_PATH`).
- Never commit `.env`; `.gitignore:2` ignores it. `.env.production` exists in the working tree and is not ignored — never print or copy its values.
- Booleans: `.lower() == "true"` (`services/workers/src/syariah_guard.py:8`). Numeric envs: wrap in `int()` / `float()` at read site.
- Secrets gating: paper mode is chosen when mode is not `LIVE` or keys are blank/placeholders (`services/orchestrator/src/broker_gateway.py:169`).

**Thresholds:**
- Hard-coded strategy constants live at the top of the owning module (`BREAKEVEN_THRESHOLD_PCT = 0.0065`, `TRAILING_DISTANCE_PCT = 0.0018` — `services/orchestrator/src/main.py:29-30`); user-tunable risk values are env vars. Follow this split: market-microstructure constants in code, operator-tunable limits in env.

## Domain Idioms (use these exact patterns)

**Currency-aware rounding:**
- IDR pairs round to whole units, USD pairs to 2 decimals with the repeated ternary idiom:
  ```python
  round(price, 0 if IS_IDR_PAIR else 2)
  ```
  (`services/orchestrator/src/main.py:167`, `:305-308`; `services/orchestrator/src/strategy_engine.py:260-275`). `IS_IDR` / `IS_IDR_PAIR` derived as `"IDR" in TRADING_SYMBOL` (`services/guardian/src/main.py:52`, `services/orchestrator/src/main.py:22`).

**Money formatting:**
- Guardian formats all user-visible currency through `fmt_curr()`:
  ```python
  def fmt_curr(val) -> str:
      try:
          v = float(val or 0.0)
      except Exception:
          v = 0.0
      if IS_IDR:
          return f"Rp {v:,.0f}"
      return f"${v:,.2f}"
  ```
  (`services/guardian/src/main.py:57-64`). Never hand-format currency in new Telegram messages.

**Telegram HTML safety:**
- All bot messages use `parse_mode="HTML"` and escape untrusted text with `.replace("<", "&lt;").replace(">", "&gt;")` (`services/guardian/src/main.py:206`, `:514`). Keep this for log lines and error strings.
- Message layout convention: `<b>` title, `━━━━━━━━━━━━━━━━━━` divider, `• Label: <code>value</code>` rows (`services/guardian/src/main.py:162-177`, `:250-258`).

**Redis channel/key naming:**
- Namespace prefix `hermes:` for every key and channel: `hermes:logs`, `hermes:telemetry`, `hermes:alerts`, `hermes:events`, `hermes:trade_queue`, `hermes:trade_result:<trade_id>`, `hermes:kill_switch`, `hermes:sl_cooldown`, `hermes:sl_hits:<ticket>`. Add new channels under the same prefix.
- Cross-process payloads are JSON strings published via `r.publish(channel, json.dumps(payload))`; producers and consumers both tolerate malformed payloads in a `try/except`.

**Alerts:**
- Every alert payload has a `"type"` discriminator consumed by the guardian's `listen_trade_alerts` switch (`services/guardian/src/main.py:493-581`). Existing types: `CIRCUIT_BREAKER_TRIGGERED`, `SYSTEM_ERROR`, `SIGNAL_SKIPPED`, `TRADE_REJECTED`, `TRAILING_SL_UPDATED`, `ORDER_EXECUTED`, `POSITION_CLOSED`. Add a new `elif event_type == ...` branch in the guardian when introducing a type.

**SQLite access:**
- Always go through `get_connection()` which creates the parent directory and returns a fresh `sqlite3.connect(DB_PATH)` (`services/orchestrator/src/database.py:7-10`). Commit and `close()` in the same function. Use `conn.row_factory = sqlite3.Row` + `dict(row)` for dict results (`services/orchestrator/src/database.py:84-91`).
- Schema changes are additive `ALTER TABLE ... ADD COLUMN` guards in `init_db()` using the `required_cols` dict (`services/orchestrator/src/database.py:51-68`). Add new columns there; do not drop or rename.
- Parameterized queries only (`?` placeholders); no f-string SQL except the migration DDL built from the internal `required_cols` dict.

## Error Handling

**Patterns:**
- Fail fast during bootstrap: Redis ping failure → log then `sys.exit(1)` (`services/orchestrator/src/main.py:44-46`, `services/workers/src/main.py:22-24`, `services/guardian/src/main.py:86-88`).
- Long-running loops wrap each iteration in `try/except Exception` and continue, logging the error: `services/orchestrator/src/main.py:351-394`.
- Domain failures raise custom exceptions; boundary code converts them to log + alert: `raise BrokerExecutionError(...)` inside `CCXTSpotBroker` (`services/orchestrator/src/broker_gateway.py:92-93`, `:121-122`), caught as `BrokerExecutionError` at `services/orchestrator/src/main.py:276-277`; `SyariahComplianceError` caught at `services/workers/src/main.py:74-77`.
- Best-effort reads swallow errors deliberately: `except Exception: pass` for telemetry (`services/orchestrator/src/main.py:317-318`), message edits (`services/guardian/src/main.py:227-231`), balance lookup (`services/guardian/src/main.py:138-143`).
- Return safe defaults instead of raising for non-critical reads: `get_account_balance` returns `{"cash_balance": 0.0, "asset_balance": 0.0}` (`services/orchestrator/src/broker_gateway.py:61-63`); `get_recent_candles` returns `[]` (`services/orchestrator/src/strategy_engine.py:36-38`).
- Prescribe: catch the narrowest exception that is actually handled; log with `sys_log(..., "ERROR", notify_telegram=True)` in orchestrator, `print("[service] ...")` elsewhere. Do not add bare `except:` (none exist); `except Exception` is the accepted form for external-I/O boundaries.

## Logging

**Framework:** `print()` only — no `logging` module anywhere.

**Patterns:**
- Prefixed, lowercase service tag in brackets: `[orchestrator]`, `[guardian]`, `[workers]`, `[backup]`, `[database]`, `[broker_gateway]`, `[strategy_engine]`.
- Orchestrator uses structured helper:
  ```python
  def sys_log(msg: str, level: str = "INFO", notify_telegram: bool = False):
  ```
  It timestamps in WIB (`UTC + 7`), prints with `flush=True`, `LPUSH`es to `hermes:logs` and `LTRIM`s to 50 entries, and optionally publishes a `SYSTEM_ERROR` alert with a 60-second ERROR rate limit (`services/orchestrator/src/main.py:48-68`). Use `sys_log` for all orchestrator logging; levels in use: `"INFO"`, `"WARN"`, `"ERROR"`.
- Other services use raw `print(f"[service] ...")`; guardian/orchestrator log buffer is consumed by the Telegram `/logs` view (`services/guardian/src/main.py:181-217`).
- Log message text is Indonesian, with values formatted inline (`:,.0f` for prices). Keep new log text in Indonesian to match operator expectations.

## Comments and Docstrings

**When to comment:**
- Non-obvious trading logic gets a short Indonesian comment or numbered step marker: `# 1. Cek Kill-Switch`, `# 2. Cek News Filter`, `# 3. Validasi Syariah` (`services/workers/src/main.py:53-68`); `# Hitung EMA Fast (3) & Slow (5)` (`services/guardian/src/chart_generator.py:66`).
- Explain *why* a threshold exists, not what the line does: `# Valid jika sudah terdiskon minimal 23.6% dari Swing High` (`services/orchestrator/src/strategy_engine.py:207`).
- All comments and docstrings are Indonesian; code identifiers remain English. Do not mix English comment prose into new code.

**Docstrings:**
- Triple-quoted one-liners or short paragraphs on public classes/methods that encode business rules: `CCXTSpotBroker` (`services/orchestrator/src/broker_gateway.py:10`), `get_spot_price` (`services/orchestrator/src/strategy_engine.py:42`), `calculate_fib_pullback` (`:176-182`), `generate_paxg_chart` (`services/guardian/src/chart_generator.py:42`).
- No docstring style standard (no Google/NumPy/Sphinx sections). Add a docstring when the function encodes a trading/risk rule.

## Function Design

**Size:** Helper functions are short (5-30 lines). `services/guardian/src/main.py` mixes 60+ line message-builder functions with handlers; `generate_performance_report` (`:112-179`) and `listen_trade_alerts` (`:493-581`) are the largest. Extract new message building into a `build_*_text()` function when it exceeds ~40 lines, following `build_logs_and_telemetry_text` (`:181-217`).

**Parameters:**
- Positional domain args with default `None` for optional context: `get_account_balance(self, pair: str = None)`, `close_spot_position(self, ticket, volume, entry_price, current_price, pair=None)`.
- Keyword args at call sites for readability where multiple floats are involved (`services/orchestrator/src/main.py:236-241`).

**Return values:**
- Dicts with stable snake_case keys for domain results (`{"ticket", "pair", "action", "volume", "fill_price", "total_cost", "remaining_balance"}` — `services/orchestrator/src/broker_gateway.py:83-91`).
- `None` for "no signal" (`EMAMomentumStrategy.analyze` returns `None`, `services/orchestrator/src/strategy_engine.py:293`); a `{"action": "SKIPPED", ...}` dict when a signal was seen but filtered (same file `:252-265`). Preserve this distinction.
- Booleans from `add_price` indicate whether a new sample was recorded (`services/orchestrator/src/strategy_engine.py:88-114`).

## Module Design

**Exports:**
- No `__all__`. Modules expose all top-level names; consumers import exactly the needed functions:
  ```python
  from database import (
      init_db, record_order, record_audit,
      get_open_positions, close_position_db, get_today_realized_pnl, update_trailing_stop
  )
  ```
  (`services/orchestrator/src/main.py:12-15`).

**Barrel files:** Not used. There are no `__init__.py` files.

**Class design:**
- Two adapter classes behind one factory (`get_broker_client`) is the canonical strategy pattern: `CCXTSpotBroker` and `PaperSpotBroker` must keep identical method signatures (`get_account_balance`, `execute_spot_order`, `close_spot_position`) — `services/orchestrator/src/broker_gateway.py:9-174`.
- `MarketDataFeed` uses `@classmethod` with class-level cache state (`_exchange`, `_last_valid_price`, `_last_spread_pct`) instead of instantiation (`services/orchestrator/src/strategy_engine.py:8-71`).
- Strategy state lives on the instance (`self.prices`, `self.highs`, `self.lows`, `self._bucket_high/low`) with a 100-sample cap (`services/orchestrator/src/strategy_engine.py:75-114`).

## Git and Commit Conventions

**Commit messages:** Imperative, sentence-case, no Conventional Commits prefix (only the initial commit used `feat:`):
```
Add Fibonacci Pullback Guard (<= 76.4% swing zone) to prevent buying at local tops
Fix syntax in orchestrator main.py and finalize EMA 5/13 mid-price engine
Adjust PAXG/IDR spread guard threshold to 0.85% to match Indodax orderbook
```
Write new commits in the same imperative style, naming the subsystem (`orchestrator`, `guardian`, `workers`) and the behavior change.

**Branching/CI:** No CI workflow, no `.github/` directory, no pre-commit hooks. `.gitignore` excludes `.env`, `*.db`, `data/`, `backups/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`.

## Anti-Patterns To Avoid When Adding Code

| Avoid | Why | Do instead |
|-------|-----|------------|
| Reading `.env` at runtime with `load_dotenv()` | Env already injected by Compose; keeps secrets in one place | `os.getenv("NAME", default)` as today |
| Adding `__init__.py` / relative imports | Breaks flat `src/` runtime model and Docker `COPY` layout | Flat absolute imports |
| New `except Exception: pass` around trading decisions | Hides failed orders/fills | Log via `sys_log(..., "ERROR", notify_telegram=True)`; swallow only for telemetry/UI |
| Inline emoji or currency formatting in guardian messages | Inconsistent output | `EMOJI_*` constants + `fmt_curr()` |
| New Redis key without `hermes:` prefix | Collides with generic Redis keys and operator tooling | `hermes:<domain>:<id>` |
| Hard-coding exchange name / pair | Multi-exchange via `EXCHANGE_ID` / `TRADING_SYMBOL` is supported | Read env at module top, derive `IS_IDR` |

---

*Convention analysis: 2026-11-18*
