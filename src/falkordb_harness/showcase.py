"""Showcase prompt for the debug "Run Showcase" button.

The prompt is returned by the ``/api/debug-allowed`` endpoint and injected
into the Chainlit composer by ``public/debug_button.js``. It instructs the
agent to run the full pipeline end-to-end against a throwaway graph,
exercising every feature, and to finish with a pass/fail report.
"""

from __future__ import annotations

SHOWCASE_PROMPT: str = """\
Run a full end-to-end showcase of the knowledge-graph pipeline. Follow these steps in order and report a pass/fail table at the end.

**Step 0 — Create a throwaway graph:**
Create a new knowledge graph with a name like `_debug_smoke_<ISO timestamp>` (use the current UTC time in ISO 8601 format, e.g. `_debug_smoke_2026-08-10T12:00:00Z`). The description should be: "Throwaway graph for end-to-end showcase run."

**Step 1 — Discover files:**
List the files under `originals/<your_session_id>/`. If the directory is empty, copy `data/showcase_fixture.md` into `originals/<your_session_id>/showcase_fixture.md`, then re-list.

**Step 2 — Inspect files:**
For each file found, inspect its metadata (size, type, page/line counts) and read a small excerpt (first 20 lines or so). Report what each file contains in 1-2 sentences.

**Step 3 — Preprocess (if needed):**
If any file is a binary format (PDF, DOCX, image, etc.), convert it to Markdown. Plain `.md`/`.txt` files can be skipped — they are already LLM-ready.

**Step 4 — Chunk preview:**
Preview the chunks that would be produced from the preprocessed directory (or the originals directory if no preprocessing was needed). Report the chunk count.

**Step 5 — Ingest:**
Run the full ingestion pipeline on the same directory. This extracts entities via LLM and writes them to the graph. Report the extraction count, node count, conflicts, and any potential duplicates found.

**Step 6 — Inspect the graph:**
Inspect the graph schema and list available graphs. Query the node count and relationship type counts. Report the schema, counts, and available graphs.

**Step 7 — Query:**
Run a natural-language query asking how many nodes are in the graph and what types of entities were extracted.

**Step 8 — Search:**
Run a keyword search for "machine" and a semantic search for "factory equipment". Report the top results and their scores.

**Step 9 — Reconciliation:**
List any potential duplicate links found during ingestion. For each duplicate, present it to the user and resolve it with the user's choice (accept the merge, reject the link, or keep the entities separate).

**Step 10 — Graph description:**
Read all graph descriptions, then update the active graph's description with a concise 1-3 sentence summary of what was ingested (entities, source files, scope).

**Step 11 — Report:**
Produce a markdown table with one row per step (0–10), showing the step name, status (PASS/FAIL), and a brief note (e.g. "3 nodes, 2 edges" or the error message if it failed). Do NOT reset the graph, switch graphs, request ingestion confirmation, or ask the user clarifying questions — those are either destructive or interactive and are skipped for this automated run.
"""
