"""The demo snapshot (infrastructure/demo/agenteval-demo.sql.gz) that the read-only image
restores. The image runs no migrations, so the snapshot must be at the current head, and it
must stay free of API keys. Regenerate it with scripts/make_demo_snapshot.sh."""

import gzip
import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "infrastructure/demo/agenteval-demo.sql.gz"


def _sql() -> str:
    return gzip.decompress(SNAPSHOT.read_bytes()).decode()


def _copy_rows(sql: str, table: str) -> list[str]:
    match = re.search(rf"^COPY public\.{table} \(.*?\) FROM stdin;\n(.*?)^\\\.$", sql, re.M | re.S)
    return match.group(1).splitlines() if match else []


def test_the_snapshot_is_at_the_migration_head() -> None:
    cfg = Config(str(ROOT / "packages/core/alembic.ini"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert _copy_rows(_sql(), "alembic_version") == [head], (
        "the demo snapshot is behind the migrations; regenerate it (docs/deployment.md)"
    )


def test_the_snapshot_holds_no_api_keys() -> None:
    sql = _sql()
    assert "CREATE TABLE public.api_keys" in sql
    assert _copy_rows(sql, "api_keys") == []
    assert not re.search(r"ae_[0-9a-f]{8}_[A-Za-z0-9_-]{20,}", sql)
