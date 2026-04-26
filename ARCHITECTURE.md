# ARCHITECTURE — Telegram ↔ Claude Code Bridge

## What this system does

It lets one human control many parallel Claude Code sessions through Telegram. Each Telegram bot is dedicated to one project. When the human messages a bot, a Claude Code session running in tmux wakes up, reads the project context, processes the message, replies in-chat, and goes back to sleep. Multiple bots = multiple parallel projects, all reachable from the same phone.

It is intentionally minimal:
- Pure Python stdlib. No pip deps.
- SQLite for all message and session state.
- tmux for session persistence.
- The "intelligence" lives in a CLAUDE.md protocol file that each Claude session reads on startup. The Python code is plumbing.

---

## The four moving parts

```
                 ┌──────────────────────────────────────┐
                 │  HUMAN on Telegram                   │
                 └──────────────┬───────────────────────┘
                                │ messages, photos, voice
                                ▼
                 ┌──────────────────────────────────────┐
                 │  Telegram Bot API (cloud)            │
                 └──────────────┬───────────────────────┘
                                │ getUpdates / sendMessage
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  POLLER  (one daemon, polls all bots)                        │
   │  • inbound: getUpdates → write to messages.db                │
   │  • lifecycle: spawn / restart / consolidate / kill tmux      │
   │  • health: monitor pane state, hung-idle, stuck-restart      │
   │  • config: re-reads telegram_bots.json every ~60s            │
   └──────────────┬─────────────────────────────────────┬─────────┘
                  │                                     │
                  │ spawns                              │ writes/reads
                  ▼                                     ▼
   ┌──────────────────────────────────┐   ┌────────────────────────┐
   │  TMUX SESSION (one per bot)      │   │  SQLite                │
   │  └─ Claude Code process          │◄──┤  • messages.db         │
   │     ├─ reads CLAUDE.md protocol  │   │  • sessions table      │
   │     ├─ checks unread via         │   │  • todos.db            │
   │     │   tg_relay.py check        │   │  • insights.jsonl      │
   │     ├─ replies via               │   │     (per-project file) │
   │     │   tg_relay.py reply        │   └────────────────────────┘
   │     └─ blocks on                 │
   │       tg_session_wait.py         │
   │       (returns when new msg or   │
   │        idle keepalive)           │
   └──────────────────────────────────┘
                  │
                  │ HTTPS POST sendMessage
                  ▼
              Telegram cloud → human
```

The four parts are: the Telegram cloud (you don't run it), the **poller** (one daemon for all bots), one **tmux session per bot** (each running Claude Code), and **SQLite** (the only persistent state besides per-project markdown files).

---

## Component contracts

### 1. Poller — `tg_master_poller.py`

A single long-running Python process. It:

- Calls `getUpdates` for every enabled bot every ~3s. Each update is parsed and inserted into `messages.db` with `direction='in'`.
- Monitors every tmux session: is it alive, is Claude alive inside it, what does the pane look like.
- Spawns new tmux sessions on demand: when a message arrives for a bot whose session is dead, the poller launches `tmux new-session -d -s tg_<bot_key> 'claude --resume <session_id>'` (or a fresh `claude` if no resume).
- Force-restarts on schedule (`force_restart_hours`) with a 10-minute pre-restart `consolidate` heads-up.
- Hourly idle-kill of sessions with zero inbound since last consolidate (skipped for `hourly_idle_kill_exempt: true`).
- Last-resort STUCK-RESTART for any session whose oldest unread message is older than ~30 min.
- Hot-reloads `telegram_bots.json` every ~60s — no restart needed for config edits.
- **Never silently kills** a non-zombie session. Killing is always traced and emits an event.

The poller does not do any user-facing work. It writes to the DB, manages tmux, and that's it. All replies come from Claude.

### 2. Claude session (one tmux per bot)

Started by the poller. Each session boots into a Claude Code process that:

1. Reads its bot's protocol from `~/CLAUDE.md` (the relay protocol — generic, shared across bots).
2. Reads its bot's project-specific protocol from `projects/<project>/CLAUDE.md`.
3. Reads recent insights from `projects/<project>/insights.jsonl`.
4. Reads pending TODOs from the project TODO store.
5. Sends a "Connected" message to the user via `tg_relay.py reply`.
6. Spawns `tg_session_wait.py` in the background.
7. When `tg_session_wait.py` exits, processes whatever it returned (new messages, keepalive, consolidate, or AUTO_EXPIRE) and re-spawns it.

The session runs forever (until poller kills it). Context is preserved across messages because Claude Code keeps the same process.

### 3. `tg_relay.py` — the I/O surface for Claude

A single CLI tool with subcommands:

| Subcommand | Purpose |
|---|---|
| `check --bot <key>` | List unread messages for this bot (fetches from `messages.db`, marks `read_time`). |
| `reply --bot <key> "text"` | Send a reply to the most recent message's chat. Telegram `sendMessage` + DB `responded_time` update. |
| `progress --bot <key> "text"` | Same as `reply` but throttled (max once per 2 min) — for "still working" updates. |
| `send --bot <self> <target> "text"` | Inter-bot message: writes to `<target>`'s inbox with `sender='bot:<self>'`. The `[from @x]` convention is enforced by the caller. |
| `voice --bot <key> <file_id>` | Download voice file to local disk (returns path). |
| `photo --bot <key> <file_id>` | Same for photo. |

`tg_relay.py` is short-lived (subprocess per call). All state goes through SQLite. This means schema changes propagate without restarting Claude.

### 4. `tg_session_wait.py` — Claude's blocking input loop

A Python process that Claude spawns and waits on. It blocks until **one of**:

- A new message arrives for this bot (writes message text to stdout, exits).
- A keepalive timer fires (writes `keepalive` to stdout, exits).
- A consolidate event arrives (poller wrote `command: consolidate` to a flag file, exits).
- The token budget is exhausted (writes `budget exhausted`, exits).
- AUTO_EXPIRE fires (the bot's session has been alive past its expiry window, writes `AUTO_EXPIRE`).

When `tg_session_wait.py` exits, Claude reads its stdout, decides what to do, and spawns it again. This is the heart of the "wake on message" loop.

The reason this is a separate subprocess (and not a Python loop inside Claude's reasoning) is that Claude Code can only pause cheaply on a subprocess. If Claude's reasoning had to poll, it would burn tokens forever. By blocking on a subprocess, Claude consumes zero tokens while waiting.

---

## Data model

### `messages.db` — single SQLite file, every bot writes here

```sql
CREATE TABLE messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  bot             TEXT NOT NULL,             -- bot_key, e.g. 'general'
  direction       TEXT NOT NULL,             -- 'in' or 'out'
  sender          TEXT NOT NULL,             -- chat_id (numeric str) for users; 'bot:<bot_key>' for inter-bot
  chat_id         TEXT,                      -- destination chat for outbound; source for inbound
  msg_id          INTEGER,                   -- Telegram message id
  text            TEXT NOT NULL,             -- up to 4096 chars
  msg_time        TEXT NOT NULL,             -- ISO timestamp from Telegram
  read_time       TEXT,                      -- when Claude session_wait delivered it (NULL = unread)
  responded_time  TEXT,                      -- when reply sent (NULL = unanswered)
  response_text   TEXT,                      -- copy of the reply (audit)
  metadata        TEXT                       -- JSON blob: voice_file_id, photo_file_id, parse_mode, etc.
);

CREATE INDEX ix_messages_bot_unread     ON messages(bot, read_time);
CREATE INDEX ix_messages_bot_unanswered ON messages(bot, responded_time);
```

Liveness queries:
```sql
-- Unread per bot
SELECT bot, COUNT(*) FROM messages WHERE direction='in' AND read_time IS NULL GROUP BY bot;
-- Stuck (delivered but no reply)
SELECT bot, COUNT(*) FROM messages WHERE direction='in' AND read_time IS NOT NULL AND responded_time IS NULL;
-- Cross-bot ack drift
SELECT bot, sender, COUNT(*) FROM messages WHERE sender LIKE 'bot:%' AND responded_time IS NULL GROUP BY bot, sender;
```

### `sessions` table — same DB

```sql
CREATE TABLE sessions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  bot             TEXT NOT NULL,
  session_id      TEXT,                      -- Claude resume id
  session_type    TEXT NOT NULL,             -- 'active' | 'dead' | 'standby'
  started_at      TEXT NOT NULL,
  ended_at        TEXT,
  heartbeat       TEXT,                      -- last keepalive write
  reason          TEXT                       -- why ended
);
```

### Per-project files (filesystem, not DB)

```
projects/<project>/
├── CLAUDE.md           # project-specific protocol
├── insights.jsonl      # append-only learnings, one JSON per line
├── todos.jsonl         # pending TODOs (or use a per-project SQLite)
├── CURRENT_TASK.md     # optional: resumable mid-task state
└── tg_wait_result_<bot>.json  # save_context() output, read by next session
```

Why files instead of DB for these: they're append-mostly, low-frequency, and benefit from being human-readable + grep-able + git-trackable.

---

## Event flow — a complete message round-trip

```
user types "fix the bug" in Telegram for bot @myproject
      │
      │ Telegram cloud
      ▼
┌─────────────────────────────────────────────────────────────────────┐
│ POLLER (3s tick)                                                    │
│   • getUpdates for myproject_bot                                    │
│   • sees the message                                                │
│   • INSERT INTO messages (bot, direction, sender, text, msg_time)   │
│         VALUES ('myproject', 'in', '<chat_id>', 'fix the bug', ...) │
│   • check tmux: is tg_myproject session alive?                      │
│       ├─ NO  → spawn it now (tmux new + claude --resume)            │
│       └─ YES → do nothing; the running session_wait will pick it up │
└─────────────────────────────────────────────────────────────────────┘
      │
      │  in the running tmux session, tg_session_wait.py was blocked
      │  it polls messages.db every ~3s, sees the new row, exits with
      │  "trigger: 1 unread message(s) for myproject"
      ▼
┌─────────────────────────────────────────────────────────────────────┐
│ CLAUDE inside tmux                                                  │
│   • reads stdout of session_wait → "CHECK_MESSAGES"                 │
│   • runs `tg_relay.py check --bot myproject`                        │
│         (which marks read_time on the row)                          │
│   • thinks, acts (edits files, runs commands, etc.)                 │
│   • runs `tg_relay.py reply --bot myproject "I fixed it: ..."`      │
│         (sends to Telegram + UPDATE responded_time, response_text)  │
│   • re-spawns tg_session_wait.py in background                      │
│   • idle (consumes zero tokens until the next event)                │
└─────────────────────────────────────────────────────────────────────┘
      │
      ▼
   user sees reply in Telegram
```

Cost: one background subprocess wait. Latency: typically <2s from message to "Claude is processing", then dominated by the actual work Claude does.

---

## Lifecycle events

`tg_session_wait.py` returns with a `command:` line. Claude branches on it:

| Command | Trigger | Claude's action |
|---|---|---|
| `CHECK_MESSAGES` | New message in DB | Fetch via `tg_relay.py check`, process, reply, re-arm wait. |
| `keepalive` | Idle timer (configurable, e.g. 8min) | Write a heartbeat (proves the session is alive), maybe do idle work (process suggestions / reflect / write insights), re-arm wait. |
| `consolidate` (hourly) | Round-hour tick | Write a session summary (insights, TODOs, save_context), send a brief Telegram summary, re-arm wait. |
| `consolidate` (idle-shutdown) | Idle past threshold (configurable, e.g. 1h after last consolidate) | Same as above + send goodbye message. Poller observes the goodbye and kills the tmux. |
| `AUTO_EXPIRE` | Session age past hard expiry | save_context, send goodbye, exit. Poller auto-launches a replacement session. |
| `budget exhausted` | Token budget config exceeded | Re-arm wait (let the budget tick reset). |

The CLAUDE.md protocol tells Claude exactly what to do in each branch. The poller doesn't enforce any of this — it just emits the events.

---

## Bot configuration — `telegram_bots.json`

One JSON file holds every bot's config. The poller hot-reloads it.

```jsonc
{
  "bots": {
    "myproject": {
      "token": "1234567890:ABC...",          // BotFather token
      "name": "My Project",                  // human-readable
      "username": "MyProjectBot",            // Telegram @username (no @)
      "short": "@my",                        // what the user types in @ relay (optional)
      "project": "myproject",                // folder under projects/
      "auto_launch": true,                   // start at poller boot, don't wait for first message
      "platform": "mac",                     // optional: differentiate Mac vs Linux features
      "force_restart_hours": "even",         // restart at 00,02,04,...
      "force_restart_at_minute": 1,
      "hourly_idle_kill_exempt": false,
      "allowed_user_ids": [123456789],       // ACL — if set, only these can message
      "user_aliases": { "alice": 123456789 },// for inter-bot routing
      "require_approval": false,             // gate replies until human OKs
      "chat_id": 123456789                   // override: where "Connected" goes
    }
    // ... more bots
  },
  "daemon": {
    "enabled_bots": ["myproject", ...],      // explicit allowlist for the poller
    "default_model": "sonnet",               // for `claude --model`
    "poll_interval_sec": 3,
    "default_timeout_sec": 300,
    "history_limit": 20
  }
}
```

The `templates/telegram_bots.json.template` in this repo is the starting point.

---

## Directory layout (deployed)

```
~/your-claude-bots/                  # your local root (this repo, renamed/forked)
├── PITFALLS.md, ARCHITECTURE.md     # docs (this repo)
├── README.md, LICENSE               # docs (this repo)
├── SNIPPETS.md                      # critical code excerpts (this repo)
├── docs/CLAUDE.md.template          # the relay protocol — copy to ~/CLAUDE.md
├── templates/                       # config + bot_creator templates
├── bin/                             # YOUR implementation lives here (gitignored)
│   ├── tg_master_poller.py
│   ├── tg_relay.py
│   ├── tg_session_wait.py
│   └── ...
├── telegram_bots.json               # YOUR bot registry (gitignored)
├── messages.db                      # SQLite (gitignored)
└── projects/                        # YOUR project folders (gitignored)
    └── <project>/
        ├── CLAUDE.md
        ├── insights.jsonl
        └── ...
```

The repo ships docs and templates. The `bin/` and `projects/` and `telegram_bots.json` are yours to create.

---

## Why this design

- **One poller, many bots.** Cheaper than one daemon per bot, and you want the cross-bot view (inter-bot messaging, monorepo git, shared dashboard). Single point of failure is a real cost — mitigate with launchd/systemd auto-restart.
- **tmux per bot.** Survives the poller restarting. You can `tmux attach` for live debugging without disrupting anything. State (Claude's context window) lives in the tmux process, not in any file you have to checkpoint.
- **SQLite for everything stateful.** No external DB, easy backup, queryable from CLI, locks are crash-safe. Single-machine concurrency is fine — multi-machine is out of scope.
- **Subprocess CLI as Claude's I/O.** Avoids tying Claude's prompt to any one language's API surface. `tg_relay.py reply "text"` is a stable contract; everything else can change.
- **Protocol in CLAUDE.md, not in code.** The behavior is in text Claude reads. Editing the protocol = editing a markdown file. No code redeploy.

---

## Multi-machine deployments (proposed pattern, untested)

> **STATUS: Untested.** This section is a design — it has not been deployed in production. Treat as a starting point, not a recipe. If you build it, contributing fixes / corrections back to PITFALLS.md is welcome.

The base design is single-machine. If you have bots running on more than one host (e.g., a Mac for some projects + a PC or Linux box for others) and want them to coordinate, the recommended pattern is:

- **Real-time inter-bot messages → Telegram.** Small text, instant. The transport you already have.
- **Heavy artifacts → private GitHub repo.** Files larger than ~4KB, multi-MB reports, anything you want versioned. Sender pushes, receiver pulls when ready. Latency is acceptable because artifacts are not on the latency-critical path.
- **No SSH, no Tailscale, no inbound ports.** Both machines only need outbound HTTPS to Telegram and GitHub. Works behind NAT, no firewall changes.

### How an inter-bot Telegram message goes cross-machine

Each Telegram bot is identified by its token. Tokens are normally machine-local. To enable cross-machine sending, **share the relevant bot tokens across both machines** — specifically, machine 1 must hold the tokens of every bot it might want to send to (even if the actual polling of those bots happens on machine 2).

Sending flow:

```
machine 1, bot A's session decides to send a task to bot B
  │
  ▼
tg_relay.py send <bot_B> "[from @A] please handle X"
  │   (bot B is in machine 1's telegram_bots.json with its token,
  │    even though machine 1 doesn't poll bot B)
  ▼
Telegram API:  POST /bot<TOKEN_OF_B>/sendMessage
                chat_id=<B's user-chat>
                text="[from @A] please handle X"
  │
  ▼
machine 2's poller, on its next getUpdates for bot B,
sees the new message with sender = the bot account
  │
  ▼
machine 2 wakes bot B's tmux session, B reads the message,
sees "[from @A]" prefix, treats as inter-bot directive
  │
  ▼
B replies via tg_relay.py reply (sent to user-chat of B)
  │
  ▼
machine 1's poller is also subscribed to that chat (if it polls A
in the same chat) — OR — B sends a reciprocal inter-bot message
to A using bot A's token (which machine 2 also holds).
```

Two important details:

1. **Both machines must share the user's chat_id** — usually the human owner is the same, so this is just "your Telegram user ID, hardcoded once".
2. **`getUpdates` is exclusive per token.** Only one machine at a time can call `getUpdates` on a given bot token (the second one steals updates from the first). So even though both machines have the token, only one *polls* with it; the other only *sends* with it.

### How heavy artifacts move

For anything larger than a one-line task, attach it via a private GitHub repo:

1. Bot A on machine 1 writes the artifact to a working tree of a private repo (e.g., `~/handoff-bus/artifacts/<uuid>/report.md`).
2. `git add . && git commit -m "artifact <uuid> from A→B" && git push`.
3. A sends an inter-bot Telegram message to B with just the reference: `"[from @A] artifact ready: <uuid>, see commit <hash>"`.
4. B receives, runs `git pull`, reads `~/handoff-bus/artifacts/<uuid>/report.md`, processes.
5. B writes `~/handoff-bus/acks/<uuid>.json`, pushes, sends Telegram `"[from @B] ack <uuid> done"`.

A few rules:

- The handoff repo is **private**. Use a fine-grained PAT or deploy key per machine.
- Squash or expire artifacts periodically. The repo will grow.
- Don't put secrets in artifacts (they're in git history forever). If you must, use git-crypt or age-encrypted blobs.
- For files >50MB, use Git LFS or skip git entirely (e.g., upload to a private S3-like store, send a presigned URL via Telegram).

### Code skeleton

In `tg_relay.py send`, when the target bot's config has `multi_machine: true`:

```python
def send_cross_machine(target_bot_cfg: dict, sender_bot_key: str, text: str):
    """Send an inter-bot message to a bot on another machine.
    Uses the target bot's Telegram token directly; the target's poller
    (on the other machine) sees the message via getUpdates."""
    if not text.startswith(f"[from @"):
        text = f"[from @{sender_bot_key}] {text}"
    token = target_bot_cfg["token"]
    chat_id = target_bot_cfg["chat_id"]  # hardcoded user chat for the owner
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=payload,
                                  headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()
    # NOTE: do NOT also write to local messages.db — the receiving machine
    # will pick this up via its own getUpdates and write its own row.
```

Heavy artifact helper:

```python
def push_artifact(uuid: str, content: bytes, filename: str, repo_dir: Path) -> str:
    """Drop the artifact in the handoff repo and push. Returns commit hash."""
    target = repo_dir / "artifacts" / uuid / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", f"artifact {uuid}"], cwd=repo_dir, check=True)
    subprocess.run(["git", "push"], cwd=repo_dir, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir,
                          capture_output=True, text=True).stdout.strip()
```

### Pitfalls specific to this pattern (also untested — add real ones as you find them)

- **Token leak from machine 1 = compromise of bot B.** Mitigate: each machine's token store is encrypted at rest; rotate tokens on machine loss.
- **Race on `getUpdates`.** If by mistake both machines poll the same bot token, they will alternate stealing updates and both will appear unreliable. Enforce "one poller per token" in your config validation.
- **Telegram rate limits cross-bot.** A token can sendMessage at ~30 msg/sec globally and 1 msg/sec per chat. Multi-machine doesn't multiply this — both machines using the same token share the budget. For burst traffic, use the GitHub artifact path.
- **GitHub PAT scope.** The handoff PAT should be scoped to one repo, not user-wide. If it leaks, the blast radius is the handoff repo only.
- **Heartbeat / liveness.** With cross-machine, "is the other side alive" becomes a real question. Recommend a periodic inter-bot heartbeat (every 10 min, "@A → @B: alive?") and an alert if no reply within 2 cycles.

### When this pattern is the wrong choice

- If you can install Tailscale, that's still simpler — the entire mesh is encrypted, no token sharing, no GitHub dependency. Use this Telegram+GitHub pattern when SSH/Tailscale is unavailable or undesired (e.g., security policy, locked-down corp machine).
- If real-time isn't needed and everything can be batch, skip Telegram for inter-bot entirely and use only the GitHub queue. Simpler, fewer moving parts.
- If you have >2 machines, the token-sharing matrix gets unwieldy fast. Consider a coordinator bot polled by all machines (pattern (b) from the design discussion), at the cost of one indirection per message.

---

## What this design is NOT

- **Not multi-machine** *by default*. All bots and the poller live on one host. The shared SQLite file enforces this. See "Multi-machine deployments" above for the proposed extension (untested).
- **Not a chat UI.** Telegram is the UI. We don't render anything ourselves.
- **Not auth-managed.** Telegram allowed_user_ids gate ACL. There is no OAuth flow, no signup, no rate limiting beyond what Telegram provides.
- **Not multi-tenant.** Built for one human controlling many of their own projects.
- **Not zero-ops.** You'll babysit it. The PITFALLS.md exists because production reveals things tests don't.

---

## Read this next

- **[PITFALLS.md](PITFALLS.md)** — the failure modes you will hit. Read before implementing, not after.
- **[SNIPPETS.md](SNIPPETS.md)** — small annotated code excerpts for the trickier bits (zombie check, fcntl lock, batch ack, keychain validation).
- **[docs/CLAUDE.md.template](docs/CLAUDE.md.template)** — the relay protocol Claude follows. This is the "brain" — adapt it, then your sessions behave correctly.
- **[templates/telegram_bots.json.template](templates/telegram_bots.json.template)** — the config schema, all the way out.
- **[templates/bot_creator_CLAUDE.md](templates/bot_creator_CLAUDE.md)** — the meta-bot pattern: a bot that creates other bots when you message it a BotFather token.
