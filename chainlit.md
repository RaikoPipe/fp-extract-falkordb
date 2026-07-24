# FalkorDB Knowledge Graph Agent

This agent helps you build and query a factory-planning knowledge graph backed by FalkorDB.

## Capabilities

- **Document ingestion** — Upload PDFs, DOCX, PPTX, images, or text files and ingest them into the knowledge graph with one button press.
- **Natural language queries** — Ask questions about your data in plain English.
- **Cypher queries** — Run raw Cypher against the graph for precise lookups.
- **Schema inspection** — View labels, relationship types, and node counts.

## Ingestion Toolbar

The sidebar (gear icon) lets you select which knowledge graph the agent targets:

- **Active knowledge graph** (dropdown) — the single graph all queries and ingestion target.
- **Enabled knowledge graphs** (checkboxes) — the graphs the agent may switch to at runtime via `use_graph`.
- **Create a new knowledge graph** (text field) — type a name and save to create an empty graph, which becomes the active graph.

Upload files via the chat input (paperclip button), then press the **Ingest Documents** button to run the full pipeline — preprocess (if needed), chunk, LLM-extract entities, and write into the active graph — in a single press. Progress is streamed live as collapsible steps.
