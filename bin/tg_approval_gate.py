#!/usr/bin/env python3
"""
Telegram Approval Gate — requires human approval for file modifications.

When a Telegram bot wants to modify files (Edit, Write, Delete, Git), this gate:
1. Sends approval request to the user via Telegram
2. Waits for reply ("approve" / "deny")
3. Allows/blocks the operation based on response

Usage:
    This is called automatically by the daemon when processing Telegram messages.
    To bypass for trusted bots, add "skip_approval_gate": true in telegram_bots.json

Approval flow:
    Bot: "I want to edit config.py to add timeout parameter"
    → Gate sends to user: "Approve file edit? config.py (add timeout). Reply: approve/deny"
    → User replies: "approve"
    → Gate allows operation
"""

import json
import time
import sys
from pathlib import Path
from datetime import datetime

DIR = Path(__file__).resolve().parent

# Operation types that require approval
APPROVAL_REQUIRED = {
    "Edit", "Write", "NotebookEdit", "Bash",
    # Git operations via Bash are caught by content inspection
}

# Bypass approval for read-only operations
SAFE_OPERATIONS = {
    "Read", "Glob", "Grep", "WebFetch", "WebSearch",
    "Task", "AskUserQuestion", "TodoWrite",
}

def approval_path(bot_name):
    return DIR / f"tg_approval_{bot_name}.json"

def requires_approval(tool_name, tool_params=None):
    """Check if this tool call requires human approval."""
    if tool_name in SAFE_OPERATIONS:
        return False

    if tool_name in APPROVAL_REQUIRED:
        # Special case: Bash commands for git read operations are safe
        if tool_name == "Bash" and tool_params:
            cmd = tool_params.get("command", "")
            # Allow read-only git commands
            safe_git = ["git status", "git diff", "git log", "git show", "git branch"]
            if any(cmd.strip().startswith(safe) for safe in safe_git):
                return False
        return True

    return False

def create_approval_request(bot_name, tool_name, tool_params, reason=""):
    """Create an approval request file for the user to review."""
    request = {
        "bot": bot_name,
        "tool": tool_name,
        "params": tool_params,
        "reason": reason,
        "requested_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
        "response": None,
        "responded_at": None,
    }

    approval_path(bot_name).write_text(
        json.dumps(request, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    return request

def format_approval_message(request):
    """Format approval request for Telegram message."""
    tool = request["tool"]
    params = request.get("params", {})
    reason = request.get("reason", "")

    # Build human-readable description
    if tool == "Edit":
        file_path = params.get("file_path", "?")
        desc = f"Edit file: {Path(file_path).name}"
    elif tool == "Write":
        file_path = params.get("file_path", "?")
        desc = f"Create/overwrite file: {Path(file_path).name}"
    elif tool == "NotebookEdit":
        nb_path = params.get("notebook_path", "?")
        desc = f"Edit notebook: {Path(nb_path).name}"
    elif tool == "Bash":
        cmd = params.get("command", "")[:100]
        desc = f"Run command: {cmd}"
    else:
        desc = f"{tool} operation"

    msg = f"🔒 APPROVAL REQUIRED\n\n{desc}"
    if reason:
        msg += f"\n\nReason: {reason}"
    msg += "\n\nReply: 'approve' or 'deny'"

    return msg

def wait_for_approval(bot_name, timeout_sec=300):
    """Wait for user approval. Returns True if approved, False if denied/timeout."""
    approval_file = approval_path(bot_name)

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not approval_file.exists():
            # User deleted the file = implicit denial
            return False

        try:
            request = json.loads(approval_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            time.sleep(1)
            continue

        status = request.get("status")
        if status == "approved":
            return True
        elif status == "denied":
            return False

        time.sleep(1)

    # Timeout = denial
    return False

def approve_request(bot_name):
    """Mark pending approval as approved."""
    approval_file = approval_path(bot_name)
    if not approval_file.exists():
        return False

    try:
        request = json.loads(approval_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    request["status"] = "approved"
    request["responded_at"] = datetime.now().isoformat(timespec="seconds")

    approval_file.write_text(
        json.dumps(request, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    return True

def deny_request(bot_name):
    """Mark pending approval as denied."""
    approval_file = approval_path(bot_name)
    if not approval_file.exists():
        return False

    try:
        request = json.loads(approval_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    request["status"] = "denied"
    request["responded_at"] = datetime.now().isoformat(timespec="seconds")

    approval_file.write_text(
        json.dumps(request, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    return True

def check_approval_reply(message_text):
    """Check if message is an approval/denial reply. Returns 'approve', 'deny', or None."""
    text = message_text.lower().strip()

    if text in ("approve", "approved", "yes", "ok", "go ahead", "אישור", "כן"):
        return "approve"
    elif text in ("deny", "denied", "no", "cancel", "stop", "סירוב", "לא"):
        return "deny"

    return None

if __name__ == "__main__":
    # CLI for testing
    import argparse
    parser = argparse.ArgumentParser(description="Telegram approval gate CLI")
    parser.add_argument("--bot", required=True, help="Bot name")
    parser.add_argument("--approve", action="store_true", help="Approve pending request")
    parser.add_argument("--deny", action="store_true", help="Deny pending request")
    parser.add_argument("--status", action="store_true", help="Show pending request")
    args = parser.parse_args()

    if args.approve:
        if approve_request(args.bot):
            print(f"Approved request for {args.bot}")
        else:
            print(f"No pending request for {args.bot}")
    elif args.deny:
        if deny_request(args.bot):
            print(f"Denied request for {args.bot}")
        else:
            print(f"No pending request for {args.bot}")
    elif args.status:
        approval_file = approval_path(args.bot)
        if approval_file.exists():
            request = json.loads(approval_file.read_text(encoding="utf-8"))
            print(json.dumps(request, indent=2))
        else:
            print(f"No pending request for {args.bot}")
