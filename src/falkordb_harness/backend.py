"""Shared backend instances for all tools."""

from __future__ import annotations

import os
from functools import lru_cache

from knowledge.falkordb_backend import FalkorDBBackend
from knowledge.search import GraphSearcher


@lru_cache(maxsize=1)
def get_backend() -> FalkorDBBackend:
    return FalkorDBBackend(
        host=os.getenv("FALKORDB_HOST"),
        port=int(os.getenv("FALKORDB_PORT", "6379")) if os.getenv("FALKORDB_PORT") else None,
        graph_name=os.getenv("FALKORDB_GRAPH"),
        merge_mode=os.getenv("MERGE_MODE"),
        conflicts_log_path=os.getenv("CONFLICTS_LOG"),
    )


@lru_cache(maxsize=1)
def get_searcher() -> GraphSearcher:
    return GraphSearcher(
        get_backend(),
        llm_model=os.getenv("LLM_MODEL"),
        api_base=os.getenv("OLLAMA_BASE_URL"),
    )
