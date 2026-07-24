"""Tools for similarity-based reconciliation of plain-name Resource nodes."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend
from falkordb_harness.tools._retry import awith_retry, with_retry


@tool
def get_reconciliations(label: str = "") -> str:
    """List ``POSSIBLE_DUPLICATE_OF`` links recorded by the reconciliation step.

    Each row carries ``plain_name``, ``indexed_name``, labels, cosine
    similarity, LLM confidence, detection timestamp, and provenance.
    Optionally filter by the originating node label (e.g. 'Resource').
    """
    return with_retry(lambda: _get_reconciliations_impl(label))


def _get_reconciliations_impl(label: str) -> str:
    backend = get_backend()
    records = backend.get_reconciliations(label=label or None)
    return json.dumps(records, indent=2, ensure_ascii=False, default=str)


@tool
def clear_reconciliations(label: str = "", plain_name: str = "") -> str:
    """Dismiss reviewed reconciliation links by deleting the edge.

    Also removes the ``canonical_name`` from the plain node and the
    alias from the indexed node. Optionally filter by label and/or
    plain entity name. Returns the number of edges deleted.
    """
    return with_retry(lambda: _clear_reconciliations_impl(label, plain_name))


def _clear_reconciliations_impl(label: str, plain_name: str) -> str:
    backend = get_backend()
    count = backend.clear_reconciliations(
        label=label or None,
        plain_name=plain_name or None,
    )
    return f"{count} reconciliation link(s) deleted"


@tool
async def reconcile_posthoc() -> str:
    """Run a post-hoc reconciliation pass over existing plain-name Resources.

    Scans Resource nodes without a distinguishing index that do not yet have
    a ``POSSIBLE_DUPLICATE_OF`` link, embeds each, and runs the two-stage
    (cosine + LLM pairwise confidence) reconciliation pipeline. Writes any new
    links to the graph and the reconciliations JSONL log.

    Use this after ingesting documents whose plain-name Resources arrived
    before their indexed counterparts. Returns the new reconciliation records.
    """
    return await awith_retry(lambda: _reconcile_posthoc_impl())


async def _reconcile_posthoc_impl() -> str:
    backend = get_backend()
    records = await backend.reconcile_posthoc()
    return json.dumps(
        {"new_reconciliations": len(records), "records": records},
        indent=2,
        ensure_ascii=False,
        default=str,
    )