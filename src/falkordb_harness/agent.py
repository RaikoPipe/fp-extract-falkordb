"""Build the deep agent with all FalkorDB tools.

Uses ``deepagents.create_deep_agent`` (see
https://github.com/langchain-ai/deepagents) on top of LangGraph, giving the
agent planning, filesystem, subagent, and context-management capabilities
in addition to the FalkorDB tool suite.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.types import Checkpointer

from falkordb_harness._loop_guard import RepeatGuardMiddleware
from falkordb_harness.tools import all_tools

# Default recursion limit for the agent graph. LangGraph's built-in default
# (25) is too low for the tool-heavy PRE-INGESTION REVIEW ROUTINE, which can
# legitimately chain 6+ tool calls per file; combined with the repeat-guard
# middleware this gives headroom while still bounding runaway loops.
_DEFAULT_RECURSION_LIMIT = 50

logger = logging.getLogger("falkordb_harness.attachments")
agent_logger = logging.getLogger("falkordb_harness.agent")
if not agent_logger.handlers:
    _agent_handler = logging.StreamHandler()
    _agent_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    agent_logger.addHandler(_agent_handler)
    agent_logger.setLevel(logging.INFO)
    agent_logger.propagate = False

# Toggle for the attachment wire-format logger. Controlled by the
# ``LOG_ATTACHMENTS`` env var (default ``"1"`` when unset). When enabled, the
# graph dumps the raw content of the last ``HumanMessage`` on every invocation
# so you can see exactly how `langgraph dev` delivers file uploads to the
# agent (e.g. ``image_url`` parts with ``data:`` base64 URLs).
_LOG_ATTACHMENTS = os.getenv("LOG_ATTACHMENTS", "1") not in ("", "0", "false", "no")

# Ensure the attachment logger emits to stderr even when the host process
# (e.g. `langgraph dev`) hasn't configured root logging. Set
# ``LOG_ATTACHMENTS=0`` in the env to suppress entirely.
if _LOG_ATTACHMENTS:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False


def _summarise_part(part: object) -> object:
    """Return a compact, log-safe representation of a message content part.

    Raw base64 payloads are truncated to their mime type + length so the log
    stays readable; non-string parts are serialised verbatim when possible.
    """
    if isinstance(part, dict):
        kind = part.get("type")
        if kind == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if isinstance(url, str) and url.startswith("data:"):
                head, _, payload = url.partition(",")
                size = len(payload)
                return {"type": "image_url", "image_url": {"url": f"{head},<base64 len={size}>"}}
        # Recurse into nested dicts but keep it shallow.
        try:
            return {k: _summarise_part(v) for k, v in part.items()}
        except Exception:
            return part
    if isinstance(part, list):
        return [_summarise_part(p) for p in part]
    if isinstance(part, str):
        # Truncate long inline strings (e.g. accidentally-inlined base64).
        return part if len(part) <= 500 else f"<str len={len(part)}>: {part[:200]}..."
    return part


def _log_attachments(state: dict) -> dict:
    """Pre-graph node that logs how the last human message arrived.

    Inspects ``state["messages"]`` and emits the type and a compact
    representation of the content of the last ``HumanMessage``. This is purely
    diagnostic and does not modify state.
    """
    messages: list[AnyMessage] = state.get("messages", [])
    if not messages:
        logger.info("attachments: no messages in state")
        return {}

    last = messages[-1]
    if not isinstance(last, HumanMessage):
        logger.info("attachments: last message is %s (not HumanMessage)", type(last).__name__)
        return {}

    content = last.content
    if isinstance(content, str):
        logger.info(
            "attachments: HumanMessage.content is str len=%d: %.300r",
            len(content), content,
        )
    elif isinstance(content, list):
        logger.info(
            "attachments: HumanMessage.content is list len=%d parts=%r",
            len(content),
            [p.get("type") if isinstance(p, dict) else type(p).__name__ for p in content],
        )
        for i, part in enumerate(content):
            logger.info(
                "attachments: part[%d] = %s",
                i, json.dumps(_summarise_part(part), default=repr),
            )
    else:
        logger.info("attachments: HumanMessage.content is %s: %r", type(content).__name__, content)

    # Also log any non-content metadata that tools sometimes use to pass
    # attached files (e.g. ``additional_kwargs``).
    if getattr(last, "additional_kwargs", None):
        logger.info(
            "attachments: additional_kwargs = %s",
            json.dumps(_summarise_part(last.additional_kwargs), default=repr),
        )
    return {}

SYSTEM_PROMPT = """\
You are a knowledge-graph assistant for a factory-planning FalkorDB database.

You can:
- Inspect raw source files before ingestion (file_metadata, read_excerpt)
- Preprocess binary/scanned/image documents into Markdown \
(preprocess_document)
- Ingest preprocessed Markdown into the graph (chunk_documents, extract_and_write)
- Query the graph with raw Cypher (cypher_query) or natural language (nl_query)
- Search by full-text (fulltext_search) or vector similarity (vector_search)
- Inspect the graph schema, nodes, edges, and node count \
(get_schema, list_nodes, list_edges, node_count)
- Discover which knowledge graphs exist in the FalkorDB instance (list_graphs) \
and switch the active graph among the user's enabled set (use_graph)
- Manage merge conflicts (get_conflicts, clear_conflicts)
- Manage similarity-based reconciliation of plain-name Resources: \
review POSSIBLE_DUPLICATE_OF links (get_reconciliations), dismiss reviewed ones \
(clear_reconciliations), and run a post-hoc pass over pre-existing plain-name \
nodes (reconcile_posthoc)
- Reset the graph (reset_graph) — use only when explicitly asked

PRE-INGESTION REVIEW ROUTINE (mandatory before extract_and_write):
This routine is a soft guardrail that prevents large amounts of data noise from \
entering the knowledge graph. Follow it every time the user asks to ingest from \
a directory or names files to ingest.
1. DISCOVER: call ls (or glob) on the ``originals/`` directory to list \
candidate files. The filesystem root is DATA_DIR; both ``originals/`` (raw \
uploaded sources) and ``preprocessed/`` (Markdown output) are visible under it.
2. METADATA: call file_metadata on each candidate file (or a representative \
sample if there are many) to get size, type, page/char/word counts. Pass \
paths as ``originals/<name>``.
3. EXCERPT: call read_excerpt on a FEW small slices per file — e.g. the first \
lines (text) or pages 1, 3, and the last page (PDF/DOCX) — enough to understand \
the content, not the whole file. Avoid dumping large bodies into context.
3b. PREPROCESS (when needed): if a file is a scanned PDF, image, Excel with \
charts, or any binary format where read_excerpt returned garbage, placeholders, \
or low text density, call preprocess_document(path) to convert it to Markdown \
in the ``preprocessed/`` tree. Do NOT preprocess plain .txt/.md sources — they \
are already LLM-ready and preprocessing them wastes a VLM call. After \
preprocessing, call read_excerpt on the ``output_path`` the tool returned \
(e.g. ``preprocessed/<stem>.md``) to verify the conversion before extraction.
4. SUMMARIZE: report back to the user, in plain prose, what each file contains:
   - file name, type, size, page/line count
   - a 1-3 sentence content description per file
   - anything that looks like noise, out-of-scope, or non-factory-planning data
   - which files were preprocessed and which were skipped (already Markdown)
5. CONFIRM: STOP and ask the user to confirm before calling extract_and_write. \
Do NOT call extract_and_write until the user explicitly confirms. \
chunk_documents (preview-only, no graph writes) may be used during this review \
to preview chunks, but the actual ingestion must wait for confirmation.
6. PROCEED: only after explicit user confirmation, call extract_and_write. \
extract_and_write reads from the ``preprocessed/`` tree by default; only point \
it at ``originals/`` if the user explicitly wants to ingest raw text sources \
directly.
Err on the side of showing the user too much summary rather than too little.

Guidelines:
- Before querying, call get_schema to understand available labels and relationships.
- Prefer nl_query for open-ended questions; use cypher_query when the user \
provides Cypher or when you can construct a precise query.
- Always report results clearly, including counts, conflicts detected, and \
reconciliation links.
- Reconciliation applies to Resources only and never auto-merges duplicates; \
always leave adjudication to the human via clear_reconciliations.
- Never reset the graph without explicit user confirmation.
- You are restricted to the user's enabled knowledge graphs. \
use_graph(name) will reject any graph the user has not enabled. \
When asked "which knowledge graphs are available?", answer with the session's \
enabled set (from the preamble) and/or call list_graphs for the full instance \
listing. Do NOT claim no graphs exist just because the active graph is empty.
"""


def _build_graph_context_prefix(
    active_graph: str | None,
    allowed_graphs: list[str] | None,
) -> str:
    """Build the dynamic preamble appended to SYSTEM_PROMPT for graph selection.

    Tells the agent which graph is active and which graphs are in scope, so it
    can answer "which KGs are available?" without falling back to node_count
    on the bound graph. Returns an empty string when no per-session selection
    is configured (the CLI / default path), preserving the original prompt.
    """
    if not active_graph and not allowed_graphs:
        return ""
    parts: list[str] = [
        "",
        "KNOWLEDGE GRAPH SELECTION (user-controlled for this session):",
    ]
    if active_graph:
        parts.append(f"- Active graph (all queries/ingestion target this): '{active_graph}'")
    if allowed_graphs:
        parts.append(
            "- Enabled graphs (the only ones you may switch to via use_graph): "
            + ", ".join(f"'{g}'" for g in allowed_graphs)
        )
    else:
        parts.append("- Enabled graphs: unrestricted (any graph name is accepted)")
    parts.append(
        "- To switch the active graph, call use_graph(name) with one of the "
        "enabled names. The user selected these via the UI; do not question "
        "or expand the set."
    )
    parts.append("")
    return "\n".join(parts)


def _normalize_model_id(model_name: str) -> str:
    """Translate the AGENT_LLM_MODEL convention to init_chat_model's.

    ``init_chat_model`` expects ``"<provider>:<model>"`` strings. The harness
    accepts both slash and colon forms:

    - ``anthropic/...`` / ``claude`` ids -> ``anthropic:<model>``
      (ChatAnthropic, requires ``ANTHROPIC_API_KEY``).
    - ``openai/...`` / ``gpt`` ids -> ``openai:<model>`` (ChatOpenAI, requires
      ``OPENAI_API_KEY``).
    - Bare Ollama tags (e.g. ``glm-5.2:cloud``, ``llama3.1``) ->
      ``openai:<tag>`` (ChatOpenAI pointed at the Ollama OpenAI-compatible
      endpoint). The base URL and API key are taken from ``OLLAMA_API_BASE``
      / ``OLLAMA_API_KEY`` and exported to ``OPENAI_API_BASE`` /
      ``OPENAI_API_KEY`` at model-resolution time so ``init_chat_model``'s
      ``openai`` provider picks them up. This avoids the fragile
      ``langchain-litellm`` adapter, whose content-block conversion broke
      streaming with reasoning content (bare strings passed through into the
      Ollama transformer, causing ``AttributeError: 'str' object has no
      attribute 'get'``).
    - ``openai:<model>`` / ``anthropic:<model>`` (already-colon form) are
      passed through unchanged.
    """
    # Early-return only for known provider prefixes (``openai:`` /
    # ``anthropic:``) — bare Ollama tags like ``glm-5.2:cloud`` contain a
    # colon but are not provider-prefixed, so they must fall through to the
    # Ollama-routing branch below.
    if model_name.startswith("openai:") or model_name.startswith("anthropic:"):
        return model_name
    if model_name.startswith("anthropic/") or "claude" in model_name:
        model_id = model_name.removeprefix("anthropic/")
        return f"anthropic:{model_id}"
    if model_name.startswith("openai/") or "gpt" in model_name:
        model_id = model_name.removeprefix("openai/")
        return f"openai:{model_id}"
    # Bare Ollama tags (or any other unknown id) route to the OpenAI provider,
    # which ChatOpenAI points at Ollama's OpenAI-compatible endpoint via
    # OPENAI_API_BASE / OPENAI_API_KEY (derived from OLLAMA_API_BASE /
    # OLLAMA_API_KEY in resolve_model).
    return f"openai:{model_name}"


def _provider_for(model_id: str) -> str:
    """Return the LangChain provider key for an ``init_chat_model`` id.

    Used to look up the credentials a given provider requires so we can fail
    fast with a clear message instead of letting the underlying SDK raise an
    opaque ``TypeError`` deep in the call stack.
    """
    # ``init_chat_model`` form is "<provider>:<model>".
    return model_id.split(":", 1)[0].strip().lower()


# Providers that require OpenAI-style credentials (``OPENAI_API_KEY`` or, when
# routing to Ollama's OpenAI-compatible endpoint, ``OLLAMA_API_KEY`` which
# ``resolve_model`` exports to ``OPENAI_API_KEY``).
_PROVIDERS_NEEDING_OPENAI_CREDS: tuple[str, ...] = ("openai",)


def _missing_credentials(provider: str) -> list[str]:
    """Return the list of required credential envvars that are unset.

    For the ``openai`` provider, accepts either ``OPENAI_API_KEY`` or
    ``OLLAMA_API_KEY`` (the latter is exported to the former by
    ``resolve_model`` when routing to Ollama's OpenAI-compatible endpoint).
    """
    if provider == "anthropic":
        return [name for name in ("ANTHROPIC_API_KEY",) if not os.getenv(name)]
    if provider in _PROVIDERS_NEEDING_OPENAI_CREDS:
        if os.getenv("OPENAI_API_KEY") or os.getenv("OLLAMA_API_KEY"):
            return []
        return ["OPENAI_API_KEY or OLLAMA_API_KEY"]
    return []


def resolve_model(
    model_name: str | None = None,
    temperature: float = 0.0,
) -> BaseChatModel:
    """Return a configured chat model based on the AGENT_LLM_MODEL convention.

    Routes ``anthropic/...`` / ``claude`` ids to ChatAnthropic, and everything
    else (``openai/...`` / ``gpt`` ids and bare Ollama tags like
    ``glm-5.2:cloud``) to ChatOpenAI. Bare Ollama tags are served by Ollama's
    OpenAI-compatible endpoint: ``OLLAMA_API_BASE`` / ``OLLAMA_API_KEY`` are
    exported to ``OPENAI_API_BASE`` / ``OPENAI_API_KEY`` in-process so
    ``init_chat_model``'s ``openai`` provider picks them up — keeping
    ``OLLAMA_*`` as the single source of truth in ``.env``.

    Fails fast with a clear ``RuntimeError`` listing the missing credential
    environment variables for the resolved provider, rather than letting the
    underlying SDK raise an opaque ``TypeError`` about unresolved
    authentication.
    """
    model_name = model_name or os.getenv(
        "AGENT_LLM_MODEL", "anthropic/claude-sonnet-4-20250514"
    )
    model_id = _normalize_model_id(model_name)
    provider = _provider_for(model_id)

    # When routing to the openai provider for an Ollama model, export the
    # Ollama credentials/base URL to the OpenAI env vars that
    # ``init_chat_model`` -> ``ChatOpenAI`` reads. Keep ``OLLAMA_*`` as the
    # canonical source; ``OPENAI_*`` is derived here so the user only
    # configures one backend in ``.env``.
    if provider in _PROVIDERS_NEEDING_OPENAI_CREDS and os.getenv("OLLAMA_API_KEY"):
        ollama_base = os.getenv("OLLAMA_API_BASE", "https://ollama.com")
        # ChatOpenAI expects the base URL *with* /v1 (it posts to
        # {base_url}/chat/completions).
        if not ollama_base.rstrip("/").endswith("/v1"):
            ollama_base = f"{ollama_base.rstrip('/')}/v1"
        os.environ.setdefault("OPENAI_API_BASE", ollama_base)
        os.environ.setdefault("OPENAI_API_KEY", os.getenv("OLLAMA_API_KEY", ""))

    missing = _missing_credentials(provider)
    if missing:
        raise RuntimeError(
            f"Agent LLM provider '{provider}' (model '{model_id}') is missing "
            f"required credentials: {', '.join(missing)}. Set them in your "
            f".env (e.g. ANTHROPIC_API_KEY, or OLLAMA_API_BASE + OLLAMA_API_KEY "
            f"for the Ollama OpenAI-compatible endpoint) or point "
            f"AGENT_LLM_MODEL at a provider whose credentials are already "
            f"configured."
        )

    return init_chat_model(
        model_id,
        temperature=temperature,
        streaming=True,
    )


def build_agent(
    config: RunnableConfig | None = None,
):
    """Create and return the compiled deep agent.

    The graph speaks the standard LangGraph messages protocol
    (``{"messages": [...]}`` in, ``{"messages": [...]}`` out) and is used both
    by the ``falkordb-agent`` CLI and LangGraph Studio.

    Built on ``deepagents.create_deep_agent``, so in addition to the FalkorDB
    tools the agent has access to the harness's bundled capabilities:
    planning (``write_todos``), a virtual filesystem (``ls``, ``read_file``,
    ``write_file``, ``edit_file``, ``glob``, ``grep``), shell execution
    (``execute``, inert without a sandbox backend), and subagent delegation
    (``task``). See https://docs.langchain.com/oss/python/deepagents/overview
    for details.

    The filesystem is backed by ``FilesystemBackend(root_dir=DATA_DIR,
    virtual_mode=True)`` rather than the default ephemeral ``StateBackend``,
    so ``ls``/``read_file``/``glob``/``grep`` and the custom
    ``file_metadata``/``read_excerpt`` tools all see the real on-disk
    ``originals/`` raw sources and ``preprocessed/`` Markdown output (with
    path-traversal containment). This is required for the PRE-INGESTION
    REVIEW ROUTINE in the system prompt to inspect raw files before
    ``preprocess_document`` converts them and ``extract_and_write`` ingests
    the resulting Markdown from the ``preprocessed/`` tree.

    Model selection falls back through (in order):
    1. ``config["configurable"]["model_name"]`` / ``["temperature"]``
       (per-request overrides, used by LangGraph Studio and the CLI).
    2. ``AGENT_LLM_MODEL`` / ``AGENT_LLM_TEMPERATURE`` environment variables.
    3. ``anthropic/claude-sonnet-4-20250514`` / ``0.0`` defaults.

    Knowledge-graph selection (Chainlit UI): ``config["configurable"]`` may
    carry ``active_graph`` (the single graph the agent targets) and
    ``allowed_graphs`` (the checkbox set the user enabled). When present, a
    per-session :class:`FalkorDBBackend` is constructed and installed via
    :func:`set_session_backend` so all tools route to the chosen graph, and a
    preamble is appended to the system prompt telling the agent what's in
    scope. When absent (the CLI / ``langgraph dev`` path), the module-level
    env-driven backend cache is used and the prompt is unchanged.

    The signature accepts only ``RunnableConfig`` because the LangGraph runtime
    restricts graph-factory parameters to ``ServerRuntime`` and/or
    ``RunnableConfig``.
    """
    configurable: dict = {}
    if config is not None:
        configurable = config.get("configurable", {}) or {}

    model_name = configurable.get("model_name") or os.getenv(
        "AGENT_LLM_MODEL", "anthropic/claude-sonnet-4-20250514"
    )
    temperature = configurable.get(
        "temperature",
        float(os.getenv("AGENT_LLM_TEMPERATURE", "0.0")),
    )

    llm = resolve_model(model_name, temperature)

    # Per-session knowledge-graph selection (Chainlit UI). When the user
    # picks a graph in the sidebar, the Chainlit layer passes active_graph +
    # allowed_graphs through the configurable; we build a session-scoped
    # backend bound to that graph and install it so every tool sees it via
    # get_backend(). The CLI path leaves these unset and falls back to the
    # module-level env-driven backend cache.
    active_graph: str | None = configurable.get("active_graph")
    allowed_graphs_raw = configurable.get("allowed_graphs")
    allowed_graphs: list[str] | None = None
    if isinstance(allowed_graphs_raw, (list, tuple)):
        allowed_graphs = [str(g) for g in allowed_graphs_raw if g]

    if active_graph:
        from falkordb_harness.backend import set_session_backend
        from knowledge.falkordb_backend import FalkorDBBackend

        # The active graph must always be inside the enabled set; if the UI
        # passed an inconsistent state, fix it up rather than rejecting.
        if allowed_graphs is None:
            allowed_graphs = [active_graph]
        elif active_graph not in allowed_graphs:
            allowed_graphs = [active_graph, *allowed_graphs]

        session_backend = FalkorDBBackend(
            graph_name=active_graph,
            allowed_graphs=allowed_graphs,
        )
        set_session_backend(session_backend)

    system_prompt = SYSTEM_PROMPT + _build_graph_context_prefix(
        active_graph, allowed_graphs
    )

    data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
    backend = FilesystemBackend(root_dir=str(data_dir), virtual_mode=True)
    agent = create_deep_agent(
        model=llm,
        tools=all_tools,
        system_prompt=system_prompt,
        backend=backend,
        middleware=[RepeatGuardMiddleware()],
    )

    if not _LOG_ATTACHMENTS:
        return agent

    # Wrap the deep agent in a parent graph whose first node logs the raw
    # wire format of the last HumanMessage. This lets us inspect how
    # `langgraph dev` delivers file uploads (e.g. image_url parts with
    # data: base64 URLs) without modifying the agent itself.
    parent = StateGraph(MessagesState)
    parent.add_node("log_attachments", _log_attachments)
    parent.add_node("agent", agent)
    parent.add_edge(START, "log_attachments")
    parent.add_edge("log_attachments", "agent")
    # Preserve the checkpointer/await already configured on the deep agent.
    checkpointer: Checkpointer | None = getattr(agent, "checkpointer", None)
    return parent.compile(checkpointer=checkpointer)


# Alias for LangGraph Studio / langgraph.json, which expects a graph factory.
build_graph = build_agent
