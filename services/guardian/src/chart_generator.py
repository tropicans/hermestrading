# -*- coding: utf-8 -*-
import io
import sqlite3
import datetime
import matplotlib
matplotlib.use('Agg')  # Headless mode tanpa GUI
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

DB_PATH = "/app/data/trading.db"

def generate_paxg_chart(limit=30) -> io.BytesIO:
    """Mengambil riwayat transaksi/harga dari database dan merender chart visual ke buffer byte."""
    conn = sqlite3.connect(DB_PATH)
    try:
        # Ambil data transaksi orders terbaru
        query = """
            SELECT fill_price, created_at, status, take_profit, stop_loss
            FROM orders
            ORDER BY id DESC
            LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=(limit,))
    finally:
        conn.close()

    if df.empty or len(df) < 2:
        # Buat dummy/current visualisasi jika order masih sangat sedikit
        base_price = df['fill_price'].iloc[0] if not df.empty else 2685.0
        now = datetime.datetime.now(datetime.timezone.utc)
        times = [now - datetime.timedelta(minutes=i*5) for i in reversed(range(10))]
        prices = [base_price + (i * 0.15 * (-1 if i % 2 == 0 else 1)) for i in range(10)]
        df = pd.DataFrame({'created_at': times, 'fill_price': prices})
    else:
        df['created_at'] = pd.to_datetime(df['created_at'])
        df = df.sort_values('created_at').reset_index(drop=True)

    # Hitung EMA Fast (3) & Slow (5)
    df['ema_fast'] = df['fill_price'].ewm(span=3, adjust=False).mean()
    df['ema_slow'] = df['fill_price'].ewm(span=5, adjust=False).mean()

    # Setup Tema Dark Mode Trading
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150)
    fig.patch.set_facecolor('#131722')
    ax.set_facecolor('#131722')

    # Plot Garis Harga & EMA
    ax.plot(df['created_at'], df['fill_price'], label='PAXG/USD Spot', color='#d1d4dc', linewidth=1.5, marker='o', markersize=4)
    ax.plot(df['created_at'], df['ema_fast'], label='EMA Fast (3)', color='#2962ff', linewidth=1.8, linestyle='--')
    ax.plot(df['created_at'], df['ema_slow'], label='EMA Slow (5)', color='#ff9800', linewidth=1.8, linestyle=':')

    # Anotasi Harga Terakhir
    last_price = df['fill_price'].iloc[-1]
    last_time = df['created_at'].iloc[-1]
    ax.scatter([last_time], [last_price], color='#00e676', s=70, zorder=5)
    ax.annotate(f"\${last_price:,.2f}", (last_time, last_price),
                textcoords="offset points", xytext=(0, 10), ha='center',
                color='#00e676', fontweight='bold', fontsize=9,
                bbox=dict(boxstyle="round,pad=0.2", fc="#1e222d", ec="#00e676", lw=1))

    # Format Tampilan Axis & Grid
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.grid(True, linestyle=':', alpha=0.3, color='#434651')
    ax.set_title("HERMES ENGINE - PAXG/USD SPOT (EMA MOMENTUM)", fontsize=11, color='#e0e3eb', fontweight='bold', pad=12)
    ax.set_ylabel("Price (USD)", fontsize=9, color='#787b86')
    ax.legend(loc='upper left', frameon=True, facecolor='#1e222d', edgecolor='#434651', fontsize=8)

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', facecolor=fig.get_facecolor(), edgecolor='none')
    buf.seek(0)
    plt.close(fig)
    return buf
