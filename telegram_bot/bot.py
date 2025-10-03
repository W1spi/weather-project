# bot.py
# Телеграм-бот для чтения погодной БД (SQLite) и выдачи быстрых ответов.
# Добавлен выбор датчика через инлайн-кнопки (BME280 / DHT22) с подсветкой активного.

import os, sqlite3, time, json, asyncio
from datetime import datetime
from typing import Optional, Tuple, Dict, Any
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest

# ---------- конфиг через окружение ----------
DB_FILE             = os.environ.get("DB_FILE", "/data/weather.db")
TZ                  = os.environ.get("TZ", "Europe/Vilnius")
TZINFO              = ZoneInfo(TZ)

# Сенсор по умолчанию (bme280|dht22)
SENSOR_DEFAULT      = (os.environ.get("SENSOR_PREFERRED", "bme280") or "bme280").lower()

# Ретеншн БД (в днях) — только чтобы ограничивать /ago
RETENTION_DAYS      = int(os.environ.get("RETENTION_DAYS", "90"))

# Кэш ответов (сек)
CACHE_TTL_SEC       = float(os.environ.get("CACHE_TTL_SEC", "2.0"))

# Read-only доступ к БД
READONLY            = True

# Анти-дребезг кликов инлайн-кнопок (мс)
DEBOUNCE_MS         = int(os.environ.get("DEBOUNCE_MS", "400"))

# Дефолтный интервал тренда
TREND_MINUTES_DEFAULT = int(os.environ.get("TREND_MINUTES", "30"))

# ---------- кэш/антиспам/состояние выбора сенсора ----------
# Ключ кэша — (minutes, sensor). Значение — (text, expires_at).
_cache: Dict[Tuple[int, str], Tuple[str, float]] = {}
_last_hit: Dict[int, float] = {}
_selected_sensor: Dict[int, str] = {}  # chat_id -> 'bme280'|'dht22'

def get_selected_sensor(chat_id: int) -> str:
    return _selected_sensor.get(chat_id, SENSOR_DEFAULT)

def set_selected_sensor(chat_id: int, sensor: str) -> None:
    if sensor in ("bme280", "dht22"):
        _selected_sensor[chat_id] = sensor

def _cache_get(minutes: int, sensor: str) -> Optional[str]:
    it = _cache.get((minutes, sensor))
    if not it: return None
    text, exp = it
    return text if time.time() <= exp else None

def _cache_put(minutes: int, sensor: str, text: str) -> None:
    _cache[(minutes, sensor)] = (text, time.time() + CACHE_TTL_SEC)

def _too_fast(chat_id: int, ms: int = DEBOUNCE_MS) -> bool:
    now = time.monotonic()
    prev = _last_hit.get(chat_id, 0.0)
    _last_hit[chat_id] = now
    return (now - prev) * 1000.0 < ms


# ---------- утилиты времени/формата ----------
def ts_to_local_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=TZINFO).strftime("%Y-%m-%d %H:%M:%S")

def _fmt_num(v: Optional[float], fmt: str) -> str:
    return "—" if v is None else fmt.format(float(v))

def _fmt_trend(delta: Optional[float], fmt: str) -> str:
    if delta is None: return "—"
    d = float(delta)
    arrow = "↗" if d > 0 else ("↘" if d < 0 else "→")
    return f"{arrow} " + fmt.format(d)


# ---------- DB: подключение и короткие запросы ----------
def connect(readonly: bool = READONLY) -> sqlite3.Connection:
    """
    Read-only подключение к SQLite.
    mode=ro + immutable=1: SQLite не пишет служебные данные.
    cache=shared: общий кэш страниц между соединениями.
    """
    if readonly:
        uri = f"file:{DB_FILE}?mode=ro&cache=shared&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000;")
        con.execute("PRAGMA temp_store=MEMORY;")
        con.execute("PRAGMA cache_size=-20000;")  # ~20MB
        try: con.execute("PRAGMA mmap_size=268435456;")
        except Exception: pass
        return con
    else:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        con = sqlite3.connect(DB_FILE, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000;")
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA temp_store=MEMORY;")
        con.execute("PRAGMA cache_size=-20000;")
        try: con.execute("PRAGMA mmap_size=268435456;")
        except Exception: pass
        return con

SQL_LATEST_FOR_SENSOR = """
SELECT sensor, ts, temp, hum, press
FROM readings
WHERE sensor=?
ORDER BY ts DESC
LIMIT 1;
"""
SQL_LATEST_ANY = """
SELECT sensor, ts, temp, hum, press
FROM readings
ORDER BY ts DESC
LIMIT 1;
"""
SQL_AT_MINUS_FOR_SENSOR = """
SELECT sensor, ts, temp, hum, press
FROM readings
WHERE sensor=? AND ts <= ?
ORDER BY ts DESC
LIMIT 1;
"""
SQL_AT_MINUS_ANY = """
SELECT sensor, ts, temp, hum, press
FROM readings
WHERE ts <= ?
ORDER BY ts DESC
LIMIT 1;
"""
SQL_SENSORS_OVERVIEW = """
SELECT sensor, COUNT(*) AS cnt, MAX(ts) AS last_ts
FROM readings
GROUP BY sensor
ORDER BY last_ts DESC;
"""

def _fetch_one(sql: str, params=()):
    con = connect()
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()

def get_latest_for_sensor(sensor: str):
    row = _fetch_one(SQL_LATEST_FOR_SENSOR, (sensor,))
    return (row["sensor"], row["ts"], row["temp"], row["hum"], row["press"]) if row else None

def get_latest_any():
    row = _fetch_one(SQL_LATEST_ANY)
    return (row["sensor"], row["ts"], row["temp"], row["hum"], row["press"]) if row else None

def get_at_minus_for_sensor(sensor: str, minutes: int):
    target = int(time.time()) - max(0, minutes) * 60
    row = _fetch_one(SQL_AT_MINUS_FOR_SENSOR, (sensor, target))
    return (row["sensor"], row["ts"], row["temp"], row["hum"], row["press"]) if row else None

def get_at_minus_any(minutes: int):
    target = int(time.time()) - max(0, minutes) * 60
    row = _fetch_one(SQL_AT_MINUS_ANY, (target,))
    return (row["sensor"], row["ts"], row["temp"], row["hum"], row["press"]) if row else None

def sensors_overview():
    con = connect()
    try:
        return con.execute(SQL_SENSORS_OVERVIEW).fetchall()
    finally:
        con.close()

def compute_trend(sensor: str, minutes: int):
    """Δ между «сейчас» и значением N минут назад по тому же сенсору."""
    now_row  = get_latest_for_sensor(sensor) or get_latest_any()
    past_row = get_at_minus_for_sensor(sensor, minutes) or get_at_minus_any(minutes)
    if not now_row or not past_row:
        return (None, None, None)
    _, _, t_now, h_now, p_now = now_row
    _, _, t_past, h_past, p_past = past_row
    def diff(a, b): return None if a is None or b is None else float(a) - float(b)
    return (diff(t_now, t_past), diff(h_now, h_past), diff(p_now, p_past))


# ---------- обёртка: DB-операции в пуле потоков ----------
async def _run_db(fn, *a, **kw):
    return await asyncio.to_thread(fn, *a, **kw)


# ---------- форматирование / UI ----------
def format_row(title: str, row, selected_sensor: str, trend=None) -> str:
    """
    Заголовок включает активный сенсор.
    row: (sensor, ts, temp, hum, press)
    trend: (Δt, Δh, Δp) — если передан, добавляется в конце.
    """
    header = f"Выбран датчик: *{selected_sensor.upper()}*"
    if not row:
        return f"{header}\n\n{title}\nНет данных за выбранный период."

    sensor, ts, t, h, p = row

    text = (
        f"{header}\n\n"
        f"{title} ({sensor})\n"
        f"🕒 {ts_to_local_str(ts)}\n"
        f"🌡 Температура: {_fmt_num(t, '{:.1f}')} °C\n"
        f"💧 Влажность: {_fmt_num(h, '{:.0f}')} %\n"
        f"🧭 Давление: {_fmt_num(p, '{:.0f}')} mmHg"
    )

    if trend is not None:
        dt, dh, dp = trend
        text += (
            "\n"
            f"Тренд: "
            f"{_fmt_trend(dt, '{:+.1f}')} °C | "
            f"{_fmt_trend(dh, '{:+.0f}')} % | "
            f"{_fmt_trend(dp, '{:+.0f}')} mm"
        )

    return text

def kb_main(selected_sensor: str) -> InlineKeyboardMarkup:
    """
    Клавиатура:
      [BME280] [DHT22]    ← строка выбора датчика (активный с галочкой)
      [ Сейчас ] [ 30 мин ] [ 1 час ] [ 2 часа ]
      [ Δ 30 мин ]
    """
    row_sensors = [
        InlineKeyboardButton(
            ("✅ BME280" if selected_sensor == "bme280" else "BME280"),
            callback_data="sensor:bme280"
        ),
        InlineKeyboardButton(
            ("✅ DHT22" if selected_sensor == "dht22" else "DHT22"),
            callback_data="sensor:dht22"
        ),
    ]
    row_time = [
        InlineKeyboardButton("Сейчас", callback_data="ago:0"),
        InlineKeyboardButton("30 мин", callback_data="ago:30"),
        InlineKeyboardButton("1 час",  callback_data="ago:60"),
        InlineKeyboardButton("2 часа", callback_data="ago:120"),
    ]
    row_trend = [InlineKeyboardButton("Δ 30 мин", callback_data=f"trend:{TREND_MINUTES_DEFAULT}")]
    return InlineKeyboardMarkup([row_sensors, row_time, row_trend])


# ---------- бизнес-логика выбора записи ----------
def pick_latest_sync(sensor: str, minutes: int = 0):
    """Выбор записи по сенсору и минутам (0 = текущая)."""
    if minutes <= 0:
        row = get_latest_for_sensor(sensor)
        return row or get_latest_any()
    row = get_at_minus_for_sensor(sensor, minutes)
    return row or get_at_minus_any(minutes)


# ---------- команды ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    set_selected_sensor(chat_id, get_selected_sensor(chat_id))  # инициализация значением по умолчанию
    text = (
        "Привет! Я бот с показаниями датчиков.\n\n"
        "Команды:\n"
        "/weather — текущее с меню\n"
        "/ago <минуты> [sensor] — показания N минут назад\n"
        "/trend [минуты] [sensor] — тренд Δ (по умолчанию 30 мин)\n"
        "/debug — краткая диагностика БД\n"
        "/dbdiag — путь, размер и статистика"
    )
    await update.message.reply_text(text, reply_markup=kb_main(get_selected_sensor(chat_id)))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текущее значение по выбранному сенсору (из меню)."""
    try:
        chat_id = update.message.chat_id
        sensor  = get_selected_sensor(chat_id)
        minutes = 0

        cached = _cache_get(minutes, sensor)
        if cached:
            await update.message.reply_text(cached, reply_markup=kb_main(sensor), parse_mode="Markdown")
            return

        row = await _run_db(pick_latest_sync, sensor, minutes)
        new_text = format_row("Сейчас", row, sensor)
        _cache_put(minutes, sensor, new_text)

        await update.message.reply_text(new_text, reply_markup=kb_main(sensor), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {type(e).__name__}: {e}")

async def cmd_ago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /ago <минуты> [sensor]
    Примеры: /ago 15   |   /ago 90 dht22
    """
    try:
        chat_id = update.message.chat_id
        args = context.args or []

        minutes = None
        sensor = None
        for a in args:
            if minutes is None and a.isdigit():
                minutes = int(a)
            elif sensor is None:
                sensor = a.lower()

        if minutes is None:
            await update.message.reply_text("Нужно число минут: /ago 30 [bme280|dht22]")
            return

        max_minutes = RETENTION_DAYS * 24 * 60
        minutes = max(0, min(minutes, max_minutes))
        if sensor not in ("bme280", "dht22", None):
            await update.message.reply_text("Неизвестный сенсор. Допустимо: bme280 или dht22.")
            return
        if sensor is None:
            sensor = get_selected_sensor(chat_id)

        cached = _cache_get(minutes, sensor)
        if cached:
            await update.message.reply_text(cached, reply_markup=kb_main(sensor), parse_mode="Markdown")
            return

        row = await _run_db(pick_latest_sync, sensor, minutes)
        title = "Сейчас" if minutes <= 0 else f"{minutes} мин назад"
        new_text = format_row(title, row, sensor)
        _cache_put(minutes, sensor, new_text)

        await update.message.reply_text(new_text, reply_markup=kb_main(sensor), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {type(e).__name__}: {e}")

async def cmd_trend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /trend [минуты] [sensor] — тренд Δ между «сейчас» и N минут назад.
    Примеры: /trend   |   /trend 60   |   /trend 45 dht22
    """
    try:
        chat_id = update.message.chat_id
        args = context.args or []
        minutes = None
        sensor = None
        for a in args:
            if minutes is None and a.isdigit():
                minutes = int(a)
            elif sensor is None:
                sensor = a.lower()

        if minutes is None:
            minutes = TREND_MINUTES_DEFAULT

        max_minutes = RETENTION_DAYS * 24 * 60
        minutes = max(1, min(minutes, max_minutes))
        if sensor not in ("bme280", "dht22", None):
            await update.message.reply_text("Неизвестный сенсор. Допустимо: bme280 или dht22.")
            return
        if sensor is None:
            sensor = get_selected_sensor(chat_id)

        dt, dh, dp = await _run_db(compute_trend, sensor, minutes)
        base = await _run_db(pick_latest_sync, sensor, 0)
        text = format_row("Сейчас", base, sensor, trend=(dt, dh, dp))
        text += f"\n(Тренд за {minutes} мин)"
        await update.message.reply_text(text, reply_markup=kb_main(sensor), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {type(e).__name__}: {e}")

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = await _run_db(sensors_overview)
        if not rows:
            await update.message.reply_text("В БД пока нет записей.")
            return
        lines = [f"• {r['sensor']}: {r['cnt']} шт, последний: {ts_to_local_str(r['last_ts'])}" for r in rows]
        await update.message.reply_text("Сенсоры в БД:\n" + "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {type(e).__name__}: {e}")

async def cmd_dbdiag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        path = os.path.abspath(DB_FILE)
        exists = os.path.exists(DB_FILE)
        size = os.path.getsize(DB_FILE) if exists else 0
        rows = await _run_db(sensors_overview)
        cnt = sum(r["cnt"] for r in rows) if rows else 0

        lines = [
            f"DB_FILE: {DB_FILE}",
            f"Абс. путь: {path}",
            f"Файл существует: {exists}",
            f"Размер: {size} байт",
            f"Всего записей: {cnt}",
        ]
        if rows:
            for r in rows:
                lines.append(f"• {r['sensor']}: {r['cnt']} шт, последний: {ts_to_local_str(r['last_ts'])}")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {type(e).__name__}: {e}")


# ---------- инлайн-кнопки ----------
async def on_select_sensor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение сенсора кнопками BME280/DHT22 и мгновенное обновление экрана (режим 'Сейчас')."""
    q = update.callback_query
    try:
        await q.answer()
        chat_id = q.message.chat_id
        if _too_fast(chat_id):
            await q.answer("Подождите мгновение…", show_alert=False)
            return

        sensor = q.data.split(":")[1]
        set_selected_sensor(chat_id, sensor)

        # Показываем «Сейчас» по новому сенсору
        minutes = 0
        cached = _cache_get(minutes, sensor)
        if cached:
            if q.message and (q.message.text or "") == cached:
                await q.edit_message_reply_markup(reply_markup=kb_main(sensor))
                return
            await q.edit_message_text(cached, reply_markup=kb_main(sensor), parse_mode="Markdown")
            return

        row = await _run_db(pick_latest_sync, sensor, minutes)
        new_text = format_row("Сейчас", row, sensor)
        _cache_put(minutes, sensor, new_text)

        # Обновляем текст и клавиатуру. Если текст не изменился — обновим только клавиатуру.
        if q.message and (q.message.text or "") == new_text:
            await q.edit_message_reply_markup(reply_markup=kb_main(sensor))
        else:
            await q.edit_message_text(new_text, reply_markup=kb_main(sensor), parse_mode="Markdown")

    except BadRequest as e:
        if "Message is not modified" in str(e):
            await q.answer("Без изменений", show_alert=False); return
        await q.edit_message_text(f"Ошибка: BadRequest: {e}")
    except Exception as e:
        await q.edit_message_text(f"Ошибка: {type(e).__name__}: {e}")

async def on_ago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
        chat_id = q.message.chat_id
        if _too_fast(chat_id):
            await q.answer("Подождите мгновение…", show_alert=False)
            return

        minutes = int(q.data.split(":")[1])
        sensor  = get_selected_sensor(chat_id)

        cached = _cache_get(minutes, sensor)
        if cached:
            # Если реально один-в-один — не трогаем текст, обновим только клавиатуру (чтоб галочка не сбрасывалась)
            if q.message and (q.message.text or "") == cached:
                await q.edit_message_reply_markup(reply_markup=kb_main(sensor))
                return
            await q.edit_message_text(cached, reply_markup=kb_main(sensor), parse_mode="Markdown")
            return

        row = await _run_db(pick_latest_sync, sensor, minutes)
        title = "Сейчас" if minutes <= 0 else f"{minutes} мин назад"
        new_text = format_row(title, row, sensor)
        _cache_put(minutes, sensor, new_text)

        if q.message and (q.message.text or "") == new_text:
            await q.edit_message_reply_markup(reply_markup=kb_main(sensor))
            return
        await q.edit_message_text(new_text, reply_markup=kb_main(sensor), parse_mode="Markdown")

    except BadRequest as e:
        if "Message is not modified" in str(e):
            await q.answer("Без изменений", show_alert=False); return
        await q.edit_message_text(f"Ошибка: BadRequest: {e}")
    except Exception as e:
        await q.edit_message_text(f"Ошибка: {type(e).__name__}: {e}")

async def on_trend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Δ 30 мин' — быстрый тренд по выбранному сенсору."""
    q = update.callback_query
    try:
        await q.answer()
        chat_id = q.message.chat_id
        if _too_fast(chat_id):
            await q.answer("Подождите мгновение…", show_alert=False)
            return

        minutes = int(q.data.split(":")[1])
        sensor  = get_selected_sensor(chat_id)

        dt, dh, dp = await _run_db(compute_trend, sensor, minutes)
        base = await _run_db(pick_latest_sync, sensor, 0)
        text = format_row("Сейчас", base, sensor, trend=(dt, dh, dp))
        text += f"\n(Тренд за {minutes} мин)"

        if q.message and (q.message.text or "") == text:
            await q.edit_message_reply_markup(reply_markup=kb_main(sensor))
            return
        await q.edit_message_text(text, reply_markup=kb_main(sensor), parse_mode="Markdown")

    except BadRequest as e:
        if "Message is not modified" in str(e):
            await q.answer("Без изменений", show_alert=False); return
        await q.edit_message_text(f"Ошибка: BadRequest: {e}")
    except Exception as e:
        await q.edit_message_text(f"Ошибка: {type(e).__name__}: {e}")


# ---------- регистрация и запуск ----------
async def setup_commands(app):
    await app.bot.set_my_commands([
        BotCommand("start",     "Памятка и меню"),
        BotCommand("help",      "Памятка"),
        BotCommand("weather",   "Показать текущую погоду (меню)"),
        BotCommand("ago",       "Показания N минут назад: /ago 15 [sensor]"),
        BotCommand("trend",     "Тренд Δ: /trend [минуты] [sensor]"),
        BotCommand("debug",     "Диагностика: что в БД"),
        BotCommand("dbdiag",    "Путь к БД и статистика"),
    ])

def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    app = ApplicationBuilder().token(token).build()

    # Команды
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("weather",   cmd_weather))
    app.add_handler(CommandHandler("ago",       cmd_ago))
    app.add_handler(CommandHandler("trend",     cmd_trend))
    app.add_handler(CommandHandler("debug",     cmd_debug))
    app.add_handler(CommandHandler("dbdiag",    cmd_dbdiag))

    # Инлайн-кнопки: выбор сенсора, время и тренд
    app.add_handler(CallbackQueryHandler(on_ago,           pattern=r"^ago:\d+$"))
    app.add_handler(CallbackQueryHandler(on_select_sensor, pattern=r"^sensor:(bme280|dht22)$"))
    app.add_handler(CallbackQueryHandler(on_trend,         pattern=r"^trend:\d+$"))

    app.post_init = setup_commands
    app.run_polling()

if __name__ == "__main__":
    main()
