"""
Session State Detector — single source of truth for bot session state.

Three states:
    WAITING  — session is alive, idle, waiting for messages (in session_wait)
    BUSY     — session is alive, actively working (processing a message, running tools)
    DEAD     — session is not responding, needs replacement

Usage:
    from tg_session_state import get_state, get_all_states
    state = get_state("telegram")      # -> "WAITING" | "BUSY" | "DEAD"
    all_states = get_all_states()       # -> {"telegram": "BUSY", "food": "DEAD", ...}

    # CLI:
    python tg_session_state.py                  # show all bot states
    python tg_session_state.py telegram         # show single bot state
    python tg_session_state.py --json           # JSON output

Detection uses THREE independent signals (any fresh signal = alive):
    1. gate_state mtime  — tool calls update gate_state/*.json every few seconds
    2. cmd.txt consumption — session_wait consumes cmd.txt commands within seconds
    3. claude_heartbeat   — AI writes heartbeat to DB during idle session_wait loops

State logic:
    if no signal is fresh → DEAD
    if gate_state updated recently (< 5 min) → BUSY (actively running tools)
    if cmd.txt was consumed recently OR heartbeat is fresh → WAITING
    if busy.txt exists AND gate_state is fresh → BUSY
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DIR = Path(__file__).resolve().parent
GATE_STATE_DIR = DIR.parent.parent / "session_tracking" / "gate_state"
CMD_DIR = DIR / "tg_cmd"
TG_DB = DIR / "tg_messages.db"

# Bots that run on Windows — all others run on Mac via tmux
WINDOWS_ONLY_BOTS = {"telegram", "gemini_search"}

# Mac connection
MAC_HOST = "USER@HOST"
TMUX = "/opt/homebrew/bin/tmux"

# Thresholds (seconds)
GATE_STATE_FRESH = 300       # 5 min — tool call activity (must match poller's TRANSCRIPT_ALIVE_SECONDS)
CMD_STALE_NORMAL = 60        # 1 min — unconsumed CHECK_MESSAGES = dead
CMD_STALE_BUSY = 180         # 3 min — doubled when busy.txt exists
HEARTBEAT_FRESH = 180        # 3 min — last_heartbeat in DB (updated every ~60s by session_wait)
BUSY_TXT_FRESH = 120         # 2 min — busy.txt mtime (matches gate_state threshold)

# Commands that a live session should consume quickly
DEATH_SIGNAL_CMDS = {"CHECK_MESSAGES"}
TERMINAL_CMDS = {"DECLARED_DEAD", "PROMOTE"}


def _get_gate_state_age(bot_key):
    """Return seconds since last gate_state update for this bot, or None if not found."""
    try:
        if not GATE_STATE_DIR.exists():
            return None
        now = time.time()
        best_age = None
        for gs_file in GATE_STATE_DIR.iterdir():
            if not gs_file.name.endswith(".json") or gs_file.name.startswith("_"):
                continue
            try:
                mtime = gs_file.stat().st_mtime
                age = now - mtime
                if age > GATE_STATE_FRESH:
                    continue  # Skip stale files (optimization)
                data = json.loads(gs_file.read_text(encoding="utf-8"))
                if data.get("telegram_bot") == bot_key:
                    if best_age is None or age < best_age:
                        best_age = age
            except Exception:
                continue
        return best_age
    except Exception:
        return None


def _get_cmd_info(bot_key):
    """Return (command, age_seconds, is_terminal) for bot's cmd.txt, or None."""
    cmd_file = CMD_DIR / bot_key / "cmd.txt"
    if not cmd_file.exists():
        return None

    try:
        content = cmd_file.read_text(encoding="utf-8").strip()
        if not content:
            return None

        lines = content.split("\n")
        command = lines[0].strip()

        # Parse timestamp
        timestamp = None
        if len(lines) > 1:
            try:
                timestamp = datetime.fromisoformat(lines[1].strip())
            except (ValueError, IndexError):
                pass
        if timestamp is None:
            timestamp = datetime.fromtimestamp(cmd_file.stat().st_mtime)

        age = (datetime.now() - timestamp).total_seconds()
        is_terminal = command in TERMINAL_CMDS
        is_death_signal = command in DEATH_SIGNAL_CMDS

        return {
            "command": command,
            "age": age,
            "is_terminal": is_terminal,
            "is_death_signal": is_death_signal,
        }
    except Exception:
        return None


def _get_heartbeat_age(bot_key):
    """Return seconds since last heartbeat for this bot, or None.
    Uses last_heartbeat (updated every ~60s by session_wait budget cycle).
    claude_heartbeat is no longer updated (keepalive system removed 2026-03-13)."""
    if not TG_DB.exists():
        return None
    try:
        conn = sqlite3.connect(str(TG_DB), timeout=2)
        row = conn.execute(
            """SELECT last_heartbeat FROM sessions
               WHERE bot=? AND last_heartbeat IS NOT NULL
               ORDER BY last_heartbeat DESC LIMIT 1""",
            (bot_key,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            hb_time = datetime.fromisoformat(row[0])
            return (datetime.now() - hb_time).total_seconds()
    except Exception:
        pass
    return None


def _has_busy_file(bot_key):
    """Check if busy.txt exists and is recent."""
    busy_file = CMD_DIR / bot_key / "busy.txt"
    if not busy_file.exists():
        return False
    try:
        age = time.time() - busy_file.stat().st_mtime
        return age < BUSY_TXT_FRESH
    except Exception:
        return False


def _is_mac_session_alive(bot_key):
    """Check if a Mac bot's tmux session has a live Claude process.
    Just having a tmux session is NOT enough — Claude must be running inside it.
    Returns True/False, or None if SSH failed (fall back to old method)."""
    import subprocess
    tmux_name = f"tg_{bot_key}"
    try:
        if sys.platform == "darwin":
            # Running on Mac — check directly
            # Step 1: tmux session exists?
            result = subprocess.run(
                [TMUX, "has-session", "-t", f"={tmux_name}"],
                capture_output=True, timeout=5)
            if result.returncode != 0:
                return False  # No tmux session at all
            # Step 2: Check if Claude (node) is running inside the pane
            result = subprocess.run(
                [TMUX, "list-panes", "-t", f"={tmux_name}", "-F", "#{pane_current_command}"],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                cmd = result.stdout.strip()
                # Claude runs as 'node' or 'claude'. Shell prompts (bash/zsh) mean Claude exited.
                if cmd in ("bash", "zsh", "sh", "fish", "login"):
                    return False  # Zombie tmux — Claude has exited
            return True
        else:
            # Running on Windows — check via SSH
            # Check both tmux existence AND pane command in one SSH call
            check_cmd = (
                f"{TMUX} has-session -t '={tmux_name}' 2>/dev/null && "
                f"{TMUX} list-panes -t '={tmux_name}' -F '#{{pane_current_command}}' 2>/dev/null || echo DEAD"
            )
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
                 MAC_HOST, check_cmd],
                capture_output=True, text=True, timeout=8)
            if result.returncode == 0:
                output = result.stdout.strip()
                if output == "DEAD":
                    return False  # No tmux session
                # output is the pane command (e.g., "node", "bash")
                if output in ("bash", "zsh", "sh", "fish", "login"):
                    return False  # Zombie tmux — Claude has exited
                return True
    except Exception:
        pass
    return None  # SSH failed — caller should fall back


def get_state(bot_key):
    """
    Determine session state for a bot.

    Returns: "WAITING" | "BUSY" | "DEAD"

    For Mac bots: tmux session existence is the source of truth.
    For Windows bots: uses gate_state, cmd.txt, and heartbeat signals.

    Decision tree (Windows bots):
    1. cmd.txt says DECLARED_DEAD → DEAD (already declared)
    2. gate_state updated < 5 min → BUSY (tools running)
    3. cmd.txt is a death signal AND stale beyond threshold:
       a. heartbeat is fresh → WAITING (idle but alive)
       b. no heartbeat → DEAD
    4. cmd.txt is NOT a death signal (e.g. keepalive_at, countdown) → WAITING
    5. No cmd.txt at all:
       a. gate_state fresh → BUSY
       b. heartbeat fresh → WAITING
       c. nothing → DEAD
    """
    # Mac bots: tmux is source of truth
    if bot_key not in WINDOWS_ONLY_BOTS:
        alive = _is_mac_session_alive(bot_key)
        if alive is not None:
            if not alive:
                return "DEAD"
            # tmux session exists — check if BUSY or WAITING
            gate_age = _get_gate_state_age(bot_key)
            if gate_age is not None and gate_age < GATE_STATE_FRESH:
                return "BUSY"
            return "WAITING"
        # SSH failed — fall through to old method as fallback
    cmd = _get_cmd_info(bot_key)
    gate_age = _get_gate_state_age(bot_key)
    hb_age = _get_heartbeat_age(bot_key)
    busy = _has_busy_file(bot_key)

    gate_fresh = gate_age is not None and gate_age < GATE_STATE_FRESH
    hb_fresh = hb_age is not None and hb_age < HEARTBEAT_FRESH

    # 1. Already declared dead and no fresh life signals
    if cmd and cmd["is_terminal"]:
        if gate_fresh:
            return "BUSY"  # Session came alive after being declared dead (rare but possible)
        if hb_fresh:
            return "WAITING"
        return "DEAD"

    # 2. Gate state is fresh → session is actively working
    if gate_fresh:
        return "BUSY"

    # 3. cmd.txt has a death-signal command (CHECK_MESSAGES, KEEPALIVE)
    if cmd and cmd["is_death_signal"]:
        threshold = CMD_STALE_BUSY if busy else CMD_STALE_NORMAL
        if cmd["age"] < threshold:
            return "WAITING"  # Command is fresh, session may be about to consume it
        # Command is stale — check heartbeat as last resort
        if hb_fresh:
            return "WAITING"
        return "DEAD"

    # 4. cmd.txt has a non-death command (keepalive_at, countdown, etc.)
    if cmd and not cmd["is_death_signal"] and not cmd["is_terminal"]:
        # These commands don't need quick consumption — session is in a wait state
        if hb_fresh:
            return "WAITING"
        # No heartbeat, but command isn't a death signal — could be waiting
        # Check if the command is very old (> 10 min with no heartbeat = dead)
        if cmd["age"] > HEARTBEAT_FRESH:
            return "DEAD"
        return "WAITING"

    # 5. No cmd.txt at all
    if hb_fresh:
        return "WAITING"
    return "DEAD"


def get_state_detail(bot_key):
    """Get state with full diagnostic info."""
    cmd = _get_cmd_info(bot_key)
    gate_age = _get_gate_state_age(bot_key)
    hb_age = _get_heartbeat_age(bot_key)
    busy = _has_busy_file(bot_key)
    state = get_state(bot_key)

    return {
        "bot": bot_key,
        "state": state,
        "signals": {
            "gate_state_age": round(gate_age, 1) if gate_age is not None else None,
            "gate_state_fresh": gate_age is not None and gate_age < GATE_STATE_FRESH,
            "cmd_command": cmd["command"] if cmd else None,
            "cmd_age": round(cmd["age"], 1) if cmd else None,
            "heartbeat_age": round(hb_age, 1) if hb_age is not None else None,
            "heartbeat_fresh": hb_age is not None and hb_age < HEARTBEAT_FRESH,
            "busy_file": busy,
        },
    }


def get_all_states():
    """Get state for all bots that have a cmd directory."""
    states = {}
    if CMD_DIR.exists():
        for d in sorted(CMD_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                states[d.name] = get_state(d.name)
    return states


def get_all_states_detail():
    """Get detailed state for all bots."""
    details = {}
    if CMD_DIR.exists():
        for d in sorted(CMD_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                details[d.name] = get_state_detail(d.name)
    return details


# --- CLI ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Session state detector")
    parser.add_argument("bot", nargs="?", help="Bot key (omit for all)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--detail", action="store_true", help="Show diagnostic details")
    args = parser.parse_args()

    if args.bot:
        if args.detail or args.json:
            info = get_state_detail(args.bot)
            if args.json:
                print(json.dumps(info, indent=2))
            else:
                print(f"{info['bot']}: {info['state']}")
                for k, v in info["signals"].items():
                    print(f"  {k}: {v}")
        else:
            print(get_state(args.bot))
    else:
        if args.detail:
            details = get_all_states_detail()
            if args.json:
                print(json.dumps(details, indent=2))
            else:
                for bot, info in details.items():
                    s = info["signals"]
                    gate = f"gate={s['gate_state_age']:.0f}s" if s['gate_state_age'] else "gate=∅"
                    hb = f"hb={s['heartbeat_age']:.0f}s" if s['heartbeat_age'] else "hb=∅"
                    cmd = f"cmd={s['cmd_command']}({s['cmd_age']:.0f}s)" if s['cmd_command'] else "cmd=∅"
                    busy = " [BUSY.TXT]" if s['busy_file'] else ""
                    print(f"  {bot:25s} {info['state']:8s} {gate:15s} {hb:12s} {cmd}{busy}")
        else:
            states = get_all_states()
            if args.json:
                print(json.dumps(states, indent=2))
            else:
                # Group by state
                for state in ["BUSY", "WAITING", "DEAD"]:
                    bots = [b for b, s in states.items() if s == state]
                    if bots:
                        print(f"\n{state} ({len(bots)}):")
                        for b in bots:
                            print(f"  {b}")
