#!/usr/bin/env python3
"""
read_insights.py — Read recent insights for a project at session startup.

Usage:
    python read_insights.py --project food              # last 20 insights
    python read_insights.py --project food --last 10    # last 10
    python read_insights.py --project food --all        # all insights
    python read_insights.py --project food --days 3     # last 3 days only
    python read_insights.py --project food --since 2026-03-05  # since specific date
    python read_insights.py --project food --json       # JSON output

File: projects/<project>/insights.jsonl
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECTS_DIR = Path(__file__).parent.parent.resolve()  # persistent-team/projects/


def get_insights_path(project):
    """Get the insights.jsonl path for a project."""
    return PROJECTS_DIR / project / "insights.jsonl"


def read_insights(project, last_n=20, days=None, since=None):
    """Read insights from a project's insights.jsonl.

    Returns list of dicts, most recent last.
    """
    path = get_insights_path(project)
    if not path.exists():
        return []

    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError:
                continue

    # Filter by --since date (e.g. "2026-03-05" or "2026-03-05T10:00")
    if since is not None:
        entries = [e for e in entries if e.get("ts", "") >= since]

    # Filter by days if specified
    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        entries = [e for e in entries if e.get("ts", "") >= cutoff]

    # Return last N (only when no date filter is active)
    if last_n and not days and not since:
        entries = entries[-last_n:]

    return entries


def format_insight(entry):
    """Format a single insight for display."""
    ts = entry.get("ts", "?")[:16]  # YYYY-MM-DDTHH:MM
    itype = entry.get("type", "?")
    text = entry.get("text", "")
    bot = entry.get("bot", "")
    bot_str = f" [{bot}]" if bot else ""
    return f"  [{ts}] ({itype}){bot_str} {text}"


def main():
    parser = argparse.ArgumentParser(description="Read project insights")
    parser.add_argument("--project", required=True, help="Project name")
    parser.add_argument("--last", type=int, default=20, help="Number of recent insights (default: 20)")
    parser.add_argument("--days", type=int, default=None, help="Only insights from last N days")
    parser.add_argument("--since", type=str, default=None, help="Only insights since date (e.g. 2026-03-05 or 2026-03-05T10:00)")
    parser.add_argument("--all", action="store_true", help="Show all insights")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--count", action="store_true", help="Just show count")
    args = parser.parse_args()

    last_n = None if args.all else args.last
    entries = read_insights(args.project, last_n=last_n, days=args.days, since=args.since)

    if args.count:
        total_path = get_insights_path(args.project)
        if total_path.exists():
            with open(total_path) as f:
                total = sum(1 for line in f if line.strip())
        else:
            total = 0
        print(f"{args.project}: {len(entries)} shown / {total} total insights")
        return

    if not entries:
        print(f"No insights found for project '{args.project}'")
        return

    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return

    print(f"Recent insights for '{args.project}' ({len(entries)} entries):")
    print()
    for entry in entries:
        print(format_insight(entry))


if __name__ == "__main__":
    main()
