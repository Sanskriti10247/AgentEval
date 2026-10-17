"""API-key format, hashing and verification primitives (ADR-012 A).

A key is `ae_<8 hex prefix>_<43 base64url chars>` (256 bits of secret). Only the
SHA-256 of the secret is stored; the prefix is the (non-secret) lookup handle shown
by `list` and used by `revoke`. SHA-256 is enough because the secret is random, not
a password: there is nothing to dictionary-attack, and no per-request KDF cost.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum

KEY_PREFIX_TAG = "ae_"
PREFIX_LEN = 8
SECRET_BYTES = 32  # token_urlsafe(32) is 43 characters
_KEY = re.compile(r"^ae_(?P<prefix>[0-9a-f]{8})_(?P<secret>[A-Za-z0-9_-]{43})$")
_PREFIX = re.compile(r"^[0-9a-f]{8}$")


class Scope(StrEnum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


ALL_SCOPES = frozenset(Scope)


class AuthFailure(Exception):
    """A key was not accepted. `reason` is one of REASONS and is safe to show to
    the caller and to count as a metric label."""

    REASONS = ("missing", "invalid", "revoked", "expired")

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ParsedKey:
    prefix: str
    secret: str


@dataclass(frozen=True)
class GeneratedKey:
    plaintext: str
    prefix: str
    secret_sha256: bytes


def hash_secret(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("ascii")).digest()


def generate_key() -> GeneratedKey:
    prefix = secrets.token_hex(PREFIX_LEN // 2)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return GeneratedKey(f"{KEY_PREFIX_TAG}{prefix}_{secret}", prefix, hash_secret(secret))


def parse_key(plaintext: str) -> ParsedKey | None:
    """None when the text is not shaped like a key (the database is not consulted)."""
    m = _KEY.match(plaintext)
    return ParsedKey(m["prefix"], m["secret"]) if m else None


def is_valid_prefix(prefix: str) -> bool:
    return bool(_PREFIX.match(prefix))


def secrets_match(candidate_sha256: bytes, stored_sha256: bytes) -> bool:
    """Constant-time comparison of two digests."""
    return hmac.compare_digest(candidate_sha256, stored_sha256)


def parse_scopes(text: str) -> list[Scope]:
    """`read,write` -> [READ, WRITE] (deduplicated, in canonical order)."""
    wanted: set[Scope] = set()
    for part in text.split(","):
        name = part.strip().lower()
        if not name:
            continue
        try:
            wanted.add(Scope(name))
        except ValueError:
            raise ValueError(
                f"unknown scope {name!r}; choose from {', '.join(s.value for s in Scope)}"
            ) from None
    if not wanted:
        raise ValueError("at least one scope is required")
    return [s for s in Scope if s in wanted]
