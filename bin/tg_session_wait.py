"""
Lightweight session waiter for the central poller architecture (Mac).

Uses SIGUSR1 signal for instant wake-up (<50ms). The central poller writes
command files and sends SIGUSR1 to this process; we wake immediately
without polling.

Command channel: tg_cmd/{bot}/cmd.txt
  - CHECK_MESSAGES — new Telegram message arrived, check DB
  - IDLE — no pending command (reset state)
  - Future: STOP, RUN_TASK:id, RELOAD_CONFIG, etc.

Usage (from Claude session, run_in_background with timeout=600000):
  python tg_session_wait.py --bot food --session SESSION_ID

Exits when:
  - A command is detected in the command channel file
  - New unread message(s) found for this bot
  - Budget exhausted (default 4 hours)
"""
import sys, os, time, sqlite3, random, json, signal
from pathlib import Path
from datetime import datetime

ANIMALS = [
    "pangolin", "quokka", "axolotl", "capybara", "okapi", "narwhal", "wombat",
    "caracal", "pika", "gharial", "kinkajou", "fossa", "numbat", "dugong",
    "tarsier", "binturong", "margay", "serval", "ocelot", "mandrill",
    "red panda", "snow leopard", "clouded leopard", "fennec fox", "maned wolf",
    "sun bear", "coati", "tamandua", "bushbaby", "loris", "aye-aye",
    "kakapo", "shoebill", "secretary bird", "hoatzin", "cassowary",
]

DIR = Path(__file__).resolve().parent
DB_PATH = DIR / "tg_messages.db"
COMMANDS_FILE = DIR / "tg_commands.json"
DEFAULT_BUDGET = 14400  # 4 hours — sessions stay alive, respond instantly via signal
SESSION_ID = "unknown"  # Set in main()
SESSION_PROJECT = "unknown"  # Set in main()

# Signal-based instant wake-up — SIGUSR1 from poller interrupts sleep immediately
_signal_received = False

def _sigusr1_handler(signum, frame):
    """SIGUSR1 handler — sets flag to break out of sleep immediately."""
    global _signal_received
    _signal_received = True

def _write_pid_file(bot):
    """Write PID file so the poller can send SIGUSR1 to wake us instantly."""
    pid_file = DIR / f"tg_session_{bot}.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    return pid_file

def _remove_pid_file(bot):
    """Clean up PID file on exit."""
    pid_file = DIR / f"tg_session_{bot}.pid"
    try:
        pid_file.unlink(missing_ok=True)
    except Exception:
        pass

# --- Command Dictionary (shared with central poller via tg_commands.json) ---
_cmd_cache = None
_cmd_cache_mtime = 0

def load_commands():
    """Load command dictionary from tg_commands.json. Cached, reloads on file change."""
    global _cmd_cache, _cmd_cache_mtime
    try:
        import json as _json
        mtime = COMMANDS_FILE.stat().st_mtime
        if _cmd_cache is not None and mtime == _cmd_cache_mtime:
            return _cmd_cache
        raw = _json.loads(COMMANDS_FILE.read_text(encoding="utf-8"))
        _cmd_cache = raw.get("commands", {})
        _cmd_cache_mtime = mtime
        return _cmd_cache
    except Exception:
        return {"CHECK_MESSAGES": {"type": "wake", "handler": "check_messages"},
                "KEEPALIVE": {"type": "wake", "handler": "keepalive"},
                "DECLARED_DEAD": {"type": "terminal", "handler": "declared_dead"}}

def _match_command(cmd_str):
    """Find the dictionary entry matching a command string.
    Returns (key, entry) or (None, None) if no match."""
    import re
    cmd_str = cmd_str.strip()
    cmd_dict = load_commands()
    if cmd_str in cmd_dict:
        return cmd_str, cmd_dict[cmd_str]
    for key, entry in cmd_dict.items():
        pattern = entry.get("match")
        if pattern and re.match(pattern, cmd_str):
            return key, entry
    return None, None



def ensure_sessions_table():
    """Create sessions table if it doesn't exist."""
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        bot TEXT,
        started_at TEXT NOT NULL,
        last_heartbeat TEXT NOT NULL
    )""")
    # Add columns if missing (upgrade path)
    for col, coltype in [("pid", "INTEGER"), ("project", "TEXT"), ("claude_heartbeat", "TEXT"), ("claude_animal", "TEXT"),
                          ("session_type", "TEXT"), ("keepalive_interval", "INTEGER"), ("restart_log", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def write_heartbeat(session_id, bot, project=None, session_type="active", keepalive_interval=None, new_session=False):
    """Update heartbeat timestamp, PID, and project for this session.
    Pass new_session=True on first registration to force started_at update."""
    if not DB_PATH.exists():
        return
    # Auto-detect standby from bot name — prevents misregistration when --standby flag is missing
    if bot.startswith("standby") and session_type == "active":
        session_type = "standby"
    now = datetime.now().isoformat(timespec="milliseconds")
    pid = os.getpid()
    platform = "mac" if sys.platform == "darwin" else "win"
    conn = sqlite3.connect(str(DB_PATH))
    # Lazy-add platform column if missing
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN platform TEXT DEFAULT 'win'")
    except Exception:
        pass  # Column already exists
    # Mark any OTHER active sessions for this bot as dead (prevents accumulation)
    conn.execute(
        "UPDATE sessions SET session_type='dead' WHERE bot=? AND session_id!=? AND session_type NOT IN ('dead','promoting')",
        (bot, session_id))
    # Build restart_log entry
    import json as _json
    restart_entry = {"ts": now, "pid": pid}
    existing_log = []
    row = conn.execute("SELECT restart_log FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row and row[0]:
        try:
            existing_log = _json.loads(row[0])
        except Exception:
            pass
    existing_log.append(restart_entry)
    restart_log_json = _json.dumps(existing_log)
    # Don't overwrite 'promoting' state — the poller sets this during standby promotion
    # and it must persist until the session actually processes the PROMOTE command
    conn.execute(
        """INSERT INTO sessions (session_id, bot, started_at, last_heartbeat, pid, project, session_type, keepalive_interval, claude_heartbeat, platform, restart_log)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
           started_at=CASE WHEN sessions.session_type='dead' OR ?=1 THEN ? ELSE sessions.started_at END,
           last_heartbeat=?, pid=?, project=COALESCE(?, project),
           session_type=CASE WHEN sessions.session_type='promoting' THEN 'promoting' ELSE COALESCE(?, sessions.session_type) END,
           keepalive_interval=COALESCE(?, keepalive_interval),
           claude_heartbeat=?, platform=?, restart_log=?""",
        (session_id, bot, now, now, pid, project, session_type, keepalive_interval, now, platform, restart_log_json,
         1 if new_session else 0, now, now, pid, project, session_type, keepalive_interval, now, platform, restart_log_json))
    conn.commit()
    conn.close()


ALSO_WATCH = []  # Sibling bots to also check for unread messages
COUNTDOWN_NUMBER = None  # If set, write this number back after KEEPALIVE (for standby countdown)

def get_unread(bot):
    """Get unread messages for a bot (and its siblings) from central poller DB."""
    if not DB_PATH.exists():
        return []
    bots_to_check = [bot] + ALSO_WATCH
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in bots_to_check)
    rows = conn.execute(
        f"SELECT * FROM messages WHERE bot IN ({placeholders}) AND read_time IS NULL AND direction='in'"
        f" AND received_time > datetime('now', '-24 hours')"
        f" ORDER BY received_time",
        bots_to_check).fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result


def _cmd_dir(bot):
    """Get per-bot command directory. Creates it if needed."""
    d = DIR / "tg_cmd" / bot
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cmd_file(bot):
    """Get path to bot's command file (new per-bot dir or legacy flat file)."""
    new_path = _cmd_dir(bot) / "cmd.txt"
    if new_path.exists():
        return new_path
    # Fallback to legacy flat file
    legacy = DIR / f"tg_cmd_{bot}.txt"
    if legacy.exists():
        return legacy
    # Default to new path
    return new_path


_last_poller_cycle = 0  # Last known poller cycle, read from cmd.txt

def read_command(bot):
    """Read command from the bot's command channel file. Returns command string or None.
    Ignores IDLE, countdown numbers, keepalive_at thresholds (session consumed = waiting state)."""
    global _last_poller_cycle
    cf = _cmd_file(bot)
    if not cf.exists():
        return None
    try:
        content = cf.read_text(encoding="utf-8").strip()
        lines = content.split("\n")
        command = lines[0].strip()
        # Extract poller cycle from cmd.txt (poller writes cycle=N)
        for line in lines:
            if line.strip().startswith("cycle="):
                try:
                    _last_poller_cycle = int(line.strip().split("=", 1)[1])
                except (ValueError, IndexError):
                    pass
        if not command or command == "IDLE" or command.isdigit():
            return None
        # "Alive N" (no ?) = session already confirmed, waiting for poller — ignore
        if command.startswith("Alive ") and "?" not in command:
            return None
        # keepalive_at:N = threshold waiting state — ignore
        if command.startswith("keepalive_at:"):
            return None
        return command
    except Exception:
        pass
    return None


DEFAULT_ACTIVE_COUNTDOWN = 240  # Legacy — kept for standby Alive threshold calculation

def reset_command(bot, use_countdown=False):
    """Reset the command channel file after processing.
    Active sessions: writes 'IDLE' — no keepalive, sessions restart only on user message.
    Standby sessions: writes 'Alive <threshold_cycle>' (poller checks at that cycle)."""
    global _last_poller_cycle
    cf = _cmd_dir(bot) / "cmd.txt"
    if use_countdown and COUNTDOWN_NUMBER is not None:
        # Standby — still needs Alive threshold for poller liveness checks
        if _last_poller_cycle == 0:
            hb_file = DIR / "tg_master_poller.heartbeat"
            try:
                for line in hb_file.read_text(encoding="utf-8").split("\n"):
                    if line.strip().startswith("cycle="):
                        _last_poller_cycle = int(line.strip().split("=", 1)[1])
                        break
            except Exception:
                pass
        cycle = _last_poller_cycle
        threshold = cycle + COUNTDOWN_NUMBER * DEFAULT_ACTIVE_COUNTDOWN
        cf.write_text(f"Alive {threshold}\n{datetime.now().isoformat(timespec='milliseconds')}", encoding="utf-8")
    else:
        # Active session — just idle, no keepalive probing
        cf.write_text(f"IDLE\n{datetime.now().isoformat(timespec='milliseconds')}", encoding="utf-8")


# --- Command Handlers (keyed by "handler" field in tg_commands.json) ---

def _handler_check_messages(bot, command):
    """Handler for CHECK_MESSAGES — check DB for unread messages."""
    unread = get_unread(bot)
    # Filter out poller hash commands first — if those are ALL there is, don't wake the session.
    real_unread = _drop_and_mark_hash_cmds(unread)
    if real_unread:
        busy_file = _cmd_dir(bot) / "busy.txt"
        busy_file.write_text(f"{datetime.now().isoformat(timespec='milliseconds')}\nsession={SESSION_ID}", encoding="utf-8")
        print(f"command: CHECK_MESSAGES")
        deliver_unread(bot, real_unread)
        reset_command(bot)
        return True
    reset_command(bot)
    return False

def _handler_keepalive(bot, command):
    """Handler for KEEPALIVE — LEGACY, keepalive system removed.
    If a stale KEEPALIVE is found in cmd.txt, just consume it and keep waiting.
    For standby sessions: still handle autonomously."""
    write_heartbeat(SESSION_ID, bot, project=None)

    # STANDBY — still needs autonomous heartbeat for poller Alive? protocol
    if COUNTDOWN_NUMBER is not None:
        animal = random.choice(ANIMALS)
        now = datetime.now().isoformat(timespec="milliseconds")
        if DB_PATH.exists():
            ensure_sessions_table()
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute(
                "UPDATE sessions SET claude_heartbeat=?, claude_animal=? WHERE session_id=?",
                (now, animal, SESSION_ID))
            if conn.total_changes == 0:
                conn.execute(
                    """INSERT INTO sessions (session_id, bot, started_at, last_heartbeat, claude_heartbeat, claude_animal, pid)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (SESSION_ID, bot, now, now, now, animal, os.getpid()))
            conn.commit()
            conn.close()
        reset_command(bot, use_countdown=True)
        return False

    # ACTIVE SESSION — keepalive removed. Just consume stale command and keep waiting.
    reset_command(bot)
    return False

def _handler_declared_dead(bot, command):
    """Handler for DECLARED_DEAD — exit gracefully."""
    print("DECLARED_DEAD")
    print("reason: Central poller declared this session dead (unread messages not consumed)")
    print("action: Write pending insights/TODOs, then exit gracefully")
    sys.stdout.flush()
    reset_command(bot)
    return True

def _handler_alive_challenge(bot, command):
    """Handler for Alive? — standby liveness protocol (threshold-based).
    New format: bare 'Alive?' — always triggers keepalive (session writes new threshold).
    Legacy format: 'Alive? N' — N>0 means intermediate check, N==0 means keepalive."""
    # New threshold format: bare "Alive?" (no number)
    if command.strip() == "Alive?":
        # Immediately answer the challenge to prevent poller declaring us dead on next cycle
        reset_command(bot, use_countdown=True)
        write_heartbeat(SESSION_ID, bot, project=None)
        # STANDBY AUTONOMOUS: handle without waking Claude (same as _handler_keepalive)
        if COUNTDOWN_NUMBER is not None:
            animal = random.choice(ANIMALS)
            now = datetime.now().isoformat(timespec="milliseconds")
            if DB_PATH.exists():
                ensure_sessions_table()
                conn = sqlite3.connect(str(DB_PATH))
                conn.execute(
                    "UPDATE sessions SET claude_heartbeat=?, claude_animal=? WHERE session_id=?",
                    (now, animal, SESSION_ID))
                conn.commit()
                conn.close()
            return False  # Keep waiting — no need to wake Claude
        # Active session — keepalive removed, just consume and keep waiting
        reset_command(bot)
        return False
    # Legacy format: "Alive? N"
    try:
        n = int(command.split("? ", 1)[1])
    except (ValueError, IndexError):
        n = 0
    if n > 0:
        cf = _cmd_dir(bot) / "cmd.txt"
        cf.write_text(f"Alive {n}\n{datetime.now().isoformat(timespec='milliseconds')}", encoding="utf-8")
        write_heartbeat(SESSION_ID, bot, project=None)
        return False  # Keep waiting
    else:
        # Active session — keepalive removed
        reset_command(bot)
        return False

def _handler_promote(bot, command):
    """Handler for PROMOTE:{target_bot}:{session_id} — standby promotion."""
    promote_parts = command.split(":")
    target_bot = promote_parts[1].strip() if len(promote_parts) > 1 else ""
    target_session_id = promote_parts[2].strip() if len(promote_parts) > 2 else None
    if target_session_id and target_session_id != SESSION_ID:
        print(f"PROMOTE:{target_bot} skipped (for session {target_session_id}, I am {SESSION_ID})")
        sys.stdout.flush()
        reset_command(bot, use_countdown=True)
        return False
    lock_file = _cmd_dir(bot) / "promote.lock"
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{SESSION_ID}\n{target_bot}".encode())
        os.close(fd)
    except FileExistsError:
        print(f"PROMOTE:{target_bot} — LOST RACE (another session claimed it)")
        sys.stdout.flush()
        reset_command(bot, use_countdown=True)
        return False
    print(f"PROMOTE:{target_bot}")
    target_project = _resolve_project_static(target_bot)
    print(f"target_project: {target_project}")
    helper_meta = _resolve_helper_meta(target_bot)
    if helper_meta:
        print(f"is_helper: true")
        print(f"helper_target: {helper_meta.get('helper_target', 'light')}")
        print(f"helper_model: {helper_meta.get('helper_model', 'sonnet')}")
        print(f"helper_tasks_dir: {helper_meta.get('helper_tasks_dir', '')}")
    target_unread = _count_unread_for_bot(target_bot)
    print(f"target_unread: {target_unread}")
    _inject_critical_rules(target_project)
    sys.stdout.flush()
    reset_command(bot)
    try:
        lock_file.unlink()
    except Exception:
        pass
    return True

def _handler_promote_helper(bot, command):
    """Handler for PROMOTE_HELPER:{project}:{model}:{session_id} — standby becomes helper.

    Mechanical helper loop: picks pending tasks from the requesting bot's task queue,
    presents them to Claude with full context. Claude executes and calls session_wait again.
    On subsequent calls, picks next task or waits for new ones."""
    parts = command.split(":")
    target_project = parts[1].strip() if len(parts) > 1 else ""
    model = parts[2].strip() if len(parts) > 2 else "sonnet"
    # Session ID in command is informational only — don't filter on it.
    # The promote.lock handles race conditions if multiple sessions exist on this bot.
    # Atomic claim via lock file
    lock_file = _cmd_dir(bot) / "promote.lock"
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{SESSION_ID}\nhelper:{target_project}".encode())
        os.close(fd)
    except FileExistsError:
        print(f"PROMOTE_HELPER:{target_project} — LOST RACE (another session claimed it)")
        sys.stdout.flush()
        reset_command(bot, use_countdown=True)
        return False

    # --- Mechanical helper setup ---
    # Find the requesting bot's task queue from the result file
    result_dir = DIR / "helper_queue"
    requesting_bot = None
    for rf in result_dir.glob("*.json"):
        try:
            req = json.loads(rf.read_text(encoding="utf-8"))
            if req.get("standby") == bot and req.get("project") == target_project:
                requesting_bot = req.get("bot")
                break
        except Exception:
            pass
    if not requesting_bot:
        requesting_bot = target_project  # fallback

    # Write helper state file so subsequent session_wait calls know we're a helper
    helper_state = _cmd_dir(bot) / "helper_state.json"
    helper_state.write_text(json.dumps({
        "project": target_project,
        "model": model,
        "requesting_bot": requesting_bot,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "session_id": SESSION_ID
    }, ensure_ascii=False), encoding="utf-8")

    # Read project CLAUDE.md mechanically and print it
    project_dir = DIR.parent / target_project
    claude_md = project_dir / "CLAUDE.md"
    print(f"PROMOTED TO HELPER: project={target_project}, requesting_bot={requesting_bot}")
    print(f"helper_model: {model}")
    print(f"is_helper: true")
    if claude_md.exists():
        try:
            content = claude_md.read_text(encoding="utf-8")
            # Print first 100 lines of CLAUDE.md for context
            lines = content.split("\n")[:100]
            print(f"--- PROJECT CLAUDE.MD ({target_project}) ---")
            print("\n".join(lines))
            if len(content.split("\n")) > 100:
                print(f"... ({len(content.split(chr(10))) - 100} more lines)")
            print("--- END PROJECT CLAUDE.MD ---")
        except Exception:
            pass

    # Mechanically pick pending task
    task_info = _pick_helper_task(requesting_bot)
    if task_info:
        print(f"--- TASK ---")
        print(f"task_id: {task_info['id']}")
        print(f"description: {task_info['description']}")
        print(f"files: {task_info.get('files', '')}")
        print(f"--- INSTRUCTIONS ---")
        print(f"1. Execute the task described above")
        print(f"2. When done, run: python {DIR}/helper_request.py done --bot {requesting_bot} --id {task_info['id']} --summary \"<what you did>\"")
        print(f"3. Then restart session_wait to pick next task")
        print(f"--- END TASK ---")
    else:
        print("No pending tasks in queue. Waiting for tasks...")
        # Write state so main loop watches task dir

    _inject_critical_rules(target_project)
    sys.stdout.flush()
    reset_command(bot)
    try:
        lock_file.unlink()
    except Exception:
        pass
    return True


def _pick_helper_task(requesting_bot):
    """Mechanically pick next pending task from requesting bot's task DB."""
    try:
        import sqlite3 as _sqlite3
        task_db = DIR / "helper_tasks" / requesting_bot / "tasks.db"
        if not task_db.exists():
            return None
        conn = _sqlite3.connect(str(task_db))
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            "SELECT * FROM tasks WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            conn.close()
            return None
        task = dict(row)
        # Mark as in_progress
        conn.execute(
            "UPDATE tasks SET status='in_progress', started_at=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), task['id']))
        conn.commit()
        conn.close()
        return task
    except Exception as e:
        print(f"[helper] Error picking task: {e}")
        return None

def _handler_bot_request(bot, command):
    """Handler for BOT_REQUEST:{from_bot}:{request_id} — inter-bot incoming request."""
    parts = command.split(":")
    from_bot = parts[1].strip() if len(parts) > 1 else "unknown"
    req_id = parts[2].strip() if len(parts) > 2 else "?"
    # Read request details from bot_comms.db
    comms_db = DIR / "bot_comms.db"
    detail = ""
    if comms_db.exists():
        try:
            conn = sqlite3.connect(str(comms_db), timeout=5)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
            if row:
                detail = f"type={row['request_type']}, summary={row['summary']}"
                if row.get("payload"):
                    detail += f", payload={row['payload'][:200]}"
            conn.close()
        except Exception:
            pass
    print(f"BOT_REQUEST from {from_bot} (id={req_id})")
    if detail:
        print(f"detail: {detail}")
    print(f"action: Check inbox with `python bot_comms.py inbox --bot {bot}`")
    print(f"        Accept with `python bot_comms.py accept --bot {bot} --id {req_id}`")
    print(f"        Complete with `python bot_comms.py complete --bot {bot} --id {req_id} --summary \"result\"`")
    sys.stdout.flush()
    reset_command(bot)
    return True

def _handler_bot_response(bot, command):
    """Handler for BOT_RESPONSE:{from_bot}:{request_id} — inter-bot response to your request."""
    parts = command.split(":")
    from_bot = parts[1].strip() if len(parts) > 1 else "unknown"
    req_id = parts[2].strip() if len(parts) > 2 else "?"
    # Read response details from bot_comms.db
    comms_db = DIR / "bot_comms.db"
    detail = ""
    if comms_db.exists():
        try:
            conn = sqlite3.connect(str(comms_db), timeout=5)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
            if row:
                detail = f"status={row['status']}, result={row.get('result_summary', '')}"
                if row.get("result_payload"):
                    detail += f", payload={row['result_payload'][:200]}"
            conn.close()
        except Exception:
            pass
    print(f"BOT_RESPONSE from {from_bot} (id={req_id})")
    if detail:
        print(f"detail: {detail}")
    print(f"action: Check responses with `python bot_comms.py responses --bot {bot}`")
    print(f"        Get details with `python bot_comms.py get --id {req_id}`")
    sys.stdout.flush()
    reset_command(bot)
    return True

def _handler_passthrough(bot, command):
    """Generic handler — print command with metadata from dictionary, wake Claude."""
    _, entry = _match_command(command)
    print(f"command: {command}")
    if entry and entry.get("desc"):
        print(f"desc: {entry['desc']}")
    sys.stdout.flush()
    reset_command(bot)
    return True

def _handler_consolidate(bot, command):
    """Handler for CONSOLIDATE — wake session so it does git commit + save context.
    Poller sends this at round hours to sessions that had incoming messages."""
    reset_command(bot)
    print("command: consolidate")
    print("trigger: hourly consolidate — MANDATORY: summarize what you worked on since last consolidation (specific files changed, bugs found, insights learned). Write insights/TODOs. Git commit project changes. Save context. Then resume listening. NEVER send a generic 'done' or 'Resuming' message — your summary must contain real content about what happened this session.")
    sys.stdout.flush()
    return True  # Wake the session

def _handler_idle_shutdown(bot, command):
    """Handler for IDLE_SHUTDOWN — wake session to save work, same as consolidate.
    Session saves context, writes insights/TODOs, does git commit, then resumes listening."""
    reset_command(bot)
    print("command: consolidate")
    print("trigger: idle consolidate — MANDATORY: summarize what you worked on since last consolidation (specific files changed, bugs found, insights learned). If idle with no work done, say so explicitly with what you were listening for. Write insights/TODOs. Git commit. Save context. Resume listening.")
    sys.stdout.flush()
    return True  # Wake the session

# Handler dispatch table — maps "handler" field in tg_commands.json to functions
_HANDLERS = {
    "check_messages": _handler_check_messages,
    "keepalive": _handler_keepalive,
    "declared_dead": _handler_declared_dead,
    "alive_challenge": _handler_alive_challenge,
    "promote": _handler_promote,
    "passthrough": _handler_passthrough,
    "consolidate": _handler_consolidate,
    "idle_shutdown": _handler_idle_shutdown,
    "promote_helper": _handler_promote_helper,
    "bot_request": _handler_bot_request,
    "bot_response": _handler_bot_response,
}

def handle_command(bot, command):
    """Process a command from the channel. Returns True if session should wake up.
    Uses dictionary-driven dispatch: reads tg_commands.json to find handler."""
    key, entry = _match_command(command)
    if entry is not None:
        handler_name = entry.get("handler")
        if handler_name is None:
            # Explicitly null handler = ignore (e.g. IDLE, ALIVE_RESPONSE)
            return False
        handler_fn = _HANDLERS.get(handler_name)
        if handler_fn:
            return handler_fn(bot, command)
        # Handler name specified but not in _HANDLERS → use passthrough
        return _handler_passthrough(bot, command)
    # Not in dictionary at all — pass through (future/unknown command)
    print(f"command: {command}")
    print(f"note: command not in tg_commands.json — passed through to session")
    sys.stdout.flush()
    reset_command(bot)
    return True


def _inject_critical_rules(project_name):
    """Print critical rules from project's critical_rules.txt if it exists.
    These are injected directly into the PROMOTE output so the AI sees them
    immediately — mechanical enforcement, not relying on AI reading CLAUDE.md."""
    if not project_name:
        return
    base = DIR.parent  # persistent-team/projects/
    # Try both project name forms
    for name in [project_name, project_name.replace("_", " ")]:
        rules_file = base / name / "critical_rules.txt"
        if rules_file.exists():
            try:
                rules = rules_file.read_text(encoding="utf-8").strip()
                if rules:
                    print(f"--- CRITICAL RULES ({project_name}) ---")
                    print(rules)
                    print("--- END CRITICAL RULES ---")
                return
            except Exception:
                pass


def _resolve_project_static(bot_key):
    """Resolve project name from bot key using telegram_bots.json (standalone, no global state)."""
    try:
        import json
        bots_file = DIR / "telegram_bots.json"
        if bots_file.exists():
            bots = json.loads(bots_file.read_text(encoding="utf-8"))
            return bots.get("bots", {}).get(bot_key, {}).get("project", bot_key)
    except Exception:
        pass
    return bot_key


def _resolve_helper_meta(bot_key):
    """Return helper metadata if bot is a helper, else None."""
    try:
        import json
        bots_file = DIR / "telegram_bots.json"
        if bots_file.exists():
            bot_info = json.loads(bots_file.read_text(encoding="utf-8")).get("bots", {}).get(bot_key, {})
            if bot_info.get("is_helper"):
                return bot_info
    except Exception:
        pass
    return None


def _count_unread_for_bot(bot_key):
    """Count unread messages for a specific bot."""
    if not DB_PATH.exists():
        return 0
    conn = sqlite3.connect(str(DB_PATH))
    count = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE bot=? AND read_time IS NULL AND direction='in'",
        (bot_key,)).fetchone()[0]
    conn.close()
    return count


_HASH_CMD_TEXTS = {"#", "##", "###", "####", "?"}

def _drop_and_mark_hash_cmds(unread):
    """Strip pure hash commands from the unread list and mark them read in DB.
    Hash commands are handled by the poller — sessions should never see them as text.
    Without this filter, after a ### restart the new session sees a stray '#' and treats it as text."""
    if not unread:
        return unread
    real, suppressed_ids = [], []
    for msg in unread:
        text = (msg.get("text") or "").strip()
        if msg.get("type") == "text" and text in _HASH_CMD_TEXTS:
            suppressed_ids.append(msg["id"])
            continue
        real.append(msg)
    if suppressed_ids:
        now_iso = datetime.now().isoformat(timespec="milliseconds")
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.executemany(
                "UPDATE messages SET read_time=?, session_id=? WHERE id=?",
                [(now_iso, SESSION_ID, mid) for mid in suppressed_ids])
            conn.commit()
            conn.close()
        except Exception:
            pass
    return real


def deliver_unread(bot, unread):
    """Output unread messages and mark them as read atomically.
    This eliminates the race where messages get consumed between detection and relay check."""
    if not unread:
        return False
    print(f"trigger: {len(unread)} unread message(s) for {bot}")
    # Output message content in same format as tg_relay.py check
    for msg in unread:
        sender = msg.get("sender", "?")
        msg_type = msg.get("type", "text")
        # Show received_time so sessions can track message arrival vs read delay
        rcv = msg.get("received_time", "")
        rcv_tag = ""
        if rcv:
            try:
                rcv_tag = f" (rcv {rcv[11:19]})"
            except Exception:
                rcv_tag = f" (rcv {rcv})"
        if msg_type == "voice":
            fid = msg.get("file_id", "")
            print(f"[VOICE from {sender}]{rcv_tag} duration={msg.get('duration', 0)}s file_id={fid}")
            print(f"  → To process: python tg_relay.py --bot {bot} voice {fid}")
        elif msg_type == "photo":
            fid = msg.get("file_id", "")
            cap = f" caption=\"{msg['caption']}\"" if msg.get("caption") else ""
            print(f"[PHOTO from {sender}]{rcv_tag}{cap} file_id={fid}")
            print(f"  → To view: python tg_relay.py --bot {bot} photo {fid}")
        elif msg_type == "document":
            fid = msg.get("file_id", "")
            fname = msg.get("text", "unknown")
            cap = f" caption=\"{msg['caption']}\"" if msg.get("caption") else ""
            print(f"[DOCUMENT from {sender}]{rcv_tag} file=\"{fname}\"{cap} file_id={fid}")
        elif msg_type == "audio":
            fid = msg.get("file_id", "")
            title = msg.get("text", "") or "audio"
            cap = f" caption=\"{msg['caption']}\"" if msg.get("caption") else ""
            print(f"[AUDIO from {sender}]{rcv_tag} title=\"{title}\" duration={msg.get('duration', 0)}s{cap} file_id={fid}")
            print(f"  → To download: python tg_relay.py --bot {bot} audio {fid}")
        elif msg_type == "sticker":
            fid = msg.get("file_id", "")
            emoji = msg.get("text", "")
            print(f"[STICKER from {sender}]{rcv_tag} emoji=\"{emoji}\" file_id={fid}")
        elif msg_type == "text":
            print(f"[{sender}]{rcv_tag} {msg.get('text', '')}")
        else:
            print(f"[{sender}]{rcv_tag} ({msg_type})")
    sys.stdout.flush()
    # Mark as read atomically
    now = datetime.now().isoformat(timespec="milliseconds")
    ids = [msg["id"] for msg in unread]
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.executemany(
            "UPDATE messages SET read_time=?, session_id=? WHERE id=?",
            [(now, SESSION_ID, mid) for mid in ids])
        conn.commit()
        conn.close()
    except Exception:
        pass  # Non-fatal — relay check will pick them up
    return True


def _check_worker_done(bot):
    """Check for completed worker tasks (done_*.flag in to_manager/)."""
    manager_dir = DIR / "helper_tasks" / bot / "to_manager"
    if not manager_dir.exists():
        return []
    done_flags = list(manager_dir.glob("done_*.flag")) + list(manager_dir.glob("error_*.flag"))
    return done_flags


def wait_for_flag_poll(bot, budget):
    """Mac/non-Windows wait using SIGUSR1 for instant wake-up.
    The poller sends SIGUSR1 after writing command/flag files, so we wake
    in <50ms instead of polling every 2s. Falls back to 10s periodic check
    as safety net (signal lost, poller restarted, etc.).
    Watches for: command files, Telegram notify flags, AND worker completion flags."""
    global _signal_received
    cmd_file = _cmd_file(bot)
    flag_file = DIR / f"tg_notify_{bot}.flag"
    last_cmd_mtime = cmd_file.stat().st_mtime if cmd_file.exists() else 0
    last_flag_mtime = flag_file.stat().st_mtime if flag_file.exists() else 0
    # Track known worker-done flags to detect new ones
    known_done_flags = set(str(f) for f in _check_worker_done(bot))
    start = time.time()

    # Register signal handler and write PID file for instant wake-up
    signal.signal(signal.SIGUSR1, _sigusr1_handler)
    _write_pid_file(bot)

    try:
        while True:
            elapsed = time.time() - start
            if elapsed >= budget:
                return False

            # Reset signal flag before checking (avoids race)
            _signal_received = False

            # Check command file
            if cmd_file.exists():
                current_mtime = cmd_file.stat().st_mtime
                if current_mtime > last_cmd_mtime:
                    last_cmd_mtime = current_mtime
                    cmd = read_command(bot)
                    if cmd and handle_command(bot, cmd):
                        return True

            # Check old flag file (backward compat)
            if flag_file.exists():
                current_mtime = flag_file.stat().st_mtime
                if current_mtime > last_flag_mtime:
                    last_flag_mtime = current_mtime
                    unread = _drop_and_mark_hash_cmds(get_unread(bot))
                    if deliver_unread(bot, unread):
                        return True

            # Check worker completion flags
            current_done = _check_worker_done(bot)
            new_done = [f for f in current_done if str(f) not in known_done_flags]
            if new_done:
                for f in new_done:
                    known_done_flags.add(str(f))
                # Report worker completions
                for f in new_done:
                    flag_name = f.name  # e.g. done_1.flag or error_2.flag
                    print(f"worker: {flag_name}")
                sys.stdout.flush()
                return True

            # Periodic DB check every 10s (safety net)
            if int(elapsed) % 10 == 0 and int(elapsed) > 0:
                unread = _drop_and_mark_hash_cmds(get_unread(bot))
                if deliver_unread(bot, unread):
                    return True

            # Sleep until SIGUSR1 arrives or 10s timeout (safety net).
            # SIGUSR1 interrupts sleep() immediately, so typical wake-up is <50ms.
            try:
                time.sleep(10)
            except (InterruptedError, OSError):
                pass  # SIGUSR1 interrupted sleep — check flags immediately
            # Also handle case where signal set flag but didn't raise InterruptedError
            if _signal_received:
                _signal_received = False
                continue  # Re-check all flags immediately
    finally:
        _remove_pid_file(bot)


def write_result(bot, result_type, detail=""):
    """Write wait result to a persistent JSON file.
    Sessions can read this instead of parsing task output paths (avoids Windows \\ vs / issues)."""
    import json
    try:
        from events import write_event
        write_event("session_wait", "exit", bot=bot, session=SESSION_ID,
                    result=result_type, detail=detail)
    except Exception:
        pass
    result_file = DIR / f"tg_wait_result_{bot}.json"
    result_file.write_text(json.dumps({
        "result": result_type,
        "detail": detail,
        "time": datetime.now().isoformat(timespec="milliseconds"),
        "session": SESSION_ID,
        "project": SESSION_PROJECT
    }), encoding="utf-8")


def _resolve_project(bot):
    """Resolve project name from bot key using telegram_bots.json."""
    try:
        bots_file = DIR / "telegram_bots.json"
        if bots_file.exists():
            import json
            bots = json.loads(bots_file.read_text())
            bot_cfg = bots.get("bots", {}).get(bot, {})
            return bot_cfg.get("project", bot)
    except Exception:
        pass
    return bot


def _resolve_siblings(bot):
    """Find sibling bots — other bots with the same project in telegram_bots.json."""
    try:
        bots_file = DIR / "telegram_bots.json"
        if bots_file.exists():
            import json
            all_bots = json.loads(bots_file.read_text()).get("bots", {})
            my_project = all_bots.get(bot, {}).get("project", "")
            if not my_project:
                return []
            return [k for k, v in all_bots.items() if k != bot and v.get("project") == my_project]
    except Exception:
        pass
    return []



def cmd_busy(bot, action):
    """Manage busy.txt indicator file. 'start' creates it, 'stop' removes it."""
    busy_file = _cmd_dir(bot) / "busy.txt"
    if action == "start":
        busy_file.write_text(datetime.now().isoformat(timespec="milliseconds"), encoding="utf-8")
        print(f"busy: ON (bot={bot})")
    elif action == "stop":
        busy_file.unlink(missing_ok=True)
        print(f"busy: OFF (bot={bot})")
    else:
        print(f"ERROR: unknown busy action '{action}' (use 'start' or 'stop')")


def cmd_claude_heartbeat(bot, session_id, animal="unknown"):
    """Write claude_heartbeat timestamp + random animal — confirms the Claude AI actually processed the keepalive.
    The animal name proves AI involvement (can't be faked by a script)."""
    if not DB_PATH.exists():
        print("ERROR: DB not found")
        return
    now = datetime.now().isoformat(timespec="milliseconds")
    ensure_sessions_table()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "UPDATE sessions SET claude_heartbeat=?, claude_animal=? WHERE session_id=?",
        (now, animal, session_id))
    if conn.total_changes == 0:
        conn.execute(
            """INSERT INTO sessions (session_id, bot, started_at, last_heartbeat, claude_heartbeat, claude_animal, pid)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, bot, now, now, now, animal, os.getpid()))
    conn.commit()
    conn.close()
    # NOW consume the KEEPALIVE — write countdown to cmd.txt.
    # This is the only place KEEPALIVE gets consumed, proving Claude is alive.
    reset_command(bot, use_countdown=True)
    print(f"claude_heartbeat: {now} animal={animal} (session={session_id}, bot={bot})")


def cmd_sessions_status():
    """Show all sessions with both heartbeats — quick health check."""
    if not DB_PATH.exists():
        print("ERROR: DB not found")
        return
    ensure_sessions_table()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT session_id, bot, started_at, last_heartbeat, claude_heartbeat, claude_animal, pid, project
           FROM sessions ORDER BY last_heartbeat DESC LIMIT 20""").fetchall()
    now = datetime.now()
    print(f"{'SESSION':<35} {'BOT':<15} {'WAIT_HB':<12} {'CLAUDE_HB':<12} {'ANIMAL':<15} {'STATUS'}")
    print("-" * 105)
    for r in rows:
        # Calculate age
        try:
            wait_dt = datetime.fromisoformat(r['last_heartbeat'])
            wait_age = (now - wait_dt).total_seconds()
            wait_str = f"{int(wait_age)}s ago" if wait_age < 600 else f"{int(wait_age/60)}m ago"
        except Exception:
            wait_str = "never"
        try:
            claude_dt = datetime.fromisoformat(r['claude_heartbeat']) if r['claude_heartbeat'] else None
            if claude_dt:
                claude_age = (now - claude_dt).total_seconds()
                claude_str = f"{int(claude_age)}s ago" if claude_age < 600 else f"{int(claude_age/60)}m ago"
            else:
                claude_str = "never"
        except Exception:
            claude_str = "never"
        animal = r['claude_animal'] if r['claude_animal'] else "-"
        status = "ALIVE" if wait_str != "never" and "m ago" not in wait_str else "DEAD"
        if status == "ALIVE" and claude_str == "never":
            status = "WAIT_ONLY"  # session_wait alive but Claude never confirmed
        print(f"{r['session_id'][:34]:<35} {r['bot']:<15} {wait_str:<12} {claude_str:<12} {animal:<15} {status}")
    conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--session", default="unknown")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--project", default=None, help="Project name (auto-resolved from bot if omitted)")
    parser.add_argument("--claude-heartbeat", action="store_true",
                        help="Just write claude_heartbeat and exit (confirms Claude session is alive)")
    parser.add_argument("--animal", default="unknown",
                        help="Random animal name — proof that AI generated this heartbeat")
    parser.add_argument("--also-watch", nargs="*", default=[],
                        help="Sibling bot keys to also check for unread messages")
    parser.add_argument("--standby", nargs="?", const=1, type=int, default=None,
                        help="Register as standby with countdown N (keepalive every N*8min). E.g. --standby 3")
    parser.add_argument("--keepalive-interval", type=int, default=None,
                        help="Custom keepalive interval in seconds (for standby experiments)")
    parser.add_argument("--new-session", action="store_true",
                        help="Signal that this is a new Claude session (forces started_at update)")
    parser.add_argument("--busy", choices=["start", "stop"], default=None,
                        help="Set/clear busy indicator (doubles death detection threshold)")
    parser.add_argument("--status", action="store_true",
                        help="Show all sessions status and exit")
    args = parser.parse_args()

    if args.status:
        cmd_sessions_status()
        return

    if args.busy:
        cmd_busy(args.bot, args.busy)
        return

    if args.claude_heartbeat:
        cmd_claude_heartbeat(args.bot, args.session, args.animal)
        return

    global SESSION_ID, SESSION_PROJECT, ALSO_WATCH, COUNTDOWN_NUMBER
    bot = args.bot
    budget = args.budget
    session_id = args.session
    SESSION_ID = session_id
    # Read also_watch from CLI args or from telegram_bots.json config
    ALSO_WATCH = args.also_watch or []
    if not ALSO_WATCH:
        try:
            bots_file = DIR / "telegram_bots.json"
            if bots_file.exists():
                import json as _json
                _cfg = _json.loads(bots_file.read_text()).get("bots", {}).get(bot, {})
                ALSO_WATCH = _cfg.get("also_watch", [])
        except Exception:
            pass
    project = args.project or _resolve_project(bot)
    SESSION_PROJECT = project
    is_standby = args.standby is not None
    session_type = "standby" if is_standby else "active"
    COUNTDOWN_NUMBER = args.standby if is_standby else None

    info = f"Session wait started: bot={bot}, session={args.session}, budget={budget}s"
    if is_standby:
        info += f", type=standby, countdown={COUNTDOWN_NUMBER}"
    print(info)
    sys.stdout.flush()

    try:
        from events import write_event
        write_event("session_wait", "start", bot=bot, session=args.session,
                    budget=budget, standby=bool(is_standby),
                    countdown=COUNTDOWN_NUMBER,
                    new_session=bool(args.new_session))
    except Exception:
        pass

    # Initialize sessions table and write initial heartbeat
    ensure_sessions_table()
    keepalive_interval_sec = (COUNTDOWN_NUMBER or 1) * 480 if is_standby else None
    write_heartbeat(SESSION_ID, bot, project, session_type=session_type,
                    keepalive_interval=keepalive_interval_sec or args.keepalive_interval,
                    new_session=args.new_session)

    # Clear busy flag — session is now idle/waiting
    busy_file = DIR / "tg_cmd" / bot / "busy.txt"
    try:
        busy_file.unlink(missing_ok=True)
    except Exception:
        pass

    # Reset any stale command from previous session
    # For standbys: write the countdown number so central poller starts decrementing
    reset_command(bot, use_countdown=(COUNTDOWN_NUMBER is not None))

    # --- Helper mode: if helper_state.json exists, check for tasks ---
    helper_state_file = DIR / "tg_cmd" / bot / "helper_state.json"
    if helper_state_file.exists():
        try:
            hs = json.loads(helper_state_file.read_text(encoding="utf-8"))
            requesting_bot = hs.get("requesting_bot", "")
            task = _pick_helper_task(requesting_bot)
            if task:
                print(f"--- HELPER TASK ---")
                print(f"project: {hs.get('project', '')}")
                print(f"task_id: {task['id']}")
                print(f"description: {task['description']}")
                print(f"files: {task.get('files', '')}")
                print(f"--- INSTRUCTIONS ---")
                print(f"1. Execute the task described above")
                print(f"2. When done, run: python {DIR}/helper_request.py done --bot {requesting_bot} --id {task['id']} --summary \"<what you did>\"")
                print(f"3. Then restart session_wait to pick next task")
                print(f"--- END TASK ---")
                sys.stdout.flush()
                write_result(bot, "helper_task", f"task #{task['id']}")
                return
        except Exception:
            pass
        # No task — fall through to normal wait (will pick up new tasks via file watch)

    # Check for already-unread messages first (catch-up)
    unread = _drop_and_mark_hash_cmds(get_unread(bot))
    if deliver_unread(bot, unread):
        write_result(bot, "unread", f"{len(unread)} message(s)")
        return

    # Check for pending command (ignore DECLARED_DEAD on startup — we just registered fresh)
    cmd = read_command(bot)
    if cmd and cmd == "DECLARED_DEAD":
        # Stale death signal from before we started — clear it and continue
        reset_command(bot, use_countdown=(COUNTDOWN_NUMBER is not None))
        cmd = None
    if cmd and handle_command(bot, cmd):
        write_result(bot, "command", cmd)
        return

    # Block until SIGUSR1 signal or flag file changes
    found = wait_for_flag_poll(bot, budget)

    if not found:
        # No idle exit — sessions stay alive until explicitly closed
        print(f"budget exhausted")
        write_result(bot, "budget", "no messages")
        sys.stdout.flush()
    else:
        write_result(bot, "wakeup", "message or command detected")


if __name__ == "__main__":
    main()
