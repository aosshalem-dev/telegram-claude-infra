"""
Telegram relay for Claude Code sessions.
Poll for messages, send replies, download voice files.
Supports multiple bots via --bot flag.

Usage:
  python tg_relay.py check                           — show new messages (default bot: lili)
  python tg_relay.py reply "text"                    — send reply to last chat
  python tg_relay.py reply --to <alias> "text"       — multi-user bot: explicit recipient (e.g. --to owner / --to carol)
  python tg_relay.py voice <file_id>                 — download voice file
  python tg_relay.py send-doc <path> [caption]       — upload + send document (mirrors to owner's paired bot)
  python tg_relay.py send-photo <path> [caption]     — upload + send photo (mirrors to owner's paired bot)
  python tg_relay.py send-voice <path> [caption]     — upload + send voice (mirrors to owner's paired bot)
  python tg_relay.py heartbeat [interval]            — send "." every N sec (default 30)
  python tg_relay.py --bot henry_giroux check        — use a different bot
  python tg_relay.py --bot henry_giroux reply "text" — reply via different bot
  python tg_relay.py --bot telegram send city_ranking "msg" — send to another bot (triggers auto-launch)
"""
import sys, json, urllib.request, urllib.parse, time, signal, os
from pathlib import Path
from datetime import datetime

try:
    from tg_events import log_event as _log_event
    _has_events = True
except ImportError:
    _has_events = False

DIR = Path(__file__).parent

# Impersonator @s — disabled 2026-04-20 per owner (mirrors poller's
# IMPERSONATOR_ENABLED flag in tg_master_poller.py). Set True to re-enable
# the @s relay paths: outbound text mirror to @s, outbound media mirror to
# @s, and reply auto-redirect when last inbound was tagged 'owner (via @s)'.
IMPERSONATOR_ENABLED = False

def get_bot_config(bot_name):
    bots_file = DIR / "telegram_bots.json"
    if bots_file.exists():
        bots = json.loads(bots_file.read_text())
        if bot_name in bots["bots"]:
            return bots["bots"][bot_name]["token"]
        # Bot not found — error instead of silently falling back to wrong bot
        available = list(bots["bots"].keys())
        print(f"ERROR: Bot '{bot_name}' not found in telegram_bots.json. Available: {available}", file=sys.stderr)
        sys.exit(1)
    # Fallback to original config only when telegram_bots.json doesn't exist
    cfg = json.loads((DIR / "telegram_config.json").read_text())
    return cfg["telegram"]["bot_token"]

def parse_args():
    args = sys.argv[1:]
    bot_name = "lili"
    if "--bot" in args:
        idx = args.index("--bot")
        bot_name = args[idx + 1]
        args = args[:idx] + args[idx + 2:]
    cmd = args[0] if args else "check"
    cmd_args = args[1:] if len(args) > 1 else []
    return bot_name, cmd, cmd_args

BOT_NAME, CMD, CMD_ARGS = parse_args()
TOKEN = get_bot_config(BOT_NAME)
API = f"https://api.telegram.org/bot{TOKEN}"
STATE = DIR / f"tg_relay_state_{BOT_NAME}.json"

def get_allowed_users(bot_name):
    bots_file = DIR / "telegram_bots.json"
    if bots_file.exists():
        bots = json.loads(bots_file.read_text())
        # Per-bot allowed_user_ids override global
        bot_cfg = bots.get("bots", {}).get(bot_name, {})
        if "allowed_user_ids" in bot_cfg:
            return bot_cfg["allowed_user_ids"]
        return bots.get("allowed_user_ids", [])
    return []

ALLOWED_USERS = get_allowed_users(BOT_NAME)

def _event(event, **kwargs):
    if _has_events:
        _log_event(BOT_NAME, event, **kwargs)

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"offset": 0, "chat_id": None}

def save_state(state):
    # Offset protection: never decrease
    if STATE.exists():
        try:
            old = json.loads(STATE.read_text())
            old_offset = old.get("offset", 0)
            if state.get("offset", 0) < old_offset:
                state["offset"] = old_offset
        except (json.JSONDecodeError, ValueError):
            pass
    STATE.write_text(json.dumps(state))

def api_call(method, params=None):
    url = f"{API}/{method}"
    if params:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data)
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def _is_pid_alive(pid):
    """Check if a process is alive. Works on both Windows and Unix."""
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5, creationflags=0x08000000
            )
            return str(pid) in result.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False


def _check_mac_db():
    """Check Mac's tg_messages.db via SSH for unread messages directed at this bot.
    Catches messages from Mac sessions that used tg_relay.py --bot X reply (outgoing on Mac,
    but content directed at this session). Only shows direction='out' messages from 'bot' sender
    that haven't been read, as incoming human messages are handled by the Mac poller."""
    import subprocess
    try:
        cmd = (
            f'cd ~/telegram-claude-infra/projects/today && python3 -c "'
            f'import sqlite3, json; '
            f'conn = sqlite3.connect(\\\"tg_messages.db\\\"); '
            f'rows = conn.execute('
            f'\\\"SELECT id, sender, text, msg_time FROM messages '
            f'WHERE bot = \\\\\\\"{BOT_NAME}\\\\\\\" AND read_time IS NULL '
            f'AND direction = \\\\\\\"out\\\\\\\" AND sender = \\\\\\\"bot\\\\\\\" '
            f'ORDER BY id DESC LIMIT 10\\\").fetchall(); '
            f'[print(json.dumps(dict(id=r[0], sender=r[1], text=r[2], time=r[3]))) for r in rows]; '
            f'ids = [r[0] for r in rows]; '
            f'conn.executemany(\\\"UPDATE messages SET read_time=datetime(\\\\\\\"now\\\\\\\") WHERE id=?\\\", '
            f'[(i,) for i in ids]); '
            f'conn.commit(); conn.close()"'
        )
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "USER@HOST", cmd],
            capture_output=True, text=True, timeout=10
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                try:
                    msg = json.loads(line)
                    print(f"[mac:{msg.get('sender', '?')}] {msg.get('text', '')}")
                except Exception:
                    pass
        else:
            print("No new messages.")
    except Exception:
        print("No new messages.")


def check():
    """Check for unread messages from the central poller's SQLite DB.
    Uses atomic claim+confirm: messages are marked read only after being displayed.
    Previously-read but unresponded messages from crashed sessions are reclaimed
    after 10 minutes (stale_threshold)."""
    _event("relay_check", source="db")
    db_path = DIR / "tg_messages.db"
    if not db_path.exists():
        print("No message DB yet. Is tg_master_poller.py running?")
        return

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Look up active session from sessions table instead of env var
    try:
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE bot=? ORDER BY last_heartbeat DESC LIMIT 1",
            (BOT_NAME,)).fetchone()
        session_id = row[0] if row else ""
    except Exception:
        session_id = ""
    now = datetime.now().isoformat(timespec="milliseconds")

    # Also reclaim stale messages: read but not responded after 10 min (crashed session)
    stale_threshold = 600  # seconds
    conn.execute(
        "UPDATE messages SET read_time=NULL, session_id=NULL "
        "WHERE bot=? AND read_time IS NOT NULL AND responded_time IS NULL "
        "AND direction='in' "
        "AND julianday('now') - julianday(read_time) > ?/86400.0",
        (BOT_NAME, stale_threshold))
    conn.commit()

    # Atomic claim: UPDATE only unclaimed messages, then display only what we claimed
    unclaimed = conn.execute(
        "SELECT id FROM messages WHERE bot=? AND read_time IS NULL AND direction='in' "
        "ORDER BY received_time",
        (BOT_NAME,)).fetchall()

    if not unclaimed:
        conn.close()
        _check_mac_db()
        return

    # Claim atomically: only set read_time where still NULL (prevents race with other sessions)
    claimed_ids = []
    for row in unclaimed:
        cur = conn.execute(
            "UPDATE messages SET read_time=?, session_id=? "
            "WHERE id=? AND read_time IS NULL",
            (now, session_id or None, row["id"]))
        if cur.rowcount > 0:
            claimed_ids.append(row["id"])
    conn.commit()

    if not claimed_ids:
        print("No new messages.")
        conn.close()
        return

    # Display only messages we successfully claimed
    placeholders = ",".join("?" * len(claimed_ids))
    rows = conn.execute(
        f"SELECT * FROM messages WHERE id IN ({placeholders}) ORDER BY received_time",
        claimed_ids).fetchall()

    for r in rows:
        sender = r["sender"] or "?"
        msg_type = r["type"] or "text"
        # Show received_time so sessions can track message arrival vs read delay
        rcv = r["received_time"] if "received_time" in r.keys() else ""
        rcv_tag = ""
        if rcv:
            try:
                rcv_tag = f" (rcv {rcv[11:19]})"
            except Exception:
                rcv_tag = f" (rcv {rcv})"
        if msg_type == "voice":
            fid = r['file_id'] or ''
            print(f"[VOICE from {sender}]{rcv_tag} duration={r['duration'] or 0}s file_id={fid}")
            print(f"  → To process: python tg_relay.py --bot {BOT_NAME} voice {fid}  # downloads .ogg, then transcribe with: python voice/transcribe.py <file>")
        elif msg_type == "photo":
            fid = r['file_id'] or ''
            cap = f" caption=\"{r['caption']}\"" if r["caption"] else ""
            print(f"[PHOTO from {sender}]{rcv_tag}{cap} file_id={fid}")
            print(f"  → To view: python tg_relay.py --bot {BOT_NAME} photo {fid}  # downloads image, then use Read tool on the downloaded file")
        elif msg_type in ("video", "video_note", "animation"):
            fid = r['file_id'] or ''
            dur = f" duration={r['duration'] or 0}s" if r.get('duration') else ""
            cap = f" caption=\"{r['caption']}\"" if r.get("caption") else ""
            print(f"[{msg_type.upper()} from {sender}]{rcv_tag}{dur}{cap} file_id={fid}")
            print(f"  → To download: python tg_relay.py --bot {BOT_NAME} video {fid}  # downloads video file")
        elif msg_type == "document":
            fid = r['file_id'] or ''
            fname = r['text'] or 'unknown'
            cap = f" caption=\"{r['caption']}\"" if r["caption"] else ""
            print(f"[DOCUMENT from {sender}]{rcv_tag} file=\"{fname}\"{cap} file_id={fid}")
            print(f"  → To download: python tg_relay.py --bot {BOT_NAME} document {fid}  # downloads file")
        elif msg_type == "text":
            print(f"[{sender}]{rcv_tag} {r['text'] or ''}")
        else:
            print(f"[{sender}]{rcv_tag} ({msg_type})")

    stolen = len(unclaimed) - len(claimed_ids)
    if stolen > 0:
        print(f"⚠️ {stolen} message(s) claimed by another session")
    conn.close()

def _get_session_tokens():
    """Read latest token usage from the calling Claude session's JSONL.
    Returns (input_tokens, output_tokens) or (None, None) if unavailable."""
    try:
        project_dir = Path.home() / ".claude" / "projects"
        # Find dirs matching persistent-team (main project)
        jsonl_dirs = [d for d in project_dir.iterdir()
                      if d.is_dir() and "persistent-team" in d.name and "projects-" not in d.name]
        if not jsonl_dirs:
            return None, None

        # Find most recent JSONL for this bot by checking first user message
        bots_file = DIR / "telegram_bots.json"
        my_short = ""
        if bots_file.exists():
            bots = json.loads(bots_file.read_text())["bots"]
            cfg = bots.get(BOT_NAME, {})
            my_short = cfg.get("short", f"@{BOT_NAME}")

        best_file = None
        best_mtime = 0
        for d in jsonl_dirs:
            for f in d.glob("*.jsonl"):
                mtime = f.stat().st_mtime
                if mtime <= best_mtime:
                    continue
                # Quick check: read first few lines to find bot tag
                with open(f) as fh:
                    for line in fh:
                        entry = json.loads(line.strip())
                        if entry.get("type") == "user":
                            msg = entry.get("message", {})
                            content = msg.get("content", "") if isinstance(msg, dict) else ""
                            if isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        content = block.get("text", "")
                                        break
                            if isinstance(content, str) and content.strip().startswith(my_short):
                                best_file = f
                                best_mtime = mtime
                            break

        if not best_file:
            return None, None

        # Sum all usage in this session
        total_in = 0
        total_out = 0
        with open(best_file) as fh:
            for line in fh:
                entry = json.loads(line.strip())
                if entry.get("type") == "assistant":
                    usage = entry.get("message", {}).get("usage", {})
                    if usage:
                        total_in += usage.get("input_tokens", 0)
                        total_in += usage.get("cache_read_input_tokens", 0)
                        total_in += usage.get("cache_creation_input_tokens", 0)
                        total_out += usage.get("output_tokens", 0)

        return total_in if total_in else None, total_out if total_out else None
    except Exception:
        return None, None


def _mark_responded_db(reply_text="", explicit_chat_id=None, specific_msg_id=None):
    """Mark incoming message(s) as responded in the central DB. Insert an
    outgoing row for conversation log.

    specific_msg_id: if provided, mark ONLY that row (for auto-route, where
    the reply targets one specific thread). Otherwise mark all
    read-but-unresponded (legacy bulk behavior).

    explicit_chat_id: stored verbatim in the outbound row. Otherwise falls
    back to state-based resolution."""
    db_path = DIR / "tg_messages.db"
    if not db_path.exists():
        return
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        # Lazy-add token columns if missing
        try:
            conn.execute("SELECT input_tokens FROM messages LIMIT 0")
        except Exception:
            conn.execute("ALTER TABLE messages ADD COLUMN input_tokens INTEGER")
            conn.execute("ALTER TABLE messages ADD COLUMN output_tokens INTEGER")
            conn.commit()
        # Look up active session from sessions table instead of env var
        try:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE bot=? ORDER BY last_heartbeat DESC LIMIT 1",
                (BOT_NAME,)).fetchone()
            session_id = row[0] if row else ""
        except Exception:
            session_id = ""
        now = datetime.now().isoformat(timespec="milliseconds")
        # Mark incoming messages as responded. When auto-route resolved a
        # specific row, mark only that one so subsequent replies can still
        # pick up the next oldest. Legacy path marks all read-unresponded.
        if specific_msg_id is not None:
            conn.execute(
                "UPDATE messages SET responded_time=?, response_text=?, session_id=? "
                "WHERE id=? AND responded_time IS NULL",
                (now, (reply_text or "")[:500], session_id or None, specific_msg_id))
        else:
            conn.execute(
                "UPDATE messages SET responded_time=?, response_text=?, session_id=? "
                "WHERE bot=? AND read_time IS NOT NULL AND responded_time IS NULL",
                (now, (reply_text or "")[:500], session_id or None, BOT_NAME))
        # Get cumulative session token usage
        tok_in, tok_out = _get_session_tokens()
        # Insert outgoing reply as separate row for conversation log
        if explicit_chat_id is not None:
            out_chat_id = explicit_chat_id
        else:
            state = load_state()
            out_chat_id = _resolve_reply_chat_id(state)
        conn.execute(
            "INSERT INTO messages (bot, sender, chat_id, type, text, msg_time, "
            "received_time, project, direction, session_id, read_time, input_tokens, output_tokens) "
            "VALUES (?, 'bot', ?, 'text', ?, ?, ?, ?, 'out', ?, ?, ?, ?)",
            (BOT_NAME, out_chat_id, (reply_text or "")[:2000], now, now,
             BOT_NAME.split("_")[0] if "_" in BOT_NAME else BOT_NAME,
             session_id or None, now, tok_in, tok_out))
        conn.commit()
        conn.close()
    except Exception:
        pass

def _latest_inbound_sender():
    """Sender of most recent inbound message for this bot (for recipient tag)."""
    try:
        import sqlite3
        db_path = DIR / "tg_messages.db"
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT sender FROM messages WHERE bot=? AND direction='in' "
            "ORDER BY id DESC LIMIT 1", (BOT_NAME,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _resolve_recipient_label(my_cfg, chat_id, is_to_owner):
    """Produce a reliable recipient label for mirror prefixes from the ACTUAL
    destination chat_id (not from last-inbound-sender, which can lie if the
    conversation state advanced after this reply was drafted)."""
    if is_to_owner:
        return "owner"
    if chat_id is not None:
        try:
            aliases = my_cfg.get("user_aliases") or {}
            for name, cid in aliases.items():
                if int(cid) == int(chat_id):
                    return name
        except Exception:
            pass
    # Fallback: last-inbound-sender (legacy), trimmed of 'bot:' prefix
    lbl = _latest_inbound_sender() or "?"
    if lbl.startswith("bot:"):
        lbl = lbl.split(":", 1)[1]
    return lbl


def _mirror_media_to_owner_monitor(kind, file_id, caption, chat_id):
    """Media counterpart of _mirror_reply_to_owner_monitor — re-sends the same
    file (by file_id, no re-upload) into owner's chat with THIS bot."""
    meta = _KIND_META.get(kind)
    if not meta:
        return
    try:
        OWNER_CHAT = 0
        if chat_id is not None and int(chat_id) == OWNER_CHAT:
            return
        bots_file = DIR / "telegram_bots.json"
        if not bots_file.exists():
            return
        bots = json.loads(bots_file.read_text())["bots"]
        my_cfg = bots.get(BOT_NAME, {})
        if my_cfg.get("short") and not my_cfg.get("user_aliases"):
            return
        my_tok = my_cfg.get("token")
        if not my_tok:
            return
        recipient = _resolve_recipient_label(my_cfg, chat_id, is_to_owner=False)
        mirror_caption = (f"\U0001F916 [\u2192 {recipient}]: " + (caption or ""))[:1000]
        data = urllib.parse.urlencode({
            "chat_id": OWNER_CHAT,
            meta["field"]: file_id,
            "caption": mirror_caption,
        }).encode()
        url = f"https://api.telegram.org/bot{my_tok}/{meta['method']}"
        urllib.request.urlopen(urllib.request.Request(url, data))
    except Exception:
        pass


def _mirror_reply_to_owner_monitor(text, chat_id):
    """Mirror outbound reply into owner's chat with THIS bot, so he sees the
    conversation in the same UI (2026-04-19 owner: 'every colleague bot also
    serves as my monitoring channel'). Skip: (a) owner-bound replies, already
    direct; (b) owner's own 'short' bots."""
    try:
        OWNER_CHAT = 0
        if chat_id is not None and int(chat_id) == OWNER_CHAT:
            return
        bots_file = DIR / "telegram_bots.json"
        if not bots_file.exists():
            return
        bots = json.loads(bots_file.read_text())["bots"]
        my_cfg = bots.get(BOT_NAME, {})
        if my_cfg.get("short") and not my_cfg.get("user_aliases"):
            return
        my_tok = my_cfg.get("token")
        if not my_tok:
            return
        recipient = _resolve_recipient_label(my_cfg, chat_id, is_to_owner=False)
        mirror_text = f"\U0001F916 [\u2192 {recipient}]: {text}"
        if len(mirror_text) > 4000:
            mirror_text = mirror_text[:3997] + "..."
        data = urllib.parse.urlencode({"chat_id": OWNER_CHAT, "text": mirror_text}).encode()
        url = f"https://api.telegram.org/bot{my_tok}/sendMessage"
        urllib.request.urlopen(urllib.request.Request(url, data))
    except Exception:
        pass


def _forward_reply_to_siblings(text, chat_id=None):
    """Mirror outgoing reply to @gg universal mirror only.
    Format: 🤖 [project | @bot_username → <recipient>] <text>  (client-bound)
            [project | @bot_username → owner] <text>             (owner-bound — no 🤖)
    Skips bots with a 'short' alias (owner's own bots — recipient is owner, redundant).
    Paired-sibling forward removed 2026-04-18 — @gg replaces it."""
    try:
        if BOT_NAME == "mirror_gg":
            return
        bots_file = DIR / "telegram_bots.json"
        if not bots_file.exists():
            return
        bots = json.loads(bots_file.read_text())["bots"]
        my_cfg = bots.get(BOT_NAME, {})
        if my_cfg.get("short") and not my_cfg.get("user_aliases"):
            return
        my_project = my_cfg.get("project", "") or "?"
        mirror_cfg = bots.get("mirror_gg")
        if not mirror_cfg:
            return
        mtok = mirror_cfg.get("token")
        mchat = mirror_cfg.get("chat_id")
        if not (mtok and mchat):
            return
        my_user = my_cfg.get("username", "") or BOT_NAME
        is_to_owner = False
        try:
            if chat_id is not None and int(chat_id) == int(mchat):
                is_to_owner = True
        except Exception:
            pass
        recipient = _resolve_recipient_label(my_cfg, chat_id, is_to_owner)
        emoji = "" if is_to_owner else "\U0001F916 "
        prefix = f"{emoji}[{my_project} | @{my_user} \u2192 {recipient}] "
        mirror_text = prefix + text
        if len(mirror_text) > 4000:
            mirror_text = mirror_text[:3997] + "..."
        sib_api = f"https://api.telegram.org/bot{mtok}"
        data = urllib.parse.urlencode({"chat_id": mchat, "text": mirror_text}).encode()
        req = urllib.request.Request(f"{sib_api}/sendMessage", data)
        urllib.request.urlopen(req)
        # Impersonator live-copy: if @s is currently connected to THIS bot,
        # also mirror the outgoing reply to @s so owner sees the live convo.
        try:
            ipath = DIR / "impersonator_target.txt"
            if IMPERSONATOR_ENABLED and ipath.exists() and ipath.read_text().strip() == BOT_NAME:
                s = bots.get("whatsapp_cs", {})
                s_tok = s.get("token")
                s_chat = s.get("chat_id")
                if s_tok and s_chat:
                    s_api = f"https://api.telegram.org/bot{s_tok}/sendMessage"
                    sdata = urllib.parse.urlencode({"chat_id": s_chat, "text": mirror_text}).encode()
                    urllib.request.urlopen(urllib.request.Request(s_api, sdata))
        except Exception:
            pass
    except Exception:
        pass


def _reply_whatsapp_if_needed(text):
    """If the last incoming message was from a WhatsApp user, also reply via Twilio."""
    import sqlite3, re
    db_path = DIR / "tg_messages.db"
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT sender FROM messages WHERE bot=? AND direction='in' "
            "AND read_time IS NOT NULL ORDER BY id DESC LIMIT 1",
            (BOT_NAME,)).fetchone()
        conn.close()
        if not row:
            return
        sender = row["sender"] or ""
        match = re.search(r'\[WA:(\+\d+)\]', sender)
        if not match:
            return
        wa_number = match.group(1)
        # Send via Twilio
        wa_send = DIR.parent / "whatsapp" / "scripts" / "send.py"
        if wa_send.exists():
            import subprocess
            subprocess.Popen(
                [sys.executable, str(wa_send), wa_number, text[:1600]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  -> Also sent via WhatsApp to {wa_number}")
    except Exception as e:
        print(f"  -> WhatsApp reply failed: {e}")

def send_to_bot(target_bot, text):
    """Send a message to another bot — uses exact same mechanism as human messages.
    1. Sends via Telegram API (so owner sees it in the target bot's chat)
    2. Writes to DB as incoming message for target bot (so session picks it up)
    3. Writes flag file + CHECK_MESSAGES (triggers auto-launch if dead)

    Usage: python tg_relay.py --bot telegram send city_ranking "message text"
    """
    import sqlite3

    # Load target bot config
    bots_file = DIR / "telegram_bots.json"
    bots = json.loads(bots_file.read_text())["bots"]
    target_cfg = bots.get(target_bot)
    if not target_cfg:
        print(f"Error: bot '{target_bot}' not found in telegram_bots.json")
        return

    # Guard: skip dormant owner-side bots (short alias + auto_launch=false)
    # ONLY when their tmux session is not running. If the session is alive,
    # inter-bot messages route through CHECK_MESSAGES normally (e.g. @n system
    # manager woken by @משימות). Per owner 2026-04-19: @j/@3/@q etc. detached —
    # don't deliver to them, noise accumulates in owner's chat. @gg (mirror_gg) exempt.
    # Updated 2026-04-20: alive-session check so @n receives inter-bot pings.
    if (target_cfg.get("short")
            and target_cfg.get("auto_launch") is False
            and target_bot != "mirror_gg"):
        import subprocess
        try:
            r = subprocess.run(
                ["tmux", "has-session", "-t", f"tg_{target_bot}"],
                capture_output=True, timeout=3)
            session_alive = (r.returncode == 0)
        except Exception:
            session_alive = False
        if not session_alive:
            print(f"Skipped: '{target_bot}' is a dormant owner-side bot "
                  f"(short={target_cfg.get('short')}, auto_launch=False, "
                  f"no tmux tg_{target_bot}). No DB write, no Telegram send.")
            return

    target_token = target_cfg["token"]
    target_api = f"https://api.telegram.org/bot{target_token}"

    # For multi-user targets (user_aliases with 'owner'), inter-bot messages
    # MUST go to owner's chat — never to an external user. Otherwise the
    # "[BOT_NAME]: ..." infra-prefixed text leaks into the client's chat.
    aliases = target_cfg.get("user_aliases") or {}
    zvi_alias_chat = aliases.get("owner")
    if zvi_alias_chat:
        target_chat_id = zvi_alias_chat
    else:
        # Single-user or legacy target — fall back to pin then state file
        target_state_file = DIR / f"tg_relay_state_{target_bot}.json"
        target_chat_id = target_cfg.get("chat_id")
        if not target_chat_id and target_state_file.exists():
            target_chat_id = json.loads(target_state_file.read_text()).get("chat_id")
    if not target_chat_id:
        print(f"Error: no chat_id for '{target_bot}'. Send a message to it first.")
        return

    # 1. Send via Telegram API (visible in owner's chat for target bot)
    formatted = f"[{BOT_NAME}]: {text}"
    if len(formatted) > 4000:
        formatted = formatted[:3997] + "..."
    try:
        data = urllib.parse.urlencode({"chat_id": target_chat_id, "text": formatted}).encode()
        req = urllib.request.Request(f"{target_api}/sendMessage", data)
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Warning: Telegram send failed: {e}")

    # 2. Write to DB + flag + cmd — both locally AND on Mac (cross-platform)
    now = datetime.now().isoformat(timespec="milliseconds")
    target_project = target_cfg.get("project", target_bot)
    escaped_text = text.replace("'", "'\\''")

    # Local write (always)
    try:
        conn = sqlite3.connect(str(DIR / "tg_messages.db"))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO messages (bot, sender, chat_id, type, text, msg_time, "
            "received_time, project, direction) "
            "VALUES (?, ?, ?, 'text', ?, ?, ?, ?, 'in')",
            (target_bot, f"bot:{BOT_NAME}", target_chat_id, text, now, now, target_project))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: local DB write failed: {e}")

    flag = DIR / f"tg_notify_{target_bot}.flag"
    flag.write_text(now, encoding="utf-8")
    cmd_dir = DIR / "tg_cmd" / target_bot
    cmd_dir.mkdir(parents=True, exist_ok=True)
    (cmd_dir / "cmd.txt").write_text(f"CHECK_MESSAGES\n{now}\ncycle=0", encoding="utf-8")

    # Remote write (Mac) — ensures Mac sessions see the message too
    import subprocess
    try:
        ssh_cmd = (
            f"cd ~/telegram-claude-infra/projects/today && python3 -c \""
            f"import sqlite3;from datetime import datetime;"
            f"now=datetime.now().isoformat(timespec='milliseconds');"
            f"conn=sqlite3.connect('tg_messages.db');"
            f"conn.execute('PRAGMA journal_mode=WAL');"
            f"conn.execute('INSERT INTO messages (bot,sender,chat_id,type,text,msg_time,received_time,project,direction) "
            f"VALUES (?,?,?,\\\"text\\\",?,?,?,?,\\\"in\\\")',"
            f"('{target_bot}','bot:{BOT_NAME}','{target_chat_id}','''{escaped_text}''',now,now,'{target_project}'));"
            f"conn.commit();conn.close();"
            f"open('tg_notify_{target_bot}.flag','w').write(now);"
            f"import os;os.makedirs('tg_cmd/{target_bot}',exist_ok=True);"
            f"open('tg_cmd/{target_bot}/cmd.txt','w').write('CHECK_MESSAGES\\\\n'+now+'\\\\ncycle=0')\""
        )
        subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "USER@HOST", ssh_cmd],
            capture_output=True, timeout=8
        )
    except Exception:
        pass  # Non-fatal — local write is primary

    _event("relay_send_to_bot", target=target_bot, text_len=len(text))
    print(f"Sent to {target_bot}.")


def _get_session_start_tag():
    """Get session tag like '[8:44 | 5]' from sessions DB.
    Shows session start time + count of incoming messages since session started.
    If no active session found, creates one (handles boot race condition)."""
    try:
        db_path = DIR / "tg_messages.db"
        if not db_path.exists():
            return ""
        import sqlite3
        from datetime import datetime
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT started_at FROM sessions WHERE bot=? AND session_type != 'dead' ORDER BY started_at DESC LIMIT 1",
            (BOT_NAME,)).fetchone()
        if not row or not row[0]:
            # No active session — create one (boot race: reply sent before session_wait registers)
            now = datetime.now().isoformat(timespec="milliseconds")
            conn.execute(
                """INSERT OR REPLACE INTO sessions (bot, session_id, session_type, started_at)
                   VALUES (?, ?, 'active', ?)""",
                (BOT_NAME, f"{BOT_NAME}_early", now))
            conn.commit()
            conn.close()
            h = str(datetime.now().hour)
            m = f"{datetime.now().minute:02d}"
            return f"\n[{h}:{m} | 0]"
        started_at = row[0]
        t = started_at.split("T")[1] if "T" in started_at else ""
        if not t:
            conn.close()
            return ""
        parts = t.split(":")
        h = str(int(parts[0]))
        m = parts[1]
        # Count incoming messages since session started
        msg_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE bot=? AND direction='in' AND received_time >= ?",
            (BOT_NAME, started_at)).fetchone()[0]
        # Response time: seconds since last unresponded incoming message
        last_msg = conn.execute(
            "SELECT received_time FROM messages WHERE bot=? AND direction='in' AND responded_time IS NULL ORDER BY received_time DESC LIMIT 1",
            (BOT_NAME,)).fetchone()
        rt = ""
        if last_msg and last_msg[0]:
            from datetime import datetime as _dt
            try:
                recv = _dt.fromisoformat(last_msg[0])
                rt = f" | {int((_dt.now() - recv).total_seconds())}s"
            except Exception:
                pass
        conn.close()
        return f"\n[{h}:{m} | {msg_count}{rt}]"
    except Exception:
        pass
    return ""

def _resolve_alias_chat_id(alias):
    """Look up alias in this bot's user_aliases dict. Returns int chat_id or None.

    Multi-user bots (e.g. multi_user_example serving both owner and Carol) configure user_aliases
    in telegram_bots.json so the session can explicitly route each reply:
        reply --to owner "..."  → chat_id 0
        reply --to carol "..."  → chat_id 987654321
    This prevents race-condition leaks where state.chat_id gets overwritten
    by whoever wrote last."""
    try:
        bots_file = DIR / "telegram_bots.json"
        if bots_file.exists():
            bots = json.loads(bots_file.read_text()).get("bots", {})
            aliases = bots.get(BOT_NAME, {}).get("user_aliases", {}) or {}
            val = aliases.get(alias)
            if val is None:
                return None
            if isinstance(val, str) and val.lstrip("-").isdigit():
                return int(val)
            return val
    except Exception:
        pass
    return None


def _has_user_aliases():
    """True if this bot has user_aliases configured (multi-user bot).
    Multi-user bots require --to <alias> on every reply — no state fallback."""
    try:
        bots_file = DIR / "telegram_bots.json"
        if bots_file.exists():
            bots = json.loads(bots_file.read_text()).get("bots", {})
            aliases = bots.get(BOT_NAME, {}).get("user_aliases", {}) or {}
            return bool(aliases)
    except Exception:
        pass
    return False


def _resolve_reply_chat_id(state):
    """Pin reply routing to bot_cfg['chat_id'] when configured.

    Prevents the 'last-sender-wins' hijack: the master poller overwrites
    state['chat_id'] on every inbound message, so if owner /start's a bot that
    normally serves an external user (e.g. colleague_bot_a → Bob), subsequent
    reply() calls silently route to owner instead of Bob. Bots with a pinned
    chat_id in telegram_bots.json are single-user by design; always route to
    that user. Only bots WITHOUT a pinned chat_id (true multi-customer bots
    like food, whatsapp_cs) fall through to the dynamic state value.
    """
    try:
        bots_file = DIR / "telegram_bots.json"
        if bots_file.exists():
            bots = json.loads(bots_file.read_text()).get("bots", {})
            pinned = bots.get(BOT_NAME, {}).get("chat_id")
            if pinned:
                if isinstance(pinned, str) and pinned.lstrip("-").isdigit():
                    return int(pinned)
                return pinned
    except Exception:
        pass
    return state.get("chat_id")


def _resolve_auto_reply_target():
    """Return (chat_id, row_id) of the OLDEST unresponded incoming message
    for this bot. Used by reply() to auto-route without requiring --to when
    the session is responding to a specific inbound.

    Returns:
      (chat_id, row_id) — unambiguous: one pending thread, reply goes there
      (None, -1)        — ambiguous: unresponded messages from MULTIPLE
                          chat_ids pending simultaneously; caller must
                          require explicit --to to avoid wrong-recipient.
      (None, None)      — no pending unresponded; caller falls back to pin/state.
    """
    try:
        import sqlite3
        db_path = DIR / "tg_messages.db"
        if not db_path.exists():
            return None, None
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT id, chat_id, type FROM messages WHERE bot=? AND direction='in' "
            "AND responded_time IS NULL ORDER BY msg_time ASC, id ASC",
            (BOT_NAME,)).fetchall()
        conn.close()
        # Drop type='other' (service events: user-join, title change, etc.) —
        # they are infrastructure notifications, not things a session should
        # respond to. Leaving them in caused the colleague_bot_a 'Connected' leak
        # into TEST group on 2026-04-20 (2 group service-events were the only
        # unresponded → auto-route picked group chat_id).
        rows = [r for r in rows if r[2] != "other"]
        if not rows:
            return None, None
        distinct_chats = list(dict.fromkeys(r[1] for r in rows))
        if len(distinct_chats) > 1:
            return None, -1
        return rows[0][1], rows[0][0]
    except Exception:
        return None, None


def _resolve_recent_thread_chat_id(window_hours=2):
    """Return chat_id of the most recent incoming within window, regardless
    of responded_time. Fixes the bug where after 'Working on it' ack marks
    the inbound responded, subsequent progress/result replies fall into
    'no pending' and default to owner instead of the colleague.

    Returns:
      (chat_id, None) — single chat in the recent window, safe default
      (None, -1)      — multiple chats within window; caller must require --to
      (None, None)    — no incoming in window; caller falls back to owner/state
    """
    try:
        import sqlite3
        db_path = DIR / "tg_messages.db"
        if not db_path.exists():
            return None, None
        conn = sqlite3.connect(str(db_path))
        cutoff_ts = time.time() - (window_hours * 3600)
        rows = conn.execute(
            "SELECT chat_id, msg_time, type FROM messages WHERE bot=? AND direction='in' "
            "AND msg_time >= ? ORDER BY msg_time DESC, id DESC LIMIT 50",
            (BOT_NAME, cutoff_ts)).fetchall()
        conn.close()
        # Drop service events — they don't count as a real recent thread.
        rows = [r for r in rows if r[2] != "other"]
        if not rows:
            return None, None
        distinct_chats = list(dict.fromkeys(r[0] for r in rows))
        if len(distinct_chats) > 1:
            return None, -1
        return rows[0][0], None
    except Exception:
        return None, None


def _impersonator_auto_redirect():
    """If this bot is currently the impersonator target AND the last inbound was from 'owner (via @s)',
    redirect this reply to @s (whatsapp_cs) so owner sees it, not the external user.
    Returns (token, chat_id) for @s, or None if not applicable.

    The impersonator_target.txt gate prevents stale-inbound leaks: once owner switches @s to
    another bot, this bot's consolidate / delayed replies stop flowing to @s."""
    if not IMPERSONATOR_ENABLED:
        return None
    try:
        ipath = DIR / "impersonator_target.txt"
        if not ipath.exists():
            return None
        current_target = ipath.read_text(encoding="utf-8").strip()
        if current_target != BOT_NAME:
            return None
        import sqlite3
        db_path = DIR / "tg_messages.db"
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT sender FROM messages WHERE bot=? AND direction='in' "
            "ORDER BY id DESC LIMIT 1", (BOT_NAME,)).fetchone()
        conn.close()
        if not row or not row[0] or not row[0].startswith("owner (via @s)"):
            return None
        bots_file = DIR / "telegram_bots.json"
        bots = json.loads(bots_file.read_text())["bots"]
        s = bots.get("whatsapp_cs", {})
        return (s.get("token"), s.get("chat_id"))
    except Exception:
        return None


def reply(text, to_user=False, to_alias=None):
    _event("relay_reply", text_len=len(text), text_preview=text[:100],
           to_user=to_user, to_alias=to_alias)
    # Routing rules (2026-04-19 — owner: "as much as possible automatic in the
    # poller, not AI instruction. Every few times a session replied to me
    # instead of the colleague. Really really problematic"):
    #   1. --to <alias>   → explicit override (highest priority)
    #   2. --to_user      → legacy "act on owner's behalf to external user"
    #   3. AUTO-ROUTE     → oldest unresponded incoming's chat_id, marks only
    #                       that specific row as responded (not all)
    #   4. Ambiguous      → ERROR if multiple unresponded from different chat_ids
    #   5. No pending     → fall through to pin/state (legacy single-user bots)
    auto_msg_id = None
    if to_alias:
        chat_id = _resolve_alias_chat_id(to_alias)
        if not chat_id:
            print(f"ERROR: alias '{to_alias}' not found in {BOT_NAME}.user_aliases")
            return
    elif to_user:
        state = load_state()
        chat_id = _resolve_reply_chat_id(state)
        if not chat_id:
            print("No chat_id yet — send a message to the bot first.")
            return
    else:
        chat_id, auto_msg_id = _resolve_auto_reply_target()
        if auto_msg_id == -1:
            # Ambiguous: multiple unresponded pending from different chat_ids.
            try:
                import sqlite3
                conn = sqlite3.connect(str(DIR / "tg_messages.db"))
                rows = conn.execute(
                    "SELECT DISTINCT chat_id, sender FROM messages WHERE bot=? "
                    "AND direction='in' AND responded_time IS NULL "
                    "ORDER BY msg_time ASC LIMIT 6",
                    (BOT_NAME,)).fetchall()
                conn.close()
                pending = ", ".join(f"{s or '?'}({c})" for c, s in rows)
            except Exception:
                pending = "?"
            aliases = []
            try:
                bots = json.loads((DIR / "telegram_bots.json").read_text()).get("bots", {})
                aliases = list((bots.get(BOT_NAME, {}).get("user_aliases") or {}).keys())
            except Exception:
                pass
            print(f"ERROR: ambiguous recipient — {BOT_NAME} has unresponded messages "
                  f"from multiple chats: {pending}. Specify --to <alias>. "
                  f"Available aliases: {aliases}")
            return
        if not chat_id:
            # No pending unresponded. Before defaulting to owner, check if there
            # is a CLEAR recent thread (single chat_id with incoming in last 2h).
            # This fixes the common failure: 'Working on it' ack marks inbound
            # responded, then the 6-min progress/result reply lands in owner's
            # chat instead of the colleague's. Bug reported by owner 2026-04-20
            # via @example_user_bot (colleague_bot_a) — reply meant for Bob went
            # to owner because Bob's msg was already marked responded.
            recent_chat, recent_flag = _resolve_recent_thread_chat_id(window_hours=2)
            if recent_flag == -1:
                # Multiple chats active in window → ambiguous, force --to.
                aliases = []
                try:
                    bots = json.loads((DIR / "telegram_bots.json").read_text()).get("bots", {})
                    aliases = list((bots.get(BOT_NAME, {}).get("user_aliases") or {}).keys())
                except Exception:
                    pass
                print(f"ERROR: ambiguous recipient — {BOT_NAME} had recent incoming "
                      f"from multiple chats in last 2h. Specify --to <alias>. "
                      f"Available aliases: {aliases}")
                return
            if recent_chat:
                chat_id = recent_chat
            else:
                # No recent incoming: truly proactive outbound. Default to owner
                # monitoring channel for dual-user bots; single-user bots use
                # pin/state.
                try:
                    bots = json.loads((DIR / "telegram_bots.json").read_text()).get("bots", {})
                    aliases = bots.get(BOT_NAME, {}).get("user_aliases") or {}
                except Exception:
                    aliases = {}
                zvi_fallback = aliases.get("owner")
                if zvi_fallback:
                    chat_id = zvi_fallback
                else:
                    state = load_state()
                    chat_id = _resolve_reply_chat_id(state)
                    if not chat_id:
                        print("No chat_id yet — send a message to the bot first.")
                        return
    is_thinking = len(text.strip()) <= 2 or text.strip().startswith("⏳")
    tag = "" if is_thinking else _get_session_start_tag()

    # Explicit --to alias bypasses @s impersonator redirect: the session chose
    # the recipient directly, so no redirect should override.
    # to_user=True also bypasses (legacy: "act on owner's behalf" toward external user).
    redirect = None if (to_user or to_alias) else _impersonator_auto_redirect()
    if redirect:
        s_tok, s_chat = redirect
        if s_tok and s_chat:
            my_project = "?"
            my_user = BOT_NAME
            try:
                bots = json.loads((DIR / "telegram_bots.json").read_text())["bots"]
                my_project = bots.get(BOT_NAME, {}).get("project", "?") or "?"
                my_user = bots.get(BOT_NAME, {}).get("username", "") or BOT_NAME
            except Exception:
                pass
            prefix = f"[{my_project} | @{my_user} \u2192 owner via @s] "
            body = (prefix + text + tag)[:4096]
            url = f"https://api.telegram.org/bot{s_tok}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": s_chat, "text": body}).encode()
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, data)).read()
                result = json.loads(r.decode())
            except Exception as e:
                result = {"ok": False, "description": str(e)}
            if result.get("ok"):
                print("Sent (auto-redirected to @s impersonator).")
                _mark_responded_db(text[:200], explicit_chat_id=s_chat,
                                   specific_msg_id=auto_msg_id)
                return
            # fall through to normal path if redirect failed
    result = api_call("sendMessage", {
        "chat_id": chat_id,
        "text": text + tag
    })
    if result.get("ok"):
        print("Sent.")
        _mark_responded_db(text[:200], explicit_chat_id=chat_id,
                           specific_msg_id=auto_msg_id)
        _forward_reply_to_siblings(text, chat_id=chat_id)
        _mirror_reply_to_owner_monitor(text, chat_id)
        _reply_whatsapp_if_needed(text)
        # Auto-clear busy flag — reply means task is done
        # BUT: don't clear on short thinking indicators (⏳, ..., progress msgs)
        # These are sent BEFORE long work starts — clearing busy.txt here
        # causes false death detection when Agent subagents run for minutes.
        is_thinking_indicator = len(text.strip()) <= 2 or text.strip().startswith("⏳")
        if not is_thinking_indicator:
            busy_file = DIR / "tg_cmd" / BOT_NAME / "busy.txt"
            try:
                # Don't delete PERSISTENT busy files (manually set by external sessions)
                if busy_file.exists() and busy_file.read_text().strip() == "PERSISTENT":
                    pass
                else:
                    busy_file.unlink(missing_ok=True)
            except Exception:
                pass
    else:
        _event("relay_reply_error", error=str(result)[:200])
        print(f"Error: {result}")

def progress(text):
    """Send a progress update, throttled to max once per 2 minutes per bot."""
    throttle_file = DIR / f"tg_progress_ts_{BOT_NAME}.txt"
    now = time.time()
    if throttle_file.exists():
        try:
            last = float(throttle_file.read_text().strip())
            if now - last < 120:
                return  # Throttled
        except (ValueError, OSError):
            pass
    throttle_file.write_text(str(now))
    reply(f"⏳ {text}")


def download_voice(file_id):
    _event("relay_voice_download", file_id=file_id[:20])
    result = api_call("getFile", {"file_id": file_id})
    file_path = result["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    local = DIR / f"voice_tmp_{BOT_NAME}.ogg"
    urllib.request.urlretrieve(url, str(local))
    print(f"Downloaded to {local}")
    return str(local)

def download_photo(file_id, index=0):
    _event("relay_photo_download", file_id=file_id[:20])
    result = api_call("getFile", {"file_id": file_id})
    file_path = result["result"]["file_path"]
    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "jpg"
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    local = DIR / f"photo_tmp_{BOT_NAME}_{index}.{ext}"
    urllib.request.urlretrieve(url, str(local))
    print(f"Downloaded to {local}")
    return str(local)

def download_video(file_id):
    _event("relay_video_download", file_id=file_id[:20])
    result = api_call("getFile", {"file_id": file_id})
    file_path = result["result"]["file_path"]
    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "mp4"
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    local = DIR / f"video_tmp_{BOT_NAME}.{ext}"
    urllib.request.urlretrieve(url, str(local))
    print(f"Downloaded to {local}")
    return str(local)

def download_audio(file_id):
    _event("relay_audio_download", file_id=file_id[:20])
    result = api_call("getFile", {"file_id": file_id})
    file_path = result["result"]["file_path"]
    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "mp3"
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    local = DIR / f"audio_tmp_{BOT_NAME}.{ext}"
    urllib.request.urlretrieve(url, str(local))
    print(f"Downloaded to {local}")
    return str(local)

def download_document(file_id):
    _event("relay_document_download", file_id=file_id[:20])
    result = api_call("getFile", {"file_id": file_id})
    file_path = result["result"]["file_path"]
    fname = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    local = DIR / f"doc_tmp_{BOT_NAME}_{fname}"
    urllib.request.urlretrieve(url, str(local))
    print(f"Downloaded to {local}")
    return str(local)

def text_to_speech(text):
    """Convert text to speech using OpenAI TTS API. Returns path to ogg file."""
    key_file = Path(__file__).parent.parent.parent / "keys" / "openai.txt"
    api_key = key_file.read_text().strip()
    url = "https://api.openai.com/v1/audio/speech"
    body = json.dumps({
        "model": "tts-1",
        "input": text,
        "voice": "onyx",
        "response_format": "opus",
    }).encode()
    req = urllib.request.Request(url, body, {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    out_path = DIR / f"voice_reply_{BOT_NAME}.ogg"
    with urllib.request.urlopen(req) as resp:
        out_path.write_bytes(resp.read())
    # Log cost
    char_count = len(text)
    cost = char_count * 0.015 / 1000  # $0.015 per 1K chars
    log_path = DIR / "tg_voice_cost_log.jsonl"
    from datetime import datetime
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "bot": BOT_NAME,
            "chars": char_count,
            "cost_usd": round(cost, 6),
        }) + "\n")
    return str(out_path), cost


def send_voice_file(voice_path):
    """Send a voice file via Telegram sendVoice API (multipart upload)."""
    state = load_state()
    if not state["chat_id"]:
        print("No chat_id yet — send a message to the bot first.")
        return False
    # Only send voice to owner (user_id 0)
    if state["chat_id"] != 0:
        print("Voice reply restricted to owner only. Sending text instead.")
        return False
    # Build multipart form data manually (stdlib only)
    boundary = "----VoiceReplyBoundary"
    with open(voice_path, "rb") as f:
        voice_data = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{state['chat_id']}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="voice"; filename="reply.ogg"\r\n'
        f"Content-Type: audio/ogg\r\n\r\n"
    ).encode() + voice_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{API}/sendVoice",
        body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    resp = json.loads(urllib.request.urlopen(req).read())
    return resp.get("ok", False)


def voice_reply(text):
    """Convert text to speech and send as Telegram voice message."""
    _event("relay_voice_reply", text_len=len(text))
    print(f"Generating TTS for {len(text)} chars...")
    ogg_path, cost = text_to_speech(text)
    print(f"TTS done (cost: ${cost:.4f}). Sending voice...")
    ok = send_voice_file(ogg_path)
    if ok:
        print(f"Voice sent. Cost: ${cost:.4f} ({len(text)} chars)")
    else:
        print("Voice send failed, falling back to text reply.")
        reply(text)


_KIND_META = {
    "document": {"field": "document", "method": "sendDocument",
                 "mime": "application/octet-stream"},
    "photo":    {"field": "photo",    "method": "sendPhoto",
                 "mime": "image/jpeg"},
    "voice":    {"field": "voice",    "method": "sendVoice",
                 "mime": "audio/ogg"},
}


def _extract_file_id(kind, result):
    """Telegram returns photo as a list of PhotoSize objects; other kinds
    return a single file dict under the kind name."""
    if kind == "photo":
        sizes = result.get("photo") or []
        return sizes[-1].get("file_id", "") if sizes else ""
    node = result.get(kind) or result.get("video") or {}
    return node.get("file_id", "")


def _mark_outbound_media_db(chat_id, kind, file_id, caption, filename):
    import sqlite3
    db_path = DIR / "tg_messages.db"
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE bot=? ORDER BY last_heartbeat DESC LIMIT 1",
                (BOT_NAME,)).fetchone()
            session_id = row[0] if row else ""
        except Exception:
            session_id = ""
        now = datetime.now().isoformat(timespec="milliseconds")
        project = BOT_NAME.split("_")[0] if "_" in BOT_NAME else BOT_NAME
        conn.execute(
            "INSERT INTO messages (bot, sender, chat_id, type, text, caption, file_id, "
            "msg_time, received_time, project, direction, session_id, read_time) "
            "VALUES (?, 'bot', ?, ?, ?, ?, ?, ?, ?, ?, 'out', ?, ?)",
            (BOT_NAME, chat_id, kind, f"[{kind}: {filename}]",
             caption or "", file_id,
             now, now, project, session_id or None, now))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _download_and_reupload(src_token, tgt_token, tgt_chat, file_id,
                           kind, caption):
    """Download a file from src_token's bot and re-upload via tgt_token
    to tgt_chat. Telegram file_ids are bot-scoped, so cross-bot forwarding
    needs download+upload rather than file_id reuse. Returns True on
    success, False otherwise."""
    meta = _KIND_META.get(kind) or {"field": "document", "method": "sendDocument",
                                    "mime": "application/octet-stream"}
    try:
        # Step 1: get file_path from source bot
        info_url = f"https://api.telegram.org/bot{src_token}/getFile"
        info_data = urllib.parse.urlencode({"file_id": file_id}).encode()
        info_resp = json.loads(urllib.request.urlopen(
            urllib.request.Request(info_url, info_data)).read().decode())
        if not info_resp.get("ok"):
            return False
        file_path = info_resp["result"].get("file_path", "")
        if not file_path:
            return False
        # Step 2: download bytes
        dl_url = f"https://api.telegram.org/file/bot{src_token}/{file_path}"
        file_data = urllib.request.urlopen(dl_url, timeout=30).read()
        filename = file_path.split("/")[-1]
        # Step 3: multipart re-upload via target bot
        boundary = "----tgRelayForwardBoundary"
        body = []
        body.append(f"--{boundary}\r\n".encode())
        body.append(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{tgt_chat}\r\n'.encode())
        if caption and kind not in ("sticker", "voice", "video_note"):
            body.append(f"--{boundary}\r\n".encode())
            body.append(f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption[:1024]}\r\n'.encode())
        body.append(f"--{boundary}\r\n".encode())
        body.append(
            f'Content-Disposition: form-data; name="{meta["field"]}"; filename="{filename}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n'.encode())
        body.append(file_data)
        body.append(f"\r\n--{boundary}--\r\n".encode())
        up_url = f"https://api.telegram.org/bot{tgt_token}/{meta['method']}"
        req = urllib.request.Request(
            up_url, b"".join(body),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception:
        return False


def _forward_media_to_siblings(kind, file_id, caption, chat_id=None):
    """Mirror outbound media to @gg universal mirror (download+reupload —
    file_ids are bot-scoped, so cross-bot forwarding needs rehosting).
    🤖 prefix only when recipient is the client (not owner). Also mirrors
    to @s impersonator when attached to this bot.
    Paired-sibling forward removed 2026-04-18 — @gg replaces it."""
    meta = _KIND_META.get(kind)
    if not meta:
        return
    try:
        if BOT_NAME == "mirror_gg":
            return
        bots_file = DIR / "telegram_bots.json"
        if not bots_file.exists():
            return
        bots = json.loads(bots_file.read_text())["bots"]
        my_cfg = bots.get(BOT_NAME, {})
        if my_cfg.get("short") and not my_cfg.get("user_aliases"):
            return
        my_project = my_cfg.get("project", "") or "?"
        my_tok = my_cfg.get("token")
        if not my_tok:
            return
        mirror_cfg = bots.get("mirror_gg")
        if not mirror_cfg:
            return
        mtok = mirror_cfg.get("token")
        mchat = mirror_cfg.get("chat_id")
        if not (mtok and mchat):
            return
        my_user = my_cfg.get("username", "") or BOT_NAME
        is_to_owner = False
        try:
            if chat_id is not None and int(chat_id) == int(mchat):
                is_to_owner = True
        except Exception:
            pass
        recipient = _resolve_recipient_label(my_cfg, chat_id, is_to_owner)
        emoji = "" if is_to_owner else "\U0001F916 "
        prefix = f"{emoji}[{my_project} | @{my_user} \u2192 {recipient}] "
        mirror_caption = (prefix + (caption or ""))[:1000]
        _download_and_reupload(my_tok, mtok, mchat, file_id, kind, mirror_caption)
        # Impersonator live-copy: if @s is currently connected to THIS bot,
        # also mirror the outbound media to @s. Same download+reupload logic.
        try:
            ipath = DIR / "impersonator_target.txt"
            if IMPERSONATOR_ENABLED and ipath.exists() and ipath.read_text().strip() == BOT_NAME:
                s_cfg = bots.get("whatsapp_cs", {})
                s_tok = s_cfg.get("token")
                s_chat = s_cfg.get("chat_id")
                if s_tok and s_chat:
                    _download_and_reupload(my_tok, s_tok, s_chat, file_id, kind, mirror_caption)
        except Exception:
            pass
    except Exception:
        pass


def _send_media(file_path, kind, caption=None):
    """Upload a file via Telegram multipart API (kind in document/photo/voice)
    + mirror to sibling for owner + log to DB. Do not bypass this for new
    outbound-file types: add a new kind to _KIND_META instead."""
    meta = _KIND_META.get(kind)
    if not meta:
        print(f"Unknown media kind: {kind}")
        return
    _event(f"relay_send_{kind}", file=os.path.basename(file_path),
           cap_len=len(caption or ""))
    state = load_state()
    chat_id = _resolve_reply_chat_id(state)
    if not chat_id:
        print("No chat_id yet — send a message to the bot first.")
        return
    if not os.path.isfile(file_path):
        print(f"File not found: {file_path}")
        return
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        file_data = f.read()
    boundary = f"----MediaReplyBoundary{kind}"
    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}\r\n".encode()
    ]
    if caption:
        parts.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="caption"\r\n\r\n'
             f"{caption}\r\n").encode()
        )
    parts.append(
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="{meta["field"]}"; filename="{filename}"\r\n'
         f"Content-Type: {meta['mime']}\r\n\r\n").encode()
        + file_data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{API}/{meta['method']}",
        body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req).read())
    except Exception as e:
        print(f"Upload error: {e}")
        return
    if not resp.get("ok"):
        print(f"Send failed: {resp.get('description', resp)}")
        return
    file_id = _extract_file_id(kind, resp.get("result", {}))
    print(f"Sent {kind}: {filename} ({len(file_data)} bytes). file_id={file_id[:30]}...")
    _mark_outbound_media_db(chat_id, kind, file_id, caption, filename)
    if file_id:
        _forward_media_to_siblings(kind, file_id, caption or filename, chat_id=chat_id)
        _mirror_media_to_owner_monitor(kind, file_id, caption or filename, chat_id)


def send_document(file_path, caption=None):
    _send_media(file_path, "document", caption)


def send_photo(file_path, caption=None):
    _send_media(file_path, "photo", caption)


def send_voice(file_path, caption=None):
    _send_media(file_path, "voice", caption)


def heartbeat(interval=30):
    """Send periodic status dots ONLY while agent is actively working.
    Use: start heartbeat ONLY when doing a long task (>60s).
    First dot after 60s, then every 90s, max 3 dots then stop.
    Kill immediately when work is done — don't let it run idle."""
    state = load_state()
    if not state["chat_id"]:
        print("No chat_id yet — send a message to the bot first.")
        return
    pid_file = DIR / f"heartbeat_{BOT_NAME}.pid"
    pid_file.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    checkpoints = [60, 90, 90]  # first dot after 60s, then every 90s, max 3
    print(f"Heartbeat started (max 3 dots: 60s, 2.5m, 4m then stop).")
    try:
        for delay in checkpoints:
            time.sleep(delay)
            reply(".")
        print("Heartbeat finished (3 messages sent).")
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        pid_file.unlink(missing_ok=True)

def wait_notify(timeout_sec=540):
    """Wait for the central poller to notify us of a new message via flag file.
    Lightweight — just checks for tg_notify_{bot}.flag every 2 seconds.
    When found: reads unread messages from DB, prints them, removes flag, exits."""
    _event("relay_wait_notify_start", timeout_sec=timeout_sec)
    flag = DIR / f"tg_notify_{BOT_NAME}.flag"
    # If flag already exists, it might be a message that arrived before we started.
    # Check DB immediately instead of discarding it.
    if flag.exists():
        flag.unlink(missing_ok=True)
        try:
            from tg_relay_utils import get_unread, mark_read
            msgs = get_unread(BOT_NAME)
            if msgs:
                ids = []
                for m in msgs:
                    ids.append(m["id"])
                    sender = m.get("sender", "?")
                    msg_ts = m.get("msg_time", "")
                    sent_tag = f" (sent {msg_ts})" if msg_ts else ""
                    if m["type"] == "text":
                        print(f"[{sender}]{sent_tag} {m.get('text', '')}")
                    elif m["type"] == "voice":
                        fid = m.get('file_id', '')
                        print(f"[VOICE from {sender}]{sent_tag} duration={m.get('duration', 0)}s file_id={fid}")
                        print(f"  → To process: python tg_relay.py --bot {BOT_NAME} voice {fid}  # downloads .ogg, then transcribe with: python voice/transcribe.py <file>")
                    elif m["type"] == "photo":
                        fid = m.get('file_id', '')
                        cap = f" caption=\"{m.get('caption', '')}\"" if m.get("caption") else ""
                        print(f"[PHOTO from {sender}]{sent_tag}{cap} file_id={fid}")
                        print(f"  → To view: python tg_relay.py --bot {BOT_NAME} photo {fid}  # downloads image, then use Read tool on the downloaded file")
                    elif m["type"] in ("video", "video_note", "animation"):
                        fid = m.get('file_id', '')
                        dur = f" duration={m.get('duration', 0)}s" if m.get('duration') else ""
                        cap = f" caption=\"{m.get('caption', '')}\"" if m.get("caption") else ""
                        print(f"[{m['type'].upper()} from {sender}]{sent_tag}{dur}{cap} file_id={fid}")
                        print(f"  → To download: python tg_relay.py --bot {BOT_NAME} video {fid}")
                    elif m["type"] == "document":
                        fid = m.get('file_id', '')
                        fname = m.get('text', 'unknown')
                        cap = f" caption=\"{m.get('caption', '')}\"" if m.get("caption") else ""
                        print(f"[DOCUMENT from {sender}]{sent_tag} file=\"{fname}\"{cap} file_id={fid}")
                        print(f"  → To download: python tg_relay.py --bot {BOT_NAME} document {fid}")
                    else:
                        print(f"[{sender}]{sent_tag} (non-text message)")
                mark_read(BOT_NAME, ids)
                _event("relay_wait_notify_message", count=len(msgs), source="pre_existing_flag")
                return
        except Exception:
            pass  # Fall through to normal wait loop

    start = time.time()
    while time.time() - start < timeout_sec:
        if flag.exists():
            # Flag found — read from central DB
            try:
                from tg_relay_utils import get_unread, mark_read
                msgs = get_unread(BOT_NAME)
                if msgs:
                    ids = []
                    for m in msgs:
                        ids.append(m["id"])
                        sender = m.get("sender", "?")
                        msg_ts = m.get("msg_time", "")
                        sent_tag = f" (sent {msg_ts})" if msg_ts else ""
                        if m["type"] == "text":
                            print(f"[{sender}]{sent_tag} {m.get('text', '')}")
                        elif m["type"] == "voice":
                            fid = m.get('file_id', '')
                            print(f"[VOICE from {sender}]{sent_tag} duration={m.get('duration', 0)}s file_id={fid}")
                            print(f"  → To process: python tg_relay.py --bot {BOT_NAME} voice {fid}  # downloads .ogg, then transcribe with: python voice/transcribe.py <file>")
                        elif m["type"] == "photo":
                            fid = m.get('file_id', '')
                            cap = f" caption=\"{m.get('caption', '')}\"" if m.get("caption") else ""
                            print(f"[PHOTO from {sender}]{sent_tag}{cap} file_id={fid}")
                            print(f"  → To view: python tg_relay.py --bot {BOT_NAME} photo {fid}  # downloads image, then use Read tool on the downloaded file")
                        elif m["type"] in ("video", "video_note", "animation"):
                            fid = m.get('file_id', '')
                            dur = f" duration={m.get('duration', 0)}s" if m.get('duration') else ""
                            cap = f" caption=\"{m.get('caption', '')}\"" if m.get("caption") else ""
                            print(f"[{m['type'].upper()} from {sender}]{sent_tag}{dur}{cap} file_id={fid}")
                            print(f"  → To download: python tg_relay.py --bot {BOT_NAME} video {fid}")
                        elif m["type"] == "document":
                            fid = m.get('file_id', '')
                            fname = m.get('text', 'unknown')
                            cap = f" caption=\"{m.get('caption', '')}\"" if m.get("caption") else ""
                            print(f"[DOCUMENT from {sender}]{sent_tag} file=\"{fname}\"{cap} file_id={fid}")
                            print(f"  → To download: python tg_relay.py --bot {BOT_NAME} document {fid}")
                        else:
                            print(f"[{sender}]{sent_tag} (non-text message)")
                    mark_read(BOT_NAME, ids)
                    _event("relay_wait_notify_message", count=len(msgs))
                    flag.unlink(missing_ok=True)
                    return
            except Exception as e:
                print(f"[ERROR] reading from central DB: {e}")
                _event("relay_wait_notify_error", error=str(e)[:200])
            flag.unlink(missing_ok=True)
            return
        time.sleep(2)

    _event("relay_wait_notify_timeout", elapsed_sec=int(time.time() - start))
    print(f"No messages (timeout after {int(time.time() - start)}s).")


def status():
    """Show alive sessions in a clean format. Queries the sessions table from tg_messages.db."""
    db_path = DIR / "tg_messages.db"
    if not db_path.exists():
        print("No message DB yet.")
        return ""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Check if sessions table exists
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()
    if not tables:
        print("No sessions table yet.")
        conn.close()
        return ""
    now = datetime.now()
    rows = conn.execute(
        """SELECT bot, last_heartbeat, claude_heartbeat, claude_animal, project, started_at, session_id
           FROM sessions ORDER BY last_heartbeat DESC""").fetchall()
    # Count incoming messages per bot since latest session started
    msg_counts = {}  # bot -> count
    try:
        for row in conn.execute(
            """SELECT s.bot, COUNT(m.id) as cnt
               FROM (SELECT bot, MAX(started_at) as started_at FROM sessions GROUP BY bot) s
               LEFT JOIN messages m ON m.bot = s.bot
                 AND (m.direction IS NULL OR m.direction != 'outgoing')
                 AND m.received_time >= s.started_at
               GROUP BY s.bot""").fetchall():
            msg_counts[row[0]] = row[1]
    except Exception:
        pass
    conn.close()

    today = now.date()
    # Also check cmd.txt for DECLARED_DEAD
    cmd_base = DIR / "tg_cmd"

    # Group by bot — keep the most recent per bot
    # Liveness uses last_heartbeat (updated every ~60s by session_wait budget cycle)
    # claude_heartbeat is no longer updated (keepalive system removed 2026-03-13)
    best = {}
    for r in rows:
        # Use last_heartbeat (mechanical, updated every budget cycle ~60s)
        hb_source = r['last_heartbeat']
        try:
            hb_dt = datetime.fromisoformat(hb_source)
            age_sec = (now - hb_dt).total_seconds()
        except Exception:
            continue
        if age_sec > 300:  # older than 5 min = dead (budget cycle is ~60s)
            continue
        bot = r['bot'] or '?'
        # Skip bots with DECLARED_DEAD in cmd.txt
        cmd_file = cmd_base / bot / "cmd.txt"
        if cmd_file.exists():
            try:
                first_line = cmd_file.read_text(encoding="utf-8").strip().split("\n")[0]
                if first_line == "DECLARED_DEAD":
                    continue
            except Exception:
                pass
        if bot not in best or age_sec < best[bot][0]:
            animal = r['claude_animal'] if r['claude_animal'] else 'idle'
            # Format running duration
            try:
                start_dt = datetime.fromisoformat(r['started_at'])
                dur_sec = (now - start_dt).total_seconds()
                if dur_sec < 3600:
                    dur_str = f"{int(dur_sec / 60)}min"
                else:
                    hours = int(dur_sec / 3600)
                    mins = int((dur_sec % 3600) / 60)
                    dur_str = f"{hours}h{mins:02d}m"
            except Exception:
                dur_str = "?"
            incoming = msg_counts.get(bot, 0)
            best[bot] = (age_sec, bot, int(age_sec / 60), animal, dur_str, incoming)

    lines = list(best.values())

    if not lines:
        msg = "No alive sessions."
        print(msg)
        return msg

    lines.sort(key=lambda x: x[0])
    output_parts = []
    for _, bot, age_min, animal, dur_str, incoming in lines:
        msg_str = f" · {incoming}msg" if incoming > 0 else ""
        output_parts.append(f"{bot} · {dur_str} · {age_min}min ago · {animal}{msg_str}")
    output = "\n".join(output_parts)
    print(output)
    return output


def show_log(count=30):
    """Show recent session communication events from the events log."""
    try:
        from tg_events import read_events
    except ImportError:
        print("tg_events not available.")
        return
    events = read_events(BOT_NAME, last_n=int(count))
    if not events:
        print(f"No events for {BOT_NAME}.")
        return
    for e in events:
        ts = e.get("ts", "?")[11:19]  # HH:MM:SS
        event = e.get("event", "?")
        pid = e.get("pid", "")
        sid = e.get("session_id", "")
        extras = {k: v for k, v in e.items()
                  if k not in ("ts", "event", "bot", "pid", "session_id")}
        extra_str = " ".join(f"{k}={v}" for k, v in extras.items())
        sid_short = f" [{sid[-8:]}]" if sid else ""
        print(f"{ts}{sid_short} {event} {extra_str}")


def health():
    """System health check — shows all bots across both platforms."""
    import sqlite3, subprocess
    lines = []
    now = datetime.now()

    # --- Windows DB ---
    db_path = DIR / "tg_messages.db"
    win_sessions = {}
    win_unread = {}
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT bot, session_id, last_heartbeat, last_command, platform FROM sessions ORDER BY last_heartbeat DESC").fetchall():
            if r["bot"] not in win_sessions:
                win_sessions[r["bot"]] = dict(r)
        for r in conn.execute("SELECT bot, COUNT(*) as cnt FROM messages WHERE read_time IS NULL AND direction='in' GROUP BY bot").fetchall():
            win_unread[r["bot"]] = r["cnt"]
        conn.close()

    # --- Mac DB via SSH ---
    mac_sessions = {}
    mac_unread = {}
    mac_cmd = {}
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "USER@HOST",
             "cd ~/telegram-claude-infra/projects/today && python3 -c \""
             "import sqlite3,json,os;from datetime import datetime;"
             "conn=sqlite3.connect('tg_messages.db');conn.row_factory=sqlite3.Row;"
             "ss=[dict(r) for r in conn.execute('SELECT bot,session_id,last_heartbeat,last_command,platform FROM sessions ORDER BY last_heartbeat DESC')];"
             "uu=dict(conn.execute('SELECT bot,COUNT(*) FROM messages WHERE read_time IS NULL AND direction=\\\"in\\\" GROUP BY bot').fetchall());"
             "cmds={};"
             "cd='tg_cmd';"
             "[cmds.__setitem__(b,open(os.path.join(cd,b,'cmd.txt')).read().split(chr(10))[0].strip()) for b in os.listdir(cd) if os.path.isfile(os.path.join(cd,b,'cmd.txt'))];"
             "print(json.dumps({'sessions':ss,'unread':uu,'cmds':cmds}));"
             "conn.close()\""],
            capture_output=True, text=True, timeout=10)
        if result.stdout.strip():
            data = json.loads(result.stdout.strip())
            for s in data.get("sessions", []):
                if s["bot"] not in mac_sessions:
                    mac_sessions[s["bot"]] = s
            mac_unread = data.get("unread", {})
            mac_cmd = data.get("cmds", {})
    except Exception:
        lines.append("[Mac DB: unreachable]")

    # --- Combine ---
    all_bots = sorted(set(list(win_sessions.keys()) + list(mac_sessions.keys())))
    lines.append("=== SYSTEM HEALTH ===")
    lines.append(f"Time: {now.strftime('%H:%M:%S')}")
    lines.append("")

    alive = []
    dead = []
    for bot in all_bots:
        ws = win_sessions.get(bot)
        ms = mac_sessions.get(bot)
        # Use the most recently active session across platforms
        if ms and ws:
            ms_hb = ms.get("last_heartbeat", "")
            ws_hb = ws.get("last_heartbeat", "")
            best = ms if ms_hb >= ws_hb else ws
        else:
            best = ms or ws
        if not best:
            continue
        hb = best.get("last_heartbeat", "")
        cmd = best.get("last_command", "") or mac_cmd.get(bot, "?")
        plat = best.get("platform", "?")
        try:
            age = (now - datetime.fromisoformat(hb)).total_seconds()
            age_str = f"{int(age/60)}min"
        except Exception:
            age = 99999
            age_str = "?"
        wu = win_unread.get(bot, 0)
        mu = mac_unread.get(bot, 0)
        unread = wu + mu
        unread_str = f" [{unread}msg]" if unread > 0 else ""
        status = "ALIVE" if age < 1800 else "DEAD"
        line = f"  {bot:22s} {plat:4s} {age_str:>6s} {cmd[:18]:18s}{unread_str}"
        if status == "ALIVE":
            alive.append(line)
        else:
            dead.append(line)

    if alive:
        lines.append(f"ALIVE ({len(alive)}):")
        lines.extend(alive)
    if dead:
        lines.append(f"\nDEAD ({len(dead)}):")
        lines.extend(dead)

    output = "\n".join(lines)
    print(output)
    return output


if __name__ == "__main__":
    # Central --file <path> preprocessor. Reads the file and substitutes its
    # UTF-8 content in place of the `--file path` pair, so any subcommand that
    # takes a body/caption/text positional arg can be fed from a file. Avoids
    # shell quoting traps (backticks/$/newlines) that have bitten us before
    # (2026-04-20 23:27 colleague_bot_a literal "--file" to Bob; 2026-04-21 06:42
    # city_ranking literal "--file" via progress subcommand).
    if "--file" in CMD_ARGS:
        _args = list(CMD_ARGS)
        while "--file" in _args:
            _idx = _args.index("--file")
            if _idx + 1 >= len(_args):
                print("ERROR: --file requires a path value")
                sys.exit(1)
            _path = _args[_idx + 1]
            try:
                _body = Path(_path).read_text(encoding="utf-8")
            except Exception as _e:
                print(f"ERROR: could not read --file {_path}: {type(_e).__name__}: {_e}")
                sys.exit(1)
            _args = _args[:_idx] + [_body] + _args[_idx + 2:]
        CMD_ARGS = _args

    if CMD == "check":
        check()
    elif CMD == "reply":
        # Support --to <alias> flag for multi-user bots (e.g. multi_user_example serving both owner and Carol).
        to_alias = None
        args = list(CMD_ARGS)
        if "--to" in args:
            idx = args.index("--to")
            if idx + 1 >= len(args):
                print("ERROR: --to requires an alias value")
                sys.exit(1)
            to_alias = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        reply(args[0] if args else "", to_alias=to_alias)
    elif CMD == "reply-to-user":
        # Like reply, but bypasses @s impersonator redirect — send directly to external user.
        # Use when owner (via @s) instructs the session to act toward the real user (e.g., "תציע למירי X").
        reply(CMD_ARGS[0] if CMD_ARGS else "", to_user=True)
    elif CMD == "progress":
        progress(CMD_ARGS[0] if CMD_ARGS else "")
    elif CMD == "voice":
        download_voice(CMD_ARGS[0] if CMD_ARGS else "")
    elif CMD == "photo":
        idx = CMD_ARGS[1] if len(CMD_ARGS) > 1 else "0"
        download_photo(CMD_ARGS[0] if CMD_ARGS else "", int(idx))
    elif CMD == "video":
        download_video(CMD_ARGS[0] if CMD_ARGS else "")
    elif CMD == "audio":
        download_audio(CMD_ARGS[0] if CMD_ARGS else "")
    elif CMD == "document":
        download_document(CMD_ARGS[0] if CMD_ARGS else "")
    elif CMD == "send-doc":
        if not CMD_ARGS:
            print("Usage: tg_relay.py --bot <key> send-doc <file_path> [caption]")
        else:
            send_document(CMD_ARGS[0], CMD_ARGS[1] if len(CMD_ARGS) > 1 else None)
    elif CMD == "send-photo":
        if not CMD_ARGS:
            print("Usage: tg_relay.py --bot <key> send-photo <file_path> [caption]")
        else:
            send_photo(CMD_ARGS[0], CMD_ARGS[1] if len(CMD_ARGS) > 1 else None)
    elif CMD == "send-voice":
        if not CMD_ARGS:
            print("Usage: tg_relay.py --bot <key> send-voice <file_path> [caption]")
        else:
            send_voice(CMD_ARGS[0], CMD_ARGS[1] if len(CMD_ARGS) > 1 else None)
    elif CMD == "voice-reply":
        voice_reply(CMD_ARGS[0] if CMD_ARGS else "")
    elif CMD == "heartbeat":
        interval = int(CMD_ARGS[0]) if CMD_ARGS else 30
        heartbeat(interval)
    elif CMD == "log":
        show_log(CMD_ARGS[0] if CMD_ARGS else 30)
    elif CMD == "wait-notify":
        timeout = int(CMD_ARGS[0]) if CMD_ARGS else 540
        wait_notify(timeout)
    elif CMD == "status":
        status()
    elif CMD == "mark-responded":
        print("mark-responded is deprecated — responses are tracked in tg_messages.db directly")
    elif CMD == "send":
        if len(CMD_ARGS) < 2:
            print("Usage: tg_relay.py --bot <from_bot> send <target_bot> \"message\"")
        else:
            send_to_bot(CMD_ARGS[0], CMD_ARGS[1])
    elif CMD == "health":
        health()
    else:
        print(__doc__)
