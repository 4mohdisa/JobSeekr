"""structlog wiring: readable on the console, JSON on disk, always both.

The apply loop runs unattended for hours, so every event has to survive in a
file that can be diffed after the fact — stdlib loggers (uvicorn, httpx,
playwright) are routed through the same formatter so nothing lands untagged.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Any

import structlog

from backend.config import settings

_configured = False

# Rotate so a runaway loop can't fill the disk; keep enough history to debug a
# week of overnight runs.
_LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
_LOG_FILE_BACKUP_COUNT = 5


def _shared_processors() -> list[Any]:
    """Processors applied to structlog and stdlib records alike."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]


def configure_logging(force: bool = False) -> None:
    """Install the logging pipeline. Idempotent; pass force=True to rebuild."""
    global _configured
    if _configured and not force:
        return

    shared = _shared_processors()

    structlog.configure(
        processors=[
            *shared,
            # Hand the event dict to the stdlib formatter instead of rendering
            # here, so structlog and stdlib records share one output path.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    console_renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if settings.app_env == "local"
        else structlog.processors.JSONRenderer()
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            console_renderer,
        ],
    )
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )

    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(console_formatter)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=settings.logs_dir / "jobseekr.log",
        maxBytes=_LOG_FILE_MAX_BYTES,
        backupCount=_LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root.setLevel(settings.log_level.upper())

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, configuring the pipeline on first use.

    Lazy configuration means a module-level ``log = get_logger(__name__)`` can
    never silently drop events just because main() hasn't run yet.
    """
    if not _configured:
        configure_logging()
    return structlog.stdlib.get_logger(name)
