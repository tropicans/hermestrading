# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import uuid
import threading
import redis
from broker_gateway import get_broker_client, BrokerExecutionError
from strategy_engine import MarketDataFeed, EMAMomentumStrategy
from database import (
    init_db, record_order, record_audit, 
    get_open_positions, close_position_db, get_today_realized_pnl, update_trailing_stop
)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

MAX_CAPITAL_RISK_PER_TRADE = 0.05
DAILY_MAX_DRAWDOWN = -150.0
MAX_CONCURRENT_POSITIONS = 3

# Rasio Trailing disesuaikan agar lebih fleksibel terhadap ayunan emas
BREAKEVEN_THRESHOLD_PCT = 0.0012  # Profit +0.12% sebelum trailing aktif
TRAILING_DISTANCE_PCT = 0.0008

last_known_price = 2685.0

try:
    r = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True
    )
    r.ping()
    print("[orchestrator] Terhubung ke Redis event bus.")
except Exception as e:
    print(f"[orchestrator] Gagal terkoneksi ke Redis: {e}")
    sys.exit(1)

init_db()
broker = get_broker_client()
strategy = EMAMomentumStrategy(fast_period=3, slow_period=5, rsi_period=14, atr_period=14)

def check_risk_circuit_breaker() -> bool:
    today_pnl = get_today_realized_pnl()
    if today_pnl <= DAILY_MAX_DRAWDOWN:
        print(f"[orchestrator] CIRCUIT BREAKER! Rugi harian (${today_pnl}) menyentuh limit.")
        r.set("hermes:kill_switch", "active")
        alert_payload = {
            "type": "CIRCUIT_BREAKER_TRIGGERED",
            "reason": f"Akumulasi rugi harian menyentuh batas: ${today_pnl:,.2f}"
        }
        r.publish("hermes:alerts", json.dumps(alert_payload))
        return False
    return True

def calculate_dynamic_volume(price: float) -> float:
    acc = broker.get_account_balance()
    available_cash = acc.get("cash_balance", 0.0)
    allocated_cash = available_cash * MAX_CAPITAL_RISK_PER_TRADE
    if price <= 0:
        return 0.01
    raw_volume = allocated_cash / price
    return max(0.01, round(raw_volume, 4))

def close_single_position(pos: dict, current_price: float, reason: str = "MANUAL_CLOSE"):
    global broker
    ticket = pos["ticket"]
    buy_p = float(pos["fill_price"])
    vol = float(pos["volume"])
    pair = pos["pair"]

    res = broker.close_spot_position(ticket, vol, buy_p, current_price)
    close_position_db(ticket, current_price, res["pnl"], res["remaining_balance"])
    
    alert_payload = {
        "type": "POSITION_CLOSED",
        "ticket": ticket,
        "pair": pair,
        "reason": reason,
        "entry_price": buy_p,
        "close_price": current_price,
        "pnl": res["pnl"],
        "balance": res["remaining_balance"]
    }
    r.publish("hermes:alerts", json.dumps(alert_payload))
    check_risk_circuit_breaker()
    print(f"[orchestrator] Posisi {ticket} ditutup ({reason}) @ ${current_price} | PnL: ${res['pnl']}")

def listen_manual_close_events():
    sub_r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    pubsub = sub_r.pubsub()
    pubsub.subscribe("hermes:manual_close")

    for msg in pubsub.listen():
        if msg["type"] == "message":
            try:
                data = json.loads(msg["data"])
                target_ticket = data.get("ticket")
                curr_p = last_known_price
                positions = get_open_positions()

                if target_ticket == "ALL":
                    for p in positions:
                        close_single_position(p, curr_p, "MANUAL_CLOSE_ALL")
                else:
                    for p in positions:
                        if p["ticket"] == target_ticket:
                            close_single_position(p, curr_p, "MANUAL_CLOSE")
                            break
            except Exception as e:
                print(f"[orchestrator] Error manual close: {e}")

def process_trailing_and_exits(pair: str, current_price: float):
    positions = get_open_positions()
    for pos in positions:
        if pos["pair"] != pair:
            continue
            
        ticket = pos["ticket"]
        buy_p = pos["fill_price"]
        tp = pos["take_profit"]
        sl = pos["stop_loss"]
        highest_p = pos.get("highest_price") or buy_p

        new_high = max(highest_p, current_price)
        gain_from_entry = (new_high - buy_p) / buy_p

        # Trailing Stop hanya merayap naik jika profit sudah melewati ambang breakeven
        if gain_from_entry >= BREAKEVEN_THRESHOLD_PCT:
            proposed_sl = round(new_high * (1.0 - TRAILING_DISTANCE_PCT), 2)
            if proposed_sl > sl:
                update_trailing_stop(ticket, proposed_sl, new_high)
                r.publish("hermes:alerts", json.dumps({
                    "type": "TRAILING_SL_UPDATED",
                    "ticket": ticket,
                    "new_sl": proposed_sl,
                    "highest": new_high,
                    "entry": buy_p
                }))
                sl = proposed_sl
        elif new_high > highest_p:
            update_trailing_stop(ticket, sl, new_high)

        triggered = None
        if tp and current_price >= tp:
            triggered = "TAKE_PROFIT_ATR"
        elif sl and current_price <= sl:
            triggered = "TRAILING_STOP_LOSS" if sl >= buy_p else "STOP_LOSS_ATR"

        if triggered:
            close_single_position(pos, current_price, triggered)

def dispatch_trade_proposal(proposal: dict):
    if r.get("hermes:kill_switch") == "active":
        return

    if not check_risk_circuit_breaker():
        return

    trade_id = str(uuid.uuid4())[:8]
    proposal["trade_id"] = trade_id
    payload = json.dumps(proposal)

    pubsub = r.pubsub()
    response_channel = f"hermes:trade_result:{trade_id}"
    pubsub.subscribe(response_channel)

    while True:
        msg = pubsub.get_message(timeout=0.2)
        if msg and msg["type"] == "subscribe":
            break

    r.rpush("hermes:trade_queue", payload)

    start_wait = time.time()
    while time.time() - start_wait < 5:
        message = pubsub.get_message(timeout=1.0)
        if message and message["type"] == "message":
            res = json.loads(message["data"])
            if res.get("status") == "APPROVED":
                try:
                    fill_p = proposal.get("trigger_price", 2685.0)
                    take_profit = proposal.get("take_profit")
                    stop_loss = proposal.get("stop_loss")

                    exec_res = broker.execute_spot_order(
                        pair=proposal["pair"],
                        action=proposal["action"],
                        volume=proposal["volume"],
                        price=fill_p
                    )
                    
                    record_order(
                        trade_id=trade_id,
                        ticket=exec_res["ticket"],
                        pair=exec_res["pair"],
                        action=exec_res["action"],
                        contract_type=proposal.get("contract_type", "SPOT"),
                        volume=exec_res["volume"],
                        fill_price=exec_res["fill_price"],
                        total_cost=exec_res["total_cost"],
                        remaining_balance=exec_res["remaining_balance"],
                        tp=take_profit,
                        sl=stop_loss,
                        status="OPEN"
                    )
                    record_audit(trade_id, proposal["pair"], "APPROVED", "Syariah Compliant (Spot 1:1)")

                    meta_info = proposal.get("meta", {})
                    print(f"[orchestrator] FILLED: #{exec_res['ticket']} @ ${fill_p} | TP: ${take_profit} | SL: ${stop_loss} | RSI: {meta_info.get('rsi')} | ATR: ${meta_info.get('atr')}")

                    alert_payload = {
                        "type": "ORDER_EXECUTED",
                        "trade_id": trade_id,
                        "ticket": exec_res["ticket"],
                        "pair": exec_res["pair"],
                        "action": exec_res["action"],
                        "fill_price": exec_res["fill_price"],
                        "volume": exec_res["volume"],
                        "total_cost": exec_res["total_cost"],
                        "balance": exec_res["remaining_balance"],
                        "tp": take_profit,
                        "sl": stop_loss
                    }
                    r.publish("hermes:alerts", json.dumps(alert_payload))
                except BrokerExecutionError as b_err:
                    print(f"[orchestrator] Gagal eksekusi broker: {b_err}")
            else:
                reason = res.get("reason", "Tidak diketahui")
                record_audit(trade_id, proposal["pair"], "REJECTED", reason)
                r.publish("hermes:alerts", json.dumps({
                    "type": "TRADE_REJECTED",
                    "trade_id": trade_id,
                    "pair": proposal["pair"],
                    "reason": reason
                }))
            break
            
    pubsub.unsubscribe(response_channel)

def start_automated_loop():
    global last_known_price
    pair = "PAXGUSD"
    print(f"[orchestrator] Strategy Engine (EMA+RSI+ATR Guard) Aktif pada pair: {pair}...")

    t_manual = threading.Thread(target=listen_manual_close_events, daemon=True)
    t_manual.start()

    # Pemanasan data feed awal (15 tick)
    print("[orchestrator] Mengumpulkan data harga awal untuk inisialisasi indikator...")
    for _ in range(15):
        price = MarketDataFeed.get_spot_price(pair)
        strategy.add_price(price)
        last_known_price = price
        time.sleep(1)

    print("[orchestrator] Monitoring pasar aktif.")

    while True:
        try:
            current_price = MarketDataFeed.get_spot_price(pair)
            last_known_price = current_price
            process_trailing_and_exits(pair, current_price)
            
            open_positions = get_open_positions()
            if len(open_positions) < MAX_CONCURRENT_POSITIONS:
                proposal = strategy.analyze(pair, current_price)
                if proposal:
                    dynamic_vol = calculate_dynamic_volume(current_price)
                    proposal["volume"] = dynamic_vol
                    dispatch_trade_proposal(proposal)
                    time.sleep(15)  # Cooldown pasca entri
        except Exception as e:
            print(f"[orchestrator] Error loop: {e}")
            
        time.sleep(4)

if __name__ == "__main__":
    start_automated_loop()
