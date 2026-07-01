# /// script
# requires-python = ">=3.11"
# dependencies = ["uptime-kuma-api>=1.2.1"]
# ///
"""Create Group monitors in Uptime Kuma and re-parent existing monitors
under them so the dashboard collapses related items.

Groups (name → list of child monitor names):
    See GROUPS dict below. Edit inline to taste.

Idempotent: if a group exists, reuse it. Children already pointing at the
right parent are skipped.

Env:
    KUMA_URL, KUMA_USER, KUMA_PASS
"""

from __future__ import annotations

import os
import secrets
import string
import sys

from uptime_kuma_api import MonitorType, UptimeKumaApi

KUMA_URL = os.environ.get("KUMA_URL", "http://localhost:3001")
KUMA_USER = os.environ.get("KUMA_USER")
KUMA_PASS = os.environ.get("KUMA_PASS")

GROUPS: dict[str, list[str]] = {
    "ga-scout": [
        "ga-scout daily poll",
        "docker: ga-scout-cron",
    ],
    "course-scout": [
        "course-scout daily scan",
        "docker: course-scout-cron",
    ],
    "novel-scout": [
        "docker: novel-scout",
        "docker: novel-scout-cron",
    ],
    "Infrastructure": [
        "docker: tailscale",
        "docker: syncthing",
        "docker: pihole_2",
        "docker: unbound",
        "docker: vaultwarden",
        "docker: paperless",
        "docker: paperless-db",
        "docker: paperless-broker",
    ],
    "Media": [
        "docker: jellyfin",
        "docker: calibre-web",
    ],
    "Dev": [
        "docker: forgejo",
        "docker: claude-code",
        "docker: nas-resolver-go",
        "docker: cf-bypass",
    ],
}


def _gen_token() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))


def main() -> int:
    if not KUMA_USER or not KUMA_PASS:
        print("ERROR: set KUMA_USER and KUMA_PASS env vars", file=sys.stderr)
        return 1

    api = UptimeKumaApi(KUMA_URL, timeout=30)
    api.login(KUMA_USER, KUMA_PASS)

    monitors_by_name = {m["name"]: m for m in api.get_monitors()}

    for group_name, children in GROUPS.items():
        # 1. Create or reuse the group monitor.
        group = monitors_by_name.get(group_name)
        if group:
            gid = group["id"]
            print(f"Reusing group #{gid}: {group_name}")
        else:
            data = api._build_monitor_data(
                type=MonitorType.GROUP,
                name=group_name,
                interval=60,
                retryInterval=60,
                maxretries=0,
            )
            data["conditions"] = []
            data.setdefault("pushToken", _gen_token())
            try:
                result = api._call("add", data)
                gid = result["monitorID"]
                print(f"Created group #{gid}: {group_name}")
            except Exception as e:
                print(f"  WARN: group {group_name} failed: {e}")
                continue

        # 2. Re-parent each child.
        for child_name in children:
            child = monitors_by_name.get(child_name)
            if not child:
                print(f"  skip — child not found: {child_name}")
                continue
            current_parent = child.get("parent")
            if current_parent == gid:
                print(f"  already parented: {child_name}")
                continue
            try:
                api.edit_monitor(child["id"], parent=gid)
                print(f"  parented: {child_name}")
            except Exception as e:
                print(f"  WARN: re-parent {child_name} failed: {e}")

    api.disconnect()
    print("\nDone. Reload Kuma UI — collapsible groups should appear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
