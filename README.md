# telegram-claude-infra

A pattern for letting one human control many parallel Claude Code sessions through Telegram bots. Each bot = one project. Each message wakes a dedicated tmux-backed Claude session, which replies in-chat and goes back to sleep.

> **This repo is intentionally documentation-first.** It does not ship a runnable implementation. It ships everything you need for your AI assistant to **build** the implementation correctly: the architecture, the failure modes, the small annotated code excerpts that are easy to get wrong.
>
> If you want a tutorial-style "clone-and-run" repo, this isn't it. If you want to deploy a Telegram-Claude bridge that survives months of production weirdness, read on.

---

## Why no code?

The architecture (poller + per-bot tmux + SQLite + a CLI relay) is conceptually simple. Re-implementing it from `ARCHITECTURE.md` will get you a working v1 in a weekend. The value an outsider can't easily reproduce is the **set of failure modes you'll hit in production** — zombie tmux sessions, monorepo git races, keychain corruption masquerading as silence, ack-drift across inter-bot bursts, "alive but deaf" Claude processes. Those live in `PITFALLS.md`.

Shipping the original implementation tempts the receiving AI to refactor away the fixes (they look like cruft until you know what they're protecting against). Shipping the description + the bug memorial transfers what's actually expensive: the lessons.

---

## What's in this repo

| File | What it gives you |
|---|---|
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | The system: 4 components, contracts, DB schema, message round-trip, lifecycle events, config schema. Read first. |
| **[PITFALLS.md](PITFALLS.md)** | 17 failure modes from production. Each entry: symptom / root cause / fix / why it's easy to miss. **The most valuable file in the repo.** |
| **[SNIPPETS.md](SNIPPETS.md)** | 8 small annotated code excerpts for the trickier bits (zombie detection, batch-ack, fcntl lock, keychain validation). Use as sanity checks on your implementation. |
| **[docs/CLAUDE.md.template](docs/CLAUDE.md.template)** | The relay protocol — the markdown file each Claude session reads on startup. Defines what Claude does for each event (new message, keepalive, consolidate, expire). The "brain" lives here, not in the Python. |
| **[templates/telegram_bots.json.template](templates/telegram_bots.json.template)** | Bot registry schema — every config option for every bot. |
| **[templates/bot_creator_CLAUDE.md](templates/bot_creator_CLAUDE.md)** | The meta-bot pattern: register a "bot_creator" bot once, then register all future bots by messaging it a BotFather token. |

---

## Recommended reading order

1. **README.md** (this file) — orientation. ~5 min.
2. **[ARCHITECTURE.md](ARCHITECTURE.md)** — what you're building. ~15 min.
3. **[PITFALLS.md](PITFALLS.md)** — what will break. ~25 min, but worth re-reading after your v1 to make sure you've covered them.
4. **[SNIPPETS.md](SNIPPETS.md)** — code-level sanity checks. ~10 min, mostly skim.
5. **[docs/CLAUDE.md.template](docs/CLAUDE.md.template)** — the protocol your sessions follow. Adapt this for your conventions.
6. **[templates/](templates/)** — config and bot-creation flow.

---

## What you need to provide

This repo describes the system; you implement it. You'll need:

- **A machine that stays on.** macOS or Linux. Not Windows (tmux-dependent).
- **Python 3.9+.** Stdlib only — no pip dependencies. (See PITFALL #11 for why.)
- **`tmux` and `sqlite3`.** Standard.
- **[Claude Code](https://docs.claude.com/claude-code) installed and logged in.** The whole system runs Claude Code subprocesses; whatever quota and account you log in with is what they use.
- **At least one Telegram bot token** from [@BotFather](https://t.me/BotFather). One per project once you scale up.
- **An always-on supervisor** for your poller daemon — `launchd` on macOS, `systemd` on Linux. The poller is a single point of failure; auto-restart it.

---

## High-level architecture

```
Telegram cloud ──► poller (one daemon for all bots) ──► tmux per bot ──► Claude Code
                          │                                  │
                          └─ writes ──► SQLite (messages.db) ◄┘ reads
                                              │
                                              └─ tg_relay.py reply ──► Telegram cloud ──► user
```

Full diagrams + contracts in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## How to use this repo with your AI assistant

Suggested workflow:

1. Fork this repo (or just clone it as starter docs).
2. Tell your AI assistant: "Implement the system described in `ARCHITECTURE.md`. Then walk through every entry in `PITFALLS.md` and confirm whether your implementation handles it. For the entries you don't explicitly handle, explain why they're not relevant *or* add the fix. Finally, compare your code against `SNIPPETS.md` to catch shape-level issues."
3. After v1 boots, re-read `PITFALLS.md` once more and challenge the AI on any glossed-over items.
4. Add your own bot configs in `telegram_bots.json` (start from `templates/telegram_bots.json.template`).
5. Adapt `docs/CLAUDE.md.template` to your conventions, copy to `~/CLAUDE.md`.
6. Create `projects/<your_project>/CLAUDE.md` for each project.
7. Start the poller. Send a message. Iterate.

---

## License

MIT. No warranty. No support.

If you ship something based on this, attribution is welcome but not required. PRs to improve PITFALLS.md (new failure modes you discover) are very welcome — that file should grow over time.

---

## What this repo used to be

Until April 2026 this repo shipped the actual `bin/*.py` implementation (~7,600 lines) directly. Experience with handoffs taught us that the code without the bug memorial is misleading: receiving AIs refactored away the very fixes that made it production-stable. The current shape — description + warnings + small annotated excerpts — transfers more usefully.

If you want to see the original implementation as a reference, check out the git history before the 2026-04-26 commit that introduced this layout.
