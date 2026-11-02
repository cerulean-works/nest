"""Internal logging setup and request context; importing Nest configures no output."""

import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_request_id = ContextVar("nest_request_id", default="-")


class _RequestAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        kwargs["extra"] = {**kwargs.get("extra", {}), "request_id": _request_id.get()}
        return msg, kwargs


def get_logger(name):
    return _RequestAdapter(logging.getLogger(name), {})


@contextmanager
def request_context(value=None):
    """Accept only UUID hex IDs, so incoming metadata cannot inject log lines."""
    value = value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) else uuid.uuid4().hex
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
    requested = os.getenv("NEST_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, requested, None)
    valid = isinstance(level, int)
    logger.setLevel(level if valid else logging.INFO)
    if not any(getattr(handler, "_nest_console", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler._nest_console = True
        formatter = logging.Formatter(
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
        get_logger("nest.logging").warning("Invalid NEST_LOG_LEVEL=%r; using INFO", requested)


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
