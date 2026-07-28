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
from typing import Any

import chainlit as cl
from chainlit import input_widget
from chainlit.action import Action
from chainlit.element import Task, TaskList, TaskStatus
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage

from falkordb_harness.chainlit_elements import (
    build_ingestion_summary_plot,
    build_label_distribution_plot,
    build_rel_distribution_plot,
    build_result_dataframe,
    build_schema_card_props,
    build_search_score_plot,
    build_source_elements,
)
from falkordb_harness.i18n import t
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
    """Construct the tabbed chat-settings widgets.

    Two tabs:

    **Graph** — graph selection (unchanged behaviour):
    - ``active_graph`` (Select): the single graph the agent targets.
    - ``allowed_graphs`` (MultiSelect): the checkbox set of graphs the agent
      may switch among at runtime via ``use_graph``.
    - ``new_graph_name`` (TextInput): type a name and hit Save to create a
      new empty knowledge graph on the FalkorDB instance.

    **Ingestion** — pipeline parameters (previously env-var only). These
    are read by the Ingest action callback and fall back to the env vars
    when unset, so the CLI path is unaffected:
    - ``chunk_size`` (Slider): chunk size in characters.
    - ``overlap`` (Slider): overlap between chunks.
    - ``concurrency`` (Slider): parallel LLM extraction calls.
    - ``overwrite_preprocessed`` (Switch): re-run docprep even if ``.md``
      exists.
    - ``merge_mode`` (Select): overwrite | conflict | skip.
    """
    # Ensure the default is present in the list even if FalkorDB returned it.
    if _DEFAULT_GRAPH not in graphs:
        graphs = [*_graphs_unique(graphs), _DEFAULT_GRAPH]

    graph_tab = input_widget.Tab(
        id="graph",
        label=t("settings.tab.graph.label"),
        inputs=[
            input_widget.Select(
                id="active_graph",
                label=t("settings.active_graph.label"),
                values=graphs,
                initial_value=_DEFAULT_GRAPH,
                description=t("settings.active_graph.desc"),
            ),
            input_widget.MultiSelect(
                id="allowed_graphs",
                label=t("settings.allowed_graphs.label"),
                values=graphs,
                initial=[_DEFAULT_GRAPH],
                description=t("settings.allowed_graphs.desc"),
            ),
            input_widget.TextInput(
                id="new_graph_name",
                label=t("settings.new_graph_name.label"),
                placeholder=t("settings.new_graph_name.placeholder"),
                description=t("settings.new_graph_name.desc"),
            ),
            input_widget.Tags(
                id="label_filter",
                label=t("settings.label_filter.label"),
                initial=[],
                description=t("settings.label_filter.desc"),
            ),
        ],
    )

    ingestion_tab = input_widget.Tab(
        id="ingestion",
        label=t("settings.tab.ingestion.label"),
        inputs=[
            input_widget.Slider(
                id="chunk_size",
                label=t("settings.chunk_size.label"),
                initial=int(os.getenv("INGEST_CHUNK_SIZE", "4000")),
                min=500,
                max=8000,
                step=500,
                description=t("settings.chunk_size.desc"),
            ),
            input_widget.Slider(
                id="overlap",
                label=t("settings.overlap.label"),
                initial=int(os.getenv("INGEST_OVERLAP", "200")),
                min=0,
                max=1000,
                step=50,
                description=t("settings.overlap.desc"),
            ),
            input_widget.Slider(
                id="concurrency",
                label=t("settings.concurrency.label"),
                initial=int(os.getenv("INGEST_CONCURRENCY", "4")),
                min=1,
                max=16,
                step=1,
                description=t("settings.concurrency.desc"),
            ),
            input_widget.Switch(
                id="overwrite_preprocessed",
                label=t("settings.overwrite_preprocessed.label"),
                initial=False,
                description=t("settings.overwrite_preprocessed.desc"),
            ),
            input_widget.Select(
                id="merge_mode",
                label=t("settings.merge_mode.label"),
                values=["overwrite", "conflict", "skip"],
                initial_value=os.getenv("MERGE_MODE", "overwrite"),
                description=t("settings.merge_mode.desc"),
            ),
        ],
    )

    # NOTE: ChatSettings.__init__ only accepts ``inputs=`` (the ``tabs=``
    # kwarg shown in the docs is aspirational and silently dropped on
    # Chainlit 2.11). Pass the Tab objects via ``inputs`` — the field's
    # type is ``List[InputWidget] | List[Tab]`` and ``_inputs_as_dicts``
    # serializes Tabs recursively, so the UI renders them as tabs.
    #
    # Language selection is intentionally NOT a tab here: the UI language
    # (and the README language) follow the browser's Accept-Language header
    # automatically (see i18n.lang_from_accept_language and on_chat_start).
    return cl.ChatSettings(inputs=[graph_tab, ingestion_tab])


def _graphs_unique(graphs: list[str]) -> list[str]:
    """Return ``graphs`` de-duplicated, order-preserving."""
    seen: set[str] = set()
    out: list[str] = []
    for g in graphs:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _default_ingestion_settings() -> dict:
    """Return ingestion settings seeded from env vars (the Ingestion tab's
    initial values mirror these). Used on chat start and as a fallback when
    the user never opens the Ingestion tab.
    """
    return {
        "chunk_size": int(os.getenv("INGEST_CHUNK_SIZE", "4000")),
        "overlap": int(os.getenv("INGEST_OVERLAP", "200")),
        "concurrency": int(os.getenv("INGEST_CONCURRENCY", "4")),
        "overwrite_preprocessed": os.getenv(
            "DOCPREP_OVERWRITE", ""
        ).lower() in ("1", "true", "yes"),
        "merge_mode": os.getenv("MERGE_MODE", "overwrite"),
    }


def _coerce_ingestion_settings(settings: dict) -> dict:
    """Pull ingestion-tab values from a settings dict, falling back to env.

    Coerces types (Slider -> int, Switch -> bool, Select -> str) and
    ignores missing keys so a partial settings dict (e.g. from an older
    client that only sent the Graph tab) still works.
    """
    base = _default_ingestion_settings()
    try:
        if "chunk_size" in settings:
            base["chunk_size"] = int(settings["chunk_size"])
        if "overlap" in settings:
            base["overlap"] = int(settings["overlap"])
        if "concurrency" in settings:
            base["concurrency"] = int(settings["concurrency"])
        if "overwrite_preprocessed" in settings:
            base["overwrite_preprocessed"] = bool(
                settings["overwrite_preprocessed"]
            )
        if "merge_mode" in settings:
            base["merge_mode"] = str(settings["merge_mode"])
    except (TypeError, ValueError) as exc:
        logger.warning("Could not coerce ingestion settings: %s", exc)
    return base


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


@cl.set_starter_categories
async def set_starter_categories() -> list[cl.StarterCategory]:
    """Group starters into Query / Ingest / Inspect categories.

    Categories appear as clickable buttons; selecting one reveals its
    starters. This replaces the flat starter list so the user can quickly
    find the kind of action they want.
    """
    _icon = "/public/logo.svg"
    query = cl.StarterCategory(
        label=t("starter.category.query.label"),
        icon=_icon,
        starters=[
            cl.Starter(
                label=t("starter.query.machines.label"),
                message=t("starter.query.machines.message"),
                icon=_icon,
            ),
            cl.Starter(
                label=t("starter.query.transport.label"),
                message=t("starter.query.transport.message"),
                icon=_icon,
            ),
            cl.Starter(
                label=t("starter.query.shifts.label"),
                message=t("starter.query.shifts.message"),
                icon=_icon,
            ),
            cl.Starter(
                label=t("starter.query.search_resource.label"),
                message=t("starter.query.search_resource.message"),
                icon=_icon,
            ),
        ],
    )
    inspect = cl.StarterCategory(
        label=t("starter.category.inspect.label"),
        icon=_icon,
        starters=[
            cl.Starter(
                label=t("starter.inspect.schema.label"),
                message=t("starter.inspect.schema.message"),
                icon=_icon,
            ),
            cl.Starter(
                label=t("starter.inspect.reconciliations.label"),
                message=t("starter.inspect.reconciliations.message"),
                icon=_icon,
            ),
        ],
    )
    ingest = cl.StarterCategory(
        label=t("starter.category.ingest.label"),
        icon=_icon,
        starters=[
            cl.Starter(
                label=t("starter.ingest.how.label"),
                message=t("starter.ingest.how.message"),
                icon=_icon,
            ),
        ],
    )
    return [query, inspect, ingest]


async def _ui_prompt_callback(**kwargs: Any) -> str:
    """Handle UI prompt requests from the agent's interactive tools.

    Dispatches on ``kind``:
    - ``confirm``: emits an ``AskActionMessage`` with Confirm/Cancel
      buttons and returns ``"confirmed"`` / ``"cancelled"``.
    - ``question``: emits an ``AskUserMessage`` and returns the user's
      free-text answer.

    On timeout (user didn't respond) returns ``"cancelled"`` for confirms
    and ``"(no response)"`` for questions so the agent can recover.
    """
    kind = kwargs.get("kind", "")
    if kind == "confirm":
        res = await cl.AskActionMessage(
            content=kwargs.get("summary", t("ui_prompt.confirm.default")),
            actions=[
                Action(
                    name="confirm",
                    payload={"value": "confirmed"},
                    label=t("ui_prompt.confirm.label"),
                ),
                Action(
                    name="cancel",
                    payload={"value": "cancelled"},
                    label=t("ui_prompt.cancel.label"),
                ),
            ],
            timeout=300,
        ).send()
        if res is None:
            return "cancelled"
        return str((res.get("payload") or {}).get("value", "cancelled"))
    if kind == "question":
        res = await cl.AskUserMessage(
            content=kwargs.get("question", t("ui_prompt.question.default")),
            timeout=300,
        ).send()
        if res is None:
            return t("ui_prompt.no_response")
        # AskUserMessage returns a StepDict with an "output" key.
        return str(res.get("output", "") or t("ui_prompt.no_response"))
    return t("ui_prompt.unknown_kind", kind=kind)


@cl.on_chat_start
async def on_chat_start() -> None:
    # Per-session UI language. Driven by the browser's Accept-Language header
    # (exposed by Chainlit as cl.context.session.language). German is the
    # fallback for any locale that is not recognisably English; the README
    # (chainlit_de-DE.md / chainlit_en-US.md) follows the same routing on the
    # Chainlit server side. There is no manual language switcher.
    from falkordb_harness.i18n import lang_from_accept_language

    browser_lang = "en-US"
    try:
        browser_lang = cl.context.session.language or "en-US"
    except Exception:  # noqa: BLE001, S110 — not in a Chainlit context
        pass
    cl.user_session.set("lang", lang_from_accept_language(browser_lang))

    graphs = _list_available_graphs()
    settings = _build_settings_widgets(graphs)
    await settings.send()

    active, allowed = _normalize_selection(_DEFAULT_GRAPH, [_DEFAULT_GRAPH])
    _rebuild_agent_for_selection(active, allowed)
    cl.user_session.set("chat_history", [])
    # Track uploaded file paths across the session so the Ingest button can
    # pick them up. Each upload appends to this list (see on_message).
    cl.user_session.set("uploaded_files", [])
    # Seed ingestion settings from env defaults so the Ingest button has
    # values even before the user opens the Ingestion tab. Updated on save.
    cl.user_session.set(
        "ingestion_settings",
        _default_ingestion_settings(),
    )
    # Default node-label filter (Tags widget in the Graph tab). Empty by
    # default; the agent reads it as a UI hint when browsing nodes.
    cl.user_session.set("label_filter", [])
    # Install the interactive UI prompt callback so the agent's
    # request_ingestion_confirmation / ask_user tools emit Chainlit
    # AskActionMessage / AskUserMessage prompts (and block until the user
    # responds). Falls back to stdin in the CLI path.
    from falkordb_harness.ui_prompts import set_ui_callback

    set_ui_callback(_ui_prompt_callback)


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

    The UI language is browser-driven (see ``on_chat_start``) and is no
    longer controlled from this panel, so there is no language-tab value to
    persist here. Localized messages below use the session language already
    seeded from the browser.
    """
    active_raw = settings.get("active_graph")
    allowed_raw = settings.get("allowed_graphs")
    new_graph_name = (settings.get("new_graph_name") or "").strip()

    # Persist ingestion-tab settings into the session regardless of whether
    # the graph selection changed — the user may have only edited chunk size.
    ingestion = _coerce_ingestion_settings(settings)
    cl.user_session.set("ingestion_settings", ingestion)
    # Persist the optional node-label filter (Tags widget). Empty list when
    # unset so downstream code can default to no filter.
    label_filter = settings.get("label_filter")
    cl.user_session.set(
        "label_filter",
        [str(x) for x in label_filter] if isinstance(label_filter, list) else [],
    )

    if new_graph_name:
        # Attempt to create the new graph on the FalkorDB instance. Use a
        # throwaway backend (like _list_available_graphs) so the session
        # backend is not disturbed on failure.
        try:
            from knowledge.falkordb_backend import FalkorDBBackend

            FalkorDBBackend().create_graph(new_graph_name)
        except ValueError as exc:
            await cl.Message(
                content=t("settings.create.value_error", name=new_graph_name, exc=exc),
            ).send()
            # Fall through to rebuild with the existing selection (no new graph).
            new_graph_name = ""
        except Exception as exc:  # noqa: BLE001 — UI must stay usable on conn error
            logger.warning("Could not create FalkorDB graph %r: %s", new_graph_name, exc)
            await cl.Message(
                content=t(
                    "settings.create.unreachable",
                    name=new_graph_name,
                    exc=exc,
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
        # Tabs are stored in ChatSettings.inputs (the ``tabs=`` kwarg is
        # silently dropped on Chainlit 2.11); walk both for safety.
        for tab in getattr(refreshed, "tabs", None) or refreshed.inputs:
            for widget in getattr(tab, "inputs", []) or []:
                if getattr(widget, "id", None) == "new_graph_name":
                    widget.initial = ""
        await refreshed.send()

        await cl.Message(
            content=t(
                "settings.create.success",
                active=active,
                allowed=", ".join(allowed),
            ),
        ).send()
        return

    active, allowed = _normalize_selection(active_raw, allowed_raw)
    _rebuild_agent_for_selection(active, allowed)

    await cl.Message(
        content=t(
            "settings.update.success",
            active=active,
            allowed=", ".join(allowed),
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
            content=t("ingest.no_files"),
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
        content=t("ingest.starting", n=len(files), graph=active_graph),
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
        "stage": t("ingest.stage.stage"),
        "preprocess": t("ingest.stage.preprocess"),
        "chunk": t("ingest.stage.chunk"),
        "extract": t("ingest.stage.extract"),
        "write": t("ingest.stage.write"),
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
                title=t("ingest.failed.file", stage=stage, file=fname, err=err[:160]),
                status=TaskStatus.FAILED,
            )
            await tasklist.add_task(err_task)
            await tasklist.update()
        elif kind == "error":
            err = (details or {}).get("error", "")
            err_task = Task(
                title=t(
                    "ingest.failed.stage",
                    stage=stage or "pipeline",
                    err=err[:160],
                ),
                status=TaskStatus.FAILED,
            )
            await tasklist.add_task(err_task)
            await tasklist.update()
        # info events: no task change, the label still surfaces in chat if needed.

    yaml_path = os.getenv("DOCPREP_YAML", "")
    # Read ingestion parameters from the session (set by the Ingestion tab,
    # falling back to env vars on chat start). This lets the user tune
    # chunk size / overlap / concurrency / overwrite / merge_mode in the UI
    # without editing .env.
    ingest_cfg = cl.user_session.get("ingestion_settings") or _default_ingestion_settings()
    overwrite = bool(ingest_cfg.get("overwrite_preprocessed", False))

    try:
        result = await run_ingestion(
            files,
            chunk_size=int(ingest_cfg.get("chunk_size", 4000)),
            overlap=int(ingest_cfg.get("overlap", 200)),
            concurrency=int(ingest_cfg.get("concurrency", 4)),
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
            content=t("ingest.failed.pipeline", exc=exc),
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
        t("ingest.summary.complete", graph=active_graph),
        t("ingest.summary.files_staged", n=result["files_staged"]),
        t("ingest.summary.files_preprocessed", n=result["files_preprocessed"]),
        t("ingest.summary.chunks", n=result["chunks_processed"]),
        t("ingest.summary.extractions", n=result["extractions"]),
        t("ingest.summary.cypher", n=result["cypher_statements"]),
        t("ingest.summary.nodes", n=result["nodes_in_graph"]),
        t("ingest.summary.conflicts", n=result["conflicts_detected"]),
        t("ingest.summary.merge_mode", mode=result["merge_mode"]),
    ]
    if errors:
        summary_lines.append(t("ingest.summary.errors.header", n=len(errors)))
        for e in errors[:10]:
            summary_lines.append(f"  - {e}")
        if len(errors) > 10:
            summary_lines.append(t("ingest.summary.errors.more", n=len(errors) - 10))
    summary_elements: list = []
    chart = build_ingestion_summary_plot(result)
    if chart is not None:
        summary_elements.append(chart)
    await cl.Message(
        content="\n".join(summary_lines),
        elements=summary_elements,
    ).send()


def _step_meta(tool_name: str) -> tuple[str | None, str | None, bool]:
    """Return ``(icon, language, default_open)`` for a tool's Step panel.

    - ``icon``: a Lucide icon name rendered instead of the default avatar.
    - ``language``: syntax-highlight language for the step's input/output.
    - ``default_open``: whether the step renders expanded by default.

    Returns ``(None, None, False)`` for tools without specific metadata so
    older Chainlit versions (which lack ``icon``/``default_open``) are
    handled gracefully by the caller.
    """
    _ICONS = {
        "cypher_query": "database",
        "nl_query": "message-circle",
        "fulltext_search": "search",
        "vector_search": "search",
        "get_schema": "boxes",
        "list_nodes": "circle-dot",
        "list_edges": "link",
        "node_count": "hash",
        "list_graphs": "network",
        "file_metadata": "file-text",
        "read_excerpt": "file-text",
        "preprocess_document": "file-cog",
        "chunk_documents": "scissors",
        "extract_and_write": "package-plus",
        "get_reconciliations": "copy-check",
        "clear_reconciliations": "copy-x",
        "reconcile_posthoc": "copy-check",
        "use_graph": "network",
        "reset_graph": "trash-2",
        "request_ingestion_confirmation": "clipboard-check",
        "ask_user": "message-circle-question",
    }
    _LANG = {
        "cypher_query": "cypher",
        "list_nodes": "json",
        "list_edges": "json",
        "fulltext_search": "json",
        "vector_search": "json",
        "get_schema": "json",
        "node_count": "json",
        "list_graphs": "json",
        "file_metadata": "json",
        "preprocess_document": "json",
        "chunk_documents": "json",
        "extract_and_write": "json",
        "get_reconciliations": "json",
        "reconcile_posthoc": "json",
        "use_graph": "json",
    }
    # Steps the user usually wants to see expanded (high-signal output).
    _OPEN = {
        "get_schema",
        "cypher_query",
        "list_nodes",
        "list_edges",
        "request_ingestion_confirmation",
    }
    return (
        _ICONS.get(tool_name),
        _LANG.get(tool_name),
        tool_name in _OPEN,
    )


async def _collect_visual_elements(
    tool_name: str, output: Any, pending: list
) -> None:
    """Append visual elements for a tool's output to ``pending`` (in place).

    Each builder returns ``None`` when its optional dependency (pandas/
    plotly) is missing or the output shape is unsuitable, so this is a
    no-op in those cases. The caller (``on_message``) attaches the
    collected elements to the final assistant message.
    """
    try:
        # Dataframe for tabular results.
        df = build_result_dataframe(tool_name, output)
        if df is not None:
            pending.append(df)
        # Plotly charts keyed by tool.
        if tool_name == "list_nodes":
            chart = build_label_distribution_plot(
                output if isinstance(output, str) else str(output)
            )
            if chart is not None:
                pending.append(chart)
        elif tool_name == "list_edges":
            chart = build_rel_distribution_plot(
                output if isinstance(output, str) else str(output)
            )
            if chart is not None:
                pending.append(chart)
        elif tool_name in ("fulltext_search", "vector_search"):
            chart = build_search_score_plot(
                output if isinstance(output, str) else str(output)
            )
            if chart is not None:
                pending.append(chart)
        # Source-file elements (Pdf/Image/Text) for the pre-ingestion review.
        # Shows the original (side panel) + preprocessed Markdown (inline)
        # so the user can see what's being ingested.
        if tool_name in ("preprocess_document", "read_excerpt", "file_metadata"):
            from falkordb_harness.tools._paths import data_dir

            elements = build_source_elements(
                output if isinstance(output, str) else str(output),
                data_dir(),
            )
            pending.extend(elements)
    except Exception as exc:  # noqa: BLE001 — never break the chat on a chart
        logger.debug("visual element build failed for %s: %s", tool_name, exc)


async def _pin_schema_sidebar(schema_raw: str) -> None:
    """Pin the current graph schema to the Chainlit ElementSidebar.

    Renders the schema (labels / relationship types / property keys) as a
    ``Text`` element in the side panel so it stays visible while the user
    queries. On any failure (e.g. older Chainlit without ElementSidebar),
    the call is a silent no-op — the schema is already in the Step panel
    and the CustomElement (if sent).
    """
    try:
        import chainlit as cl
    except ImportError:
        return
    try:
        props = build_schema_card_props(schema_raw)
    except Exception:  # noqa: BLE001
        props = None
    elements: list = []
    if props is not None:
        try:
            elements.append(
                cl.CustomElement(name="SchemaBrowser", props=props)
            )
        except Exception as exc:  # noqa: BLE001 — CustomElement may be unavailable
            logger.debug("CustomElement build failed: %s", exc)
    # Always also push a Text fallback so the sidebar is useful even
    # without the JSX file mounted.
    try:
        from falkordb_harness.chainlit_formatting import format_tool_output

        rendered = format_tool_output("get_schema", schema_raw)
        elements.append(
            cl.Text(name="Schema", content=rendered, display="side")
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("schema Text fallback failed: %s", exc)
    if not elements:
        return
    try:
        selection = cl.user_session.get("graph_selection") or {}
        active = selection.get("active_graph", _DEFAULT_GRAPH)
        await cl.ElementSidebar.set_title(t("sidebar.schema.title", active=active))
        await cl.ElementSidebar.set_elements(elements)
    except Exception as exc:  # noqa: BLE001 — older Chainlit lacks ElementSidebar
        logger.debug("ElementSidebar pin failed: %s", exc)


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
                content=t(
                    "upload.receipt",
                    n_new=n_new,
                    n_total=len(uploaded),
                    graph=active_graph,
                ),
                actions=[
                    Action(
                        name="ingest_documents",
                        payload={},
                        label=t("upload.ingest_now.label"),
                        tooltip=t("upload.ingest_now.tooltip"),
                        icon="upload",
                    ),
                ],
            ).send()

    response_msg = cl.Message(content="")
    await response_msg.send()

    active_steps: dict[str, cl.Step] = {}
    full_response = ""
    # Visual elements (Dataframe/Plotly/CustomElement) collected during the
    # stream and attached to the final assistant message so the chat stays
    # compact — each tool's detailed output already lives in its Step panel.
    pending_elements: list = []
    # Track whether the agent fetched the schema so we can (a) pin it to the
    # ElementSidebar and (b) build a label-distribution chart if list_nodes
    # ran too.
    last_schema_raw: str | None = None

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
                    _icon, _lang, _open = _step_meta(tool_name)
                    step = cl.Step(name=tool_name, type="tool")
                    if _icon:
                        try:
                            step.icon = _icon
                        except Exception as exc:  # noqa: BLE001 — older Chainlit
                            logger.debug("step.icon unsupported: %s", exc)
                    if _lang:
                        step.language = _lang
                    if _open:
                        try:
                            step.default_open = True
                        except Exception as exc:  # noqa: BLE001 — older Chainlit
                            logger.debug("step.default_open unsupported: %s", exc)
                    try:
                        step.tags = [tool_name]
                    except Exception as exc:  # noqa: BLE001 — older Chainlit
                        logger.debug("step.tags unsupported: %s", exc)
                    try:
                        from falkordb_harness.chainlit_formatting import (
                            format_tool_input,
                        )
                        step.input = format_tool_input(tool_name, tool_input)
                    except Exception:
                        step.input = str(tool_input)[:2000]
                    await step.send()
                    active_steps[run_id] = step

                elif kind == "on_tool_end":
                    run_id = event.get("run_id", "")
                    tool_name = event.get("name") or "tool"
                    step = active_steps.pop(run_id, None)
                    output = event.get("data", {}).get("output", "")
                    if step:
                        try:
                            from falkordb_harness.chainlit_formatting import format_tool_output
                            step.output = format_tool_output(tool_name, output)
                        except Exception:
                            step.output = str(output)[:2000]
                        await step.update()

                    # Build visual elements for the final assistant message.
                    # All builders are fail-safe (return None on missing deps
                    # or unsuitable output), so the chat still works without
                    # pandas/plotly. We collect rather than send immediately
                    # to keep the conversation compact — the Step already
                    # shows the formatted output.
                    _collect_visual_elements(
                        tool_name, output, pending_elements
                    )
                    if tool_name == "get_schema":
                        last_schema_raw = (
                            output if isinstance(output, str) else str(output)
                        )
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
            full_response = t("error.recursion")
            await response_msg.stream_token(full_response)
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in agent streaming: %s", exc, exc_info=True)
        if full_response:
            await response_msg.stream_token(t("error.interrupted.partial"))
        else:
            full_response = t("error.unexpected")
            await response_msg.stream_token(full_response)
        for step in active_steps.values():
            step.output = t("error.interrupted.step")
            await step.update()
        active_steps.clear()

    await response_msg.update()

    # Attach any visual elements (Dataframe/Plotly) collected during the
    # stream to the final assistant message. Sending them as a separate
    # element-only message keeps the streamed text intact and the
    # conversation compact.
    if pending_elements:
        try:
            await cl.Message(
                content="",
                elements=pending_elements,
            ).send()
        except Exception as exc:  # noqa: BLE001 — never break on element send
            logger.debug("element send failed: %s", exc)

    # If the agent fetched the schema, pin it to the ElementSidebar so it
    # stays visible while the user queries. Refresh on every get_schema so
    # a use_graph switch (which re-fetches) updates the pinned view.
    if last_schema_raw is not None:
        await _pin_schema_sidebar(last_schema_raw)

    chat_history.append(HumanMessage(content=user_content))
    chat_history.append(AIMessage(content=full_response))

    if len(chat_history) > MAX_HISTORY_PAIRS * 2:
        chat_history[:] = chat_history[-(MAX_HISTORY_PAIRS * 2) :]

    cl.user_session.set("chat_history", chat_history)
