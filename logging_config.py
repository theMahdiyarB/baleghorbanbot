# bale_bot/logging_config.py
# Structured JSON logging with structlog
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog
from structlog.stdlib import ProcessorFormatter

from config import get_settings


def setup_logging(settings=None) -> None:
    """Configure structlog with JSON output to stdout + file."""
    if settings is None:
        settings = get_settings()

    # Standard library logging config
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # Shared processors for both stdlib and structlog
    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        ContextInjector(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    # Configure stdlib logging
    logging.basicConfig(
        level=log_level,
        format="%(message)s",  # structlog handles formatting
        handlers=[],
    )

    # File handler with JSON formatting
    file_handler = logging.FileHandler("bale_bot.log", encoding="utf-8")
    file_handler.setLevel(log_level)

    if settings.LOG_JSON:
        file_formatter = ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    else:
        file_formatter = ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False),
            foreign_pre_chain=shared_processors,
        )
    file_handler.setFormatter(file_formatter)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    if settings.LOG_JSON:
        console_formatter = ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    else:
        console_formatter = ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=True),
            foreign_pre_chain=shared_processors,
        )
    console_handler.setFormatter(console_formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [console_handler, file_handler]

    # Reduce noise from noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.PositionalArgumentsFormatter(),
            ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)


# Context variables for request-scoped logging
import contextvars

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
chat_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("chat_id", default=None)
platform_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("platform", default=None)


def bind_request_context(request_id: str, chat_id: int | None = None, platform: str | None = None) -> None:
    """Bind context variables for structured logging."""
    request_id_var.set(request_id)
    if chat_id is not None:
        chat_id_var.set(chat_id)
    if platform is not None:
        platform_var.set(platform)


def clear_request_context() -> None:
    """Clear request context variables."""
    request_id_var.set(None)
    chat_id_var.set(None)
    platform_var.set(None)


class ContextInjector:
    """Structlog processor that injects context variables into log entries."""

    def __call__(self, logger: Any, method_name: str, event_dict: dict) -> dict:
        rid = request_id_var.get()
        cid = chat_id_var.get()
        plat = platform_var.get()
        if rid:
            event_dict["request_id"] = rid
        if cid is not None:
            event_dict["chat_id"] = cid
        if plat:
            event_dict["platform"] = plat
        return event_dict