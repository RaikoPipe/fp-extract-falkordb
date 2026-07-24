"""Graph search: raw Cypher, LLM-assisted natural language to Cypher,
full-text search, and vector similarity search."""

from __future__ import annotations

import asyncio
import json
import os

from loguru import logger

from knowledge._clients import chat_client, embedding_client
from knowledge.falkordb_backend import FalkorDBBackend

_DEFAULT_LLM_MODEL = "qwen3.5:122b-a10b"

# Embedding model + dimension. Falls back to the chat model when no
# dedicated embedding model is configured.
_DEFAULT_EMBEDDING_MODEL = "bge-m3"
_DEFAULT_EMBEDDING_DIM = 1024

_NL_TO_CYPHER_SYSTEM = """\
You are a Cypher query expert for FalkorDB (OpenCypher dialect).
Given a graph schema and a natural language question, generate a single \
Cypher query that answers the question.

Rules:
- Return ONLY the Cypher query, no explanation, no markdown fences.
- Use MATCH, RETURN, WHERE, ORDER BY, LIMIT as needed.
- Property values are case-sensitive; use toLower() for case-insensitive matching.
- String list properties are stored as JSON strings; use individual relationships for traversal.
"""

_SUMMARIZE_SYSTEM = """\
You are a helpful assistant. Given query results from a manufacturing \
knowledge graph, provide a clear, concise natural-language answer to the \
user's question. If the results are empty, say so.
"""


class GraphSearcher:
    """Search interface for the FalkorDB knowledge graph.

    Modes:
        - "graph":     raw Cypher or natural-language -> Cypher (default).
        - "fulltext":  RediSearch full-text query over node properties.
        - "vector":    nearest-neighbour vector similarity search using
                       embeddings of the user's query.
    """

    def __init__(
        self,
        backend: FalkorDBBackend,
        llm_model: str | None = None,
        api_base: str | None = None,
        mode: str = "graph",
        fulltext_label: str | None = None,
        fulltext_property: str | None = None,
        vector_label: str | None = None,
        vector_property: str | None = None,
        vector_dim: int | None = None,
        embedding_model: str | None = None,
        vector_k: int = 10,
        fulltext_k: int = 10,
    ) -> None:
        self._backend = backend
        self._llm_model = llm_model or os.getenv("LLM_MODEL", _DEFAULT_LLM_MODEL)
        self._mode = mode
        self._fulltext_label = fulltext_label or os.getenv(
            "FULLTEXT_LABEL", "Resource"
        )
        self._fulltext_property = fulltext_property or os.getenv(
            "FULLTEXT_PROPERTY", "name"
        )
        self._vector_label = vector_label or os.getenv("VECTOR_LABEL", "Resource")
        self._vector_property = vector_property or os.getenv(
            "VECTOR_PROPERTY", "embedding"
        )
        env_dim = os.getenv("VECTOR_DIM")
        if vector_dim is not None:
            self._vector_dim: int | None = int(vector_dim)
        elif env_dim is not None:
            self._vector_dim = int(env_dim)
        else:
            # Defer: probe the embedding model on first vector search.
            self._vector_dim = None
        self._embedding_model = embedding_model or os.getenv(
            "EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL
        )
        self._vector_k = vector_k
        self._fulltext_k = fulltext_k

    async def _resolve_vector_dim(self) -> int:
        """Return the effective vector dim, probing the embedding model if needed.

        When ``_vector_dim`` is already set (explicit arg, ``VECTOR_DIM`` env,
        or a cached probe result), return it directly. Otherwise embed a short
        probe string once via the configured embedding model and cache the
        length so subsequent calls are free.
        """
        if self._vector_dim is not None:
            return self._vector_dim
        probe = await self._embed("dimension probe")
        self._vector_dim = len(probe)
        return self._vector_dim

    # ------------------------------------------------------------------
    # Graph mode (raw Cypher + NL -> Cypher)
    # ------------------------------------------------------------------
    def cypher_query(self, cypher: str) -> list:
        """Execute raw Cypher and return result rows."""
        result = self._backend.execute(cypher)
        return result.result_set if result.result_set else []

    async def natural_language_query(self, question: str) -> str:
        """Translate a NL question to Cypher, execute, and summarize."""
        schema = self._backend.get_schema_info()
        schema_text = json.dumps(schema, indent=2)

        # Step 1: NL -> Cypher
        cypher_messages = [
            {"role": "system", "content": _NL_TO_CYPHER_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Graph schema:\n{schema_text}\n\n"
                    f"Question: {question}"
                ),
            },
        ]
        logger.debug(
            "NL-to-Cypher request | model={} | messages={}",
            self._llm_model,
            json.dumps(cypher_messages, indent=2, ensure_ascii=False),
        )
        cypher_response = await chat_client().chat.completions.create(
            model=self._llm_model,
            messages=cypher_messages,  # type: ignore[arg-type]
            temperature=0.0,
        )
        cypher = (cypher_response.choices[0].message.content or "").strip()
        logger.debug("NL-to-Cypher response | raw={}", cypher)

        # Strip markdown fences if present
        if cypher.startswith("```"):
            lines = cypher.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cypher = "\n".join(lines)

        print(f"  [Cypher] {cypher}")

        # Step 2: Execute
        try:
            rows = self.cypher_query(cypher)
        except Exception as exc:
            return f"Query failed: {exc}\nGenerated Cypher: {cypher}"

        if not rows:
            return "(no results)"

        results_text = json.dumps(
            [str(row) for row in rows[:20]], indent=2, ensure_ascii=False, default=str
        )

        # Step 3: Summarize
        summary_messages = [
            {"role": "system", "content": _SUMMARIZE_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Query results:\n{results_text}"
                ),
            },
        ]
        logger.debug(
            "Summarization request | model={} | messages={}",
            self._llm_model,
            json.dumps(summary_messages, indent=2, ensure_ascii=False),
        )
        summary_response = await chat_client().chat.completions.create(
            model=self._llm_model,
            messages=summary_messages,  # type: ignore[arg-type]
            temperature=0.0,
        )
        summary = (summary_response.choices[0].message.content or "").strip()
        logger.debug("Summarization response | raw={}", summary)
        return summary

    # ------------------------------------------------------------------
    # Full-text mode
    # ------------------------------------------------------------------
    def fulltext_search(self, query: str) -> list[dict]:
        """Run a full-text search and return matching node dicts."""
        self._backend.ensure_fulltext_index(
            label=self._fulltext_label,
            properties=(self._fulltext_property,),
        )
        return self._backend.fulltext_search(
            query,
            label=self._fulltext_label,
            k=self._fulltext_k,
        )

    # ------------------------------------------------------------------
    # Vector mode
    # ------------------------------------------------------------------
    async def _embed(self, text: str) -> list[float]:
        """Embed ``text`` via the embedding client and return a list of floats."""
        logger.debug(
            "Embedding request | model={} | input_length={}",
            self._embedding_model,
            len(text),
        )
        response = await embedding_client().embeddings.create(
            model=self._embedding_model,
            input=text,
        )
        embedding = list(response.data[0].embedding)
        logger.debug("Embedding response | dim={}", len(embedding))
        return embedding

    async def vector_search(self, query: str) -> list[dict]:
        """Embed ``query`` and run a vector similarity search."""
        dim = await self._resolve_vector_dim()
        self._backend.ensure_vector_index(
            label=self._vector_label,
            property=self._vector_property,
            dim=dim,
        )
        embedding = await self._embed(query)
        return self._backend.vector_search(
            embedding,
            label=self._vector_label,
            property=self._vector_property,
            k=self._vector_k,
        )

    # ------------------------------------------------------------------
    # REPL
    # ------------------------------------------------------------------
    async def _run_query(self, query: str) -> str | list[dict]:
        """Dispatch ``query`` to the active search mode."""
        if self._mode == "fulltext":
            return self.fulltext_search(query)
        if self._mode == "vector":
            return await self.vector_search(query)
        # default: graph mode
        return await self.natural_language_query(query)

    async def search_loop(self) -> None:
        """Interactive search REPL.

        In graph mode a line prefixed with ``>`` is treated as raw Cypher;
        otherwise natural language. In fulltext/vector modes every line is
        a search query against the corresponding index.
        """
        mode_label = self._mode
        print(f"\n[+] Search REPL — mode: {mode_label} (type 'quit' to exit)")
        if mode_label == "graph":
            print("    Prefix with '>' for raw Cypher, otherwise natural language.")
        elif mode_label == "fulltext":
            print(
                f"    Full-text search on :{self._fulltext_label}"
                f"(.{self._fulltext_property})."
            )
        elif mode_label == "vector":
            dim = self._vector_dim
            dim_str = str(dim) if dim is not None else "auto-detected"
            print(
                f"    Vector search on :{self._vector_label}"
                f"(.{self._vector_property}), dim={dim_str}."
            )
        print()

        while True:
            try:
                prompt = f"Search ({mode_label})> "
                query = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if query.lower() in {"quit", "exit", "q"}:
                break
            if not query:
                continue

            try:
                if mode_label == "graph" and query.startswith(">"):
                    cypher = query[1:].strip()
                    rows = self.cypher_query(cypher)
                    for row in rows:
                        print(f"  {row}")
                    if not rows:
                        print("  (no results)")
                else:
                    result = await self._run_query(query)
                    if isinstance(result, list):
                        for row in result:
                            print(f"  {row}")
                        if not result:
                            print("  (no results)")
                    else:
                        print(f"\n  {result}\n")
            except Exception as exc:
                print(f"  Error: {exc}")