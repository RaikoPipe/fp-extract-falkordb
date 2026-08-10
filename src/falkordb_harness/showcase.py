"""Showcase prompt for the debug "Run Showcase" button.

The prompt is returned by the ``/api/debug-allowed`` endpoint and injected
into the Chainlit composer by ``public/debug_button.js``. It instructs the
agent to run the full pipeline end-to-end against a throwaway graph,
exercising every tool/feature, and to finish with a pass/fail report.

The prompt is a single string constant so a test can assert that every tool
name the agent should call appears in it — a refactor that drops a tool from
the prompt fails the test.
"""

from __future__ import annotations

SHOWCASE_PROMPT: str = """\
Run a full end-to-end showcase of the knowledge-graph pipeline. Follow these steps in order and report a pass/fail table at the end.

**Step 0 — Create a throwaway graph:**
Call `create_graph` with a name like `_debug_smoke_<ISO timestamp>` (use the current UTC time in ISO 8601 format, e.g. `_debug_smoke_2026-08-10T12:00:00Z`). The description should be: "Throwaway graph for end-to-end showcase run."

**Step 1 — Discover files:**
Use `ls` (or `glob`) on `originals/<your_session_id>/` to list candidate files. If the directory is empty, copy `data/showcase_fixture.md` into `originals/<your_session_id>/showcase_fixture.md` using the filesystem tools, then re-list.

**Step 2 — Inspect files:**
For each file found, call `file_metadata` and `read_excerpt` (a small slice — first 20 lines or so). Report what each file contains in 1-2 sentences.

**Step 3 — Preprocess (if needed):**
If any file is a binary format (PDF, DOCX, image, etc.), call `preprocess_document` on it. Plain `.md`/`.txt` files can be skipped — they are already LLM-ready.

**Step 4 — Chunk preview:**
Call `chunk_documents` on the `preprocessed/<your_session_id>/` directory (or `originals/<your_session_id>/` if no preprocessing was needed) to preview the chunks. Report the chunk count.

**Step 5 — Ingest:**
Call `extract_and_write` on the same directory. This runs the full LLM extraction and writes entities to the graph. Report the extraction count, node count, and any conflicts.

**Step 6 — Inspect the graph:**
Call `get_schema`, `list_nodes` (limit 50), `list_edges` (limit 50), `node_count`, and `list_graphs`. Report the schema, node/edge counts, and available graphs.

**Step 7 — Query:**
Call `cypher_query` with `MATCH (n) RETURN count(n) AS total_nodes`. Then call `nl_query` with "How many nodes are in the graph and what types of entities were extracted?".

**Step 8 — Search:**
Call `fulltext_search` with query "machine" and label "Resource". Then call `vector_search` with query "factory equipment". Report the top results and their scores.

**Step 9 — Reconciliation:**
Call `reconcile_posthoc` to run the post-hoc reconciliation pass over any plain-name Resources. Then call `get_reconciliations` to list any `POSSIBLE_DUPLICATE_OF` links found. Finally call `clear_reconciliations` to dismiss them.

**Step 10 — Graph description:**
Call `describe_graph` with no arguments to read all graph descriptions. Then call `update_graph_description` with a concise 1-3 sentence summary of what was ingested (entities, source files, scope).

**Step 11 — Report:**
Produce a markdown table with one row per step (0–10), showing the step name, status (PASS/FAIL), and a brief note (e.g. "3 nodes, 2 edges" or the error message if it failed). Do NOT call `reset_graph`, `request_graph_switch`, `use_graph`, `request_ingestion_confirmation`, or `ask_user` — those are either destructive or interactive and are skipped for this automated run.
"""
