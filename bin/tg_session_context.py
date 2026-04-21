"""
Session Context — live reflection system for Telegram bot sessions.

Distinguishes the messy creation process from the distilled product + insights.
Sessions write context during work; new/promoted sessions read it on startup.

Storage: tg_cmd/{bot}/session_context.json

Usage (from a session):
    from tg_session_context import save_context, load_context, format_context_for_prompt

    # Read previous session's context on startup
    ctx = load_context("example_project")
    if ctx:
        print(format_context_for_prompt(ctx))

    # Save context after completing work
    save_context("example_project", {
        "current_task": "Adding RTL support to dictionary",
        "status": "in_progress",
        "artifacts": ["example_project/index.html - dictionary panel added"],
        "pending": ["Fix nikud toggle persistence"],
        "insights": ["Arabic diacritics need special Unicode handling"],
        "user_preferences": ["Compact layout, no animations"],
    })

CLI:
    python tg_session_context.py example_project              # show context
    python tg_session_context.py --all                 # show all contexts
    python tg_session_context.py example_project --clear       # clear context
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

DIR = Path(__file__).resolve().parent
CMD_DIR = DIR / "tg_cmd"


def _context_path(bot_key):
    """Path to session_context.json for a bot."""
    d = CMD_DIR / bot_key
    d.mkdir(parents=True, exist_ok=True)
    return d / "session_context.json"


def save_context(bot_key, context_dict):
    """Save session context for a bot.

    context_dict should have:
        current_task: str - what the session is working on
        status: str - "in_progress" | "completed" | "blocked" | "idle"
        resume_instructions: str - explicit instructions for the NEXT session on what to do
            (e.g. "Continue updating the site CSS. File is X. User asked for Y.")
            This is the most important field for handoff continuity.
        artifacts: list[str] - files/features created or modified (with paths)
        pending: list[str] - things left to do
        insights: list[str] - what was learned (failures, workarounds, preferences)
        user_preferences: list[str] - user taste/style preferences discovered
        last_messages: list[str] - last 3-5 user messages (for continuity)
        error_log: list[str] - errors encountered and how they were resolved
    """
    path = _context_path(bot_key)

    # Merge with existing context (don't overwrite, accumulate insights)
    existing = load_context(bot_key) or {}

    # Accumulate insights (dedup)
    old_insights = set(existing.get("insights", []))
    new_insights = context_dict.get("insights", [])
    merged_insights = list(old_insights | set(new_insights))

    old_prefs = set(existing.get("user_preferences", []))
    new_prefs = context_dict.get("user_preferences", [])
    merged_prefs = list(old_prefs | set(new_prefs))

    # Keep last N errors (rolling window)
    old_errors = existing.get("error_log", [])
    new_errors = context_dict.get("error_log", [])
    merged_errors = (old_errors + new_errors)[-10:]  # keep last 10

    # Build merged context
    merged = {
        "bot": bot_key,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "session_id": context_dict.get("session_id", existing.get("session_id")),
        "current_task": context_dict.get("current_task", existing.get("current_task")),
        "status": context_dict.get("status", "in_progress"),
        "resume_instructions": context_dict.get("resume_instructions", existing.get("resume_instructions")),
        "artifacts": context_dict.get("artifacts", existing.get("artifacts", [])),
        "pending": context_dict.get("pending", existing.get("pending", [])),
        "insights": merged_insights[-20:],  # cap at 20
        "user_preferences": merged_prefs[-15:],  # cap at 15
        "last_messages": context_dict.get("last_messages", existing.get("last_messages", []))[-5:],
        "error_log": merged_errors,
        "history": _update_history(existing, context_dict),
    }

    # Atomic write
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return merged


def _update_history(existing, new_ctx):
    """Keep a brief history of task transitions."""
    history = existing.get("history", [])
    old_task = existing.get("current_task")
    new_task = new_ctx.get("current_task")

    if old_task and new_task and old_task != new_task:
        history.append({
            "task": old_task,
            "status": existing.get("status", "unknown"),
            "ended_at": datetime.now().isoformat(timespec="seconds"),
        })

    return history[-10:]  # keep last 10 transitions


def load_context(bot_key):
    """Load session context for a bot. Returns dict or None."""
    path = _context_path(bot_key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return None


def format_context_for_prompt(ctx):
    """Format context into a concise prompt injection for a new session.

    This is what a new/promoted session reads to understand what happened before.
    """
    if not ctx:
        return ""

    lines = ["## Previous Session Context"]
    lines.append(f"Last updated: {ctx.get('updated_at', '?')}")

    task = ctx.get("current_task")
    status = ctx.get("status", "?")
    resume = ctx.get("resume_instructions")
    if task:
        lines.append(f"\n**Current task:** {task} (status: {status})")

    # Auto-continue directive — the most important part of handoff
    if resume:
        lines.append(f"\n**⚡ Resume instructions (from previous session):** {resume}")
    if status in ("in_progress", "blocked") and (resume or task):
        lines.append("\n**⚠️ AUTO-CONTINUE MANDATORY:** The previous session was mid-work. "
                     "After sending Connected, immediately continue this work. "
                     "Do NOT ask the user what to do — the instructions above tell you. "
                     "Send a brief update: 'Continuing: <task description>'.")

    artifacts = ctx.get("artifacts", [])
    if artifacts:
        lines.append("\n**Artifacts (files/features that exist):**")
        for a in artifacts[-10:]:
            lines.append(f"- {a}")

    pending = ctx.get("pending", [])
    if pending:
        lines.append("\n**Pending (left to do):**")
        for p in pending[-5:]:
            lines.append(f"- {p}")

    insights = ctx.get("insights", [])
    if insights:
        lines.append("\n**Insights (learned from previous work):**")
        for i in insights[-10:]:
            lines.append(f"- {i}")

    prefs = ctx.get("user_preferences", [])
    if prefs:
        lines.append("\n**User preferences discovered:**")
        for p in prefs[-5:]:
            lines.append(f"- {p}")

    msgs = ctx.get("last_messages", [])
    if msgs:
        lines.append("\n**Last user messages:**")
        for m in msgs:
            lines.append(f"- {m[:150]}")

    errors = ctx.get("error_log", [])
    if errors:
        lines.append("\n**Recent errors/issues:**")
        for e in errors[-3:]:
            lines.append(f"- {e}")

    history = ctx.get("history", [])
    if history:
        lines.append("\n**Task history:**")
        for h in history[-5:]:
            lines.append(f"- {h.get('task', '?')} → {h.get('status', '?')}")

    return "\n".join(lines)


def clear_context(bot_key):
    """Clear session context for a bot."""
    path = _context_path(bot_key)
    if path.exists():
        path.unlink()
        return True
    return False


def get_all_contexts():
    """Get contexts for all bots that have one."""
    contexts = {}
    if CMD_DIR.exists():
        for d in sorted(CMD_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                ctx = load_context(d.name)
                if ctx:
                    contexts[d.name] = ctx
    return contexts


# --- CLI ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Session context manager")
    parser.add_argument("bot", nargs="?", help="Bot key")
    parser.add_argument("--all", action="store_true", help="Show all contexts")
    parser.add_argument("--clear", action="store_true", help="Clear context")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    if args.all:
        contexts = get_all_contexts()
        if args.json:
            print(json.dumps(contexts, indent=2, ensure_ascii=False))
        else:
            if not contexts:
                print("No session contexts found.")
            for bot, ctx in contexts.items():
                print(f"\n{'='*50}")
                print(format_context_for_prompt(ctx))
    elif args.bot:
        if args.clear:
            if clear_context(args.bot):
                print(f"Cleared context for {args.bot}")
            else:
                print(f"No context found for {args.bot}")
        else:
            ctx = load_context(args.bot)
            if ctx:
                if args.json:
                    print(json.dumps(ctx, indent=2, ensure_ascii=False))
                else:
                    print(format_context_for_prompt(ctx))
            else:
                print(f"No context for {args.bot}")
    else:
        # Show summary of all contexts
        contexts = get_all_contexts()
        if not contexts:
            print("No session contexts found.")
        else:
            print(f"Session contexts ({len(contexts)}):\n")
            for bot, ctx in contexts.items():
                task = ctx.get("current_task", "?")[:60]
                status = ctx.get("status", "?")
                updated = ctx.get("updated_at", "?")[:19]
                n_insights = len(ctx.get("insights", []))
                n_artifacts = len(ctx.get("artifacts", []))
                print(f"  {bot:25s} {status:12s} {n_artifacts}art {n_insights}ins  {task}")
