"""SQLite storage for operational records: field reports and citizen-warning drafts.

SQLite is built into Python, so nothing needs to be installed. The database file is
data/kaskentra.db by default (set KASKENTRA_DB to change it; on Vercel it uses /tmp).
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def default_path() -> str:
    if os.environ.get("KASKENTRA_DB"):
        return os.environ["KASKENTRA_DB"]
    if os.environ.get("VERCEL"):
        return "/tmp/kaskentra.db"  # the only writable folder on Vercel
    return str(Path(__file__).resolve().parent.parent / "data" / "kaskentra.db")


DB_PATH = default_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS field_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    asset TEXT NOT NULL,
    hour INTEGER NOT NULL,
    hours INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    zone TEXT NOT NULL,
    message TEXT NOT NULL,
    scenario TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending approval',
    decided_by TEXT,
    decided_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_field_report(kind: str, asset: str, hour: int, hours: int) -> dict:
    with connect() as c:
        cur = c.execute("INSERT INTO field_reports (created_at, kind, asset, hour, hours) "
                        "VALUES (?, ?, ?, ?, ?)", (now(), kind, asset, hour, hours))
        return dict(c.execute("SELECT * FROM field_reports WHERE id = ?",
                              (cur.lastrowid,)).fetchone())


def list_field_reports(limit: int = 50) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM field_reports ORDER BY id DESC LIMIT ?", (limit,))]


def save_alert(zone: str, message: str, scenario: dict) -> dict:
    with connect() as c:
        cur = c.execute("INSERT INTO alerts (created_at, zone, message, scenario) VALUES (?, ?, ?, ?)",
                        (now(), zone, message, json.dumps(scenario, sort_keys=True)))
        return get_alert(cur.lastrowid, c)


def get_alert(alert_id: int, conn=None) -> dict | None:
    if conn is None:
        with connect() as c:
            return get_alert(alert_id, c)
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if row is None:
        return None
    alert = dict(row)
    alert["scenario"] = json.loads(alert["scenario"])
    return alert


def list_alerts(limit: int = 50) -> list[dict]:
    with connect() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM alerts ORDER BY id DESC LIMIT ?", (limit,))]
        return [get_alert(i, c) for i in ids]


def decide_alert(alert_id: int, decision: str, decided_by: str) -> dict | None:
    """Approve or reject a pending draft. Returns None if it is missing or already decided."""
    status = {"approve": "approved", "reject": "rejected"}[decision]
    with connect() as c:
        cur = c.execute("UPDATE alerts SET status = ?, decided_by = ?, decided_at = ? "
                        "WHERE id = ? AND status = 'pending approval'",
                        (status, decided_by, now(), alert_id))
        return get_alert(alert_id, c) if cur.rowcount else None
