#!/usr/bin/env python3
"""
write_suggestion.py — Manage per-project proactive suggestions (idle work).

Storage: SQLite table 'suggestions' in tg_messages.db (shared infrastructure DB).

Usage:
    python write_suggestion.py --project food add --text "add input validation to meal form"
    python write_suggestion.py --project food claim --id 2 --session SESSION_ID
    python write_suggestion.py --project food done --id 2
    python write_suggestion.py --project food list
    python write_suggestion.py --project food pick  (pick best unclaimed suggestion)
    python write_suggestion.py list-all              (cross-project open suggestions)

Guardrails (enforced at add/claim time):
- Only 1 active claim per session
- No external actions (suggestions are code-only: edits, tests, docs)
- Max scope: ~15 minutes of work
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

# Guardrail: forbidden keywords in suggestion text that imply external actions
FORBIDDEN_PATTERNS = [
    "deploy", "push to", "send email", "api call", "delete prod",
    "drop table", "force push", "cancel policy", "submit to",
]


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL,
        text TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        added_by_bot TEXT,
        added_by_session TEXT,
        claimed_by TEXT,
        claimed_at TEXT,
        done_at TEXT,
        result TEXT,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_project ON suggestions(project)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status)")
    conn.commit()
    return conn


def _migrate_jsonl(project, conn):
    """One-time migration from JSONL to SQLite for a project."""
    jsonl_path = PROJECTS_DIR / project / "suggestions.jsonl"
    if not jsonl_path.exists():
        return
    migrated_marker = PROJECTS_DIR / project / ".suggestions_migrated"
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
            """INSERT INTO suggestions (project, text, status, added_by_bot, added_by_session,
               claimed_by, claimed_at, done_at, result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project, e.get("text", ""), e.get("status", "open"),
             e.get("added_by_bot"), e.get("added_by_session"),
             e.get("claimed_by"), e.get("claimed_at"),
             e.get("done_at"), e.get("result"),
             e.get("ts", datetime.now().isoformat(timespec="seconds")))
        )
    conn.commit()
    migrated_marker.write_text(f"migrated {len(entries)} entries at {datetime.now().isoformat()}")
    print(f"  Migrated {len(entries)} suggestions from JSONL to SQLite for {project}")


def check_guardrails(text):
    text_lower = text.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in text_lower:
            print(f"GUARDRAIL: Suggestion contains forbidden pattern '{pattern}'. "
                  f"Suggestions must be code-only (edits, tests, docs). "
                  f"No external actions allowed.", file=sys.stderr)
            sys.exit(1)


def cmd_add(project, text, bot=None, session=None):
    check_guardrails(text)

    conn = get_db()
    _migrate_jsonl(project, conn)

    cur = conn.execute(
        """INSERT INTO suggestions (project, text, added_by_bot, added_by_session, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (project, text, bot, session,
         datetime.now().isoformat(timespec="seconds"))
    )
    suggestion_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(f"Suggestion #{suggestion_id} added to {project}: {text}")


def cmd_claim(project, suggestion_id, session_id):
    conn = get_db()
    _migrate_jsonl(project, conn)

    # Guardrail: check if this session already has an active claim
    existing = conn.execute(
        "SELECT * FROM suggestions WHERE claimed_by=? AND status='claimed'",
        (session_id,)
    ).fetchone()
    if existing:
        print(f"GUARDRAIL: Session {session_id} already has an active claim "
              f"(suggestion #{existing['id']}). Complete or release it first.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    row = conn.execute(
        "SELECT * FROM suggestions WHERE id=? AND project=?", (suggestion_id, project)
    ).fetchone()
    if not row:
        print(f"Suggestion #{suggestion_id} not found in {project}", file=sys.stderr)
        conn.close()
        sys.exit(1)
    if row["status"] != "open":
        print(f"Suggestion #{suggestion_id} is not open (status: {row['status']})", file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE suggestions SET status='claimed', claimed_by=?, claimed_at=? WHERE id=?",
        (session_id, datetime.now().isoformat(timespec="seconds"), suggestion_id)
    )
    conn.commit()
    print(f"Suggestion #{suggestion_id} claimed by {session_id}: {row['text']}")
    conn.close()


def cmd_done(project, suggestion_id, result=None):
    conn = get_db()
    _migrate_jsonl(project, conn)

    row = conn.execute(
        "SELECT * FROM suggestions WHERE id=? AND project=?", (suggestion_id, project)
    ).fetchone()
    if not row:
        print(f"Suggestion #{suggestion_id} not found in {project}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE suggestions SET status='done', done_at=?, result=? WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"), result, suggestion_id)
    )
    conn.commit()
    print(f"Suggestion #{suggestion_id} completed: {row['text']}")
    conn.close()


def cmd_release(project, suggestion_id):
    conn = get_db()
    _migrate_jsonl(project, conn)

    row = conn.execute(
        "SELECT * FROM suggestions WHERE id=? AND project=? AND status='claimed'",
        (suggestion_id, project)
    ).fetchone()
    if not row:
        print(f"Suggestion #{suggestion_id} not found or not claimed", file=sys.stderr)
        conn.close()
        return

    conn.execute(
        "UPDATE suggestions SET status='open', claimed_by=NULL, claimed_at=NULL WHERE id=?",
        (suggestion_id,)
    )
    conn.commit()
    print(f"Suggestion #{suggestion_id} released: {row['text']}")
    conn.close()


def cmd_pick(project):
    conn = get_db()
    _migrate_jsonl(project, conn)

    row = conn.execute(
        "SELECT * FROM suggestions WHERE project=? AND status='open' ORDER BY id LIMIT 1",
        (project,)
    ).fetchone()
    conn.close()

    if not row:
        print(f"No open suggestions for {project}")
        return

    print(f"Suggested work for {project}:")
    print(f"  #{row['id']}: {row['text']}")
    print(f"  Added: {(row['created_at'] or '?')[:16]}")
    print(f"\nTo claim: python write_suggestion.py --project {project} claim --id {row['id']} --session YOUR_SESSION")


def cmd_list(project, show_all=False):
    conn = get_db()
    _migrate_jsonl(project, conn)

    if show_all:
        rows = conn.execute(
            "SELECT * FROM suggestions WHERE project=? ORDER BY id", (project,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM suggestions WHERE project=? AND status IN ('open','claimed') ORDER BY id",
            (project,)
        ).fetchall()
    conn.close()

    if not rows:
        label = "active " if not show_all else ""
        print(f"No {label}suggestions for {project}")
        return

    print(f"Suggestions for {project} ({len(rows)}):")
    for r in rows:
        status = r["status"]
        marker = {"open": "[ ]", "claimed": "[~]", "done": "[x]"}.get(status, "[?]")
        ts = (r["created_at"] or "?")[:10]
        extra = ""
        if status == "claimed":
            extra = f" (claimed by {r['claimed_by'] or '?'})"
        print(f"  {marker} #{r['id']} {r['text']}  [{ts}]{extra}")


def cmd_list_all():
    """Cross-project: show all open suggestions."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM suggestions WHERE status='open' ORDER BY project, id"
    ).fetchall()
    conn.close()

    if not rows:
        print("No open suggestions across all projects")
        return

    print(f"All open suggestions ({len(rows)}):")
    current_project = None
    for r in rows:
        if r["project"] != current_project:
            current_project = r["project"]
            print(f"\n  [{current_project}]")
        ts = (r["created_at"] or "?")[:10]
        print(f"    #{r['id']} {r['text']}  [{ts}]")


def main():
    parser = argparse.ArgumentParser(description="Manage project suggestions for idle work")
    parser.add_argument("--project", default=None, help="Project name")
    sub = parser.add_subparsers(dest="command")

    add_p = sub.add_parser("add")
    add_p.add_argument("--text", required=True)
    add_p.add_argument("--bot", default=None)
    add_p.add_argument("--session", default=None)

    claim_p = sub.add_parser("claim")
    claim_p.add_argument("--id", type=int, required=True)
    claim_p.add_argument("--session", required=True)

    done_p = sub.add_parser("done")
    done_p.add_argument("--id", type=int, required=True)
    done_p.add_argument("--result", default=None)

    sub.add_parser("pick")

    release_p = sub.add_parser("release")
    release_p.add_argument("--id", type=int, required=True)

    list_p = sub.add_parser("list")
    list_p.add_argument("--all", action="store_true")

    sub.add_parser("list-all", help="Cross-project open suggestions")

    args = parser.parse_args()

    if args.command == "list-all":
        cmd_list_all()
    elif args.command == "add":
        if not args.project:
            print("--project required for add", file=sys.stderr)
            sys.exit(1)
        cmd_add(args.project, args.text, args.bot, args.session)
    elif args.command == "claim":
        if not args.project:
            print("--project required for claim", file=sys.stderr)
            sys.exit(1)
        cmd_claim(args.project, args.id, args.session)
    elif args.command == "done":
        if not args.project:
            print("--project required for done", file=sys.stderr)
            sys.exit(1)
        cmd_done(args.project, args.id, getattr(args, "result", None))
    elif args.command == "release":
        if not args.project:
            print("--project required for release", file=sys.stderr)
            sys.exit(1)
        cmd_release(args.project, args.id)
    elif args.command == "pick":
        if not args.project:
            print("--project required for pick", file=sys.stderr)
            sys.exit(1)
        cmd_pick(args.project)
    elif args.command == "list":
        if not args.project:
            print("--project required for list", file=sys.stderr)
            sys.exit(1)
        cmd_list(args.project, show_all=getattr(args, "all", False))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
