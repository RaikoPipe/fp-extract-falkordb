"""Unit tests for the Cypher mapper."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.cypher_mapper import (
    MergeMode,
    build_conflict_merge,
    extraction_to_cypher,
    extraction_to_cypher_with_mode,
    model_to_cypher_fetch,
    model_to_cypher_merge,
)
from knowledge.graph_models.factory_graph_model import (
    FactoryPlanningGraph,
    Resource,
    Product,
    ControlStrategy,
    TransportSegment,
)


def test_model_to_cypher_merge_basic():
    r = Resource(name="AKL-01", resource_type="AS/RS", capacity=500)
    query, params = model_to_cypher_merge(r, "Resource")

    assert "MERGE (n:Resource {name: $name})" in query
    assert "SET" in query
    assert params["name"] == "AKL-01"
    assert params["p_resource_type"] == "AS/RS"
    assert params["p_capacity"] == 500


def test_model_to_cypher_merge_omits_none_fields():
    r = Resource(name="M-100", resource_type="machine")
    query, params = model_to_cypher_merge(r, "Resource")

    assert "p_capacity" not in params
    assert "p_mtbf_s" not in params


def test_model_to_cypher_merge_skips_reference_fields():
    r = Resource(
        name="M-100",
        resource_type="machine",
        shift_model="3-shift",
        assigned_products=["Product-A"],
    )
    query, params = model_to_cypher_merge(r, "Resource")

    # shift_model and assigned_products are reference fields, not stored as SET props
    assert "p_shift_model" not in params
    assert "p_assigned_products" not in params


def test_extraction_to_cypher_nodes_and_relationships():
    graph = FactoryPlanningGraph(
        resources=[
            Resource(name="M-100", resource_type="machine", shift_model="Day-Shift"),
        ],
        control_strategies=[
            ControlStrategy(
                name="FIFO-Dispatch",
                strategy_type="dispatching",
                description="First in first out",
                affected_resources=["M-100"],
            ),
        ],
    )

    statements = extraction_to_cypher(graph)

    # At least node MERGE for Resource, ControlStrategy + relationship MERGEs
    assert len(statements) >= 3

    queries = [q for q, _ in statements]
    # Node merges
    assert any("MERGE (n:Resource {name: $name})" in q for q in queries)
    assert any("MERGE (n:ControlStrategy {name: $name})" in q for q in queries)
    # Relationship: Resource -> ShiftModel
    assert any("HAS_SHIFT_MODEL" in q for q in queries)
    # Relationship: ControlStrategy -> Resource
    assert any("GOVERNS" in q for q in queries)


def test_extraction_to_cypher_transport_segment_from_to():
    graph = FactoryPlanningGraph(
        transport_segments=[
            TransportSegment(
                name="Seg-1",
                from_node="Station-A",
                to_node="Station-B",
                length_m=50.0,
            ),
        ],
    )

    statements = extraction_to_cypher(graph)
    queries = [q for q, _ in statements]

    assert any(":FROM" in q and "Station-A" in str(p) for q, p in statements)
    assert any(":TO" in q and "Station-B" in str(p) for q, p in statements)


def test_extraction_to_cypher_empty_graph():
    graph = FactoryPlanningGraph()
    statements = extraction_to_cypher(graph)
    assert statements == []


# --------------------------------------------------------------------------
# Merge mode + conflict-detection helpers
# --------------------------------------------------------------------------
def test_merge_mode_values():
    assert MergeMode.OVERWRITE.value == "overwrite"
    assert MergeMode.CONFLICT.value == "conflict"


def test_model_to_cypher_merge_overwrite_mode_unchanged():
    # The default (overwrite) path must produce the same output as before.
    r = Resource(name="AKL-01", resource_type="AS/RS", capacity=500)
    query, params = model_to_cypher_merge(r, "Resource")
    assert "MERGE (n:Resource {name: $name})" in query
    assert "SET" in query
    assert "n.capacity = $p_capacity" in query
    assert params["p_capacity"] == 500


def test_extraction_to_cypher_default_mode_is_overwrite():
    graph = FactoryPlanningGraph(
        resources=[Resource(name="M-1", resource_type="machine")],
    )
    # overwrite path: plain list of (query, params)
    statements = extraction_to_cypher(graph)
    assert isinstance(statements, list)
    assert any("MERGE (n:Resource {name: $name})" in q for q, _ in statements)


def test_extraction_to_cypher_with_mode_overwrite_returns_no_node_entries():
    graph = FactoryPlanningGraph(
        resources=[Resource(name="M-1", resource_type="machine")],
    )
    rels, nodes = extraction_to_cypher_with_mode(graph, MergeMode.OVERWRITE)
    assert nodes == []
    assert isinstance(rels, list)


def test_extraction_to_cypher_with_mode_conflict_returns_node_entries():
    graph = FactoryPlanningGraph(
        resources=[Resource(name="M-1", resource_type="machine")],
    )
    rels, nodes = extraction_to_cypher_with_mode(graph, MergeMode.CONFLICT)
    assert len(nodes) == 1
    fetch_q, fetch_p, entity, label = nodes[0]
    assert label == "Resource"
    assert "MATCH (n:Resource {name: $name}) RETURN n" in fetch_q
    assert fetch_p == {"name": "M-1"}
    assert entity.name == "M-1"


def test_model_to_cypher_fetch_returns_read_only_match():
    r = Resource(name="AKL-01", resource_type="AS/RS")
    query, params = model_to_cypher_fetch(r, "Resource")
    assert query == "MATCH (n:Resource {name: $name}) RETURN n"
    assert params == {"name": "AKL-01"}
