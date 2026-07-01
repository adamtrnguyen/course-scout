#!/usr/bin/env -S uv run --quiet --with uptime-kuma-api python
"""One-shot: rename "course-scout daily scan" -> "course-scout weekly scan" and
bump heartbeat from 25h (90000s) to 169h (608400s = 7d + 1h slack).

Usage:
    KUMA_USER=Adam KUMA_PASS=... uv run scripts/flip_kuma_to_weekly.py

If KUMA_PASS is unset, prompts interactively.
"""

from __future__ import annotations

import getpass
import os
import sys

from uptime_kuma_api import UptimeKumaApi

KUMA_URL = os.environ.get("KUMA_URL", "http://100.85.197.24:3001")
OLD_NAME = "course-scout daily scan"
NEW_NAME = "course-scout weekly scan"
NEW_INTERVAL = 608400  # 169h = 7d + 1h slack


def main() -> int:
    user = os.environ.get("KUMA_USER") or input("Kuma user [Adam]: ").strip() or "Adam"
    password = os.environ.get("KUMA_PASS") or getpass.getpass(f"Kuma password for {user}: ")

    api = UptimeKumaApi(KUMA_URL)
    api.login(user, password)

    monitors = api.get_monitors()
    target = next((m for m in monitors if m["name"] in (OLD_NAME, NEW_NAME)), None)
    if target is None:
        print(f"ERROR: no monitor found matching {OLD_NAME!r} or {NEW_NAME!r}", file=sys.stderr)
        api.disconnect()
        return 1

    mid = target["id"]
    print(f"Found monitor #{mid}: {target['name']!r} (interval={target.get('interval')}s)")

    api.edit_monitor(id_=mid, name=NEW_NAME, interval=NEW_INTERVAL)
    print(f"✓ Updated #{mid}: name={NEW_NAME!r}, interval={NEW_INTERVAL}s (169h)")

    api.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
