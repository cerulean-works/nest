"""Internal logging setup and request context; importing Nest configures no output.

This module is copied into user interpreters as a standalone file, so it must import
only the standard library. [`config`] reads `NEST_LOG_LEVEL` through `parse_log_level`.
"""

import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_request_id = ContextVar("nest_request_id", default="-")

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def parse_log_level(raw):
    """Return `(level, valid)` for a `NEST_LOG_LEVEL` value; invalid values use INFO."""
    level = (raw or "").strip().upper() or "INFO"
    return (level, True) if level in LOG_LEVELS else ("INFO", False)


_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Mask `value`, and each of its lines, in every record Nest's handler writes."""
    for part in (value, *value.splitlines()):
        if len(part.strip()) >= 8:
            _secrets.add(part.strip())


class _RedactingFormatter(logging.Formatter):
    def format(self, record):
        text = super().format(record)
        for secret in sorted(_secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text


class _RequestAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        kwargs["extra"] = {**kwargs.get("extra", {}), "request_id": _request_id.get()}
        return msg, kwargs


def get_logger(name):
    return _RequestAdapter(logging.getLogger(name), {})


@contextmanager
def request_context(value=None):
    """Accept only UUID hex IDs, so incoming metadata cannot inject log lines."""
    value = (
        value
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)
        else uuid.uuid4().hex
    )
    token = _request_id.set(value)
    try:
        yield value
    finally:
        _request_id.reset(token)


def current_request_id():
    return _request_id.get()


def configure_logging():
    """Configure only Nest's console handler at executable entry points."""
    logger = logging.getLogger("nest")
    requested = os.environ.get("NEST_LOG_LEVEL", "")
    level, valid = parse_log_level(requested)
    logger.setLevel(getattr(logging, level))
    if not any(getattr(handler, "_nest_console", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler._nest_console = True  # ty: ignore[missing-attribute]  # marks our handler
        formatter = _RedactingFormatter(
            "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s "
            "pid=%(process)d request_id=%(request_id)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
            defaults={"request_id": "-"},
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    # Keep application/root handlers available, without emitting each event twice.
    logger.propagate = False
    if not valid:
        get_logger("nest.logging").warning(
            "Invalid NEST_LOG_LEVEL=%r; using INFO", requested.upper()
        )


@contextmanager
def operation(logger, name, level=logging.INFO):
    """Record timing and the original traceback without inspecting call arguments."""
    started = time.monotonic()
    logger.log(level, "%s started", name)
    try:
        yield
    except Exception:
        logger.exception("%s failed elapsed_ms=%.1f", name, (time.monotonic() - started) * 1000)
        raise
    logger.log(level, "%s completed elapsed_ms=%.1f", name, (time.monotonic() - started) * 1000)
