# init_db.py — одноразовая инициализация/миграция БД
import os, sqlite3, sys
from pathlib import Path

DB_FILE   = os.getenv("DB_FILE", "/data/weather.db")
DATA_DIR  = os.getenv("DATA_DIR", "/data")

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA auto_vacuum=INCREMENTAL;

CREATE TABLE IF NOT EXISTS readings (
  ts     INTEGER NOT NULL,     -- unix UTC (сек)
  sensor TEXT    NOT NULL,     -- 'bme280' | 'dht22'
  zone   TEXT,                 -- 'indoor'|'outdoor'|...
  temp   REAL,
  hum    REAL,
  press  REAL,
  source TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
CREATE INDEX IF NOT EXISTS idx_readings_sensor_ts_desc ON readings(sensor, ts DESC);
"""

def main() -> int:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    # Создаём пустой файл, если отсутствует
    if not Path(DB_FILE).exists():
        Path(DB_FILE).touch()

    # Открываем и применяем схему/миграции
    con = sqlite3.connect(DB_FILE, timeout=30)
    try:
        con.executescript(SCHEMA_SQL)
        con.commit()
        # Лёгкая проверка
        con.execute("SELECT 1 FROM readings LIMIT 1;")
    finally:
        con.close()
    print(f"[init_db] OK — база готова: {DB_FILE}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
