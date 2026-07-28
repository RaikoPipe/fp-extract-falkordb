"""Unit tests for the conflict-detecting merge mode in ``cypher_mapper``.

These tests exercise :func:`build_conflict_merge` directly with synthetic
``existing_props`` dicts — no FalkorDB connection required.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.cypher_mapper import (
    MergeMode,
    build_conflict_merge,
)
from knowledge.graph_models.factory_graph_model import (
    Resource,
    Product,
)


# --------------------------------------------------------------------------
# First-writer-wins policy
# --------------------------------------------------------------------------
def test_conflict_mode_detects_value_mismatch():
    """Existing capacity=500, incoming capacity=600 -> conflict, no overwrite."""
    r = Resource(name="M-1", name_has_index=True, description="Machine 1", resource_type="machine", capacity=600)
    existing = {"name": "M-1", "resource_type": "machine", "capacity": 500}

    query, params, conflicts = build_conflict_merge(
        r, "Resource", existing, source="doc.docx", chunk_index=3
    )

    # capacity is NOT written via SET (existing preserved)
    assert "n.capacity" not in query
    # a conflict entry is appended to n.conflicts
    assert "n.conflicts" in query
    assert conflicts and len(conflicts) == 1
    c = conflicts[0]
    assert c["property"] == "capacity"
    assert c["existing_value"] == 500
    assert c["incoming_value"] == 600
    assert c["source"] == "doc.docx"
    assert c["chunk_index"] == 3
    assert "detected_at" in c
    # stable id + resolved flag
    assert c["id"] == f"capacity:{c['detected_at']}"
    assert c["resolved"] is False


def test_conflict_mode_first_writer_wins_on_agreement():
    """Existing capacity=500, incoming capacity=500 -> no conflict, no SET."""
    r = Resource(name="M-1", name_has_index=True, description="Machine 1", resource_type="machine", capacity=500)
    existing = {"name": "M-1", "name_has_index": True, "resource_type": "machine", "capacity": 500, "description": "Machine 1"}

    query, params, conflicts = build_conflict_merge(
        r, "Resource", existing, source="doc.docx", chunk_index=0
    )

    assert conflicts == []
    # no SET clause needed since everything agrees
    assert "SET" not in query


def test_conflict_mode_writes_to_null_property():
    """Existing capacity=None (absent), incoming capacity=500 -> SET, no conflict."""
    r = Resource(name="M-1", name_has_index=True, description="Machine 1", resource_type="machine", capacity=500)
    existing = {"name": "M-1", "resource_type": "machine"}

    query, params, conflicts = build_conflict_merge(
        r, "Resource", existing, source="doc.docx", chunk_index=1
    )

    assert "n.capacity = $p_capacity" in query
    assert params["p_capacity"] == 500
    assert conflicts == []


def test_conflict_mode_records_provenance():
    """Conflict records carry source + chunk_index."""
    r = Resource(name="M-1", name_has_index=True, description="Machine 1", resource_type="machine", capacity=999)
    existing = {"name": "M-1", "resource_type": "machine", "capacity": 100}

    _, _, conflicts = build_conflict_merge(
        r, "Resource", existing, source="plant_layout.docx", chunk_index=7
    )

    assert conflicts[0]["source"] == "plant_layout.docx"
    assert conflicts[0]["chunk_index"] == 7


def test_conflict_mode_skips_reference_fields():
    """Reference fields (shift_model, assigned_products) never produce conflicts."""
    r = Resource(
        name="M-1",
        name_has_index=True,
        description="Machine 1",
        resource_type="machine",
        shift_model="Night-Shift",
        assigned_products=["P-A"],
    )
    existing = {
        "name": "M-1",
        "resource_type": "machine",
        "shift_model": "Day-Shift",
    }

    _, _, conflicts = build_conflict_merge(
        r, "Resource", existing, source="d.docx", chunk_index=0
    )

    # shift_model and assigned_products are reference fields -> relationships,
    # not scalar SET, so they must not appear in conflicts.
    prop_names = {c["property"] for c in conflicts}
    assert "shift_model" not in prop_names
    assert "assigned_products" not in prop_names


def test_conflict_mode_appends_to_existing_conflicts_list():
    """A second conflicting property yields a second conflict entry."""
    r = Resource(name="M-1", name_has_index=True, description="Machine 1", resource_type="machine", capacity=600, mtbf="d=9999s")
    existing = {
        "name": "M-1",
        "resource_type": "machine",
        "capacity": 500,
        "mtbf": "d=8000s",
    }

    query, params, conflicts = build_conflict_merge(
        r, "Resource", existing, source="d.docx", chunk_index=0
    )

    assert len(conflicts) == 2
    prop_names = {c["property"] for c in conflicts}
    assert prop_names == {"capacity", "mtbf"}
    # two separate c_<prop> params, two append SET clauses
    assert "c_capacity" in params and "c_mtbf" in params
    assert query.count("+ [$c_") == 2


def test_conflict_mode_query_includes_append_to_conflicts_list():
    """The generated Cypher appends each conflict to n.conflicts via list concat."""
    r = Resource(name="M-1", name_has_index=True, description="Machine 1", resource_type="machine", capacity=600)
    existing = {"name": "M-1", "resource_type": "machine", "capacity": 500}

    query, params, _ = build_conflict_merge(
        r, "Resource", existing, source="d.docx", chunk_index=0
    )

    assert "MERGE (n:Resource {name: $name})" in query
    assert "coalesce(n.conflicts, \"[]\")" in query
    assert "[$c_capacity]" in query
    # the conflict JSON is a valid JSON string in the params
    parsed = json.loads(params["c_capacity"])
    assert parsed["property"] == "capacity"
    assert parsed["id"] == f"capacity:{parsed['detected_at']}"
    assert parsed["resolved"] is False


def test_conflict_mode_no_existing_node_writes_all_fields():
    """When the node doesn't exist yet (empty existing_props), all fields are SET."""
    r = Resource(name="M-1", name_has_index=True, description="Machine 1", resource_type="machine", capacity=500, mtbf="d=8000s")
    existing = {}

    query, params, conflicts = build_conflict_merge(
        r, "Resource", existing, source="d.docx", chunk_index=0
    )

    assert "n.resource_type = $p_resource_type" in query
    assert "n.capacity = $p_capacity" in query
    assert "n.mtbf = $p_mtbf" in query
    assert conflicts == []


def test_conflict_mode_overwrite_mode_is_idempotent_on_repeated_ingest():
    """Overwrite mode (the original path) is unchanged — regression guard."""
    # In overwrite mode, build_conflict_merge is not used; model_to_cypher_merge
    # is. This test locks the overwrite contract: SET always overwrites.
    from knowledge.cypher_mapper import model_to_cypher_merge

    r = Resource(name="M-1", name_has_index=True, description="Machine 1", resource_type="machine", capacity=600)
    query, params = model_to_cypher_merge(r, "Resource")
    assert "n.capacity = $p_capacity" in query
    assert params["p_capacity"] == 600
    # No conflicts property in overwrite mode
    assert "conflicts" not in query


def test_conflict_mode_coalesced_description_writes_set():
    """When coalesced_values contains description, it is SET, no conflict."""
    r = Resource(name="M-1", name_has_index=True, description="New description", resource_type="machine")
    existing = {"name": "M-1", "resource_type": "machine", "description": "Old description"}

    query, params, conflicts = build_conflict_merge(
        r, "Resource", existing, source="d.docx", chunk_index=0,
        coalesced_values={"description": "Coalesced rich description"},
    )

    # description is written from the coalesced value, not treated as conflict
    assert "n.description = $p_description" in query
    assert params["p_description"] == "Coalesced rich description"
    prop_names = {c["property"] for c in conflicts}
    assert "description" not in prop_names