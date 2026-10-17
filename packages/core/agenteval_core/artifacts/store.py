"""The artifact store (ADR-008 A): where snapshot bytes live.

Postgres keeps the authoritative metadata row (`attempt_artifacts`); the store
only holds bytes. A file without a row does not exist as far as the system is
concerned, and a row whose file is missing or has a different hash is a
platform error that is never graded. The store never talks to the database.

    key   <attempt_id>/<kind>-<sha256>.tar.gz
          The content hash is part of the name, so a retry can never overwrite
          different content in place.

    put   write `<key>.<random>.tmp`, fsync the file, rename into place, fsync
          the directory. A reader can never see a partial file: bytes only ever
          go to a `.tmp`, and the final name appears after the fsync. A crash
          leaves at most a `.tmp`, which the sweeper removes once it is older
          than the grace period. (The `.tmp` carries a random suffix so two
          writers of the same key cannot truncate each other's file.)
    read  verifies size and SHA-256; a mismatch is `ArtifactCorrupt`, a
          missing file `ArtifactMissing`.
    delete is idempotent.

The interface is async because the v1 implementation does blocking file I/O,
which `LocalArtifactStore` pushes onto a thread so the worker's event loop
(and with it the lease keeper's pings) never stalls on a slow disk. An object
store can implement the same protocol later without changing any transaction.
"""

import asyncio
import hashlib
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

TMP_SUFFIX = ".tmp"
_READ_CHUNK = 1024 * 1024


class ArtifactMissing(Exception):
    """The file a row points to does not exist."""


class ArtifactCorrupt(Exception):
    """The file exists but its size or SHA-256 differs from the row's."""


@dataclass(frozen=True)
class StoredArtifact:
    key: str
    size: int
    sha256: str
    # time.monotonic() right after the rename: the save deadline (ADR-008 E) is
    # measured from here. CLOCK_MONOTONIC is system-wide, so threads agree.
    renamed_at: float
    # The final name already existed (an earlier save of the same bytes). A
    # writer that fails afterwards must not delete it: that earlier save's row
    # may reference it. The sweeper handles it if nothing does.
    existed: bool = False


def artifact_key(attempt_id: uuid.UUID, kind: str, sha256: str) -> str:
    return f"{attempt_id}/{kind}-{sha256}.tar.gz"


class ArtifactStore(Protocol):
    async def put(self, attempt_id: uuid.UUID, kind: str, data: bytes) -> StoredArtifact: ...

    async def read(self, key: str, size: int, sha256: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def list_files(self) -> list[tuple[str, float]]:
        """Every file, `.tmp` included, as (key, mtime in epoch seconds)."""
        ...


class LocalArtifactStore:
    """v1: a directory on a named volume mounted into every worker."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    async def put(self, attempt_id: uuid.UUID, kind: str, data: bytes) -> StoredArtifact:
        return await asyncio.to_thread(self._put, attempt_id, kind, data)

    async def read(self, key: str, size: int, sha256: str) -> bytes:
        return await asyncio.to_thread(self._read, key, size, sha256)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)

    async def list_files(self) -> list[tuple[str, float]]:
        return await asyncio.to_thread(self._list_files)

    # -- blocking implementations (run on a thread) -----------------------------------------

    def _path(self, key: str) -> Path:
        """The key is ours (a row or a listing), but never trust it to stay
        inside the root."""
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError(f"artifact key escapes the store: {key!r}")
        return path

    def _put(self, attempt_id: uuid.UUID, kind: str, data: bytes) -> StoredArtifact:
        sha = hashlib.sha256(data).hexdigest()
        key = artifact_key(attempt_id, kind, sha)
        final = self._path(key)
        tmp = final.with_name(f"{final.name}.{secrets.token_hex(4)}{TMP_SUFFIX}")
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            existed = final.exists()
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        renamed_at = time.monotonic()
        _fsync_dir(final.parent)
        return StoredArtifact(
            key=key, size=len(data), sha256=sha, renamed_at=renamed_at, existed=existed
        )

    def _read(self, key: str, size: int, sha256: str) -> bytes:
        try:
            path = self._path(key)
            with path.open("rb") as f:
                actual = os.fstat(f.fileno()).st_size
                if actual != size:  # never slurp a file of the wrong size
                    raise ArtifactCorrupt(f"size {actual} != {size}")
                digest = hashlib.sha256()
                chunks: list[bytes] = []
                while chunk := f.read(_READ_CHUNK):
                    digest.update(chunk)
                    chunks.append(chunk)
        except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
            raise ArtifactMissing(key) from exc
        if digest.hexdigest() != sha256:
            raise ArtifactCorrupt("sha256 mismatch")
        data = b"".join(chunks)
        if len(data) != size:  # grew while reading
            raise ArtifactCorrupt(f"size {len(data)} != {size}")
        return data

    def _delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def _list_files(self) -> list[tuple[str, float]]:
        found: list[tuple[str, float]] = []
        root = self._root.resolve()
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except FileNotFoundError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        found.append(
                            (
                                Path(entry.path).relative_to(root).as_posix(),
                                entry.stat(follow_symlinks=False).st_mtime,
                            )
                        )
                except FileNotFoundError:
                    continue  # raced with a delete or a rename
        return found


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
