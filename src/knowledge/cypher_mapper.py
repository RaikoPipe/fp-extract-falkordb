"""Map Pydantic extraction models to Cypher MERGE statements for FalkorDB."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator

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
) -> Iterator[tuple[BaseModel, str]]:
    """Yield ``(entity, label)`` for every entity in the extraction graph."""
    for field_name, _cls, label in _ENTITY_LISTS:
        for entity in getattr(graph, field_name, []) or []:
            yield entity, label


def extraction_to_cypher(
    graph: FactoryPlanningGraph,
) -> list[tuple[str, dict[str, Any]]]:
    """Convert a full extraction to a list of Cypher MERGE statements.

    First creates/updates all nodes, then creates relationships.
    Equivalent to ``extraction_to_cypher_with_mode(graph, MergeMode.OVERWRITE)``.
    """
    statements: list[tuple[str, dict[str, Any]]] = []

    for entity, label in _iter_entities(graph):
        statements.append(model_to_cypher_merge(entity, label))

    for entity, label in _iter_entities(graph):
        statements.extend(_relationship_merges(entity, label))

    return statements


# ---------------------------------------------------------------------------
# Conflict-detecting merge mode
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Current UTC timestamp in ISO-8601 (second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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

    Policy: first-writer-wins, except for fields listed in
    :data:`_COALESCED_FIELDS` (e.g. ``description``), which are handled via
    caller-supplied coalesced values.

    - For each scalar field on ``entity``:
        * If the field is in ``coalesced_values``, that value is written via
          ``SET`` (the caller — the async backend — computed it via an LLM
          coalesce call). No conflict is recorded for coalesced fields.
        * If the existing node does not have the property (or it is null),
          the incoming value is written via ``SET``.
        * If the existing value equals the incoming value, nothing happens.
        * If the existing value differs and is non-null, the incoming value
          is **not** written; instead a conflict record is appended to
          ``n.conflicts`` (a JSON-serialised list property).

    Returns ``(cypher_query, parameters, conflicts)`` where ``conflicts`` is
    the list of newly-detected conflict dicts (also embedded in the query so
    they land in-graph in the same round-trip).
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
        conflict = {
            "property": key,
            "existing_value": existing,
            "incoming_value": incoming_ser,
            "source": source,
            "chunk_index": chunk_index,
            "detected_at": _now_iso(),
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

    Returns ``(relationship_statements, node_entries)``:

    - ``relationship_statements`` — MERGE statements for cross-reference
      relationships (identical in both modes; v1 does not detect conflicts on
      edges).
    - ``node_entries`` — one ``(fetch_query, fetch_params, entity, label)``
      tuple per entity node, in the conflict mode; empty in overwrite mode.

    ``source`` and ``chunk_index`` are stored on each conflict record for
    provenance. They are accepted here for symmetry but are normally applied
    per-entity in :func:`build_conflict_merge`.

    Callers in overwrite mode should ignore the second return value and run
    the overwrite node MERGEs via :func:`extraction_to_cypher` (or
    :func:`model_to_cypher_merge` directly).

    In conflict mode, the caller is expected to:

    1. For each entry, run ``fetch_query`` to obtain the existing node props.
    2. Call :func:`build_conflict_merge` with those props to produce the
       write statement + detected conflicts.
    3. Run the write statement, append conflicts to the JSONL log.
    4. Finally run all ``relationship_statements``.
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
