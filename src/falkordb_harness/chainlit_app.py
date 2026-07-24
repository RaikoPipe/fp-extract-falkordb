"""Chainlit frontend for the FalkorDB deep-agent harness.

Run with:
    chainlit run src/falkordb_harness/chainlit_app.py --port 8000
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from pathlib import Path

import chainlit as cl
from chainlit import input_widget
from chainlit.action import Action
from chainlit.element import Task, TaskList, TaskStatus
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage

from falkordb_harness.ingest_runner import run_ingestion
from falkordb_harness.tools._paths import originals_dir, preprocessed_dir

load_dotenv(override=True)

# Uploaded raw files land in ORIGINALS_DIR (default: ./data/originals).
# Falls back to the legacy ./data/uploads path only when ORIGINALS_DIR is
# unset, preserving the previous behaviour for users who never set it.
ORIGINALS_DIR = originals_dir()
# Markdown output tree is auto-created so the agent's filesystem tools can
# ls/glob into it on the very first turn (previously it was created lazily
# inside preprocess_document, so early ls calls returned path_not_found).
PREPROCESSED_DIR = preprocessed_dir()

MAX_HISTORY_PAIRS = 20

# Default graph, used to seed the sidebar widgets when no FalkorDB instance is
# reachable yet (e.g. starting before `docker-compose up`). Resolved from the
# same env var the backend reads.
_DEFAULT_GRAPH = os.getenv("FALKORDB_GRAPH", "factory_planning")

logger = logging.getLogger("falkordb_harness.chainlit")


def _list_available_graphs() -> list[str]:
    """Return the graph names known to the FalkorDB instance, with a fallback.

    Builds a throwaway :class:`FalkorDBBackend` to call ``GRAPH.LIST`` without
    disturbing the session backend. On any connection error (FalkorDB not
    running yet), falls back to ``[FALKORDB_GRAPH]`` so the UI is still
    usable — the user can switch graphs after FalkorDB comes up.
    """
    try:
        from knowledge.falkordb_backend import FalkorDBBackend

        backend = FalkorDBBackend()
        names = backend.list_graphs()
        if names:
            return names
    except Exception as exc:  # noqa: BLE001 — UI must stay usable on conn error
        logger.warning("Could not list FalkorDB graphs: %s", exc)
    return [_DEFAULT_GRAPH]


def _build_settings_widgets(graphs: list[str]) -> cl.ChatSettings:
    """Construct the chat-settings widgets for graph selection.

    - ``active_graph`` (Select): the single graph the agent targets. The base
      dropdown requirement.
    - ``allowed_graphs`` (MultiSelect): the checkbox set of graphs the agent
      may switch among at runtime via ``use_graph``. Defaults to just the
      active graph so the base case (single dropdown) works unchanged; the
      user checks more boxes to expand scope.
    - ``new_graph_name`` (TextInput): type a name and hit the panel's Save
      button to create a new empty knowledge graph on the FalkorDB instance.
      On save, the graph is created, added to both dropdowns, set as active,
      and the field is cleared. Empty value = no-op save (normal graph
      selection behavior).
    """
    # Ensure the default is present in the list even if FalkorDB returned it.
    if _DEFAULT_GRAPH not in graphs:
        graphs = [*_graphs_unique(graphs), _DEFAULT_GRAPH]
    return cl.ChatSettings(
        inputs=[
            input_widget.Select(
                id="active_graph",
                label="Active knowledge graph",
                values=graphs,
                initial_value=_DEFAULT_GRAPH,
                description=(
                    "The graph all queries and ingestion target. Use the "
                    "checkboxes below to enable more graphs for switching."
                ),
            ),
            input_widget.MultiSelect(
                id="allowed_graphs",
                label="Enabled knowledge graphs (in scope for the assistant)",
                values=graphs,
                initial=[_DEFAULT_GRAPH],
                description=(
                    "Graphs the assistant may switch to via use_graph. The "
                    "active graph is always included automatically."
                ),
            ),
            input_widget.TextInput(
                id="new_graph_name",
                label="Create a new knowledge graph",
                placeholder="e.g. orders_v2",
                description=(
                    "Type a name and hit Save to create a new empty graph on "
                    "the FalkorDB instance. It will be added to the dropdowns "
                    "and set as the active graph. Leave blank to skip."
                ),
            ),
        ]
    )


def _graphs_unique(graphs: list[str]) -> list[str]:
    """Return ``graphs`` de-duplicated, order-preserving."""
    seen: set[str] = set()
    out: list[str] = []
    for g in graphs:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _normalize_selection(
    active_graph: str | None,
    allowed_graphs: list[str] | None,
) -> tuple[str | None, list[str] | None]:
    """Coerce raw settings-dict values into (active, allowed) and repair state.

    - ``active_graph`` must be a non-empty string; falls back to the default.
    - ``allowed_graphs`` must be a list; if empty/None it becomes
      ``[active_graph]``.
    - The active graph is always inserted into the allowed set.
    """
    if not active_graph or not isinstance(active_graph, str):
        active_graph = _DEFAULT_GRAPH
    if not allowed_graphs or not isinstance(allowed_graphs, (list, tuple)):
        allowed_graphs = [active_graph]
    else:
        allowed_graphs = [str(g) for g in allowed_graphs if g]
        if active_graph not in allowed_graphs:
            allowed_graphs = [active_graph, *allowed_graphs]
    return active_graph, allowed_graphs


def _rebuild_agent_for_selection(
    active_graph: str,
    allowed_graphs: list[str],
) -> None:
    """Rebuild the agent bound to the user's graph selection and stash it.

    Constructs the deep agent with a ``configurable`` carrying the active +
    allowed graphs; :func:`build_agent` installs a per-session backend bound
    to ``active_graph`` and restricted to ``allowed_graphs``.
    """
    from falkordb_harness.agent import build_agent

    agent = build_agent(
        {
            "configurable": {
                "active_graph": active_graph,
                "allowed_graphs": allowed_graphs,
            }
        }
    )
    cl.user_session.set("agent", agent)
    cl.user_session.set(
        "graph_selection",
        {"active_graph": active_graph, "allowed_graphs": allowed_graphs},
    )
    # build_agent installed a session-scoped FalkorDBBackend via a contextvar,
    # but Chainlit runs each handler in its own asyncio task, so that contextvar
    # does NOT survive into on_message. Stash the live backend instance in
    # user_session (which IS preserved across handler tasks, keyed by session
    # id) and re-install it at the start of on_message so the tools — which
    # call get_backend() -> _SESSION_BACKEND.get() — see the user's chosen
    # graph instead of falling back to the module-level default (factory_planning).
    from falkordb_harness.backend import _SESSION_BACKEND

    cl.user_session.set("session_backend", _SESSION_BACKEND.get())


@cl.set_starters
async def set_starters() -> list[cl.Starter]:
    return [
        cl.Starter(
            label="Show all machines",
            message="List all Resource nodes that represent machines, with their processing times and capacities.",
            icon="/public/logo.svg",
        ),
        cl.Starter(
            label="Transport routes",
            message="What transport routes and vehicles are defined in the knowledge graph?",
            icon="/public/logo.svg",
        ),
        cl.Starter(
            label="Shift models",
            message="Show me the shift models and worker pools currently in the graph.",
            icon="/public/logo.svg",
        ),
        cl.Starter(
            label="Graph schema",
            message="Show me the full schema of the current knowledge graph — labels, relationships, and properties.",
            icon="/public/logo.svg",
        ),
        cl.Starter(
            label="Search for a resource",
            message="Search the knowledge graph for resources related to washing machines.",
            icon="/public/logo.svg",
        ),
        cl.Starter(
            label="How to ingest documents",
            message="What document types can I upload, and how does the ingestion pipeline work?",
            icon="/public/logo.svg",
        ),
    ]


@cl.on_chat_start
async def on_chat_start() -> None:
    graphs = _list_available_graphs()
    settings = _build_settings_widgets(graphs)
    await settings.send()

    active, allowed = _normalize_selection(_DEFAULT_GRAPH, [_DEFAULT_GRAPH])
    _rebuild_agent_for_selection(active, allowed)
    cl.user_session.set("chat_history", [])
    # Track uploaded file paths across the session so the Ingest button can
    # pick them up. Each upload appends to this list (see on_message).
    cl.user_session.set("uploaded_files", [])

    # Welcome message carries the "Ingest Documents" action button. The
    # callback (see @cl.action_callback("ingest_documents") below) runs the
    # full pipeline directly — bypassing the agent's review routine — on the
    # files uploaded so far, targeting the graph selected in the sidebar.
    await cl.Message(
        content=(
            f"Ready on graph `{active}`. "
            "Upload files and press **Ingest Documents**, or pick a starter below."
        ),
        actions=[
            Action(
                name="ingest_documents",
                payload={},
                label="Ingest Documents",
                tooltip=(
                    "Preprocess (if needed), chunk, LLM-extract, and write "
                    "all uploaded files into the active knowledge graph."
                ),
                icon="upload",
            ),
        ],
    ).send()


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    """Rebuild the agent when the user changes graph selection in the sidebar.

    Fires on save (not live-during-edit), so rebuilding is not thrashy. Reads
    ``active_graph`` (Select) and ``allowed_graphs`` (MultiSelect) from the
    settings dict, normalizes them, and rebuilds the agent so the new session
    backend is bound to the chosen graph.

    If ``new_graph_name`` (TextInput) is non-empty, a new empty knowledge graph
    of that name is created on the FalkorDB instance first. The new graph is
    added to both dropdowns and set as the active graph; the text field is
    cleared on the refreshed panel. A name that already exists (or is empty
    after trimming) is rejected with an error message and the selection is
    left unchanged. An empty ``new_graph_name`` is a no-op (normal selection).
    """
    active_raw = settings.get("active_graph")
    allowed_raw = settings.get("allowed_graphs")
    new_graph_name = (settings.get("new_graph_name") or "").strip()

    if new_graph_name:
        # Attempt to create the new graph on the FalkorDB instance. Use a
        # throwaway backend (like _list_available_graphs) so the session
        # backend is not disturbed on failure.
        try:
            from knowledge.falkordb_backend import FalkorDBBackend

            FalkorDBBackend().create_graph(new_graph_name)
        except ValueError as exc:
            await cl.Message(
                content=(
                    f"Could not create knowledge graph `{new_graph_name}`:\n"
                    f"{exc}"
                ),
            ).send()
            # Fall through to rebuild with the existing selection (no new graph).
            new_graph_name = ""
        except Exception as exc:  # noqa: BLE001 — UI must stay usable on conn error
            logger.warning("Could not create FalkorDB graph %r: %s", new_graph_name, exc)
            await cl.Message(
                content=(
                    f"Could not create knowledge graph `{new_graph_name}` "
                    f"(FalkorDB unreachable): {exc}"
                ),
            ).send()
            new_graph_name = ""

    if new_graph_name:
        # Creation succeeded — make the new graph the active graph and ensure
        # it's in the enabled set, then refresh the widgets so the dropdowns
        # reflect the new graph and the text field is cleared.
        active, allowed = _normalize_selection(new_graph_name, list(allowed_raw or []))
        if new_graph_name not in allowed:
            allowed = [new_graph_name, *allowed]
        _rebuild_agent_for_selection(active, allowed)

        graphs = _list_available_graphs()
        if active not in graphs:
            graphs = [active, *graphs]
        refreshed = _build_settings_widgets(graphs)
        # Clear the text field on the refreshed panel so the user sees the
        # creation took effect and can't accidentally re-submit the same name.
        for widget in refreshed.inputs:
            if getattr(widget, "id", None) == "new_graph_name":
                widget.initial = ""
        await refreshed.send()

        await cl.Message(
            content=(
                f"Created new empty knowledge graph `{active}` and switched "
                f"to it.\n"
                f"- Active: `{active}`\n"
                f"- Enabled: `{', '.join(allowed)}`\n\n"
                f"Queries and ingestion now target `{active}`. I can switch "
                f"to any of the enabled graphs via `use_graph`."
            ),
        ).send()
        return

    active, allowed = _normalize_selection(active_raw, allowed_raw)
    _rebuild_agent_for_selection(active, allowed)

    await cl.Message(
        content=(
            f"Knowledge graph selection updated.\n"
            f"- Active: `{active}`\n"
            f"- Enabled: `{', '.join(allowed)}`\n\n"
            f"Queries and ingestion now target `{active}`. I can switch to "
            f"any of the enabled graphs via `use_graph`."
        ),
    ).send()


@cl.action_callback("ingest_documents")
async def on_ingest_documents(action: Action) -> None:
    """Run the full ingestion pipeline on all uploaded files in one press.

    Bypasses the agent's PRE-INGESTION REVIEW ROUTINE (the user explicitly
    pressed the button, which is the confirmation). Reuses the same library
    code as the agent's ``extract_and_write`` tool — only the orchestration
    differs. Progress is streamed as ``cl.Step`` entries so the user sees
    preprocessing, chunking, extraction, and writing unfold live.

    Targets the graph selected in the sidebar (restored into the session
    contextvar via ``_ensure_session_backend``). If no files have been
    uploaded yet, prompts the user to upload some first.
    """
    from falkordb_harness.ingest_runner import _ensure_session_backend

    _ensure_session_backend()

    uploaded: list[Path] = cl.user_session.get("uploaded_files") or []
    if not uploaded:
        await cl.Message(
            content=(
                "No files uploaded yet. Upload one or more documents "
                "(use the paperclip / attachment button in the chat input) "
                "and then press **Ingest Documents** again."
            ),
        ).send()
        return

    # De-duplicate while preserving order.
    seen: set[str] = set()
    files: list[Path] = []
    for p in uploaded:
        s = str(p)
        if s not in seen:
            seen.add(s)
            files.append(Path(p))

    selection = cl.user_session.get("graph_selection") or {}
    active_graph = selection.get("active_graph", _DEFAULT_GRAPH)

    await cl.Message(
        content=(
            f"Starting ingestion of {len(files)} file(s) into knowledge "
            f"graph `{active_graph}`…"
        ),
    ).send()

    # Live progress panel: a chainlit TaskList whose tasks flip
    # READY -> RUNNING -> DONE/FAILED as the pipeline advances. Each
    # top-level pipeline stage gets a task; per-file preprocessing and
    # chunking get nested tasks so the user sees which file is converting.
    # The runner emits discriminated events via the `details["kind"]`
    # field (see ingest_runner.ProgressFn) which we switch on here.
    tasklist = TaskList()
    tasklist.status = "Running"
    await tasklist.send()

    # Human-readable labels for the top-level stages.
    _stage_titles = {
        "stage": "Stage files",
        "preprocess": "Convert documents",
        "chunk": "Chunk text",
        "extract": "LLM entity extraction",
        "write": "Write to knowledge graph",
    }
    # One top-level Task per stage, keyed by stage name. Per-file tasks are
    # nested under their stage; we track them to update statuses in place.
    stage_tasks: dict[str, Task] = {}
    file_tasks: dict[tuple[str, str], Task] = {}

    async def _get_stage_task(stage: str) -> Task:
        task = stage_tasks.get(stage)
        if task is None:
            task = Task(title=_stage_titles.get(stage, stage), status=TaskStatus.RUNNING)
            stage_tasks[stage] = task
            await tasklist.add_task(task)
            await tasklist.update()
        return task

    async def _progress(label: str, details: dict | None = None) -> None:
        kind = (details or {}).get("kind", "info")
        stage = (details or {}).get("stage", "")
        fname = (details or {}).get("file")

        if kind == "stage_start":
            await _get_stage_task(stage)
        elif kind == "stage_end":
            task = stage_tasks.get(stage)
            if task:
                task.status = TaskStatus.DONE
                await tasklist.update()
        elif kind == "file_start" and fname:
            await _get_stage_task(stage)
            t = Task(title=f"{stage}: {fname}", status=TaskStatus.RUNNING)
            file_tasks[(stage, fname)] = t
            await tasklist.add_task(t)
            await tasklist.update()
        elif kind == "file_end" and fname:
            t = file_tasks.pop((stage, fname), None)
            if t:
                t.status = TaskStatus.DONE
                await tasklist.update()
        elif kind == "error" and fname:
            t = file_tasks.pop((stage, fname), None)
            if t:
                t.status = TaskStatus.FAILED
                await tasklist.update()
            err = (details or {}).get("error", "")
            err_task = Task(
                title=f"{stage}: {fname} — failed: {err[:160]}",
                status=TaskStatus.FAILED,
            )
            await tasklist.add_task(err_task)
            await tasklist.update()
        elif kind == "error":
            err = (details or {}).get("error", "")
            err_task = Task(
                title=f"{stage or 'pipeline'} — failed: {err[:160]}",
                status=TaskStatus.FAILED,
            )
            await tasklist.add_task(err_task)
            await tasklist.update()
        # info events: no task change, the label still surfaces in chat if needed.

    yaml_path = os.getenv("DOCPREP_YAML", "")
    overwrite = os.getenv("DOCPREP_OVERWRITE", "").lower() in ("1", "true", "yes")

    try:
        result = await run_ingestion(
            files,
            chunk_size=int(os.getenv("INGEST_CHUNK_SIZE", "4000")),
            overlap=int(os.getenv("INGEST_OVERLAP", "200")),
            concurrency=int(os.getenv("INGEST_CONCURRENCY", "4")),
            docprep_yaml=yaml_path,
            overwrite_preprocessed=overwrite,
            progress=_progress,
        )
    except Exception as exc:  # noqa: BLE001 — UI must stay usable on failure
        logger.error("Ingestion pipeline failed: %s", exc)
        # Mark any still-running stage as failed so the panel doesn't hang.
        for task in stage_tasks.values():
            if task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.FAILED
        tasklist.status = "Failed"
        await tasklist.update()
        await cl.Message(
            content=f"Ingestion failed: {exc}",
        ).send()
        return

    # Finalize the panel: mark any stage not already closed as done.
    for task in stage_tasks.values():
        if task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.DONE
    tasklist.status = "Done"
    await tasklist.update()

    errors = result.get("errors") or []
    summary_lines = [
        f"**Ingestion complete** into graph `{active_graph}`.",
        f"- Files staged: {result['files_staged']}",
        f"- Files preprocessed: {result['files_preprocessed']}",
        f"- Chunks processed: {result['chunks_processed']}",
        f"- LLM extractions: {result['extractions']}",
        f"- Cypher statements: {result['cypher_statements']}",
        f"- Nodes in graph: {result['nodes_in_graph']}",
        f"- Conflicts detected: {result['conflicts_detected']}",
        f"- Merge mode: {result['merge_mode']}",
    ]
    if errors:
        summary_lines.append(f"- Errors ({len(errors)}):")
        for e in errors[:10]:
            summary_lines.append(f"  - {e}")
        if len(errors) > 10:
            summary_lines.append(f"  - …and {len(errors) - 10} more")
    await cl.Message(content="\n".join(summary_lines)).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    # Re-install the per-session FalkorDB backend in this handler task's
    # context. Chainlit runs on_chat_start / on_settings_update / on_message
    # as separate asyncio tasks, so a contextvar set during build_agent (in
    # on_chat_start) is invisible here. We persisted the live backend in
    # user_session (keyed by session id, so it survives across handler tasks);
    # restore it now so the tools target the user's selected graph.
    from falkordb_harness.backend import set_session_backend

    session_backend = cl.user_session.get("session_backend")
    if session_backend is not None:
        set_session_backend(session_backend)

    agent = cl.user_session.get("agent")
    chat_history: list = cl.user_session.get("chat_history")

    user_content = message.content or ""

    if message.elements:
        for element in message.elements:
            if hasattr(element, "path") and element.path:
                dest = ORIGINALS_DIR / element.name
                shutil.copy2(element.path, dest)
                # Root-relative virtual path under DATA_DIR so the agent's
                # file_metadata / read_excerpt / ls tools (rooted at DATA_DIR)
                # can resolve it on this and subsequent turns.
                from falkordb_harness.tools._paths import virtual_path

                virtual = virtual_path(dest)
                user_content += f"\n[Uploaded file: {virtual}]"
                cl.user_session.set("last_uploaded_path", virtual)
                # Track the absolute on-disk path so the Ingest button can
                # run the pipeline without re-resolving from the virtual path.
                uploaded = cl.user_session.get("uploaded_files") or []
                if dest not in uploaded:
                    uploaded.append(dest)
                cl.user_session.set("uploaded_files", uploaded)

    if message.elements:
        selection = cl.user_session.get("graph_selection") or {}
        active_graph = selection.get("active_graph", _DEFAULT_GRAPH)
        uploaded = cl.user_session.get("uploaded_files") or []
        n_new = sum(1 for el in message.elements if hasattr(el, "path") and el.path)
        if n_new:
            await cl.Message(
                content=(
                    f"Received **{n_new}** file(s). "
                    f"**{len(uploaded)}** total file(s) ready for ingestion "
                    f"into graph `{active_graph}`."
                ),
                actions=[
                    Action(
                        name="ingest_documents",
                        payload={},
                        label="Ingest Documents Now",
                        tooltip=(
                            "Preprocess, chunk, LLM-extract, and write all "
                            "uploaded files into the active knowledge graph."
                        ),
                        icon="upload",
                    ),
                ],
            ).send()

    response_msg = cl.Message(content="")
    await response_msg.send()

    active_steps: dict[str, cl.Step] = {}
    full_response = ""

    agent_input = {"messages": chat_history + [HumanMessage(content=user_content)]}
    from langgraph.errors import GraphRecursionError

    from falkordb_harness.agent import _DEFAULT_RECURSION_LIMIT

    event_stream = agent.astream_events(
        agent_input,
        version="v2",
        config={"recursion_limit": _DEFAULT_RECURSION_LIMIT},
    )
    try:
        async with contextlib.aclosing(event_stream):
            async for event in event_stream:
                kind = event.get("event")

                if kind == "on_chat_model_stream":
                    metadata = event.get("metadata", {})
                    if metadata.get("langgraph_node") in ("model", "log_attachments"):
                        if metadata.get("langgraph_node") == "log_attachments":
                            continue
                    chunk = event.get("data", {}).get("chunk")
                    if chunk:
                        raw = chunk.content if hasattr(chunk, "content") else chunk
                        if isinstance(raw, list):
                            token = "".join(
                                part.get("text", "")
                                if isinstance(part, dict) and "text" in part
                                else ""
                                for part in raw
                            )
                        elif isinstance(raw, str):
                            token = raw
                        else:
                            token = str(raw) if raw else ""
                        if token:
                            full_response += token
                            await response_msg.stream_token(token)

                elif kind == "on_tool_start":
                    run_id = event.get("run_id", "")
                    tool_name = event.get("name", "tool")
                    tool_input = event.get("data", {}).get("input", "")
                    step = cl.Step(name=tool_name, type="tool")
                    try:
                        from falkordb_harness.chainlit_formatting import format_tool_input
                        step.input = format_tool_input(tool_name, tool_input)
                    except Exception:
                        step.input = str(tool_input)[:2000]
                    await step.send()
                    active_steps[run_id] = step

                elif kind == "on_tool_end":
                    run_id = event.get("run_id", "")
                    tool_name = event.get("name") or "tool"
                    step = active_steps.pop(run_id, None)
                    if step:
                        output = event.get("data", {}).get("output", "")
                        try:
                            from falkordb_harness.chainlit_formatting import format_tool_output
                            step.output = format_tool_output(tool_name, output)
                        except Exception:
                            step.output = str(output)[:2000]
                        await step.update()
    except GraphRecursionError:
        # The agent exhausted the recursion budget without reaching a stop
        # condition (the repeat-guard middleware should normally prevent
        # this, but a model that ignores the nudge can still spin). Surface a
        # friendly message with whatever partial response we collected rather
        # than letting the raw traceback reach the UI.
        logger.warning(
            "GraphRecursionError: recursion limit (%d) reached",
            _DEFAULT_RECURSION_LIMIT,
        )
        if not full_response:
            full_response = (
                "I got stuck re-checking the same things and ran out of "
                "steps before finishing. Here's what I have so far — "
                "could you rephrase or tell me which file/part to focus on?"
            )
            await response_msg.stream_token(full_response)
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in agent streaming: %s", exc, exc_info=True)
        if full_response:
            await response_msg.stream_token(
                "\n\n---\n"
                "*(Processing was interrupted by an error. "
                "The partial response above may be incomplete.)*"
            )
        else:
            full_response = (
                "An unexpected error occurred while processing your request. "
                "Please try again or rephrase your question."
            )
            await response_msg.stream_token(full_response)
        for step in active_steps.values():
            step.output = "(interrupted by error)"
            await step.update()
        active_steps.clear()

    await response_msg.update()

    chat_history.append(HumanMessage(content=user_content))
    chat_history.append(AIMessage(content=full_response))

    if len(chat_history) > MAX_HISTORY_PAIRS * 2:
        chat_history[:] = chat_history[-(MAX_HISTORY_PAIRS * 2) :]

    cl.user_session.set("chat_history", chat_history)
