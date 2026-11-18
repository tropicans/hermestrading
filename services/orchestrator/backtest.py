# -*- coding: utf-8 -*-
"""Backtester HERMES — menguji aturan entry/exit produksi pada candle historis.

Tujuan: menjawab "apakah strategi ini menguntungkan?" tanpa menunggu berbulan-bulan
trade live. Backtester memakai ulang `EMAMomentumStrategy.evaluate_signal` sehingga
aturan entry yang diuji identik dengan yang berjalan di orchestrator.

Pemakaian:
    python backtest.py --days 90
    python backtest.py --days 180 --tf 5 --pair PAXG/IDR
    python backtest.py --days 90 --no-trailing

Asumsi konservatif:
- Fee taker 0.22% per leg, slippage pad 0.05% per leg (sama dengan produksi).
- Drill-down intrabar: bila SL dan TP tersentuh dalam candle yang sama, SL dianggap
  kena lebih dulu (worst case).
- Hanya posisi long, satu posisi pada satu waktu (sesuai MAX_CONCURRENT_POSITIONS=1).
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

from strategy_engine import EMAMomentumStrategy  # noqa: E402

INDODAX_HISTORY = "https://indodax.com/tradingview/history_v2"
DEFAULT_CACHE = os.path.join(HERE, "data", "candles_cache.json")


# ---------------------------------------------------------------- data loading

def fetch_candles(pair: str, timeframe_min: int, start_ts: int, end_ts: int) -> List[dict]:
    """Ambil candle OHLC dari Indodax chart API, dipaginasi per 15 hari."""
    base, quote = pair.upper().split("/") if "/" in pair else (pair[:-3], pair[-3:])
    symbol = f"{base}{quote}"
    out: List[dict] = []
    chunk = 15 * 24 * 3600
    cursor = start_ts
    while cursor < end_ts:
        upper = min(cursor + chunk, end_ts)
        url = (f"{INDODAX_HISTORY}?symbol={symbol}&tf={timeframe_min}"
               f"&from={cursor}&to={upper}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            print(f"[backtest] Lewati blok {cursor}-{upper}: {exc}")
            data = []
        if isinstance(data, list):
            out.extend(data)
        cursor = upper + (timeframe_min * 60)
        time.sleep(0.4)

    seen = set()
    candles = []
    for c in out:
        t = c.get("Time")
        if t in seen:
            continue
        seen.add(t)
        o, h, l, cl = (float(c.get(k) or 0) for k in ("Open", "High", "Low", "Close"))
        if o <= 0 or h <= 0 or l <= 0 or cl <= 0:
            continue
        candles.append({"t": t, "o": o, "h": h, "l": l, "c": cl})
    candles.sort(key=lambda x: x["t"])
    return candles


def save_cache(candles: List[dict], path: str) -> None:
    """Simpan candle ke JSON agar bisa dipindah ke mesin lain (mis. dari VPS ke lokal)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"candles": candles}, fh)
    print(f"[backtest] Cache disimpan: {path} ({len(candles)} candle)")


def load_cache(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload.get("candles", payload if isinstance(payload, list) else [])
    candles = [{**c, "t": int(c["t"])} for c in rows]
    candles.sort(key=lambda x: x["t"])
    return candles


# ---------------------------------------------------------------- simulation

@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    volume: float
    tp: float
    sl: float
    highest: float
    exit_ts: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0
    trailing_armed: bool = False


@dataclass
class Result:
    trades: List[Trade] = field(default_factory=list)
    skipped: int = 0
    signals: int = 0
    open_at_end: Optional[Trade] = None


def run_backtest(candles: List[dict], pair: str, *, cost_pct: float, capital: float,
                 use_trailing: bool, trailing_arm: float, trailing_gap: float,
                 min_net_rr: float, max_spread: float) -> Result:
    os.environ.setdefault("ROUNDTRIP_COST_PCT", str(cost_pct))
    os.environ.setdefault("MIN_NET_RR", str(min_net_rr))
    os.environ.setdefault("MAX_SPREAD_PCT", str(max_spread))

    strategy = EMAMomentumStrategy(fast_period=5, slow_period=13, rsi_period=14, atr_period=14)
    res = Result()
    open_trade: Optional[Trade] = None
    # Biaya per leg dipakai saat entry; fee + slippage dihitung di akhir juga.
    fee_per_leg = cost_pct / 2.0
    warmup = max(strategy.slow_period, strategy.rsi_period, strategy.atr_period) + 5
    window = max(100, warmup + 40)

    for idx, candle in enumerate(candles):
        price = candle["c"]

        if open_trade is not None:
            tr = open_trade
            # --- trailing (intrabar, pakai high) ---
            if use_trailing:
                tr.highest = max(tr.highest, candle["h"])
                gain = (tr.highest - tr.entry_price) / tr.entry_price
                if gain >= trailing_arm:
                    proposed = tr.highest * (1.0 - trailing_gap)
                    if proposed > tr.sl:
                        tr.sl = proposed
                        tr.trailing_armed = True

            # --- urutan eksekusi dalam candle: SL lebih dulu (konservatif) ---
            hit_sl = candle["l"] <= tr.sl
            hit_tp = candle["h"] >= tr.tp

            exit_price = None
            reason = None
            if hit_sl:
                exit_price, reason = tr.sl, ("TRAILING_STOP_LOSS" if tr.trailing_armed else "STOP_LOSS_ATR")
            elif hit_tp:
                exit_price, reason = tr.tp, "TAKE_PROFIT_ATR"

            if exit_price is not None:
                gross = (exit_price - tr.entry_price) * tr.volume
                fees = (tr.entry_price + exit_price) * tr.volume * fee_per_leg
                tr.exit_ts = candle["t"]
                tr.exit_price = exit_price
                tr.exit_reason = reason
                tr.pnl = gross - fees
                res.trades.append(tr)
                open_trade = None

        # --- sinyal entry: hanya bila tidak ada posisi terbuka ---
        # ATR dan Fib butuh candle nyata, jadi jendela digeser per bar.
        if idx + 1 < warmup:
            continue
        window_candles = candles[max(0, idx + 1 - window): idx + 1]
        strategy.ingest_ohlc(window_candles)
        strategy._last_sample_ts = 0.0  # paksa sampel baru tiap bar backtest

        if open_trade is None:
            proposal = strategy.evaluate_signal(pair, price, spread_pct=0.0005)
            if proposal:
                res.signals += 1
                if proposal.get("action") == "SKIPPED":
                    res.skipped += 1
                else:
                    volume = max(0.0001, (capital * 0.95) / price)
                    open_trade = Trade(
                        entry_ts=candle["t"],
                        entry_price=price,
                        volume=volume,
                        tp=proposal["take_profit"],
                        sl=proposal["stop_loss"],
                        highest=price,
                    )

    if open_trade is not None:
        # Posisi masih terbuka saat data habis: catat apa adanya, jangan dihitung sebagai
        # trade selesai (PnL belum terealisasi).
        res.open_at_end = open_trade

    return res


# ---------------------------------------------------------------- reporting

def summarize(res: Result) -> dict:
    trades = res.trades
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "expectancy": 0.0, "total_pnl": 0.0, "profit_factor": 0.0,
                "max_drawdown": 0.0, "breakeven_wr": 0.0, "signals": res.signals,
                "open_at_end": ({**res.open_at_end.__dict__} if res.open_at_end else None)}

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    wr = len(wins) / n
    breakeven = (1.0 / (1.0 + (avg_win / avg_loss))) if avg_loss > 0 and avg_win > 0 else 1.0

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": wr,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": sum(t.pnl for t in trades) / n,
        "total_pnl": sum(t.pnl for t in trades),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_drawdown": max_dd,
        "breakeven_wr": breakeven,
        "signals": res.signals,
        "open_at_end": ({**res.open_at_end.__dict__} if res.open_at_end else None),
    }


def print_report(stats: dict, by_reason: dict, pair: str, days: int, tf: int) -> None:
    print()
    print("=" * 62)
    print(f"  BACKTEST HERMES — {pair} | TF {tf}m | {days} hari")
    print("=" * 62)
    print(f"  Sinyal crossover terdeteksi  : {stats['signals']}")
    print(f"  Trade dieksekusi             : {stats['trades']}")
    print(f"  Menang / Kalah               : {stats['wins']} / {stats['losses']}")
    print(f"  Win rate                     : {stats['win_rate']*100:.1f}%")
    print(f"  Breakeven win rate           : {stats['breakeven_wr']*100:.1f}%")
    verdict = "LOLOS" if stats["win_rate"] > stats["breakeven_wr"] else "GAGAL"
    print(f"  Putusan                      : {verdict}")
    print("-" * 62)
    print(f"  Rata-rata profit             : Rp {stats.get('avg_win', 0):,.0f}")
    print(f"  Rata-rata rugi               : Rp {stats.get('avg_loss', 0):,.0f}")
    print(f"  Expectancy per trade         : Rp {stats['expectancy']:,.0f}")
    print(f"  Total PnL                    : Rp {stats['total_pnl']:,.0f}")
    pf = stats["profit_factor"]
    print(f"  Profit factor                : {pf:.2f}" if pf != float("inf") else "  Profit factor                : inf")
    print(f"  Max drawdown                 : Rp {stats['max_drawdown']:,.0f}")
    print("-" * 62)
    print("  Distribusi exit:")
    for reason, count in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"    {reason:<22} {count}")
    if stats.get("open_at_end"):
        ot = stats["open_at_end"]
        print(f"  Posisi masih terbuka di akhir data: entry Rp {ot['entry_price']:,.0f} "
              f"(TP Rp {ot['tp']:,.0f} / SL Rp {ot['sl']:,.0f})")
    print("=" * 62)
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest strategi HERMES pada candle Indodax")
    ap.add_argument("--pair", default=os.getenv("TRADING_SYMBOL", "PAXG/IDR"))
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--tf", type=int, default=1, help="timeframe menit (1, 5, 15, 60)")
    ap.add_argument("--capital", type=float, default=15_000.0, help="modal per trade (IDR)")
    ap.add_argument("--cost-pct", type=float, default=float(os.getenv("ROUNDTRIP_COST_PCT", "0.0074")))
    ap.add_argument("--trailing-arm", type=float, default=float(os.getenv("TRAILING_ARM_PCT", "0.0120")))
    ap.add_argument("--trailing-gap", type=float, default=float(os.getenv("TRAILING_DISTANCE_PCT", "0.0040")))
    ap.add_argument("--no-trailing", action="store_true")
    ap.add_argument("--min-net-rr", type=float, default=float(os.getenv("MIN_NET_RR", "1.5")))
    ap.add_argument("--max-spread", type=float, default=0.0005,
                    help="spread yang diasumsikan saat backtest (fraksi harga)")
    ap.add_argument("--csv", help="simpan daftar trade ke file CSV")
    ap.add_argument("--fetch-cache", metavar="PATH", nargs="?", const=DEFAULT_CACHE,
                    help="unduh candle lalu simpan ke JSON (untuk dipindah ke mesin lain)")
    ap.add_argument("--cache", metavar="PATH",
                    help="pakai candle dari file JSON, tanpa jaringan")
    args = ap.parse_args()

    if args.cache:
        print(f"[backtest] Memuat candle dari {args.cache} ...")
        candles = load_cache(args.cache)
    else:
        end_ts = int(time.time())
        start_ts = end_ts - (args.days * 86400)
        print(f"[backtest] Mengambil {args.days} hari candle {args.pair} TF {args.tf}m ...")
        candles = fetch_candles(args.pair, args.tf, start_ts, end_ts)

    if not candles:
        print("[backtest] Tidak ada candle. Periksa pair/timeframe atau koneksi.")
        return 1
    print(f"[backtest] {len(candles)} candle dimuat.")

    if args.fetch_cache:
        save_cache(candles, args.fetch_cache)
        if not args.cache:
            # Mode unduh saja: jangan lanjut backtest.
            return 0

    cutoff = int(time.time()) - (args.days * 86400)
    candles = [c for c in candles if c["t"] >= cutoff]
    if not candles:
        print(f"[backtest] Tidak ada candle dalam {args.days} hari terakhir.")
        return 1

    res = run_backtest(
        candles, args.pair,
        cost_pct=args.cost_pct,
        capital=args.capital,
        use_trailing=not args.no_trailing,
        trailing_arm=args.trailing_arm,
        trailing_gap=args.trailing_gap,
        min_net_rr=args.min_net_rr,
        max_spread=args.max_spread,
    )

    stats = summarize(res)
    by_reason = {}
    for t in res.trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    print_report(stats, by_reason, args.pair, args.days, args.tf)

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["entry_ts", "entry_price", "tp", "sl", "exit_ts", "exit_price", "exit_reason", "pnl"])
            for t in res.trades:
                w.writerow([t.entry_ts, t.entry_price, t.tp, t.sl, t.exit_ts, t.exit_price, t.exit_reason, round(t.pnl, 2)])
        print(f"[backtest] Trade disimpan ke {args.csv}")

    return 0 if stats["trades"] else 2


if __name__ == "__main__":
    sys.exit(main())
