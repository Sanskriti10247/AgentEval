"""Pure helpers in sandbox/session.py (no Docker)."""

import io
import struct
import tarfile

import pytest

from agenteval_worker.sandbox.session import _seconds, build_tar, demux_stream


def frame(stream: int, payload: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload


def test_demux_splits_stdout_and_stderr_frames() -> None:
    raw = frame(1, b"out1 ") + frame(2, b"err1") + frame(1, b"out2")
    assert demux_stream(raw) == (b"out1 out2", b"err1")


def test_demux_ignores_a_trailing_partial_header() -> None:
    assert demux_stream(frame(2, b"e") + b"\x01\x00") == (b"", b"e")


def mtimes(data: bytes) -> list[int]:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return [int(m.mtime) for m in tar.getmembers()]


def test_build_tar_keeps_the_s1_mtime_by_default() -> None:
    assert mtimes(build_tar({"a.py": "x"})) == [0]


def test_build_tar_uses_the_given_mtime() -> None:
    assert mtimes(build_tar({"a.py": "x", "b/c.py": "y"}, mtime=1_790_000_000.9)) == [
        1_790_000_000,
        1_790_000_000,
    ]


@pytest.mark.parametrize(
    ("value", "text"), [(2, "2"), (2.5, "2.5"), (0.125, "0.125"), (59.9999, "60"), (60.0, "60")]
)
def test_seconds_formatting_for_coreutils_timeout(value: float, text: str) -> None:
    assert _seconds(value) == text


def test_a_worker_refuses_an_image_that_is_not_on_its_allowlist() -> None:
    """Security review F-W1: the worker checks the allowlist again before creating a
    container, with a reason that is never retried."""
    from agenteval_core.reliability.taxonomy import FailureReason, is_retryable
    from agenteval_worker.sandbox.session import ContainerSpec, SandboxError, check_image_allowed

    spec = ContainerSpec("evil:latest", 10, 64, 1.0, image_allowlist=frozenset({"ok:1"}))
    with pytest.raises(SandboxError) as err:
        check_image_allowed(spec)
    assert err.value.reason is FailureReason.SANDBOX_IMAGE_NOT_FOUND
    assert not is_retryable(err.value.reason)
    check_image_allowed(ContainerSpec("ok:1", 10, 64, 1.0, image_allowlist=frozenset({"ok:1"})))
    check_image_allowed(ContainerSpec("anything", 10, 64, 1.0))  # no allowlist: no check
