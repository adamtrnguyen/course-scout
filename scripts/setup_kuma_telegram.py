# /// script
# requires-python = ">=3.11"
# dependencies = ["uptime-kuma-api>=1.2.1"]
# ///
"""Add a Telegram notification provider to Uptime Kuma and attach it to all
existing monitors so they ping you on missed heartbeats / state changes.

Idempotent: if a provider with the same name already exists, reuse it.

Env:
    KUMA_URL=http://<host>:3001
    KUMA_USER=<admin>
    KUMA_PASS=<pass>
    TG_BOT_TOKEN=<from BotFather>
    TG_CHAT_ID=<from getUpdates>
    TG_PROVIDER_NAME='Telegram (Agentic Orion)'   # optional
"""

from __future__ import annotations

import os
import sys

from uptime_kuma_api import NotificationType, UptimeKumaApi

KUMA_URL = os.environ.get("KUMA_URL", "http://localhost:3001")
KUMA_USER = os.environ.get("KUMA_USER")
KUMA_PASS = os.environ.get("KUMA_PASS")
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")
PROVIDER_NAME = os.environ.get("TG_PROVIDER_NAME", "Telegram")


def main() -> int:
    for var in ("KUMA_USER", "KUMA_PASS", "BOT_TOKEN", "CHAT_ID"):
        if not globals()[var]:
            print(f"ERROR: set {var} env var", file=sys.stderr)
            return 1

    api = UptimeKumaApi(KUMA_URL)
    api.login(KUMA_USER, KUMA_PASS)

    notifications = api.get_notifications()
    existing = next((n for n in notifications if n["name"] == PROVIDER_NAME), None)

    if existing:
        nid = existing["id"]
        print(f"Reusing notification provider #{nid}: {PROVIDER_NAME}")
    else:
        result = api.add_notification(
            name=PROVIDER_NAME,
            type=NotificationType.TELEGRAM,
            isDefault=True,
            applyExisting=True,
            telegramBotToken=BOT_TOKEN,
            telegramChatID=CHAT_ID,
        )
        nid = result["id"]
        print(f"Created notification provider #{nid}: {PROVIDER_NAME}")

    # Force-attach to every existing monitor (in case applyExisting didn't fire,
    # or new monitors were created after the provider).
    monitors = api.get_monitors()
    for m in monitors:
        current = set(m.get("notificationIDList", {}).keys()) if isinstance(
            m.get("notificationIDList"), dict
        ) else set(m.get("notificationIDList") or [])
        if str(nid) not in current and nid not in current:
            try:
                api.edit_monitor(m["id"], notificationIDList={str(nid): True})
                print(f"  attached to monitor #{m['id']} ({m['name']})")
            except Exception as e:
                print(f"  WARN: could not attach to #{m['id']}: {e}")
        else:
            print(f"  already on monitor #{m['id']} ({m['name']})")

    api.disconnect()
    print("\nDone. Test by pausing one of the scouts so a heartbeat is missed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
