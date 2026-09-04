# factory-kg-agent

Factory-planning knowledge graph extraction pipeline. Ingests domain documents, extracts structured entities via an LLM into a Pydantic graph model, and writes them to **FalkorDB** as Cypher `MERGE` statements with built-in entity deduplication. A Chainlit chat app exposes the pipeline as an interactive agent.

---

## Setup & running Chainlit

The primary way to use the system is the Chainlit web app (Docker, recommended) or running it directly with `chainlit run`.

### Option A — Docker (recommended)

```bash
# 1. Clone submodules (docprep document-to-markdown pipeline)
git submodule update --init --recursive

# 2. Copy config templates
cp .env.example .env
cp .env.secrets.example .env.secrets

# 3. Generate a JWT signing secret and put it in .env.secrets
chainlit create-secret           # paste the printed value into CHAINLIT_AUTH_SECRET

# 4. (optional) bootstrap the first admin in .env / .env.secrets
#    .env:                FIRST_ADMIN_USERNAME=admin
#                         FIRST_ADMIN_EMAIL=admin@example.com
#    .env.secrets:        FIRST_ADMIN_PASSWORD=<a strong password>

# 5. Build and start everything (FalkorDB + Chainlit)
docker compose up -d --build
```

Then open **http://localhost:8001** → `/register` to create an account → `/login`.

Containers:
- FalkorDB — `localhost:6379` (graph DB), web UI at `localhost:3000`
- Chainlit — published on host port `8001` → container `8000`

### Option B — Local (no Docker)

```bash
# 1. Start FalkorDB separately (e.g. via its docker image alone), then:
pip install -e ".[chainlit]"

# 2. Configure
cp .env.example .env
cp .env.secrets.example .env.secrets
chainlit create-secret           # → CHAINLIT_AUTH_SECRET in .env.secrets

# 3. Run the app
chainlit run src/falkordb_harness/chainlit_app.py --host 0.0.0.0 --port 8000
```

### What Chainlit gives you

A chat UI backed by a LangChain deep-agent with 16 tools: preprocess a source document, chunk it, extract entities into FalkorDB, run Cypher / natural-language / fulltext / vector searches, inspect the graph schema, list and resolve merge conflicts, and run similarity-based reconciliation.

Two separate LLM configs apply (see `.env.example`):
- `LLM_MODEL` — entity extraction + NL-to-Cypher (bare Ollama tag, e.g. `glm-5.2:cloud`)
- `AGENT_LLM_MODEL` — the agent's reasoning (default `glm-5.2:cloud`; also accepts `anthropic/...`, `openai/...`)

First-run accounts: visit `/register` (disable with `REGISTER_ENABLED=0`). Chat history is persisted in a local SQLite DB (`DATABASE_URL`); uploaded files live in `ELEMENTS_DIR` under `DATA_DIR`. For multi-host deployments, point `DATABASE_URL` at Postgres and add a `postgres` service to `docker-compose.yml`.

### CLI alternatives (no UI)

```bash
# Ingest documents
python scripts/ingest.py --ingest --data-dir ./data --merge-mode conflict

# Cypher / NL search REPL
python scripts/ingest.py --search

# Headless agent
falkordb-agent
falkordb-agent --single "How many nodes are in the graph?"
```

---

## How the system works

### Merge modes

Ingestion writes entities to FalkorDB as `MERGE` statements in one of two modes:

| Mode | Behavior | Flag |
|---|---|---|
| `overwrite` (default) | Last-write-wins. Re-ingestion silently replaces prior property values. | `--merge-mode overwrite` |
| `conflict` | First-writer-wins. Existing non-null values are preserved; disagreements are isolated as **conflicts** (in-graph `conflicts` list) for human review. | `--merge-mode conflict` |

An optional similarity-based reconciliation step catches plain-name resource nodes (e.g. "Machine") that refer to the same physical entity as an indexed one (e.g. "AKL-01"). Enable with `--recon`.

### Dataflow: documents → graph

```
./data/  (txt, md, pdf, docx, csv, json, html, py)
   │
   │  Stage 1 — discover + read + chunk   (chunking.load_and_chunk)
   ▼
CHUNKS  [{ source, chunk_index, text }]   (paragraph-aware, 4000 chars + 200 overlap)
   │
   │  Stage 2 — LLM extraction            (llm_extract.extract_from_chunks)
   ▼
EXTRACTIONS  [ (FactoryPlanningGraph, source, chunk_index) ]   (temp=0, 3× retry, json_repair fallback)
   │
   │  Stage 3 — backend + merge mode      (FalkorDBBackend)
   ▼
   │  Stage 4 — write to FalkorDB         (backend.write_extraction)
   ▼
┌──────────── OVERWRITE ────────────┐   ┌──────────────── CONFLICT ────────────────┐
│ Pass 1: MERGE nodes, SET props     │   │ 4a. FETCH existing node props            │
│ Pass 2: MERGE relationships        │   │ 4b. per field: None→SET, equal→no-op,     │
│ All SETs overwrite unconditionally │   │     differ→keep existing, append conflict │
│                                   │   │ 4c. EXECUTE write; collect conflicts      │
│                                   │   │ Pass 2: MERGE relationships (same)        │
└───────────────────────────────────┘   └──────────────────────────────────────────┘
   │
   │  Stage 5 — console summary
   ▼
   │  Stage 6 — conflicts stored IN-GRAPH (n.conflicts JSON list, queryable via Cypher)
   ▼
   │  Stage 7 — surface via Cypher REPL or the agent's cypher_query tool
```

### Document preprocessing (docprep)

Raw sources (scanned PDFs, images, office formats) are converted to Markdown before ingestion, so the LLM only ever sees LLM-ready text.

```
DATA_DIR/
├── originals/      ← raw uploads/sources (PDF/DOCX/images); Chainlit uploads land here
└── preprocessed/  ← docprep Markdown output; ingestion reads here by default

ORIGINALS_DIR/scan.pdf
   │  preprocess_document()  →  docprep (Docling + EasyOCR + optional VLM fallback)
   ▼
PREPROCESSED_DIR/scan.md
   │  chunk_documents() / extract_and_write()
   ▼
FalkorDB graph
```

Preprocess scanned/image PDFs and office files with embedded figures. Don't preprocess plain `.txt`/`.md`/`.csv`/`.json`/`.html` — copy them into `PREPROCESSED_DIR` directly.

### Similarity-based reconciliation

Plain-name Resources without a distinguishing index can create silent duplicates. The reconciliation step tests each new plain-name Resource against indexed Resources (`name_has_index=true`) in three stages:

```
new Resource (name_has_index=false)
   │  Stage 1 — EMBED description
   ▼
   │  Stage 2 — COSINE SEARCH  (label=Resource, name_has_index=true, cosine ≥ 0.70)
   ▼
   │  no candidates?  →  INSERT UNIQUE (no link)
   │  candidates?     →  Stage 3 — LLM PAIRWISE confidence per candidate
   ▼
   │  best confidence < 0.90?  →  INSERT UNIQUE (no link)
   │  best confidence ≥ 0.90? →  INSERT plain node
   │                            + POSSIBLE_DUPLICATE_OF edge (plain → indexed)
   │                            + aliases on indexed, canonical_name on plain
   ▼
   reconciliations.jsonl  (append-only audit log)
```

Post-hoc pass for nodes ingested before their indexed counterpart:

```bash
python scripts/ingest.py --recon-posthoc
```

---

## Running the tests

```bash
pytest -q
```

295 tests covering chunking, Cypher mapping (both modes), conflict detection/logging, reconciliation decisions/logging, backend query helpers, CLI flag plumbing, password auth + registration, and the SQLite data layer + local element storage.

