"""FalkorDB graph database backend — connection, write, reset."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from falkordb import FalkorDB

from knowledge.cypher_mapper import (
    MergeMode,
    build_conflict_merge,
    extraction_to_cypher,
    extraction_to_cypher_with_mode,
)
from knowledge.graph_models.factory_graph_model import FactoryPlanningGraph

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 6379
_DEFAULT_GRAPH = "factory_planning"

# Labels whose `name` property is the natural full-text search target.
_DEFAULT_FULLTEXT_LABEL = "Resource"
_DEFAULT_FULLTEXT_PROPERTY = "name"

# Default embedding dimension for the configured LLM; FalkorDB supports 1-4096.
_DEFAULT_VECTOR_DIM = 1024
_DEFAULT_VECTOR_LABEL = "Resource"
_DEFAULT_VECTOR_PROPERTY = "embedding"
_DEFAULT_SIMILARITY_FUNCTION = "cosine"

# Default merge mode and on-disk conflict log path.
_DEFAULT_MERGE_MODE = MergeMode.OVERWRITE
_DEFAULT_CONFLICTS_LOG = "./data/conflicts.jsonl"


class FalkorDBBackend:
    """FalkorDB graph database backend."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        graph_name: str | None = None,
        merge_mode: MergeMode | str | None = None,
        conflicts_log_path: str | Path | None = None,
    ) -> None:
        self._host = host or os.getenv("FALKORDB_HOST", _DEFAULT_HOST)
        self._port = port or int(os.getenv("FALKORDB_PORT", str(_DEFAULT_PORT)))
        self._graph_name = graph_name or os.getenv("FALKORDB_GRAPH", _DEFAULT_GRAPH)
        self._db = FalkorDB(host=self._host, port=self._port)
        self._graph = self._db.select_graph(self._graph_name)

        # Merge mode: explicit arg > MERGE_MODE env > default (overwrite).
        if isinstance(merge_mode, MergeMode):
            self._merge_mode = merge_mode
        else:
            mode_str = merge_mode or os.getenv("MERGE_MODE", _DEFAULT_MERGE_MODE.value)
            self._merge_mode = MergeMode(mode_str)

        # Conflict log path: explicit arg > CONFLICTS_LOG env > default.
        log_str = conflicts_log_path or os.getenv(
            "CONFLICTS_LOG", _DEFAULT_CONFLICTS_LOG
        )
        self._conflicts_log_path = Path(log_str)

    @property
    def graph_name(self) -> str:
        return self._graph_name

    @property
    def merge_mode(self) -> MergeMode:
        return self._merge_mode

    @property
    def conflicts_log_path(self) -> Path:
        return self._conflicts_log_path

    def execute(self, query: str, params: dict[str, Any] | None = None) -> Any:
        """Execute a Cypher query and return the result."""
        return self._graph.query(query, params or {})

    def write_extraction(
        self,
        graph: FactoryPlanningGraph,
        *,
        source: str | None = None,
        chunk_index: int | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Write an extraction result to the database.

        In overwrite mode (default) every entity is upserted with
        last-write-wins semantics — identical to the original behaviour.

        In conflict mode each entity node is first fetched by ``{name}`` to
        discover existing scalar values; incoming values that disagree with a
        non-null existing value are recorded as conflicts (in-graph
        ``conflicts`` list property + appended to the JSONL log) and the
        existing value is preserved (first-writer-wins).

        Relationships are written with the same find-or-create MERGE in both
        modes; v1 does not detect conflicts on edge properties.

        Returns ``(statement_count, conflicts)`` where ``conflicts`` is the
        list of conflict dicts detected during this write (empty in overwrite
        mode or when none were found).
        """
        all_conflicts: list[dict[str, Any]] = []
        statements_run = 0

        if self._merge_mode is MergeMode.CONFLICT:
            rel_statements, node_entries = extraction_to_cypher_with_mode(
                graph, self._merge_mode, source=source, chunk_index=chunk_index
            )

            # Pass 1: per-entity fetch + conflict-aware merge.
            for fetch_q, fetch_p, entity, label in node_entries:
                existing_props = self._fetch_node_props(fetch_q, fetch_p, label)
                write_q, write_p, conflicts = build_conflict_merge(
                    entity,
                    label,
                    existing_props,
                    source=source,
                    chunk_index=chunk_index,
                )
                if write_q:
                    self._graph.query(write_q, write_p)
                    statements_run += 1
                if conflicts:
                    all_conflicts.extend(conflicts)

            # Pass 2: relationships.
            for query, params in rel_statements:
                self._graph.query(query, params)
                statements_run += 1
        else:
            statements = extraction_to_cypher(graph)
            for query, params in statements:
                self._graph.query(query, params)
            statements_run = len(statements)

        if all_conflicts:
            self._append_conflicts_log(all_conflicts)

        return statements_run, all_conflicts

    def _fetch_node_props(
        self, query: str, params: dict[str, Any], label: str
    ) -> dict[str, Any]:
        """Run a read-only MATCH and return the matched node's properties.

        Returns an empty dict when the node does not yet exist. The ``label``
        is unused for the lookup itself but kept for future schema-aware
        handling.
        """
        result = self._graph.query(query, params)
        rows = result.result_set if result.result_set else []
        if not rows:
            return {}
        node = rows[0][0]
        if node is None:
            return {}
        props = dict(node.properties) if hasattr(node, "properties") else {}
        # The conflicts list is itself a JSON string in-graph; leave it as-is
        # — it is not a scalar property we compare against.
        return props

    def _append_conflicts_log(self, conflicts: list[dict[str, Any]]) -> None:
        """Append conflict records to the JSONL log file (create if needed)."""
        self._conflicts_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conflicts_log_path.open("a", encoding="utf-8") as fh:
            for conflict in conflicts:
                fh.write(json.dumps(conflict, ensure_ascii=False, default=str))
                fh.write("\n")

    def get_conflicts(self, label: str | None = None) -> list[dict[str, Any]]:
        """Return all nodes that carry a non-null ``conflicts`` property.

        Each row is ``{name, labels, conflicts}`` where ``conflicts`` is the
        parsed list of conflict dicts. When ``label`` is given, restricts the
        scan to that label.
        """
        label_clause = f":{label}" if label else ""
        cypher = (
            f"MATCH (n{label_clause}) "
            f"WHERE n.conflicts IS NOT NULL "
            f"RETURN n.name AS name, labels(n) AS labels, n.conflicts AS conflicts"
        )
        result = self._graph.query(cypher)
        rows: list[dict[str, Any]] = []
        for row in result.result_set or []:
            name, labels, raw = row
            parsed: list[dict[str, Any]] = []
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = [{"_raw": raw}]
            elif isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str):
                        try:
                            parsed.append(json.loads(item))
                        except json.JSONDecodeError:
                            parsed.append({"_raw": item})
                    elif isinstance(item, dict):
                        parsed.append(item)
            rows.append({
                "name": str(name) if name is not None else "",
                "labels": list(labels) if labels else [],
                "conflicts": parsed,
            })
        return rows

    def clear_conflicts(
        self, label: str | None = None, name: str | None = None
    ) -> int:
        """Dismiss reviewed conflicts by nulling the ``conflicts`` property.

        Returns the number of nodes updated. When ``label``/``name`` are
        given, restricts the operation accordingly.
        """
        label_clause = f":{label}" if label else ""
        name_clause = " AND n.name = $name" if name else ""
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        cypher = (
            f"MATCH (n{label_clause}) "
            f"WHERE n.conflicts IS NOT NULL{name_clause} "
            f"SET n.conflicts = null"
        )
        result = self._graph.query(cypher, params)
        # FalkorDB returns statistics on writes; fall back to 0 if absent.
        if hasattr(result, "statistics"):
            stats = result.statistics
            for key in ("properties_set", "nodes_updated"):
                if hasattr(stats, key):
                    return int(getattr(stats, key) or 0)
        return 0

    def reset(self) -> None:
        """Delete all nodes and relationships.

        Note: this does NOT clear the on-disk conflicts JSONL log, which is
        an audit trail that survives graph resets.
        """
        self._graph.query("MATCH (n) DETACH DELETE n")

    def node_count(self) -> int:
        """Return the total number of nodes in the graph."""
        result = self._graph.query("MATCH (n) RETURN count(n) AS cnt")
        return result.result_set[0][0] if result.result_set else 0

    def get_all_nodes(self) -> list[dict[str, Any]]:
        """Return all nodes as dicts with labels and properties."""
        result = self._graph.query("MATCH (n) RETURN n")
        nodes = []
        for row in result.result_set:
            node = row[0]
            props = dict(node.properties) if hasattr(node, "properties") else {}
            labels = list(node.labels) if hasattr(node, "labels") else []
            props["_labels"] = labels
            nodes.append(props)
        return nodes

    def get_all_edges(self) -> list[tuple[str, str, str, dict[str, Any]]]:
        """Return all edges as (src_name, tgt_name, rel_type, properties)."""
        result = self._graph.query(
            "MATCH (a)-[r]->(b) RETURN a.name, b.name, type(r), properties(r)"
        )
        edges = []
        for row in result.result_set:
            src_name, tgt_name, rel_type, props = row
            edges.append((
                str(src_name or ""),
                str(tgt_name or ""),
                str(rel_type or ""),
                dict(props) if props else {},
            ))
        return edges

    def get_schema_info(self) -> dict[str, Any]:
        """Return graph schema information for search context."""
        labels_result = self._graph.query(
            "CALL db.labels() YIELD label RETURN collect(label)"
        )
        labels = labels_result.result_set[0][0] if labels_result.result_set else []

        rel_result = self._graph.query(
            "CALL db.relationshipTypes() YIELD relationshipType RETURN collect(relationshipType)"
        )
        rel_types = rel_result.result_set[0][0] if rel_result.result_set else []

        prop_result = self._graph.query(
            "CALL db.propertyKeys() YIELD propertyKey RETURN collect(propertyKey)"
        )
        prop_keys = prop_result.result_set[0][0] if prop_result.result_set else []

        return {
            "labels": labels,
            "relationship_types": rel_types,
            "property_keys": prop_keys,
        }

    # ------------------------------------------------------------------
    # Full-text search
    # ------------------------------------------------------------------
    def ensure_fulltext_index(
        self,
        label: str = _DEFAULT_FULLTEXT_LABEL,
        properties: tuple[str, ...] = (_DEFAULT_FULLTEXT_PROPERTY,),
    ) -> None:
        """Create a full-text index on ``label`` for the given properties.

        Idempotent: existing indexes are reported by FalkorDB as an error
        ("Index already exists"), which we treat as success.
        """
        prop_list = ", ".join(f"'{p}'" for p in properties)
        cypher = f"CALL db.idx.fulltext.createNodeIndex('{label}', {prop_list})"
        try:
            self._graph.query(cypher)
        except Exception as exc:
            if "already exists" in str(exc).lower():
                return
            raise

    def fulltext_search(
        self,
        query: str,
        label: str = _DEFAULT_FULLTEXT_LABEL,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        """Run a RediSearch full-text query against ``label``.

        Returns up to ``k`` nodes as dicts with labels and properties, plus a
        ``_score`` field holding the TF-IDF relevance score.
        """
        cypher = (
            "CALL db.idx.fulltext.queryNodes($label, $query) "
            "YIELD node, score "
            "RETURN node, score "
            f"LIMIT {int(k)}"
        )
        result = self._graph.query(
            cypher, {"label": label, "query": query}
        )
        rows: list[dict[str, Any]] = []
        for row in result.result_set or []:
            node, score = row[0], row[1]
            props = dict(node.properties) if hasattr(node, "properties") else {}
            labels = list(node.labels) if hasattr(node, "labels") else []
            props["_labels"] = labels
            props["_score"] = score
            rows.append(props)
        return rows

    # ------------------------------------------------------------------
    # Vector search
    # ------------------------------------------------------------------
    def ensure_vector_index(
        self,
        label: str = _DEFAULT_VECTOR_LABEL,
        property: str = _DEFAULT_VECTOR_PROPERTY,
        dim: int = _DEFAULT_VECTOR_DIM,
        similarity_function: str = _DEFAULT_SIMILARITY_FUNCTION,
    ) -> None:
        """Create a vector index on ``label.property``.

        Idempotent: existing indexes are reported by FalkorDB as an error,
        which we treat as success.
        """
        cypher = (
            f"CREATE VECTOR INDEX FOR (n:{label}) ON (n.{property}) "
            f"OPTIONS {{dimension:{int(dim)}, "
            f"similarityFunction:'{similarity_function}'}}"
        )
        try:
            self._graph.query(cypher)
        except Exception as exc:
            if "already exists" in str(exc).lower():
                return
            raise

    def vector_search(
        self,
        embedding: list[float],
        label: str = _DEFAULT_VECTOR_LABEL,
        property: str = _DEFAULT_VECTOR_PROPERTY,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        """Run a nearest-neighbour vector search against ``label.property``.

        ``embedding`` must match the dimension the index was created with.
        Returns up to ``k`` nodes as dicts with labels and properties plus a
        ``_score`` field holding the similarity score.
        """
        vec_str = json.dumps([float(x) for x in embedding])
        cypher = (
            f"CALL db.idx.vector.queryNodes('{label}', '{property}', {int(k)}, "
            f"vecf32({vec_str})) YIELD node, score "
            "RETURN node, score"
        )
        result = self._graph.query(cypher)
        rows: list[dict[str, Any]] = []
        for row in result.result_set or []:
            node, score = row[0], row[1]
            props = dict(node.properties) if hasattr(node, "properties") else {}
            labels = list(node.labels) if hasattr(node, "labels") else []
            props["_labels"] = labels
            props["_score"] = score
            rows.append(props)
        return rows
