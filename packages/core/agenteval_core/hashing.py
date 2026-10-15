"""Canonical JSON hashing, shared by every hashed config (agent versions,
evaluation specs, scoring configs) so equal configs always hash equally."""

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def config_hash_of(config: Any) -> str:
    return sha256_hex(canonical_json(config))
