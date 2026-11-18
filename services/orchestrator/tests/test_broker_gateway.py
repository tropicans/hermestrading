"""Tes broker: pembulatan lot, PnL realistis, dan penolakan order yang tidak terisi."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import broker_gateway as bg  # noqa: E402


def test_paper_pnl_includes_fees():
    broker = bg.PaperSpotBroker(initial_balance=15_000_000.0)
    buy = broker.execute_spot_order("PAXG/IDR", "BUY", 0.000438, 69_293_940.0)
    res = broker.close_spot_position(buy["ticket"], 0.000438, 69_293_940.0, 68_865_014.0)

    gross = (68_865_014.0 - 69_293_940.0) * 0.000438
    fees = ((69_293_940.0 * 0.000438) + (68_865_014.0 * 0.000438)) * bg.TAKER_FEE_RATE

    assert res["pnl"] < gross
    assert res["pnl"] == pytest.approx(gross - fees, abs=0.02)
    assert res["fee"] > 0


def test_paper_rejects_insufficient_balance():
    broker = bg.PaperSpotBroker(initial_balance=1000.0)
    with pytest.raises(bg.BrokerExecutionError):
        broker.execute_spot_order("PAXG/IDR", "BUY", 1.0, 69_000_000.0)


def _live_broker_with_stub_client():
    broker = bg.CCXTSpotBroker.__new__(bg.CCXTSpotBroker)
    broker.exchange_id = "indodax"
    broker._markets_patched = True
    broker._market_loaded_at = 0.0
    broker._markets_cache = {"PAXG/IDR": {"symbol": "PAXG/IDR", "limits": {"amount": {"min": 0.0001}}}}
    client = MagicMock()
    client.amount_to_precision.side_effect = lambda sym, vol: f"{round(vol, 4):.4f}"
    client.price_to_precision.side_effect = lambda sym, p: f"{round(p, 0):.0f}"
    client.has = {"fetchOrder": True}
    broker.client = client
    return broker


def test_amount_precision_rounds_to_indodax_lot():
    broker = _live_broker_with_stub_client()
    assert broker._amount_precision("PAXG/IDR", 0.000438) == 0.0004


def test_amount_precision_respects_minimum_lot():
    broker = _live_broker_with_stub_client()
    broker.client.amount_to_precision.side_effect = lambda sym, vol: f"{round(vol, 4):.4f}"
    assert broker._amount_precision("PAXG/IDR", 0.00002) == 0.0001


def test_wait_for_fill_raises_when_never_filled():
    broker = _live_broker_with_stub_client()
    broker.client.fetch_order.return_value = {"id": "1", "status": "open", "filled": 0.0}
    broker.client.cancel_order.return_value = {"id": "1", "status": "canceled"}
    with patch.object(bg, "FILL_TIMEOUT_SEC", 0.2), patch.object(bg.time, "sleep", lambda *_: None):
        with pytest.raises(bg.OrderNotFilledError):
            broker._wait_for_fill({"id": "1"}, "PAXG/IDR")


def test_real_fill_price_uses_average_not_requested():
    broker = _live_broker_with_stub_client()
    order = {"average": 69_450_000.0, "price": 69_293_940.0, "filled": 0.0004}
    assert broker._real_fill_price(order, 69_293_940.0) == 69_450_000.0


def test_live_mode_without_keys_warns_and_uses_paper(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.delenv("EXCHANGE_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    broker = bg.get_broker_client()
    assert isinstance(broker, bg.PaperSpotBroker)
