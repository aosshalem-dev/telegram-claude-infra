#!/usr/bin/env python3
"""
write_todo.py — Manage per-project TODO items (unfinished work from sessions).

Storage: SQLite table 'todos' in tg_messages.db (shared infrastructure DB).

Usage:
    python write_todo.py --project food add --text "migrate voice files" --priority medium
    python write_todo.py --project food done --id 3
    python write_todo.py --project food cancel --id 5 --reason "no longer needed"
    python write_todo.py --project food list
    python write_todo.py --project food list --all  (include done/cancelled)
    python write_todo.py list-all                    (cross-project pending TODOs)
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DIR = Path(__file__).resolve().parent
DB_PATH = DIR / "tg_messages.db"
PROJECTS_DIR = DIR.parent.resolve()


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS todos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL,
        text TEXT NOT NULL,
        priority TEXT DEFAULT 'medium',
        status TEXT DEFAULT 'pending',
        bot TEXT,
        session TEXT,
        created_at TEXT NOT NULL,
        done_at TEXT,
        cancelled_at TEXT,
        cancel_reason TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_project ON todos(project)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status)")
    conn.commit()
    return conn


def _migrate_jsonl(project, conn):
    """One-time migration from JSONL to SQLite for a project."""
    jsonl_path = PROJECTS_DIR / project / "todos.jsonl"
    if not jsonl_path.exists():
        return
    migrated_marker = PROJECTS_DIR / project / ".todos_migrated"
    if migrated_marker.exists():
        return

    entries = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not entries:
        migrated_marker.write_text("migrated")
        return

    for e in entries:
        conn.execute(
            """INSERT INTO todos (project, text, priority, status, bot, session,
               created_at, done_at, cancelled_at, cancel_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project, e.get("text", ""), e.get("priority", "medium"),
             e.get("status", "pending"), e.get("bot"), e.get("session"),
             e.get("ts", datetime.now().isoformat(timespec="seconds")),
             e.get("done_at"), e.get("cancelled_at"), e.get("cancel_reason"))
        )
    conn.commit()
    migrated_marker.write_text(f"migrated {len(entries)} entries at {datetime.now().isoformat()}")
    print(f"  Migrated {len(entries)} TODOs from JSONL to SQLite for {project}")


def cmd_add(project, text, priority="medium", bot=None, session=None):
    conn = get_db()
    _migrate_jsonl(project, conn)

    cur = conn.execute(
        """INSERT INTO todos (project, text, priority, bot, session, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (project, text, priority, bot, session,
         datetime.now().isoformat(timespec="seconds"))
    )
    todo_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(f"TODO #{todo_id} added to {project}: {text}")


def cmd_done(project, todo_id):
    conn = get_db()
    _migrate_jsonl(project, conn)

    row = conn.execute("SELECT * FROM todos WHERE id=? AND project=?", (todo_id, project)).fetchone()
    if not row:
        # Try matching by old sequential id within project
        rows = conn.execute(
            "SELECT * FROM todos WHERE project=? ORDER BY id", (project,)
        ).fetchall()
        if todo_id <= len(rows):
            row = rows[todo_id - 1]
            todo_id = row["id"]

    if not row:
        print(f"TODO #{todo_id} not found in {project}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE todos SET status='done', done_at=? WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"), todo_id)
    )
    conn.commit()
    print(f"TODO #{todo_id} marked done: {row['text']}")
    conn.close()


def cmd_cancel(project, todo_id, reason=None):
    conn = get_db()
    _migrate_jsonl(project, conn)

    row = conn.execute("SELECT * FROM todos WHERE id=? AND project=?", (todo_id, project)).fetchone()
    if not row:
        print(f"TODO #{todo_id} not found in {project}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE todos SET status='cancelled', cancelled_at=?, cancel_reason=? WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"), reason, todo_id)
    )
    conn.commit()
    print(f"TODO #{todo_id} cancelled: {row['text']}")
    conn.close()


def cmd_list(project, pending_only=True):
    conn = get_db()
    _migrate_jsonl(project, conn)

    if pending_only:
        rows = conn.execute(
            "SELECT * FROM todos WHERE project=? AND status='pending' ORDER BY "
            "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 END, id",
            (project,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM todos WHERE project=? ORDER BY id", (project,)
        ).fetchall()
    conn.close()

    if not rows:
        label = "pending " if pending_only else ""
        print(f"No {label}TODOs for {project}")
        return

    label = "Pending" if pending_only else "All"
    print(f"{label} TODOs for {project} ({len(rows)}):")
    for r in rows:
        status = r["status"]
        marker = {"pending": "[ ]", "done": "[x]", "cancelled": "[-]"}.get(status, "[?]")
        ts = (r["created_at"] or "?")[:10]
        print(f"  {marker} #{r['id']} ({r['priority']}) {r['text']}  [{ts}]")


def cmd_list_all():
    """Cross-project: show all pending TODOs."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM todos WHERE status='pending' ORDER BY "
        "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 END, project, id"
    ).fetchall()
    conn.close()

    if not rows:
        print("No pending TODOs across all projects")
        return

    print(f"All pending TODOs ({len(rows)}):")
    current_project = None
    for r in rows:
        if r["project"] != current_project:
            current_project = r["project"]
            print(f"\n  [{current_project}]")
        ts = (r["created_at"] or "?")[:10]
        print(f"    #{r['id']} ({r['priority']}) {r['text']}  [{ts}]")


def main():
    parser = argparse.ArgumentParser(description="Manage project TODOs")
    parser.add_argument("--project", default=None, help="Project name")
    sub = parser.add_subparsers(dest="command")

    add_p = sub.add_parser("add")
    add_p.add_argument("--text", required=True)
    add_p.add_argument("--priority", default="medium", choices=["high", "medium", "low"])
    add_p.add_argument("--bot", default=None)
    add_p.add_argument("--session", default=None)

    done_p = sub.add_parser("done")
    done_p.add_argument("--id", type=int, required=True)

    cancel_p = sub.add_parser("cancel")
    cancel_p.add_argument("--id", type=int, required=True)
    cancel_p.add_argument("--reason", default=None)

    list_p = sub.add_parser("list")
    list_p.add_argument("--all", action="store_true", help="Show all including done/cancelled")

    sub.add_parser("list-all", help="Cross-project pending TODOs")

    args = parser.parse_args()

    if args.command == "list-all":
        cmd_list_all()
    elif args.command == "add":
        if not args.project:
            print("--project required for add", file=sys.stderr)
            sys.exit(1)
        cmd_add(args.project, args.text, args.priority, args.bot, args.session)
    elif args.command == "done":
        if not args.project:
            print("--project required for done", file=sys.stderr)
            sys.exit(1)
        cmd_done(args.project, args.id)
    elif args.command == "cancel":
        if not args.project:
            print("--project required for cancel", file=sys.stderr)
            sys.exit(1)
        cmd_cancel(args.project, args.id, getattr(args, "reason", None))
    elif args.command == "list":
        if not args.project:
            print("--project required for list", file=sys.stderr)
            sys.exit(1)
        cmd_list(args.project, pending_only=not getattr(args, "all", False))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
