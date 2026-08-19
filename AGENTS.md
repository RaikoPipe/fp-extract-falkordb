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
# Tests
pytest -q

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

- Built on `deepagents.create_deep_agent`
- `RepeatGuardMiddleware` (`_loop_guard.py`) injects a `SystemMessage` to break repeated identical tool calls before hitting the recursion limit. Threshold via `AGENT_MAX_TOOL_REPEATS` (default 3). Don't remove when refactoring the agent graph.
- `TodoListMiddleware` (from `langchain.agents.middleware`) is added explicitly in `build_agent`'s `middleware=` list alongside `RepeatGuardMiddleware`. deepagents v0.7+ no longer auto-adds it; the system prompt, `on_message`'s `write_todos` handler, and `chainlit_progress.py` all depend on it. Don't remove.
- The agent's virtual filesystem is rooted at `DATA_DIR` (`FilesystemBackend(virtual_mode=True)`), so `ls`/`read_file`/`glob`/`grep` plus the custom `file_metadata`/`read_excerpt` tools see real `originals/` and `preprocessed/` trees with path-traversal containment.
- **PythonRunnerSandbox** (`python_runner.py`): opt-in Docker-backed code execution with pandas. When `PYTHON_RUNNER_ENABLE=1`, `build_agent` swaps the backend to a `PythonRunnerSandbox` (a `BaseSandbox` subclass) that runs commands inside a per-thread `python-runner-<thread_id>` container. The host `DATA_DIR` is bind-mounted read-only at `/workspace` so the agent's built-in `ls`/`read_file`/`glob`/`grep` (auto-built by `BaseSandbox` on top of `execute`) still see `originals/` and `preprocessed/`. Custom tools (`file_metadata`, `extract_and_write`, etc.) continue to use the host-side `fs_backend()` in `_paths.py` independently. The container image (`python-runner:latest`, built from `python-runner.Dockerfile`) ships pandas, numpy, matplotlib, scipy, scikit-learn, and openpyxl preinstalled. Container lifecycle: created lazily on first `execute`/`upload_files`/`download_files`, torn down on `on_chat_end` (Chainlit) or `atexit` (CLI), with a background TTL reaper (default 3600 s idle) as a safety net. Env vars: `PYTHON_RUNNER_ENABLE`, `PYTHON_RUNNER_IMAGE`, `PYTHON_RUNNER_TTL`, `PYTHON_RUNNER_NETWORK` (default `"none"`), `PYTHON_RUNNER_DEFAULT_TIMEOUT` (default 120), `PYTHON_RUNNER_MAX_OUTPUT_BYTES` (default 100 000). Requires the `docker` Python SDK (`pip install -e ".[runner]"`).
- Per-session graph selection (Chainlit): `config["configurable"]["active_graph"]` + `["allowed_graphs"]` install a session-scoped `FalkorDBBackend`. The CLI / `langgraph dev` path leaves these unset and uses the module-level env-driven backend cache.

## Chronological chat flow ordering

All outputs of a single agent turn — thinking steps, tool calls, the AgentTodos progress panel, `AskActionMessage` confirmation prompts, and assistant answer text — must render in the chat **in the order they occurred**, never jumping above earlier outputs. The agent may freely interleave text and tool calls (Claude Code / Claude.ai style); each contiguous span of user-facing assistant text is its own `cl.Message`, positioned where it was produced.

### v3 streaming protocol (LangGraph `astream_events(version="v3")`)

The stream loop uses LangGraph's **v3 streaming protocol** (experimental beta in langgraph ≥ 1.2). The v3 protocol provides typed content-block events that solve the reasoning-vs-answer disambiguation at the source.

- **Event source**: `run = await agent.astream_events(agent_input, version="v3", config={...}, transformers=[TasksTransformer])` returns an `AsyncGraphRunStream`. Used as `async with run:` for clean shutdown.
- **Two concurrent consumers** drive the caller-driven graph pump via `asyncio.gather`:
  - `_consume_raw()`: iterates `async for event in run:` (raw protocol events). Filters on `event["method"]`:
    - `"messages"`: carries `(payload, metadata)` tuples. Payload is either a whole `AIMessage` (non-streaming model / checkpoint replay) or a dict with `"event"` key (streaming content-block deltas from a real streaming LLM). The `langgraph_node` in metadata distinguishes the `model` node from the `log_attachments.before_agent` middleware node (skipped).
    - `"values"`: state snapshots (currently a safety net; tool results come via `tasks`).
  - `_consume_tasks()`: iterates `async for task in run.tasks:` (the `TasksTransformer` projection). Processes both `model` and `tools` tasks: a `model` task result carrying an `AIMessage` with `tool_calls` opens a new batch; each `tools` task (one per tool call, dispatched via `Send("tools", [tool_call])` by `create_agent`) has `input` as a one-element list `[{"name", "args", "id", "type": "tool_call"}]` and a result with a single `ToolMessage`.
- **Why `TasksTransformer`**: the default v3 mux registers `ValuesTransformer`, `MessagesTransformer`, `LifecycleTransformer`, `SubgraphTransformer`. `LifecycleTransformer`/`SubgraphTransformer` inherit `_TasksLifecycleBase` which **suppresses** `tasks` events from the raw event log (returns `False` from `process`). Registering `TasksTransformer` as an extra transformer exposes the `run.tasks` projection (a `StreamChannel` that is NOT suppressed) for tool execution events. Without it, tool start/result would not be visible.
- **No buffering**: v3's typed content-block events make the reasoning-vs-answer distinction per-block, not per-run-end. A `text-delta` event routes to an answer message; a `reasoning-delta` event routes to the thinking `cl.Step`. A whole `AIMessage` with `tool_calls` routes its text to the thinking step; one without `tool_calls` routes to a fresh answer message. There is no `_model_run_text` dict, no `_any_tool_called` flag, no `on_chat_model_end` flush — all removed.
- **Answer messages**: `_new_answer_msg()` creates and sends a fresh `cl.Message` per answer span (one per no-`tool_calls` model message). `_answer_messages` is the authoritative list; `_active_answer_msg` is the live handle for `stream_token`, reset to `None` on each new model message so the next text block creates a fresh container.
- **Thinking step**: `_ensure_thinking_step()` creates the `cl.Step(name=t("thinking.label"), type="tool", parent_id=_on_message_step_id, default_open=False)` lazily on first reasoning text. `thinking_text` accumulates all reasoning text; the step's `output` is updated on each reasoning delta.
- **Tool steps (same-tool chain aggregation)**: at `tools`-task start, the single tool call from `input[0]` is extracted via `_extract_tool_call`. Consecutive calls to the same tool collapse into one `cl.Step` (`<tool> x N`); a different tool breaks the chain via `_close_chain_step()`. Per-call-id → step mapping (`_call_id_to_step`) matches results to steps at task-result time. `_chain_step`/`_chain_tool`/`_chain_count` track the chain state; `_close_chain_step()` resets them.
- **Batch lifecycle (Send-per-call shape)**: `create_agent` dispatches each tool call as its own `Send("tools", [tool_call])` task — one `tools` task per call, not one per AIMessage. Batches are driven by `model` task results: a `model` task result whose `AIMessage` has `tool_calls` opens a new batch (`_open_batch`), mapping each `tool_call_id` to the batch. `tools` task starts fill in step rendering; `tools` task results fill in the batch entry's `result` via `_pending_results`. When all of a batch's results have landed, `_maybe_close_batch_for_call` flushes it to `_tool_call_batches` via `_flush_tool_batch`. Multiple batches may be open concurrently; the `GraphRecursionError`/`except` paths and the end-of-stream drain flush any left open. `_current_batch` (single in-flight list) is gone — replaced by `_open_batches` (dict of batch_key → entries) + `_call_id_to_batch` (call_id → batch_key) + `_pending_results` (call_id → entry awaiting result).
- **Claude-style tool-call history**: `_tool_call_batches` accumulates `[{name, args, id, result}, ...]` per batch. `_flush_tool_batch(batch)` records a completed batch. At the end of `on_message`, each batch becomes one `AIMessage(tool_calls=[...])` + one `ToolMessage` per call, appended to `chat_history`.
- **Stream recovery**: `register_stream` is registered when each answer message is created (in `_new_answer_msg`), not eagerly. `_stream_thread_id` starts `None` and is set on the first answer message. The registry entry is overwritten each time a new answer span starts; `deregister_stream` runs once in `finally`.
- **Single-consumer projections**: each projection (`run.messages`, `run.tasks`, etc.) is single-consumer — iterating it twice raises. The raw event stream (`async for event in run:`) and `run.tasks` are separate projections; `asyncio.gather` drives both concurrently. The shared graph pump uses an `asyncio.Lock` for single-flight coordination. **Critical**: the `tasks` projection only buffers items while a subscriber is active — items pushed before subscription are dropped. The `asyncio.gather(_consume_raw(), _consume_tasks())` pattern subscribes to `run.tasks` concurrently with `run`, so items are buffered correctly.
- **`write_todos` handling**: at `tools`-task start, if `tool_name == "write_todos"`, the tool input's `todos` list is routed to `_get_or_create_todos_element` (from `chainlit_progress.py`) to populate the pinned AgentTodos element. This was previously in `on_tool_start`; now in `_consume_tasks`'s task-start branch.
- **`log_attachments` middleware**: `build_agent` installs a `LogAttachmentsMiddleware` (an `AgentMiddleware` with a `before_agent` hook) that logs the raw wire format of the last `HumanMessage` before the agent execution starts. This replaced a parent `StateGraph` wrapper that placed the deep agent in a subgraph at namespace `('agent',)`, which filtered all `tools`/`model` task events out of the root-scoped `TasksTransformer` and silently dropped every tool-call step + the Claude-style tool-call history. The middleware node is named `log_attachments.before_agent` inside the agent graph (same scope as `model`/`tools`), so task events arrive at scope `()` and the `TasksTransformer` sees them. The `messages` events from this node are skipped (filtered by `langgraph_node == "log_attachments.before_agent"`; the old `"log_attachments"` name is also kept for defensiveness).
- **v3 API is experimental**: langgraph marks `astream_events(version="v3")` with `@beta(message="The v3 streaming protocol on Pregel is experimental.")`. Pin `langgraph==1.2.11` and `deepagents==0.7.6` in `pyproject.toml` to avoid surprise breakage.
- **Don't** revert to v2's `astream_events(version="v2")` or the buffer-then-flush pattern: the v2 approach deferred the reasoning-vs-answer distinction to `on_chat_model_end`, requiring per-run text buffering (`_model_run_text`) and the `_any_tool_called` gate. v3's typed content-block events eliminate this entirely. The `chat_flow_test.py` mock harness emits v3 protocol events matching the real Send-per-call shape: a single interleaved `timeline` of `(stream, event)` tuples (raw `messages`/`values` events + `tasks` events with `TaskPayload`/`TaskResultPayload`); the `MockRunStream` mimics `AsyncGraphRunStream` with `async with` + `__aiter__` + `.tasks`, distributing events to the two projections via a shared `_SharedCursor` so the concurrent consumers observe faithful protocol order (a later raw message cannot arrive before an earlier batch's task events).

## Pipeline behavior to preserve

- Merge modes: `overwrite` (last-write-wins, default) vs `conflict` (first-writer-wins; disagreements stored as an in-graph `conflicts` JSON list, queryable via Cypher). Set via `MERGE_MODE` env or `--merge-mode`.
- Reconciliation links plain-name Resources (`name_has_index=false`) to indexed ones via `POSSIBLE_DUPLICATE_OF` edges using cosine similarity (`RECON_COSINE_CUTOFF` default 0.70) + LLM pairwise confidence (`RECON_CONFIDENCE_THRESHOLD` default 0.90). Append-only audit log at `RECONCILIATIONS_LOG`; it survives `--reset`.
- `FalkorDBBackend` connects **lazily** and reconnects on transient errors (see `test_backend_reconnect.py`). Don't add eager connection in `__init__`.
- Preprocessing (docprep) is only for scanned/image PDFs and office files with embedded figures. Plain `.txt`/`.md`/`.csv`/`.json`/`.html` go straight into `PREPROCESSED_DIR`. `docprep.yaml` configures the VLM fallback (Docling + EasyOCR + Ollama-hosted VLM).

## Data layer schema

- `steps` column set must cover **every** key in `chainlit.step.StepDict`: the SQLAlchemy layer builds its INSERT column list dynamically from the StepDict keys (`sql_alchemy.create_step`), so a missing column raises `sqlite3.OperationalError` at runtime. Keep the DDL in `data_layer._DDL_STATEMENTS` in sync with `chainlit/step.py`'s `StepDict` (and the SELECT column list in `sql_alchemy.get_step` / `get_all_user_threads`).
- Forward-only migration: `_MIGRATION_COLUMNS` lists columns that may be missing from `steps`/`elements`/`users` tables in older DBs. SQLite lacks `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so `_migrate_columns` checks `PRAGMA table_info` and adds only what's absent. New StepDict columns added by a future Chainlit release must be added here **and** to `_DDL_STATEMENTS`.

## Docker / deployment

- Container listens on `8000`; compose publishes on host `8001` (host `8000` is taken on the deploy host). Update `APP_BASE_URL` to match if you change the mapping.
- Image runs as root only during `docker-entrypoint.sh` (re-chowns the bind-mounted `/app/data` to `appuser` uid 1001), then `gosu` drops to `appuser`. Don't add a `USER` directive to the Dockerfile.
- `.chainlit/config.toml` ships `allow_origins = ["https://REPLACE_WITH_YOUR_PRODUCTION_ORIGIN"]` — intentionally broken until you set the real FQDN, so a forgotten edit fails closed. Set it before deploying.
- For production, put Chainlit behind a TLS-terminating reverse proxy (JWT cookies must travel over HTTPS). For multi-host deployments, point `DATABASE_URL` at Postgres and add a `postgres` service.