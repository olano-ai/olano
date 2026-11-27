"""Print the Telegram Bot API calls an agent's bot made for one chat, during a demo take.

    python telegram-outbox.py <agent> <chat id> [since]

Reads them from the take's Telegram seam (``demo/teams/_telegram.py``), which records each call
instead of sending it: there is no bot token on the demo machine. Used by ``flow-monetization.mjs``.
"""

import json
import os
import re
import sys
import textwrap
import urllib.request

agent, chat = sys.argv[1], int(sys.argv[2])
since = int(sys.argv[3]) if len(sys.argv) > 3 else 0
port = os.environ.get("DEMO_MONETIZATION_PORT", "8962")
width = int(os.environ.get("OUTBOX_WIDTH", "64"))
with urllib.request.urlopen(f"http://127.0.0.1:{port}/calls?agent={agent}&since={since}") as r:
    calls = json.load(r)["calls"]


def plain(text: str) -> str:
    """MarkdownV2 as the person reads it."""
    return re.sub(r"\\(.)", r"\1", text or "")


def cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


print(f"Bot API calls for chat {chat} (recorded, not sent: no bot token here)")
for call in calls:
    method = call["method"]
    if method in ("sendChatAction",) or call.get("chat_id", chat) != chat:
        continue
    print()
    if method == "sendMessage":
        print("sendMessage")
        for line in plain(call.get("text", "")).splitlines():
            for row in textwrap.wrap(line, width - 2) or [""]:
                print("  " + row)
        for row in call.get("reply_markup") or []:
            for button in row:
                print(
                    f"  [{button['text']}] {cut(button.get('url') or '', width - 6 - len(button['text']))}"
                )
    elif method == "createInvoiceLink":
        prices = ", ".join(
            f"{p['amount']} {call.get('currency', '')}" for p in call.get("prices", [])
        )
        days = int(call.get("subscription_period") or 0) // 86400
        print("createInvoiceLink   (Telegram Stars)")
        print(f"  {call.get('title', '')}: {prices}, every {days} days")
        print(f"  payload {call.get('payload', '')}")
    else:
        print(method)
