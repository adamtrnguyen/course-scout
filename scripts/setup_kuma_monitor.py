# /// script
# requires-python = ">=3.11"
# dependencies = ["uptime-kuma-api>=1.2.1"]
# ///
"""Create a 'Push' monitor in Uptime Kuma for course-scout and write
the resulting push URL into ../.env as KUMA_PUSH_URL.

Usage:
    KUMA_URL=http://<nas-ip>:3001 \\
    KUMA_USER=<admin-user> \\
    KUMA_PASS=<admin-pass> \\
    uv run scripts/setup_kuma_monitor.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from uptime_kuma_api import MonitorType, UptimeKumaApi

KUMA_URL = os.environ.get("KUMA_URL", "http://localhost:3001")
KUMA_USER = os.environ.get("KUMA_USER")
KUMA_PASS = os.environ.get("KUMA_PASS")
MONITOR_NAME = os.environ.get("KUMA_MONITOR_NAME", "course-scout weekly scan")
KUMA_INTERVAL = int(os.environ.get("KUMA_INTERVAL", "608400"))  # 169h = 7d + 1h slack (weekly)
# KUMA_ENV_PATH lets a sibling project (ga-scout, etc.) reuse this script
# by pointing at its own .env. Defaults to ../.env relative to the script.
ENV_PATH = Path(
    os.environ.get(
        "KUMA_ENV_PATH",
        str(Path(__file__).resolve().parent.parent / ".env"),
    )
)


def main() -> int:
    if not KUMA_USER or not KUMA_PASS:
        print("ERROR: set KUMA_USER and KUMA_PASS env vars", file=sys.stderr)
        return 1

    api = UptimeKumaApi(KUMA_URL)

    # First-run: Kuma has no admin yet. Create it with the supplied creds.
    if api.need_setup():
        print(f"Kuma is fresh — creating admin user '{KUMA_USER}'")
        api.setup(KUMA_USER, KUMA_PASS)

    api.login(KUMA_USER, KUMA_PASS)

    # If a monitor with the same name already exists, reuse it (idempotent).
    existing = next(
        (m for m in api.get_monitors() if m["name"] == MONITOR_NAME),
        None,
    )

    import secrets
    import string

    def _gen_token() -> str:
        return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))

    if existing:
        mid = existing["id"]
        print(f"Reusing existing monitor #{mid}: {MONITOR_NAME}")
    else:
        mid = None  # create below

    if mid is None or True:  # always (re)set token to ensure it's populated
        # Build payload via private helper; inject the v2-required `conditions`
        # field and a freshly generated `pushToken` (Kuma 2.x doesn't auto-gen
        # one from this code path).
        data = api._build_monitor_data(
            type=MonitorType.PUSH,
            name=MONITOR_NAME,
            interval=KUMA_INTERVAL,  # default: 169h (weekly + 1h slack); ga-scout sets KUMA_INTERVAL=90000 for daily
            retryInterval=3600,
            maxretries=1,
        )
        data["conditions"] = []
        data["pushToken"] = _gen_token()

        if mid is None:
            result = api._call("add", data)
            mid = result["monitorID"]
            print(f"Created monitor #{mid}: {MONITOR_NAME}")
        else:
            data["id"] = mid
            api._call("editMonitor", data)
            print(f"Updated monitor #{mid} with fresh push token")

        token = data["pushToken"]

    # Bare base URL — the docker-compose wrapper appends ?status=up&msg=exit_N
    # at runtime. Storing the canonical "?status=up&msg=OK" suffix here would
    # produce a malformed double-query when the wrapper concatenates.
    push_url = f"{KUMA_URL.rstrip('/')}/api/push/{token}"

    api.disconnect()

    # Update .env: replace existing KUMA_PUSH_URL line if present, else append.
    env_text = ENV_PATH.read_text() if ENV_PATH.is_file() else ""
    if re.search(r"^KUMA_PUSH_URL=", env_text, re.MULTILINE):
        env_text = re.sub(
            r"^KUMA_PUSH_URL=.*$",
            f"KUMA_PUSH_URL={push_url}",
            env_text,
            flags=re.MULTILINE,
        )
    else:
        if env_text and not env_text.endswith("\n"):
            env_text += "\n"
        env_text += f"KUMA_PUSH_URL={push_url}\n"
    ENV_PATH.write_text(env_text)

    print(f"\nPush URL: {push_url}")
    print(f"Wrote KUMA_PUSH_URL to: {ENV_PATH}")
    print("\nNext steps:")
    print("  1. Attach a notification provider (Telegram, etc.) to the monitor in Kuma UI")
    print("  2. Restart course-scout: docker --context ugreen compose -f docker-compose.nas.yml up -d --force-recreate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
