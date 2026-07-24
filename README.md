# fp-extract-falkordb

Factory-planning knowledge graph extraction pipeline. Ingests domain documents, extracts structured entities via an LLM into a Pydantic graph model, and writes them to **FalkorDB** as Cypher `MERGE` statements with built-in entity deduplication.

Supports two merge modes:

| Mode | Behavior | Flag |
|---|---|---|
| `overwrite` (default) | Last-write-wins. Re-ingestion silently replaces prior property values. | `--merge-mode overwrite` |
| `conflict` | First-writer-wins. Existing non-null values are preserved; disagreements are isolated as **conflicts** (in-graph `conflicts` list + append-only JSONL audit log) for human review. | `--merge-mode conflict` |

An optional **similarity-based reconciliation** step catches plain-name resource nodes (e.g. "Machine") that refer to the same physical entity as an indexed resource (e.g. "AKL-01"). Enabled with `--recon`; details below.

---

## Quick start

```bash
# 1. Start FalkorDB
docker-compose up -d

# 2. Configure
cp .env.example .env   # edit as needed

# 3. Ingest documents (conflict mode)
python scripts/ingest.py --ingest --data-dir ./data --merge-mode conflict

# 4. Search (graph mode: raw Cypher or NL -> Cypher)
python scripts/ingest.py --search

# 5. Inspect conflicts
python scripts/ingest.py --search
# > MATCH (n) WHERE n.conflicts IS NOT NULL RETURN n.name, labels(n), n.conflicts
```

Visualization: FalkorDB's built-in web UI at `http://localhost:3000`.

### LangChain agent harness

A LangChain deep-agent harness (`falkordb_harness`) is bundled in this repo. It exposes the extraction pipeline and graph search as 16 tools driven by an agent you can interact with via the `falkordb-agent` CLI:

```bash
# Interactive agent session
falkordb-agent

# Single query
falkordb-agent --single "How many nodes are in the graph?"

# Use a specific agent model
falkordb-agent --model openai/gpt-4o
```

Two separate LLM configurations apply:

- **`LLM_MODEL`** — drives entity extraction and NL-to-Cypher (via the OpenAI-compatible endpoint). Bare Ollama tag (e.g. `glm-5.2:cloud`).
- **`AGENT_LLM_MODEL`** — drives the LangChain agent's reasoning (default: `anthropic/claude-sonnet-4-20250514`). Supports `anthropic/...`, `openai/...`, and bare Ollama tags (e.g. `glm-5.2:cloud`) via Ollama's OpenAI-compatible endpoint.

| Tool | Description |
|------|-------------|
| `file_metadata` | Inspect raw source file metadata (size, type, page count) |
| `read_excerpt` | Read a bounded excerpt of a raw source file |
| `preprocess_document` | Convert a raw source (scanned PDF/image/office) to Markdown via docprep, write to `PREPROCESSED_DIR` |
| `chunk_documents` | Preview document chunking without ingestion |
| `extract_and_write` | Full pipeline: chunk, extract, write to FalkorDB |
| `cypher_query` | Execute raw Cypher queries |
| `nl_query` | Natural language to Cypher with summarized answer |
| `fulltext_search` | RediSearch full-text search |
| `vector_search` | Vector similarity search via embeddings |
| `get_schema` | Inspect graph labels, relationships, properties |
| `list_nodes` | List nodes with properties |
| `list_edges` | List relationships |
| `node_count` | Count total nodes |
| `get_conflicts` | View merge conflicts |
| `clear_conflicts` | Dismiss reviewed conflicts |
| `get_reconciliations` | List `POSSIBLE_DUPLICATE_OF` links |
| `clear_reconciliations` | Dismiss reviewed reconciliation links |
| `reconcile_posthoc` | Post-hoc reconciliation pass over plain-name Resources |
| `reset_graph` | Delete all graph data |

See `.env.example` for all configuration options.

---

## Dataflow: documents to finished graph

The pipeline runs in seven stages. Each stage names the module and function that implements it, the data shape it produces, and (where relevant) the exact Cypher it generates.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  ./data/                                                                │
│  DOC1_Lastenheft_Fragment_v1-2.md                                       │
│  DOC3_Maschinenparameter_Tabelle.md                                     │
│  DOC8_Schichtplan_Personalbedarfsplanung.md                             │
│  ... (txt, md, pdf, docx, csv, json, html, py)                          │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │
                            │  Stage 1 — discover + read + chunk
                            │  chunking.load_and_chunk()
                            │  scripts/ingest.py:52-58
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  CHUNKS                                                                 │
│  list[dict] where each dict = {                                         │
│    "source":       "DOC3_Maschinenparameter_Tabelle.md",   ← provenance │
│    "chunk_index":  2,                                      ← provenance │
│    "text":         "AKL-01: capacity=500, AS/RS, zone-A..."             │
│  }                                                                      │
│                                                                         │
│  chunking.chunk_text(): paragraph-aware split on "\n\n", pack to        │
│  chunk_size=4000 chars, carry overlap=200 chars forward.                │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │
                            │  Stage 2 — LLM extraction (provenance-preserving)
                            │  llm_extract.extract_from_chunks()
                            │  scripts/ingest.py:60-69
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  EXTRACTIONS WITH PROVENANCE                                            │
│  list[tuple[FactoryPlanningGraph, str, int]]                            │
│                                                                         │
│  Per chunk:                                                             │
│    • build_extraction_prompt() injects the Pydantic JSON schema + text  │
│    • chat_client().chat.completions.create(model=LLM_MODEL, temperature=0.0)
│    • strip markdown fences → model_validate_json → json_repair fallback │
│    • retries up to 3× with exponential backoff                          │
│    • provenance (source, chunk_index) ATTACHED to the result tuple      │
│                                                                         │
│  FactoryPlanningGraph holds 15 typed entity lists:                      │
│    resources, transport_vehicles, trailers, transport_segments,         │
│    transport_routes, traffic_rules, products, production_programs,      │
│    order_logic, shift_models, worker_pools, control_strategies,         │
│    layout_elements, kpis, stochastic_parameters                         │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │
                            │  Stage 3 — backend construction + mode selection
                            │  FalkorDBBackend(merge_mode=..., conflicts_log_path=...)
                            │  scripts/ingest.py:137-141
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  FalkorDBBackend                                                        │
│                                                                         │
│  merge_mode resolution:                                                 │
│    explicit MergeMode arg  >  string arg  >  MERGE_MODE env  >  OVERWRITE│
│                                                                         │
│  conflicts_log_path resolution:                                         │
│    explicit arg  >  CONFLICTS_LOG env  >  ./data/conflicts.jsonl        │
│                                                                         │
│  MergeMode (cypher_mapper.py):                                          │
│    OVERWRITE = "overwrite"   last-write-wins (original behavior)        │
│    CONFLICT  = "conflict"    first-writer-wins + conflict isolation     │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │
                            │  Stage 4 — write to FalkorDB
                            │  backend.write_extraction(graph, source=, chunk_index=)
                            │  scripts/ingest.py:71-79
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  write_extraction branches on merge_mode                                │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │
              ┌─────────────┴──────────────┐
              │                            │
              ▼                            ▼
┌──────────────────────────┐  ┌──────────────────────────────────────────┐
│  OVERWRITE PATH          │  │  CONFLICT PATH                            │
│  (unchanged from v0)     │  │  (new)                                    │
│                          │  │                                          │
│  extraction_to_cypher()  │  │  extraction_to_cypher_with_mode(          │
│  cypher_mapper.py:176    │  │    graph, MergeMode.CONFLICT,             │
│                          │  │    source=, chunk_index=)                 │
│  Flat list of MERGEs:    │  │  cypher_mapper.py:292                     │
│  ┌─────────────────────┐ │  │                                          │
│  │ Pass 1: nodes       │ │  │  Returns (rel_statements, node_entries): │
│  │ MERGE (n:Label      │ │  │                                          │
│  │   {name: $name})    │ │  │  ┌─────────────────────────────────────┐ │
│  │ SET n.k = $p_k, ... │ │  │  │ Pass 1 — per entity (fetch + write) │ │
│  └─────────────────────┘ │  │  │                                     │ │
│  ┌─────────────────────┐ │  │  │ 4a. FETCH existing node             │ │
│  │ Pass 2: rels        │ │  │  │   model_to_cypher_fetch()           │ │
│  │ MATCH (a:Label      │ │  │  │   cypher_mapper.py:211              │ │
│  │   {name:$src})      │ │  │  │   ┌─────────────────────────────┐   │ │
│  │ MERGE (b:Tgt        │ │  │  │   │ MATCH (n:Resource            │   │ │
│  │   {name:$tgt})      │ │  │  │   │   {name: $name}) RETURN n   │   │ │
│  │ MERGE (a)-[r:TYPE]  │ │  │  │   └─────────────────────────────┘   │ │
│  │   ->(b)             │ │  │  │   _fetch_node_props() returns       │ │
│  └─────────────────────┘ │  │  │   dict of existing props, or {}     │ │
│                          │  │  │   if node doesn't exist yet         │ │
│  All SETs overwrite      │  │  │                                     │ │
│  unconditionally.        │  │  │ 4b. COMPARE + BUILD WRITE           │ │
│  No conflicts detected.  │  │  │   build_conflict_merge(             │ │
│  Returns (count, []).    │  │  │     entity, label, existing_props,  │ │
│                          │  │  │     source=, chunk_index=)          │ │
│                          │  │  │   cypher_mapper.py:222              │ │
│                          │  │  │                                     │ │
│                          │  │  │   For each scalar field (non-None,  │ │
│                          │  │  │   non-reference):                   │ │
│                          │  │  │   ┌───────────────────────────────┐ │ │
│                          │  │  │   │ existing is None?             │ │ │
│                          │  │  │   │   → SET n.k = $p_k  (WRITE)   │ │ │
│                          │  │  │   │ existing == incoming?         │ │ │
│                          │  │  │   │   → no-op        (AGREE)      │ │ │
│                          │  │  │   │ existing != incoming & non-null?│ │
│                          │  │  │   │   → CONFLICT: keep existing,  │ │ │
│                          │  │  │   │     append record to conflicts│ │ │
│                          │  │  │   └───────────────────────────────┘ │ │
│                          │  │  │                                     │ │
│                          │  │  │  Conflict record shape:             │ │
│                          │  │  │  {                                   │ │
│                          │  │  │    "property":        "capacity",   │ │
│                          │  │  │    "existing_value":   500,         │ │
│                          │  │  │    "incoming_value":   600,         │ │
│                          │  │  │    "source":           "DOC3....md",│ │
│                          │  │  │    "chunk_index":      2,           │ │
│                          │  │  │    "detected_at":      ISO-8601 UTC │ │
│                          │  │  │  }                                   │ │
│                          │  │  │                                     │ │
│                          │  │  │  Generated Cypher (conflict case):  │ │
│                          │  │  │  ┌───────────────────────────────┐  │ │
│                          │  │  │  │ MERGE (n:Resource             │  │ │
│                          │  │  │  │   {name: $name})              │  │ │
│                          │  │  │  │ SET n.conflicts =             │  │ │
│                          │  │  │  │   coalesce(n.conflicts, "[]") │  │ │
│                          │  │  │  │   + [$c_capacity]             │  │ │
│                          │  │  │  └───────────────────────────────┘  │ │
│                          │  │  │  (existing capacity is NOT touched) │ │
│                          │  │  │                                     │ │
│                          │  │  │ 4c. EXECUTE write                   │ │
│                          │  │  │   self._graph.query(write_q, ...)   │ │
│                          │  │  │   collect conflicts into all_conflicts│
│                          │  │  └─────────────────────────────────────┘ │
│                          │  │                                          │
│                          │  │  ┌─────────────────────────────────────┐ │
│                          │  │  │ Pass 2 — relationships (both modes) │ │
│                          │  │  │ _relationship_merges()              │ │
│                          │  │  │ cypher_mapper.py:131                │ │
│                          │  │  │                                     │ │
│                          │  │  │ For each reference field on entity: │ │
│                          │  │  │ ┌─────────────────────────────────┐ │ │
│                          │  │  │ │ MATCH (a:Label {name:$src_name})│ │ │
│                          │  │  │ │ MERGE (b:TgtLabel               │ │ │
│                          │  │  │ │   {name:$tgt_name})             │ │ │
│                          │  │  │ │ MERGE (a)-[r:REL_TYPE]->(b)     │ │ │
│                          │  │  │ └─────────────────────────────────┘ │ │
│                          │  │  │                                     │ │
│                          │  │  │ stop_sequence gets {seq: i} on edge │ │
│                          │  │  │ v1: NO conflict detection on edges  │ │
│                          │  │  └─────────────────────────────────────┘ │
│                          │  │                                          │
│                          │  │  4d. JSONL APPEND                       │
│                          │  │  _append_conflicts_log()                │
│                          │  │  falkordb_backend.py:170                │
│                          │  │  if all_conflicts:                      │
│                          │  │    append 1 json.dumps(conflict) line  │
│                          │  │    per conflict to conflicts.jsonl      │
│                          │  │                                          │
│                          │  │  Returns (statements_run, all_conflicts)│
│                          │  └──────────────────────────────────────────┘
└──────────────────────────┘  └──────────────────────────────────────────┘
                            │
                            │  Stage 5 — post-write summary
                            │  scripts/ingest.py:81-86
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  CONSOLE OUTPUT                                                         │
│                                                                         │
│  [+] Writing to FalkorDB...                                             │
│      42 Cypher statements executed                                      │
│      17 node(s) in graph 'factory_planning'                             │
│      merge mode: conflict                                               │
│      3 property conflict(s) logged to data/conflicts.jsonl             │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │
                            │  Stage 6 — conflict persistence (hybrid)
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  TWO COMPLEMENTARY CONFLICT STORES                                      │
│                                                                         │
│  ┌────────────────────────────────┐  ┌────────────────────────────────┐ │
│  │ IN-GRAPH: n.conflicts          │  │ ON-DISK: conflicts.jsonl       │ │
│  │ (current per-entity state)     │  │ (append-only audit history)    │ │
│  │                                │  │                                │ │
│  │ • JSON-serialized list on each │  │ • one JSON object per line     │ │
│  │   conflicted node              │  │ • full provenance per record   │ │
│  │ • queryable via Cypher:        │  │ • survives --reset (reset()    │ │
│  │   MATCH (n) WHERE              │  │   only touches the graph)      │ │
│  │   n.conflicts IS NOT NULL      │  │ • git-diffable                 │ │
│  │   RETURN n.name, n.conflicts   │  │ • consumable by external tools │ │
│  │ • mutable via clear_conflicts()│  │ • never truncated              │ │
│  │   → SET n.conflicts = null     │  │                                │ │
│  └────────────────────────────────┘  └────────────────────────────────┘ │
│                                                                         │
│  Backend helpers (falkordb_backend.py):                                 │
│    get_conflicts(label=None)  → parsed [{name, labels, conflicts}]      │
│    clear_conflicts(label=, name=) → nulls n.conflicts, returns count    │
└───────────────────────────┬─────────────────────────────────────────────┘
                            │
                            │  Stage 7 — surface to human / search
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  THREE ACCESS PATHS (all already wired)                                 │
│                                                                         │
│  1. Cypher REPL  — python scripts/ingest.py --search                    │
│     > MATCH (n) WHERE n.conflicts IS NOT NULL                           │
│         RETURN n.name, labels(n), n.conflicts                           │
│                                                                         │
│  2. Programmatic — backend.get_conflicts(label="Resource")              │
│                                                                         │
│  3. File review  — cat data/conflicts.jsonl | jq                        │
│     each line: {property, existing_value, incoming_value,               │
│                 source, chunk_index, detected_at}                       │
│                                                                         │
│  After adjudication: backend.clear_conflicts(name="M-100")              │
│  (JSONL record remains as history)                                      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Entity & relationship catalog

The `FactoryPlanningGraph` schema (`src/knowledge/graph_models/factory_graph_model.py`) defines 15 entity types. Cross-reference fields become relationships via `_REFERENCE_FIELDS` (`cypher_mapper.py:33-51`):

| Source label | Reference field | Relationship type | Target label |
|---|---|---|---|
| Resource | `shift_model` | `HAS_SHIFT_MODEL` | ShiftModel |
| Resource | `assigned_products` | `PROCESSES` | Product |
| TransportSegment | `from_node` | `FROM` | Resource |
| TransportSegment | `to_node` | `TO` | Resource |
| TransportRoute | `stop_sequence` | `STOPS_AT` | Resource |
| TransportRoute | `waiting_positions` | `HAS_WAITING_POSITION` | Resource |
| TransportRoute | `served_demand_points` | `SERVES` | Resource |
| TrafficRule | `affected_segments` | `AFFECTS_SEGMENT` | TransportSegment |
| Product | `bom_children` | `HAS_CHILD` | Product |
| OrderLogic | `associated_product` | `FOR_PRODUCT` | Product |
| OrderLogic | `associated_resource` | `TARGETS` | Resource |
| ShiftModel | `applicable_zones` | `APPLIES_TO_ZONE` | LayoutElement |
| WorkerPool | `assigned_resources` | `OPERATES` | Resource |
| ControlStrategy | `affected_resources` | `GOVERNS` | Resource |
| ControlStrategy | `affected_products` | `AFFECTS` | Product |
| StochasticParameter | `associated_entity` | `DESCRIBES` | Resource |
| KPI | `scope` | `SCOPED_TO` | Resource |

Reference fields are **never** stored as scalar node properties and **never** produce property conflicts — they are only translated to relationship MERGEs.

---

## Conflict record schema

Every conflict (both in-graph and JSONL) is a JSON object with these fields:

```json
{
  "property":        "capacity",
  "existing_value":  500,
  "incoming_value":  600,
  "source":          "DOC3_Maschinenparameter_Tabelle.md",
  "chunk_index":     2,
  "detected_at":     "2026-07-08T14:22:01Z"
}
```

| Field | Type | Description |
|---|---|---|
| `property` | str | The scalar field name that conflicted |
| `existing_value` | any | The value already stored on the node (preserved) |
| `incoming_value` | any | The value the new extraction tried to write (rejected) |
| `source` | str \| null | Originating document filename |
| `chunk_index` | int \| null | Positional chunk index within `source` |
| `detected_at` | str | ISO-8601 UTC timestamp of detection |

---

## Module map

| Module | Responsibility |
|---|---|
| `scripts/ingest.py` | CLI entry point, pipeline orchestration, search REPL launch |
| `src/knowledge/chunking.py` | Document discovery, reading, paragraph-aware chunking |
| `src/knowledge/llm_extract.py` | LLM-based structured extraction, provenance attachment |
| `src/knowledge/graph_models/factory_graph_model.py` | Pydantic entity + root extraction schema |
| `src/knowledge/cypher_mapper.py` | Pydantic → Cypher MERGE generation, `MergeMode`, conflict detection, reconciliation link Cypher |
| `src/knowledge/reconciliation.py` | Similarity-based reconciliation engine: embedding, cosine search, LLM pairwise confidence, description coalescing |
| `src/knowledge/falkordb_backend.py` | FalkorDB connection, write, conflict + reconciliation logging, search helpers |
| `src/knowledge/search.py` | Graph / fulltext / vector search REPL |

---

## Configuration reference

| CLI flag | Env var | Default | Description |
|---|---|---|---|
| `--data-dir` | `DATA_DIR` | `./data` | Source document root (originals + preprocessed live under it) |
| — | `ORIGINALS_DIR` | `./data/originals` | Raw uploaded/source files (PDF/DOCX/images); Chainlit uploads land here, and `file_metadata`/`read_excerpt`/`ls` inspect this tree |
| — | `PREPROCESSED_DIR` | `./data/preprocessed` | docprep Markdown output; `chunk_documents`/`extract_and_write` read here by default |
| `--graph-name` | `FALKORDB_GRAPH` | `factory_planning` | FalkorDB graph name |
| `--chunk-size` | — | `4000` | Chunk size in characters |
| `--concurrency` | — | `4` | Max parallel LLM calls |
| `--llm-model` | `LLM_MODEL` | `qwen3.5:122b-a10b` | bare Ollama model tag |
| `--api-base` | `OLLAMA_API_BASE` | — | LLM provider base URL |
| `--merge-mode` | `MERGE_MODE` | `overwrite` | `overwrite` or `conflict` |
| `--conflicts-log` | `CONFLICTS_LOG` | `./data/conflicts.jsonl` | JSONL conflict log path |
| `--recon` / `--no-recon` | `RECON_ENABLE` | `false` | Enable similarity reconciliation for plain-name Resources |
| `--recon-posthoc` | — | — | Post-hoc reconciliation pass over existing plain-name nodes |
| `--recon-cosine-cutoff` | `RECON_COSINE_CUTOFF` | `0.70` | Minimum cosine similarity for candidates |
| `--recon-confidence-threshold` | `RECON_CONFIDENCE_THRESHOLD` | `0.90` | Minimum LLM confidence to link a duplicate |
| `--recon-top-k` | `RECON_TOP_K` | `10` | Top-k cosine candidates before LLM pairwise |
| `--reconciliations-log` | `RECONCILIATIONS_LOG` | `./data/reconciliations.jsonl` | JSONL reconciliation log path |
| `--search` | — | — | Launch search REPL |
| `--fulltext` | — | — | Fulltext search mode (requires `--search`) |
| `--vector` | — | — | Vector search mode (requires `--search`) |
| `--reset` | — | — | Delete all graph data (preserves JSONL logs) |

---

## Document preprocessing (docprep)

The agent harness wraps the [`docprep`](src/document-to-markdown) submodule
(git submodule) as a `preprocess_document` tool. It converts raw source
documents — scanned PDFs, images, Excel charts, office formats — into
Markdown via Docling + EasyOCR + an optional VLM fallback, writing the result
into `PREPROCESSED_DIR` so the ingest tools pick it up.

### Directory model

```
DATA_DIR/
├── originals/      ← ORIGINALS_DIR: raw uploads/sources (PDF/DOCX/images)
│                      Chainlit uploads land here; file_metadata/read_excerpt/ls inspect this tree.
└── preprocessed/   ← PREPROCESSED_DIR: docprep Markdown output.
                       chunk_documents / extract_and_write read here by default.
```

Keeping originals and preprocessed Markdown in separate directories prevents
the ingest tools from double-counting a document by reading both the original
and its Markdown twin.

### Flow

```
ORIGINALS_DIR/scan.pdf
        │  preprocess_document(path="scan.pdf")
        │  docprep.convert() -> Docling + EasyOCR (+ VLM fallback if quality gate fails)
        ▼
PREPROCESSED_DIR/scan.md
        │  chunk_documents() / extract_and_write()  (default data_dir = PREPROCESSED_DIR)
        ▼
FalkorDB graph
```

### When to preprocess

- **Do** preprocess: scanned PDFs, image-only PDFs, images (PNG/JPEG/TIFF),
  Excel files with charts, DOCX/PPTX with embedded figures.
- **Don't** preprocess: plain `.txt` / `.md` / `.csv` / `.json` / `.html`
  sources — they are already LLM-ready and preprocessing wastes a VLM call.
  Copy them into `PREPROCESSED_DIR` directly (or point `extract_and_write`
  at `ORIGINALS_DIR` for that run).

### Configuration

docprep reads config in this precedence order (highest first):
1. `preprocess_document(yaml_path=...)` arg
2. `./docprep.yaml` if present
3. `DOCPREP_*` env vars: `DOCPREP_FALLBACK_PROVIDER`, `DOCPREP_FALLBACK_MODEL`,
   `DOCPREP_FALLBACK_BASE_URL`
4. `PipelineConfig` defaults

The `[ollama]` extra (the only one the harness pulls in) reuses
`OLLAMA_API_BASE` / `OLLAMA_API_KEY` for the VLM fallback endpoint. To use a
different provider (Mistral/OpenAI/Gemini/Anthropic), install the matching
`docprep[...]` extra and set its `_API_KEY` env var.

> **System dependency:** `python-magic` requires `libmagic` — install
> `libmagic1` (Debian/Ubuntu) or `libmagic` (macOS) if you see
> `ImportError: failed to find libmagic`.

See `docprep.example.yaml` for the full config schema and
`src/document-to-markdown/README.md` for the pipeline routing details.

---

## Similarity-based reconciliation

Plain-name resource nodes (e.g. "Machine", "Buffer") that lack a distinguishing index can slip past the exact-name deduplication and create silent duplicates. The reconciliation step catches these by testing each new plain-name Resource against all indexed Resource nodes (`name_has_index=true`) using a two-stage pipeline:

### Prerequisites

1. **`description` (required)** — Every Resource carries a semantically rich description. On name-match merge, the description is **coalesced** via an LLM call (old + new → merged) and the node is **re-embedded** so cosine search stays accurate as descriptions evolve. This happens in both merge modes.
2. **`name_has_index (required, bool)** — `true` when the name includes a clear index/ID (e.g. `AKL-01`, `Workstation-3A`), `false` for plain names. Only indexed nodes serve as similarity candidates.

### Reconciliation pipeline (per new plain-name Resource with no name match)

```
                         ┌──────────────────────────────────────┐
    new Resource         │  Stage 1 — EMBED                     │
    (name_has_index=false)│  embed_description(description)     │
                         └──────────────────┬───────────────────┘
                                              │
                         ┌──────────────────▼───────────────────┐
                         │  Stage 2 — COSINE SEARCH              │
                         │  vector_search(top_k, label=Resource) │
                         │  filter: name_has_index=true           │
                         │  filter: cosine >= 0.70 (cutoff)      │
                         │  filter: name != self                  │
                         └──────────────────┬───────────────────┘
                                              │
                    ┌─────────────────────────┴──────────────────┐
                    │ no candidates?      candidates?             │
                    ▼                         ▼                   │
            INSERT UNIQUE         ┌────────────────────────────┐  │
            (no link, no log)      │  Stage 3 — LLM PAIRWISE    │  │
                                  │  for each candidate:        │  │
                                  │    llm_pairwise_confidence(  │  │
                                  │      new, candidate)        │  │
                                  │  resource_type as strong     │  │
                                  │  tie-breaker signal          │  │
                                  └────────────┬───────────────┘  │
                                               │                  │
                                  ┌────────────▼───────────────┐  │
                                  │  pick highest confidence   │  │
                                  └────────────┬───────────────┘  │
                                   ┌───────────┴────────────┐     │
                                   │ conf < 0.90?  conf >= 0.90?│
                                   ▼              ▼           │     │
                           INSERT UNIQUE   INSERT DISTINCT NODE│
                           (no link)       + POSSIBLE_DUPLICATE_OF│
                                           edge (plain→indexed)   │
                                           + alias on indexed node │
                                           + canonical_name on plain│
                                           + reconciliations.jsonl │
```

### Post-hoc reconciliation (`--recon-posthoc`)

Plain-name nodes ingested *before* their indexed counterpart arrived can be reconciled later:

```bash
python scripts/ingest.py --recon-posthoc
```

Scans all `Resource` nodes with `name_has_index=false` that do not yet have an outgoing `POSSIBLE_DUPLICATE_OF`, embeds each, and runs the same pipeline. Can be run repeatedly as the graph grows.

### Reconciliation record schema

Every reconciliation (in-graph edge + JSONL) carries:

```json
{
  "new_name":          "Machine",
  "matched_name":      "AKL-01",
  "matched_label":     "Resource",
  "cosine_similarity": 0.8523,
  "llm_confidence":    0.9300,
  "source":            "DOC3_Maschinenparameter_Tabelle.md",
  "chunk_index":       2,
  "detected_at":       "2026-07-10T14:22:01Z"
}
```

### In-graph artifacts

| Artifact | Location | Description |
|---|---|---|
| `POSSIBLE_DUPLICATE_OF` edge | `(plain)-[:POSSIBLE_DUPLICATE_OF]->(indexed)` | Carries `cosine_similarity`, `llm_confidence`, `detected_at`, `source`, `chunk_index` as edge properties |
| `aliases` list | on the indexed node | JSON-encoded list of plain names linked to this indexed node |
| `canonical_name` | on the plain node | The indexed node's name, for lookups from the plain side |

### Backend helpers

```python
backend.get_reconciliations(label="Resource")   # list POSSIBLE_DUPLICATE_OF edges
backend.clear_reconciliations(plain_name="...")  # delete edge + remove alias/canonical
backend.reconcile_posthoc()                      # run post-hoc pass (async)
```

---

## v1 scope boundaries

- **Reconciliation applies to Resources only** — other entity types are not reconciled.
- **No auto-merge of duplicates** — the plain node is kept as a distinct node with a `POSSIBLE_DUPLICATE_OF` link; humans adjudicate via `clear_reconciliations`.
- **Relationship property conflicts** — edges use find-or-create `MERGE` in both modes; no conflict detection on edge properties (e.g. `seq` on `STOPS_AT`).
- **Auto-resolution** — no heuristic picks a winner for property conflicts; humans adjudicate via `clear_conflicts`.
- **Conflict/reconciliation log rotation** — the JSONL files grow unbounded by design (audit trail).

---

## Running the tests

```bash
pytest -q
```

87 tests covering chunking, Cypher mapping (both modes), conflict detection, conflict logging, reconciliation decisions, reconciliation logging, backend query helpers, and CLI flag plumbing.