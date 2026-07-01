# /// script
# requires-python = ">=3.11"
# dependencies = ["uptime-kuma-api>=1.2.1"]
# ///
"""Register a local Docker host in Uptime Kuma and create a Docker-container
monitor for each long-running service. Idempotent — safe to re-run.

The default Telegram notification provider (isDefault=True from
setup_kuma_telegram.py) auto-attaches to every new monitor.

Containers to monitor are passed as a comma-separated list via env. Defaults
to a sensible set of long-running NAS services. ONE-SHOT containers (e.g.
course-scout, ga-scout) are excluded — for those the Push monitor is the
right tool, not Docker.

Env:
    KUMA_URL=http://<host>:3001
    KUMA_USER=<admin>
    KUMA_PASS=<pass>
    KUMA_DOCKER_HOST_NAME='Local NAS'  # optional
    KUMA_DOCKER_CONTAINERS='a,b,c'     # optional override; comma-separated
"""

from __future__ import annotations

import os
import secrets
import string
import sys

from uptime_kuma_api import DockerType, MonitorType, UptimeKumaApi

KUMA_URL = os.environ.get("KUMA_URL", "http://localhost:3001")
KUMA_USER = os.environ.get("KUMA_USER")
KUMA_PASS = os.environ.get("KUMA_PASS")
HOST_NAME = os.environ.get("KUMA_DOCKER_HOST_NAME", "Local NAS")

# Default set of long-running services. Excludes:
#   - one-shots (course-scout, ga-scout) — those use Push monitors
#   - uptime-kuma itself (chicken-and-egg)
DEFAULT_CONTAINERS = [
    "claude-code",
    "calibre-web",
    "nas-resolver-go",
    "forgejo",
    "manwha-scout",
    "novel-scout",
    "novel-scout-cron",
    "course-scout-cron",
    "ga-scout-cron",
    "cf-bypass",
    "paper-scout",
    "jellyfin",
    "tailscale",
    "syncthing",
    "pihole_2",
    "unbound",
]

CONTAINERS = [
    c.strip()
    for c in os.environ.get("KUMA_DOCKER_CONTAINERS", ",".join(DEFAULT_CONTAINERS)).split(",")
    if c.strip()
]


def _gen_token() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))


def main() -> int:
    if not KUMA_USER or not KUMA_PASS:
        print("ERROR: set KUMA_USER and KUMA_PASS env vars", file=sys.stderr)
        return 1

    api = UptimeKumaApi(KUMA_URL, timeout=30)
    api.login(KUMA_USER, KUMA_PASS)

    # 1. Docker host — idempotent by name.
    hosts = api.get_docker_hosts()
    host = next((h for h in hosts if h["name"] == HOST_NAME), None)
    if host:
        host_id = host["id"]
        print(f"Reusing Docker host #{host_id}: {HOST_NAME}")
    else:
        result = api.add_docker_host(
            name=HOST_NAME,
            dockerType=DockerType.SOCKET,
            dockerDaemon="/var/run/docker.sock",
        )
        host_id = result["id"]
        print(f"Created Docker host #{host_id}: {HOST_NAME} (socket)")

    # 2. One Docker monitor per container, idempotent by name.
    existing = {m["name"]: m for m in api.get_monitors()}

    created = 0
    skipped = 0
    for c in CONTAINERS:
        name = f"docker: {c}"
        if name in existing:
            print(f"  skip — already monitored: {name}")
            skipped += 1
            continue

        # Bypass add_monitor() to inject Kuma 2.x's NOT NULL `conditions` field.
        data = api._build_monitor_data(
            type=MonitorType.DOCKER,
            name=name,
            interval=120,  # 2 min — Docker checks are cheap
            retryInterval=60,
            maxretries=2,
            docker_container=c,
            docker_host=host_id,
        )
        data["conditions"] = []
        # pushToken is irrelevant for DOCKER monitors but the schema keeps
        # the column NOT NULL — set to a throwaway random value.
        data.setdefault("pushToken", _gen_token())

        try:
            api._call("add", data)
            created += 1
            print(f"  created: {name}")
        except Exception as e:
            print(f"  WARN: {name} failed: {e}")

    api.disconnect()
    print(f"\nDone. Created {created}, skipped {skipped} (already existed).")
    print("New monitors auto-attach to the default Telegram provider.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
