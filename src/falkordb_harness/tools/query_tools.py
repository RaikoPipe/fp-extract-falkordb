"""Tools for querying the knowledge graph."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from falkordb_harness.backend import get_searcher


@tool
def cypher_query(cypher: str) -> str:
    """Execute a raw Cypher query against the FalkorDB knowledge graph.

    Use this when you know the exact Cypher statement to run.
    Returns the result rows as JSON.
    """
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
    return await get_searcher().natural_language_query(question)


@tool
def fulltext_search(query: str, label: str = "Resource", k: int = 10) -> str:
    """Run a full-text search over node properties.

    Searches nodes with the given label using RediSearch full-text indexing.
    Returns up to k matching nodes with relevance scores.
    """
    results = get_searcher().fulltext_search(query)
    return json.dumps(results[:k], indent=2, ensure_ascii=False, default=str)


@tool
async def vector_search(query: str, k: int = 10) -> str:
    """Run a vector similarity search using an embedding of the query.

    Embeds the query text and finds the nearest neighbours in the graph's
    vector index. Returns up to k matching nodes with similarity scores.
    """
    results = await get_searcher().vector_search(query)
    return json.dumps(results[:k], indent=2, ensure_ascii=False, default=str)
