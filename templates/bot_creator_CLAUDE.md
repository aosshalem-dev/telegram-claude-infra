# Bot Creator Project

A meta-project for creating and managing Telegram bots. Each bot = one project channel for remote Claude Code control via Telegram.

This bot's channel (e.g. `@<your-bot-username>`) is the command interface: the user sends BotFather output here, and the agent processes it.

## Architecture Note
This bot is polled by the central poller (`bin/tg_master_poller.py`). Sessions use `bin/tg_session_wait.py` to listen for messages.

## How This Bot Works
The user sends one of two message types via Telegram:

### Mode 1: Create new project + register bot
User sends BotFather output + "create new project called X"

Agent must:
1. Parse BotFather message to extract: bot username, token
2. Create project folder: `projects/<name>/`
3. Create `projects/<name>/CLAUDE.md` with basic template
4. Register bot in `telegram_bots.json` with `project: "<name>"`
5. Reply on Telegram confirming everything is set up

### Mode 2: Link bot to existing project
User sends BotFather output + "link to project X"

Agent must:
1. Parse BotFather message to extract: bot username, token
2. Verify the project exists (`projects/<name>/`)
3. Register bot in `telegram_bots.json` with `project: "<name>"`
4. Reply on Telegram confirming the bot is linked

## Parsing BotFather Output
The BotFather message always contains:
- Bot URL: `t.me/<BotUsername>` → extract username
- Token: line after "Use this token to access the HTTP API:" → extract token
- These are the only two fields needed

## Adding the new bot to the poller
After registering the bot in `telegram_bots.json`, also add its key to `daemon.enabled_bots` and restart the poller:
```bash
# Kill existing poller, start fresh (pre-approval required)
pkill -f tg_master_poller.py
nohup python3 bin/tg_master_poller.py > /tmp/poller.out 2> /tmp/poller.err &
```

## Required Fields per Bot Entry
Minimum:
```json
{
  "token": "BOTFATHER_TOKEN",
  "name": "Display Name",
  "username": "telegram_username",
  "short": "@shortname",
  "project": "project_folder_name",
  "chat_id": 123456789,
  "auto_launch": true,
  "allowed_user_ids": [123456789],
  "user_aliases": {"you": 123456789}
}
```

## Key Files
- Bot registry: `telegram_bots.json` (source of truth)
- Relay script: `bin/tg_relay.py` (shared, never duplicate)
- Poller: `bin/tg_master_poller.py`

## Rules
- One bot = one project
- Source of truth is `telegram_bots.json`
- NEVER use Telethon or pip packages — relay uses only stdlib (urllib)
- Always reply on Telegram to confirm actions taken
- After responding, wait for the poller to deliver the next message — do NOT poll independently
