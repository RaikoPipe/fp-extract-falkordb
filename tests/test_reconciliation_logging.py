"""Unit tests for FalkorDBBackend reconciliation logging + link helpers.

Uses a mocked ``_graph`` so no live FalkorDB connection is required.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.cypher_mapper import MergeMode, build_reconciliation_link_cypher
from knowledge.falkordb_backend import FalkorDBBackend
from knowledge.graph_models.factory_graph_model import (
    FactoryPlanningGraph,
    Resource,
)


class _FakeNode:
    def __init__(self, props: dict, labels: list[str] | None = None) -> None:
        self.properties = props
        self.labels = labels or []


class _FakeResult:
    def __init__(self, rows, stats=None) -> None:
        self.result_set = rows
        self.statistics = stats


class _FakeStats:
    def __init__(self, relationships_deleted: int = 0) -> None:
        self.relationships_deleted = relationships_deleted


def _make_backend(
    mode: MergeMode,
    tmp_path: Path,
) -> FalkorDBBackend:
    backend = FalkorDBBackend.__new__(FalkorDBBackend)
    backend._host = "localhost"
    backend._port = 6379
    backend._graph_name = "factory_planning"
    backend._db = MagicMock()
    backend._graph = MagicMock()
    backend._merge_mode = mode
    backend._reconciliations_log_path = tmp_path / "reconciliations.jsonl"
    backend._recon_enabled = False
    backend._recon_cosine_cutoff = 0.70
    backend._recon_confidence_threshold = 0.90
    backend._recon_top_k = 10
    backend._llm_model = None
    backend._embedding_model = None
    backend._api_base = None
    backend._embedding_api_base = None
    backend._embedding_api_key = None
    backend._embedding_dim = 1024
    return backend


# --------------------------------------------------------------------------
# build_reconciliation_link_cypher
# --------------------------------------------------------------------------
def test_build_reconciliation_link_cypher_structure():
    query, params = build_reconciliation_link_cypher(
        "Machine", "AKL-01",
        cosine=0.85, confidence=0.93,
        detected_at="2026-07-10T12:00:00+00:00",
        source="doc.md", chunk_index=2,
    )
    assert "MERGE (a:Resource {name: $plain_name})" in query
    assert "MERGE (b:Resource {name: $indexed_name})" in query
    assert "POSSIBLE_DUPLICATE_OF" in query
    assert "aliases" in query
    assert "canonical_name" in query
    assert params["plain_name"] == "Machine"
    assert params["indexed_name"] == "AKL-01"
    assert params["cosine"] == 0.85
    assert params["confidence"] == 0.93
    assert params["source"] == "doc.md"
    assert params["chunk_index"] == 2


# --------------------------------------------------------------------------
# write_extraction with reconciliation enabled
# --------------------------------------------------------------------------
def test_backend_write_extraction_recon_links_plain_name(tmp_path, monkeypatch):
    """In conflict mode + recon enabled, a new plain-name Resource is reconciled."""
    backend = _make_backend(MergeMode.CONFLICT, tmp_path)
    backend._recon_enabled = True

    # Fetch returns empty (new node) for the plain-name resource.
    fetch_results = iter([_FakeResult([])])

    def _dispatch(q, p=None):
        if q.startswith("MATCH"):
            return next(fetch_results)
        return _FakeResult([])

    backend._graph.query.side_effect = _dispatch

    # Mock embedding + reconciliation
    async def fake_embed(desc, **kwargs):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr("knowledge.falkordb_backend.embed_description", fake_embed)

    from knowledge.reconciliation import ReconciliationDecision

    async def fake_reconcile(b, entity, **kwargs):
        return ReconciliationDecision(
            linked=True,
            matched_name="AKL-01",
            cosine_similarity=0.85,
            llm_confidence=0.93,
            record={
                "new_name": "Machine",
                "matched_name": "AKL-01",
                "matched_label": "Resource",
                "cosine_similarity": 0.85,
                "llm_confidence": 0.93,
                "source": "doc.md",
                "chunk_index": 0,
                "detected_at": "2026-07-10T12:00:00+00:00",
            },
        )

    monkeypatch.setattr("knowledge.falkordb_backend.reconcile_new_node", fake_reconcile)

    # ensure_vector_index should not raise
    backend.ensure_vector_index = MagicMock()

    graph = FactoryPlanningGraph(
        resources=[
            Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine"),
        ],
    )

    statements, conflicts, reconciliations = asyncio.run(
        backend.write_extraction(graph, source="doc.md", chunk_index=0)
    )

    assert len(reconciliations) == 1
    assert reconciliations[0]["new_name"] == "Machine"
    assert reconciliations[0]["matched_name"] == "AKL-01"

    # jsonl log written
    log_path = tmp_path / "reconciliations.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["new_name"] == "Machine"
    assert entry["matched_name"] == "AKL-01"


def test_backend_write_extraction_recon_disabled_does_not_reconcile(tmp_path, monkeypatch):
    """When recon is disabled, plain-name resources are not reconciled."""
    backend = _make_backend(MergeMode.CONFLICT, tmp_path)
    backend._recon_enabled = False

    fetch_results = iter([_FakeResult([])])

    def _dispatch(q, p=None):
        if q.startswith("MATCH"):
            return next(fetch_results)
        return _FakeResult([])

    backend._graph.query.side_effect = _dispatch

    graph = FactoryPlanningGraph(
        resources=[
            Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine"),
        ],
    )

    statements, conflicts, reconciliations = asyncio.run(
        backend.write_extraction(graph, source="doc.md", chunk_index=0)
    )

    assert reconciliations == []
    log_path = tmp_path / "reconciliations.jsonl"
    assert not log_path.exists()


def test_backend_write_extraction_indexed_name_not_reconciled(tmp_path):
    """Indexed-name Resources (name_has_index=true) are never reconciled."""
    backend = _make_backend(MergeMode.CONFLICT, tmp_path)
    backend._recon_enabled = True

    fetch_results = iter([_FakeResult([])])

    def _dispatch(q, p=None):
        if q.startswith("MATCH"):
            return next(fetch_results)
        return _FakeResult([])

    backend._graph.query.side_effect = _dispatch

    graph = FactoryPlanningGraph(
        resources=[
            Resource(name="AKL-01", name_has_index=True, description="An AS/RS", resource_type="AS/RS"),
        ],
    )

    statements, conflicts, reconciliations = asyncio.run(
        backend.write_extraction(graph, source="doc.md", chunk_index=0)
    )

    assert reconciliations == []


# --------------------------------------------------------------------------
# get_reconciliations / clear_reconciliations
# --------------------------------------------------------------------------
def test_backend_get_reconciliations_returns_edges():
    backend = _make_backend(MergeMode.CONFLICT, Path("/tmp"))
    backend._graph.query.return_value = _FakeResult([
        ["Machine", "AKL-01", ["Resource"], 0.85, 0.93, "2026-07-10T12:00:00+00:00", "doc.md", 0],
    ])

    rows = backend.get_reconciliations()
    assert len(rows) == 1
    assert rows[0]["plain_name"] == "Machine"
    assert rows[0]["indexed_name"] == "AKL-01"
    assert rows[0]["cosine_similarity"] == 0.85
    assert rows[0]["llm_confidence"] == 0.93


def test_backend_get_reconciliations_label_filter():
    backend = _make_backend(MergeMode.CONFLICT, Path("/tmp"))
    backend._graph.query.return_value = _FakeResult([])

    backend.get_reconciliations(label="Resource")
    cypher = backend._graph.query.call_args.args[0]
    assert ":Resource" in cypher
    assert "POSSIBLE_DUPLICATE_OF" in cypher


def test_backend_clear_reconciliations_deletes_edge():
    backend = _make_backend(MergeMode.CONFLICT, Path("/tmp"))
    backend._graph.query.return_value = _FakeResult([], stats=_FakeStats(2))

    updated = backend.clear_reconciliations()
    cypher = backend._graph.query.call_args.args[0]
    assert "DELETE r" in cypher
    assert "POSSIBLE_DUPLICATE_OF" in cypher
    assert updated == 2


def test_backend_clear_reconciliations_name_filter():
    backend = _make_backend(MergeMode.CONFLICT, Path("/tmp"))
    backend._graph.query.return_value = _FakeResult([], stats=_FakeStats(1))

    backend.clear_reconciliations(label="Resource", plain_name="Machine")
    cypher, params = backend._graph.query.call_args.args
    assert "a.name = $plain_name" in cypher
    assert params == {"plain_name": "Machine"}