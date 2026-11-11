import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

# Si estamos en internet, usamos la base de datos del servidor. Si no, usamos la local.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///energi2.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    product_id TEXT,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS latest_telemetry (
    device_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    source_timestamp BIGINT NOT NULL,
    received_at BIGINT NOT NULL,
    voltage REAL, voltage_updated_at BIGINT,
    current REAL, current_updated_at BIGINT,
    power REAL, power_updated_at BIGINT,
    energy_kwh REAL, energy_updated_at BIGINT
);

CREATE TABLE IF NOT EXISTS energy_readings (
    id SERIAL PRIMARY KEY,
    device_id TEXT NOT NULL,
    sampled_at BIGINT NOT NULL,
    energy_kwh REAL NOT NULL,
    UNIQUE(device_id, sampled_at, energy_kwh)
);
"""

def init_db() -> None:
    if DATABASE_URL.startswith("sqlite"):
        import sqlite3
        with sqlite3.connect("energi2.sqlite3") as conn:
            conn.executescript(SCHEMA.replace("SERIAL", "INTEGER PRIMARY KEY AUTOINCREMENT").replace("BIGINT", "INTEGER"))
    else:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
            conn.commit()

@contextmanager
def get_conn():
    if DATABASE_URL.startswith("sqlite"):
        import sqlite3
        conn = sqlite3.connect("energi2.sqlite3")
        conn.row_factory = sqlite3.Row
        try: yield conn
        finally: conn.close()
    else:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur
            conn.commit()
        finally:
            conn.close()
