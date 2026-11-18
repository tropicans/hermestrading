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
    get_open_positions, close_position_db, get_today_realized_pnl, update_trailing_stop
)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

TRADING_SYMBOL = os.getenv("TRADING_SYMBOL", "PAXG/IDR").upper()
IS_IDR_PAIR = "IDR" in TRADING_SYMBOL

MAX_CAPITAL_RISK_PER_TRADE = float(os.getenv("MAX_CAPITAL_RISK_PER_TRADE", "0.05"))
DEFAULT_DRAWDOWN = "-2500000.0" if IS_IDR_PAIR else "-150.0"
DAILY_MAX_DRAWDOWN = float(os.getenv("DAILY_MAX_DRAWDOWN", DEFAULT_DRAWDOWN))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))

BREAKEVEN_THRESHOLD_PCT = float(os.getenv("TRAILING_ARM_PCT", "0.0120"))
TRAILING_DISTANCE_PCT = float(os.getenv("TRAILING_DISTANCE_PCT", "0.0040"))
# Biaya bolak-balik: 2 x slippage pad + 2 x fee taker. Trailing hanya menguntungkan
# bila jarak arm melebihi angka ini plus jarak trailing.
SLIPPAGE_PAD_PCT = float(os.getenv("SLIPPAGE_PAD_PCT", "0.0005"))
TAKER_FEE_RATE = float(os.getenv("INDODAX_TAKER_FEE", "0.0022"))
ROUNDTRIP_COST_PCT = (2 * SLIPPAGE_PAD_PCT) + (2 * TAKER_FEE_RATE)
# Batas kerugian harian sebagai fraksi ekuitas; 0 berarti pakai nilai absolut DAILY_MAX_DRAWDOWN.
DAILY_DRAWDOWN_EQUITY_PCT = float(os.getenv("DAILY_DRAWDOWN_EQUITY_PCT", "0.03"))

last_known_price = 71000000.0 if IS_IDR_PAIR else 2685.0
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
strategy = EMAMomentumStrategy(fast_period=5, slow_period=13, rsi_period=14, atr_period=14)

def get_current_equity() -> float:
    """Nilai ekuitas saat ini (kas + nilai aset) untuk batas drawdown proporsional."""
    try:
        acc = broker.get_account_balance(TRADING_SYMBOL)
        cash = float(acc.get("cash_balance", 0.0) or 0.0)
        asset = float(acc.get("asset_balance", 0.0) or 0.0)
        return cash + (asset * last_known_price)
    except Exception:
        return 0.0

def effective_daily_limit() -> float:
    """Batas rugi harian: persentase ekuitas bila aktif, kalikan -1 untuk batas bawah."""
    if DAILY_DRAWDOWN_EQUITY_PCT > 0:
        equity = get_current_equity()
        if equity > 0:
            return -abs(equity * DAILY_DRAWDOWN_EQUITY_PCT)
    return DAILY_MAX_DRAWDOWN

def check_risk_circuit_breaker() -> bool:
    """Cek drawdown harian. Kill-switch dipasang hanya sekali per hari agar tidak latch permanen."""
    today_pnl = get_today_realized_pnl()
    limit = effective_daily_limit()
    if today_pnl <= limit:
        today_key = (datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%d")
        breaker_key = f"hermes:circuit_breaker:{today_key}"

        if r.get(breaker_key) == "active":
            # Sudah trip hari ini. Jangan menimpa kill-switch yang mungkin sudah di-resume admin.
            if r.get("hermes:kill_switch") != "active":
                sys_log(f"Circuit breaker hari ini aktif ({today_pnl:,.0f} <= {limit:,.0f}). Trading tetap ditahan hingga reset harian.", "WARN")
            return False

        r.setex(breaker_key, 86400, "active")
        r.set("hermes:kill_switch", "active")
        sys_log(f"CIRCUIT BREAKER! Rugi harian ({today_pnl:,.2f}) menyentuh limit ({limit:,.2f}).", "WARN")
        alert_payload = {
            "type": "CIRCUIT_BREAKER_TRIGGERED",
            "reason": f"Akumulasi rugi harian menyentuh batas: {today_pnl:,.2f} (limit {limit:,.2f}). "
                      f"Kill-switch terpasang; resume manual akan tetap dicek terhadap batas ini sampai pergantian hari UTC."
        }
        r.publish("hermes:alerts", json.dumps(alert_payload))
        return False

    return True

def calculate_dynamic_volume(price: float) -> float:
    acc = broker.get_account_balance(TRADING_SYMBOL)
    available_cash = acc.get("cash_balance", 0.0)
    allocated_cash = available_cash * MAX_CAPITAL_RISK_PER_TRADE
    # Biaya taker + slippage harus masuk perhitungan agar dana tidak "habis" karena fee.
    alloc_factor = 1.0 - ROUNDTRIP_COST_PCT
    if IS_IDR_PAIR and available_cash >= 15000.0:
        allocated_cash = max(allocated_cash, min(available_cash * 0.95 * alloc_factor, 15000.0))
    if price <= 0:
        return 0.0002
    raw_volume = allocated_cash / price
    min_volume = float(os.getenv("MIN_TRADE_VOLUME", "0.0001"))
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
        
        alert_payload = {
            "type": "POSITION_CLOSED",
            "ticket": ticket,
            "pair": pair,
            "reason": reason,
            "entry_price": buy_p,
            "close_price": realized_close,
            "pnl": res["pnl"],
            "fee": res.get("fee", 0.0),
            "balance": res["remaining_balance"]
        }
        r.publish("hermes:alerts", json.dumps(alert_payload))
        check_risk_circuit_breaker()
        if reason == "STOP_LOSS_ATR":
            r.setex("hermes:sl_cooldown", 1800, "active")
            sys_log("COOLDOWN AKTIF: Jeda beli 30 menit pasca-Stop Loss murni untuk menghindari whipsaw.", "WARN")
        sys_log(f"Posisi {ticket} ditutup ({reason}) @ {realized_close:,.0f} | Net PnL: {res['pnl']:,.0f}")
    except BrokerExecutionError as e:
        # Order keluar gagal/tidak terisi: posisi TETAP OPEN di DB.
        # Dulu posisi ditandai CLOSED dengan harga mid tanpa ada jual sungguhan,
        # sehingga PnL dilaporkan padahal aset masih dipegang.
        sys_log(f"Gagal menutup posisi {ticket} ({reason}): {e}. Posisi dibiarkan OPEN, akan dicoba lagi.", "ERROR", notify_telegram=True)
        r.publish("hermes:alerts", json.dumps({
            "type": "SYSTEM_ERROR",
            "level": "ERROR",
            "message": f"Close {ticket} gagal ({reason}): {e}. Posisi masih OPEN."
        }))
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

        # Arming hanya ketika keuntungan sudah melewati biaya bolak-balik + jarak trailing.
        # Dengan ROUNDTRIP_COST_PCT 0.74% dan jarak 0.40%, SL terkunci minimal +0.20% neto.
        # Nilai lama (arm 0.65%, jarak 0.18%) mengunci -0.27% neto: selalu rugi.
        arm_threshold = max(BREAKEVEN_THRESHOLD_PCT, ROUNDTRIP_COST_PCT + TRAILING_DISTANCE_PCT)

        if gain_from_entry >= arm_threshold:
            proposed_sl = round(new_high * (1.0 - TRAILING_DISTANCE_PCT), 0 if IS_IDR_PAIR else 2)
            if proposed_sl > sl:
                update_trailing_stop(ticket, proposed_sl, new_high)
                r.publish("hermes:alerts", json.dumps({
                    "type": "TRAILING_SL_UPDATED",
                    "ticket": ticket,
                    "new_sl": proposed_sl,
                    "highest": new_high,
                    "entry": buy_p,
                    "net_locked_pct": round(gain_from_entry - TRAILING_DISTANCE_PCT - ROUNDTRIP_COST_PCT, 5)
                }))
                sys_log(f"Trailing SL #{ticket} naik ke {proposed_sl:,.0f} (net terkunci {(gain_from_entry - TRAILING_DISTANCE_PCT - ROUNDTRIP_COST_PCT)*100:.2f}%)")
                sl = proposed_sl
        elif new_high > highest_p:
            update_trailing_stop(ticket, sl, new_high)

        triggered = None
        if tp and current_price >= tp:
            triggered = "TAKE_PROFIT_ATR"
        elif sl and current_price <= sl:
            triggered = "TRAILING_STOP_LOSS" if sl >= buy_p else "STOP_LOSS_ATR"

        if triggered:
            if "STOP_LOSS" in triggered:
                sl_hits = int(r.get(f"hermes:sl_hits:{ticket}") or 0) + 1
                r.setex(f"hermes:sl_hits:{ticket}", 15, str(sl_hits))
                if sl_hits < 2:
                    sys_log(f" Tahan SL #{ticket} (Tick 1/2 @ {current_price:,.0f} <= {sl:,.0f}) - verifikasi kedipan orderbook...", "INFO")
                    continue
            close_single_position(pos, current_price, triggered)

def dispatch_trade_proposal(proposal: dict):
    if r.get("hermes:kill_switch") == "active":
        return

    ttl = r.ttl("hermes:sl_cooldown")
    if ttl and ttl > 0:
        sys_log(f"Sinyal BUY dilewati: Masih dalam masa Cooldown pasca-SL ({ttl // 60} menit tersisa).", "INFO")
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
    sys_log(f"Sinyal BUY terdeteksi @ {proposal.get('trigger_price'):,.0f} (Vol: {proposal.get('volume')}). Meminta validasi worker...")

    start_wait = time.time()
    while time.time() - start_wait < 5:
        message = pubsub.get_message(timeout=1.0)
        if message and message["type"] == "message":
            res = json.loads(message["data"])
            if res.get("status") == "APPROVED":
                try:
                    # Harga referensi harus harga pasar terkini, bukan harga pemicu sinyal.
                    # Sinyal bisa berumur beberapa detik; slippage dihitung dari pasar live.
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
                    record_audit(trade_id, proposal["pair"], "APPROVED", "Syariah Compliant (Spot 1:1)")

                    meta_info = proposal.get("meta", {})
                    net_rr = meta_info.get("net_rr")
                    sys_log(f"ORDER FILLED: #{exec_res['ticket']} @ {exec_res['fill_price']:,.0f} | TP: {take_profit:,.0f} | SL: {stop_loss:,.0f} | RSI: {meta_info.get('rsi')} | Net R:R {net_rr}")

                    alert_payload = {
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
                        "net_rr": net_rr
                    }
                    r.publish("hermes:alerts", json.dumps(alert_payload))
                except BrokerExecutionError as b_err:
                    sys_log(f"Gagal eksekusi broker: {b_err}", "ERROR", notify_telegram=True)
                except Exception as ex_err:
                    sys_log(f"Error tak terduga saat order: {ex_err}", "ERROR", notify_telegram=True)
            else:
                reason = res.get("reason", "Tidak diketahui")
                record_audit(trade_id, proposal["pair"], "REJECTED", reason)
                sys_log(f"Order ditolak Worker: {reason}", "WARN")
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
        ema_f = strategy.calculate_ema(strategy.fast_period, live_price=price) if strategy.prices else price
        ema_s = strategy.calculate_ema(strategy.slow_period, live_price=price) if strategy.prices else price
        rsi_v = strategy.calculate_rsi(live_price=price) if strategy.prices else 50.0
        atr_v = strategy.calculate_atr() if strategy.prices else 0.0
        fib_v = strategy.calculate_fib_pullback(live_price=price)
        now_wib = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S WIB")
        telemetry = {
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
        }
        r.set("hermes:telemetry", json.dumps(telemetry))
    except Exception:
        pass

def start_automated_loop():
    global last_known_price
    pair = TRADING_SYMBOL
    sys_log(f"Strategy Engine (EMA 5/13 + RSI + Fib Pullback Guard) Aktif pada pair: {pair}")

    t_manual = threading.Thread(target=listen_manual_close_events, daemon=True)
    t_manual.start()

    sys_log(f"Mengumpulkan 15 sampel data harga awal {pair} dari {os.getenv('EXCHANGE_ID', 'indodax').upper()}...")
    hist_candles = MarketDataFeed.get_recent_candles(pair, limit=20)
    if len(hist_candles) >= 15:
        for hc in hist_candles:
            strategy.add_price(hc, force_sample=True)
        price = MarketDataFeed.get_spot_price(pair)
        strategy.add_price(price, force_sample=True)
        last_known_price = price
        update_telemetry(pair, price)
    else:
        for i in range(15):
            try:
                price = MarketDataFeed.get_spot_price(pair)
                strategy.add_price(price, force_sample=True)
                last_known_price = price
                update_telemetry(pair, price)
            except Exception as e:
                sys_log(f"Gagal ambil sampel awal ke-{i+1}: {e}", "WARN")
            time.sleep(1)

    sys_log(f"Monitoring pasar aktif! Mid-Price terkini {pair}: Rp {last_known_price:,.0f}")

    tick_counter = 0
    while True:
        try:
            current_price = MarketDataFeed.get_spot_price(pair)
            last_known_price = current_price
            process_trailing_and_exits(pair, current_price)
            
            open_positions = get_open_positions()
            if len(open_positions) < MAX_CONCURRENT_POSITIONS:
                proposal = strategy.analyze(pair, current_price)
                update_telemetry(pair, current_price)
                if proposal:
                    if proposal.get("action") == "SKIPPED":
                        meta = proposal.get("meta", {})
                        reason_str = proposal.get("reason", "-")
                        sys_log(f"RADAR: Crossover EMA5/13 @ {current_price:,.0f} ditahan ({reason_str})", "INFO")
                        r.publish("hermes:alerts", json.dumps({
                            "type": "SIGNAL_SKIPPED",
                            "pair": pair,
                            "price": current_price,
                            "reason": reason_str,
                            "rsi": meta.get("rsi"),
                            "ema_fast": meta.get("ema_fast"),
                            "ema_slow": meta.get("ema_slow")
                        }))
                    else:
                        dynamic_vol = calculate_dynamic_volume(current_price)
                        proposal["volume"] = dynamic_vol
                        dispatch_trade_proposal(proposal)
                        time.sleep(15)
            else:
                strategy.add_price(current_price, force_sample=False)
                update_telemetry(pair, current_price)

            tick_counter += 1
            if tick_counter % 225 == 0:
                ema_f = strategy.calculate_ema(strategy.fast_period)
                ema_s = strategy.calculate_ema(strategy.slow_period)
                rsi_v = strategy.calculate_rsi()
                sys_log(f"Heartbeat OK | Harga: {current_price:,.0f} | EMA5: {ema_f:,.0f} | EMA13: {ema_s:,.0f} | RSI: {rsi_v:.1f}")

        except Exception as e:
            sys_log(f"Error pada loop utama: {e}", "ERROR", notify_telegram=True)
            
        time.sleep(4)

if __name__ == "__main__":
    start_automated_loop()
