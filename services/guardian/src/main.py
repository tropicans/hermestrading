# -*- coding: utf-8 -*-
import os, sys, time, json, sqlite3, datetime, threading
import telebot
from telebot import apihelper, types
import redis
from chart_generator import generate_paxg_chart

sys.path.insert(0, '/app/services/orchestrator/src')
try:
    from broker_gateway import get_broker_client
    _global_broker = get_broker_client()
except Exception:
    _global_broker = None

EMOJI_SHIELD = "\U0001F6E1\uFE0F"
EMOJI_CHART = "\U0001F4CA"
EMOJI_UP_GRAPH = "\U0001F4C8"
EMOJI_CLIPBOARD = "\U0001F4CB"
EMOJI_SCROLL = "\U0001F4DC"
EMOJI_DISK = "\U0001F4BE"
EMOJI_SIREN = "\U0001F6A8"
EMOJI_GREEN_CIRCLE = "\U0001F7E2"
EMOJI_RED_CIRCLE = "\U0001F534"
EMOJI_ROCKET = "\U0001F680"
EMOJI_MONEY_BAG = "\U0001F4B0"
EMOJI_OCTAGON = "\U0001F6D1"
EMOJI_PACKAGE = "\U0001F4E6"
EMOJI_BULB = "\U0001F4A1"
EMOJI_LIGHTNING = "\u26A1"
EMOJI_PLAY = "\u25B6\uFE0F"
EMOJI_CALENDAR = "\U0001F4C5"
EMOJI_REFRESH = "\U0001F504"
EMOJI_CHECK = "\u2705"
EMOJI_WARNING = "\u26A0\uFE0F"
EMOJI_INFO = "\u2139\uFE0F"
EMOJI_MOON = "\U0001F319"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

PRIMARY_SYMBOL = os.getenv("TRADING_SYMBOL", "BTC/IDR").upper()
TRADING_SYMBOLS = [s.strip().upper() for s in os.getenv("TRADING_SYMBOLS", PRIMARY_SYMBOL).split(",") if s.strip()] or [PRIMARY_SYMBOL]
TRADING_SYMBOL = TRADING_SYMBOLS[0]
EXCHANGE_ID = os.getenv("EXCHANGE_ID", "indodax").upper()
IS_IDR = any("IDR" in s for s in TRADING_SYMBOLS)

DB_PATH = "/app/data/trading.db"
BACKUP_DIR = "/app/backups"

def fmt_curr(val) -> str:
    try:
        v = float(val or 0.0)
    except Exception:
        v = 0.0
    return f"Rp {v:,.0f}" if IS_IDR else f"${v:,.2f}"

if not BOT_TOKEN or "ISI_" in BOT_TOKEN:
    print("[guardian] PERINGATAN: TELEGRAM_BOT_TOKEN belum disetel.")
    while True:
        time.sleep(60)

apihelper.CONNECT_TIMEOUT = 15
apihelper.READ_TIMEOUT = 25
apihelper.RETRY_ON_ERROR = True

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    r.ping()
    print("[guardian] Terhubung ke Redis event broker.")
except Exception as e:
    print(f"[guardian] Gagal konek Redis: {e}")
    sys.exit(1)

def is_admin(obj):
    if isinstance(obj, types.CallbackQuery):
        return str(obj.from_user.id) == str(ADMIN_CHAT_ID)
    elif isinstance(obj, types.Message):
        return str(obj.chat.id) == str(ADMIN_CHAT_ID)
    return False

def get_main_reply_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton(f"{EMOJI_CHART} Status Sistem"),
        types.KeyboardButton(f"{EMOJI_UP_GRAPH} Rekap PnL")
    )
    markup.add(
        types.KeyboardButton(f"{EMOJI_CLIPBOARD} Posisi Aktif"),
        types.KeyboardButton(f"{EMOJI_SCROLL} Log & Indikator")
    )
    markup.add(
        types.KeyboardButton(f"{EMOJI_CHART} Lihat Chart"),
        types.KeyboardButton(f"{EMOJI_DISK} Menu Backup")
    )
    markup.add(types.KeyboardButton(f"{EMOJI_SIREN} Emergency Kill"))
    return markup

def generate_performance_report(date_str="now"):
    if not os.path.exists(DB_PATH):
        return f"{EMOJI_WARNING} Database belum tersedia."
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT
                COUNT(*),
                SUM(CASE WHEN action = 'BUY' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'CLOSED' AND pnl > 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'CLOSED' AND pnl < 0 THEN 1 ELSE 0 END),
                COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN pnl ELSE 0 END), 0.0),
                COALESCE(SUM(total_cost), 0.0)
            FROM orders
        """)
        row = c.fetchone()
        conn.close()

        latest_balance = 0.0
        asset_lines = []
        if _global_broker:
            for idx, sym in enumerate(TRADING_SYMBOLS):
                try:
                    acc_bal = _global_broker.get_account_balance(sym)
                    if idx == 0:
                        latest_balance = float(acc_bal.get("cash_balance", 0.0))
                    a_bal = float(acc_bal.get("asset_balance", 0.0))
                    coin = sym.split('/')[0]
                    asset_lines.append(f"• Saldo {coin}: <code>{a_bal:.8f} {coin}</code>")
                except Exception:
                    pass

        if latest_balance <= 0 and os.getenv("TRADING_MODE", "PAPER").upper() != "LIVE":
            latest_balance = float(os.getenv("PAPER_INITIAL_BALANCE", 150000000.0 if IS_IDR else 10000.0))

        total_orders, _, open_pos, closed_pos, win_count, loss_count, realized_pnl, total_turnover = row
        total_orders, open_pos, closed_pos = total_orders or 0, open_pos or 0, closed_pos or 0
        win_count, loss_count = win_count or 0, loss_count or 0
        realized_pnl = round(realized_pnl or 0.0, 2)

        win_rate = (win_count / closed_pos * 100.0) if closed_pos > 0 else 0.0
        pnl_prefix = "+" if realized_pnl >= 0 else "-"
        pnl_badge = EMOJI_GREEN_CIRCLE if realized_pnl >= 0 else EMOJI_RED_CIRCLE
        mode_label = os.getenv("TRADING_MODE", "PAPER").upper()
        pairs_str = ", ".join(TRADING_SYMBOLS)
        assets_block = "\n".join(asset_lines) if asset_lines else f"• Saldo Aset: <code>0</code>"

        return (
            f"{EMOJI_UP_GRAPH} <b>HERMES PERFORMANCE RECAP ({mode_label})</b>\n"
            f"{EMOJI_CALENDAR} Tanggal: <code>{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d UTC')}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• Exchange: <code>{EXCHANGE_ID} ({pairs_str})</code>\n"
            f"• Total Order: <code>{total_orders}</code> | Terbuka: <code>{open_pos}</code> | Selesai: <code>{closed_pos}</code>\n"
            f"• Win Rate: <code>{win_rate:.1f}%</code> ({win_count}W / {loss_count}L)\n"
            f"• Perputaran Kas: <code>{fmt_curr(total_turnover)}</code>\n"
            f"• Realized PnL: {pnl_badge} <code>{pnl_prefix}{fmt_curr(abs(realized_pnl))}</code>\n"
            f"• Saldo Kas IDR: <code>{fmt_curr(latest_balance)}</code>\n"
            f"{assets_block}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Status Syariah: <code>STRICT (Spot 1:1, Zero-Swap)</code>"
        )
    except Exception as e:
        return f"{EMOJI_OCTAGON} Gagal kalkulasi database: {e}"

def build_logs_and_telemetry_text():
    now_click = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime("%H:%M:%S WIB")
    tel_sections = []
    for sym in TRADING_SYMBOLS:
        raw_tel = r.get(f"hermes:telemetry:{sym.replace('/', '_')}")
        if not raw_tel and sym == TRADING_SYMBOLS[0]:
            raw_tel = r.get("hermes:telemetry")
        if raw_tel:
            try:
                t = json.loads(raw_tel)
                curr_p = float(t.get('price', 0) or 0)
                ema50_v = float(t.get('ema_slow', 0) or 0)
                step_pct = float(t.get('step_pct', 0.045) or 0.045)
                rsi_lim = float(t.get('rsi_limit', 35.0) or 35.0)
                alloc_pct = float(t.get('alloc_pct', 100.0) or 100.0)
                l1_target = ema50_v * (1.0 - step_pct)
                dist_pct = ((curr_p - ema50_v) / ema50_v * 100.0) if ema50_v > 0 else 0.0
                grid_status = f'ZONA BELI (-{step_pct*100:.1f}%)' if curr_p <= l1_target else f'STANDBY ({dist_pct:+.2f}% vs EMA50)'
                tel_sections.append(
                    f"<b>[{sym} | Modal {alloc_pct:.0f}%]</b>\n"
                    f"• Harga: <code>{fmt_curr(curr_p)}</code> | EMA50: <code>{fmt_curr(ema50_v)}</code>\n"
                    f"• Trigger L1 (-{step_pct*100:.1f}%): <code>{fmt_curr(l1_target)}</code>\n"
                    f"• Status: <code>{grid_status}</code>\n"
                    f"• RSI (14): <code>{t.get('rsi')}</code> (Syarat &lt; <code>{rsi_lim:.0f}</code>) | Update: <code>{str(t.get('updated_at', '-'))[-12:]}</code>"
                )
            except Exception:
                pass

    tel_block = "\n\n".join(tel_sections) if tel_sections else "<i>Belum ada data telemetri.</i>"
    logs = r.lrange("hermes:logs", 0, 10)
    log_lines = "\n".join([l.replace("<", "&lt;").replace(">", "&gt;") for l in reversed(logs)]) if logs else "Belum ada catatan log di Redis."

    return (
        f"{EMOJI_SCROLL} <b>LIVE TELEMETRY &amp; SYSTEM LOGS</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{tel_block}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>11 Aktivitas Terakhir</b> (Dicek: <code>{now_click}</code>):\n"
        f"<pre>{log_lines}</pre>"
    )

def show_logs(chat_id, message_id=None):
    text = build_logs_and_telemetry_text()
    inline_kb = types.InlineKeyboardMarkup(row_width=2)
    inline_kb.add(
        types.InlineKeyboardButton(f"{EMOJI_REFRESH} Refresh Log", callback_data="btn_logs"),
        types.InlineKeyboardButton(f"{EMOJI_CHART} Lihat Chart", callback_data="btn_send_chart")
    )
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=inline_kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=inline_kb)

def show_status(chat_id):
    is_killed = r.get("hermes:kill_switch") == "active"
    mode_label = os.getenv("TRADING_MODE", "PAPER").upper()
    state_str = f"{EMOJI_RED_CIRCLE} HALTED (Kill-Switch Active)" if is_killed else f"{EMOJI_GREEN_CIRCLE} RUNNING ({mode_label})"

    pair_lines = []
    last_update_str = "-"
    for sym in TRADING_SYMBOLS:
        raw_tel = r.get(f"hermes:telemetry:{sym.replace('/', '_')}")
        if not raw_tel and sym == TRADING_SYMBOLS[0]:
            raw_tel = r.get("hermes:telemetry")
        if raw_tel:
            try:
                t = json.loads(raw_tel)
                last_update_str = t.get("updated_at", last_update_str)
                alloc_p = float(t.get("alloc_pct", 100.0) or 100.0)
                pair_lines.append(f"• <code>{sym}</code> ({alloc_p:.0f}%): <code>{fmt_curr(t.get('price', 0))}</code>")
            except Exception:
                pair_lines.append(f"• <code>{sym}</code>: <code>-</code>")
        else:
            pair_lines.append(f"• <code>{sym}</code>: <code>-</code>")

    pairs_block = "\n".join(pair_lines)
    status_text = (
        f"{EMOJI_CHART} <b>HERMES v3 MULTI-PAIR STATUS</b>\n"
        f"• Status Trading: <code>{state_str}</code>\n"
        f"• Exchange: <code>{EXCHANGE_ID}</code>\n"
        f"{pairs_block}\n"
        f"• Detak Terakhir: <code>{last_update_str}</code>\n"
        f"• Kepatuhan Syariah: <code>{EMOJI_CHECK} STRICT (Spot 1:1, Zero-Swap)</code>\n"
    )
    inline_kb = types.InlineKeyboardMarkup(row_width=2)
    if is_killed:
        inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_PLAY} Resume Trading", callback_data="btn_resume"))
    else:
        inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_OCTAGON} Matikan Trading (Kill)", callback_data="btn_kill"))
    inline_kb.add(
        types.InlineKeyboardButton(f"{EMOJI_SCROLL} Cek Log & Indikator", callback_data="btn_logs"),
        types.InlineKeyboardButton(f"{EMOJI_CHART} Chart Snapshot", callback_data="btn_send_chart")
    )
    bot.send_message(chat_id, status_text, parse_mode="HTML", reply_markup=inline_kb)

def send_chart_image(chat_id):
    for sym in TRADING_SYMBOLS:
        try:
            bot.send_chat_action(chat_id, 'upload_photo')
            chart_buf = generate_paxg_chart(limit=30, pair=sym)
            bot.send_photo(
                chat_id,
                chart_buf,
                caption=f"{EMOJI_CHART} <b>{sym} Technical Overview ({EXCHANGE_ID})</b>\n• Strategi: HERMES v3 Wide-Swing 3-Layer Spot Grid (1H)\n• Mode: Spot 1:1 (Syariah)",
                parse_mode="HTML"
            )
        except Exception as e:
            bot.send_message(chat_id, f"{EMOJI_WARNING} Gagal merender grafik {sym}: {e}")

def execute_resume(chat_id):
    r.delete("hermes:kill_switch")
    r.publish("hermes:events", "TRADING_RESUMED")
    bot.send_message(chat_id, f"{EMOJI_CHECK} <b>KILL-SWITCH DICABUT</b>. Sistem kembali normal.", parse_mode="HTML")
    show_status(chat_id)

def execute_kill(chat_id):
    r.set("hermes:kill_switch", "active")
    r.publish("hermes:events", "KILL_SWITCH_TRIGGERED")
    inline_kb = types.InlineKeyboardMarkup()
    inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_PLAY} Resume Trading", callback_data="btn_resume"))
    bot.send_message(chat_id, f"{EMOJI_SIREN} <b>KILL-SWITCH DIAKTIFKAN</b>. Seluruh trading distop darurat.", parse_mode="HTML", reply_markup=inline_kb)

@bot.message_handler(commands=['start', 'menu'])
def send_welcome(message):
    if not is_admin(message): return
    mode_label = os.getenv("TRADING_MODE", "PAPER").upper()
    text = f"{EMOJI_SHIELD} <b>HERMES v3 CONTROL PANEL ({EXCHANGE_ID} - {mode_label})</b>\n\nGunakan menu tombol navigasi di bawah layar:"
    inline_kb = types.InlineKeyboardMarkup(row_width=2)
    inline_kb.add(
        types.InlineKeyboardButton(f"{EMOJI_CHART} Status", callback_data="btn_status"),
        types.InlineKeyboardButton(f"{EMOJI_SCROLL} Log & Indikator", callback_data="btn_logs"),
        types.InlineKeyboardButton(f"{EMOJI_UP_GRAPH} Rekap PnL", callback_data="btn_summary"),
        types.InlineKeyboardButton(f"{EMOJI_CLIPBOARD} Posisi", callback_data="btn_positions"),
        types.InlineKeyboardButton(f"{EMOJI_CHART} Chart", callback_data="btn_send_chart"),
        types.InlineKeyboardButton(f"{EMOJI_PACKAGE} Unduh DB", callback_data="btn_download_live")
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=get_main_reply_keyboard())
    bot.send_message(message.chat.id, f"{EMOJI_BULB} <b>Menu Cepat Interaktif:</b>", parse_mode="HTML", reply_markup=inline_kb)

@bot.message_handler(func=lambda msg: msg.text in [f"{EMOJI_SCROLL} Log & Indikator", "📜 Log & Indikator", "/logs", "/log"])
def handle_logs_msg(message):
    if is_admin(message): show_logs(message.chat.id)

@bot.message_handler(func=lambda msg: msg.text in [f"{EMOJI_CHART} Lihat Chart", "📊 Lihat Chart", "/chart"])
def handle_chart_msg(message):
    if is_admin(message): send_chart_image(message.chat.id)

@bot.message_handler(func=lambda msg: msg.text in [f"{EMOJI_SIREN} Emergency Kill", "🚨 Emergency Kill", "/kill"])
def handle_kill_msg(message):
    if is_admin(message): execute_kill(message.chat.id)

@bot.message_handler(commands=['resume'])
def handle_resume_msg(message):
    if is_admin(message): execute_resume(message.chat.id)

@bot.message_handler(func=lambda msg: msg.text in [f"{EMOJI_CHART} Status Sistem", "📊 Status Sistem", "/status"])
def handle_status(message):
    if is_admin(message): show_status(message.chat.id)

@bot.message_handler(func=lambda msg: msg.text in [f"{EMOJI_UP_GRAPH} Rekap PnL", "📈 Rekap PnL", "/summary"])
def handle_summary(message):
    if not is_admin(message): return
    report = generate_performance_report("now")
    inline_kb = types.InlineKeyboardMarkup()
    inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_REFRESH} Refresh Rekap", callback_data="btn_summary"))
    bot.send_message(message.chat.id, report, parse_mode="HTML", reply_markup=inline_kb)

@bot.message_handler(func=lambda msg: msg.text in [f"{EMOJI_CLIPBOARD} Posisi Aktif", "📋 Posisi Aktif", "/positions"])
def handle_positions(message):
    if not is_admin(message): return
    if not os.path.exists(DB_PATH):
        bot.send_message(message.chat.id, f"{EMOJI_WARNING} Database belum tersedia.")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT ticket, pair, volume, fill_price, take_profit, stop_loss, created_at FROM orders WHERE status = 'OPEN'")
        rows = c.fetchall()
        conn.close()
        if not rows:
            bot.send_message(message.chat.id, f"{EMOJI_INFO} Tidak ada posisi terbuka saat ini.")
            return
        resp = f"{EMOJI_CLIPBOARD} <b>POSISI TERBUKA ({len(rows)} Posisi)</b>:\n\n"
        inline_kb = types.InlineKeyboardMarkup(row_width=1)
        for r_item in rows:
            ticket = r_item[0]
            resp += (
                f"• Tiket: <code>{ticket}</code> | Pair: <code>{r_item[1]}</code>\n"
                f"  Vol: <code>{r_item[2]}</code> | Entry: <code>{fmt_curr(r_item[3])}</code>\n"
                f"  TP: <code>{fmt_curr(r_item[4])}</code> | SL: <code>{fmt_curr(r_item[5])}</code>\n"
                f"  Waktu: <code>{r_item[6]}</code>\n\n"
            )
            inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_RED_CIRCLE} Tutup {ticket} ({r_item[1]})", callback_data=f"close_{ticket}"))
        if len(rows) > 1:
            inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_WARNING} TUTUP SEMUA POSISI", callback_data="close_ALL"))
        bot.send_message(message.chat.id, resp, parse_mode="HTML", reply_markup=inline_kb)
    except Exception as e:
        bot.send_message(message.chat.id, f"{EMOJI_OCTAGON} Error baca database: {e}")

@bot.message_handler(func=lambda msg: msg.text in [f"{EMOJI_DISK} Menu Backup", "💾 Menu Backup", "/backup"])
def handle_backup(message):
    if not is_admin(message): return
    if not os.path.exists(BACKUP_DIR):
        bot.send_message(message.chat.id, f"{EMOJI_WARNING} Direktori backup vault belum siap.")
        return
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')], reverse=True)
    resp = f"{EMOJI_DISK} <b>HERMES BACKUP VAULT</b> ({len(backups)} snapshot):\n\n"
    for b in backups[:5]:
        size_kb = os.path.getsize(os.path.join(BACKUP_DIR, b)) // 1024
        resp += f"• <code>{b}</code> ({size_kb} KB)\n"
    inline_kb = types.InlineKeyboardMarkup(row_width=2)
    inline_kb.add(
        types.InlineKeyboardButton(f"{EMOJI_PACKAGE} Unduh Snapshot", callback_data="btn_download_backup"),
        types.InlineKeyboardButton(f"{EMOJI_RED_CIRCLE} Unduh Live DB", callback_data="btn_download_live")
    )
    bot.send_message(message.chat.id, resp, parse_mode="HTML", reply_markup=inline_kb)

@bot.callback_query_handler(func=lambda call: True)
def handle_inline_clicks(call):
    if not is_admin(call):
        bot.answer_callback_query(call.id, "Akses ditolak.")
        return
    data = call.data
    chat_id = call.message.chat.id

    if data.startswith("close_"):
        ticket = data.replace("close_", "")
        bot.answer_callback_query(call.id, f"Menutup order {ticket}...")
        r.publish("hermes:manual_close", json.dumps({"ticket": ticket, "pair": TRADING_SYMBOL}))
        bot.send_message(chat_id, f"{EMOJI_LIGHTNING} Perintah penutupan dikirim untuk: <code>{ticket}</code>", parse_mode="HTML")
    elif data == "btn_status":
        bot.answer_callback_query(call.id)
        show_status(chat_id)
    elif data == "btn_logs":
        bot.answer_callback_query(call.id, "Memuat telemetri & log...")
        show_logs(chat_id, call.message.message_id)
    elif data == "btn_send_chart":
        bot.answer_callback_query(call.id, "Merender grafik...")
        send_chart_image(chat_id)
    elif data == "btn_resume":
        bot.answer_callback_query(call.id, "Mengaktifkan kembali sistem...")
        execute_resume(chat_id)
    elif data == "btn_kill":
        bot.answer_callback_query(call.id, "Kill-switch aktif!")
        execute_kill(chat_id)
    elif data == "btn_summary":
        bot.answer_callback_query(call.id, "Memperbarui rekap...")
        report = generate_performance_report("now")
        inline_kb = types.InlineKeyboardMarkup()
        inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_REFRESH} Refresh Rekap", callback_data="btn_summary"))
        try:
            bot.edit_message_text(report, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=inline_kb)
        except Exception:
            bot.send_message(chat_id, report, parse_mode="HTML", reply_markup=inline_kb)
    elif data == "btn_positions":
        bot.answer_callback_query(call.id)
        handle_positions(call.message)
    elif data == "btn_backup":
        bot.answer_callback_query(call.id)
        handle_backup(call.message)
    elif data == "btn_download_backup":
        bot.answer_callback_query(call.id, "Mengambil snapshot...")
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')], reverse=True)
        if backups:
            target = os.path.join(BACKUP_DIR, backups[0])
            with open(target, 'rb') as doc:
                bot.send_document(chat_id, doc, caption=f"{EMOJI_PACKAGE} Snapshot: <code>{backups[0]}</code>", parse_mode="HTML", timeout=60)
        else:
            bot.send_message(chat_id, f"{EMOJI_WARNING} File snapshot tidak ditemukan.")
    elif data == "btn_download_live":
        bot.answer_callback_query(call.id, "Mengambil file live DB...")
        temp_backup = "/tmp/trading_live_export.db"
        try:
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(temp_backup)
            with dst:
                src.backup(dst, pages=1, progress=None)
            dst.close()
            src.close()
            with open(temp_backup, 'rb') as doc:
                bot.send_document(chat_id, doc, caption=f"{EMOJI_RED_CIRCLE} <b>Live DB Aktif</b>", parse_mode="HTML", timeout=60)
            if os.path.exists(temp_backup):
                os.remove(temp_backup)
        except Exception as e:
            bot.send_message(chat_id, f"{EMOJI_OCTAGON} Gagal ambil live DB: {e}")

def safe_send_alert(text):
    for _ in range(3):
        try:
            bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML", timeout=15)
            return
        except Exception:
            time.sleep(2)

def listen_trade_alerts():
    pubsub = r.pubsub()
    pubsub.subscribe("hermes:alerts")
    for msg in pubsub.listen():
        if msg["type"] == "message":
            try:
                alert = json.loads(msg["data"])
                event_type = alert.get("type")
                pair = alert.get("pair", TRADING_SYMBOL)

                if event_type == "CIRCUIT_BREAKER_TRIGGERED":
                    text = (
                        f"{EMOJI_SIREN} <b>CIRCUIT BREAKER RISK ACTIVATED</b> {EMOJI_SIREN}\n\n"
                        f"Sistem menghentikan seluruh aktivitas trading otomatis.\n"
                        f"Alasan: {alert.get('reason')}\n\n"
                        f"Tekan tombol di bawah jika ingin mengaktifkan kembali secara manual."
                    )
                    inline_kb = types.InlineKeyboardMarkup()
                    inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_PLAY} Resume Trading", callback_data="btn_resume"))
                    bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML", reply_markup=inline_kb)
                elif event_type == "SYSTEM_ERROR":
                    err_msg = str(alert.get("message", "")).replace("<", "&lt;").replace(">", "&gt;")
                    text = (
                        f"{EMOJI_WARNING} <b>SYSTEM / BROKER ALERT ({alert.get('level', 'ERROR')})</b>\n"
                        f"• Waktu: <code>{alert.get('timestamp', '-')}</code>\n"
                        f"• Detail: <code>{err_msg}</code>\n"
                    )
                    inline_kb = types.InlineKeyboardMarkup()
                    inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_SCROLL} Lihat Log Lengkap", callback_data="btn_logs"))
                    bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML", reply_markup=inline_kb)
                elif event_type == "SIGNAL_SKIPPED":
                    text = (
                        f"👀 <b>RADAR SINYAL: GRID DITAHAN FILTER</b>\n"
                        f"• Pasangan: <code>{pair}</code>\n"
                        f"• Harga Saat Ini: <code>{fmt_curr(alert.get('price'))}</code>\n"
                        f"• EMA 20 vs 50: <code>{fmt_curr(alert.get('ema_fast'))}</code> | <code>{fmt_curr(alert.get('ema_slow'))}</code>\n"
                        f"• RSI (14): <code>{alert.get('rsi')}</code>\n"
                        f"• Alasan Tahan: <code>{alert.get('reason')}</code>\n\n"
                        f"<i>Bot menunggu konfirmasi pantulan sebelum masuk posisi.</i>"
                    )
                    safe_send_alert(text)
                elif event_type == "TRADE_REJECTED":
                    text = (
                        f"{EMOJI_OCTAGON} <b>SINYAL DITOLAK WORKER GUARD</b>\n"
                        f"• Pair: <code>{pair}</code>\n"
                        f"• Alasan: <code>{alert.get('reason')}</code>"
                    )
                    safe_send_alert(text)
                elif event_type == "ORDER_EXECUTED":
                    ticket = alert.get('ticket')
                    text = (
                        f"{EMOJI_ROCKET} <b>SPOT ORDER FILLED (SYARIAH COMPLIANT)</b>\n"
                        f"• Tiket: <code>{ticket}</code>\n"
                        f"• Pasangan: <code>{pair}</code> (Spot 1:1)\n"
                        f"• Aksi: <code>{alert.get('action')}</code> | Vol: <code>{alert.get('volume')}</code>\n"
                        f"• Harga Fill: <code>{fmt_curr(alert.get('fill_price'))}</code>\n"
                        f"• Total Nilai: <code>{fmt_curr(alert.get('total_cost', 0))}</code>\n"
                        f"• Target TP: <code>{fmt_curr(alert.get('tp'))}</code>\n"
                        f"• Sisa Kas Tunai: <code>{fmt_curr(alert.get('balance'))}</code>\n"
                    )
                    inline_kb = types.InlineKeyboardMarkup()
                    inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_RED_CIRCLE} Tutup {ticket}", callback_data=f"close_{ticket}"))
                    inline_kb.add(types.InlineKeyboardButton(f"{EMOJI_CHART} Lihat Chart", callback_data="btn_send_chart"))
                    bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML", reply_markup=inline_kb)
                elif event_type == "POSITION_CLOSED":
                    pnl = float(alert.get("pnl", 0.0))
                    emoji_badge = EMOJI_MONEY_BAG if pnl >= 0 else EMOJI_OCTAGON
                    text = (
                        f"{emoji_badge} <b>POSISI DITUTUP ({alert.get('reason')})</b>\n"
                        f"• Tiket: <code>{alert.get('ticket')}</code>\n"
                        f"• Pasangan: <code>{pair}</code>\n"
                        f"• Entry: <code>{fmt_curr(alert.get('entry_price'))}</code>\n"
                        f"• Exit: <code>{fmt_curr(alert.get('close_price'))}</code>\n"
                        f"• PnL: <code>{fmt_curr(pnl)}</code>\n"
                        f"• Saldo Kas Akhir: <code>{fmt_curr(alert.get('balance'))}</code>\n"
                    )
                    safe_send_alert(text)
            except Exception as err:
                print(f"[guardian] Error parsing alert: {err}")

def midnight_summary_scheduler():
    last_sent_day = None
    while True:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if now_utc.hour == 0 and now_utc.minute == 0 and last_sent_day != now_utc.day:
            yesterday_str = (now_utc - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            report = f"{EMOJI_MOON} <b>LAPORAN PENUTUPAN HARIAN (EOD UTC)</b>\n\n" + generate_performance_report(yesterday_str)
            safe_send_alert(report)
            last_sent_day = now_utc.day
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=listen_trade_alerts, daemon=True).start()
    threading.Thread(target=midnight_summary_scheduler, daemon=True).start()
    print(f"[guardian] Telegram Guardian Bot ({EXCHANGE_ID} - {', '.join(TRADING_SYMBOLS)}) aktif.")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as err:
            print(f"[guardian] Polling reconnect ({err})...")
            time.sleep(3)
