"""Map Pydantic extraction models to Cypher MERGE statements for FalkorDB."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Iterator

from pydantic import BaseModel

from knowledge._textutils import utc_now_iso
from knowledge.graph_models.factory_graph_model import (
    AmbiguousDuration,
    ControlStrategy,
    FactoryPlanningGraph,
    KPI,
    OrderLogic,
    Product,
    ProductionProgram,
    Resource,
    ShiftModel,
    Trailer,
    TrafficRule,
    TransportRoute,
    TransportSegment,
    TransportVehicle,
    WorkerPool,
    Zone,
)

# Fields on each entity type that reference names of other entity types.
# Maps (source_label, field_name) -> (rel_type, target_label).
_REFERENCE_FIELDS: dict[tuple[str, str], tuple[str, str]] = {
    ("Resource", "shift_model"): ("HAS_SHIFT_MODEL", "ShiftModel"),
    ("Resource", "assigned_products"): ("PROCESSES", "Product"),
    ("Resource", "zone"): ("CONTAINED_IN", "Zone"),
    ("TransportSegment", "from_node"): ("FROM", "Resource"),
    ("TransportSegment", "to_node"): ("TO", "Resource"),
    ("TransportRoute", "stop_sequence"): ("STOPS_AT", "Resource"),
    ("TransportRoute", "waiting_positions"): ("HAS_WAITING_POSITION", "Resource"),
    ("TransportRoute", "served_demand_points"): ("SERVES", "Resource"),
    ("TrafficRule", "affected_segments"): ("AFFECTS_SEGMENT", "TransportSegment"),
    ("Product", "bom_children"): ("HAS_CHILD", "Product"),
    ("OrderLogic", "associated_product"): ("FOR_PRODUCT", "Product"),
    ("OrderLogic", "associated_resource"): ("TARGETS", "Resource"),
    ("ShiftModel", "applicable_zones"): ("APPLIES_TO_ZONE", "Zone"),
    ("WorkerPool", "assigned_resources"): ("OPERATES", "Resource"),
    ("ControlStrategy", "affected_resources"): ("GOVERNS", "Resource"),
    ("ControlStrategy", "affected_products"): ("AFFECTS", "Product"),
    ("Zone", "parent_zone"): ("PART_OF", "Zone"),
    ("Zone", "member_resources"): ("CONTAINS", "Resource"),
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
    ("zones", Zone, "Zone"),
    ("kpis", KPI, "KPI"),
    ("ambiguous_durations", AmbiguousDuration, "AmbiguousDuration"),
]

# Fields that are cross-references and should not be stored as scalar properties.
_REF_FIELD_NAMES: set[str] = {field for (_, field) in _REFERENCE_FIELDS}

# Name of the list-valued node property that records property conflicts in-graph.
_CONFLICTS_PROP = "conflicts"

# Node property that holds a JSON-encoded list of aliases (plain names that
# were reconciled as possible duplicates of this indexed node).
_ALIASES_PROP = "aliases"

# Node property set on a plain-name node pointing to its canonical indexed name.
_CANONICAL_NAME_PROP = "canonical_name"

# Relationship type linking a plain-name node to the indexed node it may duplicate.
_RECON_REL_TYPE = "POSSIBLE_DUPLICATE_OF"

# Scalar fields on Resource that require special handling (not plain conflict
# detection). ``description`` is coalesced via LLM instead of first-writer-wins.
_COALESCED_FIELDS = {"description"}


class MergeMode(str, Enum):
    """How to reconcile property values when MERGE matches an existing node.

    - OVERWRITE: last-write-wins (the original behaviour). ``SET n.k = $v``
      unconditionally overwrites prior values.
    - CONFLICT:  first-writer-wins. Existing non-null values are preserved;
      incoming values that disagree are recorded in ``n.conflicts`` (and in
      an out-of-graph JSONL log) for human review.
    """

    OVERWRITE = "overwrite"
    CONFLICT = "conflict"


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


def _iter_entities(
    graph: FactoryPlanningGraph,
    *,
    label: str | None = None,
) -> Iterator[tuple[BaseModel, str]]:
    """Yield ``(entity, label)`` for every entity in the extraction graph.

    When ``label`` is given, only entities of that label are yielded.
    """
    for field_name, _cls, ent_label in _ENTITY_LISTS:
        if label is not None and ent_label != label:
            continue
        for entity in getattr(graph, field_name, []) or []:
            yield entity, ent_label


def extraction_to_cypher(
    graph: FactoryPlanningGraph,
) -> list[tuple[str, dict[str, Any]]]:
    """Convert a full extraction to a list of Cypher MERGE statements.

    Nodes first, then relationships. Equivalent to
    ``extraction_to_cypher_with_mode(graph, MergeMode.OVERWRITE)`` for the
    relationship half; the node half is the plain overwrite MERGEs.
    """
    statements = [model_to_cypher_merge(e, lbl) for e, lbl in _iter_entities(graph)]
    rel_statements, _ = extraction_to_cypher_with_mode(graph, MergeMode.OVERWRITE)
    statements.extend(rel_statements)
    return statements


# ---------------------------------------------------------------------------
# Conflict-detecting merge mode
# ---------------------------------------------------------------------------

def _scalar_fields(entity: BaseModel) -> dict[str, Any]:
    """Return non-None, non-reference scalar fields of ``entity``."""
    data = entity.model_dump(exclude_none=True)
    data.pop("name", None)
    return {k: v for k, v in data.items() if k not in _REF_FIELD_NAMES}


def model_to_cypher_fetch(entity: BaseModel, label: str) -> tuple[str, dict[str, Any]]:
    """Build a read-only MATCH that returns the existing node's scalar props.

    Used by the conflict merge mode to discover prior values before deciding
    whether to write or record a conflict.
    """
    name = entity.model_dump()["name"]
    query = f"MATCH (n:{label} {{name: $name}}) RETURN n"
    return query, {"name": name}


def build_conflict_merge(
    entity: BaseModel,
    label: str,
    existing_props: dict[str, Any],
    *,
    source: str | None = None,
    chunk_index: int | None = None,
    coalesced_values: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Convert one Pydantic entity to a conflict-aware MERGE + SET statement.

    First-writer-wins for scalar fields: a null existing value is written,
    an equal value is a no-op, a differing non-null existing value is kept
    and the incoming value is recorded as a conflict in ``n.conflicts``.
    Fields in :data:`_COALESCED_FIELDS` (e.g. ``description``) are instead
    written from caller-supplied ``coalesced_values`` with no conflict.

    Returns ``(cypher_query, parameters, conflicts)`` — ``conflicts`` is also
    embedded in the query so it lands in-graph in the same round-trip.
    """
    coalesced_values = coalesced_values or {}
    data = entity.model_dump(exclude_none=True)
    name = data.pop("name")
    params: dict[str, Any] = {"name": name}
    set_parts: list[str] = []
    conflicts: list[dict[str, Any]] = []
    conflict_params: dict[str, Any] = {}

    for key, incoming in _scalar_fields(entity).items():
        if key in coalesced_values:
            coalesced = coalesced_values[key]
            param_key = f"p_{key}"
            params[param_key] = _serialize_value(coalesced)
            set_parts.append(f"n.{key} = ${param_key}")
            continue

        existing = existing_props.get(key)
        if existing is None:
            # No prior value — write it.
            param_key = f"p_{key}"
            params[param_key] = _serialize_value(incoming)
            set_parts.append(f"n.{key} = ${param_key}")
            continue

        incoming_ser = _serialize_value(incoming)
        if existing == incoming_ser:
            # Agreement — no-op.
            continue

        # Conflict: keep existing, record incoming.
        detected_at = utc_now_iso()
        conflict = {
            "id": f"{key}:{detected_at}",
            "property": key,
            "existing_value": existing,
            "incoming_value": incoming_ser,
            "source": source,
            "chunk_index": chunk_index,
            "detected_at": detected_at,
            "resolved": False,
        }
        conflicts.append(conflict)
        c_key = f"c_{key}"
        conflict_params[c_key] = json.dumps(conflict)
        set_parts.append(
            f"n.{_CONFLICTS_PROP} = "
            f"coalesce(n.{_CONFLICTS_PROP}, \"[]\") + [${c_key}]"
        )

    query = f"MERGE (n:{label} {{name: $name}})"
    if set_parts:
        query += " SET " + ", ".join(set_parts)

    params.update(conflict_params)
    return query, params, conflicts


def build_reconciliation_link_cypher(
    plain_name: str,
    indexed_name: str,
    *,
    cosine: float,
    confidence: float,
    detected_at: str,
    source: str | None = None,
    chunk_index: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the Cypher that links a plain-name node to its indexed duplicate.

    Creates a ``POSSIBLE_DUPLICATE_OF`` relationship from the plain node to the
    indexed node, stores the plain name as an alias on the indexed node, and
    sets the indexed name as ``canonical_name`` on the plain node.

    Returns ``(cypher_query, parameters)``.
    """
    params: dict[str, Any] = {
        "plain_name": plain_name,
        "indexed_name": indexed_name,
        "cosine": round(float(cosine), 4),
        "confidence": round(float(confidence), 4),
        "detected_at": detected_at,
        "source": source,
        "chunk_index": chunk_index,
    }
    query = (
        "MERGE (a:Resource {name: $plain_name}) "
        "MERGE (b:Resource {name: $indexed_name}) "
        f"MERGE (a)-[r:{_RECON_REL_TYPE}]->(b) "
        "SET r.cosine_similarity = $cosine, "
        "r.llm_confidence = $confidence, "
        "r.detected_at = $detected_at, "
        "r.source = $source, "
        "r.chunk_index = $chunk_index, "
        f"b.{_ALIASES_PROP} = coalesce(b.{_ALIASES_PROP}, \"[]\") + [$plain_name], "
        f"a.{_CANONICAL_NAME_PROP} = $indexed_name"
    )
    return query, params


def extraction_to_cypher_with_mode(
    graph: FactoryPlanningGraph,
    mode: MergeMode,
    *,
    source: str | None = None,
    chunk_index: int | None = None,
) -> tuple[
    list[tuple[str, dict[str, Any]]],
    list[tuple[str, dict[str, Any], BaseModel, str]],
]:
    """Convert an extraction to Cypher statements under ``mode``.

    Returns ``(relationship_statements, node_entries)``. Relationship MERGEs
    are identical in both modes (no edge-conflict detection). ``node_entries``
    holds one ``(fetch_query, fetch_params, entity, label)`` per entity in
    conflict mode (empty in overwrite mode); the caller fetches each node,
    feeds the props to :func:`build_conflict_merge`, runs the write, then
    runs all relationship statements. ``source``/``chunk_index`` are stored
    on each conflict record for provenance.
    """
    rel_statements: list[tuple[str, dict[str, Any]]] = []
    for entity, label in _iter_entities(graph):
        rel_statements.extend(_relationship_merges(entity, label))

    node_entries: list[tuple[str, dict[str, Any], BaseModel, str]] = []
    if mode is MergeMode.CONFLICT:
        for entity, label in _iter_entities(graph):
            fetch_q, fetch_p = model_to_cypher_fetch(entity, label)
            node_entries.append((fetch_q, fetch_p, entity, label))

    return rel_statements, node_entries
