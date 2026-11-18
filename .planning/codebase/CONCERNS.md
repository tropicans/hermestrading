---
last_mapped_commit: d1a9d2cdaa03771ac60583cfd0a95a8769237e3b
last_mapped_at: 2026-11-18
---
# Codebase Concerns

**Analysis Date:** 2026-11-18

## Tech Debt

**Dead per-service Dockerfiles / single root image used for everything:**
- Issue: `docker-compose.yml` builds `build: .` (the root `Dockerfile`) for all four services. The purpose-built files `services/orchestrator/Dockerfile`, `services/workers/Dockerfile`, `services/guardian/Dockerfile`, `services/backup_vault/Dockerfile` are never used. The per-service files also disagree with the root one (`python:3.11-slim-bookworm` + non-root `hermes_user` in the service files vs `python:3.10-slim` + root user in the root file).
- Files: `docker-compose.yml`, `Dockerfile`, `services/orchestrator/Dockerfile`, `services/workers/Dockerfile`, `services/guardian/Dockerfile`, `services/backup_vault/Dockerfile`
- Impact: Every container runs as root, carries `build-essential`, and contains the entire repo. Fixing runtime base image or hardening is impossible without editing all of them consistently; nobody knows which Dockerfile is authoritative.
- Fix approach: Delete the unused per-service Dockerfiles, or switch compose to `build: { context: ., dockerfile: services/<svc>/Dockerfile }` with per-service `working_dir`. Keep one path.

**`COPY . .` with no `.dockerignore`:**
- Issue: Root `Dockerfile` copies the whole worktree, including `.env.production`, `.git/`, `.planning/`, `.agents/`, `.opencode/`. No `.dockerignore` exists.
- Files: `Dockerfile:13`, missing `.dockerignore`
- Impact: Secrets are baked into every image layer; image size inflated; `git` history shipped to production.
- Fix approach: Add `.dockerignore` (`.git`, `.env*`, `.planning`, `.agents`, `.opencode`, `data`, `backups`, `__pycache__`), and copy only `services/` + `requirements.txt`.

**Duplicated and unpinned dependency manifests:**
- Issue: Root `requirements.txt` duplicates `pyTelegramBotAPI` (lines 7-8) and leaves every package unpinned, while the per-service manifests pin some packages and duplicate entries too (`services/guardian/requirements.txt` repeats `pyTelegramBotAPI` and `matplotlib`). The per-service `requirements.txt` also omit `ccxt`, which `services/orchestrator/src/strategy_engine.py` and `services/guardian/src/chart_generator.py` require at import time.
- Files: `requirements.txt`, `services/orchestrator/requirements.txt`, `services/guardian/requirements.txt`, `services/workers/requirements.txt`
- Impact: Non-reproducible builds; building with the service-level Dockerfiles crashes on `import ccxt`; `websockets` is installed but never imported anywhere.
- Fix approach: One pinned lockfile (`pip-compile` / `uv lock`) per service, no duplicates, `ccxt` declared where imported, drop `websockets`.

**Decorative safety modules:**
- Issue: `services/workers/src/news_filter.py` never fires — `set_blackout_events()` is never called (grep: only its definition), so `scheduled_events` is always `[]` and `is_in_blackout()` always returns `False`. `services/workers/src/exchange.py` is entirely unused. `services/workers/src/main.py:29-38` `listen_emergency_events()` only `print()`s and takes no action. Both `healthcheck.py` scripts are never referenced by `docker-compose.yml`.
- Files: `services/workers/src/news_filter.py:9-11`, `services/workers/src/exchange.py:1-13`, `services/workers/src/main.py:29-38`, `services/orchestrator/src/healthcheck.py`, `services/guardian/src/healthcheck.py`
- Impact: Logs/messages claim "News Filter" protection that cannot trigger (false safety signal before real money). Operator assumes container health is monitored because healthcheck scripts exist.
- Fix approach: Wire a real economic-calendar feed into `set_blackout_events`, or remove the filter and its log line. Delete `exchange.py`. Add `healthcheck:` blocks to compose invoking the existing scripts.

**Hardcoded absolute paths and cross-service imports:**
- Issue: Guardian reaches into another service's source via `sys.path.insert(0, '/app/services/orchestrator/src')` (`services/guardian/src/main.py:14`). DB/backup paths are hardcoded (`DB_PATH = "/app/data/trading.db"`, `BACKUP_DIR = "/app/backups"` in `services/guardian/src/main.py:54-55`, `services/guardian/src/chart_generator.py:14`, `services/backup_vault/backup_daemon.py:8-9`), while `services/orchestrator/src/database.py:5` uses `os.getenv("DB_PATH", ...)`.
- Files: `services/guardian/src/main.py:14`, `services/guardian/src/main.py:54-55`, `services/guardian/src/chart_generator.py:14`, `services/backup_vault/backup_daemon.py:8-10`
- Impact: Guardian only works under the root-Dockerfile layout; with `services/guardian/Dockerfile` the import fails and `_global_broker` silently becomes `None`, so PnL recap loses balance data. Local development requires `/app`-style paths.
- Fix approach: Extract a shared `hermes_core` package (or duplicate the small broker adapter), and read paths from env with a single default.

**Ad-hoc schema migration loop:**
- Issue: `init_db()` creates tables then repeatedly `ALTER TABLE ... ADD COLUMN` for any missing column (`services/orchestrator/src/database.py:51-65`). No migration versioning, no rollback, column types are declarative strings interpolated into SQL.
- Files: `services/orchestrator/src/database.py:12-69`
- Impact: Schema changes require editing this dict; partial failures leave unknown state; no audit of what ran.
- Fix approach: Add a `schema_version` table with ordered migration functions, or adopt Alembic even for SQLite.

**Dead parameters and duplicated formatting logic:**
- Issue: `generate_performance_report(date_str="now")` accepts a date and ignores it entirely (`services/guardian/src/main.py:112-131`); the midnight scheduler still labels its message "EOD UTC" with yesterday's date (`services/guardian/src/main.py:588-589`) while the report is all-time. `fmt_curr` is re-implemented in `fmt_curr` (guardian) and inline `:,.0f` conditionals in orchestrator/strategy.
- Files: `services/guardian/src/main.py:112`, `services/guardian/src/main.py:583-592`, `services/orchestrator/src/main.py:305-308`
- Impact: Misleading daily report; misleading parameter invites wrong callers.
- Fix approach: Either implement date filtering or drop the parameter and rename the scheduler message.

## Known Bugs

**Live order is recorded as filled without ever checking the fill:**
- Symptoms: `execute_spot_order()` submits a *limit* order at `price * 1.001` for IDR buys (`services/orchestrator/src/broker_gateway.py:73-75`), then immediately returns `fill_price = price` and `volume = volume` (`:83-91`). Order status/filled amount is never inspected. Orchestrator writes an `OPEN` row and emits `ORDER_EXECUTED` regardless.
- Files: `services/orchestrator/src/broker_gateway.py:65-93`, `services/orchestrator/src/main.py:236-275`
- Trigger: Any LIVE trade in a moving or thin market (PAXG/IDR).
- Workaround: None. Operator must compare DB against Indodax manually.
- Fix: Poll `fetch_order` until filled/partial, record actual `filled`/`average`, cancel or mark `PENDING` on timeout.

**Position close is assumed successful:**
- Symptoms: `close_spot_position()` sends a limit sell at `current_price * 0.998` and unconditionally computes PnL from the *mid* price minus a hardcoded 0.22% fee estimate (`services/orchestrator/src/broker_gateway.py:107-114`). `close_position_db()` then marks the row `CLOSED` and publishes `POSITION_CLOSED`.
- Files: `services/orchestrator/src/broker_gateway.py:95-122`, `services/orchestrator/src/main.py:98-126`
- Trigger: Live sells that do not fill (price falls below limit), partial fills, fee mismatch.
- Impact: DB says flat while assets remain on the exchange; `remaining_balance` (from API) diverges from `pnl` (estimated), and `get_today_realized_pnl()` drives the circuit breaker from fabricated numbers.
- Fix: Verify fill before DB close; derive PnL from balance delta or `fetch_my_trades`; treat unfilled as retry/alert.

**Orphaned exchange position when the DB write fails:**
- Symptoms: `record_order()` opens a fresh SQLite connection with no `timeout`/`busy_timeout` and no WAL (`services/orchestrator/src/database.py:7-10`). If it raises `database is locked` after the broker call succeeded, the exception is caught in `dispatch_trade_proposal` and only logged (`services/orchestrator/src/main.py:278-279`), leaving a real live position with no DB row — invisible to trailing stops, manual close, and the kill switch.
- Files: `services/orchestrator/src/main.py:236-279`, `services/orchestrator/src/database.py:7-10`
- Trigger: Concurrent write from the manual-close thread, backup daemon hot-copy, or guardian read while a trade executes.
- Fix: Persist intent *before* the broker call (status `PENDING`), use WAL + `busy_timeout`, retry, and reconcile at startup.

**Paper equity silently resets on restart:**
- Symptoms: `PaperSpotBroker.balance` lives only in memory (`services/orchestrator/src/broker_gateway.py:126-160`), initialized to `PAPER_INITIAL_BALANCE` on every process start, while open positions persist in SQLite. Guardian's recap therefore reports `PAPER_INITIAL_BALANCE` forever (`services/guardian/src/main.py:137-147` calls `get_account_balance`, which always returns the fresh 150,000,000).
- Files: `services/orchestrator/src/broker_gateway.py:125-160`, `services/guardian/src/main.py:137-147`
- Trigger: Container restart; opening "Rekap PnL" in paper mode.
- Impact: Paper PnL/balance display is meaningless; sizing after restart uses inflated cash; restart while holding a position double-counts capital.
- Fix: Persist paper balance and asset balance in SQLite (single row `account` table) and reload at startup.

**Circuit breaker latches the kill switch permanently:**
- Symptoms: `check_risk_circuit_breaker()` writes `hermes:kill_switch = "active"` (`services/orchestrator/src/main.py:78`). Nothing ever clears it on a new trading day — only the manual Telegram "Resume" button (`services/guardian/src/main.py:283-287`). Redis runs with `--appendonly yes`, so the flag survives restarts.
- Files: `services/orchestrator/src/main.py:74-85`, `docker-compose.yml:6`
- Trigger: One day over `DAILY_MAX_DRAWDOWN`.
- Impact: Bot stays halted indefinitely even after the daily window resets; if the operator misses the alert, trading never resumes.
- Fix: Give the kill switch a source (`"circuit_breaker"` vs `"manual"`) and auto-clear breaker-sourced kills when the trading day rolls over; keep manual kills sticky.

**"Daily" loss window is UTC, not the WIB trading day:**
- Symptoms: `get_today_realized_pnl()` filters `DATE(closed_at) = DATE('now')` (`services/orchestrator/src/database.py:121`), and SQLite `CURRENT_TIMESTAMP` is UTC. All operator-facing timestamps are WIB (UTC+7) — `services/orchestrator/src/main.py:50`, `services/guardian/src/main.py:182`.
- Files: `services/orchestrator/src/database.py:104-125`, `services/orchestrator/src/main.py:50`
- Impact: The risk day resets at 07:00 WIB; losses between 00:00 and 07:00 WIB count against the previous day, and PnL reports/alerts never agree with the breaker.
- Fix: Store UTC explicit and compute the day boundary with `datetime('now','+7 hours')` (or a configurable timezone) in one helper.

**Double-close race between manual close and the exit loop:**
- Symptoms: `listen_manual_close_events()` runs in a separate thread (`services/orchestrator/src/main.py:325-326`) and calls `get_open_positions()` independently of `process_trailing_and_exits()`. There is no row claim/transaction, so the same ticket can be sold twice. When the asset balance is already 0, `close_spot_position` falls back to selling the original `volume` anyway (`services/orchestrator/src/broker_gateway.py:101-102`).
- Files: `services/orchestrator/src/main.py:128-149`, `services/orchestrator/src/main.py:151-195`, `services/orchestrator/src/broker_gateway.py:95-110`
- Trigger: Telegram "Tutup" pressed within the same 4s tick as a TP/SL trigger, or "TUTUP SEMUA" while a stop fires.
- Fix: Claim the position atomically (`UPDATE orders SET status='CLOSING' WHERE ticket=? AND status='OPEN'` and check `rowcount`), and refuse to sell when `avail_asset == 0`.

**`TUTUP SEMUA` aborts on the first failure:**
- Symptoms: The `ALL` branch loops `close_single_position()` and the first exception (caught inside `close_single_position`, so actually logged and skipped) or any unexpected error in the loop is caught once at the outer handler (`services/orchestrator/src/main.py:140-149`); remaining positions are never attempted.
- Files: `services/orchestrator/src/main.py:98-149`
- Trigger: One position fails to close while several are open.
- Fix: Iterate with per-position try/except and report a per-ticket result summary.

**Stale price treated as live:**
- Symptoms: `MarketDataFeed.get_spot_price()` falls back to `cls._last_valid_price` with no age check (`services/orchestrator/src/strategy_engine.py:68-69`). `last_known_price` is also pre-seeded to `71000000.0` (`services/orchestrator/src/main.py:32`) and used as the close price for manual closes (`:137`).
- Files: `services/orchestrator/src/strategy_engine.py:40-71`, `services/orchestrator/src/main.py:32`, `services/orchestrator/src/main.py:137`
- Trigger: Indodax API outage/rate-limit; manual close before the first tick.
- Impact: Trades and exits execute against a frozen or invented price; kill-switch-free operation looks healthy in telemetry (`updated_at` keeps refreshing).
- Fix: Record `_last_price_ts`, raise/alert past a threshold (e.g. 30s), and never use a seeded constant as an execution price.

**Guardian without a token becomes a silent no-op container:**
- Symptoms: `if not BOT_TOKEN or "ISI_" in BOT_TOKEN: while True: time.sleep(60)` (`services/guardian/src/main.py:66-69`). With `restart: always` and no compose healthcheck, the container reports "Up" forever while no alerts, kill switch, or resume are available.
- Files: `services/guardian/src/main.py:66-69`, `docker-compose.yml:45-58`
- Impact: Loss of the only human control surface, with no signal that it is gone.
- Fix: `sys.exit(1)` (crashloop is visible) or mark unhealthy, plus a compose healthcheck on Redis.

## Security Considerations

**`.env.production` is committed to git:**
- Risk: `git ls-files` lists `.env.production` as tracked (376 bytes). It is not matched by `.gitignore`, which only ignores the exact name `.env` (`/.gitignore:2-3`). Whatever it holds (API keys, bot token, chat id) is in the repository history and will be baked into images by `Dockerfile:13`.
- Files: `.env.production`, `.gitignore`, `Dockerfile:13`
- Current mitigation: None detected (contents not inspected per policy).
- Recommendations: Treat all values in it as compromised and rotate exchange API keys + Telegram bot token. Remove from the index (`git rm --cached .env.production`), add `.env*` with `!.env.example` to `.gitignore`, and purge history. Provide an `.env.example` template instead.

**Redis is unauthenticated and published on all host interfaces:**
- Risk: `docker-compose.yml:7-8` maps `6379:6379`, the image runs with no `requirepass`, and `REDIS_PASSWORD` is referenced but never set in compose. Anyone reaching the host port can read balances/telemetry, rewrite `hermes:telemetry`, inject into `hermes:trade_queue`, clear `hermes:kill_switch`, or drain the log buffer.
- Files: `docker-compose.yml:2-15`, `services/orchestrator/src/main.py:17-19`, `services/guardian/src/main.py:289-291`
- Current mitigation: None.
- Recommendations: Remove the host port mapping (services share the compose network), set `--requirepass`, bind to `127.0.0.1:6379` if host access is truly needed, and drop `REDIS_PASSWORD` defaults to `""`.

**Containers run as root with build tooling installed:**
- Risk: The active root `Dockerfile` has no `USER`, installs `build-essential`/`curl`, and `COPY . .` adds `.git`, `.env.production`, and tooling dirs. Exchange API keys are also injected as env vars (visible via `docker inspect` and `/proc/1/environ`).
- Files: `Dockerfile`, `docker-compose.yml:26-27`, `docker-compose.yml:40-41`, `docker-compose.yml:54-55`, `docker-compose.yml:69-70`
- Current mitigation: The unused per-service Dockerfiles do create `hermes_user`, suggesting intent.
- Recommendations: Non-root user in the image actually built, drop `build-essential` from runtime (multi-stage), file-based secrets (`secrets:`) instead of `env_file` for API keys, `read_only` + tmpfs where possible.

**LIVE-mode credential failure silently downgrades to paper trading:**
- Risk: `get_broker_client()` returns `PaperSpotBroker()` whenever creds are missing or contain `your_`/`ISI_` placeholders, even with `TRADING_MODE=LIVE` (`services/orchestrator/src/broker_gateway.py:163-174`). It only prints a warning.
- Files: `services/orchestrator/src/broker_gateway.py:163-174`, `services/orchestrator/src/main.py:71`
- Current mitigation: A console line and Telegram text formatting elsewhere.
- Recommendations: Fail fast (`sys.exit(1)` + alert) when `TRADING_MODE=LIVE` and credentials are unusable. Make paper mode require an explicit `TRADING_MODE=PAPER`.

**Telegram admin gating depends on a single env value with no fallback:**
- Risk: `is_admin()` compares chat id to `ADMIN_CHAT_ID` (`services/guardian/src/main.py:90-95`). If unset, every command is silently ignored; if wrong, the owner is locked out of kill/resume while `listen_trade_alerts` still pushes to an unset/incorrect chat (`safe_send_alert` swallows failures, `:485-491`).
- Files: `services/guardian/src/main.py:45-46`, `services/guardian/src/main.py:90-95`, `services/guardian/src/main.py:485-491`
- Recommendations: Validate `ADMIN_CHAT_ID` at startup and exit non-zero if missing; log denied admin attempts with the chat id.

**Unescaped alert text in HTML messages:**
- Risk: `alert.get('reason')`, `pair`, and ticket values are interpolated into `parse_mode="HTML"` strings without escaping (`services/guardian/src/main.py:534-539`, `:571-577`). A broker/strategy string containing `<` breaks the message; a crafted pair/reason could inject links.
- Files: `services/guardian/src/main.py:513-579`
- Recommendations: Central `html.escape()` for every dynamic value (the code already does this for logs at `:206` and errors at `:514`).

## Performance Bottlenecks

**Blocking 15s sleep inside the trading loop:**
- Problem: After dispatching a trade, `time.sleep(15)` pauses the entire loop (`services/orchestrator/src/main.py:379`), so trailing stops and exits are not evaluated for 15s — precisely when a fill just happened.
- Files: `services/orchestrator/src/main.py:351-394`
- Cause: Synchronous request/response between orchestrator and worker over a pubsub channel, blocked up to 5s (`:226`) plus 15s cooldown.
- Improvement path: Move trade dispatch to its own thread/queue consumer and let the main loop keep ticking at 4s.

**Two SQLite connections per 4s tick, no WAL:**
- Problem: `get_open_positions()` is called in `process_trailing_and_exits()` and again in the main loop on every tick (`services/orchestrator/src/main.py:355`, `:357`), each opening and closing a connection with no `busy_timeout` and no WAL.
- Files: `services/orchestrator/src/database.py:7-10`, `services/orchestrator/src/main.py:151-195`, `:351-382`
- Cause: Connection-per-call design and no read cache.
- Improvement path: One long-lived connection with `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000`, read positions once per tick, and pass the list to both consumers.

**Exchange REST calls on the hot path:**
- Problem: `get_spot_price()` performs a fresh HTTPS request every 4s (`services/orchestrator/src/strategy_engine.py:50-52`, `:353`), and `calculate_dynamic_volume()` calls `fetch_balance()` on every signal (`services/orchestrator/src/main.py:88`). CCXT's rate limiter is not applied to the direct `urllib` ticker path.
- Files: `services/orchestrator/src/strategy_engine.py:40-71`, `services/orchestrator/src/main.py:87-96`
- Cause: Two independent market-data paths (raw `urllib` for tickers, CCXT only for chart fallback) and no balance cache.
- Improvement path: A single CCXT client for all calls, 1-2s ticker cache, and balance caching with a short TTL invalidated after fills.

**Heavy image for one chart:**
- Problem: `matplotlib` + `pandas` are installed in orchestrator and guardian images solely for `generate_paxg_chart()`, and `plt.style.use` mutates global state while bot handlers run in threads.
- Files: `services/guardian/src/chart_generator.py:7-12`, `services/guardian/src/main.py:270-281`
- Cause: Plotting in-process inside a threaded Telegram bot.
- Improvement path: Isolate rendering in a subprocess or a small sidecar, or emit SVG via a lighter library.

**Unbounded pubsub object creation per trade:**
- Problem: Each proposal creates `r.pubsub()`, subscribes, then calls `unsubscribe` but never `close()` (`services/orchestrator/src/main.py:213-292`); the early-return paths leave the subscription alive.
- Files: `services/orchestrator/src/main.py:213-292`
- Cause: No `try/finally`.
- Improvement path: Wrap in `try/finally: pubsub.close()` or switch to `BLPOP` on a dedicated reply key with a timeout.

## Fragile Areas

**Undocumented Indodax chart endpoint:**
- Files: `services/orchestrator/src/strategy_engine.py:22-38`
- Why fragile: `https://indodax.com/tradingview/history_v2?...` is not a public/contracted API; there is no retry, response shape is guessed (`Close` key), and any change silently returns `[]`, dropping the bot into the 1-per-second synthetic bootstrap path (`services/orchestrator/src/main.py:338-346`).
- Safe modification: Keep the call isolated, add schema validation and an explicit fallback log at WARN, and add a test with a recorded fixture.
- Test coverage: None.

**Synthetic OHLC feeding the Fib guard and ATR:**
- Files: `services/orchestrator/src/strategy_engine.py:101-108`, `:158-173`, `:175-216`
- Why fragile: `highs`/`lows` are manufactured as `price ± 0.02%` from sampled closes, not real candles. In flat markets the 30-sample swing range collapses under the `0.20%` bypass (`:195`), so the "Fib Pullback Guard" silently passes everything; ATR min-clamps to `0.1%` of price (`:160`). Signal tuning therefore behaves differently from what the docs/comments claim.
- Safe modification: Feed real OHLCV candles into the strategy (the CCXT client already has `fetch_ohlcv`, used only in `services/guardian/src/chart_generator.py:24-25`) and keep the synthetic path only for tests.
- Test coverage: None.

**Cross-service import of the broker adapter:**
- Files: `services/guardian/src/main.py:14-19`, `services/orchestrator/src/broker_gateway.py`
- Why fragile: The `try/except` around the import swallows every failure (including a bad CCXT exchange id) and sets `_global_broker = None`; downstream code then silently reports paper defaults. Changing orchestrator's file layout breaks guardian's balances with no error.
- Safe modification: Extract the adapter into a shared module, let import errors surface at startup.
- Test coverage: None.

**Fee math disagrees across files:**
- Files: `services/orchestrator/src/broker_gateway.py:113` (`0.0022` round trip), `services/orchestrator/src/main.py:29` (`BREAKEVEN_THRESHOLD_PCT = 0.0065`), git history mentions "Indodax 0.44% fee"
- Why fragile: PnL in the DB, TP/SL distances, and the breakeven trigger each assume a different fee level; paper mode applies no fee at all (`services/orchestrator/src/broker_gateway.py:152-160`). Strategy backtests/tuning on paper will overstate profitability, and the circuit breaker consumes the inflated PnL.
- Safe modification: Single `FEES` config (maker/taker per exchange) used by paper, live estimate, and breakeven threshold; compute PnL from actual balance delta in live mode.
- Test coverage: None.

**Timezone assumption spread across four files:**
- Files: `services/orchestrator/src/main.py:50`, `services/orchestrator/src/database.py:121`, `services/guardian/src/main.py:164`, `services/guardian/src/main.py:182`, `services/guardian/src/main.py:586-591`
- Why fragile: UTC storage, WIB display, WIB log timestamps, and UTC day boundaries are mixed with no shared helper; adding a new report or risk window will reproduce the mismatch.
- Safe modification: Add `hermes/timeutil.py` with `now_utc()`, `now_wib()`, `trading_day_start()` and use it everywhere.
- Test coverage: None.

## Scaling Limits

**SQLite as the single trading ledger:**
- Current capacity: Two writer contexts in the orchestrator process plus a 6-hourly hot copy and guardian reads; DB is a bind-mounted single file (`docker-compose.yml:29`, `:43`, `:57`, `:72`).
- Limit: First concurrent-write collision produces `database is locked`; already reachable whenever a close and a new fill overlap with the backup daemon (`services/backup_vault/backup_daemon.py:23-28`).
- Scaling path: WAL + `busy_timeout` immediately; move to Postgres if symbol count or strategy instances grow beyond one process.

**Single-symbol, single-position design:**
- Current capacity: `MAX_CONCURRENT_POSITIONS` defaults to `1`, one `TRADING_SYMBOL` per deployment, one strategy instance (`services/orchestrator/src/main.py:21`, `:27`, `:72`).
- Limit: Adding a second pair requires a separate container set because telemetry, `last_known_price`, kill switch scope, cooldown key, and the Paper broker's balance are all global.
- Scaling path: Key `hermes:telemetry:{pair}`, `hermes:sl_cooldown:{pair}`; make the strategy loop per-pair; move account balance to a single account service.

**Redis log ring buffer of 50 entries:**
- Current capacity: `ltrim 0 49` (`services/orchestrator/src/main.py:54-55`), Telegram shows 12 (`services/guardian/src/main.py:203`).
- Limit: Post-incident forensics is impossible beyond a few minutes of activity; logs are also lost on Redis flush.
- Scaling path: Ship to stdout in structured JSON and rely on the container log driver.

**Backups on the same host volume:**
- Current capacity: 6-hour interval, 7-day retention (`services/backup_vault/backup_daemon.py:44-57`); target is `./backups` on the same disk as `./data`.
- Limit: Host/disk loss destroys data and every backup together; worst-case RPO 6h of trades.
- Scaling path: Push snapshots off-host (S3-compatible bucket or another volume) and keep the rotation logic honest about only deleting `trading_backup_*.db` (currently it deletes *any* file older than 7 days, `:46-50`).

## Dependencies at Risk

**`ccxt` unpinned:**
- Risk: Installed without a version in `requirements.txt:10` and `services/workers/requirements.txt`; CCXT ships breaking exchange-API changes frequently, and this code patches Indodax market objects directly (`services/orchestrator/src/broker_gateway.py:26-37` mutates `m["id"]` inside the markets dict).
- Impact: A rebuild can change order semantics or invalidate the ticker-id hack; live orders fail or route to the wrong market.
- Migration plan: Pin CCXT, add a startup smoke test that loads markets and validates `paxg_idr`, and prefer `exchange.options['fetchMarkets']` patching over mutating returned dicts.

**`pyTelegramBotAPI==4.16.1` + monkey-patched apihelper:**
- Risk: The code mutates module-level `apihelper.CONNECT_TIMEOUT/READ_TIMEOUT/RETRY_ON_ERROR` (`services/guardian/src/main.py:71-73`) and uses `callback_data` with ticket strings; library upgrades change these globals and callback semantics.
- Impact: Alert delivery and kill-switch buttons silently stop working.
- Migration plan: Pin and test upgrade in a branch; move to async `python-telegram-bot` long term.

**`matplotlib==3.8.4` / `pandas==2.2.2` pinned only in unused manifests:**
- Risk: The root manifest that actually installs runtime deps pins nothing (`requirements.txt`), while `services/*/requirements.txt` pins versions that are never installed.
- Impact: Two sources of truth; upgrades land untested via the root file.
- Migration plan: Generate one lockfile from per-service declarations and CI-build from it.

## Missing Critical Features

**No startup reconciliation with the exchange:**
- Problem: Nothing compares `orders WHERE status='OPEN'` against actual balances/open orders at boot. After a crash mid-trade the bot resumes with a DB-only view; in LIVE mode it may sell assets it no longer holds or ignore assets it holds.
- Blocks: Safe restarts in LIVE mode; `restart: always` is currently a liability rather than a recovery mechanism.

**No order lifecycle / partial-fill handling:**
- Problem: Only `OPEN`/`CLOSED` exist; no `PENDING`, `PARTIAL`, `CANCELED`, and no cancel path anywhere. `execute_spot_order` never cancels a stale limit order.
- Blocks: Correct PnL, correct position sizing, and the circuit breaker's accuracy.

**No `.env.example` or README:**
- Problem: Deployment requires guessing variable names (`REDIS_*`, `TRADING_MODE`, `EXCHANGE_*`, `TELEGRAM_*`, `PAPER_INITIAL_BALANCE`, `MAX_LEVERAGE_RATIO`, `NEWS_BLACKOUT_*`) from source. `.env` is required by `docker-compose.yml:26-27` but absent.
- Blocks: Reproducible onboarding and any CI check of required config.

**News-blackout protection is not implemented:**
- Problem: `scheduled_events` is never populated and no calendar client exists (`services/workers/src/news_filter.py`).
- Blocks: The advertised "avoid high-impact news" guard, which matters for the gold/PAXG thesis.

**No alerting when the guardian or workers die:**
- Problem: No compose healthchecks, no dead-man's-switch, no heartbeat expiry check on `hermes:telemetry` even though `updated_at` is written (`services/orchestrator/src/main.py:314`).
- Blocks: Knowing that the only control plane (Telegram bot) or the validator (workers) is down while orchestrator keeps trading.

## Test Coverage Gaps

**Entire repository:**
- What's not tested: No test files, no `conftest.py`, no test runner config, no CI workflow (verified by recursive search for `test_*.py`, `*_test.py`, `conftest.py`). `.pytest_cache/` exists but is empty.
- Files: all of `services/**/src/*.py`
- Risk: Any refactor of the risk, sizing, or exit logic is unverifiable.
- Priority: High

**Risk and exit math:**
- What's not tested: `check_risk_circuit_breaker()` latching, `get_today_realized_pnl()` day boundary, trailing-stop promotion, 2-tick SL confirmation (`services/orchestrator/src/main.py:74-195`), and `calculate_dynamic_volume()` floors (`:87-96`).
- Files: `services/orchestrator/src/main.py`, `services/orchestrator/src/database.py`
- Risk: A subtle change silently removes the loss limit; money-losing bug ships green.
- Priority: High

**Broker adapter contract:**
- What's not tested: Fill handling, partial fills, Indodax market-id patching, fee/PnL math, paper-vs-live parity (`services/orchestrator/src/broker_gateway.py`).
- Files: `services/orchestrator/src/broker_gateway.py`
- Risk: Paper results cannot be trusted as a proxy for live behavior.
- Priority: High

**Worker validation path:**
- What's not tested: `SyariahGuard.validate_trade()` accepted-pair list, the queue→pubsub round trip with a 5s timeout, and rejection reasons (`services/workers/src/main.py:40-77`, `services/workers/src/syariah_guard.py`).
- Files: `services/workers/src/main.py`, `services/workers/src/syariah_guard.py`
- Risk: A malformed payload or a renamed `contract_type` silently rejects or approves every trade; no test pins the async handshake.
- Priority: Medium

**Strategy indicators:**
- What's not tested: EMA seeding/`live_price` substitution, RSI noise floor, ATR min clamp, Fib bypass (`services/orchestrator/src/strategy_engine.py:116-216`).
- Files: `services/orchestrator/src/strategy_engine.py`
- Risk: Indicator drift changes signal frequency and cannot be compared across versions.
- Priority: Medium

**Guardian formatting and admin gating:**
- What's not tested: `fmt_curr`, HTML escaping, `is_admin`, report aggregation with NULL/empty DB (`services/guardian/src/main.py:57-179`).
- Files: `services/guardian/src/main.py`
- Risk: Broken messages during incidents, or an unauthorized user reaching kill/resume.
- Priority: Low

---

*Concerns audit: 2026-11-18*
