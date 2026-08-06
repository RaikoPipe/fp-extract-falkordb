# AGENTS.md

Repository-specific guidance for OpenCode sessions. Read this before editing.

## What this is

Factory-planning knowledge graph pipeline: documents → LLM extraction → Pydantic graph model → FalkorDB via Cypher `MERGE` (with dedup). A Chainlit chat app + a `deepagents` LangGraph agent expose the pipeline. See `README.md` for the full dataflow diagram; this file only covers things not obvious from filenames.

## Source layout

- `src/knowledge/` — core pipeline (chunking, LLM extraction, Cypher mapping, `FalkorDBBackend`, reconciliation, search). Backend-agnostic library code.
- `src/falkordb_harness/` — Chainlit app (`chainlit_app.py`), LangGraph agent (`agent.py`), CLI (`cli.py`), auth (`auth.py`), data layer, and the `tools/` suite wired into the agent.
- `src/document-to-markdown/` — **git submodule** (docprep, the document→Markdown preprocessor). See setup below.
- `scripts/ingest.py` — standalone CLI for ingest / search REPL / reset / post-hoc reconciliation (not the agent path).
- Setuptools `src` layout (`packages.find: where=["src"]`); imports are `knowledge.*` and `falkordb_harness.*`.

## Setup gotchas

- **Submodule first:** `git submodule update --init --recursive` before `pip install` or Docker build. `pyproject.toml` declares `docprep[ollama] @ file:src/document-to-markdown`; pip resolves this from a local path, so the submodule must be populated or install fails.
- **pip ≥ 25** required to parse the `file:src/...` dependency URL (the Dockerfile upgrades pip explicitly).
- Two env files, both git-ignored: `.env` (non-secret config) and `.env.secrets` (secrets). Copy from `.env.example` / `.env.secrets.example`. In `docker-compose.yml`, secrets go via `env_file: .env.secrets` (loaded literally, no `${}` interpolation — values with `$`, `/`, `@` survive); non-secrets are passed explicitly under `environment:`, so adding a var to host `.env` does **not** auto-leak into the container.
- `chainlit create-secret` → put the result in `CHAINLIT_AUTH_SECRET` in `.env.secrets`. The app fail-fasts if it's missing while password auth is registered (`src/falkordb_harness/auth.py:191`).

## Commands

```bash
# Tests (295 unit tests, all mocked — no live FalkorDB / LLM / Chainlit needed)
pytest -q

# Run the agent graph via LangGraph Studio (uses langgraph.json -> build_graph)
langgraph dev

# Chainlit app, local (start FalkorDB separately first)
pip install -e ".[chainlit]"
chainlit run src/falkordb_harness/chainlit_app.py --host 0.0.0.0 --port 8000

# Docker (FalkorDB + Chainlit): host port 8001 -> container 8000
docker compose up -d --build

# Headless agent / admin bootstrap
falkordb-agent --single "How many nodes are in the graph?"
falkordb-agent create-admin --username admin --email admin@example.com

# Ingest / search REPL without the UI
python scripts/ingest.py --ingest --data-dir ./data --merge-mode conflict
python scripts/ingest.py --search            # Cypher / NL-to-Cypher
python scripts/ingest.py --search --fulltext # RediSearch
python scripts/ingest.py --search --vector   # embedding similarity
python scripts/ingest.py --recon-posthoc      # reconcile plain names added before their indexed counterpart
```

## Testing notes

- `testpaths = ["tests"]` in `pyproject.toml` — the docprep submodule's own tests (with integration/api markers and extra deps) are deliberately excluded; run them via `pytest` *inside* `src/document-to-markdown` if needed.
- No `conftest.py` under `tests/`; tests insert `src/` onto `sys.path` themselves. Don't add a root conftest that changes collection without checking it doesn't pull in the submodule.
- Tests use `unittest.mock`/`MagicMock` for `FalkorDB`, the LLM, and Chainlit — see e.g. `test_backend_reconnect.py`, `test_reconciliation.py`, `test_ui_prompts.py`. Do not introduce a live-connection test without a marker/skip.
- `pytest-asyncio` is in the `dev` extra; async tests use `@pytest.mark.asyncio`. `reportlab` (also `dev`) is optional — `test_file_inspection.py` uses `pytest.importorskip("reportlab")`.
- No lint/typecheck/formatter config exists in the repo. Don't claim a command runs; check `pyproject.toml` first.

## Two LLM models — don't conflate

- `LLM_MODEL` — entity extraction + NL-to-Cypher. Bare Ollama tag (e.g. `glm-5.2:cloud`), served via Ollama's OpenAI-compatible endpoint at `OLLAMA_API_BASE`.
- `AGENT_LLM_MODEL` — the LangGraph agent's reasoning. Accepts `anthropic/...`, `openai/...`, `gpt...`, `claude...`, or a bare Ollama tag. Resolution in `agent.py:resolve_model` routes bare Ollama tags to `ChatOpenAI` pointed at `OLLAMA_API_BASE` (exporting `OLLAMA_API_KEY`→`OPENAI_API_KEY` in-process), and routes `anthropic/...`/`claude` to `ChatAnthropic`. Code default is `anthropic/claude-sonnet-4-20250514`; `.env.example` ships `glm-5.2:cloud`.
- When editing agent model wiring, preserve the fail-fast credential check (`_missing_credentials`) and the `/v1` base-URL normalization — the litellm adapter was deliberately avoided (streaming broke; see comment in `agent.py`).

## Agent wiring specifics

- Built on `deepagents.create_deep_agent`; recursion limit is **50** (not LangGraph's default 25) to accommodate the multi-tool PRE-INGESTION REVIEW ROUTINE. See `agent._DEFAULT_RECURSION_LIMIT`.
- `RepeatGuardMiddleware` (`_loop_guard.py`) injects a `SystemMessage` to break repeated identical tool calls before hitting the recursion limit. Threshold via `AGENT_MAX_TOOL_REPEATS` (default 3). Don't remove when refactoring the agent graph.
- The agent's virtual filesystem is rooted at `DATA_DIR` (`FilesystemBackend(virtual_mode=True)`), so `ls`/`read_file`/`glob`/`grep` plus the custom `file_metadata`/`read_excerpt` tools see real `originals/` and `preprocessed/` trees with path-traversal containment.
- Per-session graph selection (Chainlit): `config["configurable"]["active_graph"]` + `["allowed_graphs"]` install a session-scoped `FalkorDBBackend`. The CLI / `langgraph dev` path leaves these unset and uses the module-level env-driven backend cache.

## Pipeline behavior to preserve

- Merge modes: `overwrite` (last-write-wins, default) vs `conflict` (first-writer-wins; disagreements stored as an in-graph `conflicts` JSON list, queryable via Cypher). Set via `MERGE_MODE` env or `--merge-mode`.
- Reconciliation links plain-name Resources (`name_has_index=false`) to indexed ones via `POSSIBLE_DUPLICATE_OF` edges using cosine similarity (`RECON_COSINE_CUTOFF` default 0.70) + LLM pairwise confidence (`RECON_CONFIDENCE_THRESHOLD` default 0.90). Append-only audit log at `RECONCILIATIONS_LOG`; it survives `--reset`.
- `FalkorDBBackend` connects **lazily** and reconnects on transient errors (see `test_backend_reconnect.py`). Don't add eager connection in `__init__`.
- Preprocessing (docprep) is only for scanned/image PDFs and office files with embedded figures. Plain `.txt`/`.md`/`.csv`/`.json`/`.html` go straight into `PREPROCESSED_DIR`. `docprep.yaml` configures the VLM fallback (Docling + EasyOCR + Ollama-hosted VLM).

## Docker / deployment

- Container listens on `8000`; compose publishes on host `8001` (host `8000` is taken on the deploy host). Update `APP_BASE_URL` to match if you change the mapping.
- Image runs as root only during `docker-entrypoint.sh` (re-chowns the bind-mounted `/app/data` to `appuser` uid 1001), then `gosu` drops to `appuser`. Don't add a `USER` directive to the Dockerfile.
- `.chainlit/config.toml` ships `allow_origins = ["https://REPLACE_WITH_YOUR_PRODUCTION_ORIGIN"]` — intentionally broken until you set the real FQDN, so a forgotten edit fails closed. Set it before deploying.
- For production, put Chainlit behind a TLS-terminating reverse proxy (JWT cookies must travel over HTTPS). For multi-host deployments, point `DATABASE_URL` at Postgres and add a `postgres` service.