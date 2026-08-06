"""Tests for the ``/api/graph-snapshot`` endpoint helper.

Covers :func:`falkordb_harness.auth.build_graph_snapshot`, the module-level
serializer the endpoint closure delegates to. The endpoint itself is a
thin auth + ``get_last_graph`` shim, so the shape / truncation / error
behaviour is fully exercised here without invoking ``register_routes``
(which mutates Chainlit's global router).

FalkorDB is mocked via ``unittest.mock.patch`` on ``FalkorDBBackend`` — no
live graph connection is made (consistent with the repo's mock-only test
policy in ``AGENTS.md``).
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _make_node(name, labels, **props):
    """Build a node dict shaped like ``FalkorDBBackend.get_all_nodes`` output."""
    n = {"name": name, "_labels": labels}
    n.update(props)
    return n


def _make_edge(src, tgt, rel):
    """Build an edge tuple shaped like ``FalkorDBBackend.get_all_edges`` output."""
    return (src, tgt, rel, {})


# ---------------------------------------------------------------------------
# Empty / unset graph
# ---------------------------------------------------------------------------

def test_empty_graph_name_returns_empty_snapshot():
    from falkordb_harness.auth import build_graph_snapshot

    payload = build_graph_snapshot(None)
    assert payload["graph"] == ""
    assert payload["nodes"] == []
    assert payload["edges"] == []
    assert payload["stats"]["node_count"] == 0
    assert payload["stats"]["edge_count"] == 0
    assert payload["stats"]["truncated"] is False


def test_blank_graph_name_returns_empty_snapshot():
    from falkordb_harness.auth import build_graph_snapshot

    payload = build_graph_snapshot("")
    assert payload["graph"] == ""
    assert payload["nodes"] == []


# ---------------------------------------------------------------------------
# Happy path: nodes + edges serialized with label / rel-type counts
# ---------------------------------------------------------------------------

def test_serializes_nodes_and_edges_with_counts():
    from falkordb_harness.auth import build_graph_snapshot

    nodes = [
        _make_node("Order_42", ["Resource"], qty=10),
        _make_node("Line_7", ["Resource", "Line"]),
        _make_node("Machine_A", ["Machine"]),
    ]
    edges = [
        _make_edge("Order_42", "Line_7", "USES"),
        _make_edge("Line_7", "Machine_A", "CONTAINS"),
    ]

    with patch("knowledge.falkordb_backend.FalkorDBBackend") as BackendMock:
        instance = BackendMock.return_value
        instance.get_all_nodes.return_value = nodes
        instance.get_all_edges.return_value = edges

        payload = build_graph_snapshot("factory_planning")

    assert payload["graph"] == "factory_planning"
    # Nodes: scalar props kept, _labels preserved, no embedding/conflicts keys.
    assert len(payload["nodes"]) == 3
    names = [n["name"] for n in payload["nodes"]]
    assert names == ["Order_42", "Line_7", "Machine_A"]
    order = next(n for n in payload["nodes"] if n["name"] == "Order_42")
    assert order["qty"] == 10
    assert order["_labels"] == ["Resource"]
    assert "embedding" not in order
    assert "conflicts" not in order

    # Edges: filtered to surviving nodes; rel_type counted.
    assert len(payload["edges"]) == 2
    assert {"source": "Order_42", "target": "Line_7", "type": "USES"} in payload["edges"]
    assert {"source": "Line_7", "target": "Machine_A", "type": "CONTAINS"} in payload["edges"]

    # Stats reflect the full graph (pre-truncation counts).
    assert payload["stats"]["node_count"] == 3
    assert payload["stats"]["edge_count"] == 2
    assert payload["stats"]["label_counts"] == {
        "Resource": 2,
        "Line": 1,
        "Machine": 1,
    }
    assert payload["stats"]["rel_type_counts"] == {"USES": 1, "CONTAINS": 1}
    assert payload["stats"]["truncated"] is False


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

def test_truncates_nodes_and_drops_orphan_edges():
    from falkordb_harness.auth import build_graph_snapshot

    # 3 nodes, cap at 2 -> the 3rd node is dropped.
    nodes = [
        _make_node("A", ["Resource"]),
        _make_node("B", ["Resource"]),
        _make_node("C", ["Resource"]),
    ]
    # Edge A->C references a dropped node -> orphan, must be filtered out.
    # Edge A->B references two surviving nodes -> kept.
    edges = [
        _make_edge("A", "B", "LINKS"),
        _make_edge("A", "C", "LINKS"),
    ]

    with patch("knowledge.falkordb_backend.FalkorDBBackend") as BackendMock:
        instance = BackendMock.return_value
        instance.get_all_nodes.return_value = nodes
        instance.get_all_edges.return_value = edges

        payload = build_graph_snapshot("g", max_nodes=2)

    assert payload["stats"]["node_count"] == 3  # pre-truncation total
    assert payload["stats"]["truncated"] is True
    assert len(payload["nodes"]) == 2
    surviving = {n["name"] for n in payload["nodes"]}
    assert surviving == {"A", "B"}
    # Orphan edge A->C dropped; A->B kept.
    assert len(payload["edges"]) == 1
    assert payload["edges"][0] == {"source": "A", "target": "B", "type": "LINKS"}
    # Edge count in stats reflects the FULL edge set (pre-filter), so the
    # client can show "X edges in graph, Y rendered".
    assert payload["stats"]["edge_count"] == 2


# ---------------------------------------------------------------------------
# Property slimming
# ---------------------------------------------------------------------------

def test_strips_embedding_conflicts_and_nested_values():
    from falkordb_harness.auth import build_graph_snapshot

    # Node carries an embedding (large vector), a conflicts JSON list, and a
    # nested dict — all should be stripped from the serialized form.
    nodes = [
        {
            "name": "R1",
            "_labels": ["Resource"],
            "embedding": "[0.1, 0.2, 0.3]",
            "conflicts": ['{"id":"x"}'],
            "nested": {"k": "v"},
            "scalar": 42,
            "text": "hello",
        },
    ]
    edges = []

    with patch("knowledge.falkordb_backend.FalkorDBBackend") as BackendMock:
        instance = BackendMock.return_value
        instance.get_all_nodes.return_value = nodes
        instance.get_all_edges.return_value = edges

        payload = build_graph_snapshot("g")

    assert len(payload["nodes"]) == 1
    n = payload["nodes"][0]
    assert n["name"] == "R1"
    assert n["scalar"] == 42
    assert n["text"] == "hello"
    assert n["_labels"] == ["Resource"]
    assert "embedding" not in n
    assert "conflicts" not in n
    assert "nested" not in n


def test_unlabeled_node_counted_under_unlabeled_bucket():
    from falkordb_harness.auth import build_graph_snapshot

    nodes = [
        {"name": "N1", "_labels": []},
        {"name": "N2", "_labels": ["Resource"]},
    ]
    edges = []

    with patch("knowledge.falkordb_backend.FalkorDBBackend") as BackendMock:
        instance = BackendMock.return_value
        instance.get_all_nodes.return_value = nodes
        instance.get_all_edges.return_value = edges

        payload = build_graph_snapshot("g")

    assert payload["stats"]["label_counts"] == {"(unlabeled)": 1, "Resource": 1}


# ---------------------------------------------------------------------------
# FalkorDB failure degrades to empty snapshot
# ---------------------------------------------------------------------------

def test_backend_failure_returns_empty_snapshot_with_error_flag():
    from falkordb_harness.auth import build_graph_snapshot

    with patch("knowledge.falkordb_backend.FalkorDBBackend") as BackendMock:
        # Construction itself fails (FalkorDB unreachable).
        BackendMock.side_effect = RuntimeError("connection refused")

        payload = build_graph_snapshot("factory_planning")

    assert payload["graph"] == "factory_planning"
    assert payload["nodes"] == []
    assert payload["edges"] == []
    assert payload["stats"]["node_count"] == 0
    assert payload["stats"]["edge_count"] == 0
    assert payload["stats"]["truncated"] is False
    assert payload["stats"]["error"] == "graph-unavailable"


def test_backend_get_all_nodes_failure_returns_empty_snapshot_with_error_flag():
    from falkordb_harness.auth import build_graph_snapshot

    with patch("knowledge.falkordb_backend.FalkorDBBackend") as BackendMock:
        instance = BackendMock.return_value
        instance.get_all_nodes.side_effect = RuntimeError("query failed")

        payload = build_graph_snapshot("factory_planning")

    assert payload["graph"] == "factory_planning"
    assert payload["nodes"] == []
    assert payload["stats"]["error"] == "graph-unavailable"


# ---------------------------------------------------------------------------
# Backend is bound to the user's graph name
# ---------------------------------------------------------------------------

def test_backend_constructed_with_user_graph_name():
    """The one-shot backend must be built with the user's last graph, not
    the module-level default — this is the whole reason the endpoint doesn't
    just call ``get_backend()``."""
    from falkordb_harness.auth import build_graph_snapshot

    with patch("knowledge.falkordb_backend.FalkorDBBackend") as BackendMock:
        instance = BackendMock.return_value
        instance.get_all_nodes.return_value = []
        instance.get_all_edges.return_value = []

        build_graph_snapshot("orders_graph")

    BackendMock.assert_called_once_with(graph_name="orders_graph")