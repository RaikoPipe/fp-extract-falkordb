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

from falkordb_harness.tools import all_tools

logger = logging.getLogger("falkordb_harness.attachments")

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
- Inspect files before ingestion (file_metadata, read_excerpt)
- Ingest documents into the graph (chunk_documents, extract_and_write)
- Query the graph with raw Cypher (cypher_query) or natural language (nl_query)
- Search by full-text (fulltext_search) or vector similarity (vector_search)
- Inspect the graph schema, nodes, edges, and node count \
(get_schema, list_nodes, list_edges, node_count)
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
1. DISCOVER: call ls (or glob) on the target directory to list candidate files.
2. METADATA: call file_metadata on each candidate file (or a representative \
sample if there are many) to get size, type, page/char/word counts.
3. EXCERPT: call read_excerpt on a FEW small slices per file — e.g. the first \
lines (text) or pages 1, 3, and the last page (PDF/DOCX) — enough to understand \
the content, not the whole file. Avoid dumping large bodies into context.
4. SUMMARIZE: report back to the user, in plain prose, what each file contains:
   - file name, type, size, page/line count
   - a 1-3 sentence content description per file
   - anything that looks like noise, out-of-scope, or non-factory-planning data
5. CONFIRM: STOP and ask the user to confirm before calling extract_and_write. \
Do NOT call extract_and_write until the user explicitly confirms. \
chunk_documents (preview-only, no graph writes) may be used during this review \
to preview chunks, but the actual ingestion must wait for confirmation.
6. PROCEED: only after explicit user confirmation, call extract_and_write.
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
"""


def _normalize_model_id(model_name: str) -> str:
    """Translate the AGENT_LLM_MODEL convention to init_chat_model's.

    ``init_chat_model`` expects ``"<provider>:<model>"`` strings. The harness
    historically used litellm-style ``"<provider>/<model>"`` (e.g.
    ``anthropic/claude-sonnet-4-20250514``, ``openai/gpt-4o``,
    ``ollama/llama3.1``). We accept both forms and normalise the slash to a
    colon for ``init_chat_model``; plain ``claude``/``gpt`` ids are routed to
    their default providers.

    LiteLLM-style ids without a provider prefix (e.g. ``ollama/llama3.1``)
    are routed through the ``litellm`` provider, which requires the
    ``langchain-litellm`` package.
    """
    if ":" in model_name and "/" not in model_name.split(":", 1)[0]:
        return model_name  # already in init_chat_model form
    if model_name.startswith("anthropic/") or "claude" in model_name:
        model_id = model_name.removeprefix("anthropic/")
        return f"anthropic:{model_id}"
    if model_name.startswith("openai/") or "gpt" in model_name:
        model_id = model_name.removeprefix("openai/")
        return f"openai:{model_id}"
    # ollama/..., together/..., groq/... etc. -> litellm provider
    return f"litellm:{model_name}"


def _provider_for(model_id: str) -> str:
    """Return the LangChain provider key for an ``init_chat_model`` id.

    Used to look up the credentials a given provider requires so we can fail
    fast with a clear message instead of letting the underlying SDK raise an
    opaque ``TypeError`` deep in the call stack.
    """
    # ``init_chat_model`` form is "<provider>:<model>"; litellm composite ids
    # like "litellm:ollama/..." keep the litellm prefix.
    return model_id.split(":", 1)[0].strip().lower()


# Minimal credential requirements per LangChain provider. Only providers the
# harness realistically routes to are listed; unknown providers are left to
# their own initialisation to surface whatever error they raise.
_PROVIDER_CREDENTIAL_ENVVARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "litellm": (),  # credentials come from per-backend envvars (e.g. OLLAMA_API_KEY)
}


def _missing_credentials(provider: str) -> list[str]:
    """Return the list of required credential envvars that are unset."""
    required = _PROVIDER_CREDENTIAL_ENVVARS.get(provider, ())
    return [name for name in required if not os.getenv(name)]


def resolve_model(
    model_name: str | None = None,
    temperature: float = 0.0,
) -> BaseChatModel:
    """Return a configured chat model based on the AGENT_LLM_MODEL convention.

    Routes ``anthropic/...`` / ``claude`` ids to ChatAnthropic, ``openai/...``
    / ``gpt`` ids to ChatOpenAI, and everything else (e.g. ``ollama/...``)
    through LiteLLM via the ``langchain-litellm`` package.

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
    missing = _missing_credentials(provider)
    if missing:
        raise RuntimeError(
            f"Agent LLM provider '{provider}' (model '{model_id}') is missing "
            f"required credentials: {', '.join(missing)}. Set them in your "
            f".env (e.g. ANTHROPIC_API_KEY, OPENAI_API_KEY) or point "
            f"AGENT_LLM_MODEL at a provider whose credentials are already "
            f"configured (e.g. ollama/...)."
        )
    return init_chat_model(
        model_id,
        temperature=temperature,
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
    ``DATA_DIR`` contents (with path-traversal containment). This is required
    for the PRE-INGESTION REVIEW ROUTINE in the system prompt to inspect files
    before ``extract_and_write`` ingests them.

    Model selection falls back through (in order):
    1. ``config["configurable"]["model_name"]`` / ``["temperature"]``
       (per-request overrides, used by LangGraph Studio and the CLI).
    2. ``AGENT_LLM_MODEL`` / ``AGENT_LLM_TEMPERATURE`` environment variables.
    3. ``anthropic/claude-sonnet-4-20250514`` / ``0.0`` defaults.

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

    data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
    backend = FilesystemBackend(root_dir=str(data_dir), virtual_mode=True)
    agent = create_deep_agent(
        model=llm,
        tools=all_tools,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
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