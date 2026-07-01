"""Structured logging setup for course-scout.

Two modes, controlled by the COURSE_SCOUT_LOG_FORMAT env var:

- "json"    — JSON lines to stdout. Used in containers; consumed by Vector →
              VictoriaLogs. Drops the file handler — VL is the persistence layer.
- "console" — Pretty colored output to stdout + rotating file handler. Used
              for local dev. Default.

Existing `logging.getLogger(__name__)` calls keep working unchanged; their
output is rendered through structlog's stdlib bridge. New code can use
`structlog.get_logger()` for fully-structured calls.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import structlog

DEFAULT_LOG_DIR = os.environ.get("COURSE_SCOUT_LOG_DIR", "/tmp/course-scout")
_LOG_FORMAT = os.environ.get("COURSE_SCOUT_LOG_FORMAT", "console").lower()


def setup_logging(log_dir: str | None = None, log_level: int = logging.DEBUG) -> None:
    """Configure structlog + stdlib bridge.

    Idempotent — safe to call multiple times.
    """
    log_dir = log_dir or DEFAULT_LOG_DIR
    json_mode = _LOG_FORMAT == "json"

    # Processors run before the final renderer for both stdlib- and
    # structlog-originated records. Order matters: contextvars merge first
    # so per-task bindings show up; level/timestamp next; finally rename
    # `event` → `message` so VictoriaLogs can use `_msg_field=message`.
    #
    # `format_exc_info` only goes in the JSON branch — ConsoleRenderer
    # handles tracebacks itself and warns if format_exc_info ran first.
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", key="@timestamp"),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: structlog.types.Processor
    if json_mode:
        shared_processors.append(structlog.processors.format_exc_info)
        shared_processors.append(structlog.processors.EventRenamer("message"))
        renderer = structlog.processors.JSONRenderer()
    else:
        # ConsoleRenderer gets to render exc_info itself for rich tracebacks.
        shared_processors.append(structlog.processors.EventRenamer("message"))
        renderer = structlog.dev.ConsoleRenderer(colors=True, event_key="message")

    # Configure structlog itself (for `structlog.get_logger()` callers).
    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Stdlib formatter that runs `shared_processors` over foreign (stdlib)
    # records before the final renderer. This is what makes existing
    # `logging.getLogger(__name__).info(...)` calls emit through structlog.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    root = logging.getLogger()
    if root.hasHandlers():
        root.handlers.clear()
    root.setLevel(log_level)

    # Always log to stdout. Container or shell, this is where logs go.
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level if json_mode else logging.INFO)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    # File handler only in console mode — local dev convenience. In containers
    # VictoriaLogs is the persistence layer; double-writing is wasted disk.
    if not json_mode:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "course_scout.log")
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Suppress noisy externals — they emit at DEBUG/INFO frequently and
    # drown out the signals we actually want to query. httpcore is httpx's
    # underlying transport; its DEBUG output includes request bodies and
    # would leak credentials into logs.
    for name in (
        "telethon",
        "httpx",
        "httpcore",
        "claude_agent_sdk",
        "openai",
        "asyncio",
        "markdown_it",  # PDF renderer parser — emits ~30K DEBUG lines/scan otherwise
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Bind service identity to every record in this process.
    structlog.contextvars.bind_contextvars(service="course-scout")

    logging.getLogger(__name__).info(
        "logging_initialized",
        extra={"format": _LOG_FORMAT, "log_dir": log_dir if not json_mode else None},
    )
