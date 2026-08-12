"""Tools for similarity-based reconciliation of plain-name Resource nodes."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend
from falkordb_harness.tools._retry import with_retry


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
def resolve_duplicate(plain_name: str, action: str) -> str:
    """Resolve a POSSIBLE_DUPLICATE_OF link for a plain-name Resource.

    ``action`` is one of:
    - ``"accept"`` — merge the plain node into the indexed node: transfer
      outgoing relationships, copy missing properties, record conflicting
      properties in ``n.conflicts`` on the surviving node, then delete the
      plain node. Warn the user about any conflicts created.
    - ``"reject"`` — dismiss the link (delete the edge, clean up
      canonical_name/aliases). The two nodes remain separate.
    - ``"keep_separate"`` — same graph operation as reject; use when the
      user acknowledges the suggestion but considers the entities distinct.

    Call ``get_reconciliations`` first to list outstanding duplicates, then
    present each one to the user for adjudication. Never batch-accept or
    batch-reject — walk through duplicates one at a time.
    """
    return with_retry(lambda: _resolve_duplicate_impl(plain_name, action))


def _resolve_duplicate_impl(plain_name: str, action: str) -> str:
    backend = get_backend()
    result = backend.resolve_duplicate(plain_name, action)
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)