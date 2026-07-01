"""Pre-flight Claude OAuth quota check.

Lets a cron-triggered scan wait until the rolling-window quota has refilled
instead of hitting 429 mid-run.

Strategy
--------
Make a 1-token POST to /v1/messages and read the response's
`anthropic-ratelimit-unified-*` headers. The `representative-claim` header
tells which window (5h or 7d) is currently the gating constraint; if the
matching utilization is at/above the threshold, sleep until that window's
reset timestamp.

Why not GET /api/oauth/usage: that endpoint requires `user:profile` scope,
which `claude setup-token` tokens don't grant — call returns 403 by design.
The 1-token probe costs ~$0.000005 and uses negligible quota itself.

Limitations
-----------
- The unified-* headers and the `claude-code-20250219` beta are not formally
  documented for third-party use. If Anthropic tightens, this falls back to
  log-and-proceed instead of blocking the scan.
- We don't distinguish 7d-overflow gracefully — if a 7d cap is hit, sleeping
  could be days. The 6h max_wait cap protects against that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

PROBE_URL = "https://api.anthropic.com/v1/messages"
PROBE_MODEL = "claude-haiku-4-5"
BETA_HEADERS = "claude-code-20250219,oauth-2025-04-20"


def _get_oauth_token() -> str | None:
    """Bearer from CLAUDE_CODE_OAUTH_TOKEN env or ~/.claude/.credentials.json."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return token
    creds = Path.home() / ".claude" / ".credentials.json"
    if creds.is_file():
        try:
            data = json.loads(creds.read_text())
            return data.get("claudeAiOauth", {}).get("accessToken")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _probe_quota(token: str) -> tuple[float, int, str] | None:
    """One 1-token /v1/messages call; returns (util_pct, reset_ts, window) or None.

    Tolerates HTTP error responses (including 429 "rate limited") because the
    `anthropic-ratelimit-unified-*` headers come back on every response, even
    rejected ones. Only network errors / missing headers return None.
    """
    try:
        r = httpx.post(
            PROBE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": BETA_HEADERS,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": PROBE_MODEL,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}],
            },
            timeout=15.0,
        )
    except Exception as e:
        logger.warning("rate_limit: probe failed (%s) — no rate-limit info", e)
        return None

    h = r.headers
    claim = h.get("anthropic-ratelimit-unified-representative-claim", "five_hour")
    window = "5h" if claim == "five_hour" else "7d"
    util_str = h.get(f"anthropic-ratelimit-unified-{window}-utilization")
    reset_str = h.get(f"anthropic-ratelimit-unified-{window}-reset")

    if util_str is None or reset_str is None:
        # Auth or header issue — surface response status to help debug.
        logger.warning(
            "rate_limit: probe HTTP %d returned no unified-* headers — proceeding",
            r.status_code,
        )
        return None

    return float(util_str) * 100, int(reset_str), window


def wait_for_quota(threshold_pct: float = 95.0, max_wait_seconds: int = 6 * 3600) -> None:
    """Block until current rolling window utilization is below threshold.

    Args:
        threshold_pct: utilization (0-100) at or above which we wait
        max_wait_seconds: hard cap on sleep duration (default 6h)
    """
    token = _get_oauth_token()
    if not token:
        logger.info("rate_limit: no OAuth token; skipping pre-flight")
        return

    result = _probe_quota(token)
    if result is None:
        return  # already logged

    util_pct, reset_ts, window = result
    logger.info("rate_limit: %s utilization=%.1f%%", window, util_pct)

    if util_pct < threshold_pct:
        return

    now = time.time()
    wait_s = (reset_ts - now) + 30  # 30s grace past reset

    if wait_s <= 0:
        logger.info("rate_limit: reset already passed — proceeding")
        return
    if wait_s > max_wait_seconds:
        logger.warning(
            "rate_limit: wait %ds > cap %ds — capping",
            int(wait_s),
            max_wait_seconds,
        )
        wait_s = max_wait_seconds

    logger.info(
        "rate_limit: %s at %.1f%% — sleeping %d min until reset",
        window,
        util_pct,
        int(wait_s / 60),
    )
    time.sleep(wait_s)


async def await_window_reset(
    fallback_seconds: float = 65.0,
    max_wait_seconds: int = 6 * 3600,
) -> None:
    """Async: on 429, probe headers and sleep until the gating window resets.

    Designed to replace the legacy fixed-`rate_limit_retry_sleep` retry inside
    `AIAgent.run()`. If the probe can't determine reset time (no token, network
    error, missing headers), falls back to a short fixed sleep so the caller
    still backs off before retrying.
    """
    token = _get_oauth_token()
    if not token:
        await asyncio.sleep(fallback_seconds)
        return

    result = _probe_quota(token)
    if result is None:
        await asyncio.sleep(fallback_seconds)
        return

    util_pct, reset_ts, window = result
    wait_s = (reset_ts - time.time()) + 30  # 30s grace past reset

    if wait_s <= 0:
        # Window already reset — short backoff and let the caller retry.
        await asyncio.sleep(fallback_seconds)
        return
    if wait_s > max_wait_seconds:
        logger.warning(
            "rate_limit (mid-call): wait %ds > cap %ds — capping",
            int(wait_s),
            max_wait_seconds,
        )
        wait_s = max_wait_seconds

    logger.info(
        "rate_limit (mid-call): %s at %.1f%% — sleeping %d min until reset",
        window,
        util_pct,
        int(wait_s / 60),
    )
    await asyncio.sleep(wait_s)
