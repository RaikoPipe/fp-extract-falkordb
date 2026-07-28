"""Unit tests for the Zone entity and the Resource/Zone boundary."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.cypher_mapper import (
    _ENTITY_LISTS,
    _REFERENCE_FIELDS,
    extraction_to_cypher,
)
from knowledge.graph_models.factory_graph_model import (
    FactoryPlanningGraph,
    Resource,
    Zone,
)


def test_zone_basic_fields():
    z = Zone(name="Hall-A", zone_type="hall", description="Main hall", floor_area_m2=1200.0)
    assert z.name == "Hall-A"
    assert z.zone_type == "hall"
    assert z.parent_zone is None
    assert z.member_resources == []


def test_zone_nesting_and_members():
    z = Zone(
        name="Assembly-Line-1",
        zone_type="assembly_line",
        parent_zone="Hall-A",
        member_resources=["WS-1", "WS-2"],
    )
    assert z.parent_zone == "Hall-A"
    assert z.member_resources == ["WS-1", "WS-2"]


def test_resource_zone_reference_is_string():
    r = Resource(
        name="M-1",
        name_has_index=True,
        description="m",
        resource_type="machine",
        zone="Hall-A",
    )
    assert r.zone == "Hall-A"


def test_resource_type_no_longer_lists_pick_zone_as_resource():
    import json
    schema = FactoryPlanningGraph.model_json_schema()
    rt_desc = schema["$defs"]["Resource"]["properties"]["resource_type"]["description"]
    # pick_zone and assembly_line must be gone from the One-of list
    one_of = rt_desc.split("One of:")[1].split("or other")[0]
    assert "pick_zone" not in one_of
    assert "assembly_line" not in one_of
    # but supermarket + warehouse remain (they are atomic storage resources)
    assert "supermarket" in one_of
    assert "warehouse" in one_of


def test_zone_type_includes_assembly_line_and_pick_zone():
    import json
    schema = FactoryPlanningGraph.model_json_schema()
    zt_desc = schema["$defs"]["Zone"]["properties"]["zone_type"]["description"]
    assert "assembly_line" in zt_desc
    assert "pick_zone" in zt_desc
    assert "hall" in zt_desc
    assert "segment" in zt_desc


def test_layout_element_removed_from_schema():
    import json
    schema = FactoryPlanningGraph.model_json_schema()
    assert "LayoutElement" not in json.dumps(schema)
    assert "Zone" in schema["$defs"]


def test_entity_list_uses_zones_not_layout_elements():
    labels = [e[2] for e in _ENTITY_LISTS]
    assert "Zone" in labels
    assert "LayoutElement" not in labels
    field_names = [e[0] for e in _ENTITY_LISTS]
    assert "zones" in field_names
    assert "layout_elements" not in field_names


def test_reference_fields_include_zone_relationships():
    assert _REFERENCE_FIELDS[("Resource", "zone")] == ("CONTAINED_IN", "Zone")
    assert _REFERENCE_FIELDS[("Zone", "parent_zone")] == ("PART_OF", "Zone")
    assert _REFERENCE_FIELDS[("Zone", "member_resources")] == ("CONTAINS", "Resource")
    assert _REFERENCE_FIELDS[("ShiftModel", "applicable_zones")] == ("APPLIES_TO_ZONE", "Zone")


def test_extraction_emits_zone_node_and_relationships():
    g = FactoryPlanningGraph(
        zones=[
            Zone(name="Hall-A", zone_type="hall", member_resources=["M-1"]),
            Zone(name="Line-1", zone_type="assembly_line", parent_zone="Hall-A", member_resources=["WS-1"]),
        ],
        resources=[
            Resource(name="M-1", name_has_index=True, description="m", resource_type="machine", zone="Hall-A"),
        ],
    )
    statements = extraction_to_cypher(g)
    queries = [q for q, _ in statements]

    # Zone node MERGEs
    assert any("MERGE (n:Zone {name: $name})" in q and "Hall-A" in str(p) for q, p in statements)
    assert any("MERGE (n:Zone {name: $name})" in q and "Line-1" in str(p) for q, p in statements)

    # Resource CONTAINED_IN Zone
    assert any("CONTAINED_IN" in q and "Hall-A" in str(p) for q, p in statements)
    # Zone CONTAINS Resource
    assert any("CONTAINS" in q for q in queries)
    # Zone PART_OF Zone (nesting)
    assert any("PART_OF" in q and "Hall-A" in str(p) for q, p in statements)


def test_assembly_line_is_zone_not_resource_round_trip():
    g = FactoryPlanningGraph.model_validate_json(
        '{"zones": [{"name": "AL-1", "zone_type": "assembly_line", '
        '"member_resources": ["WS-1", "WS-2"]}]}'
    )
    assert g.zones[0].zone_type == "assembly_line"
    assert g.resources == []