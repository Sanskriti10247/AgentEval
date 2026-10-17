"""LocalArtifactStore (ADR-008 A): atomic write, verified read, idempotent
delete, listing. A real directory (pytest tmp_path); no database."""

import hashlib
import os
import time
import uuid
from pathlib import Path

import pytest

from agenteval_core.artifacts import (
    ArtifactCorrupt,
    ArtifactMissing,
    LocalArtifactStore,
    artifact_key,
)
from agenteval_core.config import Settings

ATTEMPT = uuid.UUID("018f0000-0000-7000-8000-000000000001")
DATA = b"tar.gz bytes " * 100


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "vol")


async def test_put_names_the_file_by_attempt_kind_and_hash(store: LocalArtifactStore) -> None:
    sha = hashlib.sha256(DATA).hexdigest()
    stored = await store.put(ATTEMPT, "workspace", DATA)
    assert (
        stored.key == f"{ATTEMPT}/workspace-{sha}.tar.gz" == artifact_key(ATTEMPT, "workspace", sha)
    )
    assert (stored.size, stored.sha256, stored.existed) == (len(DATA), sha, False)
    assert (store.root / stored.key).read_bytes() == DATA
    # Bytes only ever went to a .tmp first: nothing but the final file is left.
    assert [k for k, _ in await store.list_files()] == [stored.key]


async def test_put_of_the_same_bytes_again_is_idempotent_and_reports_it(
    store: LocalArtifactStore,
) -> None:
    first = await store.put(ATTEMPT, "workspace", DATA)
    again = await store.put(ATTEMPT, "workspace", DATA)
    assert again.key == first.key and again.existed is True
    assert await store.read(again.key, again.size, again.sha256) == DATA


async def test_different_bytes_never_overwrite_in_place(store: LocalArtifactStore) -> None:
    a = await store.put(ATTEMPT, "workspace", b"one")
    b = await store.put(ATTEMPT, "workspace", b"two")
    assert a.key != b.key
    assert await store.read(a.key, a.size, a.sha256) == b"one"


async def test_a_failed_write_leaves_no_tmp_and_no_final(
    store: LocalArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        await store.put(ATTEMPT, "workspace", DATA)
    assert await store.list_files() == []


async def test_the_final_name_only_appears_after_the_file_is_synced(
    store: LocalArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order: write .tmp, fsync the file, rename, fsync the directory."""
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        events.append("dir" if os.path.isdir(f"/proc/self/fd/{fd}") else "file")
        real_fsync(fd)

    def replace(src: str | Path, dst: str | Path) -> None:
        assert str(src).endswith(".tmp") and not str(dst).endswith(".tmp")
        events.append("rename")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    await store.put(ATTEMPT, "workspace", DATA)
    assert events == ["file", "rename", "dir"]


async def test_read_verifies_size_and_sha256(store: LocalArtifactStore) -> None:
    stored = await store.put(ATTEMPT, "workspace", DATA)
    path = store.root / stored.key
    assert await store.read(stored.key, stored.size, stored.sha256) == DATA

    with pytest.raises(ArtifactCorrupt):  # the row says a different size
        await store.read(stored.key, stored.size + 1, stored.sha256)
    with pytest.raises(ArtifactCorrupt):  # the row says a different hash
        await store.read(stored.key, stored.size, "0" * 64)
    path.write_bytes(b"x" * len(DATA))  # same size, other bytes
    with pytest.raises(ArtifactCorrupt):
        await store.read(stored.key, stored.size, stored.sha256)
    path.write_bytes(b"short")
    with pytest.raises(ArtifactCorrupt):
        await store.read(stored.key, stored.size, stored.sha256)


async def test_read_of_a_missing_file_is_artifact_missing(store: LocalArtifactStore) -> None:
    with pytest.raises(ArtifactMissing):
        await store.read(artifact_key(ATTEMPT, "workspace", "a" * 64), 1, "a" * 64)


async def test_keys_cannot_escape_the_root(store: LocalArtifactStore, tmp_path: Path) -> None:
    (tmp_path / "secret").write_bytes(b"s")
    with pytest.raises(ArtifactMissing):
        await store.read("../secret", 1, hashlib.sha256(b"s").hexdigest())
    with pytest.raises(ValueError, match="escapes"):
        await store.delete("../secret")
    assert (tmp_path / "secret").exists()


async def test_delete_is_idempotent(store: LocalArtifactStore) -> None:
    stored = await store.put(ATTEMPT, "workspace", DATA)
    await store.delete(stored.key)
    await store.delete(stored.key)  # already gone: not an error
    await store.delete(artifact_key(uuid.uuid4(), "workspace", "b" * 64))
    assert await store.list_files() == []


async def test_list_files_includes_tmp_files_with_their_mtime(store: LocalArtifactStore) -> None:
    stored = await store.put(ATTEMPT, "workspace", DATA)
    leftover = store.root / str(ATTEMPT) / "workspace-dead.tar.gz.1234.tmp"
    leftover.write_bytes(b"partial")
    old = time.time() - 3600
    os.utime(leftover, (old, old))
    listed = dict(await store.list_files())
    assert set(listed) == {stored.key, f"{ATTEMPT}/{leftover.name}"}
    assert abs(listed[f"{ATTEMPT}/{leftover.name}"] - old) < 1
    assert listed[stored.key] > old + 3000


async def test_list_files_of_an_empty_store(store: LocalArtifactStore) -> None:
    assert await store.list_files() == []


def test_the_default_artifact_timings_satisfy_the_startup_check() -> None:
    s = Settings()
    s.validate_artifact_timing()
    assert (
        s.artifact_dir,
        s.artifact_insert_timeout_s,
        s.artifact_save_deadline_s,
        s.artifact_sweep_grace_s,
    ) == ("/var/lib/agenteval/artifacts", 10.0, 15.0, 120.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"artifact_sweep_grace_s": 25.0},  # not strictly above deadline + insert timeout
        {"artifact_sweep_grace_s": 10.0},
        {"artifact_save_deadline_s": 0.0},
        {"artifact_insert_timeout_s": -1.0},
    ],
)
def test_startup_refuses_an_unsafe_artifact_timing(overrides: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="artifact_"):
        Settings(**overrides).validate_artifact_timing()  # type: ignore[arg-type]


def test_artifact_settings_read_the_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTEVAL_ARTIFACT_DIR", "/data/a")
    monkeypatch.setenv("AGENTEVAL_ARTIFACT_SWEEP_GRACE_S", "300")
    s = Settings()
    assert (s.artifact_dir, s.artifact_sweep_grace_s) == ("/data/a", 300.0)
