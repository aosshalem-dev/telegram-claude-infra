# telegram-claude-infra

Telegram ↔ Claude Code bridge. Lets a user control multiple Claude Code sessions through Telegram bots: each bot = one project, each message wakes a dedicated tmux-backed Claude session, and Claude replies back to the user in-chat.

Pure Python stdlib (no external pip packages). SQLite for message history. tmux for session persistence.

## What this repo contains
Just the **machinery** — no project content, no bot tokens, no conversation data. Setting up your own instance requires:
1. A machine that stays on (macOS/Linux; Windows unsupported).
2. Python 3.9+, `tmux`, `sqlite3` (all standard).
3. [Claude Code](https://docs.claude.com/claude-code) installed and logged in.
4. At least one Telegram bot token from [@BotFather](https://t.me/BotFather).

## Architecture

```
┌──────────────┐                   ┌──────────────────────────────────┐
│  Telegram    │◄──getUpdates───── │  tg_master_poller.py (daemon)    │
│  user        │                   │   • polls all bots every 3s      │
│              │                   │   • logs messages to SQLite      │
│              │                   │   • spawns/kills tmux per bot    │
└──────────────┘                   │   • force-restarts dead sessions │
       ▲                           └────────────┬─────────────────────┘
       │                                        │ spawns
       │                                        ▼
       │                           ┌──────────────────────────────────┐
       │                           │  tmux: tg_<bot_key>              │
       │                           │  └─ Claude Code session          │
       │                           │       │                          │
       └─────sendMessage───────────┤       │ calls:                   │
           (via tg_relay.py)       │       │                          │
                                   │       ▼                          │
                                   │  bin/tg_session_wait.py          │
                                   │   • waits for new messages       │
                                   │   • returns to Claude on arrival │
                                   │                                  │
                                   │  bin/tg_relay.py                 │
                                   │   • sends replies/progress       │
                                   │   • inter-bot messages           │
                                   │   • photo/voice download         │
                                   └──────────────────────────────────┘
```

**Core loop per bot:**
1. Poller sees new Telegram message → writes row to `tg_messages.db` → wakes or spawns tmux session.
2. Claude starts in the tmux pane with an `@<bot>` command, reads bot config, starts `tg_session_wait.py` in background.
3. `tg_session_wait.py` exits when (a) a new message arrives, (b) idle keepalive fires, or (c) consolidate/expire fires.
4. Claude processes the message, replies via `tg_relay.py`, restarts `tg_session_wait.py`, loops.

## Directory layout (recommended)

```
~/telegram-claude-infra/        # root of this repo on the deploying machine
├── bin/                        # infrastructure scripts (shipped here)
│   ├── tg_master_poller.py     # the daemon
│   ├── tg_relay.py             # message I/O
│   ├── tg_session_wait.py      # blocking wait for new messages
│   ├── tg_session_state.py     # session state & heartbeat
│   ├── tg_session_context.py   # save_context / load_context
│   ├── tg_relay_utils.py       # shared helpers
│   ├── tg_approval_gate.py     # optional approval gating
│   ├── read_insights.py        # read project insights
│   ├── write_insight.py        # append to project insights.jsonl
│   ├── write_todo.py           # per-project TODO list (SQLite)
│   ├── write_suggestion.py     # user-approved suggestions
│   └── send_reminder.py        # scheduled reminder helper
├── docs/
│   └── CLAUDE.md.template      # generic relay protocol (copy to ~/CLAUDE.md)
├── templates/
│   ├── telegram_bots.json.template  # bot registry template
│   └── bot_creator_CLAUDE.md   # the bot-creation flow
├── telegram_bots.json          # YOUR bot registry (not committed — .gitignored)
├── tg_messages.db              # SQLite message history (not committed)
└── projects/                   # YOUR project folders (not committed)
    ├── bot_creator/            # the meta-bot that registers new bots
    │   └── CLAUDE.md
    └── <your_project>/
        ├── CLAUDE.md
        ├── insights.jsonl
        └── CURRENT_TASK.md     # optional: resumable task state
```

## Setup — quick start

### 1. Clone
```bash
git clone https://github.com/<you>/telegram-claude-infra.git ~/telegram-claude-infra
cd ~/telegram-claude-infra
```

### 2. Dependencies
```bash
# macOS
brew install tmux sqlite3
# Linux (Debian/Ubuntu)
sudo apt install tmux sqlite3 python3
```
Install Claude Code CLI separately (see Anthropic docs).

### 3. Create your first bot
In Telegram, message `@BotFather`:
```
/newbot
→ pick name, pick username ending in "bot"
→ BotFather gives you a token
```
Find your Telegram user ID by messaging `@userinfobot`.

### 4. Configure
Copy the template and fill in real values:
```bash
cp templates/telegram_bots.json.template telegram_bots.json
# Edit telegram_bots.json:
#   - replace YOUR_BOT_TOKEN with BotFather token
#   - replace 123456789 with your Telegram user ID
#   - set project folder name
```

### 5. Create project folder + protocol
```bash
mkdir -p projects/<your_project>
touch projects/<your_project>/CLAUDE.md projects/<your_project>/insights.jsonl
# Copy the generic relay protocol to your home:
cp docs/CLAUDE.md.template ~/CLAUDE.md
# Edit ~/CLAUDE.md: replace <REPO_ROOT> with ~/telegram-claude-infra
```

### 6. Start the poller
```bash
nohup python3 bin/tg_master_poller.py > /tmp/poller.out 2> /tmp/poller.err &
```
For always-on: wrap in `launchd` (macOS) or `systemd` service (Linux). Sample `launchd` plist in Appendix A.

### 7. Send a message
Open the bot in Telegram and send any text. The poller will:
1. Log the message in `tg_messages.db`.
2. Spawn a tmux session named `tg_<your_bot_key>` running Claude Code.
3. Claude reads the message, replies via `tg_relay.py`.

### 8. Watch a session
```bash
tmux attach -t tg_<bot_key>      # read-only: add -r flag
tmux ls                          # list all sessions
```

## Registering more bots (the `bot_creator` flow)
Register `bot_creator` as one of your bots first. Then in the bot_creator Telegram chat, paste any BotFather output and say "create project called X" — the Claude session running there will parse it, create `projects/X/`, register the bot, and tell you it's ready. See `templates/bot_creator_CLAUDE.md` for the full protocol.

## Admin visibility
Every message (incoming, outgoing, inter-bot) is logged to `tg_messages.db`. Useful queries:
```bash
# Last 50 messages across all bots
sqlite3 tg_messages.db "SELECT bot, direction, sender, substr(text,1,80), msg_time FROM messages ORDER BY id DESC LIMIT 50"

# Activity for one bot
sqlite3 tg_messages.db "SELECT direction, substr(text,1,80), msg_time FROM messages WHERE bot='<bot_key>' ORDER BY id DESC LIMIT 20"

# Session starts/stops
sqlite3 tg_messages.db "SELECT bot, session_type, started_at, heartbeat FROM sessions ORDER BY id DESC LIMIT 20"
```

In addition, every bot writes its insights/TODOs to `projects/<project>/insights.jsonl` and the TODO SQLite — so you can grep them for a rolling activity log per project.

## Hardcoded paths — spots to review
A few helper behaviors assume specific deployment shapes. Before running, review:
- `bin/tg_master_poller.py` line ~1657–1659 (`MAC_DIR`, `MAC_CLAUDE`, `MAC_PATH`) — adjust to your Claude binary location and shell PATH.
- `bin/tg_relay.py` SSH blocks (`USER@HOST` placeholders) — these are optional cross-machine relay features; leave as-is if not using, or replace with your own SSH target.
- `bin/tg_session_state.py` (`MAC_HOST`) — same as above.

Everything else uses `Path(__file__).parent` for script-relative resolution — no edits needed if you keep the `bin/` folder structure.

## Key design choices
- **No pip dependencies.** Only stdlib. This reduces supply-chain risk and makes setup trivial.
- **SQLite for message history.** One `tg_messages.db` holds all history; cross-bot queries are easy.
- **tmux for sessions.** Each bot runs Claude Code inside `tmux tg_<bot_key>`, so sessions survive the poller restarting and can be attached for debugging.
- **Inter-bot messaging.** `tg_relay.py send <target_bot> "msg"` writes to the target's inbox — enables multi-bot workflows (manager bot coordinating worker bots).
- **Session protocol in `CLAUDE.md`.** The behavior Claude follows (startup, session_wait loop, reply formatting) is in the user-level CLAUDE.md. The Python scripts are just plumbing — the "brain" is the protocol text Claude reads.

## Limitations
- macOS / Linux only (tmux required).
- Single-machine: no multi-host bot distribution. All bots run on one daemon.
- No web UI. Admin is via CLI + SQLite queries + `tmux attach`.
- Claude Code authentication (login) is handled outside this repo.

## License
Released under MIT. No warranty. No official support.

---

## Appendix A — launchd sample (macOS)
`~/Library/LaunchAgents/com.telegram-claude-infra.poller.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.telegram-claude-infra.poller</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOU/telegram-claude-infra/bin/tg_master_poller.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/tg_poller.out</string>
  <key>StandardErrorPath</key><string>/tmp/tg_poller.err</string>
</dict>
</plist>
```
Load: `launchctl load ~/Library/LaunchAgents/com.telegram-claude-infra.poller.plist`

## Appendix B — systemd sample (Linux)
`/etc/systemd/system/tg-poller.service`:
```ini
[Unit]
Description=Telegram↔Claude poller
After=network.target

[Service]
Type=simple
User=YOU
WorkingDirectory=/home/YOU/telegram-claude-infra
ExecStart=/usr/bin/python3 /home/YOU/telegram-claude-infra/bin/tg_master_poller.py
Restart=always

[Install]
WantedBy=default.target
```
Enable: `sudo systemctl enable --now tg-poller`
