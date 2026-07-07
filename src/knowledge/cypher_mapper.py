"""Map Pydantic extraction models to Cypher MERGE statements for FalkorDB."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from knowledge.graph_models.factory_graph_model import (
    ControlStrategy,
    FactoryPlanningGraph,
    KPI,
    LayoutElement,
    OrderLogic,
    Product,
    ProductionProgram,
    Resource,
    ShiftModel,
    StochasticParameter,
    Trailer,
    TrafficRule,
    TransportRoute,
    TransportSegment,
    TransportVehicle,
    WorkerPool,
)

# Fields on each entity type that reference names of other entity types.
# Maps (source_label, field_name) -> (rel_type, target_label).
_REFERENCE_FIELDS: dict[tuple[str, str], tuple[str, str]] = {
    ("Resource", "shift_model"): ("HAS_SHIFT_MODEL", "ShiftModel"),
    ("Resource", "assigned_products"): ("PROCESSES", "Product"),
    ("TransportSegment", "from_node"): ("FROM", "Resource"),
    ("TransportSegment", "to_node"): ("TO", "Resource"),
    ("TransportRoute", "stop_sequence"): ("STOPS_AT", "Resource"),
    ("TransportRoute", "waiting_positions"): ("HAS_WAITING_POSITION", "Resource"),
    ("TransportRoute", "served_demand_points"): ("SERVES", "Resource"),
    ("TrafficRule", "affected_segments"): ("AFFECTS_SEGMENT", "TransportSegment"),
    ("Product", "bom_children"): ("HAS_CHILD", "Product"),
    ("OrderLogic", "associated_product"): ("FOR_PRODUCT", "Product"),
    ("OrderLogic", "associated_resource"): ("TARGETS", "Resource"),
    ("ShiftModel", "applicable_zones"): ("APPLIES_TO_ZONE", "LayoutElement"),
    ("WorkerPool", "assigned_resources"): ("OPERATES", "Resource"),
    ("ControlStrategy", "affected_resources"): ("GOVERNS", "Resource"),
    ("ControlStrategy", "affected_products"): ("AFFECTS", "Product"),
    ("StochasticParameter", "associated_entity"): ("DESCRIBES", "Resource"),
    ("KPI", "scope"): ("SCOPED_TO", "Resource"),
}

# Entity list field name on FactoryPlanningGraph -> (Pydantic class, Cypher label)
_ENTITY_LISTS: list[tuple[str, type[BaseModel], str]] = [
    ("resources", Resource, "Resource"),
    ("transport_vehicles", TransportVehicle, "TransportVehicle"),
    ("trailers", Trailer, "Trailer"),
    ("transport_segments", TransportSegment, "TransportSegment"),
    ("transport_routes", TransportRoute, "TransportRoute"),
    ("traffic_rules", TrafficRule, "TrafficRule"),
    ("products", Product, "Product"),
    ("production_programs", ProductionProgram, "ProductionProgram"),
    ("order_logic", OrderLogic, "OrderLogic"),
    ("shift_models", ShiftModel, "ShiftModel"),
    ("worker_pools", WorkerPool, "WorkerPool"),
    ("control_strategies", ControlStrategy, "ControlStrategy"),
    ("layout_elements", LayoutElement, "LayoutElement"),
    ("kpis", KPI, "KPI"),
    ("stochastic_parameters", StochasticParameter, "StochasticParameter"),
]

# Fields that are cross-references and should not be stored as scalar properties.
_REF_FIELD_NAMES: set[str] = {field for (_, field) in _REFERENCE_FIELDS}


def _serialize_value(value: Any) -> Any:
    """Convert a Python value to something FalkorDB can store."""
    if isinstance(value, list):
        return json.dumps(value)
    if isinstance(value, bool):
        return value
    return value


def model_to_cypher_merge(
    entity: BaseModel, label: str
) -> tuple[str, dict[str, Any]]:
    """Convert one Pydantic entity to a Cypher MERGE + SET statement.

    MERGE key is always ``{name: $name}``. All non-None, non-reference fields
    are written via SET.

    Returns ``(cypher_query, parameters)``.
    """
    data = entity.model_dump(exclude_none=True)
    name = data.pop("name")
    params: dict[str, Any] = {"name": name}

    set_parts: list[str] = []
    for key, value in data.items():
        if key in _REF_FIELD_NAMES:
            continue
        param_key = f"p_{key}"
        params[param_key] = _serialize_value(value)
        set_parts.append(f"n.{key} = ${param_key}")

    query = f"MERGE (n:{label} {{name: $name}})"
    if set_parts:
        query += " SET " + ", ".join(set_parts)

    return query, params


def _relationship_merges(
    entity: BaseModel, label: str
) -> list[tuple[str, dict[str, Any]]]:
    """Generate MERGE statements for cross-reference relationships."""
    data = entity.model_dump(exclude_none=True)
    source_name = data.get("name")
    if not source_name:
        return []

    statements: list[tuple[str, dict[str, Any]]] = []

    for (src_label, field_name), (rel_type, target_label) in _REFERENCE_FIELDS.items():
        if src_label != label:
            continue
        value = data.get(field_name)
        if not value:
            continue

        targets = value if isinstance(value, list) else [value]
        for i, target_name in enumerate(targets):
            if not target_name or not isinstance(target_name, str):
                continue
            params = {"src_name": source_name, "tgt_name": target_name}
            query = (
                f"MATCH (a:{label} {{name: $src_name}}) "
                f"MERGE (b:{target_label} {{name: $tgt_name}}) "
                f"MERGE (a)-[r:{rel_type}"
            )
            if field_name == "stop_sequence":
                query += f" {{seq: {i}}}"
            query += "]->(b)"
            statements.append((query, params))

    return statements


def extraction_to_cypher(
    graph: FactoryPlanningGraph,
) -> list[tuple[str, dict[str, Any]]]:
    """Convert a full extraction to a list of Cypher MERGE statements.

    First creates/updates all nodes, then creates relationships.
    """
    statements: list[tuple[str, dict[str, Any]]] = []

    for field_name, _cls, label in _ENTITY_LISTS:
        entities = getattr(graph, field_name, [])
        for entity in entities:
            statements.append(model_to_cypher_merge(entity, label))

    for field_name, _cls, label in _ENTITY_LISTS:
        entities = getattr(graph, field_name, [])
        for entity in entities:
            statements.extend(_relationship_merges(entity, label))

    return statements
