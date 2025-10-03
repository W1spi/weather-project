# esp_server.py
# HTTP-сервер для приёма показаний от ESP и записи в SQLite.

from http.server import BaseHTTPRequestHandler, HTTPServer
import json, os, sqlite3, time, math
from pathlib import Path

# ---------- Конфигурация через окружение ----------
DATA_DIR        = os.environ.get("DATA_DIR", "/data")
DB_FILE         = os.environ.get("DB_FILE",  f"{DATA_DIR}/weather.db")

# Окно хранения (в днях)
RETENTION_DAYS  = int(os.environ.get("RETENTION_DAYS", "90"))

# Обслуживание
COMMIT_EVERY            = int(os.environ.get("COMMIT_EVERY", "1"))
CLEANUP_EVERY           = int(os.environ.get("CLEANUP_EVERY", "400"))
CLEANUP_INTERVAL_SEC    = int(os.environ.get("CLEANUP_INTERVAL_SEC", "60"))

# Значения по умолчанию (зона/источник)
DEFAULT_ZONE    = os.environ.get("DEFAULT_ZONE", "indoor")
DEFAULT_SOURCE  = os.environ.get("DEFAULT_SOURCE", "esp32")

# Жёсткая привязка датчик→зона (опционально)
INDOOR_SENSOR   = (os.environ.get("INDOOR_SENSOR") or "").lower().strip()  # "bme280"|"dht22"|"" (пусто = не фиксируем)
OUTDOOR_SENSOR  = (os.environ.get("OUTDOOR_SENSOR") or "").lower().strip()
FORCE_ZONE_BY_SENSOR = os.environ.get("FORCE_ZONE_BY_SENSOR", "false").lower() == "true"

# ---------- Подготовка каталога ----------
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# ---------- Утилиты ----------
def to_float(x):
    """Аккуратное приведение к float; None/''/NaN/inf -> None."""
    if x is None:
        return None
    try:
        s = str(x).strip()
        if not s or s.lower() == "nan":
            return None
        v = float(s)
        return v if math.isfinite(v) else None
    except Exception:
        return None

def has_pressure_mmhg(v) -> bool:
    """Грубая проверка валидного давления (мм рт. ст.). Диапазон намеренно широкий: 200..1200."""
    try:
        if v is None:
            return False
        f = float(v)
        return math.isfinite(f) and 200.0 <= f <= 1200.0
    except Exception:
        return False

def canonical_sensor(name: str | None, pressure_val) -> str:
    """
    Канонизация имени сенсора; на всякий случай оставлено.
    Сейчас основная логика — явное разделение на две группы полей.
    """
    s = (name or "").strip().lower()
    if s in ("bme280", "bme", "bmp280", "bmp", "bme-280", "bme_280"):
        return "bme280"
    if s in ("dht22", "dht", "dht-22", "dht_22", "am2302"):
        return "dht22"
    return "bme280" if has_pressure_mmhg(pressure_val) else "dht22"

def resolve_zone(sensor_canonical: str, zone_from_payload: str) -> str:
    """
    Приоритет зоны:
      1) FORCE_ZONE_BY_SENSOR → жёсткий маппинг;
      2) zone из payload;
      3) DEFAULT_ZONE.
    """
    s = (sensor_canonical or "").lower().strip()
    if FORCE_ZONE_BY_SENSOR:
        if INDOOR_SENSOR and s == INDOOR_SENSOR:
            return "indoor"
        if OUTDOOR_SENSOR and s == OUTDOOR_SENSOR:
            return "outdoor"
    if zone_from_payload:
        z = zone_from_payload.lower().strip()
        if z in ("indoor", "outdoor"):
            return z
    return DEFAULT_ZONE

# ---------- Инициализация БД ----------
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
conn.execute("PRAGMA journal_mode=TRUNCATE;")
conn.execute("PRAGMA synchronous=NORMAL;")
conn.execute("PRAGMA auto_vacuum=INCREMENTAL;")
conn.commit()

conn.execute("""
CREATE TABLE IF NOT EXISTS readings (
  ts     INTEGER NOT NULL,     -- unix UTC (сек)
  sensor TEXT    NOT NULL,     -- 'bme280' | 'dht22'
  zone   TEXT,                 -- 'indoor' | 'outdoor' | ...
  temp   REAL,                 -- °C
  hum    REAL,                 -- %
  press  REAL,                 -- мм рт. ст.
  source TEXT                  -- источник (по умолчанию 'esp32')
);
""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);")
conn.execute("DROP INDEX IF EXISTS idx_readings_sensor_ts;")
conn.commit()

_inserts_since_commit   = 0
_inserts_since_cleanup  = 0
_last_cleanup_ts        = 0.0

# ---------- HTTP-сервер ----------
class SimpleHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        """
        Принимает комбинированный JSON от ESP (оба сенсора в одном POST) и
        делает до ДВУХ вставок: одну для dht22, одну для bme280.
        Поддерживаемые поля (все синонимы равноправны):
          DHT: t_dht / temperature_dht, h_dht / humidity_dht
          BME: t_bme / temperature / temp / lt_bme, h_bme / humidity / hum, pressure / press / p_bme
          zone: 'indoor'|'outdoor' (общая; при FORCE_ZONE_BY_SENSOR переопределится)
          source: строка (по умолчанию 'esp32')
        """
        global _inserts_since_commit, _inserts_since_cleanup, _last_cleanup_ts
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n).decode("utf-8")
            payload = json.loads(raw)

            ts = int(time.time())  # серверная метка времени (UTC)

            # --- DHT группа ---
            t_dht = to_float(payload.get("t_dht") or payload.get("temperature_dht"))
            h_dht = to_float(payload.get("h_dht") or payload.get("humidity_dht"))

            # --- BME группа (сохраняем совместимость с прежними alias) ---
            t_bme = to_float(payload.get("t_bme") or payload.get("temperature") or
                             payload.get("temp")  or payload.get("lt_bme"))
            h_bme = to_float(payload.get("h_bme") or payload.get("humidity") or
                             payload.get("hum"))
            p_bme = to_float(payload.get("pressure") or payload.get("press") or
                             payload.get("p_bme"))

            zone   = (payload.get("zone") or "").strip()
            source = (payload.get("source") or DEFAULT_SOURCE).strip()

            wrote_any = False

            # --- INSERT для DHT22 (если есть хотя бы T или H) ---
            if (t_dht is not None) or (h_dht is not None):
                sensor = "dht22"
                z = resolve_zone(sensor, zone)
                cols = ["ts", "sensor"]
                vals = [ts, sensor]
                if z != DEFAULT_ZONE:
                    cols.append("zone");  vals.append(z)
                if t_dht is not None:
                    cols.append("temp");  vals.append(t_dht)
                if h_dht is not None:
                    cols.append("hum");   vals.append(h_dht)
                if source != DEFAULT_SOURCE:
                    cols.append("source"); vals.append(source)

                sql = f"INSERT INTO readings({','.join(cols)}) VALUES ({','.join(['?']*len(vals))})"
                conn.execute(sql, vals)
                wrote_any = True

            # --- INSERT для BME280 (если есть хотя бы T/H/P) ---
            if (t_bme is not None) or (h_bme is not None) or (p_bme is not None):
                sensor = "bme280"
                z = resolve_zone(sensor, zone)
                cols = ["ts", "sensor"]
                vals = [ts, sensor]
                if z != DEFAULT_ZONE:
                    cols.append("zone");  vals.append(z)
                if t_bme is not None:
                    cols.append("temp");  vals.append(t_bme)
                if h_bme is not None:
                    cols.append("hum");   vals.append(h_bme)
                if p_bme is not None:
                    cols.append("press"); vals.append(p_bme)
                if source != DEFAULT_SOURCE:
                    cols.append("source"); vals.append(source)

                sql = f"INSERT INTO readings({','.join(cols)}) VALUES ({','.join(['?']*len(vals))})"
                conn.execute(sql, vals)
                wrote_any = True

            # Если вовсе нет данных — всё равно отвечаем 200, но без вставки
            if wrote_any:
                _inserts_since_commit  += 1
                _inserts_since_cleanup += 1

                if _inserts_since_commit >= COMMIT_EVERY:
                    conn.commit()
                    _inserts_since_commit = 0

                now = time.time()
                if (_inserts_since_cleanup >= CLEANUP_EVERY) and (now - _last_cleanup_ts >= CLEANUP_INTERVAL_SEC):
                    cutoff = int(now) - RETENTION_DAYS * 24 * 3600
                    cur = conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
                    conn.commit()
                    if cur.rowcount and cur.rowcount > 0:
                        conn.execute("PRAGMA incremental_vacuum;")
                        conn.commit()
                    _inserts_since_cleanup = 0
                    _last_cleanup_ts = now

            self.send_response(200)

        except Exception as e:
            print("Ошибка при сохранении:", repr(e))
            self.send_response(500)
        finally:
            self.end_headers()

def run():
    addr = ('0.0.0.0', 8080)
    httpd = HTTPServer(addr, SimpleHandler)
    print(f"Сервер запущен на порту {addr[1]}")
    httpd.serve_forever()

if __name__ == "__main__":
    run()
