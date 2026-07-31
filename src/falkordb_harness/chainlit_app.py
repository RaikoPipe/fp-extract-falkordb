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
    build_label_distribution_plot,
    build_rel_distribution_plot,
    build_result_dataframe,
    build_search_score_plot,
    build_source_elements,
    build_source_elements_from_row,
)

# Side-effect imports: registering the @cl.data_layer / @cl.on_app_startup
# hooks and the password auth callback. These modules call into Chainlit's
# decorator API at import time, so importing them here (before any handler
# runs) wires user management + chat persistence into the running app.
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

# Uploaded raw files land in ORIGINALS_DIR (default: ./data/originals), under
# a per-session subdirectory named after the Chainlit thread id. The parent
# roots are kept for the agent's filesystem tools (rooted at DATA_DIR) and
# for any path that needs the top-level tree rather than a session subdir.
ORIGINALS_DIR = originals_dir()
# Markdown output tree is auto-created so the agent's filesystem tools can
# ls/glob into it on the very first turn (previously it was created lazily
# inside preprocess_document, so early ls calls returned path_not_found).
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
            # Chainlit's Message.to_dict() stores the text of BOTH user and
            # assistant messages in the "output" field (chainlit/message.py);
            # the "input" field is only populated for Step objects whose
            # show_input is set, which user messages never are. The SQL
            # layer (sql_alchemy.get_all_user_threads) further gates "input"
            # on showInput not in [None, "false"], so a persisted
            # user_message always returns input="" and output=<text>. Read
            # output first; fall back to input only for any legacy thread
            # whose steps were persisted the old way.
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

# Default graph, used to seed the sidebar widgets when no FalkorDB instance is
# reachable yet (e.g. starting before `docker-compose up`). Resolved from the
# same env var the backend reads.
_DEFAULT_GRAPH = os.getenv("FALKORDB_GRAPH", "factory_planning")

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
       reset, admin UI) and the ``/public/elements`` static mount.
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

    # Role-based tool gating: only admins get the destructive reset_graph
    # tool. The authenticated user's role is stashed in user_session by
    # on_chat_start/on_chat_resume (read from cl.context.session.user,
    # whose metadata carries 'role' from verify_credentials). Default to
    # 'user' when the role can't be determined (defense in depth).
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

    # Capture the authenticated user (now guaranteed because the password
    # auth callback is registered, which flips require_login() to true).
    # Stash the whole user object and the identifier separately so tools /
    # handlers can scope behaviour (e.g. per-user graph preferences) and
    # tag persisted threads without re-reading cl.context.session.
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
    # Whether the persistent "open document sidebar" floating button has
    # been sent this session. The button is NOT sent during on_chat_start so
    # the starter/startup screen is preserved (sending any chat message
    # transitions Chainlit's frontend out of the starter view). It is sent
    # lazily — on the first on_message / on_settings_update where there are
    # documents to show (see _maybe_send_open_docs_button) — and then stays
    # for the rest of the session.
    cl.user_session.set("open_docs_button_sent", False)
    # Install the interactive UI prompt callback so the agent's
    # request_ingestion_confirmation / ask_user tools emit Chainlit
    # AskActionMessage / AskUserMessage prompts (and block until the user
    # responds). Falls back to stdin in the CLI path.
    from falkordb_harness.ui_prompts import set_ui_callback

    set_ui_callback(_ui_prompt_callback)

    # Seed the sidebar with whatever documents are already tracked for
    # this thread / active graph (empty on a fresh chat). The sidebar is
    # opened via ElementSidebar.set_elements (not a chat message), so it
    # does NOT transition the frontend out of the starter screen.
    await _refresh_sidebar()
    # NOTE: _send_open_docs_button() is intentionally NOT called here.
    # Sending it would emit an assistant chat message, swapping the
    # startup/starter screen for an empty active chat. The button is
    # injected lazily on the first user turn where documents exist.

    # Send the startup welcome / acknowledgement popup (test-build warning).
    # Unlike the open-docs button, the welcome modal is a blocking overlay
    # that MUST appear on startup; it renders above any view via
    # position:fixed and is dismissed only by the "I understand and
    # acknowledge." button. Re-show is suppressed per-browser via
    # localStorage, so returning users don't see it again.
    await _send_welcome_modal()


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
    active_graph = graph_selection.get("active_graph", _DEFAULT_GRAPH)
    allowed_graphs = graph_selection.get("allowed_graphs") or [active_graph]

    # Re-send the settings widgets so the sidebar reflects the resumed
    # thread's graph selection (not the default).
    graphs = _list_available_graphs()
    settings = _build_settings_widgets(graphs)
    await settings.send()

    active, allowed = _normalize_selection(active_graph, allowed_graphs)
    _rebuild_agent_for_selection(active, allowed)

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

    # If the assistant is still streaming an answer for this thread in the
    # background (the on_message task survives a socket disconnect), recover
    # the in-flight message so the reconnected UI shows the accumulated text
    # and continues appending live tokens. Scheduled as a fire-and-forget task
    # because Chainlit emits its resume_thread socket event *after*
    # on_chat_resume returns, so the replay must land post-resume_thread to
    # avoid being overwritten by the persisted (empty-output) placeholder.
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if thread_id:
        asyncio.create_task(replay_inflight_stream(thread_id))
    # Refresh the document sidebar for the resumed thread (uploaded/
    # preprocessed rows) + active graph (ingested rows). Scheduled as a
    # fire-and-forget task for the same reason as the stream replay above
    # — so it lands after the resume_thread socket event.
    asyncio.create_task(_refresh_sidebar())
    # Re-pin the persistent "open document sidebar" button for the resumed
    # thread. A resumed thread is always an active chat (it has persisted
    # messages), so there is no starter/startup screen to disturb — sending
    # the button here does NOT swap any startup view.
    asyncio.create_task(_send_open_docs_button())
    # Mark the button as sent so on_message's _maybe_send_open_docs_button
    # guard doesn't re-send it on the next turn of this resumed thread.
    cl.user_session.set("open_docs_button_sent", True)


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
        # A freshly created graph has no ingested rows, but the thread may
        # already have uploads — lazily inject the floating Documents button
        # if there's now something to show.
        await _maybe_send_open_docs_button()
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
    # Refresh the document sidebar so ingested rows for the newly-active
    # graph appear (and the previous graph's rows disappear).
    await _refresh_sidebar()
    # The graph switch may have brought ingested rows into view (or the
    # first message of a session against a populated graph happens here
    # before any on_message). Lazily inject the floating Documents button
    # if there's now something to show and it hasn't been sent yet.
    await _maybe_send_open_docs_button()


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
    # field (see ingest_runner.ProgressFn) which the shared
    # ``make_ingestion_progress`` factory switches on.
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    _tasklist, _progress, _finalize_progress = await make_ingestion_progress()

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
        await _finalize_progress(success=False)
        await cl.Message(
            content=t("ingest.failed.pipeline", exc=exc),
        ).send()
        return

    await _finalize_progress(success=True)

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
    # Refresh the document sidebar so the newly-ingested files appear.
    await _refresh_sidebar()


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
        active = selection.get("active_graph", _DEFAULT_GRAPH)
        await cl.ElementSidebar.set_title(
            t("sidebar.title", active=active)
        )
        await cl.ElementSidebar.set_elements(elements, key="main")
    except Exception as exc:  # noqa: BLE001 — older Chainlit lacks ElementSidebar
        logger.debug("ElementSidebar refresh failed: %s", exc)


async def _send_open_docs_button() -> None:
    """Pin the persistent "open document sidebar" button to the viewport.

    Renders the ``OpenDocsButton`` CustomElement inline in a low-key
    assistant message. The JSX uses ``position: fixed`` so the button
    floats over the chat at the bottom-right corner regardless of scroll.
    Clicking it calls the ``open_document_sidebar`` action
    (:func:`on_open_document_sidebar`), which re-runs
    :func:`_refresh_sidebar` to re-open the ElementSidebar.

    Best-effort: silently no-ops on older Chainlit without CustomElement
    support, so the chat still works.
    """
    try:
        import chainlit as cl
    except ImportError:
        return
    try:
        lang = cl.user_session.get("lang") or "de"
        props = {
            "lang": lang,
            "label": t("sidebar.open_button.label"),
            "title": t("sidebar.open_button.title"),
        }
        await cl.Message(
            content="",
            elements=[cl.CustomElement(name="OpenDocsButton", props=props)],
        ).send()
    except Exception as exc:  # noqa: BLE001 — never break the chat on UI
        logger.debug("OpenDocsButton send failed: %s", exc)


async def _send_welcome_modal() -> None:
    """Send the startup welcome / acknowledgement popup (test-build warning).

    Renders the ``WelcomeModal`` CustomElement as a centered modal overlay
    via a low-key assistant message. The modal lists the compliance risks of
    this test build (hardcoded Ollama Cloud LLM provider -> no DSGVO/GDPR
    conformity, no DPA/AVV, unknown provider-side retention/logging, no
    audit logging, not security-hardened) and can only be closed by clicking
    "I understand and acknowledge." Re-show is suppressed per-browser via
    ``localStorage`` (key ``fp_welcome_ack_v1``); bump the version in the
    props to re-show after a future edit of the warning text.

    Only sent from :func:`on_chat_start` (new chats), NOT from
    :func:`on_chat_resume`. The per-browser localStorage guard means a user
    who already acknowledged won't see it again anyway, and resumed threads
    are active chats that should not be interrupted.

    Best-effort: silently no-ops on older Chainlit without CustomElement
    support, so the chat still works.
    """
    try:
        import chainlit as cl
    except ImportError:
        return
    try:
        lang = cl.user_session.get("lang") or "de"
        risks = [
            {
                "title": t("welcome.risk.cloud.title"),
                "body": t("welcome.risk.cloud.body"),
            },
            {
                "title": t("welcome.risk.compliance.title"),
                "body": t("welcome.risk.compliance.body"),
            },
            {
                "title": t("welcome.risk.retention.title"),
                "body": t("welcome.risk.retention.body"),
            },
            {
                "title": t("welcome.risk.no_audit.title"),
                "body": t("welcome.risk.no_audit.body"),
            },
            {
                "title": t("welcome.risk.not_hardened.title"),
                "body": t("welcome.risk.not_hardened.body"),
            },
        ]
        props = {
            "lang": lang,
            "title": t("welcome.title"),
            "intro": t("welcome.intro"),
            "risks": risks,
            "ackLabel": t("welcome.ack.label"),
            "dismissedKey": "fp_welcome_ack_v1",
        }
        await cl.Message(
            content="",
            elements=[cl.CustomElement(name="WelcomeModal", props=props)],
        ).send()
    except Exception as exc:  # noqa: BLE001 — never break the chat on UI
        logger.debug("WelcomeModal send failed: %s", exc)


@cl.action_callback("acknowledge_welcome")
async def on_acknowledge_welcome(action: Action) -> None:
    """Acknowledge callback for the welcome popup's "I understand" button.

    The ``WelcomeModal`` JSX already hides itself client-side and persists
    the dismissal in ``localStorage`` before calling this action, so this
    handler is effectively a no-op. It exists so ``callAction`` has a
    server-side target and to provide a hook for future server-side
    acknowledgement logging.
    """
    return


async def _maybe_send_open_docs_button() -> None:
    """Send the floating Documents button iff there are documents to show.

    Guards the once-per-session ``open_docs_button_sent`` flag so the button
    is injected at most once, and only when the document sidebar would have
    content (uploaded/preprocessed rows for this thread OR ingested rows for
    the active graph). Reuses :func:`_build_document_manager_props` as the
    "is there anything to show?" predicate. Once sent, the flag stays True
    for the rest of the session — the button persists even if the user
    later switches to an empty graph (toggling visibility would flicker;
    reopening an empty sidebar is an acceptable minor state).

    Called from:
    - :func:`on_message` (first user turn where docs exist)
    - :func:`on_settings_update` (graph switch to a populated graph)

    NOT called from :func:`on_chat_start` — sending it there would emit a
    chat message and swap the starter/startup screen for an empty chat.
    """
    try:
        import chainlit as cl
    except ImportError:
        return
    if cl.user_session.get("open_docs_button_sent"):
        return
    props = await _build_document_manager_props()
    if props is None:
        return
    await _send_open_docs_button()
    cl.user_session.set("open_docs_button_sent", True)


@cl.action_callback("open_document_sidebar")
async def on_open_document_sidebar(action: Action) -> None:
    """Re-open the ElementSidebar from the persistent OpenDocsButton.

    Re-runs :func:`_refresh_sidebar`, which re-pushes the current
    DocumentManager element (Chainlit's ``set_elements`` re-opens the
    sidebar). No-op when there are no documents to show (``_refresh_sidebar``
    returns early).
    """
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
    from falkordb_harness.document_registry import STAGE_INGESTED
    from falkordb_harness.tools._paths import data_dir

    if row.get("stage") == STAGE_INGESTED:
        await cl.Message(content=t("doc.open.ingested_hint")).send()
        return

    # Defense-in-depth: the JSX disables the Open button for rows the
    # backend can't render (no preprocessed Markdown + an original whose
    # extension build_source_elements_from_row doesn't handle, e.g. an
    # uploaded .pptx). An older cached JSX could still fire the action, so
    # short-circuit with a clear message instead of falling through to the
    # generic "file not found on disk" error (the file IS on disk — it just
    # has no inline view). Mirrors the _VIEWABLE_ORIG_EXTS set computed in
    # _build_document_manager_props.
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
    from falkordb_harness.document_registry import STAGE_UPLOADED

    if row.get("stage") != STAGE_UPLOADED:
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
    # mirroring _register_preprocessed_from_tool_output.
    if out_virtual:
        from falkordb_harness.tools._paths import resolve as _resolve2

        pre_abs = _resolve2(out_virtual)
        if not isinstance(pre_abs, str):
            try:
                thread_id = cl.context.session.thread_id
            except Exception:  # noqa: BLE001
                thread_id = None
            user_id = cl.user_session.get("user_identifier")
            name = Path(pre_abs).name
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
    await _refresh_sidebar()


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
    await _refresh_sidebar()


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

        # The preprocessed file's display name is the .md filename; the
        # original's name is the source filename. Use the original stem +
        # ".md" so it pairs with its uploaded row by name.
        name = Path(pre_abs).name
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
    - uploaded + preprocessed rows for the current thread (``threadId``).
    - ingested rows for the active graph (``graphName``).

    Returns ``{documents: [...], lang: "en"|"de", labels: {...}}`` or ``None``
    if no rows and no schema (so the sidebar isn't opened empty). Each document
    dict carries the fields the JSX component needs: ``id``, ``name``,
    ``stage``, ``bytes``, ``mime``, ``ingestedAt``, a ``path`` for the "open"
    action (the preprocessed path when available, else original), plus
    ``canPreprocess`` (only uploaded non-plain-text originals) and
    ``deletable`` (everything except ingested rows) which gate the per-row
    action buttons. ``labels`` carries the localized button tooltips/confirm
    strings so the JSX stays a dumb view.
    """
    try:
        import chainlit as cl
    except ImportError:
        return None

    from falkordb_harness.chainlit_elements import _IMAGE_EXTS
    from falkordb_harness.document_registry import (
        STAGE_INGESTED,
        STAGE_UPLOADED,
        list_for_graph,
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
    docs.extend(await list_for_graph(active_graph))

    if not docs:
        return None

    lang = cl.user_session.get("lang") or "de"
    # Trim to the fields the JSX component renders. ``canPreprocess`` is
    # True only for uploaded originals whose extension is non-plain-text
    # (preprocessed rows are already Markdown; ingested rows are permanent).
    # ``canOpen`` is True when the row has a viewable form: a preprocessed
    # Markdown path, or an original whose extension build_source_elements_from_row
    # can render (PDF / image / plain text). Ingested rows have neither and
    # are gated off regardless (no thread-scoped preview file).
    documents = []
    for d in docs:
        stage = d.get("stage")
        name = d.get("name") or ""
        can_preprocess = stage == STAGE_UPLOADED and _needs_preprocessing(
            Path(name)
        )
        has_preprocessed = bool(d.get("preprocessedPath"))
        original_ext = Path(d.get("originalPath") or name).suffix.lower()
        can_open = stage != STAGE_INGESTED and (
            has_preprocessed or original_ext in _VIEWABLE_ORIG_EXTS
        )
        documents.append(
            {
                "id": d.get("id"),
                "name": name,
                "stage": stage,
                "bytes": d.get("bytes"),
                "mime": d.get("mime"),
                "ingestedAt": d.get("ingestedAt"),
                "path": d.get("preprocessedPath") or d.get("originalPath"),
                "canPreprocess": can_preprocess,
                "canOpen": can_open,
                "deletable": stage != STAGE_INGESTED,
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

    # Lazily inject the floating "Documents" button on the first user turn
    # where there are documents to show (uploaded/preprocessed for this
    # thread, or ingested for the active graph). Once sent it persists for
    # the session. NOT sent during on_chat_start to preserve the starter
    # screen (see _maybe_send_open_docs_button).
    await _maybe_send_open_docs_button()

    agent = cl.user_session.get("agent")
    chat_history: list = cl.user_session.get("chat_history")

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
                # Per-session subdirectory so the agent's ls/glob tools
                # expose session ownership directly (mirrors the registry's
                # threadId discrimination). _thread_id was read above; it is
                # None only outside a Chainlit thread context (CLI path), in
                # which case the file lands in originals/_unscoped/.
                dest = thread_originals_dir(_thread_id) / element.name
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
                # Register the upload in the document-management registry
                # (single source of truth for the sidebar). Compute the
                # checksum lazily so a missing file or read error can't
                # break the chat — register_upload swallows its own errors
                # too, so this is best-effort tracking.
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
            # Refresh the document sidebar so the newly-uploaded files
            # appear immediately.
            await _refresh_sidebar()

    response_msg = cl.Message(content="")
    await response_msg.send()

    # Register the in-flight stream so on_chat_resume can replay its
    # accumulated content if the UI reconnects (page reload / thread
    # switch-back) while the agent is still generating. The message id
    # is stable across the stream; the registry entry is cleared in the
    # finally below once the stream concludes.
    _stream_thread_id = response_msg.thread_id
    register_stream(_stream_thread_id, response_msg)

    # Install the ingestion-progress factory so the agent's
    # ``extract_and_write`` tool (which runs inside LangGraph's tool
    # coroutine, where it can't reach this handler's locals) can build a
    # live ``cl.TaskList`` panel via ``cl.user_session``. The factory is
    # lazy — the TaskList is only created if/when ``extract_and_write`` is
    # invoked this turn — and is cleared after the stream to avoid leaking
    # across turns. Shared with the action-button path via
    # ``make_ingestion_progress`` so both ingestion entry points render
    # identical per-stage / per-file progress UI.
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    async def _ingest_progress_factory():
        return await make_ingestion_progress()

    cl.user_session.set("ingest_progress_factory", _ingest_progress_factory)

    active_steps: dict[str, cl.Step] = {}
    full_response = ""
    # Parent "Tool calls" wrapper Step. Lazily created on the first
    # on_tool_start and anchored to the assistant message (parent_id =
    # response_msg.id) so every per-tool Step nests inside it instead of
    # stacking as trailing siblings in the chat timeline — which used to
    # push the viewport away from the assistant's text. Collapsed by
    # default; auto-expands only when a tool errors.
    tool_calls_step: cl.Step | None = None
    tool_call_count = 0
    # Visual elements (Dataframe/Plotly/CustomElement) collected during the
    # stream and attached to the final assistant message so the chat stays
    # compact — each tool's detailed output already lives in its Step panel.
    pending_elements: list = []

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

                    # Lazily create the collapsed "Tool calls" wrapper,
                    # anchored to the assistant message, on the first tool
                    # of the turn. Subsequent tools nest inside it.
                    #
                    # Why type="tool": Chainlit's frontend (cot="tool_call"
                    # mode in .chainlit/config.toml) hides every non-message
                    # step whose type != "tool" — including "run" and the
                    # invalid "tools". A hidden step only renders its
                    # children, never its own header, so it can't act as a
                    # visible collapsible container. "tool" is the only
                    # non-message StepType that stays visible in this mode,
                    # and the Step panel (lKn) renders nested children
                    # (t.steps) inside a collapsible accordion — exactly
                    # the container we need.
                    if tool_calls_step is None:
                        tool_calls_step = cl.Step(
                            name="tool_calls",
                            type="tool",
                            parent_id=response_msg.id,
                            default_open=False,
                        )
                        try:
                            tool_calls_step.auto_collapse = True
                        except Exception as exc:  # noqa: BLE001 — older Chainlit
                            logger.debug("step.auto_collapse unsupported: %s", exc)
                        try:
                            tool_calls_step.tags = [t("tools.container.label")]
                        except Exception as exc:  # noqa: BLE001 — older Chainlit
                            logger.debug("step.tags unsupported: %s", exc)
                        await tool_calls_step.send()

                    tool_call_count += 1
                    try:
                        tool_calls_step.name = t(
                            "tools.container.count", n=tool_call_count
                        )
                        await tool_calls_step.update()
                    except Exception as exc:  # noqa: BLE001 — never block a turn
                        logger.debug("tool_calls_step update failed: %s", exc)

                    step = cl.Step(name=tool_name, type="tool")
                    step.parent_id = tool_calls_step.id
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
                            from falkordb_harness.chainlit_formatting import (
                                format_tool_output,
                            )
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
                    await _collect_visual_elements(
                        tool_name, output, pending_elements
                    )
                    # Register preprocessed documents in the registry when
                    # the agent ran preprocess_document directly (not via
                    # the Ingest button / extract_and_write, which register
                    # inside run_ingestion). The tool's JSON output carries
                    # ``output_path`` (preprocessed .md) and ``source``
                    # (the original). Best-effort: parse, register, refresh
                    # the sidebar so the new preprocessed file shows up.
                    if tool_name == "preprocess_document":
                        await _register_preprocessed_from_tool_output(output)
                        await _refresh_sidebar()
                    # extract_and_write registers ingested rows inside
                    # run_ingestion; refresh the sidebar now so they appear.
                    if tool_name == "extract_and_write":
                        await _refresh_sidebar()
                    # reset_graph wipes the graph data; clear the
                    # registry's ingested rows for the active graph so the
                    # sidebar matches, then refresh. Best-effort.
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
                        await _refresh_sidebar()
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
        # Auto-expand the wrapper so the failed tool's panel is visible
        # without manual expansion (it's collapsed by default otherwise).
        if tool_calls_step is not None:
            try:
                tool_calls_step.default_open = True
                await tool_calls_step.update()
            except Exception as exc:  # noqa: BLE001 — never block cleanup
                logger.debug("tool_calls_step expand failed: %s", exc)
    finally:
        # The streaming loop ended (success, error, or recursion limit).
        # Remove the in-flight stream from the recovery registry so a
        # reconnecting client doesn't replay an already-finished message.
        # The response_msg.update() call below (outside this try) then
        # persists + emits the complete text, so the step row carries the
        # full answer and a normal resume renders it verbatim. Using
        # finally (rather than a trailing line) guarantees deregistration
        # even if an except handler itself raised.
        deregister_stream(_stream_thread_id)

    await response_msg.update()

    # Attach any visual elements (Dataframe/Plotly) collected during the
    # stream to the assistant message itself so they render with the
    # streamed text rather than as a separate trailing element-only
    # message (which used to push the viewport further from the text).
    if pending_elements:
        try:
            response_msg.elements = pending_elements
            await response_msg.update()
        except Exception as exc:  # noqa: BLE001 — never break on element send
            logger.debug("element attach failed: %s", exc)

    chat_history.append(HumanMessage(content=user_content))
    chat_history.append(AIMessage(content=full_response))

    if len(chat_history) > MAX_HISTORY_PAIRS * 2:
        chat_history[:] = chat_history[-(MAX_HISTORY_PAIRS * 2) :]

    cl.user_session.set("chat_history", chat_history)
    # Drop the per-turn ingestion-progress factory so a stale closure can't
    # be reused on a later turn (each turn installs its own above).
    cl.user_session.set("ingest_progress_factory", None)
