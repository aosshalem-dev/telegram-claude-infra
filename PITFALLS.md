# PITFALLS — Read This Before You Build

This file is the most valuable artifact in this repo.

The architecture (poller + session_wait + relay + SQLite + tmux) is conceptually simple. Re-implementing it from the description in `ARCHITECTURE.md` will get you a working v1 in a weekend. What this file gives you is **everything I learned by running this in production for months** — bugs that took hours or days to diagnose, fixes that look strange until you know why.

If your AI assistant is generating an implementation from `ARCHITECTURE.md`, hand it this file too. Most of these failures are non-obvious; the AI will not invent the fixes preemptively.

Each entry: **Symptom** (what you see) / **Root cause** (what's actually broken) / **Fix** (the shape of the patch) / **Why this is easy to miss**.

---

## 1. Zombie session_wait claims messages without replying

**Symptom:** A bot's incoming messages get marked `read_time` in the DB but no reply ever goes out. From the user's side, the bot is silent. From the admin's side, the message looks "delivered" but isn't.

**Root cause:** `tg_session_wait.py` is a long-running subprocess. When the surrounding tmux session dies (Claude crashed, tmux killed, machine slept), the `session_wait` process can keep running for a beat and still execute its "I delivered the message" DB update. There's no Claude on the other end to actually answer.

**Fix:** Before `deliver_unread()` issues the `UPDATE messages SET read_time=...`, it must check `tmux has-session -t tg_<bot_key>` and abort if the tmux pane is gone.

**Why easy to miss:** The race window is small. You'll see this maybe 1 in 200 messages, more during reboots or sleep cycles, and only on bots that get heavy traffic. The DB looks correct — `read_time` is set, so your dashboard says "responded". You only catch it by querying for `read_time IS NOT NULL AND responded_time IS NULL` rows older than a few minutes.

---

## 2. Two pollers on the same monorepo overwrite each other's commits

**Symptom:** Cross-project pollution in commits — a "session_start" commit for project A contains 50,000 lines of project B's work. `git log` becomes a mess. Some sessions' work disappears entirely.

**Root cause:** Multiple Claude sessions running simultaneously, each doing `git add projects/<their_project>/ && git commit`. If two sessions execute these in interleaved order (A adds, B adds, A commits — now A's commit includes B's staged changes), index races corrupt the picture.

**Fix:** A `git_commit.py` wrapper that takes an exclusive `fcntl.flock` on `.git/.persistent_team_lock` for the entire `add → commit` sequence. Any concurrent committer blocks on the lock.

**Why easy to miss:** Each session's commit looks reasonable in isolation — it's only when you `git log -p` and see foreign files that you realize. Single-session test runs never reproduce. You need ≥2 active sessions hitting the same git directory in the same second.

---

## 3. Inter-bot ack drift (the `bot:%` sender backlog)

**Symptom:** Inter-bot messages (e.g. `@manager` sending to `@worker`) accumulate as "unread" in the DB even though they've been processed. Eventually you have hundreds of stale `bot:*` rows clogging dashboards. Worse: re-deliveries fire because the message looks fresh.

**Root cause:** `_mark_responded_db()` originally marked exactly one row per call — the latest unread for that sender. But inter-bot bursts arrive faster than they can be acked individually. Each ack only covered the most recent message; older ones from the same bot stayed unread forever.

**Fix:** When marking responded, batch-mark all earlier rows from the same sender that were already past `read_time`. Always auto-ack rows where `sender LIKE 'bot:%'` (inter-bot messages get one combined ack per processing cycle).

**Why easy to miss:** From a user-bot perspective everything works. The drift is invisible until you query `WHERE responded_time IS NULL`. We had 157 stuck `bot:*` rows accumulated before noticing.

---

## 4. Reply-text DB log was truncated to 200 chars (delivery was full)

**Symptom:** Auditing the message DB, you find that exactly N% of OUT rows have `LENGTH(text)=200`. Telegram delivery looked fine — users got full messages — but your audit/replay capability is destroyed.

**Root cause:** `reply()` passed `text[:200]` into the logging helper. Telegram delivery used the full text; only the DB copy was truncated. The cap was set early when DB churn was a concern, then never revisited.

**Fix:** Drop the `[:200]` cap. Raise internal cap to `[:4096]` (Telegram's actual max).

**Why easy to miss:** The bug only affects logs, not delivery. No user complaint will ever surface it. You discover it by querying `SELECT LENGTH(text), COUNT(*) FROM messages WHERE direction='out' GROUP BY LENGTH(text)` and seeing a giant spike at 200.

---

## 5. macOS keychain holds OAuth credentials — and can be corrupted to literal `<json>`

**Symptom:** All Claude sessions go silent simultaneously. Newly launched Claude says "Not logged in". Existing sessions keep working until their next token refresh, then die one-by-one over hours.

**Root cause:** The Claude Code CLI on macOS stores OAuth credentials in the keychain entry `Claude Code-credentials` (account = your username). Some interaction (cause unknown — possibly a failed shell heredoc that printed the placeholder string `<json>` instead of interpolating it) overwrites the entry with the literal string `<json>` (5 bytes). The keychain entry exists but is junk.

**Fix:**
- Pre-launch sanity check in your poller: `security find-generic-password -s "Claude Code-credentials" -w` and validate that the result parses as JSON containing a `claudeAiOauth` blob. If not, skip the launch and alert.
- **Do NOT** restore from a snapshot automatically — the saved `refreshToken` may have been rotated since, and a stale refresh will 401, killing every running session on its next refresh.
- Recovery is manual: user runs `claude /login`.

**Why easy to miss:** Running sessions hold an in-memory access token loaded at startup. They survive corruption for the lifetime of that token (often hours). The blackout creeps in gradually, looks like network/Telegram trouble, not auth. The keychain CLI must be the validation oracle — checking `~/.claude/.credentials.json` won't work on a Mac that uses keychain-only.

---

## 6. The credentials guard you write will probably guard the wrong thing

**Symptom:** You add a "validate creds before launching Claude" check after Pitfall #5 hits. Hours later, every bot stops launching — the guard returns "missing" for every bot, even though everything is actually fine.

**Root cause:** You wrote the guard against `Path('~/.claude/.credentials.json').exists()` because that's the obvious file path. On the Mac in question, that file doesn't exist; the credentials live in the keychain only. Your guard returns False for every launch.

**Fix:** Validate against the actual storage backend — for macOS, shell out to `security find-generic-password` and validate the JSON. Don't assume the disk file is the source of truth.

**Why easy to miss:** Your test environment probably has the file (you might have run `claude` differently once). The pattern is general: **when you write a guard against an incident, validate it against the actual incident state, not the theoretical one.** A guard that becomes a block needs a louder alert than the throttle of the original incident detection (or the cure becomes the disease).

---

## 7. The "alive but deaf" session

**Symptom:** A bot has unread messages for hours. The poller's `tmux ls` shows the session is up. `pgrep claude` shows a Claude process. Nothing happens.

**Root cause:** Claude is running, but it's stuck — waiting on a hung HTTP refresh, blocked on a deadlocked subprocess, or just lost in a malformed prompt. The poller's "is it alive" check sees a process and a tmux pane and assumes it's healthy.

**Fix:** Multi-tier liveness check:
1. `tmux has-session` → tmux alive?
2. Inspect pane content (last 50 lines via `tmux capture-pane`) → is the Claude footer present?
3. `ps -o etime,%cpu` on the Claude PID → has CPU been zero for >Nm?
4. If pane shows shell prompt or crash text → mark **zombie**, no auto-kill (preserve for debugging) but raise alert.
5. If alive + idle + has unread → start a "HUNG-IDLE-ON-MSG" grace timer (180s recommended). After grace, force-restart the session.

Plus a separate "STUCK-RESTART" path: any session whose oldest unread message is older than 30 min, restart unconditionally.

**Why easy to miss:** Your first version of `_session_alive()` will be one or two of these checks. The alive-but-deaf failure mode requires all of them combined. The cost of getting this wrong is hours of silence — happened to us once for 13 hours.

---

## 8. STUCK-RESTART threshold defaults too high

**Symptom:** Bots come back online after a multi-hour outage with no human intervention. You're relieved. Then you check the DB and find the oldest stuck message was 13h old when the recovery fired.

**Root cause:** The STUCK-RESTART path was defaulted to a high threshold (790 minutes in our case) on the assumption that other recovery paths would catch problems sooner. They didn't. STUCK-RESTART is the last line of defense — set it to a value where you'd be embarrassed if the user noticed.

**Fix:** Default to ~30 minutes. Per-bot override for special cases (long-running batch bots).

**Why easy to miss:** It works — eventually. Until a user happens to be looking, you won't know. The only signal is querying `MAX(now - msg_time) WHERE responded_time IS NULL` and watching for trends.

---

## 9. `telegram_bots.json` already hot-reloads; you don't need to kill sessions to edit it

**Symptom:** Every config edit triggers a "now restart all the affected bots" cascade, dropping in-flight messages and disrupting active conversations.

**Root cause:** Habit. The poller actually re-reads the bot config every ~60 seconds. New bots get picked up, removed bots get cleaned up, changed `enabled_bots` lists take effect — all without restart.

**Fix:** Just save the file. Wait 60s. Done. Reserve restart for token rotations or daemon code changes.

**Why easy to miss:** Most config systems do require restart, so you assume this one does too. Read your own poller's config-load logic before you reach for `kill`.

---

## 10. OAuth refresh: in-memory tokens outlive disk swaps (good and bad)

**Symptom A (good):** You swap accounts mid-session via `claude-use.sh`. Live sessions keep working — they're using the in-memory access token loaded at their startup. Quota stays on the original account until they next refresh.

**Symptom B (bad):** You restore credentials from a snapshot to "fix" a corruption. The snapshot's `refreshToken` was rotated since. First refresh on every running session 401s. All sessions die.

**Root cause:** The Claude Code CLI loads OAuth credentials from disk/keychain into memory at process start. Subsequent refreshes use the in-memory `refreshToken` and write the new pair back to disk/keychain. So "the credentials" are actually two things: the live in-memory pair and the at-rest pair. Rotating either out of sync with the other breaks things.

**Fix:** Treat credential-store writes as nuclear. If you must restore from a snapshot, accept that all running sessions will die at their next refresh window. Plan a coordinated relaunch.

**Why easy to miss:** Account swaps work *most* of the time, lulling you into thinking the credential file is just a config file. It's not. It's a refresh-token vault, and refresh tokens rotate.

---

## 11. Voice/photo handling without third-party libs

**Symptom:** You instinctively reach for `python-telegram-bot` or `Telethon` to handle voice/photo. Now you have a pip dep, an asyncio runtime, and a maintenance burden — and you've just added a class of supply-chain attack to a system that handles your shell.

**Root cause:** "Surely the official-looking library is the right way."

**Fix:** Telegram's Bot API is a JSON HTTP endpoint. `urllib.request` handles all of it including `getFile` + downloading the binary. Photo and voice are just file IDs — fetch the path with `getFile`, then download from `api.telegram.org/file/bot<token>/<path>`. Voice transcription is a separate concern (Whisper, etc.), independent of how you got the bytes.

**Why easy to miss:** It feels primitive, and the library docs are everywhere. But the ROI of stdlib-only is enormous: zero supply chain, zero version drift, trivial reproducibility on a fresh machine.

---

## 12. The `consolidate` ↔ `idle-kill` interaction

**Symptom:** Sessions accumulate forever. Even idle bots stay attached, eating RAM. Restart-cleanup never happens. Or the opposite: sessions get killed mid-conversation.

**Root cause:** Two different events that look similar:
- **`consolidate`** fires hourly (or 10 min before a force-restart). Tells Claude to summarize what it did, save context, restart `session_wait`. Session keeps living.
- **`consolidate` from idle-shutdown** fires when a session has been idle past a threshold. Tells Claude to summarize, save context, send a goodbye, **and exit**. Poller kills the tmux on observing the goodbye.

If you don't distinguish these, you either kill busy sessions or fail to clean up idle ones.

**Fix:**
- Hourly consolidate: send `command: consolidate` with a description that ends in "Then resume listening."
- Idle consolidate: send `command: consolidate` with a description that ends in "Ready to shut down." or include a separate signal field.
- Poller watches the outbound message Claude sends after consolidate — if it ends with "Ready to shut down" pattern, kill the tmux. Otherwise, leave it.

**Why easy to miss:** Both flows go through the same `consolidate_sessions()` function, easy to merge them. You discover the bug when an active conversation gets killed because someone moved an `if cfg.idle:` line.

---

## 13. `hourly_idle_kill_exempt` — the bot you don't want auto-killed

**Symptom:** Your "general" / control bot keeps disappearing. You log in, the session is dead, you have to relaunch it manually. Every hour.

**Root cause:** The hourly idle-kill rule applies to all bots by default. The general/control bot is often idle (it's a status panel, not an active conversation), so the rule kills it, but unlike project bots there's no user message to resurrect it.

**Fix:** Per-bot config flag `hourly_idle_kill_exempt: true`. The idle-kill loop respects this flag.

**Why easy to miss:** The rule was a good idea. The exemption is the patch. Without it, your control plane keeps dying.

---

## 14. `force_restart_hours` (clock-aligned) for known-leaky sessions

**Symptom:** Some bots accumulate context bloat over many hours and become slow / hit context limits / start hallucinating. You don't want them to die from running out of context — you want them to die proactively.

**Root cause:** Long-running Claude sessions have unbounded context growth. Compaction helps but isn't perfect. Some bots simply benefit from a hard restart at predictable intervals.

**Fix:** Per-bot config: `force_restart_hours: "even"` (or `[10,12,14,16,...]`) + `force_restart_at_minute: 1`. Combined with pre-restart `consolidate` 10 min before, you get clean handoffs.

**Why easy to miss:** It feels heavy-handed to schedule restarts. But it's strictly better than waiting for the session to fail.

---

## 15. Three persistence layers, each with its own role

**Symptom:** You build one persistence mechanism, and it has to serve every concern — handoff, audit, status, debugging. It works for 80% of cases and fails confusingly for 20%.

**Root cause:** Different needs:
- **`save_context()` / `tg_wait_result_<bot>.json`** — immediate handoff. The replacement session reads this on startup. Short, action-oriented: "what to do next, files modified, pending items".
- **`CURRENT_TASK.md`** in project folder — resumable task. If you die mid-task, the next session sees this and picks up. Survives across multiple sessions.
- **`progress_tracker.py` SQLite** — audit trail across all bots. Cross-project queries: "what was active in the last hour", "what session_X did".
- **`insights.jsonl` / `todos.jsonl`** — durable lessons. Future sessions read these as wisdom.

**Fix:** Build all four. They're not redundant. Each takes ~30 lines.

**Why easy to miss:** "Surely one of them is enough." It isn't. The three timescales (immediate, mid-task, historical) and two scopes (per-bot vs cross-bot) actually need separate stores.

---

## 16. The `[from @x]` inter-bot prefix is not optional

**Symptom:** Bots receive inter-bot messages and act on them as if they were user messages. Sometimes they reply to the wrong person, sometimes they execute commands meant for another bot's context.

**Root cause:** Telegram doesn't distinguish "real human typed this" from "another Claude session sent this via the relay". You have to encode the distinction yourself.

**Fix:**
- Sender writes `tg_relay.py send <target_bot> "[from @sender] message"` — the `[from @x]` is a hard convention.
- Receiver's CLAUDE.md instructs: "if message starts with `[from @x]`, treat as inter-bot, never reply to user with it as-is".
- DB-side: outbound inter-bot messages have `sender = 'bot:<sender_key>'` (vs `'<chat_id>'` for users), enabling SQL filters.

**Why easy to miss:** Your first inter-bot test will work. The first time a bot's reply leaks back to a user — usually as "[from @worker] task complete" reaching a customer — you'll add the convention. Better to add it from day one.

---

## 17. Don't mock the Telegram API in tests

**Symptom:** Your unit tests pass. Your bots break in production on an edge case the mock didn't cover (rate limits, file size limits, encoding edge cases, message length limits, parse_mode quirks).

**Root cause:** Telegram Bot API has a long tail of behaviors that no mock will faithfully reproduce. Rate limit headers, retry-after semantics, file ID expiration, the difference between `text` length and `entities` byte offsets in Unicode — all of these bite eventually.

**Fix:** Run integration tests against a dedicated test bot (free, takes 30s in BotFather). Hit the real API. The cost is one round-trip per test; the value is finding real bugs.

**Why easy to miss:** "Mocks are fast" — yes, and wrong. For an external API you don't control, mocks are a false sense of safety.

---

## How to use this list

1. Re-implement the architecture from `ARCHITECTURE.md`.
2. Before declaring v1 done, walk through this list and ask: "Did I handle this?"
3. If your AI says "I don't think this is a problem" — it is. The bugs in this list all looked like non-problems until they fired.
4. The fixes here are all small (10–50 lines each). The hard part was knowing they were needed.
