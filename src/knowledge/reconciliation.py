"""Similarity-based reconciliation for plain-name resource nodes.

Before inserting a new resource whose name does not match an existing node by
exact name, determine whether it is a duplicate of an indexed resource by:

1. Embedding the new node's description and running a cosine similarity search
   against all ``Resource`` nodes where ``name_has_index=true``.
2. Applying a cosine cutoff (default 0.70) to keep only the top-k candidates.
3. Running an LLM pairwise comparison of the description + properties of the
   new node against each surviving candidate, producing a confidence factor.
4. Picking the candidate with the highest confidence. If that confidence is
   above the threshold (default 0.90), the new node is inserted as a distinct
   node with a ``POSSIBLE_DUPLICATE_OF`` reference to the matched node, an
   alias on the indexed node, and a canonical name on the plain node — all
   documented in ``reconciliations.jsonl`` for human review.

A post-hoc pass (:func:`reconcile_existing_plain_nodes`) iterates existing
plain-name resource nodes and runs the same pipeline, so plain names ingested
before their indexed counterpart arrived can be reconciled later.

This module also provides :func:`coalesce_description`, which merges an
existing description with a newly discovered one via an LLM call. Coalescing
runs on every name-match merge (both merge modes) and re-embeds the node so
the cosine search stays accurate as descriptions evolve.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from knowledge._clients import chat_client, embedding_client
from knowledge.graph_models.factory_graph_model import Resource


_DEFAULT_LLM_MODEL = "qwen3.5:122b-a10b"
_DEFAULT_EMBEDDING_MODEL = "bge-m3"
_DEFAULT_EMBEDDING_DIM = 1024
_DEFAULT_EMBEDDING_API_BASE = "http://localhost:11434"

_DEFAULT_COSINE_CUTOFF = 0.70
_DEFAULT_CONFIDENCE_THRESHOLD = 0.90
_DEFAULT_TOP_K = 10

_RESOURCE_LABEL = "Resource"
_EMBEDDING_PROPERTY = "embedding"


_COALESCE_SYSTEM = """\
You are a manufacturing domain expert. You are given two descriptions of the \
same factory resource, produced from different source documents. Produce a \
single, coalesced description that preserves ALL information from both \
descriptions, resolves contradictions by keeping the most specific/sourced \
value, and reads as a coherent paragraph. Return ONLY the coalesced \
description text, no commentary.
"""

_PAIRWISE_SYSTEM = """\
You are a manufacturing domain expert. You are given two resource \
descriptions along with their properties. Determine whether they refer to \
the same physical resource.

Return ONLY a single JSON object: {"confidence": <float between 0 and 1>}
where 1.0 means "definitely the same resource" and 0.0 means "definitely \
different resources". Consider the description content, resource type, and \
all provided properties. Use resource_type as a strong tie-breaker signal: \
if the resource_type values differ (e.g. 'machine' vs 'workstation'), \
lower the confidence unless the descriptions clearly describe the same \
physical entity. No markdown fences, no commentary.
"""


@dataclass
class Candidate:
    """One cosine-similarity candidate for LLM pairwise comparison."""

    name: str
    properties: dict[str, Any]
    cosine_similarity: float


@dataclass
class ReconciliationDecision:
    """Result of reconciling a single plain-name node.

    - ``linked`` is True when a match above the confidence threshold was found.
    - ``matched_name`` is the indexed node name when linked, else None.
    - ``cosine_similarity`` / ``llm_confidence`` are the scores of the best
      candidate (or None when no candidate passed the cosine cutoff).
    - ``record`` is the jsonl-ready dict (present when linked, else None).
    """

    linked: bool
    matched_name: str | None = None
    cosine_similarity: float | None = None
    llm_confidence: float | None = None
    record: dict[str, Any] | None = None
    candidates: list[Candidate] = field(default_factory=list)


async def embed_description(
    description: str,
    *,
    model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
) -> list[float]:
    """Embed ``description`` and return a list of floats.

    Uses the **embedding** provider config (``EMBEDDING_MODEL``,
    ``EMBEDDING_API_BASE``, ``EMBEDDING_API_KEY``), which is independent from
    the LLM chat-completion provider — the LLM may run on a cloud endpoint
    that does not expose ``/api/embed``.

    ``api_base`` / ``api_key`` are accepted for backward-compatibility but
    ignored — the embedding client (:func:`knowledge._clients.embedding_client`)
    carries the base URL and API key derived from the env vars above.
    """
    embedding_model = model or os.getenv("EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL)
    response = await embedding_client().embeddings.create(
        model=embedding_model,
        input=description,
    )
    return list(response.data[0].embedding)


async def detect_embedding_dim(
    *,
    model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
) -> int:
    """Probe the configured embedding model and return its output dimension.

    Embeds a short probe string once and returns ``len(embedding)``. This lets
    callers create vector indexes with the correct dimension without relying
    on a hardcoded default or an explicit ``VECTOR_DIM`` env var.
    """
    embedding = await embed_description(
        "dimension probe", model=model, api_base=api_base, api_key=api_key
    )
    return len(embedding)


async def coalesce_description(
    existing: str,
    incoming: str,
    *,
    model: str | None = None,
    api_base: str | None = None,
) -> str:
    """Coalesce two descriptions of the same resource via an LLM call.

    Returns the merged description. When either side is empty, returns the
    other side unchanged (no LLM call needed).

    ``api_base`` is accepted for backward-compatibility but ignored — the
    chat client (:func:`knowledge._clients.chat_client`) carries the base
    URL and API key.
    """
    if not existing and not incoming:
        return ""
    if not existing:
        return incoming
    if not incoming:
        return existing
    if existing == incoming:
        return existing

    llm_model = model or os.getenv("LLM_MODEL", _DEFAULT_LLM_MODEL)
    messages = [
        {"role": "system", "content": _COALESCE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## Existing description\n\n{existing}\n\n"
                f"## New description\n\n{incoming}\n\n"
                "Produce the coalesced description."
            ),
        },
    ]
    response = await chat_client().chat.completions.create(
        model=llm_model,
        messages=messages,  # type: ignore[arg-type]
        temperature=0.0,
    )
    return (response.choices[0].message.content or "").strip()


async def llm_pairwise_confidence(
    new_node: Resource,
    candidate_props: dict[str, Any],
    *,
    model: str | None = None,
    api_base: str | None = None,
) -> float:
    """Compare a new node against one candidate via LLM; return 0–1 confidence.

    ``candidate_props`` is the indexed node's properties dict (as returned by
    ``_fetch_node_props`` or the vector-search row).

    ``api_base`` is accepted for backward-compatibility but ignored — the
    chat client (:func:`knowledge._clients.chat_client`) carries the base
    URL and API key.
    """
    import json as _json

    llm_model = model or os.getenv("LLM_MODEL", _DEFAULT_LLM_MODEL)

    new_props = new_node.model_dump(exclude_none=True)
    candidate_summary = {k: v for k, v in candidate_props.items() if v is not None}

    messages = [
        {"role": "system", "content": _PAIRWISE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## New node\n\n"
                f"{_json.dumps(new_props, indent=2, default=str, ensure_ascii=False)}\n\n"
                f"## Existing candidate\n\n"
                f"{_json.dumps(candidate_summary, indent=2, default=str, ensure_ascii=False)}\n\n"
                "Return the confidence JSON."
            ),
        },
    ]
    response = await chat_client().chat.completions.create(
        model=llm_model,
        messages=messages,  # type: ignore[arg-type]
        temperature=0.0,
    )
    raw = (response.choices[0].message.content or "").strip()

    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)

    try:
        parsed = _json.loads(raw)
        conf = float(parsed.get("confidence", 0.0))
    except Exception:
        conf = 0.0
    return max(0.0, min(1.0, conf))


def _build_record(
    new_name: str,
    matched_name: str,
    cosine: float,
    confidence: float,
    *,
    source: str | None,
    chunk_index: int | None,
    detected_at: str,
) -> dict[str, Any]:
    return {
        "new_name": new_name,
        "matched_name": matched_name,
        "matched_label": _RESOURCE_LABEL,
        "cosine_similarity": round(cosine, 4),
        "llm_confidence": round(confidence, 4),
        "source": source,
        "chunk_index": chunk_index,
        "detected_at": detected_at,
    }


async def reconcile_new_node(
    backend: Any,
    new_node: Resource,
    *,
    embedding: list[float],
    cosine_cutoff: float = _DEFAULT_COSINE_CUTOFF,
    confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
    top_k: int = _DEFAULT_TOP_K,
    llm_model: str | None = None,
    api_base: str | None = None,
    source: str | None = None,
    chunk_index: int | None = None,
) -> ReconciliationDecision:
    """Run the full reconciliation pipeline for one new plain-name resource.

    ``backend`` must expose ``vector_search`` (see FalkorDBBackend) and is used
    only for the cosine candidate lookup. The LLM pairwise calls use the
    chat client (:func:`knowledge._clients.chat_client`) directly.

    Returns a :class:`ReconciliationDecision`.
    """
    from datetime import datetime, timezone

    rows = backend.vector_search(
        embedding,
        label=_RESOURCE_LABEL,
        property=_EMBEDDING_PROPERTY,
        k=top_k,
    )

    candidates: list[Candidate] = []
    for row in rows:
        score = float(row.get("_score", 0.0))
        if score < cosine_cutoff:
            continue
        if str(row.get("name", "")).lower() == new_node.name.lower():
            continue
        candidates.append(Candidate(
            name=str(row.get("name", "")),
            properties={k: v for k, v in row.items() if k not in ("_labels", "_score")},
            cosine_similarity=score,
        ))

    if not candidates:
        return ReconciliationDecision(linked=False, candidates=[])

    best: tuple[float, Candidate] | None = None
    for cand in candidates:
        confidence = await llm_pairwise_confidence(
            new_node,
            cand.properties,
            model=llm_model,
            api_base=api_base,
        )
        if best is None or confidence > best[0]:
            best = (confidence, cand)

    if best is None:
        return ReconciliationDecision(linked=False, candidates=candidates)

    confidence, cand = best
    if confidence < confidence_threshold:
        return ReconciliationDecision(
            linked=False,
            matched_name=cand.name,
            cosine_similarity=cand.cosine_similarity,
            llm_confidence=confidence,
            candidates=candidates,
        )

    detected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    record = _build_record(
        new_node.name,
        cand.name,
        cand.cosine_similarity,
        confidence,
        source=source,
        chunk_index=chunk_index,
        detected_at=detected_at,
    )
    return ReconciliationDecision(
        linked=True,
        matched_name=cand.name,
        cosine_similarity=cand.cosine_similarity,
        llm_confidence=confidence,
        record=record,
        candidates=candidates,
    )


async def reconcile_existing_plain_nodes(
    backend: Any,
    *,
    cosine_cutoff: float = _DEFAULT_COSINE_CUTOFF,
    confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
    top_k: int = _DEFAULT_TOP_K,
    llm_model: str | None = None,
    api_base: str | None = None,
    embedding_api_base: str | None = None,
    embedding_api_key: str | None = None,
) -> list[ReconciliationDecision]:
    """Post-hoc pass: reconcile existing plain-name Resources against indexed ones.

    Iterates all ``Resource`` nodes where ``name_has_index=false`` that do not
    yet have an outgoing ``POSSIBLE_DUPLICATE_OF`` relationship, embeds each
    node's description, and runs the same pipeline as
    :func:`reconcile_new_node`. Returns the list of decisions where a link
    was found (``linked=True``); the caller writes the edges + jsonl.

    ``backend`` must expose ``vector_search`` and ``execute`` (for the MATCH
    query to discover plain-name nodes).

    ``api_base`` is accepted for backward-compatibility but ignored — the
    chat client carries the base URL. ``embedding_api_base`` /
    ``embedding_api_key`` are the embedding provider config, independent from
    the LLM provider.
    """
    cypher = (
        "MATCH (n:Resource) "
        "WHERE n.name_has_index = false "
        "AND NOT (n)-[:POSSIBLE_DUPLICATE_OF]->() "
        "RETURN n"
    )
    result = backend.execute(cypher)
    rows = result.result_set if result.result_set else []

    linked: list[ReconciliationDecision] = []
    for row in rows:
        node = row[0] if isinstance(row, list) or isinstance(row, tuple) else row
        props = dict(node.properties) if hasattr(node, "properties") else {}
        name = props.get("name")
        if not name:
            continue

        resource = Resource(
            name=str(name),
            name_has_index=False,
            description=str(props.get("description", "")),
            resource_type=str(props.get("resource_type", "other")),
        )

        embedding = await embed_description(
            resource.description,
            api_base=embedding_api_base,
            api_key=embedding_api_key,
        )

        decision = await reconcile_new_node(
            backend,
            resource,
            embedding=embedding,
            cosine_cutoff=cosine_cutoff,
            confidence_threshold=confidence_threshold,
            top_k=top_k,
            llm_model=llm_model,
            api_base=api_base,
            source=None,
            chunk_index=None,
        )
        if decision.linked:
            linked.append(decision)

    return linked