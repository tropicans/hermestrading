---
last_mapped_commit: d1a9d2cdaa03771ac60583cfd0a95a8769237e3b
last_mapped_at: 2026-11-18
---
# Testing Patterns

**Analysis Date:** 2026-11-18

## Current State: No Automated Tests

The repository contains **zero test files and no test framework configuration**.

**Evidence:**
- No `tests/`, `test/`, `*_test.py`, `test_*.py`, or `*.spec.py` files under `services/`.
- No `pytest.ini`, `tox.ini`, `pyproject.toml`, `setup.cfg`, `conftest.py`, `noxfile.py`, or `Makefile`.
- No `.github/` directory, so no CI pipeline runs tests.
- `.gitignore:14` ignores `.pytest_cache/`, which indicates pytest was intended but never set up.
- None of `requirements.txt`, `services/guardian/requirements.txt`, `services/orchestrator/requirements.txt`, or `services/workers/requirements.txt` include `pytest`, `pytest-mock`, `fakeredis`, `responses`, or `freezegun`.

**Implication for planners/executors:** Any new test infrastructure is greenfield. Follow the recommended baseline below rather than inventing a parallel structure.

## Test Framework

**Runner (recommended baseline):**
- `pytest` (latest 8.x) — matches the existing `.pytest_cache/` ignore entry.
- Config location to create: `pytest.ini` at repo root (or `pyproject.toml` `[tool.pytest.ini_options]`).
- Suggested config keys:
  ```ini
  [pytest]
  testpaths = tests
  python_files = test_*.py
  python_functions = test_*
  addopts = -q
  ```

**Assertion library:**
- Plain `assert` (pytest built-in). No `unittest`, no `hamcrest`, no `assertpy` in the codebase.

**Supporting packages to add (test-only):**
- `fakeredis` — the entire system talks to Redis via `redis.Redis(...)` clients created at module scope (`services/orchestrator/src/main.py:35-46`, `services/workers/src/main.py:13-24`, `services/guardian/src/main.py:77-88`). Without it, importing any service module fails or hits a real Redis.
- `pytest-mock` (or stdlib `unittest.mock.patch`) — needed to replace `ccxt`, `telebot`, and `urllib.request.urlopen`.
- `freezegun` (optional) — `services/workers/src/news_filter.py:15` and `services/backup_vault/backup_daemon.py:17` call `datetime.now` directly.

**Run commands (once configured):**
```bash
python -m pytest                      # Run all tests
python -m pytest -q                   # Quiet output
python -m pytest -k strategy_engine   # Single module
python -m pytest -x                   # Stop on first failure
```
Coverage is not installed; see [Coverage](#coverage).

## Test File Organization

**Recommended location:** a repo-root `tests/` directory mirroring the service layout, because there are no Python packages to co-locate inside without adding `__init__.py`:

```
tests/
├── conftest.py                    # sys.path + env fixtures + fakeredis
├── orchestrator/
│   ├── test_strategy_engine.py    # pure indicator math
│   ├── test_broker_gateway.py     # paper broker + factory selection
│   ├── test_database.py           # SQLite CRUD with tmp_path
│   └── test_main.py               # risk sizing, trailing-stop, dispatch
├── workers/
│   ├── test_syariah_guard.py      # compliance table tests
│   ├── test_news_filter.py        # blackout window boundaries
│   └── test_worker_flow.py        # approve/reject queue flow
├── guardian/
│   └── test_chart_generator.py    # PNG buffer output
└── backup_vault/
    └── test_backup_daemon.py      # snapshot + 7-day rotation
```

**Why not co-located:** service modules are flat and imported as top-level names (`from strategy_engine import EMAMomentumStrategy`). Co-locating `tests/` inside `services/<svc>/src/` would put test modules on `sys.path` in production containers built from `Dockerfile:13` (`COPY . .`).

**Naming:**
- Files: `test_<module>.py`; functions: `test_<behavior>_<condition>()`; no test classes unless grouping parametrized cases.
- Because each service has same-named modules only (`main.py` × 3), always disambiguate with the service directory in the test path and module-level `sys.path` insertion in `conftest.py`.

## Critical Setup Problem: Import-Time Side Effects

Importing service modules currently executes Redis connections, SQLite init, broker construction, and Telegram bot creation. Examples:

```python
# services/orchestrator/src/main.py:35-46
try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    r.ping()
    print("[orchestrator] Terhubung ke Redis event bus.")
except Exception as e:
    print(f"[orchestrator] Gagal terkoneksi ke Redis: {e}")
    sys.exit(1)
```

```python
# services/orchestrator/src/main.py:70-72
init_db()
broker = get_broker_client()
strategy = EMAMomentumStrategy(fast_period=5, slow_period=13, rsi_period=14, atr_period=14)
```

**Pattern to use in `tests/conftest.py`:** set env vars and stub Redis before inserting `src` on `sys.path`, and prefer `importlib` for modules that must be isolated:

```python
import os, sys
from pathlib import Path
import fakeredis
import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture(autouse=True)
def base_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("TRADING_SYMBOL", "PAXG/IDR")
    monkeypatch.setenv("EXCHANGE_ID", "indodax")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "trading.db"))
    monkeypatch.setenv("REDIS_HOST", "localhost")
    monkeypatch.setenv("REDIS_PORT", "6379")

@pytest.fixture
def fake_redis(monkeypatch):
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr("redis.Redis", lambda *a, **k: client)
    return client

@pytest.fixture
def orchestrator_path():
    p = str(ROOT / "services" / "orchestrator" / "src")
    sys.path.insert(0, p)
    yield p
    sys.path.remove(p)
```

When a test only needs pure functions, prefer importing at module level after path insertion to avoid the Redis bootstrap entirely (e.g. `database.py` has no Redis import).

## Test Structure

**Suite organization (prescribe pure-function first):**

```python
# tests/orchestrator/test_strategy_engine.py
import pytest
from strategy_engine import EMAMomentumStrategy


def make_strategy_with_prices(prices):
    s = EMAMomentumStrategy(fast_period=5, slow_period=13, rsi_period=14, atr_period=14)
    for p in prices:
        s.add_price(p, force_sample=True)
    return s


def test_calculate_ema_short_series_returns_last_price():
    s = make_strategy_with_prices([100.0, 101.0])
    assert s.calculate_ema(5) == 101.0


def test_calculate_rsi_returns_neutral_when_insufficient_samples():
    s = EMAMomentumStrategy()
    assert s.calculate_rsi() == 50.0   # services/orchestrator/src/strategy_engine.py:131-132
```

```python
# tests/workers/test_syariah_guard.py
import pytest
from syariah_guard import SyariahGuard, SyariahComplianceError


@pytest.mark.parametrize("proposal, expected_message", [
    ({"contract_type": "FUTURES", "pair": "PAXG/IDR"}, "Kontrak 'FUTURES' ditolak"),
    ({"contract_type": "SPOT", "pair": "PAXG/IDR", "swap_fee": 0.01}, "Riba"),
    ({"contract_type": "SPOT", "pair": "PAXG/IDR", "leverage": 2.0}, "Leverage 2.0x"),
    ({"contract_type": "SPOT", "pair": "DOGE/IDR"}, "tidak masuk daftar underlying"),
])
def test_validate_trade_rejections(proposal, expected_message):
    with pytest.raises(SyariahComplianceError, match=expected_message):
        SyariahGuard().validate_trade(proposal)


def test_validate_trade_accepts_valid_spot(monkeypatch):
    monkeypatch.setenv("MAX_LEVERAGE_RATIO", "1.0")
    result = SyariahGuard().validate_trade({"contract_type": "SPOT", "pair": "PAXG/IDR"})
    assert result["status"] == "APPROVED"
```

**Setup/teardown:**
- No existing setup fixtures. Use pytest fixtures with `monkeypatch` for env and `tmp_path` for SQLite files.
- `database.py` reads `DB_PATH` at import (`services/orchestrator/src/database.py:5`), so the env fixture must set `DB_PATH` **before** importing the module, or reload with `importlib.reload(database)`.
- Always call `init_db()` in a fixture for DB tests; it is idempotent (`services/orchestrator/src/database.py:12-69`).

## Mocking

**Framework:** `unittest.mock` (stdlib) is sufficient; add `pytest-mock` for the `mocker` fixture if desired. Nothing is mocked today.

**What exists to mock:**
| Boundary | Where | Replace with |
|----------|-------|--------------|
| Redis client | `redis.Redis(...)` in `services/orchestrator/src/main.py:36`, `services/workers/src/main.py:14`, `services/guardian/src/main.py:78`, `services/backup_vault/backup_daemon.py:37` | `fakeredis.FakeRedis` |
| Indodax REST | `urllib.request.urlopen` in `services/orchestrator/src/strategy_engine.py:32`, `:52` | mock returning JSON fixtures |
| CCXT public data | `services/guardian/src/chart_generator.py:22-38` (`fetch_ohlcv`, `fetch_ticker`), `services/orchestrator/src/strategy_engine.py:13-19` | fake exchange object |
| CCXT order placement | `services/orchestrator/src/broker_gateway.py:53-110` | `PaperSpotBroker` or fake `client.create_order` |
| Telegram Bot API | `telebot.TeleBot` `services/guardian/src/main.py:75`; `bot.*` calls throughout | stub object recording calls |
| Clock | `services/workers/src/news_filter.py:15`, `services/backup_vault/backup_daemon.py:17` | `freezegun.freeze_time` or `monkeypatch` the module's `datetime` |
| Sleeps/loops | `time.sleep` in all daemons | `monkeypatch.setattr("time.sleep", lambda *_: None)` |

**Example — mocking the Indodax ticker fetch:**
```python
import io, json
import pytest
from strategy_engine import MarketDataFeed


class _FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): return False


def test_get_spot_price_uses_orderbook_midprice(monkeypatch):
    MarketDataFeed._exchange = None
    MarketDataFeed._last_valid_price = None
    payload = {"ticker": {"buy": "70000000", "sell": "70200000", "last": "70100000"}}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=5: _FakeResponse(json.dumps(payload).encode()),
    )
    assert MarketDataFeed.get_spot_price("PAXG/IDR") == 70100000.0
```

**Example — SQLite with `tmp_path`:**
```python
import importlib

def test_record_and_close_order(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "trading.db"))
    import database
    importlib.reload(database)
    database.init_db()
    database.record_order(
        trade_id="t1", ticket="TICK-1", pair="PAXG/IDR", action="BUY",
        contract_type="SPOT", volume=0.0002, fill_price=71_000_000.0,
        total_cost=14_200.0, remaining_balance=100_000.0,
        tp=72_500_000.0, sl=70_500_000.0,
    )
    database.close_position_db("TICK-1", close_price=72_000_000.0, pnl=1000.0, remaining_balance=101_000.0)
    assert database.get_open_positions() == []
    assert database.get_today_realized_pnl() == 1000.0
```

**What to mock:** all network, Redis, Telegram, filesystem-root paths (`/app/data`, `/app/backups`), and clocks.

**What NOT to mock:**
- `EMAMomentumStrategy` math (`calculate_ema`, `calculate_rsi`, `calculate_atr`, `calculate_fib_pullback`) — test real values.
- `PaperSpotBroker` — it is the intended in-repo test double (`services/orchestrator/src/broker_gateway.py:125-160`), use it directly for order-lifecycle tests.
- `SyariahGuard` / `NewsBlackoutFilter` logic — pass dicts directly.
- `fmt_curr` (`services/guardian/src/main.py:57-64`).

## Fixtures and Factories

**No fixtures or factories exist in-repo.** Create small builders in `tests/conftest.py` rather than per-test dicts:

```python
@pytest.fixture
def trade_proposal():
    return {
        "pair": "PAXG/IDR",
        "action": "BUY",
        "contract_type": "SPOT",
        "trigger_price": 71000000.0,
        "take_profit": 72500000.0,
        "stop_loss": 70500000.0,
        "volume": 0.0002,
        "meta": {"rsi": 55.0, "atr": 500000.0, "ema_fast": 71100000.0, "ema_slow": 70900000.0},
    }

@pytest.fixture
def price_series():
    """15 rising samples so EMA/RSI have enough data (rsi_period + 2 gate)."""
    return [70_000_000.0 + i * 50_000 for i in range(20)]
```

**Location:** `tests/conftest.py` for cross-service fixtures; `tests/<service>/conftest.py` for service-local ones.

**Existing test-data shims to reuse:**
- `PaperSpotBroker` — deterministic fake broker with `TICK-XXXXXXXX` tickets and balance tracking (`services/orchestrator/src/broker_gateway.py:141-150`).
- `NewsBlackoutFilter.set_blackout_events(events)` — injectable event list (`services/workers/src/news_filter.py:11-12`), designed for tests.
- `MarketDataFeed._last_valid_price` / class-level caches — reset these between tests to avoid leakage (`services/orchestrator/src/strategy_engine.py:9-11`).

## Runtime / Manual Verification (what exists today)

The project is verified by running containers and probing health, not by tests.

**Health probes (usable as integration smoke checks):**
- `services/orchestrator/src/healthcheck.py` — exits 0 only if `DB_PATH` exists and `SELECT 1 FROM orders LIMIT 1` succeeds; wire it as `HEALTHCHECK` in the orchestrator Dockerfile if adding CI.
- `services/guardian/src/healthcheck.py` — exits 0 if Redis `PING` succeeds with `socket_timeout=3`.
- Redis itself has a compose healthcheck (`docker-compose.yml:11-15`); orchestrator/workers/guardian/backup_vault define `depends_on: redis: condition: service_healthy` but no own `healthcheck:` blocks (`docker-compose.yml:17-73`).

**Manual smoke commands:**
```bash
docker compose up -d
docker compose logs -f orchestrator workers guardian
docker compose exec orchestrator python src/healthcheck.py
curl -s "https://indodax.com/api/ticker/paxg_idr"   # market data reachability
```

**Telegram-based acceptance checks** (guardian, admin-only via `is_admin`, `services/guardian/src/main.py:90-95`):
- `/status` — system state, kill-switch, telemetry heartbeat.
- `/logs` — Redis `hermes:logs` + `hermes:telemetry`.
- `/summary` — PnL recap from SQLite.
- `/positions` — open positions and close buttons.
- `/chart` — matplotlib PNG generation.
- `/kill` then `/resume` — kill-switch round trip; verify `hermes:kill_switch` set/deleted and event published.

**Paper-mode trade simulation:** set `TRADING_MODE=PAPER` and `PAPER_INITIAL_BALANCE`, run orchestrator, and watch `hermes:alerts` / `hermes:logs` — this is effectively the only end-to-end path exercised today.

## Coverage

**Requirements:** None enforced; no coverage tool installed.

**View coverage (after adding):**
```bash
pip install pytest-cov
python -m pytest --cov=services/orchestrator/src --cov=services/workers/src --cov-report=term-missing
```

**Recommended initial gate:** 60% on pure modules (`strategy_engine.py`, `database.py`, `syariah_guard.py`, `news_filter.py`, `broker_gateway.py`). Guardian Telegram handlers and daemon loops are lower value to unit test; cover them with smoke/integration checks instead.

## Test Types

**Unit Tests (highest value, easiest to add):**
- `EMAMomentumStrategy`: EMA formula (`services/orchestrator/src/strategy_engine.py:116-127`), RSI neutral floor (`:129-156`), ATR min floor (`:158-173`), Fibonacci zone validity threshold 76.4% (`:175-216`), crossover gating and `SKIPPED` reason composition (`:218-293`).
- `PaperSpotBroker`: insufficient balance raises `BrokerExecutionError` (`services/orchestrator/src/broker_gateway.py:137-138`), PnL arithmetic on close (`:152-160`).
- `SyariahGuard`: four rejection rules and one approval path (`services/workers/src/syariah_guard.py:11-43`).
- `NewsBlackoutFilter`: boundary inclusivity `start <= now <= end` (`services/workers/src/news_filter.py:23`), empty list returns free-market message.
- `database.py`: `init_db()` idempotency and additive migration (`:12-69`), `record_order` upsert, `close_position_db`, `get_today_realized_pnl`.
- `fmt_curr` for IDR vs USD output (`services/guardian/src/main.py:57-64`).

**Integration Tests (medium value):**
- Worker approve/reject: publish proposal to `hermes:trade_queue`, assert reply on `hermes:trade_result:<trade_id>` for kill-switch, blackout, and Syariah branches (`services/workers/src/main.py:40-77`).
- Orchestrator dispatch: with `fakeredis`, assert `hermes:trade_queue` gets a JSON payload and `record_order` is called on `APPROVED` (`services/orchestrator/src/main.py:197-292`).
- Backup daemon: create a temp SQLite file, run `run_backup()`, assert snapshot exists and >7-day-old files are removed (`services/backup_vault/backup_daemon.py:15-50`).
- Chart rendering: `generate_paxg_chart()` returns a `BytesIO` starting with the PNG signature `b"\x89PNG"` (`services/guardian/src/chart_generator.py:41-108`).

**E2E Tests:** Not used. No orchestration harness, no browser driver, no compose-based test runner. Do not introduce Playwright/Selenium; this is a headless bot.

## Common Patterns for This Codebase

**Deterministic indicator setup:** always bypass 60-second sampling with `force_sample=True`:
```python
for price in prices:
    strategy.add_price(price, force_sample=True)
```
(`services/orchestrator/src/strategy_engine.py:88-114` — without `force_sample`, calls within 60s return `False` and append nothing.)

**Asserting skip reasons:** `analyze` returns a dict with `action == "SKIPPED"` and comma-joined Indonesian reasons — assert with `in` rather than exact equality:
```python
proposal = strategy.analyze("PAXG/IDR", current_price)
assert proposal["action"] == "SKIPPED"
assert "RSI" in proposal["reason"]
```

**Testing floating-point math:** market math rounds at the boundary. Compare with `pytest.approx` and assert rounded outputs where the code rounds (`round(..., 0 if IS_IDR else 2)`):
```python
assert strategy.calculate_atr() >= current_price * 0.001 - 1   # ATR floor, strategy_engine.py:159-160
assert result["pnl"] == pytest.approx(2000.0, rel=1e-9)
```

**Error testing:** domain errors carry Indonesian messages; match loosely:
```python
with pytest.raises(BrokerExecutionError, match="tidak mencukupi"):
    PaperSpotBroker(initial_balance=100).execute_spot_order("PAXG/IDR", "BUY", 1.0, 71_000_000.0)
```

**Env-dependent assertions:** always `monkeypatch.setenv` in the test (or fixture) — module constants like `DAILY_MAX_DRAWDOWN` and `MAX_CONCURRENT_POSITIONS` are captured at import (`services/orchestrator/src/main.py:24-27`), so changing env after import has no effect without `importlib.reload`.

**Avoid hanging tests:** `dispatch_trade_proposal` waits up to 5s for a worker reply (`services/orchestrator/src/main.py:225-290`) and `evaluate_and_respond` blocks on `BLPOP` with `timeout=2` (`services/workers/src/main.py:44`). In tests, monkeypatch `time.time`/`time.sleep` or call the inner worker function directly instead of the infinite loop.

## Test Coverage Gaps (highest risk first)

| Untested area | Files | Risk if broken silently |
|---------------|-------|--------------------------|
| Risk circuit breaker and daily drawdown | `services/orchestrator/src/main.py:74-85`, `services/orchestrator/src/database.py:115-125` | Real capital continues trading past loss limit |
| Trailing-stop / breakeven / 2-tick SL confirmation | `services/orchestrator/src/main.py:151-195` | Exits mis-fire or never fire; fee-negative exits |
| Position sizing | `services/orchestrator/src/main.py:87-96` | Oversized orders, insufficient-balance errors |
| Syariah compliance rules | `services/workers/src/syariah_guard.py` | Non-compliant contracts/leverage approved |
| Broker live-order construction (IDR rounding, ticker_id patch) | `services/orchestrator/src/broker_gateway.py:26-122` | Failed or mispriced live orders |
| SQLite migration + PnL aggregation | `services/orchestrator/src/database.py` | Wrong PnL, lost audit trail |
| Backup retention/rotation | `services/backup_vault/backup_daemon.py:43-50` | Silent data loss after 7 days |
| Guardian message/format helpers | `services/guardian/src/main.py:57-217` | Broken operator UI, unescaped HTML errors |

**Priority:** High for orchestrator risk/exit logic and `syariah_guard.py`; Medium for database and broker adapters; Low for Telegram formatting.

---

*Testing analysis: 2026-11-18*
