# SNIPPETS — Critical Code Excerpts

Small annotated excerpts of the trickier parts. **These are not a working implementation** — they're extracts, the way you'd cite a fix in a code review. Use them as a sanity check on your own implementation.

For each one: read **PITFALLS.md first** so the *why* is obvious before you copy.

---

## 1. Zombie detection via `pane_current_command`

When tmux is up but Claude is dead inside the pane, the pane's current command will be a shell (`zsh`/`bash`), not Claude. This is the cheapest reliable check.

```python
def _is_zombie(bot_key: str) -> bool:
    """tmux alive but Claude exited inside."""
    tmux_name = f"tg_{bot_key}"
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-t", f"={tmux_name}", "-F", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out in ("zsh", "bash", "sh", "")
    except Exception:
        return False
```

**Why this shape:** `tmux list-panes -F` is fast (no shell, no pipe), and `pane_current_command` is what tmux already tracks for the foreground process. Don't grep `ps`; don't read pane content; this is enough.

**Critical:** the `=` prefix on `-t =tg_<key>` forces exact match. Without it, tmux does substring matching, and `tg_foo` matches `tg_foobar`.

---

## 2. Batch-ack same-sender to prevent inter-bot drift (PITFALL #3)

When marking a message responded, also mark every **earlier** message from the same sender that's already past `read_time`. Inter-bot bursts produce hundreds of these otherwise.

```python
# Default path (specific row from auto-route): mark just one row, allow next reply to pick up the next oldest.
if specific_msg_id is not None:
    conn.execute(
        "UPDATE messages SET responded_time=?, response_text=?, session_id=? "
        "WHERE id=? AND responded_time IS NULL",
        (now, reply_text[:4096], session_id, specific_msg_id),
    )
else:
    # Bulk path: mark every read-but-unresponded row for this bot.
    conn.execute(
        "UPDATE messages SET responded_time=?, response_text=?, session_id=? "
        "WHERE bot=? AND read_time IS NOT NULL AND responded_time IS NULL",
        (now, reply_text[:4096], session_id, bot_name),
    )

# Always batch-ack inter-bot rows (sender LIKE 'bot:%') even without specific_msg_id —
# inter-bot bursts arrive faster than they can be acked individually.
conn.execute(
    "UPDATE messages SET responded_time=? "
    "WHERE bot=? AND sender LIKE 'bot:%' AND responded_time IS NULL AND read_time IS NOT NULL",
    (now, bot_name),
)
```

**Critical:** the second statement runs unconditionally. We learned the hard way that without it, 24h of inter-bot traffic accumulated 157 stuck rows (PITFALL #3).

**Cap is 4096, not 200 or 500.** Earlier code truncated to 200 chars; this destroyed audit replay capability for long messages (PITFALL #4). 4096 = Telegram's actual max message size.

---

## 3. Hung-idle grace timer

A session can be alive (tmux + Claude process) but stuck (no `session_wait`, idle CPU, has unread). Force-restart only after a grace period.

```python
HUNG_IDLE_GRACE = 180  # seconds
_hung_idle_first_seen: dict[str, float] = {}  # bot_key → epoch

def _maybe_recover_hung_idle(bot_key: str, has_unread: bool, status: str):
    """status: one of 'busy', 'idle', 'hung-idle', 'zombie', 'unknown'."""
    if status != "hung-idle" or not has_unread:
        _hung_idle_first_seen.pop(bot_key, None)
        return

    now = time.time()
    first = _hung_idle_first_seen.get(bot_key)
    if first is None:
        _hung_idle_first_seen[bot_key] = now
        log(f"[HUNG-IDLE-ON-MSG] {bot_key} — alive but deaf with unread, grace {HUNG_IDLE_GRACE}s")
        return

    if now - first < HUNG_IDLE_GRACE:
        return  # still in grace

    log(f"[HUNG-IDLE-ON-MSG] {bot_key} — grace expired, force-restarting")
    force_restart(bot_key)
    _hung_idle_first_seen.pop(bot_key, None)
```

**Critical:** the grace dict is process-local. If your poller restarts, all timers reset. That's fine — the next iteration re-detects.

**`status='hung-idle'` is computed elsewhere** (combination of: tmux alive, Claude PID alive, no `session_wait` child, low CPU for >Nm). See ARCHITECTURE.md → "alive but deaf" check.

---

## 4. Stuck-restart fallback (last line of defense)

If a bot's oldest unread is older than this, the recovery path above failed. Restart unconditionally.

```python
STUCK_RESTART_THRESHOLD = 30 * 60  # 30 min — DO NOT default this higher

def stuck_restart(bots: dict):
    now = time.time()
    for bot_key, cfg in bots.items():
        oldest = oldest_unread_age_sec(bot_key)
        if oldest is None or oldest < STUCK_RESTART_THRESHOLD:
            continue
        log(f"[STUCK-RESTART] {bot_key} — oldest unread {oldest//60}min — forcing restart")
        force_restart(bot_key)
```

**Critical:** see PITFALL #8 — we ran with this at ~13h once because we trusted earlier paths. Set it to a value where you'd be embarrassed if a user noticed silence for that long.

---

## 5. `fcntl.flock` wrapper for monorepo git commits (PITFALL #2)

If you store many projects in one git repo, multiple sessions doing `git add` + `git commit` will cross-pollute the index. Hold an exclusive lock for the whole sequence.

```python
import fcntl
from pathlib import Path

def safe_git_commit(repo_root: Path, paths: list[str], message: str) -> bool:
    """Add paths and commit, holding an exclusive flock for the entire sequence."""
    lock_path = repo_root / ".git" / ".commit_lock"
    lock_path.parent.mkdir(exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)  # blocks until acquired
        try:
            subprocess.run(["git", "add", *paths], cwd=repo_root, check=True)
            r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
            if r.returncode == 0:
                return False  # nothing staged
            subprocess.run(["git", "commit", "-m", message], cwd=repo_root, check=True)
            return True
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
```

**Critical:** the lock spans `add → commit`. Locking only `commit` is wrong — another process can `add` between your add and your commit, sneaking files into your commit.

**`flock` is advisory:** it only blocks code that uses the same lock. Make sure every committer goes through this wrapper.

---

## 6. Keychain credentials validation on macOS (PITFALL #5, #6)

Don't check `~/.claude/.credentials.json`. On Mac, credentials live in the keychain. Validate the keychain entry directly.

```python
import json, subprocess

def claude_credentials_ok() -> tuple[bool, str]:
    """Returns (ok, reason). False if keychain entry is missing or junk."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return False, f"security failed: {e}"

    if out.returncode != 0:
        return False, "keychain entry missing"

    raw = out.stdout.strip()
    if len(raw) < 50:  # the legitimate blob is several hundred bytes
        return False, f"keychain entry too short ({len(raw)} bytes) — likely corrupted"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, "keychain entry is not JSON"

    if "claudeAiOauth" not in data:
        return False, "keychain entry missing claudeAiOauth blob"

    return True, "ok"
```

**Use this as a pre-launch gate.** If it returns False, do NOT spawn Claude — alert and wait for human intervention. See PITFALL #6 about throttling: a guard that blocks every launch must alert loudly enough to be noticed *fast*, not throttle to once-per-N-minutes (which makes a blackout invisible).

---

## 7. Hot-reload `telegram_bots.json` (PITFALL #9)

```python
_BOTS_CACHE = {"path": None, "mtime": 0, "data": None}

def load_bots(path: Path) -> dict:
    """Returns parsed config; re-reads from disk only when mtime changes."""
    try:
        m = path.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _BOTS_CACHE["mtime"] == m and _BOTS_CACHE["data"] is not None:
        return _BOTS_CACHE["data"]
    with open(path) as f:
        data = json.load(f)
    _BOTS_CACHE.update({"path": path, "mtime": m, "data": data})
    log(f"[CONFIG] reloaded telegram_bots.json (mtime={m})")
    return data
```

Call `load_bots()` at the top of each poller iteration. Cost: one stat() call per cycle. Benefit: edits to config take effect within one cycle (~3s) — no kill, no restart.

---

## 8. Inter-bot send convention

```python
# In `tg_relay.py send` (called by sender bot):
def send_to_bot(target_bot: str, text: str, sender_bot: str):
    """Inter-bot message. Prefix '[from @<sender>]' is REQUIRED — receivers
    branch on it to avoid leaking inter-bot text to users."""
    if not text.startswith(f"[from @"):
        text = f"[from @{sender_bot}] {text}"
    # Insert as if it were inbound for the target — same schema as user messages.
    db.execute(
        "INSERT INTO messages (bot, direction, sender, text, msg_time) VALUES (?, 'in', ?, ?, ?)",
        (target_bot, f"bot:{sender_bot}", text, datetime.now().isoformat()),
    )
```

**Critical:** the `sender = 'bot:<key>'` prefix powers two things:
1. The receiver's CLAUDE.md branches on `[from @x]` to handle inter-bot vs user messages differently.
2. The DB-side batch-ack (snippet #2) recognizes `sender LIKE 'bot:%'` and acks aggressively.

Without the convention, replies meant for inter-bot peers leak to users.

---

That's the irreplaceable part. Everything else (HTTP plumbing, JSON parsing, tmux launching, basic SQL) you can reproduce from any tutorial. Re-read PITFALLS.md before declaring v1 done.
