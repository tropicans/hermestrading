# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import uuid
import datetime
import threading
import redis
from broker_gateway import get_broker_client, BrokerExecutionError
from strategy_engine import MarketDataFeed, EMAMomentumStrategy
from database import (
    init_db, record_order, record_audit,
    get_open_positions, close_position_db, get_today_realized_pnl
)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

TRADING_SYMBOL = os.getenv("TRADING_SYMBOL", "BTC/IDR").upper()
IS_IDR_PAIR = "IDR" in TRADING_SYMBOL

DEFAULT_DRAWDOWN = "-2500000.0" if IS_IDR_PAIR else "-150.0"
DAILY_MAX_DRAWDOWN = float(os.getenv("DAILY_MAX_DRAWDOWN", DEFAULT_DRAWDOWN))
MAX_CONCURRENT_POSITIONS = max(3, int(os.getenv("MAX_CONCURRENT_POSITIONS", "3")))

SLIPPAGE_PAD_PCT = float(os.getenv("SLIPPAGE_PAD_PCT", "0.0005"))
TAKER_FEE_RATE = float(os.getenv("INDODAX_TAKER_FEE", "0.0022"))
ROUNDTRIP_COST_PCT = (2 * SLIPPAGE_PAD_PCT) + (2 * TAKER_FEE_RATE)
DAILY_DRAWDOWN_EQUITY_PCT = float(os.getenv("DAILY_DRAWDOWN_EQUITY_PCT", "0.05"))
GRID_TP_PCT = float(os.getenv("GRID_TP_PCT", "0.042"))

last_known_price = 1150000000.0 if IS_IDR_PAIR else 70000.0
_last_error_alert_ts = 0.0

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

def sys_log(msg: str, level: str = "INFO", notify_telegram: bool = False):
    global _last_error_alert_ts
    now_wib = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime("%H:%M:%S")
    formatted = f"[{now_wib}] [{level}] {msg}"
    print(f"[orchestrator] {formatted}", flush=True)
    try:
        r.lpush("hermes:logs", formatted)
        r.ltrim("hermes:logs", 0, 49)
        if notify_telegram:
            now_ts = time.time()
            if level != "ERROR" or (now_ts - _last_error_alert_ts > 60):
                if level == "ERROR":
                    _last_error_alert_ts = now_ts
                r.publish("hermes:alerts", json.dumps({
                    "type": "SYSTEM_ERROR",
                    "level": level,
                    "message": msg,
                    "timestamp": f"{now_wib} WIB"
                }))
    except Exception:
        pass

init_db()
broker = get_broker_client()
strategy = EMAMomentumStrategy(fast_period=20, slow_period=50, trend_period=200, rsi_period=14, atr_period=14)

def get_current_equity() -> float:
    try:
        acc = broker.get_account_balance(TRADING_SYMBOL)
        cash = float(acc.get("cash_balance", 0.0) or 0.0)
        positions = get_open_positions()
        bot_asset = sum(float(p.get("volume", 0.0)) for p in positions if p.get("pair") == TRADING_SYMBOL)
        return cash + (bot_asset * last_known_price)
    except Exception:
        return 0.0

def effective_daily_limit() -> float:
    if DAILY_DRAWDOWN_EQUITY_PCT > 0:
        equity = get_current_equity()
        if equity > 0:
            return -abs(equity * DAILY_DRAWDOWN_EQUITY_PCT)
    return DAILY_MAX_DRAWDOWN

def check_risk_circuit_breaker() -> bool:
    today_pnl = get_today_realized_pnl()
    limit = effective_daily_limit()
    if today_pnl <= limit:
        today_key = (datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%d")
        breaker_key = f"hermes:circuit_breaker:{today_key}"
        if r.get(breaker_key) == "active":
            return False
        r.setex(breaker_key, 86400, "active")
        r.set("hermes:kill_switch", "active")
        sys_log(f"CIRCUIT BREAKER! Rugi harian ({today_pnl:,.2f}) menyentuh limit ({limit:,.2f}).", "WARN")
        r.publish("hermes:alerts", json.dumps({
            "type": "CIRCUIT_BREAKER_TRIGGERED",
            "reason": f"Akumulasi rugi harian menyentuh batas: {today_pnl:,.2f} (limit {limit:,.2f})."
        }))
        return False
    return True

def calculate_grid_volume(price: float, layer_weight: float = 0.25) -> float:
    acc = broker.get_account_balance(TRADING_SYMBOL)
    available_cash = float(acc.get("cash_balance", 0.0) or 0.0)
    positions = [p for p in get_open_positions() if p.get("pair") == TRADING_SYMBOL]
    locked_cost = sum(float(p.get("total_cost", 0.0) or 0.0) for p in positions)
    cycle_capital = available_cash + locked_cost

    target_alloc = cycle_capital * layer_weight
    allocated_cash = min(available_cash * 0.98, target_alloc)
    if IS_IDR_PAIR and available_cash >= 20000.0:
        allocated_cash = max(allocated_cash, min(available_cash * 0.95, 20000.0))
    if price <= 0:
        return 0.0001
    raw_volume = allocated_cash / price
    min_volume = float(os.getenv("MIN_TRADE_VOLUME", "0.00005"))
    return max(min_volume, round(raw_volume, 8))

def close_single_position(pos: dict, current_price: float, reason: str = "MANUAL_CLOSE"):
    global broker
    ticket = pos["ticket"]
    buy_p = float(pos["fill_price"])
    vol = float(pos["volume"])
    pair = pos["pair"]

    try:
        res = broker.close_spot_position(ticket, vol, buy_p, current_price, pair=pair)
        realized_close = res.get("fill_price", current_price)
        close_position_db(ticket, realized_close, res["pnl"], res["remaining_balance"], exit_reason=reason)

        r.publish("hermes:alerts", json.dumps({
            "type": "POSITION_CLOSED",
            "ticket": ticket,
            "pair": pair,
            "reason": reason,
            "entry_price": buy_p,
            "close_price": realized_close,
            "pnl": res["pnl"],
            "fee": res.get("fee", 0.0),
            "balance": res["remaining_balance"]
        }))
        check_risk_circuit_breaker()
        sys_log(f"Posisi {ticket} ditutup ({reason}) @ {realized_close:,.0f} | Net PnL: {res['pnl']:,.0f}")
    except BrokerExecutionError as e:
        sys_log(f"Gagal menutup posisi {ticket} ({reason}): {e}. Posisi dibiarkan OPEN.", "ERROR", notify_telegram=True)
    except Exception as e:
        sys_log(f"Error tak terduga saat menutup posisi {ticket}: {e}", "ERROR", notify_telegram=True)

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
                sys_log(f"Error manual close: {e}", "ERROR", notify_telegram=True)

def process_grid_exits(pair: str, current_price: float):
    """Evaluasi Weighted Average Take Profit (+4.2%) untuk seluruh lapis grid terbuka."""
    positions = [p for p in get_open_positions() if p["pair"] == pair]
    if not positions:
        return

    tot_vol = sum(float(p["volume"]) for p in positions)
    if tot_vol <= 0:
        return

    tot_cost = sum(float(p.get("total_cost") or (float(p["fill_price"]) * float(p["volume"]))) for p in positions)
    avg_entry = tot_cost / tot_vol
    weighted_tp = avg_entry * (1.0 + GRID_TP_PCT)

    if current_price >= weighted_tp:
        sys_log(f"GRID TARGET TERCAPAI! Harga {current_price:,.0f} >= Avg TP {weighted_tp:,.0f} (Avg Entry {avg_entry:,.0f} | {len(positions)} Lapis). Menutup siklus...")
        for pos in positions:
            close_single_position(pos, current_price, "GRID_AVG_TAKE_PROFIT")
        return

    for pos in positions:
        sl = pos.get("stop_loss")
        if sl and current_price <= float(sl):
            close_single_position(pos, current_price, "DISASTER_STOP_LOSS")

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

    layer_num = proposal.get("layer_num", 1)
    r.rpush("hermes:trade_queue", payload)
    sys_log(f"Sinyal GRID Lapis #{layer_num} BUY @ {proposal.get('trigger_price'):,.0f} (Vol: {proposal.get('volume')}). Meminta validasi worker...")

    start_wait = time.time()
    while time.time() - start_wait < 5:
        message = pubsub.get_message(timeout=1.0)
        if message and message["type"] == "message":
            res = json.loads(message["data"])
            if res.get("status") == "APPROVED":
                try:
                    fill_p = MarketDataFeed.get_spot_price(proposal["pair"]) or proposal.get("trigger_price", last_known_price)
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
                        status="OPEN",
                        fill_fee=exec_res.get("fee", 0.0)
                    )
                    record_audit(trade_id, proposal["pair"], "APPROVED", f"Syariah Spot 1:1 (Grid Lapis #{layer_num})")

                    meta_info = proposal.get("meta", {})
                    sys_log(f"GRID LAPIS #{layer_num} FILLED: #{exec_res['ticket']} @ {exec_res['fill_price']:,.0f} | Target TP: {take_profit:,.0f} | RSI: {meta_info.get('rsi')}")

                    r.publish("hermes:alerts", json.dumps({
                        "type": "ORDER_EXECUTED",
                        "trade_id": trade_id,
                        "ticket": exec_res["ticket"],
                        "pair": exec_res["pair"],
                        "action": exec_res["action"],
                        "fill_price": exec_res["fill_price"],
                        "volume": exec_res["volume"],
                        "total_cost": exec_res["total_cost"],
                        "fee": exec_res.get("fee", 0.0),
                        "balance": exec_res["remaining_balance"],
                        "tp": take_profit,
                        "sl": stop_loss,
                        "net_rr": meta_info.get("net_rr")
                    }))
                except BrokerExecutionError as b_err:
                    sys_log(f"Gagal eksekusi broker: {b_err}", "ERROR", notify_telegram=True)
                except Exception as ex_err:
                    sys_log(f"Error tak terduga saat order: {ex_err}", "ERROR", notify_telegram=True)
            else:
                reason = res.get("reason", "Tidak diketahui")
                record_audit(trade_id, proposal["pair"], "REJECTED", reason)
                sys_log(f"Order Grid ditolak Worker: {reason}", "WARN")
                r.publish("hermes:alerts", json.dumps({
                    "type": "TRADE_REJECTED",
                    "trade_id": trade_id,
                    "pair": proposal["pair"],
                    "reason": reason
                }))
            break

    pubsub.unsubscribe(response_channel)

def update_telemetry(pair: str, price: float):
    try:
        ema_f = strategy.calculate_ema(strategy.fast_period) if strategy.prices else price
        ema_s = strategy.calculate_ema(strategy.slow_period) if strategy.prices else price
        rsi_v = strategy.calculate_rsi() if strategy.prices else 50.0
        atr_v = strategy.calculate_atr() if strategy.prices else 0.0
        fib_v = strategy.calculate_fib_pullback(live_price=price)
        now_wib = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S WIB")
        r.set("hermes:telemetry", json.dumps({
            "pair": pair,
            "price": price,
            "ema_fast": round(ema_f, 0 if IS_IDR_PAIR else 2),
            "ema_slow": round(ema_s, 0 if IS_IDR_PAIR else 2),
            "rsi": round(rsi_v, 2),
            "atr": round(atr_v, 0 if IS_IDR_PAIR else 2),
            "fib_retrace": fib_v.get("retrace_pct", 50.0),
            "fib_zone": fib_v.get("zone_pos_pct", 50.0),
            "swing_high": fib_v.get("swing_high", price),
            "swing_low": fib_v.get("swing_low", price),
            "samples": len(strategy.prices),
            "updated_at": now_wib
        }))
    except Exception:
        pass

def start_automated_loop():
    global last_known_price
    pair = TRADING_SYMBOL
    sys_log(f"HERMES v3 (Wide-Swing 3-Layer Spot Grid | Step -4.5% | Avg TP +4.2% | TF {os.getenv('TRADING_TIMEFRAME', '1h')}) Aktif pada pair: {pair}")
    threading.Thread(target=listen_manual_close_events, daemon=True).start()

    sys_log(f"Mengumpulkan candle OHLC awal {pair} dari {os.getenv('EXCHANGE_ID', 'indodax').upper()}...")
    hist_ohlc = MarketDataFeed.get_recent_candles_ohlc(pair, limit=220)
    if len(hist_ohlc) >= 20:
        strategy.ingest_ohlc(hist_ohlc)
        price = MarketDataFeed.get_spot_price(pair)
        last_known_price = price
        update_telemetry(pair, price)
        sys_log(f"Seed dari {len(hist_ohlc)} candle OHLC resmi (ATR & RSI berbasis candle asli).")
    else:
        for _ in range(15):
            try:
                price = MarketDataFeed.get_spot_price(pair)
                strategy.add_price(price, force_sample=True)
                last_known_price = price
                update_telemetry(pair, price)
            except Exception:
                pass
            time.sleep(1)

    sys_log(f"Monitoring pasar aktif! Mid-Price terkini {pair}: Rp {last_known_price:,.0f}")
    tick_counter = 0
    tf_sec = max(60, strategy.timeframe_min * 60)
    last_candle_slot = int(time.time() // tf_sec)

    while True:
        try:
            current_price = MarketDataFeed.get_spot_price(pair)
            last_known_price = current_price
            process_grid_exits(pair, current_price)

            cur_slot = int(time.time() // tf_sec)
            is_new_bar = False
            if cur_slot > last_candle_slot:
                fresh_ohlc = MarketDataFeed.get_recent_candles_ohlc(pair, limit=220)
                if len(fresh_ohlc) >= 20:
                    strategy.ingest_ohlc(fresh_ohlc)
                    is_new_bar = True
                else:
                    is_new_bar = strategy.add_price(current_price, force_sample=True)
                last_candle_slot = cur_slot

            open_positions = [p for p in get_open_positions() if p["pair"] == pair]
            if len(open_positions) < strategy.max_layers:
                proposal = strategy.evaluate_signal(pair, current_price, open_positions=open_positions) if is_new_bar else None
                update_telemetry(pair, current_price)
                if proposal:
                    if proposal.get("action") == "SKIPPED":
                        meta = proposal.get("meta", {})
                        reason_str = proposal.get("reason", "-")
                        sys_log(f"RADAR: {reason_str} @ {current_price:,.0f}", "INFO")
                        r.publish("hermes:alerts", json.dumps({
                            "type": "SIGNAL_SKIPPED", "pair": pair, "price": current_price,
                            "reason": reason_str, "rsi": meta.get("rsi"),
                            "ema_fast": meta.get("ema_fast"), "ema_slow": meta.get("ema_slow")
                        }))
                    else:
                        weight = proposal.get("layer_weight", 0.25)
                        proposal["volume"] = calculate_grid_volume(current_price, layer_weight=weight)
                        dispatch_trade_proposal(proposal)
                        time.sleep(15)
            else:
                update_telemetry(pair, current_price)

            tick_counter += 1
            if tick_counter % 225 == 0:
                ema_f = strategy.calculate_ema(strategy.fast_period)
                ema_s = strategy.calculate_ema(strategy.slow_period)
                rsi_v = strategy.calculate_rsi()
                l1_trigger = ema_s * (1.0 - strategy.step_pct)
                sys_log(f"Heartbeat OK | Harga: {current_price:,.0f} | Lapis Aktif: {len(open_positions)}/3 | EMA50: {ema_s:,.0f} | Trigger L1: {l1_trigger:,.0f} | RSI: {rsi_v:.1f}")
        except Exception as e:
            sys_log(f"Error pada loop utama: {e}", "ERROR", notify_telegram=True)
        time.sleep(4)

if __name__ == "__main__":
    start_automated_loop()
