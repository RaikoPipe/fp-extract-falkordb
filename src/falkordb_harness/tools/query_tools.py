"""Tools for querying the knowledge graph."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from falkordb_harness.backend import get_searcher
from falkordb_harness.tools._retry import awith_retry, with_retry


@tool
def cypher_query(cypher: str) -> str:
    """Execute a raw Cypher query against the FalkorDB knowledge graph.

    Use this when you know the exact Cypher statement to run.
    Returns the result rows as JSON.
    """
    return with_retry(lambda: _cypher_query_impl(cypher))


def _cypher_query_impl(cypher: str) -> str:
    rows = get_searcher().cypher_query(cypher)
    return json.dumps(
        [str(r) for r in rows], ensure_ascii=False, default=str
    )


@tool
async def nl_query(question: str) -> str:
    """Answer a natural-language question about the knowledge graph.

    Translates the question to Cypher, executes it, and returns a
    summarized answer. Use this for open-ended questions instead of
    writing Cypher manually.
    """
    return await awith_retry(lambda: get_searcher().natural_language_query(question))


@tool
def fulltext_search(query: str, label: str = "Resource", k: int = 10) -> str:
    """Run a full-text search over node properties.

    Searches nodes with the given label using RediSearch full-text indexing.
    Returns up to k matching nodes with relevance scores.
    """
    return with_retry(lambda: _fulltext_search_impl(query, label, k))


def _fulltext_search_impl(query: str, label: str, k: int) -> str:
    results = get_searcher().fulltext_search(query)
    return json.dumps(results[:k], indent=2, ensure_ascii=False, default=str)


@tool
async def vector_search(query: str, k: int = 10) -> str:
    """Run a vector similarity search using an embedding of the query.

    Embeds the query text and finds the nearest neighbours in the graph's
    vector index. Returns up to k matching nodes with similarity scores.
    """
    return await awith_retry(lambda: _vector_search_impl(query, k))


async def _vector_search_impl(query: str, k: int) -> str:
    results = await get_searcher().vector_search(query)
    return json.dumps(results[:k], indent=2, ensure_ascii=False, default=str)
