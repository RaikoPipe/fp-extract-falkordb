"""Unit tests for FalkorDBBackend conflict logging.

Conflicts are recorded only in the graph node's ``conflicts`` list property
(no JSONL registry). Uses a mocked ``_graph`` so no live FalkorDB connection
is required.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.cypher_mapper import MergeMode
from knowledge.falkordb_backend import FalkorDBBackend
from knowledge.graph_models.factory_graph_model import (
    FactoryPlanningGraph,
    Resource,
)


# --------------------------------------------------------------------------
# Fakes mimicking FalkorDB Node / result_set shapes.
# --------------------------------------------------------------------------
class _FakeNode:
    def __init__(self, props: dict, labels: list[str] | None = None) -> None:
        self.properties = props
        self.labels = labels or []


class _FakeResult:
    def __init__(self, rows, stats=None) -> None:
        self.result_set = rows
        self.statistics = stats


def _make_backend(mode: MergeMode) -> FalkorDBBackend:
    """Construct a backend with a mocked graph."""
    backend = FalkorDBBackend.__new__(FalkorDBBackend)
    backend._host = "localhost"
    backend._port = 6379
    backend._graph_name = "factory_planning"
    backend._db = MagicMock()
    backend._graph = MagicMock()
    backend._merge_mode = mode
    backend._reconciliations_log_path = Path("/tmp/reconciliations.jsonl")
    backend._recon_enabled = False
    backend._recon_cosine_cutoff = 0.70
    backend._recon_confidence_threshold = 0.90
    backend._recon_top_k = 10
    backend._llm_model = None
    backend._embedding_model = None
    backend._api_base = None
    backend._embedding_dim = 1024
    return backend


# --------------------------------------------------------------------------
# write_extraction: conflict mode
# --------------------------------------------------------------------------
def test_backend_write_extraction_conflict_mode_returns_conflict():
    backend = _make_backend(MergeMode.CONFLICT)

    # Fetch results per entity (MATCH queries). Write queries (MERGE) get an
    # empty result — the side_effect discriminates by query prefix.
    fetch_results = iter([
        _FakeResult([]),  # M-new: no existing node
        _FakeResult([[_FakeNode({"name": "M-old", "name_has_index": True, "description": "Old", "resource_type": "machine", "capacity": 500})]]),
    ])

    def _dispatch(q, p=None):
        if q.startswith("MATCH"):
            return next(fetch_results)
        return _FakeResult([])  # MERGE/SET write

    backend._graph.query.side_effect = _dispatch

    graph = FactoryPlanningGraph(
        resources=[
            Resource(name="M-new", name_has_index=True, description="New machine", resource_type="machine", capacity=100),
            Resource(name="M-old", name_has_index=True, description="Old machine", resource_type="machine", capacity=600),
        ],
    )

    statements, conflicts, reconciliations = asyncio.run(
        backend.write_extraction(graph, source="doc.docx", chunk_index=2)
    )

    # M-new: 1 write; M-old: 1 write; no relationships. Total 2.
    assert statements >= 2
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["property"] == "capacity"
    assert c["existing_value"] == 500
    assert c["incoming_value"] == 600
    assert c["source"] == "doc.docx"
    assert c["chunk_index"] == 2
    # New: stable id + resolved flag.
    assert c["id"] == f"capacity:{c['detected_at']}"
    assert c["resolved"] is False
    assert reconciliations == []


def test_backend_write_extraction_no_conflicts_returns_empty(tmp_path):
    backend = _make_backend(MergeMode.CONFLICT)

    # Node exists with identical values -> no conflict.
    fetch_results = iter([
        _FakeResult([[_FakeNode({"name": "M-1", "name_has_index": True, "description": "M1", "resource_type": "machine", "capacity": 500})]]),
    ])

    def _dispatch(q, p=None):
        if q.startswith("MATCH"):
            return next(fetch_results)
        return _FakeResult([])  # MERGE/SET write

    backend._graph.query.side_effect = _dispatch

    graph = FactoryPlanningGraph(
        resources=[Resource(name="M-1", name_has_index=True, description="M1", resource_type="machine", capacity=500)],
    )

    statements, conflicts, reconciliations = asyncio.run(
        backend.write_extraction(graph, source="d.docx", chunk_index=0)
    )

    assert conflicts == []
    assert reconciliations == []


def test_backend_write_extraction_overwrite_mode_returns_no_conflicts(tmp_path):
    backend = _make_backend(MergeMode.OVERWRITE)

    # Overwrite mode calls extraction_to_cypher -> one MERGE per entity.
    backend._graph.query.return_value = _FakeResult([])

    graph = FactoryPlanningGraph(
        resources=[Resource(name="M-1", name_has_index=True, description="M1", resource_type="machine", capacity=500)],
    )

    statements, conflicts, reconciliations = asyncio.run(
        backend.write_extraction(graph)
    )

    assert isinstance(statements, int)
    assert statements >= 1
    assert conflicts == []
    assert reconciliations == []


# --------------------------------------------------------------------------
# merge_mode property + constructor wiring
# --------------------------------------------------------------------------
def test_backend_merge_mode_property():
    backend = _make_backend(MergeMode.CONFLICT)
    assert backend.merge_mode is MergeMode.CONFLICT
    assert backend.merge_mode.value == "conflict"


def test_backend_constructor_resolves_merge_mode_from_string():
    backend = FalkorDBBackend.__new__(FalkorDBBackend)
    backend._host = "localhost"
    backend._port = 6379
    backend._graph_name = "test"
    backend._db = MagicMock()
    backend._graph = MagicMock()
    backend._merge_mode = MergeMode("conflict")
    assert backend.merge_mode is MergeMode.CONFLICT