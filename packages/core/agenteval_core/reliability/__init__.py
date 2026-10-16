"""Phase 5 reliability core: failure taxonomy, retry policy, backoff, the
run/attempt/evaluation transition tables and lease-fencing arithmetic.

Pure Python. Nothing here touches the database, Redis or Docker; the
repositories, the worker and migration 0004 build on these definitions.
"""
