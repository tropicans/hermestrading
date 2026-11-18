"""Tes skema DB: migrasi kolom baru dan penyimpanan exit_reason."""

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture()
def db_module(tmp_path, monkeypatch):
    import importlib
    import database

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "trading.db"))
    importlib.reload(database)
    database.init_db()
    return database


def _seed(db, ticket="T1", pnl=0.0):
    db.record_order(
        trade_id="trade-1", ticket=ticket, pair="PAXG/IDR", action="BUY",
        contract_type="SPOT", volume=0.0004, fill_price=69_000_000.0,
        total_cost=27_600.0, remaining_balance=100_000.0,
        tp=70_000_000.0, sl=68_400_000.0, status="OPEN", fill_fee=60.72,
    )


def test_init_db_adds_new_columns(db_module):
    conn = db_module.get_connection()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    conn.close()
    assert "exit_reason" in cols
    assert "fill_fee" in cols


def test_close_position_persists_exit_reason(db_module):
    _seed(db_module)
    db_module.close_position_db("T1", 68_900_000.0, -149.0, 99_000.0, exit_reason="STOP_LOSS_ATR")
    row = db_module.get_order_by_ticket("T1")
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "STOP_LOSS_ATR"
    assert row["closed_at"] is not None


def test_fill_fee_is_stored(db_module):
    _seed(db_module)
    row = db_module.get_order_by_ticket("T1")
    assert row["fill_fee"] == pytest.approx(60.72)


def test_open_positions_excludes_closed(db_module):
    _seed(db_module, "T1")
    _seed(db_module, "T2")
    db_module.close_position_db("T1", 68_900_000.0, -149.0, 99_000.0, exit_reason="MANUAL_CLOSE")
    tickets = [p["ticket"] for p in db_module.get_open_positions()]
    assert tickets == ["T2"]
