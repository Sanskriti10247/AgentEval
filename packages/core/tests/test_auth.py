import pytest

from agenteval_core import auth
from agenteval_core.auth import Scope


def test_generated_keys_have_the_documented_shape() -> None:
    k = auth.generate_key()
    assert k.plaintext.startswith("ae_") and k.plaintext[11] == "_"
    assert len(k.prefix) == 8 and k.plaintext[3:11] == k.prefix
    parsed = auth.parse_key(k.plaintext)
    assert parsed is not None and parsed.prefix == k.prefix
    assert len(parsed.secret) == 43  # 32 random bytes, base64url without padding
    assert auth.hash_secret(parsed.secret) == k.secret_sha256 and len(k.secret_sha256) == 32
    assert k.secret_sha256.hex() not in k.plaintext


def test_keys_are_unique() -> None:
    assert len({auth.generate_key().plaintext for _ in range(50)}) == 50


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ae_",
        "sk-ant-api03-xxxx",
        "ae_0a1b2c3d",  # no secret
        "ae_0a1b2c3d_short",
        "ae_0A1B2C3D_" + "a" * 43,  # prefix is lower-case hex
        "ae_0a1b2c3_" + "a" * 43,  # prefix too short
        "ae_0a1b2c3d_" + "a" * 44,  # secret too long
        "ae_0a1b2c3d_" + "a" * 42 + "!",
        " ae_0a1b2c3d_" + "a" * 43,
    ],
)
def test_malformed_keys_do_not_parse(text: str) -> None:
    assert auth.parse_key(text) is None


def test_secrets_match_is_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = auth.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    d = auth.hash_secret("x")
    assert auth.secrets_match(d, d) and not auth.secrets_match(d, auth.hash_secret("y"))
    assert len(calls) == 2


def test_parse_scopes() -> None:
    assert auth.parse_scopes("write, read,read") == [Scope.READ, Scope.WRITE]
    assert auth.parse_scopes("admin") == [Scope.ADMIN]
    for bad in ("", " , ", "root", "read,delete"):
        with pytest.raises(ValueError):
            auth.parse_scopes(bad)
