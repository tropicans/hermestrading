# -*- coding: utf-8 -*-
import os, sys, time, json, uuid, datetime, threading, redis
from broker_gateway import get_broker_client, BrokerExecutionError
from strategy_engine import MarketDataFeed, EMAMomentumStrategy
from database import init_db, record_order, record_audit, get_open_positions, close_position_db, get_today_realized_pnl

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

PRIMARY_SYMBOL = os.getenv("TRADING_SYMBOL", "BTC/IDR").upper()
TRADING_SYMBOLS = [s.strip().upper() for s in os.getenv("TRADING_SYMBOLS", PRIMARY_SYMBOL).split(",") if s.strip()] or [PRIMARY_SYMBOL]
IS_IDR_PAIR = any("IDR" in s for s in TRADING_SYMBOLS)
DAILY_MAX_DRAWDOWN = float(os.getenv("DAILY_MAX_DRAWDOWN", "-2500000.0" if IS_IDR_PAIR else "-150.0"))
DAILY_DRAWDOWN_EQUITY_PCT = float(os.getenv("DAILY_DRAWDOWN_EQUITY_PCT", "0.05"))
BASE_GRID_TP_PCT = float(os.getenv("GRID_TP_PCT", "0.042"))

PAIR_PROFILES = {
    "BTC/IDR": {"step_pct": 0.045, "tp_pct": 0.042, "rsi_limit": 35.0, "default_alloc": 0.60},
    "SOL/IDR": {"step_pct": 0.055, "tp_pct": 0.052, "rsi_limit": 32.0, "default_alloc": 0.40},
    "ETH/IDR": {"step_pct": 0.050, "tp_pct": 0.046, "rsi_limit": 34.0, "default_alloc": 0.40},
}

def parse_pair_allocations(symbols: list) -> dict:
    raw = os.getenv("PAIR_ALLOCATIONS", "").strip()
    allocs = {}
    for item in raw.split(","):
        if ":" in item:
            k, v = item.split(":", 1)
            try: allocs[k.strip().upper()] = float(v.strip())
            except ValueError: pass
    if len(symbols) == 1 and symbols[0] not in allocs:
        return {symbols[0]: 1.0}
    for s in symbols:
        if s not in allocs:
            allocs[s] = PAIR_PROFILES.get(s, {}).get("default_alloc", round(1.0 / len(symbols), 4))
    total = sum(allocs.values())
    return {s: (allocs[s] / total if total > 0 else 1.0 / len(symbols)) for s in symbols}

PAIR_ALLOCATIONS = parse_pair_allocations(TRADING_SYMBOLS)
last_known_prices = {s: (1150000000.0 if "BTC" in s else 3000000.0) for s in TRADING_SYMBOLS}
_last_error_alert_ts = 0.0

try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
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
                if level == "ERROR": _last_error_alert_ts = now_ts
                r.publish("hermes:alerts", json.dumps({"type": "SYSTEM_ERROR", "level": level, "message": msg, "timestamp": f"{now_wib} WIB"}))
    except Exception: pass

init_db()
broker = get_broker_client()
strategies = {
    sym: EMAMomentumStrategy(
        fast_period=20, slow_period=50, trend_period=200, rsi_period=14, atr_period=14,
        step_pct=PAIR_PROFILES.get(sym, {}).get("step_pct", 0.045),
        tp_pct=PAIR_PROFILES.get(sym, {}).get("tp_pct", BASE_GRID_TP_PCT),
        rsi_limit=PAIR_PROFILES.get(sym, {}).get("rsi_limit", 35.0)
    ) for sym in TRADING_SYMBOLS
}

def get_current_equity() -> float:
    try:
        acc = broker.get_account_balance(TRADING_SYMBOLS[0])
        cash = float(acc.get("cash_balance", 0.0) or 0.0)
        asset_val = sum(float(p.get("volume", 0.0)) * last_known_prices.get(p.get("pair"), float(p.get("fill_price", 0.0))) for p in get_open_positions())
        return cash + asset_val
    except Exception: return 0.0

def effective_daily_limit() -> float:
    if DAILY_DRAWDOWN_EQUITY_PCT > 0:
        eq = get_current_equity()
        if eq > 0: return -abs(eq * DAILY_DRAWDOWN_EQUITY_PCT)
    return DAILY_MAX_DRAWDOWN

def check_risk_circuit_breaker() -> bool:
    today_pnl = get_today_realized_pnl()
    limit = effective_daily_limit()
    if today_pnl <= limit:
        breaker_key = f"hermes:circuit_breaker:{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}"
        if r.get(breaker_key) == "active": return False
        r.setex(breaker_key, 86400, "active")
        r.set("hermes:kill_switch", "active")
        sys_log(f"CIRCUIT BREAKER! Rugi harian ({today_pnl:,.2f}) menyentuh limit ({limit:,.2f}).", "WARN")
        r.publish("hermes:alerts", json.dumps({"type": "CIRCUIT_BREAKER_TRIGGERED", "reason": f"Rugi harian menyentuh batas: {today_pnl:,.2f}"}))
        return False
    return True

def calculate_grid_volume(pair: str, price: float, layer_weight: float = 0.25) -> float:
    acc = broker.get_account_balance(pair)
    available_cash = float(acc.get("cash_balance", 0.0) or 0.0)
    all_positions = get_open_positions()
    total_basis = available_cash + sum(float(p.get("total_cost", 0.0) or 0.0) for p in all_positions)
    pair_budget = total_basis * PAIR_ALLOCATIONS.get(pair, 1.0 / max(1, len(TRADING_SYMBOLS)))
    pair_locked = sum(float(p.get("total_cost", 0.0) or 0.0) for p in all_positions if p.get("pair") == pair)
    pair_rem = max(0.0, pair_budget - pair_locked)
    allocated_cash = min(available_cash * 0.98, pair_rem * 0.98, pair_budget * layer_weight)
    if "IDR" in pair and available_cash >= 20000.0 and pair_rem >= 20000.0:
        allocated_cash = max(allocated_cash, min(available_cash * 0.95, 20000.0))
    if price <= 0 or allocated_cash <= 0: return 0.0
    min_vol = float(os.getenv("MIN_TRADE_VOLUME", "0.00005")) if "BTC" in pair else 0.001
    return max(min_vol, round(allocated_cash / price, 8))

def close_single_position(pos: dict, current_price: float, reason: str = "MANUAL_CLOSE"):
    ticket, buy_p, vol, pair = pos["ticket"], float(pos["fill_price"]), float(pos["volume"]), pos["pair"]
    try:
        res = broker.close_spot_position(ticket, vol, buy_p, current_price, pair=pair)
        realized_close = res.get("fill_price", current_price)
        close_position_db(ticket, realized_close, res["pnl"], res["remaining_balance"], exit_reason=reason)
        r.publish("hermes:alerts", json.dumps({
            "type": "POSITION_CLOSED", "ticket": ticket, "pair": pair, "reason": reason,
            "entry_price": buy_p, "close_price": realized_close, "pnl": res["pnl"],
            "fee": res.get("fee", 0.0), "balance": res["remaining_balance"]
        }))
        check_risk_circuit_breaker()
        sys_log(f"[{pair}] Posisi {ticket} ditutup ({reason}) @ {realized_close:,.0f} | Net PnL: {res['pnl']:,.0f}")
    except Exception as e:
        sys_log(f"[{pair}] Gagal menutup posisi {ticket}: {e}", "ERROR", notify_telegram=True)

def listen_manual_close_events():
    sub_r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    pubsub = sub_r.pubsub()
    pubsub.subscribe("hermes:manual_close")
    for msg in pubsub.listen():
        if msg["type"] == "message":
            try:
                target = json.loads(msg["data"]).get("ticket")
                for p in get_open_positions():
                    if target == "ALL" or p["ticket"] == target:
                        close_single_position(p, last_known_prices.get(p["pair"], float(p["fill_price"])), "MANUAL_CLOSE_ALL" if target == "ALL" else "MANUAL_CLOSE")
                        if target != "ALL": break
            except Exception as e:
                sys_log(f"Error manual close: {e}", "ERROR", notify_telegram=True)

def process_grid_exits(pair: str, current_price: float):
    positions = [p for p in get_open_positions() if p["pair"] == pair]
    if not positions: return
    tot_vol = sum(float(p["volume"]) for p in positions)
    if tot_vol <= 0: return
    avg_entry = sum(float(p.get("total_cost") or (float(p["fill_price"]) * float(p["volume"]))) for p in positions) / tot_vol
    weighted_tp = avg_entry * (1.0 + (strategies[pair].tp_pct if pair in strategies else BASE_GRID_TP_PCT))
    if current_price >= weighted_tp:
        sys_log(f"[{pair}] GRID TARGET TERCAPAI! {current_price:,.0f} >= Avg TP {weighted_tp:,.0f}. Menutup siklus...")
        for pos in positions: close_single_position(pos, current_price, "GRID_AVG_TAKE_PROFIT")
        return
    for pos in positions:
        if pos.get("stop_loss") and current_price <= float(pos["stop_loss"]):
            close_single_position(pos, current_price, "DISASTER_STOP_LOSS")

def dispatch_trade_proposal(proposal: dict):
    if r.get("hermes:kill_switch") == "active" or not check_risk_circuit_breaker(): return
    pair = proposal["pair"]
    trade_id = str(uuid.uuid4())[:8]
    proposal["trade_id"] = trade_id
    pubsub = r.pubsub()
    resp_ch = f"hermes:trade_result:{trade_id}"
    pubsub.subscribe(resp_ch)
    while True:
        m = pubsub.get_message(timeout=0.2)
        if m and m["type"] == "subscribe": break
    layer_num = proposal.get("layer_num", 1)
    r.rpush("hermes:trade_queue", json.dumps(proposal))
    sys_log(f"[{pair}] Sinyal GRID Lapis #{layer_num} BUY @ {proposal.get('trigger_price'):,.0f} (Vol: {proposal.get('volume')}). Validasi worker...")
    start_wait = time.time()
    while time.time() - start_wait < 5:
        message = pubsub.get_message(timeout=1.0)
        if message and message["type"] == "message":
            res = json.loads(message["data"])
            if res.get("status") == "APPROVED":
                try:
                    fill_p = MarketDataFeed.get_spot_price(pair) or proposal.get("trigger_price", last_known_prices.get(pair, 0))
                    tp, sl = proposal.get("take_profit"), proposal.get("stop_loss")
                    exec_res = broker.execute_spot_order(pair=pair, action=proposal["action"], volume=proposal["volume"], price=fill_p)
                    record_order(trade_id=trade_id, ticket=exec_res["ticket"], pair=exec_res["pair"], action=exec_res["action"],
                                 contract_type="SPOT", volume=exec_res["volume"], fill_price=exec_res["fill_price"],
                                 total_cost=exec_res["total_cost"], remaining_balance=exec_res["remaining_balance"],
                                 tp=tp, sl=sl, status="OPEN", fill_fee=exec_res.get("fee", 0.0))
                    record_audit(trade_id, pair, "APPROVED", f"Syariah Spot 1:1 ({pair} Grid Lapis #{layer_num})")
                    meta = proposal.get("meta", {})
                    sys_log(f"[{pair}] GRID LAPIS #{layer_num} FILLED: #{exec_res['ticket']} @ {exec_res['fill_price']:,.0f} | TP: {tp:,.0f} | RSI: {meta.get('rsi')}")
                    r.publish("hermes:alerts", json.dumps({
                        "type": "ORDER_EXECUTED", "trade_id": trade_id, "ticket": exec_res["ticket"], "pair": exec_res["pair"],
                        "action": exec_res["action"], "fill_price": exec_res["fill_price"], "volume": exec_res["volume"],
                        "total_cost": exec_res["total_cost"], "fee": exec_res.get("fee", 0.0), "balance": exec_res["remaining_balance"],
                        "tp": tp, "sl": sl, "net_rr": meta.get("net_rr")
                    }))
                except Exception as ex:
                    sys_log(f"[{pair}] Error saat order: {ex}", "ERROR", notify_telegram=True)
            else:
                reason = res.get("reason", "Tidak diketahui")
                record_audit(trade_id, pair, "REJECTED", reason)
                sys_log(f"[{pair}] Order Grid ditolak Worker: {reason}", "WARN")
                r.publish("hermes:alerts", json.dumps({"type": "TRADE_REJECTED", "trade_id": trade_id, "pair": pair, "reason": reason}))
            break
    pubsub.unsubscribe(resp_ch)

def update_telemetry(pair: str, price: float):
    try:
        strat = strategies[pair]
        nd = 0 if "IDR" in pair else 2
        fib_v = strat.calculate_fib_pullback(live_price=price)
        now_wib = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S WIB")
        payload = {
            "pair": pair, "price": price,
            "ema_fast": round(strat.calculate_ema(strat.fast_period) if strat.prices else price, nd),
            "ema_slow": round(strat.calculate_ema(strat.slow_period) if strat.prices else price, nd),
            "step_pct": strat.step_pct, "tp_pct": strat.tp_pct, "rsi_limit": strat.rsi_limit,
            "alloc_pct": round(PAIR_ALLOCATIONS.get(pair, 0.0) * 100, 1),
            "rsi": round(strat.calculate_rsi() if strat.prices else 50.0, 2),
            "atr": round(strat.calculate_atr() if strat.prices else 0.0, nd),
            "fib_retrace": fib_v.get("retrace_pct", 50.0), "fib_zone": fib_v.get("zone_pos_pct", 50.0),
            "swing_high": fib_v.get("swing_high", price), "swing_low": fib_v.get("swing_low", price),
            "samples": len(strat.prices), "updated_at": now_wib
        }
        r.set(f"hermes:telemetry:{pair.replace('/', '_')}", json.dumps(payload))
        if pair == TRADING_SYMBOLS[0]: r.set("hermes:telemetry", json.dumps(payload))
    except Exception: pass

def start_automated_loop():
    global last_known_prices
    alloc_summary = ", ".join([f"{s} ({PAIR_ALLOCATIONS[s]*100:.0f}%)" for s in TRADING_SYMBOLS])
    sys_log(f"HERMES v3 Multi-Pair Spot Grid Aktif! Pairs: {alloc_summary} | TF {os.getenv('TRADING_TIMEFRAME', '1h')}")
    threading.Thread(target=listen_manual_close_events, daemon=True).start()

    for pair in TRADING_SYMBOLS:
        strat = strategies[pair]
        sys_log(f"[{pair}] Memuat candle OHLC awal (Step -{strat.step_pct*100:.1f}% | TP +{strat.tp_pct*100:.1f}% | RSI < {strat.rsi_limit:.0f})...")
        hist_ohlc = MarketDataFeed.get_recent_candles_ohlc(pair, limit=220)
        if len(hist_ohlc) >= 20:
            strat.ingest_ohlc(hist_ohlc)
            price = MarketDataFeed.get_spot_price(pair)
            last_known_prices[pair] = price
            update_telemetry(pair, price)
            sys_log(f"[{pair}] Siap dengan {len(hist_ohlc)} candle OHLC | Harga: Rp {price:,.0f}")
        time.sleep(1.5)

    tick_counter = 0
    tf_sec = max(60, strategies[TRADING_SYMBOLS[0]].timeframe_min * 60)
    last_candle_slot = int(time.time() // tf_sec)

    while True:
        try:
            cur_slot = int(time.time() // tf_sec)
            is_new_bar_slot = cur_slot > last_candle_slot
            for pair in TRADING_SYMBOLS:
                strat = strategies[pair]
                try:
                    current_price = MarketDataFeed.get_spot_price(pair)
                    last_known_prices[pair] = current_price
                    process_grid_exits(pair, current_price)
                    is_new_bar = False
                    if is_new_bar_slot:
                        fresh_ohlc = MarketDataFeed.get_recent_candles_ohlc(pair, limit=220)
                        if len(fresh_ohlc) >= 20:
                            strat.ingest_ohlc(fresh_ohlc)
                            is_new_bar = True
                        else:
                            is_new_bar = strat.add_price(current_price, force_sample=True)
                    open_positions = [p for p in get_open_positions() if p["pair"] == pair]
                    if len(open_positions) < strat.max_layers:
                        proposal = strat.evaluate_signal(pair, current_price, open_positions=open_positions) if is_new_bar else None
                        update_telemetry(pair, current_price)
                        if proposal:
                            if proposal.get("action") == "SKIPPED":
                                meta = proposal.get("meta", {})
                                sys_log(f"RADAR: {proposal.get('reason', '-')} @ {current_price:,.0f}", "INFO")
                                r.publish("hermes:alerts", json.dumps({"type": "SIGNAL_SKIPPED", "pair": pair, "price": current_price, "reason": proposal.get("reason", "-"), "rsi": meta.get("rsi"), "ema_fast": meta.get("ema_fast"), "ema_slow": meta.get("ema_slow")}))
                            else:
                                vol = calculate_grid_volume(pair, current_price, layer_weight=proposal.get("layer_weight", 0.25))
                                if vol > 0:
                                    proposal["volume"] = vol
                                    dispatch_trade_proposal(proposal)
                                    time.sleep(5)
                    else:
                        update_telemetry(pair, current_price)
                except Exception as pair_err:
                    sys_log(f"[{pair}] Gangguan polling: {pair_err}", "WARN")
                time.sleep(1.5)
            if is_new_bar_slot: last_candle_slot = cur_slot
            tick_counter += 1
            if tick_counter % 150 == 0:
                summaries = [f"{p}: {last_known_prices.get(p, 0):,.0f} ({len([x for x in get_open_positions() if x['pair'] == p])}/3 | L1<{strategies[p].calculate_ema(strategies[p].slow_period)*(1-strategies[p].step_pct):,.0f} | RSI {strategies[p].calculate_rsi():.1f})" for p in TRADING_SYMBOLS]
                sys_log("Heartbeat OK | " + " || ".join(summaries))
        except Exception as e:
            sys_log(f"Error pada loop utama: {e}", "ERROR", notify_telegram=True)
        time.sleep(3)

if __name__ == "__main__":
    start_automated_loop()
