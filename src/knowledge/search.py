"""Graph search: raw Cypher and LLM-assisted natural language to Cypher."""

from __future__ import annotations

import asyncio
import json
import os

import litellm

from knowledge.falkordb_backend import FalkorDBBackend

_DEFAULT_LLM_MODEL = "ollama/qwen3.5:122b-a10b"

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
    """Search interface for the FalkorDB knowledge graph."""

    def __init__(
        self,
        backend: FalkorDBBackend,
        llm_model: str | None = None,
    ) -> None:
        self._backend = backend
        self._llm_model = llm_model or os.getenv("LLM_MODEL", _DEFAULT_LLM_MODEL)

    def cypher_query(self, cypher: str) -> list:
        """Execute raw Cypher and return result rows."""
        result = self._backend.execute(cypher)
        return result.result_set if result.result_set else []

    async def natural_language_query(self, question: str) -> str:
        """Translate a NL question to Cypher, execute, and summarize."""
        schema = self._backend.get_schema_info()
        schema_text = json.dumps(schema, indent=2)

        # Step 1: NL -> Cypher
        cypher_response = await litellm.acompletion(
            model=self._llm_model,
            messages=[
                {"role": "system", "content": _NL_TO_CYPHER_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Graph schema:\n{schema_text}\n\n"
                        f"Question: {question}"
                    ),
                },
            ],
            temperature=0.0,
        )
        cypher = cypher_response.choices[0].message.content.strip()

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
        summary_response = await litellm.acompletion(
            model=self._llm_model,
            messages=[
                {"role": "system", "content": _SUMMARIZE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\n"
                        f"Query results:\n{results_text}"
                    ),
                },
            ],
            temperature=0.0,
        )
        return summary_response.choices[0].message.content.strip()

    async def search_loop(self) -> None:
        """Interactive search REPL.

        Prefix a line with ``>`` for raw Cypher; otherwise natural language.
        """
        print("\n[+] Search REPL (type 'quit' to exit)")
        print("    Prefix with '>' for raw Cypher, otherwise natural language.\n")

        while True:
            try:
                query = input("Search> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if query.lower() in {"quit", "exit", "q"}:
                break
            if not query:
                continue

            if query.startswith(">"):
                cypher = query[1:].strip()
                try:
                    rows = self.cypher_query(cypher)
                    for row in rows:
                        print(f"  {row}")
                    if not rows:
                        print("  (no results)")
                except Exception as exc:
                    print(f"  Error: {exc}")
            else:
                answer = await self.natural_language_query(query)
                print(f"\n  {answer}\n")
