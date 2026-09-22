"""SQLite 数据访问层。启动时自动建库建表, 无需外部服务。"""

from __future__ import annotations

import os
import sqlite3
import time

DATA_DIR = os.environ.get(
    "DATA_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
)
DB_PATH = os.path.join(DATA_DIR, "battery_health.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS batteries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    rated_capacity_ah  REAL NOT NULL,
    safe_vmin          REAL NOT NULL,
    safe_vmax          REAL NOT NULL,
    created_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    battery_id   INTEGER NOT NULL REFERENCES batteries(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    uploaded_at  REAL NOT NULL,
    row_count    INTEGER NOT NULL,
    bad_rows     INTEGER NOT NULL DEFAULT 0,
    duration_s   REAL NOT NULL,
    cycle_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id      INTEGER NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
    t           REAL NOT NULL,
    voltage     REAL NOT NULL,
    current     REAL NOT NULL,
    temperature REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_log ON samples(log_id, t);

CREATE TABLE IF NOT EXISTS cycles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id      INTEGER NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
    battery_id  INTEGER NOT NULL REFERENCES batteries(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    start_t     REAL NOT NULL,
    end_t       REAL NOT NULL,
    duration_s  REAL NOT NULL,
    closed      INTEGER NOT NULL,
    capacity_ah REAL NOT NULL,
    energy_wh   REAL NOT NULL,
    ir_ohm      REAL,
    v_start     REAL, v_end REAL, v_min REAL, v_min_t REAL, v_max REAL,
    i_avg       REAL, i_max REAL,
    temp_start  REAL, temp_max REAL, temp_max_t REAL
);
CREATE INDEX IF NOT EXISTS idx_cycles_battery ON cycles(battery_id, id);
"""


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------- batteries
def create_battery(name: str, rated_capacity_ah: float,
                   safe_vmin: float, safe_vmax: float) -> dict:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO batteries(name, rated_capacity_ah, safe_vmin, safe_vmax, created_at)"
            " VALUES (?,?,?,?,?)",
            (name, rated_capacity_ah, safe_vmin, safe_vmax, time.time()),
        )
        battery_id = cur.lastrowid
    return get_battery(battery_id)


def get_battery(battery_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM batteries WHERE id=?", (battery_id,)).fetchone()
        return dict(row) if row else None


def list_batteries() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT b.*, "
            " (SELECT COUNT(*) FROM cycles c WHERE c.battery_id=b.id) AS cycle_count,"
            " (SELECT COUNT(*) FROM logs l WHERE l.battery_id=b.id) AS log_count"
            " FROM batteries b ORDER BY b.id"
        ).fetchall()
        return [dict(r) for r in rows]


def update_battery(battery_id: int, fields: dict) -> dict | None:
    allowed = {"name", "rated_capacity_ah", "safe_vmin", "safe_vmax"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if sets:
        clause = ", ".join(f"{k}=?" for k in sets)
        with connect() as conn:
            conn.execute(f"UPDATE batteries SET {clause} WHERE id=?",
                         (*sets.values(), battery_id))
    return get_battery(battery_id)


def delete_battery(battery_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM batteries WHERE id=?", (battery_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------- logs
def create_log(battery_id: int, filename: str, meta: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO logs(battery_id, filename, uploaded_at, row_count, bad_rows,"
            " duration_s) VALUES (?,?,?,?,?,?)",
            (battery_id, filename, time.time(), meta["row_count"],
             meta["bad_rows"], meta["duration_s"]),
        )
        return cur.lastrowid


def set_log_cycle_count(log_id: int, count: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE logs SET cycle_count=? WHERE id=?", (count, log_id))


def list_logs(battery_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM logs WHERE battery_id=? ORDER BY id", (battery_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_log(log_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM logs WHERE id=?", (log_id,)).fetchone()
        return dict(row) if row else None


def delete_log(log_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM logs WHERE id=?", (log_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------- samples / cycles
def insert_samples(log_id: int, samples: list[dict]) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO samples(log_id, t, voltage, current, temperature)"
            " VALUES (?,?,?,?,?)",
            [(log_id, s["t"], s["voltage"], s["current"], s["temperature"])
             for s in samples],
        )


def insert_cycles(battery_id: int, log_id: int, cycles: list[dict]) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO cycles(battery_id, log_id, seq, kind, start_t, end_t, duration_s,"
            " closed, capacity_ah, energy_wh, ir_ohm, v_start, v_end, v_min, v_min_t,"
            " v_max, i_avg, i_max, temp_start, temp_max, temp_max_t)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(battery_id, log_id, c["seq"], c["kind"], c["start_t"], c["end_t"],
              c["duration_s"], int(c["closed"]), c["capacity_ah"], c["energy_wh"],
              c.get("ir_ohm"), c["v_start"], c["v_end"], c["v_min"], c["v_min_t"],
              c["v_max"], c["i_avg"], c["i_max"], c["temp_start"], c["temp_max"],
              c["temp_max_t"]) for c in cycles],
        )


def list_cycles(battery_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.*, l.filename, l.uploaded_at FROM cycles c"
            " JOIN logs l ON l.id = c.log_id"
            " WHERE c.battery_id=? ORDER BY l.uploaded_at, c.log_id, c.seq",
            (battery_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["closed"] = bool(d["closed"])
            result.append(d)
        return result


def list_cycles_by_log(log_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cycles WHERE log_id=? ORDER BY seq", (log_id,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["closed"] = bool(d["closed"])
            result.append(d)
        return result
