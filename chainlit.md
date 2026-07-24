# FalkorDB Knowledge Graph Agent

Build and query factory-planning knowledge graphs backed by FalkorDB.

## Quick Start
1. Select a graph in the **sidebar** (opens automatically).
2. Upload files (PDF, DOCX, PPTX, images, text, CSV, JSON, HTML).
3. Press **Ingest Documents** to run the extraction pipeline.
4. Ask questions in natural language or use the starter prompts.

## Supported File Types
PDF, DOCX, PPTX, XLSX, CSV, JSON, HTML, Markdown, plain text, images.

## Key Tools
| Tool | What it does |
|------|-------------|
| `get_schema` | Show graph labels, relationships, properties |
| `list_nodes` / `list_edges` | Browse graph contents |
| `cypher_query` | Run raw Cypher |
| `nl_query` | Natural-language question answering |
| `fulltext_search` / `vector_search` | Find nodes by text or embedding |
| `extract_and_write` | Ingest documents into the graph |

## Tips
- Upload files first, then press **Ingest Documents** for one-click ingestion.
- Use `get_schema` to understand the graph before querying.
- Switch graphs via the sidebar dropdown.
