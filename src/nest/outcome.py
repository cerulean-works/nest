"""
The worker's outcome sidecar: whether a remote call returned or raised.

A remote call's result, `output.pkl`, is a cloudpickle stream of either the
return value or a remote error record. The Controller must never unpickle it,
since unpickling worker output runs code the caller controls. So next to it the
worker writes `outcome.json`, a small plain-JSON record that says only whether
the call returned or raised, and the qualified name of the exception type:

    {"nest_outcome": 1, "status": "raised", "error_type": "builtins.RuntimeError"}

The Controller reads it (bounded, through [`backends.slurm.SlurmBackend.read_outcome`])
once Slurm reports `COMPLETED`, and records an application failure separately
from a Slurm job failure. The exception message and traceback stay in
`output.pkl`, delivered to the caller unchanged; they are never stored by the
Controller.

Each attempt of a worker that speaks [`outcome.WORKER_PROTOCOL`] runs in its own
artifact directory, and both files are published atomically (written beside
their final name, then renamed), so a reader never sees a partial result or
another attempt's record. For such an attempt a missing record is never a
success. Only a worker that predates the record (protocol 1), admitted when the
operator allows legacy workers, completes without one.

This module needs only the standard library: `user_runner.py` loads it from the
bootstrap directory inside the user's own interpreter.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

OUTCOME_FILE = "outcome.json"
"""File name of the sidecar, in the execution directory next to `output.pkl`."""

OUTCOME_VERSION = 1

WORKER_PROTOCOL = 2
"""
Protocol of this worker: per-attempt artifact directories, atomic publication and
a mandatory outcome record. `scripts/worker.sbatch` declares it on a line
`# nest-worker-protocol: 2`, which the Controller reads before submitting.
"""

MAX_OUTCOME_BYTES = 1024
"""Largest sidecar the Controller reads; a larger one is invalid."""

RETURNED = "returned"
RAISED = "raised"

_MAX_ERROR_TYPE = 256
# A module path and qualified name, such as `torch.cuda.OutOfMemoryError` or
# `__main__.train.<locals>.Boom`.
# ASCII only, so a name of the maximum length always fits in `MAX_OUTCOME_BYTES`.
_ERROR_TYPE = re.compile(r"[A-Za-z_][\w.<>-]{0,255}", re.ASCII)


class InvalidOutcomeError(ValueError):
    """The sidecar exists but is not a valid outcome record."""


class OutcomeUnreadableError(InvalidOutcomeError):
    """The sidecar's path exists but cannot be read: not a regular file, or no access."""


@dataclass(frozen=True)
class RemoteOutcome:
    """
    What the worker reported about a call.

    Attributes:
        status (`str`): `returned` or `raised`.
        error_type (`str | None`): The exception's qualified type when `raised`.
    """

    status: str
    error_type: str | None = None

    @property
    def raised(self) -> bool:
        return self.status == RAISED


def error_type_of(exc: BaseException) -> str:
    """The qualified name of `exc`'s type, as the remote error record names it."""
    return f"{type(exc).__module__}.{type(exc).__qualname__}"


def _path(job_dir: Path | str) -> Path:
    return Path(job_dir) / OUTCOME_FILE


def clear(job_dir: Path | str) -> None:
    """Removes a sidecar left by an earlier attempt in the same directory."""
    try:
        _path(job_dir).unlink()
    except FileNotFoundError:
        pass


@contextmanager
def publish(path: Path | str) -> Generator[BinaryIO]:
    """
    Opens a file beside `path` for writing and renames it to `path` once the block
    completes, so readers see either the previous state or the whole new file.
    On an exception nothing is published.
    """
    final = Path(path)
    partial = final.with_name(f".{final.name}.{os.getpid()}.tmp")
    try:
        with partial.open("wb") as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(partial, final)
    finally:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass


def write(job_dir: Path | str, status: str, error_type: str | None = None) -> None:
    """
    Writes the sidecar atomically, after the result file it describes, so a
    reader sees either no sidecar or a complete one.
    """
    if status not in (RETURNED, RAISED):
        raise ValueError(f"Unknown outcome status {status!r}")
    record: dict[str, object] = {"nest_outcome": OUTCOME_VERSION, "status": status}
    if status == RAISED:
        name = (error_type or "")[:_MAX_ERROR_TYPE]
        record["error_type"] = name if _ERROR_TYPE.fullmatch(name) else "unknown"
    data = json.dumps(record, separators=(",", ":"))
    if len(data.encode()) > MAX_OUTCOME_BYTES:  # never larger than parse() accepts
        record["error_type"] = "unknown"
        data = json.dumps(record, separators=(",", ":"))
    with publish(_path(job_dir)) as f:
        f.write(data.encode())


class _DuplicateKeyError(Exception):
    pass


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    record = dict(pairs)
    if len(record) != len(pairs):
        raise _DuplicateKeyError
    return record


def parse(data: bytes) -> RemoteOutcome:
    """
    Parses a sidecar's bytes strictly.

    Raises:
        [`outcome.InvalidOutcomeError`]: If it is too large, not UTF-8 JSON, repeats
            a key, is of another version, or is not exactly one of the two record
            shapes. Its message is one of a fixed set and never quotes the record.
    """
    # Every message is a constant: nothing from the record, which the worker and
    # the user's code can write, reaches a stored error message or a log.
    if len(data) > MAX_OUTCOME_BYTES:
        raise InvalidOutcomeError(f"The outcome record exceeds {MAX_OUTCOME_BYTES} bytes")
    try:
        record = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    except _DuplicateKeyError:
        raise InvalidOutcomeError("The outcome record repeats a key") from None
    except UnicodeDecodeError, ValueError:
        raise InvalidOutcomeError("The outcome record is not valid JSON") from None
    if not isinstance(record, dict):
        raise InvalidOutcomeError("The outcome record is not a JSON object")
    version = record.get("nest_outcome")
    if type(version) is not int or version != OUTCOME_VERSION:
        raise InvalidOutcomeError("The outcome record has an unsupported version")
    status = record.get("status")
    if status == RETURNED and set(record) == {"nest_outcome", "status"}:
        return RemoteOutcome(RETURNED)
    error_type = record.get("error_type")
    if (
        status == RAISED
        and set(record) == {"nest_outcome", "status", "error_type"}
        and isinstance(error_type, str)
        and (error_type == "unknown" or _ERROR_TYPE.fullmatch(error_type))
    ):
        return RemoteOutcome(RAISED, error_type)
    raise InvalidOutcomeError("The outcome record has an unexpected shape")
