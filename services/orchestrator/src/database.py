import sqlite3
import os
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", "/app/data/trading.db")

def get_connection():
    path_obj = Path(DB_PATH)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT UNIQUE,
            ticket TEXT UNIQUE,
            pair TEXT,
            action TEXT,
            contract_type TEXT,
            volume REAL,
            fill_price REAL,
            take_profit REAL,
            stop_loss REAL,
            highest_price REAL DEFAULT NULL,
            total_cost REAL,
            remaining_balance REAL,
            close_price REAL DEFAULT NULL,
            pnl REAL DEFAULT 0.0,
            status TEXT DEFAULT 'OPEN',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP DEFAULT NULL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT,
            pair TEXT,
            status TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    cursor.execute("PRAGMA table_info(orders)")
    existing_cols = [row[1] for row in cursor.fetchall()]
    
    required_cols = {
        "take_profit": "REAL",
        "stop_loss": "REAL",
        "highest_price": "REAL DEFAULT NULL",
        "close_price": "REAL DEFAULT NULL",
        "pnl": "REAL DEFAULT 0.0",
        "closed_at": "TIMESTAMP DEFAULT NULL"
    }

    for col, col_type in required_cols.items():
        if col not in existing_cols:
            cursor.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type}")

    conn.commit()
    conn.close()
    print(f"[database] SQLite aktif dan skema termigrasi siap di: {DB_PATH}")

def record_order(trade_id: str, ticket: str, pair: str, action: str, contract_type: str, 
                 volume: float, fill_price: float, total_cost: float, remaining_balance: float, 
                 tp: float, sl: float, status: str = "OPEN"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO orders 
        (trade_id, ticket, pair, action, contract_type, volume, fill_price, take_profit, stop_loss, highest_price, total_cost, remaining_balance, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (trade_id, ticket, pair, action, contract_type, volume, fill_price, tp, sl, fill_price, total_cost, remaining_balance, status))
    conn.commit()
    conn.close()

def get_open_positions():
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE status = 'OPEN'")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def update_trailing_stop(ticket: str, new_sl: float, new_high: float):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE orders 
        SET stop_loss = ?, highest_price = ?
        WHERE ticket = ?
    """, (new_sl, new_high, ticket))
    conn.commit()
    conn.close()

def close_position_db(ticket: str, close_price: float, pnl: float, remaining_balance: float):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE orders 
        SET status = 'CLOSED', close_price = ?, pnl = ?, remaining_balance = ?, closed_at = CURRENT_TIMESTAMP
        WHERE ticket = ?
    """, (close_price, pnl, remaining_balance, ticket))
    conn.commit()
    conn.close()

def get_today_realized_pnl() -> float:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COALESCE(SUM(pnl), 0.0) 
        FROM orders 
        WHERE status = 'CLOSED' AND DATE(closed_at) = DATE('now')
    """)
    result = cursor.fetchone()[0]
    conn.close()
    return float(result)

def record_audit(trade_id: str, pair: str, status: str, reason: str = ""):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO audit_logs (trade_id, pair, status, reason)
        VALUES (?, ?, ?, ?)
    """, (trade_id, pair, status, reason))
    conn.commit()
    conn.close()
