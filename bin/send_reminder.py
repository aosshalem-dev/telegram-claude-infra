#!/usr/bin/env python3
"""Send a reminder via Telegram. Called by Windows Task Scheduler."""
import sys
import json
import urllib.request
from pathlib import Path

_HERE = Path(__file__).parent

def send(text):
    with open(_HERE / "telegram_config.json") as f:
        cfg = json.load(f)
    tg = cfg.get("telegram", {})
    token = tg.get("bot_token", "")
    chat_id = tg.get("allowed_user_ids", [None])[0]
    if not token or not chat_id:
        print("Telegram not configured")
        return
    data = json.dumps({"chat_id": chat_id, "text": f"\u23f0 {text}"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data, {"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=10)
    print(f"Sent: {text}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python send_reminder.py 'reminder text'")
        sys.exit(1)
    send(" ".join(sys.argv[1:]))
