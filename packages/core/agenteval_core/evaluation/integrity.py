"""Protected-path integrity: task files whose content must not change.

The baseline comes from the task's own (immutable) files, so it can't be
influenced by the agent. A protected file that is missing, replaced by a
symlink, or changed in content is tampering (score forced to 0).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agenteval_core.evaluation.results import FileEvidence, IntegrityResult
from agenteval_core.hashing import sha256_hex


def baseline(task_files: Mapping[str, str], protected_paths: Sequence[str]) -> dict[str, str]:
    """sha256 of each protected task file's original content (UTF-8, exactly
    as the task files are written into the agent's workspace)."""
    return {p: sha256_hex(task_files[p].encode()) for p in protected_paths}


def assess(
    expected: Mapping[str, str],
    evidence: Mapping[str, FileEvidence],
    *,
    normalized_paths: Sequence[str] = (),
) -> IntegrityResult:
    """Raises KeyError if evidence is missing for a protected path: that's a
    runner bug, reported as an evaluation error, never as clean integrity."""
    tampered: dict[str, str] = {}
    for path, digest in sorted(expected.items()):
        ev = evidence[path]
        if not ev.exists:
            tampered[path] = "missing"
        elif ev.is_symlink:
            tampered[path] = "replaced by a symlink"
        elif ev.sha256 != digest:
            tampered[path] = "content changed"
    return IntegrityResult(
        ok=not tampered, tampered=tampered, normalized_paths=sorted(normalized_paths)
    )
