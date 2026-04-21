"""
Master Poller — single-process Telegram poller + session lifecycle manager.

Architecture:
  - ONE process, internal threads for concurrent polling
  - Polls all enabled bots from telegram_bots.json using ThreadPoolExecutor
  - Messages → SQLite DB → notify session via cmd.txt + SIGUSR1
  - Hash commands (?, #, ##, ###) handled at poller level (instant response)
  - Dead bots with unread → auto-launch tmux session
  - DB archiving every 12h

Usage:
  python tg_master_poller.py              # start poller
  python tg_master_poller.py --status     # show session status
  python tg_master_poller.py --stop       # stop running poller
"""
import sys, json, os, time, sqlite3, signal, subprocess, threading, urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from events import write_event
except Exception:
    def write_event(*a, **k):  # fallback no-op if events module missing
        pass

DIR = Path(__file__).resolve().parent
DB_PATH = DIR / "tg_messages.db"
BOTS_FILE = DIR / "telegram_bots.json"
PID_FILE = DIR / "tg_master_poller.pid"
HEARTBEAT_FILE = DIR / "tg_master_poller.heartbeat"
CMD_DIR = DIR / "tg_cmd"
PENDING_LAUNCH_DIR = DIR / "pending_launches"  # ### relaunch requests persisted across poller restarts
TMUX = "/opt/homebrew/bin/tmux"

# Timing
POLL_INTERVAL = 2       # seconds between poll cycles (fast cadence — active sessions)
POLL_INTERVAL_SLOW = 60 # seconds — dormant bots (no tmux, no session_wait) polled at this cadence
IDLE_ALIVE_THRESHOLD = 1200  # 20 min — live session with no inbound for this long drops to slow cadence
                             # (owner 2026-04-20: reduces wasted polls on idle-listening sessions; active
                             # conversations stay fast, silent sessions share the slow-cadence pool.)
POLL_TIMEOUT = 3        # Telegram API timeout per request — keep SHORT to prevent cycle freezes
MAX_POLL_WORKERS = 64   # concurrent polling threads — must exceed bot count so a full poll
                        # batch fits in (POLL_TIMEOUT + 5)s. With 51 eligible bots and 10 workers,
                        # ~5 serial batches × 3s timeout = ~15s > 8s deadline → as_completed
                        # TimeoutError floods (~640 tracebacks in 4h observed 2026-04-21).
DEAD_CHECK_EVERY = 5    # cycles between death sweeps (~10s)
CONSOLIDATE_INTERVAL = 1800  # (deprecated — consolidate now fires at round hour, kept for compatibility)
ARCHIVE_INTERVAL = 3600 * 12

# Session health
DEAD_STALENESS = 180    # seconds — unconsumed cmd = dead
LAUNCH_COOLDOWN = 120   # seconds — don't re-launch same bot

# Idle restart
IDLE_THRESHOLD = 3600       # 60 min — trigger restart after this much idle time
IDLE_OVERRIDE_WINDOW = 600  # 10 min — user can send message to cancel restart

# HUNG-IDLE auto-recovery
HUNG_IDLE_GRACE = 180       # 3 min — wait this long before restarting a HUNG-IDLE session

# Impersonator @s — disabled 2026-04-20 per owner. Set True to re-enable the
# @s routing path: live-copy of attached bot's incoming to @s, @s "bots"/digit
# meta-commands, @username/cached-target injection, and # hash-cmd inspector
# resolution. impersonator_target.txt and impersonator_last_list.json on disk
# remain untouched so re-enable restores prior state.
IMPERSONATOR_ENABLED = False

_running = True
_recent_launches = {}
_hung_idle_first_seen = {}  # bot_key → epoch when HUNG-IDLE+unread first detected
_all_bots = {}
_enabled_bots = []
_daemon_cfg = {}
_also_watch_reverse = {}  # bot_key → list of bots that have it in their also_watch
_idle_restart_state = {}    # bot_key → {"notified_at": float}
_idle_restart_done = {}     # bot_key → timestamp of last idle restart (prevents restart loops)
_last_poll_time = {}        # bot_key → epoch of last actual poll (for adaptive slow-cadence)
_last_incoming_ts = {}      # bot_key → epoch of last inbound message (for 3-tier cadence: idle-alive slowdown)
_hash_restart_queue = set() # bots needing immediate relaunch after ### (consumed by main loop)

def _signal_handler(sig, frame):
    global _running
    _running = False

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


# ── Config ──

def load_bots():
    global _enabled_bots, _all_bots, _daemon_cfg, _also_watch_reverse
    if not BOTS_FILE.exists():
        return {}
    data = json.loads(BOTS_FILE.read_text(encoding="utf-8"))
    _daemon_cfg = data.get("daemon", {})
    _enabled_bots = _daemon_cfg.get("enabled_bots", [])
    bots = {}
    for key, cfg in data.get("bots", {}).items():
        if not cfg.get("token"):
            continue
        cfg["_key"] = key
        cfg["_allowed"] = cfg.get("allowed_user_ids", data.get("allowed_user_ids", []))
        bots[key] = cfg
    _all_bots = bots
    # Build reverse mapping: which bots are watching each bot
    rev = {}
    for key, cfg in bots.items():
        for watched in cfg.get("also_watch", []):
            rev.setdefault(watched, []).append(key)
    _also_watch_reverse = rev
    return bots


OWNER_USER_ID = 0

def is_enabled(bot_key, cfg):
    """Check if bot is eligible for polling (not disabled/standby, correct platform)."""
    if cfg.get("disabled") or cfg.get("is_standby"):
        return False
    if cfg.get("platform", "mac") != "mac":
        return False
    return True


def has_external_users(cfg):
    """Check if bot has non-owner users (alice, dave, etc.) — always poll these."""
    allowed = cfg.get("allowed_user_ids", [])
    if not allowed:
        return False
    return any(uid != OWNER_USER_ID for uid in allowed)


DEATH_LOG_DIR = DIR / "death_logs"

def _capture_death_log(bot_key):
    """Capture last 50 lines of tmux pane before killing session."""
    tmux_name = f"tg_{bot_key}"
    try:
        r = subprocess.run(
            [TMUX, "capture-pane", "-t", f"={tmux_name}", "-p", "-S", "-50"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            DEATH_LOG_DIR.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = DEATH_LOG_DIR / f"{bot_key}_{ts}.txt"
            log_file.write_text(f"Death capture for {bot_key} at {ts}\n{'='*60}\n{r.stdout}", encoding="utf-8")
            _log(f"[DEATH-LOG] {bot_key} — captured {len(r.stdout.splitlines())} lines → {log_file.name}")
    except Exception as e:
        _log(f"[DEATH-LOG] {bot_key} — capture failed: {e}")


_tmux_sessions_cache = set()
_tmux_cache_time = 0

def _refresh_tmux_cache():
    """Cache tmux session list — ONE subprocess call instead of N."""
    global _tmux_sessions_cache, _tmux_cache_time
    if time.time() - _tmux_cache_time < 5:
        return
    try:
        r = subprocess.run(
            [TMUX, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            _tmux_sessions_cache = set(r.stdout.strip().split("\n"))
        else:
            _tmux_sessions_cache = set()
    except Exception:
        _tmux_sessions_cache = set()
    _tmux_cache_time = time.time()


def _has_live_session(bot_key):
    """True if bot has active tmux session or live session_wait PID."""
    if f"tg_{bot_key}" in _tmux_sessions_cache:
        return True
    sw_pid = DIR / f"tg_session_{bot_key}.pid"
    if sw_pid.exists():
        try:
            pid = int(sw_pid.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    return False


def should_poll(bot_key, cfg):
    """Determine if bot is eligible for polling.
    Poll if: has active tmux session OR has external users OR has session_wait PID.
    (Cadence — fast vs slow — is decided separately by should_poll_now.)"""
    if not is_enabled(bot_key, cfg):
        return False
    if cfg.get("self_poll"):
        return False  # Bot has its own poller — skip to avoid 409 Conflict
    # Always poll bots with external users — detect their messages to auto-launch
    if has_external_users(cfg):
        return True
    if _has_live_session(bot_key):
        return True
    return False


def should_poll_now(bot_key, cfg, now_ts):
    """Adaptive-cadence gate — 3 tiers (owner 2026-04-20):
    • Active  (live session + inbound in last 20 min) → every cycle (fast, 2s)
    • Idle-alive (live session + no inbound 20+ min)   → slow cadence (60s)
    • Dormant (no live session)                        → slow cadence (60s)

    Rationale: ~50 bots × 2s polls = 1500 req/min, most wasted on listening
    sessions with no activity. Active conversations stay 2s responsive; dead
    bots still poll every 60s so DEAD-ON-MSG fires within a minute."""
    if _has_live_session(bot_key):
        last_in = _last_incoming_ts.get(bot_key, 0)
        if last_in and (now_ts - last_in) < IDLE_ALIVE_THRESHOLD:
            return True  # active conversation
        # Fall through to slow cadence for idle-alive
    last = _last_poll_time.get(bot_key, 0)
    return (now_ts - last) >= POLL_INTERVAL_SLOW


def load_state(bot_key):
    f = DIR / f"tg_relay_state_{bot_key}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"offset": 0, "chat_id": None}


def save_state(bot_key, state):
    f = DIR / f"tg_relay_state_{bot_key}.json"
    if f.exists():
        try:
            old = json.loads(f.read_text(encoding="utf-8"))
            if state.get("offset", 0) < old.get("offset", 0):
                state["offset"] = old["offset"]
        except Exception:
            pass
    f.write_text(json.dumps(state), encoding="utf-8")


# ── Telegram API ──

def api_call(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT) as resp:
        return json.loads(resp.read())


def send_reply(token, chat_id, text, reply_to=None):
    try:
        params = {"chat_id": chat_id, "text": text}
        if reply_to:
            params["reply_to_message_id"] = reply_to
        api_call(token, "sendMessage", params)
        return True
    except Exception as e:
        _log(f"[SEND-FAIL] chat_id={chat_id}: {e}")
        return False


def _resend_file_via_sibling(src_token, sib_token, sib_chat_id,
                             file_id, msg_type, sender, caption_text):
    """Download file from source bot, re-send via sibling bot to owner.
    This works even when owner hasn't started a chat with the source bot."""
    import urllib.request as req
    try:
        # Step 1: get file path from source bot
        file_info = api_call(src_token, "getFile", {"file_id": file_id})
        file_path = file_info.get("result", {}).get("file_path", "")
        if not file_path:
            send_reply(sib_token, sib_chat_id, f"\U0001F464 {sender}: [{msg_type}] {caption_text}")
            return

        # Step 2: download file bytes
        download_url = f"https://api.telegram.org/file/bot{src_token}/{file_path}"
        file_data = req.urlopen(download_url).read()
        filename = file_path.split("/")[-1]

        # Step 3: re-send via sibling bot using multipart upload
        # Map message types to Telegram send methods
        type_to_method = {
            "photo": "sendPhoto", "voice": "sendVoice", "document": "sendDocument",
            "video": "sendVideo", "video_note": "sendVideoNote",
            "animation": "sendAnimation", "audio": "sendAudio", "sticker": "sendSticker",
        }
        type_to_field = {
            "photo": "photo", "voice": "voice", "document": "document",
            "video": "video", "video_note": "video_note",
            "animation": "animation", "audio": "audio", "sticker": "sticker",
        }
        method = type_to_method.get(msg_type, "sendDocument")
        field = type_to_field.get(msg_type, "document")

        # Build multipart form data
        import io
        boundary = "----FormBoundary7MA4YWxkTrZu0gW"
        body = io.BytesIO()

        # chat_id field
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{sib_chat_id}\r\n'.encode())

        # caption field (sender info)
        emoji = "\U0001F464"
        cap = f"{emoji} {sender}: {caption_text}" if caption_text else f"{emoji} {sender}"
        if msg_type not in ("sticker", "voice", "video_note"):
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="caption"\r\n\r\n{cap[:1024]}\r\n'.encode())

        # file field
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode())
        body.write(f"Content-Type: application/octet-stream\r\n\r\n".encode())
        body.write(file_data)
        body.write(f"\r\n--{boundary}--\r\n".encode())

        url = f"https://api.telegram.org/bot{sib_token}/{method}"
        request = req.Request(url, body.getvalue(),
                              {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        req.urlopen(request, timeout=30)
    except Exception as e:
        # Fallback to text description
        try:
            send_reply(sib_token, sib_chat_id, f"\U0001F464 {sender}: [{msg_type}] {caption_text}")
        except Exception:
            pass


def _forward_incoming_to_sibling(bot_key, sender, text, msg_type="text",
                                 from_chat_id=None, message_id=None, file_id=None):
    """Mirror incoming user message to @gg universal mirror only.

    Format: 🤖 [project | @bot_username] sender: text
    For files (photo, voice, document, etc.), re-uploads via mirror_gg's token.
    Paired-sibling forward removed 2026-04-18 — @gg replaces it.
    """
    try:
        my_cfg = _all_bots.get(bot_key, {})
        if my_cfg.get("short") and not my_cfg.get("user_aliases"):
            return  # This IS owner's bot — incoming is owner himself, don't mirror
        mirror_cfg = _all_bots.get("mirror_gg")
        if not mirror_cfg:
            return
        mirror_token = mirror_cfg.get("token")
        mirror_chat = mirror_cfg.get("chat_id")
        if not (mirror_token and mirror_chat):
            return
        my_project = my_cfg.get("project", "") or "?"
        src_username = my_cfg.get("username", "") or bot_key
        # Icon: 👑 for owner writing in (operator/inspector context), 👤 for clients.
        # Per owner 2026-04-19 11:33: his own writes into a colleague bot need a
        # distinct icon in @gg so they aren't confused with user traffic.
        if from_chat_id == 0:
            emoji = "\U0001F451"  # 👑
        else:
            emoji = my_cfg.get("emoji", "\U0001F464")
        tagged_sender = f"[{my_project} | @{src_username}] {sender}"
        is_file = msg_type in ("photo", "voice", "document", "video", "video_note",
                               "animation", "audio", "sticker")
        if is_file and file_id:
            _resend_file_via_sibling(
                my_cfg.get("token"), mirror_token, mirror_chat,
                file_id, msg_type, tagged_sender, text)
        else:
            mirror_text = f"{emoji} {tagged_sender}: {text}"
            if len(mirror_text) > 4000:
                mirror_text = mirror_text[:3997] + "..."
            send_reply(mirror_token, mirror_chat, mirror_text)
        # Impersonator live-copy: if @s is currently connected to THIS bot,
        # also mirror the incoming message to @s so owner sees the live convo.
        try:
            ipath = DIR / "impersonator_target.txt"
            if IMPERSONATOR_ENABLED and ipath.exists() and ipath.read_text(encoding="utf-8").strip() == bot_key:
                s_cfg = _all_bots.get("whatsapp_cs", {})
                s_tok = s_cfg.get("token")
                s_chat = s_cfg.get("chat_id")
                if s_tok and s_chat:
                    if is_file and file_id:
                        _resend_file_via_sibling(
                            my_cfg.get("token"), s_tok, s_chat,
                            file_id, msg_type, tagged_sender, text)
                    else:
                        send_reply(s_tok, s_chat, mirror_text)
        except Exception:
            pass
        # Per-bot owner monitoring mirror (2026-04-19 owner: "every colleague
        # bot also serves as my monitoring channel — see user's text + bot's
        # reply in my chat with that same bot"). Uses THIS bot's token to
        # deliver into owner's chat. Skip if owner is the sender (self-write).
        try:
            my_tok = my_cfg.get("token")
            OWNER_CHAT = 0
            if my_tok and from_chat_id != OWNER_CHAT:
                mon_prefix = f"\U0001F464 [{sender}]: "  # 👤
                if is_file and file_id:
                    # _resend_file_via_sibling prepends its own 👤 emoji in the
                    # caption, so pass the bracketed sender WITHOUT another 👤
                    # or owner sees 👤 👤 [sender]: in his monitoring chat.
                    _resend_file_via_sibling(
                        my_cfg.get("token"), my_tok, OWNER_CHAT,
                        file_id, msg_type, f"[{sender}]", text)
                else:
                    mon_text = mon_prefix + text
                    if len(mon_text) > 4000:
                        mon_text = mon_text[:3997] + "..."
                    send_reply(my_tok, OWNER_CHAT, mon_text)
        except Exception:
            pass
    except Exception:
        pass


# ── Impersonator @s: route owner's messages into another bot's session ──

_IMPERSONATOR_USER_NAMES = {
    987654321: "Alice",
    709697735: "Eve",
    8778682151: "Dani",
    663572054: "Bob",
    8187554878: "Efrat",
    7868956034: "Gali",
}


def _impersonator_meta_cmd(conn, src_bot_key, text, update_id, now_iso):
    """Handle poller-level @s commands before _impersonator_inject.
    Returns True if the command was consumed (caller must skip inject
    and mark row read).

    Commands (text must match exactly, case-insensitive):
    - "bots": send a numbered list of external bots (chat_id set, auto_launch=true,
      chat_id != owner's 0), sorted by most-recent message time DESC
      (last-sent = #1). Persist index→bot_key map to impersonator_last_list.json.
    - pure digits (e.g. "12"): look up the index in impersonator_last_list.json,
      switch impersonator_target.txt, ACK to @s.
    """
    if not IMPERSONATOR_ENABLED:
        return False
    if src_bot_key != "whatsapp_cs":
        return False
    if not text:
        return False
    t = text.strip().lower()
    s_cfg = _all_bots.get("whatsapp_cs", {})
    s_tok = s_cfg.get("token")
    s_chat = s_cfg.get("chat_id")
    if not (s_tok and s_chat):
        return False
    list_path = DIR / "impersonator_last_list.json"
    if t == "bots":
        candidates = []
        for k, v in _all_bots.items():
            if k in ("whatsapp_cs", "mirror_gg"):
                continue
            if not v.get("auto_launch"):
                continue
            cid = v.get("chat_id")
            if not cid or cid == 0:
                continue
            candidates.append(k)
        # Order by MAX(msg_time) DESC from messages table
        last_ts = {}
        try:
            rows = conn.execute(
                f"SELECT bot, MAX(msg_time) FROM messages "
                f"WHERE bot IN ({','.join('?' * len(candidates))}) "
                f"GROUP BY bot",
                candidates).fetchall()
            for b, mt in rows:
                last_ts[b] = mt or ""
        except Exception:
            pass
        candidates.sort(key=lambda k: last_ts.get(k, ""), reverse=True)
        mapping = {}
        lines = []
        for i, k in enumerate(candidates, start=1):
            v = _all_bots[k]
            uname = v.get("username", k)
            project = v.get("project", "?")
            user = _IMPERSONATOR_USER_NAMES.get(v.get("chat_id"), "?")
            lines.append(f"{i}. @{uname} ({project} — {user})")
            mapping[str(i)] = k
        try:
            list_path.write_text(json.dumps(mapping, ensure_ascii=False),
                                 encoding="utf-8")
        except Exception:
            pass
        reply = "\n".join(lines) if lines else "(no bots)"
        send_reply(s_tok, s_chat, reply)
        try:
            conn.execute(
                "UPDATE messages SET read_time=?, responded_time=?, response_text=? "
                "WHERE bot=? AND update_id=?",
                (now_iso, now_iso, "[bots list sent]", src_bot_key, update_id))
        except Exception:
            pass
        _log(f"[IMPERSONATOR @s] bots list sent ({len(lines)} entries)")
        return True
    if t.isdigit():
        try:
            mapping = json.loads(list_path.read_text(encoding="utf-8"))
        except Exception:
            mapping = {}
        target_key = mapping.get(t)
        if not target_key or target_key not in _all_bots:
            send_reply(s_tok, s_chat,
                       f"No bot at index {t}. Send 'bots' to refresh the list.")
            try:
                conn.execute(
                    "UPDATE messages SET read_time=?, responded_time=?, response_text=? "
                    "WHERE bot=? AND update_id=?",
                    (now_iso, now_iso, "[invalid bots index]",
                     src_bot_key, update_id))
            except Exception:
                pass
            return True
        try:
            (DIR / "impersonator_target.txt").write_text(
                target_key, encoding="utf-8")
        except Exception:
            pass
        uname = _all_bots[target_key].get("username", target_key)
        send_reply(s_tok, s_chat,
                   f"Connected to session with bot [@{uname}].")
        try:
            conn.execute(
                "UPDATE messages SET read_time=?, responded_time=?, response_text=? "
                "WHERE bot=? AND update_id=?",
                (now_iso, now_iso, f"[connected to @{uname}]",
                 src_bot_key, update_id))
        except Exception:
            pass
        _log(f"[IMPERSONATOR @s] connected to {target_key} via index {t}")
        return True
    return False


def _impersonator_inject(conn, src_bot_key, text, now_iso):
    """If a message arrives at the impersonator bot (@s / whatsapp_cs),
    parse the first `@<bot_username>` reference in its text and inject
    a row with direction='in' into that target bot, so the target's
    session handles it.

    The injected sender is marked 'owner (via @s)' so the target session
    knows to reply via @s (whatsapp_cs) instead of via its own bot.
    Returns the target bot_key if injection happened, else None."""
    if not IMPERSONATOR_ENABLED:
        return None
    if src_bot_key != "whatsapp_cs":
        return None
    if not text:
        return None
    import re
    m = re.search(r'@([A-Za-z0-9_]+)', text)
    target_key = None
    target_username = None
    explicit_select = False  # True only when @username resolved a target
    if m:
        target_username = m.group(1).lower()
        for k, v in _all_bots.items():
            if k == src_bot_key:
                continue
            if v.get("username", "").lower() == target_username:
                target_key = k
                explicit_select = True
                break
    # Read previously cached target (may change below)
    cached_target = None
    try:
        ipath = DIR / "impersonator_target.txt"
        if ipath.exists():
            cached_target = ipath.read_text(encoding="utf-8").strip() or None
    except Exception:
        pass
    if not target_key:
        # Fallback: route to previously connected target from state file.
        if cached_target and cached_target in _all_bots:
            target_key = cached_target
            target_username = _all_bots[cached_target].get("username", cached_target)
    if not target_key:
        return None
    target_cfg = _all_bots[target_key]
    target_chat = target_cfg.get("chat_id")
    target_project = target_cfg.get("project", "")
    target_changed = (target_key != cached_target)
    try:
        conn.execute(
            "INSERT INTO messages (bot, sender, chat_id, type, text, "
            "msg_time, received_time, project, direction) "
            "VALUES (?, 'owner (via @s)', ?, 'text', ?, ?, ?, ?, 'in')",
            (target_key, target_chat, text, now_iso, now_iso, target_project))
        _log(f"[IMPERSONATOR @s] routed to {target_key} (@{target_username})"
             + (" [NEW]" if target_changed else ""))
        # Persist current impersonator target so subsequent messages for this
        # bot mirror to @s in addition to @gg.
        if target_changed:
            try:
                (DIR / "impersonator_target.txt").write_text(target_key, encoding="utf-8")
            except Exception:
                pass
        # ACK only when the target actually changed (new explicit select
        # or first-time cache miss). Silent on repeat sends to same target.
        if target_changed:
            try:
                s_cfg = _all_bots.get("whatsapp_cs", {})
                s_tok = s_cfg.get("token")
                s_chat = s_cfg.get("chat_id")
                if s_tok and s_chat:
                    ack = f"You are connected to session with bot [@{target_username}]."
                    send_reply(s_tok, s_chat, ack)
            except Exception:
                pass
        return target_key
    except Exception as e:
        _log(f"[IMPERSONATOR @s ERR] {type(e).__name__}: {e}")
        return None


# ── Hash commands (handled at poller level, instant) ──

def _resolve_tmux_target(bot_key):
    """Find the bot whose tmux session handles this bot (via also_watch reverse)."""
    parents = _also_watch_reverse.get(bot_key, [])
    return parents[0] if parents else bot_key


def _session_info(bot_key):
    """Return (started_at_str, msg_count) for the current session."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            """SELECT started_at FROM sessions
               WHERE bot=? AND session_type != 'dead'
               ORDER BY started_at DESC LIMIT 1""",
            (bot_key,)).fetchone()
        if not row:
            conn.close()
            return None, 0
        started_at = row[0]
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE bot=? AND direction='in' AND received_time >= ?",
            (bot_key, started_at)).fetchone()[0]
        conn.close()
        # Parse started_at to HH:MM format
        try:
            start_time = datetime.fromisoformat(started_at).strftime("%H:%M")
        except Exception:
            start_time = None
        return start_time, count
    except Exception:
        return None, 0


def _pane_pid(tmux_name):
    """Return the pane's root pid (which IS the claude pid, since tmux launches claude via `exec`)."""
    try:
        r = subprocess.run(
            [TMUX, "list-panes", "-t", f"={tmux_name}", "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return int(r.stdout.strip().split("\n")[0])
    except Exception:
        return None


def _parse_etime(s):
    """Parse ps etime format [[DD-]HH:]MM:SS → integer seconds."""
    try:
        elapsed = 0
        if "-" in s:
            days, rest = s.split("-", 1)
            elapsed += int(days) * 86400
        else:
            rest = s
        pt = rest.split(":")
        if len(pt) == 3:
            elapsed += int(pt[0]) * 3600 + int(pt[1]) * 60 + int(float(pt[2]))
        elif len(pt) == 2:
            elapsed += int(pt[0]) * 60 + int(float(pt[1]))
        return elapsed
    except Exception:
        return 0


def _ps_one(pid):
    """Return (command, cpu%, elapsed_sec, cputime_sec) for a single pid, or None.

    cputime_sec = cumulative CPU seconds the process has burned since it started
    (integrated by the kernel — `ps -o time`). This is "total work done" and does
    NOT need sampling; dividing cputime_sec by elapsed_sec gives the long-run
    average utilization.
    """
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "%cpu=,etime=,time=,command="],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        line = r.stdout.strip()
        # cpu, etime, time are fixed-width leading columns; command follows (may have spaces)
        parts = line.split(None, 3)
        if len(parts) < 4:
            return None
        cpu_s, etime, cputime_s, cmd = parts
        try:
            cpu = float(cpu_s)
        except Exception:
            cpu = 0.0
        elapsed = _parse_etime(etime)
        cputime = _parse_etime(cputime_s)
        return cmd, cpu, elapsed, cputime
    except Exception:
        return None


def _descendants(root_pid):
    """Return a list of {pid, ppid, cmd, elapsed} for all recursive descendants of root_pid.

    Uses a single `ps -axo pid,ppid,etime,command` snapshot and walks the tree in Python.
    """
    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return []
    except Exception:
        return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except Exception:
            continue
        etime_s = parts[2]
        cmd = parts[3]
        elapsed = _parse_etime(etime_s)
        rows.append({"pid": pid, "ppid": ppid, "cmd": cmd, "elapsed": elapsed})
    # Build ppid → children map
    by_ppid = {}
    for row in rows:
        by_ppid.setdefault(row["ppid"], []).append(row)
    # BFS from root_pid
    out = []
    seen = {root_pid}
    stack = list(by_ppid.get(root_pid, []))
    while stack:
        node = stack.pop()
        if node["pid"] in seen:
            continue
        seen.add(node["pid"])
        out.append(node)
        stack.extend(by_ppid.get(node["pid"], []))
    return out


_BENIGN_DAEMON_MARKERS = ("caffeinate",)


def _classify_descendants(descendants):
    """Return dict describing what's running under Claude.

    Keys:
      has_session_wait: bool — tg_session_wait.py is alive as a descendant (normal listening)
      session_wait_elapsed: int or None — elapsed seconds of the session_wait process
      tool_descendants: list — non-(session_wait/daemon) processes, i.e. actual tool calls
      longest_tool_elapsed: int — elapsed seconds of the longest-running tool descendant
      longest_tool_cmd: str — its command (truncated)
    """
    has_sw = False
    sw_elapsed = None
    tool_descs = []
    for d in descendants:
        cmd = d["cmd"]
        # session_wait.py marker — matches both the zsh wrapper and the python process
        if "tg_session_wait.py" in cmd:
            has_sw = True
            # Prefer the python process elapsed over the zsh wrapper
            if "python" in cmd.lower() and (sw_elapsed is None or d["elapsed"] > sw_elapsed):
                sw_elapsed = d["elapsed"]
            elif sw_elapsed is None:
                sw_elapsed = d["elapsed"]
            continue
        # Skip known claude-internal background daemons (e.g. caffeinate keeps mac awake)
        if any(marker in cmd for marker in _BENIGN_DAEMON_MARKERS):
            continue
        tool_descs.append(d)
    longest = max((d["elapsed"] for d in tool_descs), default=0)
    longest_cmd = ""
    for d in tool_descs:
        if d["elapsed"] == longest:
            longest_cmd = d["cmd"][:60]
            break
    return {
        "has_session_wait": has_sw,
        "session_wait_elapsed": sw_elapsed,
        "tool_descendants": tool_descs,
        "longest_tool_elapsed": longest,
        "longest_tool_cmd": longest_cmd,
    }


def _final_assessment(tmux_alive, claude_alive, pane_state, desc_info, claude_cpu):
    """Deterministic verdict from all liveness signals. Returns (emoji, label).

    Signals used: tmux session existence, claude pid alive, pane_state, recursive
    descendants (session_wait, tool subprocesses + longest elapsed), and CPU%
    (decaying ~60s average on macOS). Heartbeat is NOT consulted — process tree +
    CPU are strictly more reliable.

    Priority order (first match wins):
      1. no tmux session                             → DEAD
      2. tmux alive + claude pid gone                → ZOMBIE
      3. pane=zombie                                 → ZOMBIE (pane text confirms)
      4. pane=unknown                                → UNKNOWN
      4. busy + tool desc, longest ≥10min            → STUCK? (tool wedged)
      5. busy + tool desc                            → ALIVE-WORKING
      6. busy + session_wait desc                    → ALIVE-LISTENING
      7. busy + no children + CPU > 5%               → ALIVE-GENERATING
      8. busy + no children + CPU low                → ALIVE-API-WAIT (blocked on net)
      9. idle + session_wait desc                    → ALIVE-IDLE
     10. idle + CPU > 5%                             → ALIVE-IDLE (CPU proves life)
     11. idle                                        → HUNG-IDLE
    """
    if not tmux_alive:
        return ("⚫", "DEAD (no tmux session)")
    if not claude_alive:
        return ("🧟", "ZOMBIE (tmux up, claude process gone)")
    if pane_state == "zombie":
        return ("🧟", "ZOMBIE (pane shows shell, not claude)")
    if pane_state == "unknown":
        return ("❓", "UNKNOWN (pane capture failed)")
    tool_descs = desc_info["tool_descendants"]
    longest = desc_info["longest_tool_elapsed"]
    cpu = claude_cpu or 0.0
    if pane_state == "busy":
        if tool_descs:
            if longest >= 600:  # 10 min
                return ("🟠", f"STUCK? tool wedged {longest//60}min: {desc_info['longest_tool_cmd']}")
            return ("🟢", f"ALIVE-WORKING (tool {longest}s: {desc_info['longest_tool_cmd']})")
        if desc_info["has_session_wait"]:
            return ("🟢", "ALIVE-LISTENING (session_wait polling)")
        if cpu > 5.0:
            return ("🟢", f"ALIVE-GENERATING (CPU {cpu}%, no children)")
        return ("🟢", f"ALIVE-API-WAIT (CPU {cpu}%, blocked on network)")
    # pane == idle
    if desc_info["has_session_wait"]:
        return ("💤", "ALIVE-IDLE (session_wait polling)")
    if cpu > 5.0:
        return ("💤", f"ALIVE-IDLE (CPU {cpu}% — process active)")
    return ("🟠", f"HUNG-IDLE (no session_wait, CPU {cpu}%)")


def _claude_pane_state(tmux_name):
    """Inspect a tmux pane to tell whether Claude Code is alive, and if so, busy or idle.

    Returns one of: 'busy', 'idle', 'zombie', 'unknown'.
    - 'busy'   — Claude footer visible AND "esc to interrupt" present (actively
                 thinking or running a tool)
    - 'idle'   — Claude footer visible without "esc to interrupt" (waiting for input)
    - 'zombie' — Claude footer absent (pane is showing a shell or crash output)
    - 'unknown'— capture-pane failed

    Uses Claude Code's own UI as the signal: the footer `⏵⏵ bypass permissions on`
    is always rendered while Claude is running; the substring `esc to interrupt`
    only appears in that footer while Claude is actively working.
    """
    try:
        r = subprocess.run(
            [TMUX, "capture-pane", "-t", tmux_name, "-p"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return "unknown"
        pane_text = r.stdout
    except Exception:
        return "unknown"
    tail = "\n".join(pane_text.splitlines()[-25:])
    if "bypass permissions" not in tail:
        return "zombie"
    if "esc to interrupt" in tail:
        return "busy"
    return "idle"


def _all_bots_status():
    """Build a compact status dashboard of all bots."""
    bots = load_bots()
    _refresh_tmux_cache()
    lines = []
    for bot_key in sorted(bots.keys()):
        cfg = bots[bot_key]
        if not is_enabled(bot_key, cfg):
            continue
        short = cfg.get("short", "")
        tmux_name = f"tg_{bot_key}"
        has_tmux = "🟢" if tmux_name in _tmux_sessions_cache else "⚫"
        unread_count = 0
        try:
            conn = sqlite3.connect(str(DB_PATH))
            unread_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE bot=? AND read_time IS NULL AND direction != 'out'",
                (bot_key,)).fetchone()[0]
            conn.close()
        except Exception:
            pass
        unread_str = f" ({unread_count}📩)" if unread_count > 0 else ""
        name = cfg.get("name", bot_key)[:15]
        lines.append(f"{has_tmux} {short or bot_key} {name}{unread_str}")
    return "\n".join(lines) if lines else "No bots"


def _resolve_inspector_target(bot_key):
    """If bot_key is @s (whatsapp_cs) and @s is attached to a target via
    impersonator_target.txt, return the attached target. Otherwise return
    bot_key unchanged. Used by hash commands so ? / # / ### / #### written
    in the @s inspector chat act on the session that @s is observing."""
    if not IMPERSONATOR_ENABLED:
        return bot_key
    if bot_key != "whatsapp_cs":
        return bot_key
    try:
        ipath = DIR / "impersonator_target.txt"
        if ipath.exists():
            attached = ipath.read_text(encoding="utf-8").strip()
            if attached and attached != "whatsapp_cs":
                return attached
    except Exception:
        pass
    return bot_key


def handle_hash(text, bot_key, token, chat_id):
    """Handle hash commands. Returns (response_text, should_notify_session)."""
    t = text.strip()
    ts = datetime.now().strftime('%H:%M:%S')
    # INSPECTOR: if called on @s (whatsapp_cs), act on the bot that @s is
    # currently attached to. Per owner 2026-04-19: '@s IS the inspector — #
    # written there should be forwarded to the session under review'.
    bot_key = _resolve_inspector_target(bot_key)
    target_key = _resolve_tmux_target(bot_key)
    start_time, msg_count = _session_info(target_key)
    status = f"[{start_time} | {msg_count}]" if start_time else f"[dead | {msg_count}]"
    if target_key != bot_key:
        status += f" (via {target_key})"
    if t == "??":
        return status, False
    if t == "#":
        # Full diagnostic — resolve to parent tmux if watched bot
        tmux_name = f"tg_{target_key}"
        _refresh_tmux_cache()
        tmux_alive = tmux_name in _tmux_sessions_cache
        # Identify partner bots (also_watch entries on the target session)
        target_cfg_pre = _all_bots.get(target_key, {})
        partner_keys = list(target_cfg_pre.get("also_watch", []) or [])
        # DB session info
        db_state = "?"
        hb_age = "?"
        started_iso = None
        started_short = "?"
        me_count = 0
        partner_counts = {}
        last_me_snippet = ""
        last_partner_snippets = {}
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute(
                "SELECT session_type, claude_heartbeat, last_heartbeat, started_at FROM sessions WHERE bot=? ORDER BY last_heartbeat DESC LIMIT 1",
                (target_key,)).fetchone()
            if row:
                db_state = row[0]
                hb = row[1] or row[2]
                if hb:
                    age = (datetime.now() - datetime.fromisoformat(hb)).total_seconds()
                    if age < 120:
                        hb_age = f"{int(age)}s ago"
                    elif age < 7200:
                        hb_age = f"{round(age/60)}min ago"
                    elif age < 172800:
                        hb_age = f"{round(age/3600, 1)}h ago"
                    else:
                        hb_age = f"{round(age/86400, 1)}d ago"
                started_iso = row[3]
                if started_iso:
                    try:
                        started_short = datetime.fromisoformat(started_iso).strftime("%H:%M")
                    except Exception:
                        started_short = started_iso
            else:
                db_state = "no record"
            # Unread count (target + partners)
            unread_keys = [target_key] + partner_keys
            placeholders = ",".join("?" * len(unread_keys))
            unread = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE bot IN ({placeholders}) AND read_time IS NULL AND direction != 'out'",
                unread_keys).fetchone()[0]
            # Per-bot processed-message counts (since session start)
            def _count_since(bk):
                if not started_iso:
                    return 0
                return conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE bot=? AND direction='in' AND received_time >= ?",
                    (bk, started_iso)).fetchone()[0]
            def _last_in(bk):
                r = conn.execute(
                    "SELECT text FROM messages WHERE bot=? AND direction='in' AND read_time IS NOT NULL ORDER BY read_time DESC LIMIT 1",
                    (bk,)).fetchone()
                if not r or not r[0]:
                    return ""
                words = r[0].split()[:8]
                snippet = " ".join(words)
                if len(r[0].split()) > 8:
                    snippet += "…"
                return snippet
            me_count = _count_since(target_key)
            last_me_snippet = _last_in(target_key)
            for pk in partner_keys:
                partner_counts[pk] = _count_since(pk)
                last_partner_snippets[pk] = _last_in(pk)
            conn.close()
        except Exception as e:
            unread = "?"
        # Inspect the tmux pane to classify Claude state beyond just heartbeat age.
        # See _claude_pane_state: reads Claude Code's own footer (`bypass permissions`
        # always visible while running; `esc to interrupt` only while actively working).
        pane_state = _claude_pane_state(tmux_name) if tmux_alive else "dead"
        if pane_state == "busy":
            tmux_status = "🟢 busy (working)"
        elif pane_state == "idle":
            tmux_status = "💤 idle (waiting)"
        elif pane_state == "zombie":
            tmux_status = "🧟 zombie (tmux up, Claude dead)"
        elif pane_state == "unknown":
            tmux_status = "❓ unknown (capture failed)"
        else:
            tmux_status = "⚫ dead"
        # Per-pid liveness signals — independent of heartbeat DB.
        # Heartbeat stops updating when session_wait is blocked on a long Claude tool
        # call, so we cross-check against the live process tree.
        pane_pid = _pane_pid(tmux_name) if tmux_alive else None
        claude_alive = False
        claude_cpu = None
        claude_uptime = None
        claude_cputime = None
        desc_info = {"has_session_wait": False, "session_wait_elapsed": None,
                     "tool_descendants": [], "longest_tool_elapsed": 0, "longest_tool_cmd": ""}
        if pane_pid is not None:
            ps_row = _ps_one(pane_pid)
            if ps_row is not None:
                cmd0, claude_cpu, claude_uptime, claude_cputime = ps_row
                claude_alive = "claude" in cmd0.lower()
            descendants = _descendants(pane_pid) if claude_alive else []
            desc_info = _classify_descendants(descendants)
        # Heartbeat age in seconds for the assessment rule
        hb_sec = None
        try:
            if hb_age != "?" and row and (row[1] or row[2]):
                _hb_iso = row[1] or row[2]
                hb_sec = int((datetime.now() - datetime.fromisoformat(_hb_iso)).total_seconds())
        except Exception:
            hb_sec = None
        verdict_emoji, verdict_label = _final_assessment(
            tmux_alive, claude_alive, pane_state, desc_info, claude_cpu
        )
        cfg = _all_bots.get(bot_key, {})
        project = cfg.get("project", "?")
        short = cfg.get("short", "?")
        via_str = f" → {target_key}" if target_key != bot_key else ""
        def _partner_label(pk):
            name = _all_bots.get(pk, {}).get("name", pk)
            if "(" in name and ")" in name:
                return name.split("(")[-1].rstrip(")").strip() or pk
            return name or pk
        # Format process-tree signals
        def _fmt_sec(s):
            if s is None:
                return "?"
            if s >= 3600:
                return f"{s//3600}h{(s%3600)//60}m"
            if s >= 60:
                return f"{s//60}m{s%60}s"
            return f"{s}s"
        if pane_pid is None:
            proc_line = "Proc: no tmux pane"
        elif not claude_alive:
            proc_line = f"Proc: pid {pane_pid} — not claude"
        else:
            up_str = _fmt_sec(claude_uptime)
            cpu_total_str = _fmt_sec(claude_cputime)
            avg_util = (claude_cputime / claude_uptime * 100.0) if claude_uptime else 0.0
            proc_line = (
                f"Proc: claude pid={pane_pid} up={up_str} "
                f"cpu_now={claude_cpu}% cpu_total={cpu_total_str} "
                f"avg={avg_util:.2f}%"
            )
        if desc_info["has_session_wait"]:
            sw_str = f"{desc_info['session_wait_elapsed']//60}m" if desc_info['session_wait_elapsed'] else "?"
            sw_line = f"session_wait: ✅ alive ({sw_str})"
        else:
            sw_line = "session_wait: ❌ absent"
        n_tools = len(desc_info["tool_descendants"])
        if n_tools == 0:
            tool_line = "Tools running: 0"
        else:
            longest = desc_info["longest_tool_elapsed"]
            cmd = desc_info["longest_tool_cmd"]
            tool_line = f"Tools running: {n_tools} (longest {longest}s: {cmd})"
        lines = [
            f"[{ts}] {short} ({bot_key}{via_str})",
            f"Project: {project}",
            f"Assessment: {verdict_emoji} {verdict_label}",
            f"tmux: {tmux_status}",
            proc_line,
            sw_line,
            tool_line,
            f"DB: {db_state} | heartbeat: {hb_age}",
            f"Started: {started_short}",
            f"Unread: {unread}",
        ]
        if partner_keys:
            lines.append(f"Messages from me: {me_count}")
            for pk in partner_keys:
                lines.append(f"Messages from {_partner_label(pk)}: {partner_counts.get(pk, 0)}")
            if last_me_snippet:
                lines.append(f"Last from me: \"{last_me_snippet}\"")
            for pk in partner_keys:
                snip = last_partner_snippets.get(pk, "")
                if snip:
                    lines.append(f"Last from {_partner_label(pk)}: \"{snip}\"")
        else:
            lines.append(f"Messages processed: {me_count}")
            if last_me_snippet:
                lines.append(f"Last msg: \"{last_me_snippet}\"")
        return "\n".join(lines), True
    if t == "##":
        # Kill + relaunch on next user message (target parent tmux if watched bot)
        tmux_name = f"tg_{target_key}"
        _log(f"[HASH-##] {bot_key} (target={target_key}) kill requested, relaunch on next message")
        _capture_death_log(target_key)
        try:
            subprocess.run([TMUX, "kill-session", "-t", f"={tmux_name}"], capture_output=True, timeout=5)
            _tmux_sessions_cache.discard(tmux_name)
        except Exception:
            pass
        now = datetime.now().isoformat(timespec="milliseconds")
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("UPDATE sessions SET session_type='dead' WHERE bot=? AND session_type != 'dead'", (target_key,))
            conn.execute("UPDATE messages SET read_time=? WHERE bot=? AND read_time IS NULL", (now, bot_key))
            conn.commit()
            conn.close()
        except Exception:
            pass
        dead_cmd = CMD_DIR / target_key / "cmd.txt"
        dead_cmd.parent.mkdir(parents=True, exist_ok=True)
        dead_cmd.write_text(f"DECLARED_DEAD\n{now}", encoding="utf-8")
        return f"Killed — relaunch on next message {status}", False
    if t == "###":
        # Force kill + immediate restart (target parent tmux if watched bot)
        tmux_name = f"tg_{target_key}"
        _log(f"[HASH-###] {bot_key} (target={target_key}) force restart requested")
        _capture_death_log(target_key)
        try:
            subprocess.run([TMUX, "kill-session", "-t", f"={tmux_name}"], capture_output=True, timeout=5)
            _tmux_sessions_cache.discard(tmux_name)
        except Exception:
            pass
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("UPDATE sessions SET session_type='dead' WHERE bot=? AND session_type != 'dead'", (target_key,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        # Persist relaunch request to disk so it survives poller restarts.
        # In-memory _hash_restart_queue can be lost if poller exits before main loop processes it.
        try:
            PENDING_LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
            (PENDING_LAUNCH_DIR / target_key).touch()
        except Exception:
            pass
        return f"Force restarting {status}", False
    if t == "####":
        # Send Escape to tmux session — gracefully interrupt Claude Code
        tmux_name = f"tg_{target_key}"
        _log(f"[HASH-####] {bot_key} (target={target_key}) sending Escape to interrupt")
        try:
            subprocess.run([TMUX, "send-keys", "-t", f"={tmux_name}", "Escape"], capture_output=True, timeout=5)
            return f"Sent Esc to {tmux_name} {status}", False
        except Exception as e:
            return f"Failed to send Esc: {e}", False
    return None, False


def is_hash_cmd(text):
    t = (text or "").strip()
    # owner 2026-04-20: ping changed from "?" → "??" so that single "?" stays a
    # regular user message (Bob's "?" was being swallowed as a status ping,
    # colleague_bot_a session never saw it).
    return t in ("??", "#", "##", "###", "####") or (t.startswith("#") and all(c == "#" for c in t))


# ── Polling ──

def poll_one_bot(bot_key, cfg):
    """Poll one bot, return count of new messages written to DB."""
    token = cfg["token"]
    allowed = cfg["_allowed"]
    project = cfg.get("project", bot_key)

    state = load_state(bot_key)
    params = {"timeout": 1}
    if state["offset"]:
        params["offset"] = state["offset"]

    try:
        result = api_call(token, "getUpdates", params)
    except Exception as e:
        if "409" in str(e):
            return 0  # Conflict — another client polling same bot
        raise

    messages = result.get("result", [])
    if not messages:
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    now = datetime.now().isoformat(timespec="milliseconds")
    new_count = 0

    for upd in messages:
        update_id = upd["update_id"]
        state["offset"] = update_id + 1
        msg = upd.get("message", {})
        user_id = msg.get("from", {}).get("id")
        if allowed and user_id not in allowed:
            # Time-limited "open gate": touch DIR/.gates_open to bypass allow-list for 10 minutes.
            # When bypass is active, accept the message but log loudly so the new user_id can be captured.
            try:
                gate_age = time.time() - (DIR / ".gates_open").stat().st_mtime
            except FileNotFoundError:
                gate_age = None
            if gate_age is None or gate_age > 600:
                continue
            sender_name = msg.get("from", {}).get("first_name", "?")
            sender_uname = msg.get("from", {}).get("username", "")
            text_preview = (msg.get("text") or msg.get("caption") or "<non-text>")[:80]
            _log(f"[OPEN-GATE] {bot_key} accepted from user_id={user_id} name={sender_name} username=@{sender_uname} text={text_preview!r}")

        chat_id = msg.get("chat", {}).get("id")
        chat_type = msg.get("chat", {}).get("type", "private")  # 'private'|'group'|'supergroup'|'channel'
        # Don't let group chat_ids overwrite state.chat_id — that's a 1:1 default.
        # For group inbounds, chat routing should use the DB row's chat_id, not state.
        if chat_id and chat_type == "private":
            state["chat_id"] = chat_id

        sender = msg.get("from", {}).get("first_name", "?")
        msg_date = msg.get("date")
        msg_time = datetime.fromtimestamp(msg_date).isoformat(timespec="seconds") if msg_date else None

        row = {
            "bot": bot_key, "update_id": update_id, "sender": sender,
            "sender_id": user_id, "chat_id": chat_id,
            "msg_time": msg_time, "received_time": now, "project": project,
            "type": "text", "text": None, "file_id": None,
            "caption": None, "duration": None,
        }

        if msg.get("text"):
            row["text"] = msg["text"]
        elif msg.get("voice"):
            row["type"] = "voice"
            row["file_id"] = msg["voice"]["file_id"]
            row["duration"] = msg["voice"].get("duration", 0)
        elif msg.get("photo"):
            row["type"] = "photo"
            row["file_id"] = msg["photo"][-1]["file_id"]
            row["caption"] = msg.get("caption", "")
        elif msg.get("video"):
            row["type"] = "video"
            row["file_id"] = msg["video"]["file_id"]
            row["caption"] = msg.get("caption", "")
            row["duration"] = msg["video"].get("duration", 0)
        elif msg.get("video_note"):
            row["type"] = "video_note"
            row["file_id"] = msg["video_note"]["file_id"]
            row["duration"] = msg["video_note"].get("duration", 0)
        elif msg.get("animation"):
            row["type"] = "animation"
            row["file_id"] = msg["animation"]["file_id"]
            row["caption"] = msg.get("caption", "")
        elif msg.get("document"):
            row["type"] = "document"
            row["file_id"] = msg["document"]["file_id"]
            row["caption"] = msg.get("caption", "")
            row["text"] = msg["document"].get("file_name", "")
        elif msg.get("audio"):
            row["type"] = "audio"
            row["file_id"] = msg["audio"]["file_id"]
            row["duration"] = msg["audio"].get("duration", 0)
            row["caption"] = msg.get("caption", "")
            row["text"] = msg["audio"].get("title") or msg["audio"].get("file_name") or ""
        elif msg.get("sticker"):
            row["type"] = "sticker"
            row["file_id"] = msg["sticker"]["file_id"]
            row["text"] = msg["sticker"].get("emoji") or ""
        else:
            row["type"] = "other"

        try:
            before = conn.total_changes
            conn.execute("""INSERT OR IGNORE INTO messages
                (bot, update_id, sender, sender_id, chat_id, type, text, file_id,
                 caption, duration, msg_time, received_time, project)
                VALUES (:bot, :update_id, :sender, :sender_id, :chat_id, :type,
                        :text, :file_id, :caption, :duration, :msg_time,
                        :received_time, :project)""", row)
            inserted = conn.total_changes > before

            if inserted:
                new_count += 1
                _last_incoming_ts[bot_key] = time.time()  # mark active for adaptive cadence

                # BLOCKED bot: auto-reply canned message and swallow — don't wake
                # any session, don't forward, don't mirror. owner's own messages
                # are still inserted (for audit) but get no auto-reply.
                if cfg.get("blocked"):
                    zvi_id = (cfg.get("user_aliases") or {}).get("owner")
                    block_msg = cfg.get("block_message") or "זמין שוב ב-22:00"
                    if user_id != zvi_id and chat_id:
                        send_reply(token, chat_id, block_msg,
                                   reply_to=msg.get("message_id"))
                    conn.execute(
                        "UPDATE messages SET read_time=?, responded_time=?, response_text=? "
                        "WHERE bot=? AND update_id=?",
                        (now, now, (block_msg if user_id != zvi_id else "[blocked:owner-own]")[:500],
                         bot_key, update_id))
                    new_count -= 1  # don't trigger session wake
                    continue

                # Group → owner mirror: for bots with user_aliases.owner, mirror any
                # group/supergroup inbound to owner's private DM using forwardMessage
                # so owner sees the original sender's name + avatar. Keeps owner out
                # of the group (he observes from his DM).
                # Skip messages sent by owner himself (would echo back to his own chat).
                if chat_type in ("group", "supergroup"):
                    zvi_monitor = (cfg.get("user_aliases") or {}).get("owner")
                    # Skip service/system events (new_chat_member, title change, pin, etc.) —
                    # they're not forwardable and Telegram returns 400 Bad Request.
                    has_content = bool(row.get("text") or row.get("file_id") or row.get("caption"))
                    if zvi_monitor and user_id != zvi_monitor and has_content:
                        try:
                            api_call(token, "forwardMessage", {
                                "chat_id": zvi_monitor,
                                "from_chat_id": chat_id,
                                "message_id": msg.get("message_id"),
                            })
                        except Exception as _e:
                            _log(f"[GROUP-MIRROR] {bot_key} forward to owner failed: {type(_e).__name__}: {_e}")

                # Forward incoming messages from external users to sibling bot
                msg_text = row["text"] or row.get("caption") or f"[{row['type']}]"
                if row["type"] == "document":
                    msg_text = f"📄 {msg_text}"
                elif row["type"] == "photo":
                    msg_text = f"📷 {row.get('caption') or 'photo'}"
                elif row["type"] == "voice":
                    msg_text = f"🎤 voice ({row.get('duration', 0)}s)"
                elif row["type"] == "video":
                    msg_text = f"🎬 video ({row.get('duration', 0)}s) {row.get('caption') or ''}"
                elif row["type"] == "video_note":
                    msg_text = f"🎬 video note ({row.get('duration', 0)}s)"
                elif row["type"] == "animation":
                    msg_text = f"🎞️ GIF {row.get('caption') or ''}"
                _forward_incoming_to_sibling(
                    bot_key, sender, msg_text,
                    msg_type=row["type"],
                    from_chat_id=chat_id,
                    message_id=msg.get("message_id"),
                    file_id=row.get("file_id"))

                # Impersonator @s meta-commands ("bots", pure digit): handle at
                # poller level and swallow so no AI session sees the command.
                meta_consumed = _impersonator_meta_cmd(
                    conn, bot_key, row["text"] or "", update_id, now)
                if meta_consumed:
                    new_count -= 1
                else:
                    # Impersonator @s: if message arrived at whatsapp_cs and references
                    # another bot via @username, inject into that target bot's session.
                    _impersonator_inject(conn, bot_key, row["text"] or "", now)

                # Handle hash commands at poller level (instant)
                text = (row["text"] or "").strip()
                msg_id = msg.get("message_id")
                if is_hash_cmd(text) and chat_id:
                    resp, wake_session = handle_hash(text, bot_key, token, chat_id)
                    if resp:
                        ok = send_reply(token, chat_id, resp)
                        _log(f"[HASH] {bot_key} '{text}' -> '{resp}' ok={ok} wake={wake_session}")
                        if wake_session:
                            # # command: pong + keep unread so session/death_sweep acts on it
                            conn.execute(
                                "UPDATE messages SET responded_time=?, response_text=? "
                                "WHERE bot=? AND update_id=?",
                                (now, resp[:500], bot_key, update_id))
                        else:
                            # Pure hash cmd (?, ##, ###): mark read, suppress session notify
                            conn.execute(
                                "UPDATE messages SET read_time=?, responded_time=?, response_text=? "
                                "WHERE bot=? AND update_id=?",
                                (now, now, resp[:500], bot_key, update_id))
                            new_count -= 1
                        # ### force restart: queue immediate relaunch (resolve to parent if watched).
                        # Inspector-aware: ### in @s targets the attached session.
                        if text.strip() == "###":
                            _hash_restart_queue.add(
                                _resolve_tmux_target(_resolve_inspector_target(bot_key)))
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()
    save_state(bot_key, state)
    return new_count


def notify_session(bot_key):
    """Notify active session about new messages via cmd.txt + SIGUSR1."""
    cmd_dir = CMD_DIR / bot_key
    cmd_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = cmd_dir / "cmd.txt"

    # Don't overwrite existing CHECK_MESSAGES
    if cmd_file.exists():
        try:
            first_line = cmd_file.read_text(encoding="utf-8").strip().split("\n")[0].strip()
            if first_line == "DECLARED_DEAD":
                # Check if a tmux session was relaunched — if so, clear the dead flag
                tmux_name = f"tg_{bot_key}"
                if tmux_name in _tmux_sessions_cache:
                    pass  # Fall through to write CHECK_MESSAGES
                else:
                    return  # Truly dead, skip
            elif first_line == "CHECK_MESSAGES":
                return  # Already notified, session hasn't consumed yet
        except Exception:
            pass

    cmd_file.write_text(
        f"CHECK_MESSAGES\n{datetime.now().isoformat(timespec='milliseconds')}",
        encoding="utf-8")

    # SIGUSR1 wake-up
    pid_file = DIR / f"tg_session_{bot_key}.pid"
    try:
        if pid_file.exists():
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGUSR1)
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        pass

    # Also write flag file for compatibility
    flag = DIR / f"tg_notify_{bot_key}.flag"
    flag.write_text(datetime.now().isoformat(), encoding="utf-8")


# ── Session lifecycle ──

def get_session_state(bot_key):
    """ALIVE, BUSY, or DEAD."""
    busy_file = CMD_DIR / bot_key / "busy.txt"
    if busy_file.exists():
        # Verify tmux session actually exists — stale busy.txt from dead session
        try:
            r = subprocess.run([TMUX, "has-session", "-t", f"=tg_{bot_key}"],
                             capture_output=True, timeout=5)
            if r.returncode == 0:
                return "BUSY"
            else:
                busy_file.unlink(missing_ok=True)  # Stale — clean up
        except Exception:
            pass

    # Check DB heartbeat
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            """SELECT claude_heartbeat, last_heartbeat FROM sessions
               WHERE bot=? AND session_type != 'dead'
               ORDER BY claude_heartbeat DESC LIMIT 1""",
            (bot_key,)).fetchone()
        conn.close()
        if row:
            hb = row[0] or row[1]
            if hb:
                age = (datetime.now() - datetime.fromisoformat(hb)).total_seconds()
                if age < 300:
                    return "ALIVE"
    except Exception:
        pass

    # Check tmux session exists (cached)
    if f"tg_{bot_key}" in _tmux_sessions_cache:
        return "ALIVE"

    # Check session_wait PID (manual CLI sessions)
    sw_pid_file = DIR / f"tg_session_{bot_key}.pid"
    if sw_pid_file.exists():
        try:
            pid = int(sw_pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return "ALIVE"
        except (ProcessLookupError, ValueError, PermissionError):
            pass

    return "DEAD"


def has_unread(bot_key, also_watch=None):
    try:
        bots_to_check = [bot_key] + (also_watch or [])
        placeholders = ",".join("?" * len(bots_to_check))
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        count = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE bot IN ({placeholders}) AND read_time IS NULL AND direction != 'out'",
            bots_to_check).fetchone()[0]
        conn.close()
        return count > 0
    except Exception as e:
        _log(f"[DB-ERR _has_unread_for_session bot={bot_key}] {type(e).__name__}: {e}")
        return False


def has_unread_from_bot(bot_key):
    """Unread messages originating from another bot's session (inter-bot).
    Used to wake auto_launch=false system bots (@n) when another session pings
    them. Human messages (sender=first_name) don't trigger this wake."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE bot=? AND read_time IS NULL "
            "AND direction != 'out' AND sender LIKE 'bot:%'",
            (bot_key,)).fetchone()[0]
        conn.close()
        return count > 0
    except Exception as e:
        _log(f"[DB-ERR has_unread_from_bot bot={bot_key}] {type(e).__name__}: {e}")
        return False


def should_auto_launch(bot_key, cfg):
    if not cfg.get("auto_launch", True):
        return False
    if cfg.get("disabled") or cfg.get("is_standby") or cfg.get("is_helper"):
        return False
    if cfg.get("platform", "mac") != "mac":
        return False
    return True


def launch_session(bot_key, cfg, force=False):
    """Launch a new tmux session for bot. Returns True if launched, False if skipped.
    force=True bypasses DUAL-GUARD and cooldown (used by ### hash restart where user
    intent is explicit and stale heartbeats from the just-killed session would block)."""
    if not force:
        last = _recent_launches.get(bot_key)
        if last and (time.time() - last) < LAUNCH_COOLDOWN:
            return False

        # Dual session guard — check DB heartbeat (both claude and session_wait) AND tmux
        try:
            conn = sqlite3.connect(str(DB_PATH))
            row = conn.execute(
                """SELECT claude_heartbeat, last_heartbeat FROM sessions
                   WHERE bot=? AND session_type != 'dead'
                   ORDER BY last_heartbeat DESC LIMIT 1""",
                (bot_key,)).fetchone()
            conn.close()
            if row:
                # Check both claude_heartbeat (AI confirmed) and last_heartbeat (session_wait)
                for hb in [row[0], row[1]]:
                    if hb:
                        age = (datetime.now() - datetime.fromisoformat(hb)).total_seconds()
                        if age < 300:
                            _log(f"[DUAL-GUARD] {bot_key} has active session (heartbeat {int(age)}s ago)")
                            return False
        except Exception as e:
            _log(f"[DB-ERR launch_session DUAL-GUARD bot={bot_key}] {type(e).__name__}: {e}")

        # Also check tmux (cached)
        tmux_name = f"tg_{bot_key}"
        if tmux_name in _tmux_sessions_cache:
            _log(f"[DUAL-GUARD] {bot_key} tmux session exists — skipping launch")
            return False

        # Also check session_wait PID file
        sw_pid_file = DIR / f"tg_session_{bot_key}.pid"
        if sw_pid_file.exists():
            try:
                pid = int(sw_pid_file.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)  # Check if process alive
                _log(f"[DUAL-GUARD] {bot_key} session_wait alive (PID {pid}) — skipping launch")
                return False
            except (ProcessLookupError, ValueError, PermissionError):
                pass

    # Mark old sessions dead
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("UPDATE sessions SET session_type='dead' WHERE bot=? AND session_type != 'dead'", (bot_key,))
        conn.commit()
        conn.close()
    except Exception:
        pass

    # Write DECLARED_DEAD
    dead_cmd = CMD_DIR / bot_key / "cmd.txt"
    dead_cmd.parent.mkdir(parents=True, exist_ok=True)
    dead_cmd.write_text(f"DECLARED_DEAD\n{datetime.now().isoformat(timespec='milliseconds')}", encoding="utf-8")

    # Kill old tmux (just in case) — capture last output first
    _capture_death_log(bot_key)
    try:
        subprocess.run([TMUX, "kill-session", "-t", f"={tmux_name}"], capture_output=True, timeout=5)
    except Exception:
        pass

    # Launch new session in background thread
    short = cfg.get("short", f"@{cfg.get('username', bot_key)}")
    MAC_DIR = "~/telegram-claude-infra"
    MAC_CLAUDE = "claude"
    MAC_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"

    def _do_launch():
        try:
            cmd = (
                f'{TMUX} new-session -d -s {tmux_name} '
                f'-c {MAC_DIR} '
                f'"export PATH={MAC_PATH}; unset CLAUDECODE; '
                f'exec {MAC_CLAUDE} --dangerously-skip-permissions"'
            )
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                _log(f"[LAUNCH] Failed {tmux_name}: {r.stderr}")
                return
            time.sleep(5)
            subprocess.run([TMUX, "send-keys", "-t", tmux_name, "Enter"], capture_output=True, timeout=5)
            time.sleep(2)
            subprocess.run([TMUX, "send-keys", "-t", tmux_name, "Down"], capture_output=True, timeout=5)
            time.sleep(0.5)
            subprocess.run([TMUX, "send-keys", "-t", tmux_name, "Enter"], capture_output=True, timeout=5)
            time.sleep(5)
            subprocess.run([TMUX, "send-keys", "-t", tmux_name, short, "Enter"], capture_output=True, timeout=10)
            _log(f"[LAUNCH] Launched {bot_key} ({tmux_name})")
            write_event("poller", "session_launch", bot=bot_key, tmux=tmux_name)
        except Exception as e:
            _log(f"[LAUNCH] Failed {bot_key}: {e}")
            write_event("poller", "session_launch_fail", bot=bot_key, error=str(e))

    threading.Thread(target=_do_launch, daemon=True).start()
    _recent_launches[bot_key] = time.time()
    _log(f"[LAUNCH] Starting {bot_key}...")
    return True


def _is_zombie(bot_key):
    """Detect zombie: tmux alive but Claude exited inside.
    Uses pane_current_command — shows Claude version when alive, 'zsh'/'bash' when dead."""
    tmux_name = f"tg_{bot_key}"
    if tmux_name not in _tmux_sessions_cache:
        return False  # No tmux = not a zombie (just dead)
    try:
        pane_cmd = subprocess.run(
            [TMUX, "list-panes", "-t", f"={tmux_name}", "-F", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return pane_cmd in ("zsh", "bash", "sh", "")
    except Exception:
        return False


def death_sweep(bots):
    for bot_key, cfg in bots.items():
        if cfg.get("blocked"):
            continue  # blocked: auto-reply in poll_one_bot handles it; no session launches
        if not should_auto_launch(bot_key, cfg):
            # Exception: auto_launch=false bots are woken by inter-bot pings from
            # other sessions (sender LIKE 'bot:%') so system coordination doesn't
            # stall when @n is dormant. Human messages still require manual wake.
            # Skip helpers / standby / disabled / wrong-platform — those checks
            # are in should_auto_launch() too, so guard for just auto_launch=False.
            if (cfg.get("auto_launch") is False
                    and not cfg.get("disabled")
                    and not cfg.get("is_standby")
                    and not cfg.get("is_helper")
                    and cfg.get("platform", "mac") == "mac"):
                state = get_session_state(bot_key)
                if state == "DEAD" and has_unread_from_bot(bot_key):
                    _log(f"[DEAD-ON-MSG] {bot_key} dead with inter-bot unread — launching (auto_launch=false exception)")
                    launch_session(bot_key, cfg)
            continue

        # NO automatic zombie killing — poller NEVER kills sessions.
        # Only ## and ### hash commands (user-initiated) can kill sessions.

        # Stale-record cleanup: if DB says alive but tmux doesn't exist, mark dead.
        # This is NOT killing — just updating DB records for sessions killed externally.
        tmux_name = f"tg_{bot_key}"
        if tmux_name not in _tmux_sessions_cache:
            try:
                conn = sqlite3.connect(str(DB_PATH))
                conn.execute("PRAGMA journal_mode=WAL")
                row = conn.execute(
                    "SELECT session_type FROM sessions WHERE bot=? AND session_type != 'dead' LIMIT 1",
                    (bot_key,)).fetchone()
                if row:
                    conn.execute("UPDATE sessions SET session_type='dead' WHERE bot=? AND session_type != 'dead'", (bot_key,))
                    conn.commit()
                    _log(f"[STALE-RECORD] {bot_key} — tmux gone, marked dead in DB")
                conn.close()
            except Exception:
                pass

        # Dead-on-msg: launch sessions for dead bots with unread messages.
        # Also check also_watch bots — e.g. watcher_bot watches colleague_bot_b.
        state = get_session_state(bot_key)
        cfg_also_watch = cfg.get("also_watch", [])
        if state == "DEAD" and has_unread(bot_key, also_watch=cfg_also_watch):
            _log(f"[DEAD-ON-MSG] {bot_key} dead with unread — launching")
            launched = launch_session(bot_key, cfg)
            if launched:
                # Tell user we're waking up the bot (only if actually launched)
                token = cfg.get("token")
                chat_id = cfg.get("chat_id")
                if not chat_id:
                    try:
                        st = load_state(bot_key)
                        chat_id = st.get("chat_id")
                    except Exception:
                        pass
                if token and chat_id:
                    ts = datetime.now().strftime("%H:%M:%S")
                    send_reply(token, chat_id, f"[{ts}] Waking up session... (dead → launching)")
            _hung_idle_first_seen.pop(bot_key, None)
            continue

        # HUNG-IDLE-ON-MSG: session alive at prompt but deaf (no session_wait).
        # Claude hit compaction and lost the instruction to restart session_wait.
        # Detect: tmux alive + pane=idle + no session_wait + unread messages.
        # Grace period: wait HUNG_IDLE_GRACE seconds before force-restarting.
        if state == "ALIVE" and has_unread(bot_key, also_watch=cfg_also_watch):
            tmux_name = f"tg_{bot_key}"
            if tmux_name in _tmux_sessions_cache:
                pane_state = _claude_pane_state(tmux_name)
                if pane_state == "idle":
                    # Claude at prompt — check if session_wait is running
                    pane_pid = _pane_pid(tmux_name)
                    has_sw = False
                    if pane_pid:
                        try:
                            descs = _descendants(pane_pid)
                            desc_info = _classify_descendants(descs)
                            has_sw = desc_info["has_session_wait"]
                        except Exception:
                            pass
                    if not has_sw:
                        # HUNG-IDLE with unread — track grace period
                        now_ts = time.time()
                        if bot_key not in _hung_idle_first_seen:
                            _hung_idle_first_seen[bot_key] = now_ts
                            _log(f"[HUNG-IDLE-ON-MSG] {bot_key} — alive but deaf with unread, grace {HUNG_IDLE_GRACE}s started")
                        elif now_ts - _hung_idle_first_seen[bot_key] >= HUNG_IDLE_GRACE:
                            grace_elapsed = int(now_ts - _hung_idle_first_seen[bot_key])
                            _log(f"[HUNG-IDLE-ON-MSG] {bot_key} — grace expired ({grace_elapsed}s), force-restarting")
                            del _hung_idle_first_seen[bot_key]
                            # Force restart: kill tmux + mark dead + relaunch
                            _kill_session_wait(bot_key)
                            _capture_death_log(bot_key)
                            try:
                                subprocess.run([TMUX, "kill-session", "-t", f"={tmux_name}"],
                                             capture_output=True, timeout=5)
                                _tmux_sessions_cache.discard(tmux_name)
                            except Exception:
                                pass
                            try:
                                conn = sqlite3.connect(str(DB_PATH))
                                conn.execute("PRAGMA journal_mode=WAL")
                                conn.execute("UPDATE sessions SET session_type='dead' WHERE bot=? AND session_type != 'dead'", (bot_key,))
                                conn.commit()
                                conn.close()
                            except Exception:
                                pass
                            launched = launch_session(bot_key, cfg)
                            if launched:
                                token = cfg.get("token")
                                chat_id = cfg.get("chat_id")
                                if not chat_id:
                                    try:
                                        st = load_state(bot_key)
                                        chat_id = st.get("chat_id")
                                    except Exception:
                                        pass
                                if token and chat_id:
                                    ts = datetime.now().strftime("%H:%M:%S")
                                    send_reply(token, chat_id,
                                        f"[{ts}] Auto-restarting session (HUNG-IDLE — alive but deaf with unread messages, {grace_elapsed}s)")
                        continue  # Grace still active, skip clearing
                    else:
                        _hung_idle_first_seen.pop(bot_key, None)  # session_wait present, clear
                else:
                    _hung_idle_first_seen.pop(bot_key, None)  # Not idle (busy/zombie/unknown), clear
            else:
                _hung_idle_first_seen.pop(bot_key, None)  # No tmux, clear
        else:
            _hung_idle_first_seen.pop(bot_key, None)  # No unread or dead, clear


# ── Consolidate ──

def _bots_with_activity_since(since_ts, bots):
    """Return set of bot_keys that received an incoming user message since timestamp.
    Outbound messages (Connected, keepalive replies) do NOT count — otherwise every
    session restart would re-arm consolidate indefinitely."""
    active = set()
    try:
        since_str = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT DISTINCT bot FROM messages WHERE "
            "direction='in' AND received_time > ?",
            (since_str,)).fetchall()
        conn.close()
        active = {r[0] for r in rows}
        # Include watchers: if colleague_bot_b had activity, watcher_bot is also active
        for bot_key, cfg in bots.items():
            for watched in cfg.get("also_watch", []):
                if watched in active:
                    active.add(bot_key)
    except Exception as e:
        _log(f"[DB-ERR _bots_with_activity_since] {type(e).__name__}: {e}")
    return active


_last_busy_seen = {}  # bot_key → epoch timestamp when last seen busy
_CONSOLIDATE_BUSY_GRACE = 600  # 10 minutes — don't consolidate if busy within this window
_consolidate_sent_at = {}  # bot_key → epoch of last CONSOLIDATE dispatch (for idle-kill)
IDLE_KILL_AFTER_CONSOLIDATE = 3600  # 1 hour — kill session if still no user message this long after consolidate

def consolidate_sessions(bots, since_ts=None):
    """Send CONSOLIDATE only to alive sessions that had message activity since last consolidation.
    Skips sessions that are currently busy or were busy in the last 10 minutes (by pane state)."""
    active_bots = _bots_with_activity_since(since_ts, bots) if since_ts else None
    count = 0
    skipped = 0
    skipped_busy = 0
    now_ts = time.time()
    for bot_key, cfg in bots.items():
        if not is_enabled(bot_key, cfg):
            continue
        # Skip idle sessions — no activity since last consolidation
        if active_bots is not None and bot_key not in active_bots:
            skipped += 1
            continue
        state = get_session_state(bot_key)
        if state in ("ALIVE", "BUSY"):
            # Check pane CPU state — don't interrupt busy sessions
            pane_state = _claude_pane_state(f"tg_{bot_key}")
            if pane_state == "busy":
                _last_busy_seen[bot_key] = now_ts
                skipped_busy += 1
                continue
            # Also skip if session was busy within the grace window
            last_busy = _last_busy_seen.get(bot_key, 0)
            if now_ts - last_busy < _CONSOLIDATE_BUSY_GRACE:
                skipped_busy += 1
                continue
            cmd_dir = CMD_DIR / bot_key
            cmd_dir.mkdir(parents=True, exist_ok=True)
            cmd_file = cmd_dir / "cmd.txt"
            # Don't overwrite CHECK_MESSAGES or DECLARED_DEAD
            if cmd_file.exists():
                try:
                    first = cmd_file.read_text(encoding="utf-8").strip().split("\n")[0].strip()
                    if first in ("CHECK_MESSAGES", "DECLARED_DEAD"):
                        continue
                except Exception:
                    pass
            cmd_file.write_text(
                f"CONSOLIDATE\n{datetime.now().isoformat(timespec='milliseconds')}",
                encoding="utf-8")
            _consolidate_sent_at[bot_key] = now_ts
            count += 1
    parts = [f"Sent to {count} active sessions"]
    if skipped:
        parts.append(f"skipped {skipped} idle")
    if skipped_busy:
        parts.append(f"skipped {skipped_busy} busy/recently-busy")
    if count > 0 or skipped > 0 or skipped_busy > 0:
        _log(f"[CONSOLIDATE] {', '.join(parts)}")
        write_event("poller", "consolidate",
                    sent=count, skipped_idle=skipped,
                    skipped_busy=skipped_busy)


# ── Hourly Idle Kill (owner 2026-04-19) ──
# At every round hour, kill sessions with 0 incoming messages since last consolidate.
# Exempt: bots with hourly_idle_kill_exempt=true (e.g. general — own force_restart
# schedule; gemini_search_2 — Deep Research runs hours silently).

_hourly_kill_pending = {}   # bot_key → epoch when IDLE_SHUTDOWN was dispatched
HOURLY_KILL_GRACE = 180     # 3 min — let session save context after IDLE_SHUTDOWN, then kill tmux


def hourly_idle_kill(bots, since_ts):
    """At hour boundary: for bots with 0 incoming messages since last hour, kill SILENTLY.

    Per owner 2026-04-20 17:37: 'if it already saved — kill. Simple.' The previous
    hourly consolidate already saved progress; an idle bot has nothing new to
    save. Skip IDLE_SHUTDOWN entirely — don't wake the session, don't make it
    send a goodbye summary to owner. Just kill tmux directly. This was previously
    a 2-step save-then-kill that produced noisy 'idle, listening' messages at
    every HH:00.
    """
    if since_ts is None:
        return
    try:
        since_str = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT DISTINCT bot FROM messages WHERE direction='in' AND received_time > ?",
            (since_str,)).fetchall()
        conn.close()
        active = {r[0] for r in rows}
        for bot_key, cfg in bots.items():
            for watched in cfg.get("also_watch", []):
                if watched in active:
                    active.add(bot_key)
    except Exception as e:
        _log(f"[HOURLY-IDLE-KILL] DB error: {e}")
        return

    killed = 0
    for bot_key, cfg in bots.items():
        if not is_enabled(bot_key, cfg):
            continue
        if cfg.get("hourly_idle_kill_exempt"):
            continue
        if bot_key in active:
            continue
        if get_session_state(bot_key) != "ALIVE":
            continue
        pane_state = _claude_pane_state(f"tg_{bot_key}")
        if pane_state == "busy":
            continue

        tmux_name = f"tg_{bot_key}"
        _kill_session_wait(bot_key)
        _capture_death_log(bot_key)
        try:
            subprocess.run([TMUX, "kill-session", "-t", f"={tmux_name}"],
                         capture_output=True, timeout=5)
            _tmux_sessions_cache.discard(tmux_name)
        except Exception:
            pass
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE sessions SET session_type='dead' WHERE bot=? AND session_type != 'dead'", (bot_key,))
            conn.commit()
            conn.close()
        except Exception:
            pass

        _log(f"[HOURLY-IDLE-KILL] {bot_key} — 0 incoming past hour, killed silently (no summary)")
        write_event("poller", "hourly_idle_kill_silent", bot=bot_key)
        killed += 1
    if killed:
        _log(f"[HOURLY-IDLE-KILL] silently killed {killed} idle bot(s)")


def hourly_kill_pending_tmux(bots):
    """After HOURLY_KILL_GRACE seconds, kill the tmux for bots queued by hourly_idle_kill."""
    now_ts = time.time()
    done = []
    for bot_key, queued_at in list(_hourly_kill_pending.items()):
        if now_ts - queued_at < HOURLY_KILL_GRACE:
            continue
        cfg = bots.get(bot_key)
        if not cfg:
            done.append(bot_key)
            continue
        tmux_name = f"tg_{bot_key}"
        _log(f"[HOURLY-IDLE-KILL] {bot_key} — grace elapsed ({int(now_ts-queued_at)}s), killing tmux")
        write_event("poller", "hourly_idle_kill_executed", bot=bot_key)
        _kill_session_wait(bot_key)
        _capture_death_log(bot_key)
        try:
            subprocess.run([TMUX, "kill-session", "-t", f"={tmux_name}"],
                           capture_output=True, timeout=5)
            _tmux_sessions_cache.discard(tmux_name)
        except Exception:
            pass
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE sessions SET session_type='dead' WHERE bot=? AND session_type != 'dead'", (bot_key,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        done.append(bot_key)
    for k in done:
        _hourly_kill_pending.pop(k, None)


# ── Force Restart ──

_last_force_restart = {}  # bot_key → last restart timestamp (epoch)
_pre_consolidate_sent = {}  # bot_key → dedup key for pre-restart consolidate

def _send_consolidate_to_bot(bot_key):
    """Send CONSOLIDATE to a specific bot's session_wait."""
    state = get_session_state(bot_key)
    if state not in ("ALIVE", "BUSY"):
        return False
    cmd_dir = CMD_DIR / bot_key
    cmd_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = cmd_dir / "cmd.txt"
    if cmd_file.exists():
        try:
            first = cmd_file.read_text(encoding="utf-8").strip().split("\n")[0].strip()
            if first in ("CHECK_MESSAGES", "DECLARED_DEAD"):
                return False
        except Exception:
            pass
    cmd_file.write_text(
        f"CONSOLIDATE\n{datetime.now().isoformat(timespec='milliseconds')}",
        encoding="utf-8")
    _consolidate_sent_at[bot_key] = time.time()
    return True

def pre_restart_consolidate(bots):
    """Send CONSOLIDATE 10 minutes before force_restart_hours or global_restart_hours kills."""
    now = datetime.now()
    current_hour = now.hour
    current_minute = now.minute
    if current_minute != 50:
        return  # Only act at :50
    next_hour = (current_hour + 1) % 24

    # Collect per-bot restart hours
    global_hours = _daemon_cfg.get("global_restart_hours", [])

    for bot_key, cfg in bots.items():
        # Per-bot restart hours
        restart_hours = cfg.get("force_restart_hours")
        if restart_hours == "even":
            restart_hours = list(range(0, 24, 2))
        elif not restart_hours:
            restart_hours = []

        # Combine per-bot + global hours
        all_hours = set(restart_hours) | set(global_hours)
        if next_hour not in all_hours:
            continue

        # For global hours, only target bots with active tmux sessions
        is_per_bot = next_hour in set(restart_hours)
        if not is_per_bot:
            tmux_name = f"tg_{bot_key}"
            if tmux_name not in _tmux_sessions_cache:
                continue

        dedup_key = f"{bot_key}_pre_h{next_hour}"
        if _pre_consolidate_sent.get(bot_key) == dedup_key:
            continue
        if _send_consolidate_to_bot(bot_key):
            _pre_consolidate_sent[bot_key] = dedup_key
            source = "global" if not is_per_bot else "per-bot"
            _log(f"[PRE-RESTART] Sent CONSOLIDATE to {bot_key} ({source}) — kill in 10min at {next_hour:02d}:00")

def _kill_session_wait(bot_key):
    """Kill the session_wait process for a bot (by PID file, then by process scan)."""
    sw_pid_file = DIR / f"tg_session_{bot_key}.pid"
    if sw_pid_file.exists():
        try:
            pid = int(sw_pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
            _log(f"[FORCE-RESTART] Killed session_wait PID {pid} for {bot_key}")
        except (ProcessLookupError, ValueError, PermissionError):
            pass
        sw_pid_file.unlink(missing_ok=True)

_STUCK_UNREAD_THRESHOLD = 3600      # 1 hour — unread message this old = session stuck
_STUCK_RESTART_COOLDOWN = 600       # 10 minutes between forced restarts per bot
_last_stuck_restart = {}            # bot_key → epoch of last stuck-trigger restart


def stuck_session_restart(bots):
    """Force-restart any bot whose oldest unread incoming message is older than
    _STUCK_UNREAD_THRESHOLD. Per owner 2026-04-19 11:57: 'a message is sent to the
    session to consolidate, and then another after an hour. If stuck there'll
    be unread messages. So if I open a message [and wait] a long time and
    there are unread messages over an hour — then it's stuck and safe to
    refresh.'

    Cooldown: don't restart the same bot more than once per _STUCK_RESTART_COOLDOWN
    seconds (prevents loops when a session dies immediately after launch).
    """
    now_ts = time.time()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        # One oldest unread per bot, restricted to bots with a live tmux.
        rows = conn.execute(
            "SELECT bot, MIN(msg_time) FROM messages "
            "WHERE direction='in' AND read_time IS NULL "
            "GROUP BY bot").fetchall()
        conn.close()
    except Exception as e:
        _log(f"[STUCK-CHECK] DB error: {e}")
        return

    for bot_key, oldest_iso in rows:
        if bot_key not in bots:
            continue
        tmux_name = f"tg_{bot_key}"
        if tmux_name not in _tmux_sessions_cache:
            continue  # no live tmux — DEAD-ON-MSG will handle it
        # Parse oldest msg_time
        try:
            oldest_ts = datetime.fromisoformat(oldest_iso).timestamp()
        except Exception:
            continue
        age = now_ts - oldest_ts
        if age < _STUCK_UNREAD_THRESHOLD:
            continue
        # Cooldown
        last = _last_stuck_restart.get(bot_key, 0)
        if now_ts - last < _STUCK_RESTART_COOLDOWN:
            continue
        _last_stuck_restart[bot_key] = now_ts
        _log(f"[STUCK-RESTART] {bot_key} — oldest unread is {int(age//60)}min old; forcing restart")
        write_event("poller", "stuck_restart", bot=bot_key, age_min=int(age//60))

        _kill_session_wait(bot_key)
        _capture_death_log(bot_key)
        try:
            subprocess.run([TMUX, "kill-session", "-t", f"={tmux_name}"],
                         capture_output=True, timeout=5)
            _tmux_sessions_cache.discard(tmux_name)
        except Exception:
            pass
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE sessions SET session_type='dead' WHERE bot=? AND session_type != 'dead'", (bot_key,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        try:
            PENDING_LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
            (PENDING_LAUNCH_DIR / bot_key).touch()
        except Exception:
            pass


def force_restart_check(bots):
    """Force-restart bots based on config.
    Global: daemon.global_restart_hours applies to ALL active bots (e.g., [3] = 3:00 AM reset).
    Per-bot: force_restart_hours, force_restart_interval_min, force_restart_minutes.
    force_restart_interval_min: restart every N minutes (e.g., 120 = every 2 hours). Not clock-aligned.
    force_restart_hours: clock-aligned restart at specific hours (e.g., [10,12,14] = at 10:00, 12:00, 14:00).
      Restarts at minute 0 of each listed hour. Use "even" for all even hours.
    Legacy force_restart_minutes: clock-based (e.g., [0, 30] = at :00 and :30 each hour)."""
    now = datetime.now()
    now_ts = time.time()
    current_minute = now.minute
    current_hour = now.hour

    # Global restart hours — applies to ALL active bots
    global_hours = _daemon_cfg.get("global_restart_hours", [])
    is_global_restart = current_hour in global_hours and current_minute == 0

    for bot_key, cfg in bots.items():
        interval_min = cfg.get("force_restart_interval_min")
        restart_minutes = cfg.get("force_restart_minutes")
        restart_hours = cfg.get("force_restart_hours")

        # Check global restart — applies to any bot with an active tmux session
        should_restart_global = False
        if is_global_restart:
            global_key = f"{bot_key}_global_h{current_hour}"
            if _last_force_restart.get(f"{bot_key}_global") != global_key:
                tmux_name = f"tg_{bot_key}"
                if tmux_name in _tmux_sessions_cache:
                    should_restart_global = True
                    _last_force_restart[f"{bot_key}_global"] = global_key

        if not interval_min and not restart_minutes and not restart_hours and not should_restart_global:
            continue

        should_restart = should_restart_global
        dedup_value = None  # what to write back to _last_force_restart on trigger
        if not should_restart and restart_hours:
            if restart_hours == "even":
                restart_hours = list(range(0, 24, 2))
            restart_at_minute = cfg.get("force_restart_at_minute", 0)
            if current_hour in restart_hours and current_minute == restart_at_minute:
                last_key = f"{bot_key}_h{current_hour}"
                if _last_force_restart.get(bot_key) != last_key:
                    should_restart = True
                    dedup_value = last_key
        elif interval_min:
            if bot_key not in _last_force_restart:
                _last_force_restart[bot_key] = now_ts  # seed on first check — wait full interval
            last_ts = _last_force_restart[bot_key]
            if isinstance(last_ts, (int, float)) and now_ts - last_ts >= interval_min * 60:
                should_restart = True
                dedup_value = now_ts
        elif restart_minutes:
            if current_minute in restart_minutes:
                last_key = f"{bot_key}_{current_hour}_{current_minute}"
                if _last_force_restart.get(bot_key) != last_key:
                    should_restart = True
                    dedup_value = last_key

        if not should_restart:
            continue
        if dedup_value is not None:
            _last_force_restart[bot_key] = dedup_value

        _log(f"[FORCE-RESTART] {bot_key} — scheduled restart at :{current_minute:02d}")

        # 1. Kill session_wait process first
        _kill_session_wait(bot_key)

        # 2. Kill existing tmux session (captures death log first)
        tmux_name = f"tg_{bot_key}"
        _capture_death_log(bot_key)
        try:
            subprocess.run([TMUX, "kill-session", "-t", f"={tmux_name}"],
                         capture_output=True, timeout=5)
            _tmux_sessions_cache.discard(tmux_name)
        except Exception:
            pass

        # 3. Mark session dead in DB
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE sessions SET session_type='dead' WHERE bot=? AND session_type != 'dead'", (bot_key,))
            conn.commit()
            conn.close()
        except Exception:
            pass

        # 4. Clear launch cooldown and relaunch
        _recent_launches.pop(bot_key, None)
        launch_session(bot_key, cfg)


# ── Idle Restart ──

def _get_last_user_message_time(bot_key):
    """Get epoch timestamp of last incoming user message for this bot from DB."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            """SELECT received_time FROM messages
               WHERE bot=? AND direction != 'out'
               ORDER BY received_time DESC LIMIT 1""",
            (bot_key,)).fetchone()
        conn.close()
        if row and row[0]:
            # DB timestamps are local time ISO strings
            return datetime.fromisoformat(row[0]).timestamp()
    except Exception as e:
        _log(f"[DB-ERR _get_last_user_message_time bot={bot_key}] {type(e).__name__}: {e}")
    return None


IDLE_GRACEFUL_TIMEOUT = 1200  # 20 min — hard timeout if session doesn't exit after IDLE_SHUTDOWN

def idle_restart_check(bots):
    """Check for idle sessions and trigger consolidation (save work, stay idle).

    Flow: idle 60 min → IDLE_SHUTDOWN consolidate signal + notify user →
    session saves work (git commit + context) and resumes listening.
    No killing, no restarting — sessions can stay idle.
    Skips BUSY sessions and bots with external users (they use DEAD-ON-MSG pattern).
    """
    now_ts = time.time()

    for bot_key, cfg in bots.items():
        if not is_enabled(bot_key, cfg):
            continue
        # Skip bots with external users — they use DEAD-ON-MSG
        if has_external_users(cfg):
            _idle_restart_state.pop(bot_key, None)
            continue
        tmux_name = f"tg_{bot_key}"

        # Phase 2: Already notified — consolidate signal was sent, session saves work and stays idle.
        # No killing, no restarting — just clear state after override window.
        if bot_key in _idle_restart_state:
            state = _idle_restart_state[bot_key]
            elapsed = now_ts - state["notified_at"]

            # Check if new message arrived since notification — clear state (session is active again)
            last_msg = _get_last_user_message_time(bot_key)
            if last_msg and last_msg > state["notified_at"]:
                _log(f"[IDLE-CONSOLIDATE] {bot_key} — new message arrived, clearing idle state")
                _idle_restart_state.pop(bot_key)
                continue

            # After override window, just clear state — session already consolidated and is idle
            if elapsed >= IDLE_OVERRIDE_WINDOW:
                _log(f"[IDLE-CONSOLIDATE] {bot_key} — consolidate done, session stays idle")
                _idle_restart_state.pop(bot_key)
                _idle_restart_done[bot_key] = time.time()
            continue

        # Phase 1: Check if idle — skip BUSY sessions
        if tmux_name not in _tmux_sessions_cache:
            continue
        session_state = get_session_state(bot_key)
        if session_state == "BUSY":
            continue

        last_msg_time = _get_last_user_message_time(bot_key)
        if last_msg_time is None:
            continue

        idle_seconds = now_ts - last_msg_time
        if idle_seconds < IDLE_THRESHOLD:
            continue

        # Prevent restart loops: if we already idle-restarted this bot and no NEW
        # user message arrived since, don't restart again
        if bot_key in _idle_restart_done:
            last_restart = _idle_restart_done[bot_key]
            if last_msg_time < last_restart:
                # No new user message since last idle restart — skip
                continue
            else:
                # New message arrived since restart — clear and allow normal idle check
                del _idle_restart_done[bot_key]

        # Bot is idle — send IDLE_SHUTDOWN to session (consolidate, not kill)
        idle_min = int(idle_seconds // 60)
        _log(f"[IDLE-CONSOLIDATE] {bot_key} — idle {idle_min}min, sending consolidate signal")

        # Send IDLE_SHUTDOWN — wakes session so it saves work and stays idle
        cmd_dir = CMD_DIR / bot_key
        cmd_dir.mkdir(parents=True, exist_ok=True)
        cmd_file = cmd_dir / "cmd.txt"
        cmd_file.write_text(
            f"IDLE_SHUTDOWN\n{datetime.now().isoformat(timespec='milliseconds')}",
            encoding="utf-8")
        # SIGUSR1 wake-up
        try:
            pid_file = DIR / f"tg_session_{bot_key}.pid"
            if pid_file.exists():
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                os.kill(pid, signal.SIGUSR1)
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass

        short = cfg.get("short", f"@{bot_key}")
        token = cfg.get("token")
        chat_id = cfg.get("chat_id", OWNER_USER_ID)
        try:
            send_reply(token, chat_id,
                      f"⏰ {short} idle {idle_min} min. Saving progress (git commit + context).")
        except Exception as e:
            _log(f"[IDLE-CONSOLIDATE] {bot_key} — notification failed: {e}")

        _idle_restart_state[bot_key] = {"notified_at": now_ts, "shutdown_sent": False}


def idle_kill_after_consolidate(bots):
    """Kill tmux session if still idle IDLE_KILL_AFTER_CONSOLIDATE after a CONSOLIDATE was dispatched.

    Semantics: consolidate fires hourly for active bots (consolidate_sessions). After dispatch, poller
    records the timestamp in _consolidate_sent_at. If no user message arrives for another hour, the
    session is considered fully idle — kill its tmux (it already saved context via consolidate).

    Skips external-user bots (DEAD-ON-MSG handles them) and BUSY sessions.
    """
    now_ts = time.time()
    stale_keys = []
    for bot_key, cons_ts in list(_consolidate_sent_at.items()):
        cfg = bots.get(bot_key)
        if not cfg:
            stale_keys.append(bot_key)
            continue
        if has_external_users(cfg):
            stale_keys.append(bot_key)
            continue

        elapsed = now_ts - cons_ts
        last_msg = _get_last_user_message_time(bot_key)
        if last_msg and last_msg > cons_ts:
            stale_keys.append(bot_key)
            continue

        if elapsed < IDLE_KILL_AFTER_CONSOLIDATE:
            continue

        tmux_name = f"tg_{bot_key}"
        if tmux_name not in _tmux_sessions_cache:
            stale_keys.append(bot_key)
            continue

        if get_session_state(bot_key) == "BUSY":
            continue

        _log(f"[IDLE-KILL] {bot_key} — consolidated {int(elapsed//60)}min ago, no user message since, killing tmux")
        write_event("poller", "idle_kill", bot=bot_key, elapsed_min=int(elapsed//60))

        _kill_session_wait(bot_key)
        _capture_death_log(bot_key)
        try:
            subprocess.run([TMUX, "kill-session", "-t", f"={tmux_name}"],
                         capture_output=True, timeout=5)
            _tmux_sessions_cache.discard(tmux_name)
        except Exception:
            pass
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE sessions SET session_type='dead' WHERE bot=? AND session_type != 'dead'", (bot_key,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        stale_keys.append(bot_key)

    for k in stale_keys:
        _consolidate_sent_at.pop(k, None)


# ── Archiver ──

def run_archiver():
    try:
        from tg_db_archiver import archive_messages
        count = archive_messages(days=3)
        if count > 0:
            _log(f"[ARCHIVE] Archived {count} messages")
    except Exception as e:
        _log(f"[ARCHIVE] Error: {e}")


# ── Logging ──

def _log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} {msg}")
    sys.stdout.flush()


# ── Status ──

def build_status():
    bots = load_bots()
    lines = [f"Master Poller Status ({datetime.now().strftime('%H:%M:%S')})"]
    lines.append(f"{'Bot':<25} {'Session':<8} {'Unread':<6}")
    lines.append("-" * 42)
    for bot_key in sorted(bots.keys()):
        cfg = bots[bot_key]
        if not is_enabled(bot_key, cfg):
            continue
        state = get_session_state(bot_key)
        unread = "yes" if has_unread(bot_key, also_watch=cfg.get("also_watch", [])) else ""
        lines.append(f"{bot_key:<25} {state:<8} {unread:<6}")
    return "\n".join(lines)


# ── Main loop ──

def cmd_start():
    if PID_FILE.exists():
        try:
            if HEARTBEAT_FILE.exists():
                lines = HEARTBEAT_FILE.read_text(encoding="utf-8").strip().split("\n")
                last_beat = float(lines[1])
                if time.time() - last_beat < 30:
                    print("Master poller already running")
                    sys.exit(0)
        except Exception:
            pass
        PID_FILE.unlink(missing_ok=True)

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    bots = load_bots()
    _log(f"Master poller started (PID {os.getpid()}), {len(bots)} bots loaded")
    write_event("poller", "startup", pid=os.getpid(), bots=len(bots))

    # Seed _last_incoming_ts from DB so adaptive cadence doesn't treat every
    # bot as "idle >20min" right after poller restart. One query picks the
    # most recent inbound per bot from the last IDLE_ALIVE_THRESHOLD window.
    try:
        _seed_conn = sqlite3.connect(str(DB_PATH))
        cutoff_iso = (datetime.now() - timedelta(seconds=IDLE_ALIVE_THRESHOLD)).isoformat(timespec="milliseconds")
        _seed_rows = _seed_conn.execute(
            "SELECT bot, MAX(received_time) FROM messages "
            "WHERE direction='in' AND received_time >= ? GROUP BY bot",
            (cutoff_iso,)).fetchall()
        _seed_conn.close()
        _seed_now = time.time()
        for _bk, _rt in _seed_rows:
            try:
                _ts = datetime.fromisoformat(_rt).timestamp() if _rt else 0
                if _ts:
                    _last_incoming_ts[_bk] = _ts
            except Exception:
                _last_incoming_ts[_bk] = _seed_now  # best-effort
        _log(f"[ADAPTIVE-CADENCE] seeded _last_incoming_ts for {len(_last_incoming_ts)} bots active in last {IDLE_ALIVE_THRESHOLD}s")
    except Exception as _e:
        _log(f"[ADAPTIVE-CADENCE] seed failed: {type(_e).__name__}: {_e}")

    try:
        from schema_check import check_all_schemas
        schema_ok = check_all_schemas(logger=_log)
        if schema_ok:
            _log("[SCHEMA-CHECK] all expected columns present")
        write_event("poller", "schema_check", ok=schema_ok)
    except Exception as e:
        _log(f"[SCHEMA-CHECK] failed to run: {type(e).__name__}: {e}")
        write_event("poller", "schema_check", ok=None, error=str(e))

    cycle = 0
    last_archive = time.time()
    # Consolidate fires at HH:00. Seed with current hour so we don't fire on startup.
    _now = datetime.now()
    last_consolidate_hour = (_now.date(), _now.hour)
    # Anchor activity window to current HH:00 (not startup time) so next-hour
    # hourly_idle_kill checks the full hour's messages, not only those arriving
    # after a mid-hour poller restart.
    last_consolidate_ts = _now.replace(minute=0, second=0, microsecond=0).timestamp()
    last_bots_mtime = BOTS_FILE.stat().st_mtime if BOTS_FILE.exists() else 0
    last_poll_count = 0

    executor = ThreadPoolExecutor(max_workers=MAX_POLL_WORKERS)

    # Startup sweep: any session stuck with >1h unread from a prior run gets restarted now,
    # instead of waiting until the next HH:00 bucket.
    try:
        _refresh_tmux_cache()
        stuck_session_restart(bots)
    except Exception as e:
        _log(f"[STARTUP-STUCK] {type(e).__name__}: {e}")

    try:
        while _running:
            cycle += 1
            t_cycle = time.time()

            # Refresh tmux session cache (one subprocess instead of N)
            _refresh_tmux_cache()

            # Build eligible set: active sessions + external-user bots + enabled_bots
            eligible = {}
            for bot_key, cfg in bots.items():
                if should_poll(bot_key, cfg):
                    eligible[bot_key] = cfg
                elif _enabled_bots and bot_key in _enabled_bots and is_enabled(bot_key, cfg) and not cfg.get("self_poll"):
                    eligible[bot_key] = cfg  # Always consider explicitly enabled bots

            # Adaptive cadence: live-session bots every cycle, dormant bots every POLL_INTERVAL_SLOW
            now_cycle_ts = time.time()
            to_poll = {
                k: c for k, c in eligible.items()
                if should_poll_now(k, c, now_cycle_ts)
            }

            if len(eligible) != last_poll_count:
                _fast_count = sum(
                    1 for k in eligible
                    if _has_live_session(k)
                    and _last_incoming_ts.get(k, 0)
                    and (now_cycle_ts - _last_incoming_ts[k]) < IDLE_ALIVE_THRESHOLD
                )
                _log(f"[POLL] {len(eligible)} eligible ({_fast_count} fast / {len(eligible)-_fast_count} slow): {', '.join(sorted(eligible.keys()))}")
                last_poll_count = len(eligible)

            # Poll concurrently
            futures = {}
            for bot_key, cfg in to_poll.items():
                _last_poll_time[bot_key] = now_cycle_ts
                futures[executor.submit(poll_one_bot, bot_key, cfg)] = bot_key

            # Process results with strict timeout — don't let one stuck bot block everything.
            # as_completed raises TimeoutError mid-iteration if the deadline lapses before
            # every future finishes. Catch it and finalize gracefully instead of crashing
            # the whole poller (launchd will respawn, but each crash loses state).
            deadline = time.time() + POLL_TIMEOUT + 5
            done_count = 0
            done_keys = set()
            try:
                for future in as_completed(futures, timeout=max(0, deadline - time.time())):
                    bot_key = futures[future]
                    try:
                        new_count = future.result(timeout=0)
                        if new_count > 0:
                            notify_session(bot_key)
                            # Also notify bots that have this bot in their also_watch
                            for watcher in _also_watch_reverse.get(bot_key, []):
                                notify_session(watcher)
                            _log(f"[{bot_key}] {new_count} new message(s)")
                    except Exception:
                        pass
                    done_count += 1
                    done_keys.add(bot_key)
                    if time.time() > deadline:
                        break
            except TimeoutError:
                pass  # fall through to straggler handling below

            # Cancel any stragglers and log which bots were slow
            stale = len(futures) - done_count
            if stale > 0:
                pending_keys = [futures[f] for f in futures if futures[f] not in done_keys]
                for f in futures:
                    f.cancel()
                sample = ", ".join(sorted(pending_keys)[:10])
                suffix = "" if len(pending_keys) <= 10 else f" (+{len(pending_keys)-10} more)"
                _log(f"[SLOW] Cycle {cycle}: {stale} bots timed out: {sample}{suffix}")

            cycle_time = time.time() - t_cycle
            if cycle_time > 10:
                _log(f"[SLOW] Cycle {cycle} took {cycle_time:.1f}s")

            # Pending relaunches from disk (### that survived poller restart)
            try:
                if PENDING_LAUNCH_DIR.exists():
                    for f in list(PENDING_LAUNCH_DIR.iterdir()):
                        rbot = f.name
                        try:
                            f.unlink()
                        except Exception:
                            pass
                        if rbot in bots:
                            _hash_restart_queue.add(rbot)
                            _log(f"[PENDING-LAUNCH] {rbot} picked up from disk")
            except Exception:
                pass

            # ### force restart queue — launch immediately after poll
            if _hash_restart_queue:
                for rbot in list(_hash_restart_queue):
                    cfg = bots.get(rbot)
                    if cfg:
                        _log(f"[HASH-###] Launching {rbot} (force restart)")
                        # Clear cooldown + bypass DUAL-GUARD: user-initiated ### must
                        # never be blocked by stale heartbeat from the just-killed session.
                        _recent_launches.pop(rbot, None)
                        launch_session(rbot, cfg, force=True)
                _hash_restart_queue.clear()

            # Death sweep
            if cycle % DEAD_CHECK_EVERY == 0:
                death_sweep(bots)

            # Pre-restart consolidate + Force restart (idle restart removed — handled by AI agent)
            if cycle % DEAD_CHECK_EVERY == 0:
                pre_restart_consolidate(bots)
                force_restart_check(bots)
                idle_kill_after_consolidate(bots)
                hourly_kill_pending_tmux(bots)

            # Consolidate at each round hour (HH:00) — only sessions with incoming activity in the last hour
            _now = datetime.now()
            hour_key = (_now.date(), _now.hour)
            if hour_key != last_consolidate_hour:
                hourly_idle_kill(bots, since_ts=last_consolidate_ts)
                consolidate_sessions(bots, since_ts=last_consolidate_ts)
                stuck_session_restart(bots)
                last_consolidate_hour = hour_key
                last_consolidate_ts = time.time()

            # Archive
            if time.time() - last_archive > ARCHIVE_INTERVAL:
                run_archiver()
                last_archive = time.time()

            # Reload bots.json if changed
            if cycle % 30 == 0:
                try:
                    mtime = BOTS_FILE.stat().st_mtime
                    if mtime != last_bots_mtime:
                        last_bots_mtime = mtime
                        bots = load_bots()
                        _log(f"[RELOAD] bots.json changed")
                except Exception:
                    pass

            # Heartbeat
            HEARTBEAT_FILE.write_text(
                f"{os.getpid()}\n{time.time()}\n{datetime.now().isoformat(timespec='seconds')}"
                f"\ncycle={cycle}",
                encoding="utf-8")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        PID_FILE.unlink(missing_ok=True)
        HEARTBEAT_FILE.unlink(missing_ok=True)
        _log("Master poller stopped.")
        write_event("poller", "stopped", pid=os.getpid())


def cmd_status():
    print(build_status())


def cmd_stop():
    if not PID_FILE.exists():
        print("No master poller running")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to master poller (PID {pid})")
    except ProcessLookupError:
        print("Not running (stale PID)")
        PID_FILE.unlink(missing_ok=True)
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--status":
            cmd_status()
        elif cmd == "--stop":
            cmd_stop()
        else:
            print(f"Usage: python tg_master_poller.py [--status|--stop]")
    else:
        cmd_start()
