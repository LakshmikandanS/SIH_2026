"""hnsw.iterative_scan -- docs/PLAN-M0.md task 5: "a filtered vector query that
excludes most of the corpus must still return k rows... It will matter at M2 and it
is invisible until it bites." Root AGENTS.md invariant 11 / ADR-0003 /
packages/platform/AGENTS.md: without it, a filtered HNSW query can silently return
FEWER than k rows once the filter (classification/ACL) excludes most of the index,
because HNSW's bounded graph search can exhaust its candidate list before finding k
matches that also satisfy the filter.

Needs the pgvector extension (migration 0004). Skipped only for that specific,
checked reason, via a real probe (does the `vector.control` file pg_config's own
sharedir would contain actually exist), never unconditionally. For a while this could
not run in the sandbox the repo was first built in, which had no pgvector; pgvector
0.8.1 was later built from source there, and this test now runs and passes in that
sandbox, against the same Postgres 16 the retrieval path uses -- and in Docker Compose
the `pgvector/pgvector:pg16` image provides the extension. The retrieval code relies
on what this proves: `citadel_knowledge.retrieval` filters by classification and ACL
in the same statement as the vector search, and still gets k rows back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from citadel_platform.migrations import apply_migration, discover_migrations, ensure_bootstrap
from pg_scratch import pg_scratch_db

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_MIGRATIONS_DIR = REPO_ROOT / "packages" / "platform" / "migrations"

_NOISE_ROWS = 2000  # confidential rows the classification filter must exclude
_TARGET_ROWS = 10  # public rows clustered near the query point -- the true k=10 answer


def _pgvector_available() -> bool:
    # pg_config itself may not exist at all -- not just fail -- on a machine with no
    # Postgres client tools on PATH (confirmed empirically on native Windows, which has
    # neither `postgresql-client` nor this sandbox's pre-installed one: subprocess.run
    # raises FileNotFoundError/OSError before there is a returncode to check, which
    # previously crashed test COLLECTION for the whole module rather than skipping this
    # one test cleanly). Same shape as pg_scratch.py's pg_reachable(), which already
    # handles this correctly -- this function is that one's sibling and had drifted
    # from its pattern.
    try:
        result = subprocess.run(["pg_config", "--sharedir"], capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return False
    if result.returncode != 0:
        return False
    return (Path(result.stdout.strip()) / "extension" / "vector.control").exists()


pytestmark = pytest.mark.skipif(
    not _pgvector_available(),
    reason="pgvector extension not installed on this machine -- see root AGENTS.md's sandbox note and migrations/0004_document_chunks.up.sql's header",
)


def _apply_through_document_chunks(env: dict[str, str]) -> None:
    ensure_bootstrap(env=env)
    for migration in discover_migrations(REAL_MIGRATIONS_DIR):
        apply_migration(migration, env=env)


@pytest.mark.integration
def test_filtered_knn_query_returns_exactly_k_rows_with_iterative_scan():
    with pg_scratch_db() as env:
        _apply_through_document_chunks(env)

        # A large "noise" corpus the classification filter must exclude: random
        # 768-dim vectors, uniformly distributed, classified confidential -- the
        # "most of the corpus" the PLAN-M0 wording refers to.
        noise_sql = (
            "INSERT INTO document_chunks (classification, content, embedding) "
            "SELECT 'confidential', 'noise ' || i, "
            "(SELECT array_agg(random()) FROM generate_series(1, 768))::vector "
            f"FROM generate_series(1, {_NOISE_ROWS}) AS i;"
        )
        # A small target set, tightly clustered around the query point [0.5, 0.5,
        # ...], classified public -- the true k=10 nearest neighbours once the
        # filter is applied. Distinct `content` values so the assertion below can
        # confirm these SPECIFIC rows came back, not just any 10 rows.
        target_sql = (
            "INSERT INTO document_chunks (classification, content, embedding) "
            "SELECT 'public', 'target ' || i, "
            "(SELECT array_agg(0.5 + (random() - 0.5) * 0.01) FROM generate_series(1, 768))::vector "
            f"FROM generate_series(1, {_TARGET_ROWS}) AS i;"
        )
        query_sql = (
            "SELECT content FROM document_chunks "
            "WHERE classification = 'public' "
            "ORDER BY embedding <-> (SELECT array_agg(0.5)::vector FROM generate_series(1, 768)) "
            f"LIMIT {_TARGET_ROWS};"
        )

        for sql in (noise_sql, target_sql):
            result = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1", "-f", "-"], input=sql, capture_output=True, text=True, env=env)
            assert result.returncode == 0, result.stderr

        result = subprocess.run(["psql", "-t", "-A", "-f", "-"], input=query_sql, capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stderr

        returned = {line for line in result.stdout.splitlines() if line}
        expected = {f"target {i}" for i in range(1, _TARGET_ROWS + 1)}

        assert len(returned) == _TARGET_ROWS, (
            f"expected exactly {_TARGET_ROWS} rows back despite {_NOISE_ROWS} excluded by the "
            f"classification filter -- got {len(returned)}. This is the failure mode "
            "hnsw.iterative_scan=strict_order exists to prevent (migration 0004): a plain filtered "
            "HNSW search can give up before finding k matches once the filter excludes most "
            "of the index."
        )
        assert returned == expected, f"got the wrong {_TARGET_ROWS} rows: {returned - expected}"
