"""FalkorDB graph database backend — connection, write, reset."""

from __future__ import annotations

import os
from typing import Any

from falkordb import FalkorDB

from knowledge.cypher_mapper import extraction_to_cypher
from knowledge.graph_models.factory_graph_model import FactoryPlanningGraph

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 6379
_DEFAULT_GRAPH = "factory_planning"


class FalkorDBBackend:
    """FalkorDB graph database backend."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        graph_name: str | None = None,
    ) -> None:
        self._host = host or os.getenv("FALKORDB_HOST", _DEFAULT_HOST)
        self._port = port or int(os.getenv("FALKORDB_PORT", str(_DEFAULT_PORT)))
        self._graph_name = graph_name or os.getenv("FALKORDB_GRAPH", _DEFAULT_GRAPH)
        self._db = FalkorDB(host=self._host, port=self._port)
        self._graph = self._db.select_graph(self._graph_name)

    @property
    def graph_name(self) -> str:
        return self._graph_name

    def execute(self, query: str, params: dict[str, Any] | None = None) -> Any:
        """Execute a Cypher query and return the result."""
        return self._graph.query(query, params or {})

    def write_extraction(self, graph: FactoryPlanningGraph) -> int:
        """Write a full extraction result to the database.

        Returns the number of Cypher statements executed.
        """
        statements = extraction_to_cypher(graph)
        for query, params in statements:
            self._graph.query(query, params)
        return len(statements)

    def reset(self) -> None:
        """Delete all nodes and relationships."""
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
