"""FalkorDB graph database backend — connection, write, reset."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from falkordb import FalkorDB
from loguru import logger

from knowledge._textutils import utc_now_iso
from knowledge.cypher_mapper import (
    MergeMode,
    _iter_entities,
    build_conflict_merge,
    build_reconciliation_link_cypher,
    extraction_to_cypher_with_mode,
    model_to_cypher_fetch,
    model_to_cypher_merge,
)
from knowledge.graph_models.factory_graph_model import FactoryPlanningGraph, Resource
from knowledge.reconciliation import (
    ReconciliationDecision,
    coalesce_description,
    detect_embedding_dim,
    embed_description,
    reconcile_existing_plain_nodes,
    reconcile_new_node,
)

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

# Default embedding provider — independent from the LLM provider. The LLM may
# run on a cloud endpoint (e.g. ollama.com) that does not expose /api/embed,
# so embeddings default to a local Ollama instance.
_DEFAULT_EMBEDDING_API_BASE = "http://localhost:11434"

# Default merge mode.
_DEFAULT_MERGE_MODE = MergeMode.OVERWRITE

# Reconciliation defaults.
_DEFAULT_RECONCILIATIONS_LOG = "./data/reconciliations.jsonl"
_DEFAULT_RECON_COSINE_CUTOFF = 0.40
_DEFAULT_RECON_CONFIDENCE_THRESHOLD = 0.90
_DEFAULT_RECON_TOP_K = 10
_DEFAULT_RECON_ENABLED = False


def _is_index_already_exists_error(exc: Exception) -> bool:
    """Return True if ``exc`` is a FalkorDB 'index already exists' error.

    FalkorDB reports idempotency violations with varying messages:
    "Index already exists", "Attribute '...' is already indexed", etc.
    """
    msg = str(exc).lower()
    return "already exists" in msg or "already indexed" in msg


def _serialize_embedding(embedding: list[float]) -> str:
    """Serialize an embedding vector for FalkorDB storage."""
    return json.dumps([float(x) for x in embedding])


def _append_set(query: str, set_clause: str) -> str:
    """Append a SET clause to a Cypher MERGE query, handling the SET keyword."""
    if " SET " in query:
        return query + ", " + set_clause
    return query + " SET " + set_clause


class FalkorDBBackend:
    """FalkorDB graph database backend.

    Connection is lazy: the FalkorDB client and graph handle are built on
    first use (``_get_graph``/``_get_db``) and rebuilt on transient errors
    (``_invalidate_connection``), so a Redis/FalkorDB restart mid-run does
    not poison the cached backend.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        graph_name: str | None = None,
        merge_mode: MergeMode | str | None = None,
        *,
        recon_enabled: bool | None = None,
        reconciliations_log_path: str | Path | None = None,
        recon_cosine_cutoff: float | None = None,
        recon_confidence_threshold: float | None = None,
        recon_top_k: int | None = None,
        llm_model: str | None = None,
        embedding_model: str | None = None,
        api_base: str | None = None,
        embedding_api_base: str | None = None,
        embedding_api_key: str | None = None,
        embedding_dim: int | None = None,
        allowed_graphs: list[str] | None = None,
    ) -> None:
        self._host = host or os.getenv("FALKORDB_HOST", _DEFAULT_HOST)
        self._port = port or int(os.getenv("FALKORDB_PORT", str(_DEFAULT_PORT)))
        self._graph_name = graph_name or os.getenv("FALKORDB_GRAPH", _DEFAULT_GRAPH)
        # ``None`` means unrestricted (CLI / default path); the Chainlit UI sets
        # this to enforce the user's checkbox selection.
        self._allowed_graphs: list[str] | None = list(allowed_graphs) if allowed_graphs else None
        self._db: FalkorDB | None = None
        self._graph: Any = None

        # Merge mode: explicit arg > MERGE_MODE env > default.
        if isinstance(merge_mode, MergeMode):
            self._merge_mode = merge_mode
        else:
            mode_str = merge_mode or os.getenv("MERGE_MODE", _DEFAULT_MERGE_MODE.value)
            self._merge_mode = MergeMode(mode_str)

        # Reconciliation log path: explicit arg > RECONCILIATIONS_LOG env > default.
        recon_log_str = reconciliations_log_path or os.getenv(
            "RECONCILIATIONS_LOG", _DEFAULT_RECONCILIATIONS_LOG
        )
        self._reconciliations_log_path = Path(recon_log_str)

        # Reconciliation enabled: explicit arg > RECON_ENABLE env > default.
        if recon_enabled is not None:
            self._recon_enabled = recon_enabled
        else:
            self._recon_enabled = os.getenv(
                "RECON_ENABLE", str(_DEFAULT_RECON_ENABLED)
            ).lower() in ("true", "1", "yes")

        self._recon_cosine_cutoff = float(
            recon_cosine_cutoff
            if recon_cosine_cutoff is not None
            else os.getenv("RECON_COSINE_CUTOFF", _DEFAULT_RECON_COSINE_CUTOFF)
        )
        self._recon_confidence_threshold = float(
            recon_confidence_threshold
            if recon_confidence_threshold is not None
            else os.getenv(
                "RECON_CONFIDENCE_THRESHOLD", _DEFAULT_RECON_CONFIDENCE_THRESHOLD
            )
        )
        self._recon_top_k = int(
            recon_top_k
            if recon_top_k is not None
            else os.getenv("RECON_TOP_K", _DEFAULT_RECON_TOP_K)
        )

        self._llm_model = llm_model or os.getenv("LLM_MODEL")
        self._embedding_model = embedding_model or os.getenv("EMBEDDING_MODEL")
        self._api_base = api_base or os.getenv("OLLAMA_API_BASE")
        self._embedding_api_base = embedding_api_base or os.getenv(
            "EMBEDDING_API_BASE", _DEFAULT_EMBEDDING_API_BASE
        )
        self._embedding_api_key = embedding_api_key or os.getenv("EMBEDDING_API_KEY")
        env_dim = os.getenv("VECTOR_DIM")
        if embedding_dim is not None:
            self._embedding_dim: int | None = int(embedding_dim)
        elif env_dim is not None:
            self._embedding_dim = int(env_dim)
        else:
            # Defer: probe the embedding model on first use.
            self._embedding_dim = None

    def _get_graph(self) -> Any:
        """Return the live FalkorDB graph handle, (re)connecting as needed."""
        if self._graph is None:
            if self._db is None:
                logger.debug(
                    "FalkorDB connecting to {}:{} graph='{}'",
                    self._host, self._port, self._graph_name,
                )
                self._db = FalkorDB(host=self._host, port=self._port)
            self._graph = self._db.select_graph(self._graph_name)
        return self._graph

    def _invalidate_connection(self) -> None:
        """Drop the cached handles so the next ``_get_graph`` reconnects."""
        self._graph = None
        self._db = None

    def _get_db(self) -> FalkorDB:
        """Return the live FalkorDB client, (re)connecting as needed.

        For DB-level commands (e.g. ``list_graphs``) that don't target a
        specific graph. Reuses the same client instance ``_get_graph``
        populates, so the two stay consistent.
        """
        if self._db is None:
            logger.debug(
                "FalkorDB connecting to {}:{} (for DB-level command)",
                self._host, self._port,
            )
            self._db = FalkorDB(host=self._host, port=self._port)
            self._graph = None
        return self._db

    def _query(self, query: str, params: dict[str, Any] | None = None) -> Any:
        """Run a Cypher query, invalidating the handle on a transient error."""
        try:
            return self._get_graph().query(query, params or {})
        except Exception as exc:
            if _is_index_already_exists_error(exc):
                raise
            from knowledge.retry import is_transient

            if is_transient(exc):
                logger.debug(
                    "Transient query error — invalidating FalkorDB handle: {}", exc
                )
                self._invalidate_connection()
            raise

    async def _resolve_embedding_dim(self) -> int:
        """Return the effective embedding dimension, probing the model if needed.

        When ``_embedding_dim`` is already set (explicit arg, ``VECTOR_DIM``
        env, or a cached probe result), return it directly. Otherwise probe
        the configured embedding model once via :func:`embed_description`
        and cache the result so subsequent calls are free.
        """
        if self._embedding_dim is not None:
            return self._embedding_dim
        probe = await embed_description(
            "dimension probe",
            model=self._embedding_model,
            api_base=self._embedding_api_base,
            api_key=self._embedding_api_key,
        )
        self._embedding_dim = len(probe)
        return self._embedding_dim

    @property
    def graph_name(self) -> str:
        return self._graph_name

    @property
    def allowed_graphs(self) -> list[str] | None:
        """Return the graph-name allowlist, or ``None`` for unrestricted."""
        return list(self._allowed_graphs) if self._allowed_graphs is not None else None

    def list_graphs(self) -> list[str]:
        """Return all graph names known to the FalkorDB instance.

        Issues ``GRAPH.LIST`` against the DB-level client (not a specific
        graph). The connection is established lazily via :meth:`_get_db`.
        """
        return list(self._get_db().list_graphs())

    def set_active_graph(self, name: str) -> None:
        """Switch the backend's bound graph to ``name``.

        Validates ``name`` against :attr:`allowed_graphs` when one is set
        (enforced — raises ``ValueError`` for an out-of-allowlist name),
        then records the new name and invalidates the cached graph handle
        so the next ``_get_graph`` reselects it. The underlying
        :class:`FalkorDB` client connection is reused (no reconnect).
        """
        if not name or not isinstance(name, str):
            raise ValueError(f"Graph name must be a non-empty string, got {name!r}")
        if self._allowed_graphs is not None and name not in self._allowed_graphs:
            raise ValueError(
                f"Graph '{name}' is not in the allowed set "
                f"({self._allowed_graphs}). The user has not enabled this "
                f"knowledge graph for this session."
            )
        if name == self._graph_name and self._graph is not None:
            return  # no-op: already bound and handle is live
        self._graph_name = name
        # Drop the cached handle so the next _get_graph reselects the new
        # graph name on the existing client (self._db is retained).
        self._graph = None
        logger.debug("FalkorDB active graph switched to '{}'", name)

    def create_graph(self, name: str) -> None:
        """Create a new empty knowledge graph named ``name`` on the instance.

        FalkorDB materializes a graph lazily on first write, so this runs a
        self-deleting seed-node transaction to register the name in
        ``GRAPH.LIST`` while leaving the graph empty. Validates ``name``
        (non-empty, not already in ``GRAPH.LIST``) and, when an allowlist is
        configured, appends the new graph so ``use_graph`` can target it.
        Does NOT switch the active graph — follow with ``set_active_graph``.
        """
        if not name or not isinstance(name, str):
            raise ValueError(f"Graph name must be a non-empty string, got {name!r}")

        existing = self.list_graphs()
        if name in existing:
            raise ValueError(
                f"Graph '{name}' already exists on the FalkorDB instance. "
                f"Use set_active_graph('{name}') to switch to it instead."
            )

        # Select the new graph on the existing DB client and run a minimal
        # write that materializes the graph in GRAPH.LIST. The seed node is
        # created and deleted in one transaction so the graph stays empty.
        db = self._get_db()
        graph = db.select_graph(name)
        graph.query(
            "CREATE (n:_SchemaSeed {created_at: $created_at}) "
            "DELETE n",
            {"created_at": utc_now_iso()},
        )
        logger.info("Created new empty FalkorDB graph '{}'", name)

        # Expand the allowlist so use_graph can target the new graph when
        # the backend is allowlist-restricted (Chainlit UI path).
        if self._allowed_graphs is not None and name not in self._allowed_graphs:
            self._allowed_graphs.append(name)

    @property
    def merge_mode(self) -> MergeMode:
        return self._merge_mode

    @property
    def recon_enabled(self) -> bool:
        return self._recon_enabled

    @property
    def reconciliations_log_path(self) -> Path:
        return self._reconciliations_log_path

    @property
    def recon_cosine_cutoff(self) -> float:
        return self._recon_cosine_cutoff

    @property
    def recon_confidence_threshold(self) -> float:
        return self._recon_confidence_threshold

    @property
    def recon_top_k(self) -> int:
        return self._recon_top_k

    @property
    def embedding_api_base(self) -> str:
        return self._embedding_api_base

    @property
    def embedding_api_key(self) -> str | None:
        return self._embedding_api_key

    def execute(self, query: str, params: dict[str, Any] | None = None) -> Any:
        """Execute a Cypher query and return the result."""
        logger.debug("Cypher execute | {}", query)
        return self._query(query, params)

    async def write_extraction(
        self,
        graph: FactoryPlanningGraph,
        *,
        source: str | None = None,
        chunk_index: int | None = None,
    ) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
        """Write an extraction result to the database.

        Overwrite mode upserts every entity (last-write-wins). Conflict mode
        fetches each node first and records disagreements in-graph
        (first-writer-wins; see :func:`build_conflict_merge`). When
        ``recon_enabled`` is set, plain-name Resources are tested for
        similarity against indexed ones (see :mod:`knowledge.reconciliation`).
        Resource descriptions are always LLM-coalesced on a name-match merge
        and the embedding is re-persisted.

        Returns ``(statement_count, conflicts, reconciliations)``.
        """
        all_conflicts: list[dict[str, Any]] = []
        all_reconciliations: list[dict[str, Any]] = []
        statements_run = 0

        # Ensure the vector index exists once before any embedding work.
        if self._recon_enabled:
            dim = await self._resolve_embedding_dim()
            self.ensure_vector_index(dim=dim)

        rel_statements, _node_entries = extraction_to_cypher_with_mode(
            graph, self._merge_mode, source=source, chunk_index=chunk_index
        )

        # Per-entity pass: fetch → coalesce → embed → write → maybe reconcile.
        # Both modes share this shape; only the write builder differs.
        for entity, label in _iter_entities(graph):
            fetch_q, fetch_p = model_to_cypher_fetch(entity, label)
            existing_props = self._fetch_node_props(fetch_q, fetch_p)
            is_new_node = not existing_props

            coalesced = await self._maybe_coalesce(entity, label, existing_props)

            if self._merge_mode is MergeMode.CONFLICT:
                write_q, write_p, conflicts = build_conflict_merge(
                    entity,
                    label,
                    existing_props,
                    source=source,
                    chunk_index=chunk_index,
                    coalesced_values=coalesced,
                )
            else:
                write_q, write_p = model_to_cypher_merge(entity, label)
                conflicts = []
                if coalesced:
                    write_p["p_description"] = coalesced["description"]
                    write_q = _append_set(write_q, "n.description = $p_description")

            embedding = await self._maybe_write_embedding(
                entity, label, existing_props, coalesced
            )
            if embedding:
                write_p["p_embedding"] = _serialize_embedding(embedding)
                write_q = _append_set(write_q, "n.embedding = $p_embedding")

            self._query(write_q, write_p)
            statements_run += 1
            if conflicts:
                all_conflicts.extend(conflicts)

            # Reconcile new plain-name Resources against indexed ones.
            if (
                self._recon_enabled
                and is_new_node
                and label == "Resource"
                and isinstance(entity, Resource)
                and not entity.name_has_index
            ):
                recon_record = await self._maybe_reconcile(
                    entity, source=source, chunk_index=chunk_index
                )
                if recon_record:
                    all_reconciliations.append(recon_record)

        # Relationship pass.
        for query, params in rel_statements:
            self._query(query, params)
            statements_run += 1

        if all_reconciliations:
            self._append_reconciliations_log(all_reconciliations)

        return statements_run, all_conflicts, all_reconciliations

    async def _maybe_coalesce(
        self,
        entity: BaseModel,
        label: str,
        existing_props: dict[str, Any],
    ) -> dict[str, Any]:
        """Return coalesced values for special fields (description) on name-match.

        Only applies when the node already exists (name match). Returns a dict
        like ``{"description": coalesced_text}`` or an empty dict.
        """
        if label != "Resource" or not existing_props:
            return {}
        if not isinstance(entity, Resource):
            return {}
        existing_desc = existing_props.get("description")
        incoming_desc = entity.description
        if not existing_desc or not incoming_desc:
            return {}
        if existing_desc == incoming_desc:
            return {}
        logger.debug(
            "Coalescing descriptions for '{}' | existing={!r} | incoming={!r}",
            entity.name,
            existing_desc,
            incoming_desc,
        )
        coalesced = await coalesce_description(
            str(existing_desc),
            incoming_desc,
            model=self._llm_model,
            api_base=self._api_base,
        )
        logger.debug("Coalesced description for '{}': {!r}", entity.name, coalesced)
        return {"description": coalesced}

    async def _maybe_write_embedding(
        self,
        entity: BaseModel,
        label: str,
        existing_props: dict[str, Any],
        coalesced: dict[str, Any],
    ) -> list[float] | None:
        """Compute and return the embedding to persist on a Resource node.

        Returns the embedding vector (to be SET on the node) or None when no
        embedding is needed. Re-embeds when the description was coalesced.
        """
        if label != "Resource" or not isinstance(entity, Resource):
            return None
        desc = coalesced.get("description") or entity.description
        if not desc:
            return None
        try:
            logger.debug("Embedding description for '{}'", entity.name)
            embedding = await embed_description(
                desc,
                model=self._embedding_model,
                api_base=self._embedding_api_base,
                api_key=self._embedding_api_key,
            )
            logger.debug(
                "Embedding complete for '{}' | dim={}", entity.name, len(embedding)
            )
            return embedding
        except Exception as exc:
            logger.warning("Embedding failed for {}: {}", entity.name, exc)
            return None

    async def _maybe_reconcile(
        self,
        entity: Resource,
        *,
        source: str | None = None,
        chunk_index: int | None = None,
    ) -> dict[str, Any] | None:
        """Run reconciliation for a new plain-name Resource.

        Returns the reconciliation record dict if a link was created, else
        None.
        """
        try:
            embedding = await embed_description(
                entity.description,
                model=self._embedding_model,
                api_base=self._embedding_api_base,
                api_key=self._embedding_api_key,
            )
        except Exception as exc:
            logger.warning("Reconciliation embedding failed for {}: {}", entity.name, exc)
            return None

        logger.debug("Running reconciliation for plain-name '{}'", entity.name)
        decision = await reconcile_new_node(
            self,
            entity,
            embedding=embedding,
            cosine_cutoff=self._recon_cosine_cutoff,
            confidence_threshold=self._recon_confidence_threshold,
            top_k=self._recon_top_k,
            llm_model=self._llm_model,
            api_base=self._api_base,
            source=source,
            chunk_index=chunk_index,
        )
        if decision.linked and decision.record:
            logger.debug(
                "Reconciliation linked '{}' -> '{}'",
                entity.name,
                decision.matched_name,
            )
            self._write_reconciliation_link(decision, source=source, chunk_index=chunk_index)
            return decision.record
        logger.debug("No reconciliation match for '{}'", entity.name)
        return None

    def _write_reconciliation_link(
        self,
        decision: ReconciliationDecision,
        *,
        source: str | None = None,
        chunk_index: int | None = None,
    ) -> None:
        """Execute the POSSIBLE_DUPLICATE_OF edge + alias/canonical write."""
        if not decision.record or not decision.matched_name:
            return
        query, params = build_reconciliation_link_cypher(
            decision.record["new_name"],
            decision.matched_name,
            cosine=decision.cosine_similarity or 0.0,
            confidence=decision.llm_confidence or 0.0,
            detected_at=decision.record["detected_at"],
            source=source,
            chunk_index=chunk_index,
        )
        self._query(query, params)

    async def reconcile_posthoc(self) -> list[dict[str, Any]]:
        """Post-hoc reconciliation pass over existing plain-name Resources.

        Scans all Resource nodes with ``name_has_index=false`` that do not yet
        have an outgoing ``POSSIBLE_DUPLICATE_OF``, embeds each, and runs the
        reconciliation pipeline. Writes links + appends to the jsonl log.

        Returns the list of reconciliation records created.
        """
        dim = await self._resolve_embedding_dim()
        self.ensure_vector_index(dim=dim)
        decisions = await reconcile_existing_plain_nodes(
            self,
            cosine_cutoff=self._recon_cosine_cutoff,
            confidence_threshold=self._recon_confidence_threshold,
            top_k=self._recon_top_k,
            llm_model=self._llm_model,
            api_base=self._api_base,
            embedding_api_base=self._embedding_api_base,
            embedding_api_key=self._embedding_api_key,
        )
        records: list[dict[str, Any]] = []
        for decision in decisions:
            if decision.linked and decision.record:
                self._write_reconciliation_link(decision)
                records.append(decision.record)
        if records:
            self._append_reconciliations_log(records)
        return records

    def _fetch_node_props(
        self, query: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Run a read-only MATCH and return the matched node's properties.

        Returns an empty dict when the node does not yet exist.
        """
        result = self._query(query, params)
        rows = result.result_set if result.result_set else []
        if not rows:
            return {}
        node = rows[0][0]
        if node is None:
            return {}
        props = dict(node.properties) if hasattr(node, "properties") else {}
        return props

    def _append_reconciliations_log(self, records: list[dict[str, Any]]) -> None:
        """Append reconciliation records to the JSONL log file (create if needed)."""
        self._reconciliations_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._reconciliations_log_path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, default=str))
                fh.write("\n")

    def get_reconciliations(self, label: str | None = None) -> list[dict[str, Any]]:
        """Return all ``POSSIBLE_DUPLICATE_OF`` edges with their properties.

        Each row is ``{plain_name, indexed_name, labels, cosine_similarity,
        llm_confidence, detected_at, source, chunk_index}``. When ``label``
        is given, restricts to edges originating from that label.
        """
        label_clause = f":{label}" if label else ""
        cypher = (
            f"MATCH (a{label_clause})-[r:POSSIBLE_DUPLICATE_OF]->(b) "
            f"RETURN a.name AS plain_name, b.name AS indexed_name, "
            f"labels(a) AS labels, r.cosine_similarity AS cosine_similarity, "
            f"r.llm_confidence AS llm_confidence, r.detected_at AS detected_at, "
            f"r.source AS source, r.chunk_index AS chunk_index"
        )
        result = self._query(cypher)
        rows: list[dict[str, Any]] = []
        for row in result.result_set or []:
            (plain_name, indexed_name, labels, cosine, confidence,
             detected_at, source, chunk_index) = row
            rows.append({
                "plain_name": str(plain_name or ""),
                "indexed_name": str(indexed_name or ""),
                "labels": list(labels) if labels else [],
                "cosine_similarity": float(cosine) if cosine is not None else None,
                "llm_confidence": float(confidence) if confidence is not None else None,
                "detected_at": str(detected_at or ""),
                "source": source,
                "chunk_index": chunk_index,
            })
        return rows

    def clear_reconciliations(
        self, label: str | None = None, plain_name: str | None = None
    ) -> int:
        """Dismiss reviewed reconciliation links by deleting the edge.

        Also removes the ``canonical_name`` from the plain node and the alias
        from the indexed node. Returns the number of edges deleted.
        """
        label_clause = f":{label}" if label else ""
        params: dict[str, Any] = {}
        where = ""
        if plain_name:
            params["plain_name"] = plain_name
            where = " WHERE a.name = $plain_name"
        cypher = (
            f"MATCH (a{label_clause})-[r:POSSIBLE_DUPLICATE_OF]->(b)"
            f"{where} "
            f"DELETE r "
            f"SET a.canonical_name = null, "
            f"b.aliases = [x IN coalesce(b.aliases, []) WHERE x <> a.name]"
        )
        result = self._query(cypher, params)
        if hasattr(result, "statistics"):
            stats = result.statistics
            for key in ("relationships_deleted",):
                if hasattr(stats, key):
                    return int(getattr(stats, key) or 0)
        return 0

    def reset(self) -> None:
        """Delete all nodes and relationships."""
        self._query("MATCH (n) DETACH DELETE n")

    def node_count(self) -> int:
        """Return the total number of nodes in the graph."""
        result = self._query("MATCH (n) RETURN count(n) AS cnt")
        return result.result_set[0][0] if result.result_set else 0

    def get_all_nodes(self) -> list[dict[str, Any]]:
        """Return all nodes as dicts with labels and properties."""
        result = self._query("MATCH (n) RETURN n")
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
        result = self._query(
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
        labels_result = self._query(
            "CALL db.labels() YIELD label RETURN collect(label)"
        )
        labels = labels_result.result_set[0][0] if labels_result.result_set else []

        rel_result = self._query(
            "CALL db.relationshipTypes() YIELD relationshipType RETURN collect(relationshipType)"
        )
        rel_types = rel_result.result_set[0][0] if rel_result.result_set else []

        prop_result = self._query(
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
        ("Index already exists" / "Attribute already indexed"), which we
        treat as success.
        """
        prop_list = ", ".join(f"'{p}'" for p in properties)
        cypher = f"CALL db.idx.fulltext.createNodeIndex('{label}', {prop_list})"
        try:
            self._query(cypher)
        except Exception as exc:
            if _is_index_already_exists_error(exc):
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
        result = self._query(
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

        Idempotent: existing indexes are reported by FalkorDB as an error
        ("Index already exists" / "Attribute already indexed"), which we
        treat as success.
        """
        cypher = (
            f"CREATE VECTOR INDEX FOR (n:{label}) ON (n.{property}) "
            f"OPTIONS {{dimension:{int(dim)}, "
            f"similarityFunction:'{similarity_function}'}}"
        )
        try:
            self._query(cypher)
        except Exception as exc:
            if _is_index_already_exists_error(exc):
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
        result = self._query(cypher)
        rows: list[dict[str, Any]] = []
        for row in result.result_set or []:
            node, score = row[0], row[1]
            props = dict(node.properties) if hasattr(node, "properties") else {}
            labels = list(node.labels) if hasattr(node, "labels") else []
            props["_labels"] = labels
            props["_score"] = score
            rows.append(props)
        return rows
