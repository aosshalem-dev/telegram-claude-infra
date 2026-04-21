"""Shared DB utility functions for tg_relay.py (legacy wait_notify path)."""
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).resolve().parent / "tg_messages.db"


def get_unread(bot_key):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    rows = conn.execute(
        "SELECT * FROM messages WHERE bot=? AND read_time IS NULL AND direction='in' "
        "ORDER BY received_time", (bot_key,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_read(bot_key, ids=None):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    now = datetime.now().isoformat(timespec="milliseconds")
    if ids:
        for mid in ids:
            conn.execute("UPDATE messages SET read_time=? WHERE id=?", (now, mid))
    else:
        conn.execute(
            "UPDATE messages SET read_time=? WHERE bot=? AND read_time IS NULL",
            (now, bot_key))
    conn.commit()
    conn.close()
