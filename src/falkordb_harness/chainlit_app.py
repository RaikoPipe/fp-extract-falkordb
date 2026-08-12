"""Chainlit frontend for the FalkorDB deep-agent harness.

Run with:
    chainlit run src/falkordb_harness/chainlit_app.py --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from pathlib import Path
from typing import Any

import chainlit as cl
from chainlit import input_widget
from chainlit.action import Action
from chainlit.types import ThreadDict
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage

from falkordb_harness import auth as _auth_module  # noqa: F401
from falkordb_harness.auth import register_routes
from falkordb_harness.chainlit_elements import (
    build_ingestion_summary_plot,
    build_result_dataframe,
    build_search_score_plot,
    build_source_elements,
    build_source_elements_from_row,
)

# Side-effect imports: registering @cl.data_layer / @cl.on_app_startup hooks
# and the password auth callback at import time.
from falkordb_harness.data_layer import build_data_layer, init_db
from falkordb_harness.i18n import t
from falkordb_harness.ingest_runner import run_ingestion
from falkordb_harness.stream_recovery import (
    deregister_stream,
    register_stream,
    replay_inflight_stream,
)
from falkordb_harness.tools._paths import (
    originals_dir,
    preprocessed_dir,
    thread_originals_dir,
)

load_dotenv(override=True)

# Per-session originals subdirs live under ORIGINALS_DIR (thread-id named).
ORIGINALS_DIR = originals_dir()
# Pre-created so agent filesystem tools can ls/glob it on the first turn.
PREPROCESSED_DIR = preprocessed_dir()

MAX_HISTORY_PAIRS = 20


def _history_from_thread(thread: dict) -> list:
    """Reconstruct the agent's in-memory chat history from a persisted thread.

    Only ``user_message`` / ``assistant_message`` steps are real
    conversation turns — tool-call steps (type ``"tool"`` / ``"run"``) are
    not LLM context and are skipped. The output (assistant) or input
    (user) field carries the message text. Capped at
    ``MAX_HISTORY_PAIRS`` pairs (most recent kept) so resumed long
    threads don't blow the agent's token budget.
    """
    history: list = []
    for step in thread.get("steps", []):
        step_type = step.get("type", "")
        if step_type == "user_message":
            # Chainlit stores both user+assistant text in "output"; "input" is
            # gated on showInput and always empty for user_message. See
            # AGENTS.md "Chainlit quirks".
            content = step.get("output") or step.get("input") or ""
        elif step_type == "assistant_message":
            content = step.get("output") or ""
        else:
            continue
        if not content:
            continue
        if step_type == "user_message":
            history.append(HumanMessage(content=content))
        else:
            history.append(AIMessage(content=content))
    if len(history) > MAX_HISTORY_PAIRS * 2:
        history = history[-(MAX_HISTORY_PAIRS * 2):]
    return history

# Fallback graph listing for the sidebar widgets when FalkorDB is unreachable
# and the CLI default. UI chats preselect last-used graph or _NO_GRAPH instead.
_DEFAULT_GRAPH = os.getenv("FALKORDB_GRAPH", "factory_planning")

# "No knowledge graph selected" sentinel — agent cannot query/ingest until
# create_graph runs. Rendered as a localized "(no graph selected)" entry.
_NO_GRAPH = ""

logger = logging.getLogger("falkordb_harness.chainlit")


@cl.data_layer
def _data_layer():
    """Register the SQLAlchemy + local-storage data layer with Chainlit.

    Enables per-user thread persistence: every chat thread, its steps
    (messages), elements (uploaded files) and feedback are written to
    the SQLite database at ``DATABASE_URL``. Logged-in users see their
    past threads in the sidebar and can resume them. The data layer is
    constructed once per process and reused.
    """
    return build_data_layer()


@cl.on_app_startup
async def _on_app_startup() -> None:
    """Initialize the persistence schema and mount the auth routes.

    Runs once when the Chainlit server starts (inside its lifespan
    handler). Jobs:

    1. Create the Chainlit tables (users/threads/steps/elements/feedbacks)
       in the SQLite database if missing — Chainlit's SQLAlchemy layer
       does not auto-create them. Idempotent.
    2. Register the custom auth routes (register, verify-email, password
       reset, admin UI) and the ``/public/files`` static mount.
    3. Migrate legacy pre-auth accounts (no password hash) into a disabled
       state so they can't be accidentally approved into a passwordless
       active state.
    4. Bootstrap the first admin account from FIRST_ADMIN_* env vars
       (idempotent) so an operator can provision the initial admin without
       pre-existing credentials.
    """
    from falkordb_harness.auth import (
        bootstrap_admin_from_env,
        migrate_legacy_accounts,
    )

    layer = build_data_layer()
    try:
        await init_db(layer)
    except Exception as exc:  # noqa: BLE001 — never block server startup
        logger.error("Could not initialize data layer schema: %s", exc)
    try:
        register_routes()
    except Exception as exc:  # noqa: BLE001 — never block server startup
        logger.error("Could not register auth routes: %s", exc)
    try:
        await migrate_legacy_accounts()
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not migrate legacy accounts: %s", exc)
    try:
        await bootstrap_admin_from_env()
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not bootstrap admin account: %s", exc)


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


def _build_settings_widgets(
    graphs: list[str],
    initial_active: str | None = None,
    initial_allowed: list[str] | None = None,
) -> cl.ChatSettings:
    """Construct the tabbed chat-settings widgets.

    Two tabs:

    **Graph** — graph selection:
    - ``active_graph`` (Select): the single graph the agent targets. A
      localized "(no graph selected)" entry (value ``""``) is always
      prepended so the no-graph state is selectable. ``initial_active``
      defaults to the no-graph sentinel when not provided.
    - ``new_graph_name`` (TextInput): type a name and hit Save to create a
      new empty knowledge graph on the FalkorDB instance.
    - ``new_graph_description`` (TextInput, multi-line): optional 1-3
      sentence description seeded for the new graph (the agent can revise
      it later via ``update_graph_description``).

    **Developer Settings** (formerly Ingestion) — graph scope + pipeline
    parameters (previously env-var only). The graph-scope widgets are read
    by ``on_settings_update`` to rebuild the agent; the pipeline params
    are read by the Ingest action callback and fall back to the env vars
    when unset, so the CLI path is unaffected:
    - ``label_filter`` (Tags): default node-label filter for browsing.
    - ``allowed_graphs`` (MultiSelect): the checkbox set of graphs the
      agent may switch among at runtime via ``use_graph``.
    - ``chunk_size`` (Slider): chunk size in characters.
    - ``overlap`` (Slider): overlap between chunks.
    - ``concurrency`` (Slider): parallel LLM extraction calls.
    - ``overwrite_preprocessed`` (Switch): re-run docprep even if ``.md``
      exists.
    - ``merge_mode`` (Select): overwrite | conflict | skip.
    """
    graphs = _graphs_unique(graphs)
    # value->label mapping so the _NO_GRAPH sentinel shows a localized label.
    select_items: dict[str, str] = {_NO_GRAPH: t("graph.none")}
    for g in graphs:
        select_items[g] = g

    init_active = initial_active if initial_active is not None else _NO_GRAPH
    init_allowed = initial_allowed if initial_allowed is not None else (
        [_DEFAULT_GRAPH] if _DEFAULT_GRAPH in graphs else []
    )

    graph_tab = input_widget.Tab(
        id="graph",
        label=t("settings.tab.graph.label"),
        inputs=[
            input_widget.Select(
                id="active_graph",
                label=t("settings.active_graph.label"),
                items=select_items,
                initial_value=init_active,
                description=t("settings.active_graph.desc"),
            ),
            input_widget.TextInput(
                id="new_graph_name",
                label=t("settings.new_graph_name.label"),
                placeholder=t("settings.new_graph_name.placeholder"),
                description=t("settings.new_graph_name.desc"),
            ),
            input_widget.TextInput(
                id="new_graph_description",
                label=t("settings.new_graph_description.label"),
                placeholder=t("settings.new_graph_description.placeholder"),
                description=t("settings.new_graph_description.desc"),
                multi=True,
            ),
        ],
    )

    ingestion_tab = input_widget.Tab(
        id="ingestion",
        label=t("settings.tab.ingestion.label"),
        inputs=[
            input_widget.Tags(
                id="label_filter",
                label=t("settings.label_filter.label"),
                initial=[],
                description=t("settings.label_filter.desc"),
            ),
            input_widget.MultiSelect(
                id="allowed_graphs",
                label=t("settings.allowed_graphs.label"),
                values=graphs,
                initial=init_allowed,
                description=t("settings.allowed_graphs.desc"),
            ),
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

    # ChatSettings only accepts ``inputs=`` on Chainlit 2.11 (``tabs=`` is
    # silently dropped); Tabs serialize via _inputs_as_dicts. Language is
    # browser-driven (Accept-Language), not a tab here.
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
) -> tuple[str, list[str]]:
    """Coerce raw settings-dict values into (active, allowed) and repair state.

    - ``active_graph`` may be a graph name OR the empty-string no-graph
      sentinel (``_NO_GRAPH``); None falls back to the no-graph sentinel.
    - ``allowed_graphs`` must be a list; when the active graph is a real
      graph name, an empty/None allowed set becomes ``[active_graph]``.
      When the active graph is the no-graph sentinel, the allowed set is
      forced to ``[]`` (nothing enabled).
    - A real (non-sentinel) active graph is always inserted into the
      allowed set.
    """
    if active_graph is None or not isinstance(active_graph, str):
        active_graph = _NO_GRAPH
    active_graph = active_graph.strip() if active_graph else _NO_GRAPH
    if not active_graph:
        return _NO_GRAPH, []
    if not allowed_graphs or not isinstance(allowed_graphs, (list, tuple)):
        allowed_graphs = [active_graph]
    else:
        allowed_graphs = [str(g) for g in allowed_graphs if g]
        if active_graph not in allowed_graphs:
            allowed_graphs = [active_graph, *allowed_graphs]
    return active_graph, allowed_graphs


async def _persist_last_graph_for_user(graph: str) -> None:
    """Persist ``graph`` as the current user's last-used graph (best-effort)."""
    if not graph:
        return
    ident = cl.user_session.get("user_identifier")
    if not ident:
        return
    try:
        from falkordb_harness.graph_descriptions import set_last_graph

        await set_last_graph(ident, graph)
    except Exception:  # noqa: BLE001 — never block on persistence
        pass


def _rebuild_agent_for_selection(
    active_graph: str,
    allowed_graphs: list[str],
) -> None:
    """Rebuild the agent bound to the user's graph selection and stash it.

    Constructs the deep agent with a ``configurable`` carrying the active +
    allowed graphs and the current Chainlit thread id; :func:`build_agent`
    installs a per-session backend bound to ``active_graph`` and restricted
    to ``allowed_graphs``, and surfaces the thread id in the prompt so the
    agent knows its own per-session on-disk subdirectory.
    """
    from falkordb_harness.agent import build_agent

    try:
        thread_id = cl.context.session.thread_id
    except Exception:  # noqa: BLE001 — older Chainlit / no context
        thread_id = None

    # Role-based tool gating: admins get reset_graph. Role stashed in
    # user_session by on_chat_start/resume from cl.context.session.user.
    current_user = cl.user_session.get("user")
    role = "user"
    if current_user is not None:
        role = getattr(current_user, "metadata", {}).get("role") or "user"

    agent = build_agent(
        {
            "configurable": {
                "active_graph": active_graph,
                "allowed_graphs": allowed_graphs,
                "thread_id": thread_id,
                "role": role,
            }
        }
    )
    cl.user_session.set("agent", agent)
    cl.user_session.set(
        "graph_selection",
        {"active_graph": active_graph, "allowed_graphs": allowed_graphs},
    )
    # build_agent's contextvar backend doesn't survive across Chainlit's
    # per-handler asyncio tasks; stash it in user_session and re-install in
    # on_message so tools see the user's chosen graph.
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
    # Per-session UI language from Accept-Language; German fallback.
    from falkordb_harness.i18n import lang_from_accept_language

    browser_lang = "en-US"
    try:
        browser_lang = cl.context.session.language or "en-US"
    except Exception:  # noqa: BLE001, S110 — not in a Chainlit context
        pass
    cl.user_session.set("lang", lang_from_accept_language(browser_lang))

    # Capture authenticated user; stash object + identifier for tools/handlers.
    try:
        current_user = cl.context.session.user
    except Exception:  # noqa: BLE001 — older Chainlit / no user
        current_user = None
    if current_user is not None:
        cl.user_session.set("user", current_user)
        cl.user_session.set(
            "user_identifier", getattr(current_user, "identifier", None)
        )
    else:
        cl.user_session.set("user", None)
        cl.user_session.set("user_identifier", None)

    graphs = _list_available_graphs()

    # Preselect last-used graph (per-user, persisted). Falls to _NO_GRAPH
    # when absent or no longer on the instance. No cl.Message sent —
    # preserves starter view.
    identifier = cl.user_session.get("user_identifier")
    initial_active = _NO_GRAPH
    initial_allowed: list[str] = []
    if identifier:
        try:
            from falkordb_harness.graph_descriptions import get_last_graph

            last = await get_last_graph(identifier)
        except Exception:  # noqa: BLE001 — never block chat start
            last = None
        if last and last in graphs:
            initial_active = last
            initial_allowed = [last]
        # else: fall to no-graph state (not factory_planning)

    settings = _build_settings_widgets(graphs, initial_active, initial_allowed)
    await settings.send()

    active, allowed = _normalize_selection(initial_active, initial_allowed)
    _rebuild_agent_for_selection(active, allowed)
    cl.user_session.set("graph_switch_approved", active or None)
    cl.user_session.set("chat_history", [])
    cl.user_session.set("uploaded_files", [])
    cl.user_session.set("ingestion_settings", _default_ingestion_settings())
    cl.user_session.set("label_filter", [])
    from falkordb_harness.ui_prompts import set_ui_callback

    set_ui_callback(_ui_prompt_callback)

    # Sidebar opens only on explicit toggle click (public/docs_toggle.js),
    # which re-reads the registry via _refresh_sidebar — don't seed here.
    # Sending any assistant message (even empty + CustomElement) transitions
    # Chainlit out of the starter view into active chat — a regression.
    # custom_js sidesteps this; the warning lives in /register HTML instead.


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict) -> None:
    """Restore a persisted thread when the user reopens it from the sidebar.

    Chainlit only resumes a thread when this handler is registered
    (``threadResumable = bool(config.code.on_chat_resume)`` in the
    project settings endpoint). Without it the frontend treats past
    threads as non-resumable and clicking one falls through to
    ``on_chat_start``, which wipes ``chat_history`` and starts a fresh
    conversation — the bug where opening a past chat "deleted" history.

    Two jobs:

    1. Rebuild the same session state ``on_chat_start`` would have set
       (language, user, settings widgets, agent bound to the thread's
       graph, ingestion config, UI prompt callback) so the resumed
       thread is fully functional and new messages flow into the same
       graph. The graph selection + ingestion settings are recovered
       from the thread metadata that Chainlit persisted automatically.
    2. Reconstruct the agent's in-memory ``chat_history`` from the
       thread's persisted steps (user_message / assistant_message) so
       the agent has the conversational context it had when the thread
       was last active. Without this the agent would answer the next
       message as if the conversation had never happened.
    """
    from falkordb_harness.i18n import lang_from_accept_language

    # --- language + user (mirror on_chat_start) ---
    browser_lang = "en-US"
    try:
        browser_lang = cl.context.session.language or "en-US"
    except Exception:  # noqa: BLE001, S110
        pass
    cl.user_session.set("lang", lang_from_accept_language(browser_lang))

    try:
        current_user = cl.context.session.user
    except Exception:  # noqa: BLE001
        current_user = None
    if current_user is not None:
        cl.user_session.set("user", current_user)
        cl.user_session.set("user_identifier", getattr(current_user, "identifier", None))
    else:
        cl.user_session.set("user", None)
        cl.user_session.set("user_identifier", None)

    # --- recover persisted graph selection + ingestion settings ---
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        import json as _json

        try:
            metadata = _json.loads(metadata)
        except _json.JSONDecodeError:
            metadata = {}

    graph_selection = metadata.get("graph_selection") or {}
    active_graph = graph_selection.get("active_graph") or _NO_GRAPH
    allowed_graphs = graph_selection.get("allowed_graphs") or (
        [active_graph] if active_graph else []
    )

    # Re-send settings widgets so the sidebar reflects the resumed selection.
    graphs = _list_available_graphs()
    settings = _build_settings_widgets(graphs, active_graph, allowed_graphs)
    await settings.send()

    active, allowed = _normalize_selection(active_graph, allowed_graphs)
    _rebuild_agent_for_selection(active, allowed)
    cl.user_session.set("graph_switch_approved", active or None)
    if active:
        ident = cl.user_session.get("user_identifier")
        if ident:
            try:
                from falkordb_harness.graph_descriptions import set_last_graph

                await set_last_graph(ident, active)
            except Exception:  # noqa: BLE001 — never block resume
                pass

    # --- reconstruct chat_history from persisted steps ---
    cl.user_session.set("chat_history", _history_from_thread(thread))

    # --- restore the remaining session state ---
    cl.user_session.set("uploaded_files", metadata.get("uploaded_files") or [])
    cl.user_session.set(
        "ingestion_settings",
        metadata.get("ingestion_settings") or _default_ingestion_settings(),
    )
    cl.user_session.set("label_filter", metadata.get("label_filter") or [])

    from falkordb_harness.ui_prompts import set_ui_callback

    set_ui_callback(_ui_prompt_callback)

    # Recover in-flight streaming answer for this thread so the reconnected
    # UI shows accumulated text + live tokens. Fire-and-forget because
    # Chainlit emits resume_thread *after* on_chat_resume returns.
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if thread_id:
        asyncio.create_task(replay_inflight_stream(thread_id))
    # Sidebar not refreshed here — toggle button re-reads registry on open.


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
    new_graph_desc = (settings.get("new_graph_description") or "").strip()

    ingestion = _coerce_ingestion_settings(settings)
    cl.user_session.set("ingestion_settings", ingestion)
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
        try:
            from falkordb_harness.graph_descriptions import set_description

            await set_description(new_graph_name, new_graph_desc)
        except Exception:  # noqa: BLE001 — never block creation on desc seed
            pass
        active, allowed = _normalize_selection(new_graph_name, list(allowed_raw or []))
        if new_graph_name not in allowed:
            allowed = [new_graph_name, *allowed]
        _rebuild_agent_for_selection(active, allowed)
        cl.user_session.set("graph_switch_approved", active)
        await _persist_last_graph_for_user(active)

        graphs = _list_available_graphs()
        if active not in graphs:
            graphs = [active, *graphs]
        refreshed = _build_settings_widgets(graphs, active, allowed)
        # Clear the text field on the refreshed panel. Tabs live in
        # ChatSettings.inputs on Chainlit 2.11; walk both for safety.
        for tab in getattr(refreshed, "tabs", None) or refreshed.inputs:
            for widget in getattr(tab, "inputs", []) or []:
                if getattr(widget, "id", None) in {
                    "new_graph_name",
                    "new_graph_description",
                }:
                    widget.initial = ""
        await refreshed.send()

        await cl.Message(
            content=t(
                "settings.create.success",
                active=active,
                allowed=", ".join(allowed),
            ),
        ).send()
        # Freshly created graph has no ingested rows; docs-toggle stays hidden
        # via /api/docs-info polling — no per-session injection needed.
        return

    active, allowed = _normalize_selection(active_raw, allowed_raw)
    _rebuild_agent_for_selection(active, allowed)
    # UI dropdown counts as confirmation; stamp + persist last-used.
    cl.user_session.set("graph_switch_approved", active or None)
    if active:
        await _persist_last_graph_for_user(active)

    await cl.Message(
        content=t(
            "settings.update.success",
            active=active or t("graph.none"),
            allowed=", ".join(allowed) if allowed else t("graph.none"),
        ),
    ).send()
    # Sidebar not refreshed — toggle re-reads registry on open; button polls
    # /api/docs-info and re-renders on its own after a graph switch.


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

    # Live progress via chainlit TaskList; ``make_ingestion_progress``
    # switches on ingest_runner's discriminated ``details["kind"]`` events.
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    _tasklist, _progress, _finalize_progress = await make_ingestion_progress()

    yaml_path = os.getenv("DOCPREP_YAML", "")
    ingest_cfg = cl.user_session.get("ingestion_settings") or _default_ingestion_settings()
    overwrite = bool(ingest_cfg.get("overwrite_preprocessed", False))

    # ``finally`` so cleanup also fires on ``CancelledError`` (Chainlit stop
    # button) — ``CancelledError`` is a ``BaseException`` since Py 3.8.
    _ingest_success = False
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
        _ingest_success = not (result.get("errors") or [])
    except Exception as exc:  # noqa: BLE001 — UI must stay usable on failure
        logger.error("Ingestion pipeline failed: %s", exc)
        await cl.Message(
            content=t("ingest.failed.pipeline", exc=exc),
        ).send()
        return
    finally:
        await _finalize_progress(success=_ingest_success)

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
    # Sidebar not refreshed — toggle re-reads registry on open.
    # Auto-derive a one-line description revision (button path bypasses the
    # agent, so no LLM authors one). Best-effort; never blocks on failure.
    if active_graph and not errors:
        try:
            from falkordb_harness.graph_descriptions import append_description

            addition = (
                f"Ingested {result['files_staged']} file(s), "
                f"{result['chunks_processed']} chunk(s), "
                f"{result['nodes_in_graph']} nodes "
                f"({result['conflicts_detected']} conflict(s))."
            )
            await append_description(active_graph, addition)
        except Exception:  # noqa: BLE001
            pass


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
        "search": "search",
        "get_schema": "boxes",
        "list_graphs": "network",
        "file_metadata": "file-text",
        "read_excerpt": "file-text",
        "preprocess_document": "file-cog",
        "chunk_documents": "scissors",
        "extract_and_write": "package-plus",
        "get_reconciliations": "copy-check",
        "resolve_duplicate": "copy-check",
        "use_graph": "network",
        "reset_graph": "trash-2",
        "request_ingestion_confirmation": "clipboard-check",
        "ask_user": "message-circle-question",
    }
    _LANG = {
        "cypher_query": "cypher",
        "search": "json",
        "get_schema": "json",
        "list_graphs": "json",
        "file_metadata": "json",
        "preprocess_document": "json",
        "chunk_documents": "json",
        "extract_and_write": "json",
        "get_reconciliations": "json",
        "resolve_duplicate": "json",
        "use_graph": "json",
    }
    # Steps the user usually wants to see expanded (high-signal output).
    _OPEN = {
        "get_schema",
        "cypher_query",
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
        if tool_name == "search":
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


async def _refresh_sidebar() -> None:
    """Re-render the ElementSidebar with the document manager.

    Single source of truth for the sidebar's content. Builds a
    ``DocumentManager`` CustomElement from the document registry — uploaded
    + preprocessed rows for the current thread and ingested rows for the
    active graph. The schema-browser feature that previously shared this
    slot has been retracted (it conflicted with the document manager in the
    single ``set_elements`` slot); the schema remains viewable via the
    agent's ``get_schema`` tool output in its Step panel.

    Uses a stable ``key="main"`` so the sidebar isn't needlessly re-keyed.
    On any failure (older Chainlit without ElementSidebar, registry error)
    the call is a silent no-op — the chat still works.

    Sole caller: :func:`on_window_message` (the floating toggle button's
    open path). The sidebar is intentionally NOT auto-opened from any
    behavioral flow (chat start / resume / settings update / upload /
    ingestion / preprocessing / deletion / tool-end) — only an explicit
    user click on the toggle button opens it. The registry is mutated in
    place by those flows, so the toggle's open path re-reads current data.
    """
    try:
        import chainlit as cl
    except ImportError:
        return

    elements: list = []
    # --- Document manager ---
    try:
        props = await _build_document_manager_props()
        if props is not None:
            try:
                elements.append(
                    cl.CustomElement(name="DocumentManager", props=props)
                )
            except Exception as exc:  # noqa: BLE001 — CustomElement may be unavailable
                logger.debug("DocumentManager CustomElement build failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("document manager props failed: %s", exc)

    if not elements:
        return
    try:
        selection = cl.user_session.get("graph_selection") or {}
        active = selection.get("active_graph") or _NO_GRAPH
        await cl.ElementSidebar.set_title(
            t("sidebar.title", active=active or t("graph.none"))
        )
        await cl.ElementSidebar.set_elements(elements, key="main")
    except Exception as exc:  # noqa: BLE001 — older Chainlit lacks ElementSidebar
        logger.debug("ElementSidebar refresh failed: %s", exc)


@cl.on_window_message
async def on_window_message(data: Any) -> None:
    """Handle window.postMessage payloads from custom_js scripts.

    Currently handles the document-sidebar toggle button
    (``public/docs_toggle.js``), which sends
    ``{type: "chainlit-toggle-docs-sidebar", open: true}`` when the user
    clicks the floating toggle to OPEN the sidebar. Closing is handled
    client-side (the button clicks the sidebar's own close button), so no
    server round-trip is needed for close.

    The open path re-runs :func:`_refresh_sidebar`, which re-pushes the
    current DocumentManager element (Chainlit's ``set_elements`` re-opens
    the ElementSidebar). No-op when there are no documents to show
    (``_refresh_sidebar`` returns early). Silently ignores unknown payloads
    so other window.postMessage consumers are unaffected.
    """
    if not isinstance(data, dict):
        return
    if data.get("type") != "chainlit-toggle-docs-sidebar":
        return
    if not data.get("open"):
        return
    await _refresh_sidebar()


async def _resolve_doc_row(action: Action) -> dict | None:
    """Fetch the document row referenced by a per-row action callback.

    The per-row buttons (Open/Preprocess/Delete) send ``payload={"id": ...}``
    from the ``DocumentManager`` JSX. Returns the row dict (or ``None`` when
    the id is missing/the row was already deleted) and posts a not-found
    message for the missing case so the caller can ``return`` immediately.
    """
    payload = getattr(action, "payload", {}) or {}
    row_id = payload.get("id")
    if not row_id:
        await cl.Message(content=t("doc.open.not_found")).send()
        return None
    from falkordb_harness.document_registry import get as registry_get

    row = await registry_get(row_id)
    if row is None:
        await cl.Message(content=t("doc.open.not_found")).send()
    return row


@cl.action_callback("open_document")
async def on_open_document(action: Action) -> None:
    """Render a document inline as a Chainlit element (the "Open" button).

    Prefers the preprocessed Markdown (renders as a ``cl.Text``); falls back
    to the original (``cl.Pdf`` / ``cl.Image`` / ``cl.Text`` by extension).
    Ingested rows have no thread-scoped preview file — the user is told to
    ask the assistant for an excerpt via chat instead.
    """
    row = await _resolve_doc_row(action)
    if row is None:
        return
    from falkordb_harness.tools._paths import data_dir

    # Ingested-only rows (threadId NULL: original upload thread deleted,
    # only graph-side provenance remains) — tell the user to ask via chat.
    if row.get("threadId") is None:
        await cl.Message(content=t("doc.open.ingested_hint")).send()
        return

    # Defense-in-depth: older cached JSX could fire Open for unrenderable
    # rows (no preprocessed MD + unsupported original ext). Short-circuit
    # with a clear message instead of the generic "file not found" error.
    if not row.get("preprocessedPath"):
        from pathlib import Path as _Path

        from falkordb_harness.chainlit_elements import _IMAGE_EXTS
        from falkordb_harness.ingest_runner import _PLAIN_EXTS

        _orig_ext = _Path(row.get("originalPath") or row.get("name") or "").suffix.lower()
        if _orig_ext not in _IMAGE_EXTS | _PLAIN_EXTS | {".pdf"}:
            await cl.Message(
                content=t("doc.open.unsupported", name=row.get("name") or "")
            ).send()
            return

    elements = build_source_elements_from_row(row, data_dir())
    if not elements:
        await cl.Message(
            content=t(
                "doc.open.failed",
                name=row.get("name") or "",
                err="file not found on disk",
            )
        ).send()
        return
    await cl.Message(
        content=t("doc.open.success", name=row.get("name") or ""),
        elements=elements,
    ).send()


@cl.action_callback("preprocess_document_action")
async def on_preprocess_document(action: Action) -> None:
    """Run docprep on a single uploaded original (the "Preprocess" button).

    Reuses :func:`_preprocess_document_impl` verbatim (no agent round-trip)
    so the conversion is identical to the Ingest button's per-file step.
    Runs in a worker thread because docprep (Docling + OCR / VLM) is
    synchronous and can take minutes; the JSX keeps the button disabled
    with a spinner while this callback is in flight. Registers the result
    so the new ``preprocessed`` row appears in the sidebar.
    """
    row = await _resolve_doc_row(action)
    if row is None:
        return

    # Only uploaded originals with a thread-scoped file can be
    # preprocessed. Reject rows that are already preprocessed (the
    # preprocessed Markdown is already there — re-run via the Ingest
    # button's overwrite setting if needed) and rows whose thread is
    # gone (ingested-only provenance: no on-disk original to preprocess).
    if row.get("threadId") is None or row.get("preprocessedPath"):
        await cl.Message(content=t("doc.preprocess.wrong_stage")).send()
        return

    original_path = row.get("originalPath")
    if not original_path:
        await cl.Message(
            content=t(
                "doc.preprocess.failed",
                name=row.get("name") or "",
                err="no original path recorded",
            )
        ).send()
        return

    from falkordb_harness.tools._paths import resolve as _resolve
    from falkordb_harness.tools._paths import virtual_path

    resolved = _resolve(original_path)
    if isinstance(resolved, str):
        await cl.Message(
            content=t(
                "doc.preprocess.failed",
                name=row.get("name") or "",
                err=resolved,
            )
        ).send()
        return
    virtual = virtual_path(resolved)
    yaml_path = os.getenv("DOCPREP_YAML", "")
    ingest_cfg = cl.user_session.get("ingestion_settings") or _default_ingestion_settings()
    overwrite = bool(ingest_cfg.get("overwrite_preprocessed", False))

    await cl.Message(content=t("doc.preprocess.starting", name=row.get("name") or "")).send()

    from falkordb_harness.tools.preprocess_tools import _preprocess_document_impl

    result_json = await asyncio.to_thread(
        _preprocess_document_impl, virtual, yaml_path, overwrite
    )

    import json as _json

    try:
        data = _json.loads(result_json)
    except (_json.JSONDecodeError, TypeError):
        data = {"error": result_json}

    if isinstance(data, dict) and data.get("error"):
        await cl.Message(
            content=t(
                "doc.preprocess.failed",
                name=row.get("name") or "",
                err=str(data["error"])[:300],
            )
        ).send()
        return

    out_virtual = (data or {}).get("output_path")
    if (data or {}).get("already_exists"):
        await cl.Message(
            content=t(
                "doc.preprocess.already_exists",
                name=row.get("name") or "",
                out=out_virtual or "",
            )
        ).send()
    else:
        await cl.Message(
            content=t(
                "doc.preprocess.done",
                name=row.get("name") or "",
                out=out_virtual or "",
            )
        ).send()

    # Register the preprocessed output in the registry (best-effort),
    # mirroring _register_preprocessed_from_tool_output. Pair with the
    # uploaded original's row by using the original's name (not the .md
    # filename) — under the single-row schema the preprocessed path is a
    # column on the upload's documents row, keyed by ``(threadId, name)``
    # where ``name`` is the original filename.
    if out_virtual:
        from falkordb_harness.tools._paths import resolve as _resolve2

        pre_abs = _resolve2(out_virtual)
        if not isinstance(pre_abs, str):
            try:
                thread_id = cl.context.session.thread_id
            except Exception:  # noqa: BLE001
                thread_id = None
            user_id = cl.user_session.get("user_identifier")
            name = row.get("name") or Path(pre_abs).name
            from falkordb_harness.document_registry import register_preprocessed

            try:
                await register_preprocessed(
                    thread_id=thread_id,
                    user_identifier=user_id,
                    name=name,
                    original_path=str(resolved),
                    preprocessed_path=str(pre_abs),
                )
            except Exception as exc:  # noqa: BLE001 — never block the chat
                logger.debug("register_preprocessed failed: %s", exc)
    # Sidebar not refreshed — toggle re-reads registry on open.


@cl.action_callback("delete_document")
async def on_delete_document(action: Action) -> None:
    """Delete an uploaded/preprocessed document (the "Delete" button).

    Delegates to :func:`document_registry.delete`, which removes the row and
    unlinks its on-disk file(s). Ingested rows are permanent and raise
    :class:`IngestedDocumentNotDeletable` — the JSX hides the button for
    them, so this path is a defensive fallback. The confirmation window is
    handled client-side in the JSX (``window.confirm``) before the action
    is dispatched. Trims the session's ``uploaded_files`` list so the
    Ingest button target stays consistent.
    """
    row = await _resolve_doc_row(action)
    if row is None:
        return
    from falkordb_harness.document_registry import (
        IngestedDocumentNotDeletable,
    )
    from falkordb_harness.document_registry import (
        delete as registry_delete,
    )

    try:
        deleted = await registry_delete(row["id"])
    except IngestedDocumentNotDeletable:
        await cl.Message(content=t("doc.delete.not_deletable")).send()
        return
    if deleted is None:
        await cl.Message(content=t("doc.open.not_found")).send()
        return

    # Keep the Ingest button's target list in sync with the registry.
    original_path = deleted.get("originalPath")
    if original_path:
        uploaded = cl.user_session.get("uploaded_files") or []
        if uploaded:
            uploaded = [p for p in uploaded if str(p) != original_path]
            cl.user_session.set("uploaded_files", uploaded)

    await cl.Message(content=t("doc.delete.done", name=deleted.get("name") or "")).send()
    # Sidebar not refreshed — toggle re-reads registry on open.


async def _register_preprocessed_from_tool_output(output: Any) -> None:
    """Register a preprocessed doc in the registry from a tool's JSON output.

    Called from ``on_tool_end`` when the agent ran ``preprocess_document``
    directly (not via the Ingest button / ``extract_and_write``, which
    register inside :func:`run_ingestion`). Parses the tool's JSON output
    for ``output_path`` (the preprocessed ``.md``) and ``source`` (the
    original), resolves their absolute on-disk paths, and calls
    :func:`register_preprocessed`. Best-effort: any parse/registry error
    is swallowed so the chat never breaks on a tracking failure.
    """
    try:
        import json as _json

        raw = output if isinstance(output, str) else str(output)
        data = _json.loads(raw)
        if not isinstance(data, dict) or data.get("error"):
            return
        pre_virtual = data.get("output_path")
        src_virtual = data.get("source")
        if not pre_virtual:
            return
        from falkordb_harness.tools._paths import resolve

        pre_abs = resolve(pre_virtual)
        src_abs = resolve(src_virtual) if src_virtual else pre_abs
        if isinstance(pre_abs, str) or isinstance(src_abs, str):
            return  # resolution error string — skip registration
        try:
            thread_id = cl.context.session.thread_id
        except Exception:  # noqa: BLE001
            thread_id = None
        user_id = cl.user_session.get("user_identifier")
        from pathlib import Path

        # Under the single-row schema the preprocessed path is a column on
        # the upload's documents row, keyed by ``(threadId, name)`` where
        # ``name`` is the ORIGINAL filename (not the .md output filename).
        # Pair them by using the source filename.
        name = Path(src_abs).name if src_virtual else Path(pre_abs).name
        from falkordb_harness.document_registry import register_preprocessed

        await register_preprocessed(
            thread_id=thread_id,
            user_identifier=user_id,
            name=name,
            original_path=str(src_abs),
            preprocessed_path=str(pre_abs),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort tracking
        logger.debug("register_preprocessed from tool output failed: %s", exc)


async def _build_document_manager_props() -> dict | None:
    """Build the props for the DocumentManager CustomElement.

    Reads from the document registry:
    - document rows for the current thread (``threadId``): these carry
      ``originalPath`` and an optional ``preprocessedPath``.
    - ingestion links for the active graph (``document_ingestions``):
      a thread document is "ingested" into the active graph when a link
      row exists for its id.

    Returns ``{documents: [...], lang: "en"|"de", labels: {...}}`` or
    ``None`` if no rows (so the sidebar isn't opened empty). Each
    document dict carries the fields the JSX table needs: ``id``,
    ``name``, ``bytes``, ``mime``, ``preprocessed`` (bool —
    ``preprocessedPath`` set), ``ingested`` (bool — ingested into the
    active graph), ``ingestedAt`` (the ingestion timestamp when
    ingested), a ``path`` for the "open" action (the preprocessed path
    when available, else original), plus ``canPreprocess`` (only
    uploaded non-plain-text originals without a preprocessed path) and
    ``deletable`` (not ingested into the active graph) which gate the
    per-row action buttons. ``labels`` carries the localized button
    tooltips/confirm strings so the JSX stays a dumb view.
    """
    try:
        import chainlit as cl
    except ImportError:
        return None

    from falkordb_harness.chainlit_elements import _IMAGE_EXTS
    from falkordb_harness.document_registry import (
        list_for_thread,
    )
    from falkordb_harness.ingest_runner import _PLAIN_EXTS, _needs_preprocessing

    # Extensions the "Open" button can render inline without a preprocessed
    # Markdown fallback (mirrors build_source_elements_from_row in
    # chainlit_elements.py: PDF, images, plain text). Kept here so the JSX
    # gating matches backend capability without coupling the two modules.
    _VIEWABLE_ORIG_EXTS = _IMAGE_EXTS | _PLAIN_EXTS | {".pdf"}

    try:
        thread_id = cl.context.session.thread_id
    except Exception:  # noqa: BLE001
        thread_id = None

    selection = cl.user_session.get("graph_selection") or {}
    active_graph = selection.get("active_graph", _DEFAULT_GRAPH)

    docs: list[dict] = []
    if thread_id:
        docs.extend(await list_for_thread(thread_id))
    # Plus ingested-only provenance rows (threadId NULL) for the active
    # graph — these come from list_for_graph, which joins documents with
    # document_ingestions for the active graph. We add only the ones not
    # already in ``docs`` (the thread row, when present, is the source
    # of truth for the file's preprocessed/upload state).
    from falkordb_harness.document_registry import list_for_graph

    ingested_rows = await list_for_graph(active_graph)
    docs_by_id = {d.get("id"): d for d in docs}
    for r in ingested_rows:
        if r.get("id") not in docs_by_id:
            docs.append(r)
            docs_by_id[r.get("id")] = r

    if not docs:
        return None

    # Set of document ids ingested into the active graph (drives the
    # Ingested ids drive the "Ingested" column (built from ingested_rows
    # directly; list_for_thread doesn't join).
    ingested_ids = {r.get("id") for r in ingested_rows if r.get("id")}

    lang = cl.user_session.get("lang") or "de"
    # Trim to JSX-rendered fields. canPreprocess: uploaded original with a
    # thread-scoped file needing preprocessing. canOpen: preprocessed MD or
    # renderable original ext. threadId NULL rows are ingested-only.
    documents = []
    for d in docs:
        name = d.get("name") or ""
        has_preprocessed = bool(d.get("preprocessedPath"))
        original_ext = Path(d.get("originalPath") or name).suffix.lower()
        ingested = d.get("id") in ingested_ids
        can_preprocess = (
            d.get("threadId") is not None
            and not has_preprocessed
            and _needs_preprocessing(Path(name))
        )
        can_open = d.get("threadId") is not None and (
            has_preprocessed or original_ext in _VIEWABLE_ORIG_EXTS
        )
        documents.append(
            {
                "id": d.get("id"),
                "name": name,
                "bytes": d.get("bytes"),
                "mime": d.get("mime"),
                "preprocessed": has_preprocessed,
                "ingested": ingested,
                "ingestedAt": d.get("ingestedAt"),
                "path": d.get("preprocessedPath") or d.get("originalPath"),
                "canPreprocess": can_preprocess,
                "canOpen": can_open,
                "deletable": not ingested,
            }
        )
    labels = {
        "open": t("doc.action.open.tooltip"),
        "openDisabled": t("doc.action.open.disabled_tooltip"),
        "preprocess": t("doc.action.preprocess.tooltip"),
        "delete": t("doc.action.delete.tooltip"),
        "deleteConfirm": t("doc.action.delete.confirm"),
    }
    return {"documents": documents, "lang": lang, "labels": labels}


@cl.on_message
async def on_message(message: cl.Message) -> None:
    # Re-install the per-session backend: contextvar from build_agent (in
    # on_chat_start) doesn't survive into this handler task; user_session does.
    from falkordb_harness.backend import set_session_backend

    session_backend = cl.user_session.get("session_backend")
    if session_backend is not None:
        set_session_backend(session_backend)
    logger.error("DBG on_message: entry, session_backend=%r", session_backend)

    # docs-toggle is custom_js driven by /api/docs-info; nothing to inject here.

    agent = cl.user_session.get("agent")
    chat_history: list = cl.user_session.get("chat_history")
    logger.error("DBG on_message: agent=%r chat_history=%r", agent, chat_history)

    user_content = message.content or ""

    if message.elements:
        # Resolve the current thread id + user identifier once for the
        # document-registry uploads below. ``cl.context.session.thread_id``
        # is the same id Chainlit assigns to ``response_msg.thread_id``
        # (constructed later in this handler); reading it here lets us
        # register uploads before the assistant message exists.
        try:
            _thread_id = cl.context.session.thread_id
        except Exception:  # noqa: BLE001 — older Chainlit / no context
            _thread_id = None
        _user_id = cl.user_session.get("user_identifier")
        for element in message.elements:
            if hasattr(element, "path") and element.path:
                # Per-session subdir (None thread_id → originals/_unscoped/).
                dest = thread_originals_dir(_thread_id) / element.name
                shutil.copy2(element.path, dest)
                # Virtual path under DATA_DIR for agent filesystem tools.
                from falkordb_harness.tools._paths import virtual_path

                virtual = virtual_path(dest)
                user_content += f"\n[Uploaded file: {virtual}]"
                cl.user_session.set("last_uploaded_path", virtual)
                uploaded = cl.user_session.get("uploaded_files") or []
                if dest not in uploaded:
                    uploaded.append(dest)
                cl.user_session.set("uploaded_files", uploaded)
                # Register in the document registry (best-effort).
                try:
                    from falkordb_harness.document_registry import (
                        checksum_file,
                        register_upload,
                    )

                    _mime = getattr(element, "mime", None) or None
                    _bytes = dest.stat().st_size if dest.exists() else None
                    _checksum = None
                    try:
                        _checksum = checksum_file(dest)
                    except OSError:
                        _checksum = None
                    await register_upload(
                        thread_id=_thread_id,
                        user_identifier=_user_id,
                        name=element.name,
                        original_path=str(dest),
                        mime=_mime,
                        bytes_size=_bytes,
                        checksum=_checksum,
                    )
                except Exception as exc:  # noqa: BLE001 — never block the chat
                    logger.debug("register_upload failed: %s", exc)

    if message.elements:
        selection = cl.user_session.get("graph_selection") or {}
        active_graph = selection.get("active_graph") or _NO_GRAPH
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
            # Sidebar not refreshed — toggle re-reads registry on open.

    response_msg = cl.Message(content="")
    await response_msg.send()

    # Register the in-flight stream so on_chat_resume can replay it after
    # reconnect. Cleared in the finally block once the stream concludes.
    _stream_thread_id = response_msg.thread_id
    register_stream(_stream_thread_id, response_msg)

    # Lazy factory so extract_and_write (inside LangGraph's tool coroutine)
    # can build a live TaskList via user_session. Cleared after the stream.
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    async def _ingest_progress_factory():
        return await make_ingestion_progress()

    cl.user_session.set("ingest_progress_factory", _ingest_progress_factory)

    active_steps: dict[str, cl.Step] = {}
    full_response = ""
    # Tool text streams into a collapsible "Thinking" Step; replayed into
    # response_msg as the visible answer after the stream ends.
    _any_tool_called = False
    thinking_step: cl.Step | None = None
    thinking_text: str = ""
    # Visual elements collected during the stream, attached to the final msg.
    pending_elements: list = []

    # Same-tool chain aggregation: consecutive calls to the SAME tool collapse
    # into one cl.Step ("<tool> x N"). Any non-tool event breaks the chain.
    _chain_step: cl.Step | None = None
    _chain_tool: str | None = None
    _chain_count: int = 0
    run_index: dict[str, int] = {}

    async def _close_chain_step() -> None:
        """Reset the chain-aggregate state.

        Non-reentrant: harmless to call when no chain is open. The step
        itself is left as-is on the chat (its content is already
        persisted); only the locals are cleared so the next on_tool_start
        builds a fresh step.
        """
        nonlocal _chain_step, _chain_tool, _chain_count
        _chain_step = None
        _chain_tool = None
        _chain_count = 0
        run_index.clear()

    agent_input = {"messages": chat_history + [HumanMessage(content=user_content)]}
    from langgraph.errors import GraphRecursionError

    from falkordb_harness.agent import _DEFAULT_RECURSION_LIMIT

    logger.error("DBG on_message: about to call astream_events, agent=%r", agent)
    try:
        event_stream = agent.astream_events(
            agent_input,
            version="v2",
            config={"recursion_limit": _DEFAULT_RECURSION_LIMIT},
        )
    except Exception as _e:
        logger.error("DBG on_message: astream_events() raised %r", _e, exc_info=True)
        raise
    logger.error("DBG on_message: astream_events returned, entering loop")
    try:
        async with contextlib.aclosing(event_stream):
            async for event in event_stream:
                kind = event.get("event")

                # Non-tool events break the chain; on_tool_end completes the
                # in-flight call and is exempted.
                if kind not in ("on_tool_start", "on_tool_end"):
                    if _chain_step is not None:
                        await _close_chain_step()

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
                            if _any_tool_called:
                                thinking_text += token
                                if thinking_step is None:
                                    thinking_step = cl.Step(
                                        name=t("thinking.label"),
                                        type="tool",
                                        parent_id=response_msg.id,
                                        default_open=False,
                                    )
                                    try:
                                        thinking_step.icon = "brain"
                                    except Exception:
                                        pass
                                    await thinking_step.send()
                                thinking_step.output = thinking_text
                                await thinking_step.update()
                            else:
                                await response_msg.stream_token(token)

                elif kind == "on_tool_start":
                    run_id = event.get("run_id", "")
                    tool_name = event.get("name", "tool")
                    tool_input = event.get("data", {}).get("input", "")
                    _icon, _lang, _open = _step_meta(tool_name)

                    _any_tool_called = True

                    # Same-tool chain: reuse the open step if the tool matches
                    # and no non-tool event broke the chain; else start fresh.
                    if _chain_step is not None and _chain_tool != tool_name:
                        await _close_chain_step()

                    if _chain_step is None:
                        step = cl.Step(name=tool_name, type="tool")
                        step.parent_id = response_msg.id
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
                        step.input = ""
                        step.output = ""
                        await step.send()
                        _chain_step = step
                        _chain_tool = tool_name
                        _chain_count = 0
                    else:
                        step = _chain_step

                    _chain_count += 1
                    n = _chain_count
                    run_index[run_id] = n
                    active_steps[run_id] = step

                    if n >= 2:
                        try:
                            step.name = t("tools.chain.header", tool=tool_name, n=n)
                        except Exception as exc:  # noqa: BLE001 — older Chainlit
                            logger.debug("step.name unsupported: %s", exc)
                        try:
                            if f"x{n}" not in (step.tags or []):
                                step.tags = (step.tags or []) + [f"x{n}"]
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("step.tags append unsupported: %s", exc)

                    # Append this call's input as a numbered section.
                    try:
                        from falkordb_harness.chainlit_formatting import (
                            format_tool_input,
                        )
                        formatted_in = format_tool_input(tool_name, tool_input)
                    except Exception:
                        formatted_in = str(tool_input)[:2000]
                    header = t("tools.chain.call_input", n=n)
                    if step.input:
                        step.input += "\n\n"
                    step.input += f"{header}\n{formatted_in}"
                    try:
                        await step.update()
                    except Exception as exc:  # noqa: BLE001 — never block the stream
                        logger.debug("chain step.input update failed: %s", exc)

                elif kind == "on_tool_end":
                    run_id = event.get("run_id", "")
                    tool_name = event.get("name") or "tool"
                    step = active_steps.pop(run_id, None)
                    n = run_index.pop(run_id, 0)
                    output = event.get("data", {}).get("output", "")
                    if step:
                        try:
                            from falkordb_harness.chainlit_formatting import (
                                format_tool_output,
                            )
                            formatted_out = format_tool_output(tool_name, output)
                        except Exception:
                            formatted_out = str(output)[:2000]
                        header = t("tools.chain.call_output", n=n or 1)
                        if step.output:
                            step.output += "\n\n"
                        step.output += f"{header}\n{formatted_out}"
                        try:
                            await step.update()
                        except Exception as exc:  # noqa: BLE001 — never block
                            logger.debug("chain step.output update failed: %s", exc)

                    # Build visual elements for the final assistant message.
                    # Builders are fail-safe; collected (not sent) to keep the
                    # chat compact — the Step shows the formatted output.
                    await _collect_visual_elements(
                        tool_name, output, pending_elements
                    )
                    # Register preprocessed docs when the agent ran
                    # preprocess_document directly (extract_and_write
                    # registers inside run_ingestion). Best-effort.
                    if tool_name == "preprocess_document":
                        await _register_preprocessed_from_tool_output(output)
                    # reset_graph wipes graph data; clear the registry's
                    # ingested rows for the active graph to match. Best-effort.
                    if tool_name == "reset_graph":
                        try:
                            selection = cl.user_session.get(
                                "graph_selection"
                            ) or {}
                            active = selection.get(
                                "active_graph", _DEFAULT_GRAPH
                            )
                            from falkordb_harness.document_registry import (
                                clear_ingested_for_graph,
                            )

                            await clear_ingested_for_graph(active)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("clear_ingested failed: %s", exc)
    except GraphRecursionError:
        # Recursion budget exhausted (repeat-guard normally prevents this).
        # Surface a friendly message + partial response instead of a traceback.
        logger.warning(
            "GraphRecursionError: recursion limit (%d) reached",
            _DEFAULT_RECURSION_LIMIT,
        )
        if not full_response:
            full_response = t("error.recursion")
            await response_msg.stream_token(full_response)
    except Exception as exc:
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
        await _close_chain_step()
    finally:
        # Deregister the in-flight stream (response_msg.update below persists
        # the full text for a normal resume). Finally guarantees cleanup even
        # if an except handler raised.
        deregister_stream(_stream_thread_id)
        # Drop the per-turn factory so a stale closure can't be reused. Cleared
        # here (not at end of on_message) so CancelledError still drops it.
        cl.user_session.set("ingest_progress_factory", None)

    await response_msg.update()

    # Attach visual elements collected during the stream so they render with
    # the streamed text (not as a trailing element-only message).
    if pending_elements:
        try:
            response_msg.elements = pending_elements
            await response_msg.update()
        except Exception as exc:  # noqa: BLE001 — never break on element send
            logger.debug("element attach failed: %s", exc)

    # Tools streamed their answer into the thinking step; replay into
    # response_msg so the user sees it in the main message.
    if _any_tool_called and thinking_text and not response_msg.content:
        response_msg.content = thinking_text
        await response_msg.update()

    chat_history.append(HumanMessage(content=user_content))
    chat_history.append(AIMessage(content=full_response))

    if len(chat_history) > MAX_HISTORY_PAIRS * 2:
        chat_history[:] = chat_history[-(MAX_HISTORY_PAIRS * 2) :]

    cl.user_session.set("chat_history", chat_history)
