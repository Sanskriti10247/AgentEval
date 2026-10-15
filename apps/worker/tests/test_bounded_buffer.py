import io
import tarfile

from agenteval_worker.sandbox.docker_sandbox import BoundedBuffer, build_tar


def test_under_limit_not_truncated() -> None:
    b = BoundedBuffer(10)
    b.append(b"hello")
    b.append(None)
    b.append(b"")
    assert (b.text(), b.truncated) == ("hello", False)


def test_exactly_at_limit_not_truncated() -> None:
    b = BoundedBuffer(5)
    b.append(b"hel")
    b.append(b"lo")
    assert (b.text(), b.truncated) == ("hello", False)


def test_over_limit_keeps_prefix_and_flags() -> None:
    b = BoundedBuffer(5)
    b.append(b"hel")
    b.append(b"lo world")
    b.append(b"more")
    assert (b.text(), b.truncated) == ("hello", True)


def test_cut_multibyte_char_and_invalid_utf8_are_replaced() -> None:
    b = BoundedBuffer(3)
    b.append("aé!".encode())  # a (1 byte) + é (2 bytes) fits exactly
    assert b.text() == "aé"
    c = BoundedBuffer(2)
    c.append("aé".encode())  # cuts é in half
    assert c.text() == "a�" and c.truncated
    d = BoundedBuffer(10)
    d.append(b"\xff\xfe")
    assert d.text() == "��"


def test_nul_bytes_replaced_for_postgres() -> None:
    b = BoundedBuffer(10)
    b.append(b"a\x00b")
    assert b.text() == "a�b"


def test_build_tar_roundtrip() -> None:
    data = build_tar({"main.py": "print(1)\n", "pkg/x.txt": "é"})
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = sorted(tar.getnames())
        assert names == ["main.py", "pkg/x.txt"]
        member = tar.extractfile("pkg/x.txt")
        assert member is not None and member.read().decode() == "é"
