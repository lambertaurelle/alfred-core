"""Atomic POSIX JSON and JSONL persistence repositories using .lock + os.replace."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def atomic_write_text(target_path: Path, content: str) -> None:
    """Atomically write UTF-8 text to target_path under an exclusive adjacent .lock file.

    Args:
        target_path: Destination file path.
        content: Full UTF-8 text payload to persist.
    """
    resolved = target_path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{resolved}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            _write_and_replace(resolved, content)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_and_replace(resolved_target: Path, content: str) -> None:
    """Write content to a sibling temporary file, fsync, and os.replace atomically."""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(resolved_target.parent),
        prefix=f".{resolved_target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, resolved_target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


class JsonStateRepository(Generic[T]):
    """Generic atomic JSON repository backed by Pydantic validation and POSIX flock."""

    def __init__(self, file_path: Path, model_cls: type[T]) -> None:
        """Initialize the JSON state repository for a target file and Pydantic model.

        Args:
            file_path: Path to the persisted JSON file.
            model_cls: Pydantic model class representing the root state document.
        """
        self._path = file_path.resolve()
        self._lock_path = Path(f"{self._path}.lock")
        self._model_cls = model_cls
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def file_path(self) -> Path:
        """Return resolved path of the JSON file."""
        return self._path

    def exists(self) -> bool:
        """Check whether the underlying JSON file already exists on disk.

        Returns:
            True if the JSON file exists, False otherwise.
        """
        return self._path.exists()

    def load(self, default_factory: Callable[[], T] | None = None) -> T:
        """Load and validate the JSON state under lock, or construct a default entity.

        Args:
            default_factory: Optional callable producing a default entity when absent.

        Returns:
            Validated Pydantic state model instance.
        """
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                return self._read_unlocked(default_factory)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def save(self, entity: T) -> None:
        """Atomically persist a Pydantic entity to disk under an exclusive file lock.

        Args:
            entity: Validated Pydantic model instance to serialize and persist.
        """
        payload = json.dumps(entity.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                _write_and_replace(self._path, payload)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def update(
        self,
        mutator: Callable[[T], T],
        default_factory: Callable[[], T] | None = None,
    ) -> T:
        """Execute an atomic read-modify-write transaction under a single exclusive lock.

        Args:
            mutator: Pure transformation mapping current state to updated state.
            default_factory: Optional default builder if state file does not yet exist.

        Returns:
            Updated and persisted Pydantic state model instance.
        """
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                current = self._read_unlocked(default_factory)
                updated = mutator(current)
                payload = (
                    json.dumps(updated.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
                )
                _write_and_replace(self._path, payload)
                return updated
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self, default_factory: Callable[[], T] | None = None) -> T:
        """Read and validate JSON when caller already holds the file lock."""
        if not self._path.exists():
            if default_factory is not None:
                return default_factory()
            return self._model_cls()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return self._model_cls.model_validate(raw)


class JsonlEventLog(Generic[T]):
    """Append-only atomic JSONL audit log backed by POSIX flock and fsync."""

    def __init__(self, file_path: Path, model_cls: type[T]) -> None:
        """Initialize the append-only JSONL event log.

        Args:
            file_path: Path to the .jsonl log file.
            model_cls: Pydantic model class for each JSONL record.
        """
        self._path = file_path.resolve()
        self._lock_path = Path(f"{self._path}.lock")
        self._model_cls = model_cls
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def file_path(self) -> Path:
        """Return resolved path of the JSONL file."""
        return self._path

    def append(self, event: T) -> None:
        """Append a single validated event line under exclusive lock with fsync.

        Args:
            event: Pydantic event model instance to append.
        """
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n"
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                with self._path.open("a", encoding="utf-8") as log_file:
                    log_file.write(line)
                    log_file.flush()
                    os.fsync(log_file.fileno())
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def read_all(self) -> list[T]:
        """Read and validate all JSONL records in chronological order.

        Returns:
            List of validated Pydantic event records.
        """
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if not self._path.exists():
                    return []
                records: list[T] = []
                for line in self._path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if stripped:
                        records.append(self._model_cls.model_validate(json.loads(stripped)))
                return records
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
