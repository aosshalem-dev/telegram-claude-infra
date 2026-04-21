#!/usr/bin/env python3
"""
write_insight.py — Append a session insight to a per-project insights file.

Usage:
    python write_insight.py --project food --type gotcha --text "DB has locked column"
    python write_insight.py --project telegram --type pattern --text "voice messages need language param"
    python write_insight.py --project food --type fix --text "use ogg not wav for voice"

Types: gotcha, pattern, fix, preference, warning, tip
File: projects/<project>/insights.jsonl
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path(__file__).parent.parent.resolve()  # persistent-team/projects/


def get_insights_path(project):
    """Get the insights.jsonl path for a project."""
    path = PROJECTS_DIR / project / "insights.jsonl"
    return path


def write_insight(project, insight_type, text, session_id=None, bot=None):
    """Append an insight to the project's insights.jsonl."""
    path = get_insights_path(project)

    # Ensure project dir exists
    if not path.parent.exists():
        print(f"Error: Project directory not found: {path.parent}", file=sys.stderr)
        sys.exit(1)

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "type": insight_type,
        "text": text,
    }
    if session_id:
        entry["session"] = session_id
    if bot:
        entry["bot"] = bot

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"Insight written to {path}")
    return path


def main():
    parser = argparse.ArgumentParser(description="Write a session insight")
    parser.add_argument("--project", required=True, help="Project name (e.g., food, telegram)")
    parser.add_argument("--type", default="tip", choices=["gotcha", "pattern", "fix", "preference", "warning", "tip"],
                        help="Insight type (default: tip)")
    parser.add_argument("--text", required=True, help="The insight text")
    parser.add_argument("--session", default=None, help="Session ID (optional)")
    parser.add_argument("--bot", default=None, help="Bot key (optional)")
    args = parser.parse_args()

    write_insight(args.project, args.type, args.text, args.session, args.bot)


if __name__ == "__main__":
    main()
