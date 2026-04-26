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

## What this design is NOT

- **Not multi-machine.** All bots and the poller live on one host. The shared SQLite file enforces this.
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
