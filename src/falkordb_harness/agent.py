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
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Checkpointer

from falkordb_harness._loop_guard import RepeatGuardMiddleware
from falkordb_harness.tools import all_tools_for_role
from falkordb_harness.tools.job_tools import make_run_in_background

# Recursion limit: LangGraph's default (25) is too low for the tool-heavy
# PRE-INGESTION REVIEW ROUTINE; 100 + repeat-guard bounds runaway loops.
# Raised from 50 to 100 to accommodate long multi-step showcase pipelines
# (10+ tool-call-heavy steps) without exhausting the per-turn budget.
_DEFAULT_RECURSION_LIMIT = 100

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

# Toggle for the attachment wire-format logger (LOG_ATTACHMENTS env, default
# "1"). Dumps the raw last HumanMessage to inspect upload delivery.
_LOG_ATTACHMENTS = os.getenv("LOG_ATTACHMENTS", "1") not in ("", "0", "false", "no")

# Emit to stderr even when the host hasn't configured root logging.
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

# Module-level sandbox reference for lifecycle cleanup.  Set by build_agent
# when PYTHON_RUNNER_ENABLE is active; consumed by Chainlit's on_chat_end
# and the CLI's atexit handler.
_SANDBOX: object | None = None


def get_sandbox() -> object | None:
    """Return the current PythonRunnerSandbox, or None."""
    return _SANDBOX


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


def _log_attachments(state: dict) -> None:
    """Log how the last human message arrived.

    Inspects ``state["messages"]`` and emits the type and a compact
    representation of the content of the last ``HumanMessage``. This is purely
    diagnostic and does not modify state.

    Factored out of :class:`LogAttachmentsMiddleware` so the diagnostic logic
    stays readable and unit-testable without instantiating the middleware.
    """
    messages: list[AnyMessage] = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
    if not messages:
        logger.info("attachments: no messages in state")
        return

    last = messages[-1]
    if not isinstance(last, HumanMessage):
        logger.info("attachments: last message is %s (not HumanMessage)", type(last).__name__)
        return

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


class LogAttachmentsMiddleware(AgentMiddleware):
    """Diagnostic middleware that logs the raw wire format of the last
    ``HumanMessage`` before the agent execution starts.

    Replaces the previous pre-graph ``log_attachments`` wrapper node. Running
    as a ``before_agent`` middleware hook (instead of a parent ``StateGraph``
    wrapping the deep agent) keeps the agent at the run stream's root scope so
    ``TasksTransformer``-based consumers (the Chainlit ``on_message`` handler)
    see the inner ``tools`` / ``model`` task events at scope ``()``. The prior
    parent-graph wrapping placed those tasks at the ``('agent',)`` subgraph
    namespace, where the root-scoped ``TasksTransformer`` filtered them out —
    silently dropping every tool-call step and the Claude-style tool-call
    history.

    The middleware node is named ``log_attachments.before_agent`` inside the
    agent graph (LangChain's ``create_agent`` registers ``before_agent`` hooks
    as ``f"{m.name}.before_agent"`` nodes). It returns no state updates, so it
    emits no ``messages`` events on the v3 stream.
    """

    name = "log_attachments"

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        _log_attachments(state)  # type: ignore[arg-type]
        return None

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        _log_attachments(state)  # type: ignore[arg-type]
        return None

SYSTEM_PROMPT = """\
You are a knowledge-graph assistant for a factory-planning FalkorDB database.

SESSION FILE ISOLATION (mandatory):
Raw sources and preprocessed Markdown are stored on disk under per-session \
subdirectories named after the current Chainlit thread id:
  - ``originals/<session_id>/<name>``   — raw uploaded sources
  - ``preprocessed/<session_id>/<name>`` — docprep Markdown output
``<session_id>`` is YOUR current session id, given in the preamble below. \
Files under ``originals/<session_id>/`` and ``preprocessed/<session_id>/`` \
belong to THIS session and you may freely inspect, preprocess, and ingest them.
Rules:
- Only operate on files under YOUR session's subdirectory unless the user \
explicitly references a file from another session by name or path. Do NOT \
read, preprocess, or ingest files from another session's subdirectory on \
your own initiative, even if they look relevant.
- ``originals/_unscoped/`` and ``preprocessed/_unscoped/`` belong to no \
session (CLI / pre-session uploads). Touch them only when the user \
explicitly references them.
- Ingested files are graph-scoped (cross-session), tracked in the registry \
under a graph name rather than a thread. Query them via the graph tools \
(cypher_query, nl_query, search) — do \
NOT reach into another session's directory to inspect an already-ingested \
file. The registry's ``originalPath``/``preprocessedPath`` columns on \
ingested rows are provenance only and may point at a different session's \
directory; treat them as metadata, not as files to open.

PRE-INGESTION REVIEW ROUTINE (mandatory before extract_and_write):
This routine is a soft guardrail that prevents large amounts of data noise from \
entering the knowledge graph. Follow it every time the user asks to ingest from \
a directory or names files to ingest.
0. CREATE OR SELECT GRAPH (only if no graph is selected — see preamble): \
if the preamble says NO graph is active, your FIRST step before anything else \
is to determine whether the user wants to ingest into an EXISTING graph or a \
NEW one. Do this by calling ``list_graphs`` (and ``describe_graph()`` with no \
arguments to read every graph's description), then ask the user via ``ask_user`` \
whether they want to reuse an existing graph (name one) or create a new one. \
Base your suggestion on the user's intent and the existing descriptions \
(e.g. if a graph's description matches the data domain, suggest reusing it). \
Only after the user chooses:
- If they name an existing graph, call ``request_graph_switch(name)`` and wait \
for confirmation, then ``use_graph(name)``.
- If they want a new one, call ``create_graph(name, description)`` where \
``name`` is derived from the user's ingestion intent (a short, stable \
identifier for the domain) and ``description`` is a concise 1-3 sentence \
summary of the graph's intended scope. Do NOT ask for further confirmation \
for creation — decide the name/description from user sentiment.
Only after a graph is active do you proceed to the review routine. If a graph \
IS already active, skip this step — unless the user explicitly asks to create \
a new graph, in which case call ``create_graph(name, description)`` directly \
(no need to go through the switch flow, and no confirmation needed for \
creation).
1. DISCOVER: call ls (or glob) on ``originals/<session_id>/`` to list \
candidate files for THIS session. The filesystem root is DATA_DIR; both \
``originals/`` (raw uploaded sources) and ``preprocessed/`` (Markdown output) \
are visible under it, each containing one subdirectory per session. Only \
consider files under your own ``<session_id>`` subdirectory unless the user \
explicitly asks for files from another session.
2. METADATA: call file_metadata on each candidate file (or a representative \
sample if there are many) to get size, type, page/char/word counts. Pass \
paths as ``originals/<session_id>/<name>``.
3. EXCERPT: call read_excerpt on a FEW small slices per file — e.g. the first \
lines (text) or pages 1, 3, and the last page (PDF/DOCX) — enough to understand \
the content, not the whole file. Avoid dumping large bodies into context.
3b. PREPROCESS (when needed): if a file is a scanned PDF, image, Excel with \
charts, or any binary format where read_excerpt returned garbage, placeholders, \
or low text density, call preprocess_document(path) to convert it to Markdown \
in the ``preprocessed/<session_id>/`` tree. preprocess_document also accepts \
plain-text formats (``.txt``/``.md``/``.csv``/``.json``/``.html``/``.py``) — \
for those it performs a cheap verbatim copy to ``preprocessed/<session_id>/`` \
as ``<stem>.md`` (no VLM call), which marks the file Preprocessed ✓ in the \
document sidebar. After preprocessing, call read_excerpt on the \
``output_path`` the tool returned (e.g. \
``preprocessed/<session_id>/<stem>.md``) to verify the conversion before \
extraction.
4. SUMMARIZE: report back to the user, in plain prose, what each file contains:
   - file name, type, size, page/line count
   - a 1-3 sentence content description per file
   - anything that looks like noise, out-of-scope, or non-factory-planning data
   - which files were preprocessed (binary → docprep) and which were copied \
(plain text → verbatim copy to ``preprocessed/``)
4b. ESTIMATE: call ``estimate_ingestion_time`` with the SAME ``data_dir`` / \
``chunk_size`` / ``concurrency`` you intend to pass to ``extract_and_write``. \
Read ``estimated_human`` (e.g. ``"≈ 3m 20s"``) and ``chunk_count`` from the \
result and fold them into the confirmation summary in step 5 — e.g. include a \
line like "Estimated processing time: ≈ 3m 20s (42 chunks across 5 files at \
concurrency 4)". This keeps the user informed about the expected wait BEFORE \
they confirm. Do NOT call extract_and_write here; the estimate is read-only \
and performs no LLM extraction or graph writes.
5. CONFIRM: STOP and call ``request_ingestion_confirmation`` with your \
summary (including the time estimate from 4b — do NOT ask in prose — use the \
tool so the user gets explicit Confirm/Cancel buttons). Do NOT call \
extract_and_write until the user \
confirms via that tool. chunk_documents (preview-only, no graph writes) \
may be used during this review to preview chunks, but the actual ingestion \
must wait for confirmation.
6. PROCEED: only after explicit user confirmation, call extract_and_write. \
extract_and_write reads from the ``preprocessed/`` tree by default; only point \
it at ``originals/`` if the user explicitly wants to ingest raw text sources \
directly. When passing a data_dir to extract_and_write/chunk_documents, prefer \
your session's subdirectory (``preprocessed/<session_id>``) so you do not \
pick up another session's files.
6b. UPDATE DESCRIPTION: after every successful ingestion, call \
``update_graph_description(description)`` with a revised 1-3 sentence summary \
of the graph's contents (entities, source documents, scope) so the description \
stays accurate. The description is the first thing read when understanding \
the graph.
Err on the side of showing the user too much summary rather than too little.

TASK PLANNING WITH write_todos (mandatory for multi-step work):
- Use ``write_todos`` to create and maintain a structured task list for EVERY \
multi-step operation — the user sees your plan live in a pinned panel above the \
chat input. This is NOT optional for complex work; it is the primary way the \
user tracks your progress.
- You MUST use ``write_todos`` for these workflows (they are always multi-step):
  * The PRE-INGESTION REVIEW ROUTINE (steps 0-6b above) — break it into \
concrete todos: discover files, inspect metadata, read excerpts, preprocess \
binary files, summarize findings, estimate time, confirm, ingest, update \
description.
  * Any ingestion run (extract_and_write) — mark the ingestion step as \
in_progress before calling the tool, and completed after it returns.
  * Preprocessing one or more documents (preprocess_document) — one todo per \
file, marked in_progress/completed as each finishes.
  * Graph creation + first ingestion — plan the create→review→ingest→describe \
sequence.
  * Reconciliation walkthroughs (resolve_duplicate) — one todo per duplicate \
pair.
  * Any user request that spans 3+ distinct actions.
- Mark a todo as in_progress BEFORE beginning work on it. Mark it completed \
IMMEDIATELY after finishing. Never batch completions.
- When the ingestion progress panel appears (the "Progress" section in the \
pinned panel), your todo list and the progress section render together — the \
user sees both your plan and the live pipeline ETA simultaneously. Keep your \
todos in sync with the pipeline stages.
- For simple single-step queries (a quick Cypher lookup, a schema question, a \
count), skip write_todos — it adds overhead with no benefit.

BACKGROUND EXECUTION (keep the user unblocked):
While a tool runs inside your turn, the user cannot send messages. For calls \
with a long execution time, use ``run_in_background(tool_name, tool_args, \
label)`` instead of calling the tool directly. It returns a ``job_id`` at \
once; the job keeps running after your turn ends.
- When to background — YOU decide, based on:
  * a time estimate: for ``extract_and_write``, background it when \
``estimate_ingestion_time`` reports more than about a minute;
  * the tool's (or its skill's) description: any tool whose description says \
it has a long execution time (e.g. ``preprocess_document`` on scanned PDFs or \
office files, plugin tools documented as long-running in their SKILL.md) \
should be backgrounded unless the user explicitly wants to wait.
  Quick tools (queries, file inspection, schema) always run directly.
- ``tool_args`` are exactly the arguments you would pass in a direct call. \
Backgrounding does not skip any rule: the PRE-INGESTION REVIEW ROUTINE, \
including confirmation, must be complete BEFORE you start the job.
- After starting a job: tell the user what is running (label and job id) and \
that they can keep working, then END YOUR TURN. Do not call \
``get_job_status`` in a loop or otherwise wait for the job; mark the related \
todo as in_progress and leave it.
- When jobs finish, the next user message begins with a \
``<background_job_updates>`` block holding each job's status and result. Act \
on it first: report the outcome briefly, complete the related todos, and do \
any follow-up (e.g. step 6b ``update_graph_description`` after a successful \
background ingestion). The user has already seen a short completion notice in \
the chat.
- ``list_jobs`` / ``get_job_status(job_id)`` answer user questions about \
jobs; ``cancel_job(job_id)`` cancels one when the user asks.
- Interactive tools (``ask_user``, ``request_ingestion_confirmation``, \
``request_graph_switch``, ``use_graph``, ``write_todos``) cannot be \
backgrounded.

Guidelines:
- Before querying, call get_schema to understand available labels and relationships.
- Prefer nl_query for open-ended questions; use cypher_query when the user \
provides Cypher or when you can construct a precise query.
- Always report results clearly, including counts, conflicts detected, and \
reconciliation links.
- You may create a new knowledge graph (create_graph) at any time, regardless \
of whether a graph is currently active. No user confirmation is needed — \
derive the name and description from the user's intent.
- Reconciliation applies to Resources only and never auto-merges duplicates; \
always leave adjudication to the human via resolve_duplicate.
- When accepting a merge, warn the user that conflicting properties are stored \
in the surviving node's conflicts list and offer to resolve them.
- Present duplicates one at a time; do not batch-accept or batch-reject. \
Use resolve_duplicate(plain_name, action) where action is "accept" (merge \
nodes, transfer relationships, record property conflicts in n.conflicts), \
"reject" (dismiss the link), or "keep_separate" (same as reject but the user \
considers the entities distinct). Use get_reconciliations to list outstanding \
duplicates at any time.
- Merge conflicts are stored as a ``conflicts`` JSON list on nodes. Each entry \
has a stable ``id`` of the form ``<property>:<detected_at>``. Resolve a \
conflict by rewriting its JSON to set ``resolved: true`` and ``resolved_at``, \
then SET the full ``n.conflicts`` list back in one Cypher statement. See the \
cypher_query tool docstring for the exact schema.
- Never reset the graph without explicit user confirmation.
- To switch the active graph: (1) call list_graphs to see what exists, (2) \
call request_graph_switch(name) — this asks the user to confirm via a \
Confirm/Cancel prompt, (3) only AFTER the user confirms, call use_graph(name). \
Never call use_graph without a prior confirmed request_graph_switch for the \
same name; use_graph will refuse and return an error otherwise. You may read \
graph descriptions via describe_graph (with no name argument) to help the \
user choose.
- When you need to learn about the existing knowledge graphs (e.g. before \
switching, or to answer "what's in this graph?"), call ``describe_graph()`` \
with NO arguments FIRST — it returns every graph's description in one call. \
Only fall back to get_schema on the active graph if the \
description is empty or you need structural detail.
- You are restricted to the user's enabled knowledge graphs. \
use_graph(name) will reject any graph the user has not enabled. \
When asked "which knowledge graphs are available?", answer with the session's \
enabled set (from the preamble) and/or call list_graphs for the full instance \
listing. Do NOT claim no graphs exist just because the active graph is empty.
"""


def _build_graph_context_prefix(
    active_graph: str | None,
    allowed_graphs: list[str] | None,
    thread_id: str | None = None,
    graph_description: str | None = None,
) -> str:
    """Build the dynamic preamble appended to SYSTEM_PROMPT for graph selection.

    Tells the agent which graph is active and which graphs are in scope, so it
    can answer "which KGs are available?" without falling back to cypher_query
    on the bound graph. Also surfaces the current session (Chainlit thread)
    id so the agent knows which per-session on-disk subdirectory
    (``originals/<thread_id>/`` / ``preprocessed/<thread_id>/``) is its own —
    this drives the SESSION FILE ISOLATION rule in SYSTEM_PROMPT. Returns an
    empty string when no per-session selection is configured (the CLI /
    default path), preserving the original prompt.

    When ``active_graph`` is falsy (the no-graph sentinel state), the
    preamble explicitly says NO graph is active and directs the agent to
    create one before ingestion (see PRE-INGESTION REVIEW ROUTINE step 0).
    When ``graph_description`` is provided for an active graph, it is
    included so the agent sees the graph's description without a tool call.
    """
    if not active_graph and not allowed_graphs and not thread_id:
        return ""
    parts: list[str] = [
        "",
        "KNOWLEDGE GRAPH SELECTION (user-controlled for this session):",
    ]
    if active_graph:
        parts.append(f"- Active graph (all queries/ingestion target this): '{active_graph}'")
        if graph_description:
            parts.append(f"- Active graph description: {graph_description}")
    else:
        parts.append(
            "- NO knowledge graph is currently selected. You cannot query or "
            "ingest until a graph is active. If the user wants to ingest data, "
            "your FIRST step is to determine whether to reuse an EXISTING graph "
            "or create a NEW one: call list_graphs and describe_graph() to see "
            "what exists, then ask the user via ask_user which they want. See "
            "PRE-INGESTION REVIEW ROUTINE step 0 for the full procedure."
        )
    if allowed_graphs:
        parts.append(
            "- Enabled graphs (the only ones you may switch to via use_graph): "
            + ", ".join(f"'{g}'" for g in allowed_graphs)
        )
    else:
        parts.append("- Enabled graphs: unrestricted (any graph name is accepted)")
    if active_graph:
        parts.append(
            "- To switch the active graph: call request_graph_switch(name) to "
            "ask the user to confirm, then call use_graph(name) only after "
            "confirmation. Never call use_graph without a prior confirmed "
            "request_graph_switch for the same name."
        )
    if thread_id:
        parts.append(
            f"- Your current session id is '{thread_id}'. Only files under "
            f"originals/{thread_id}/ and preprocessed/{thread_id}/ belong to "
            f"this session; do not touch other sessions' files unless the "
            f"user explicitly references them."
        )
    else:
        parts.append(
            "- No session id is set (CLI / pre-session). Files under "
            "originals/_unscoped/ and preprocessed/_unscoped/ belong to no "
            "session; touch them only when the user explicitly references them."
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
    # Early-return for known provider prefixes; bare Ollama tags like
    # ``glm-5.2:cloud`` contain a colon but aren't provider-prefixed.
    if model_name.startswith("openai:") or model_name.startswith("anthropic:"):
        return model_name
    if model_name.startswith("anthropic/") or "claude" in model_name:
        model_id = model_name.removeprefix("anthropic/")
        return f"anthropic:{model_id}"
    if model_name.startswith("openai/") or "gpt" in model_name:
        model_id = model_name.removeprefix("openai/")
        return f"openai:{model_id}"
    # Bare Ollama tags route to ChatOpenAI pointed at the Ollama endpoint.
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

    # Per-session graph selection (Chainlit). CLI path leaves these unset
    # and falls back to the module-level env-driven backend cache.
    active_graph: str | None = configurable.get("active_graph")
    allowed_graphs_raw = configurable.get("allowed_graphs")
    allowed_graphs: list[str] | None = None
    thread_id: str | None = configurable.get("thread_id")
    if isinstance(allowed_graphs_raw, (list, tuple)):
        allowed_graphs = [str(g) for g in allowed_graphs_raw if g]

    if active_graph:
        from falkordb_harness.backend import set_session_backend
        from knowledge.falkordb_backend import FalkorDBBackend

        if allowed_graphs is None:
            allowed_graphs = [active_graph]
        elif active_graph not in allowed_graphs:
            allowed_graphs = [active_graph, *allowed_graphs]

        session_backend = FalkorDBBackend(
            graph_name=active_graph,
            allowed_graphs=allowed_graphs,
        )
        set_session_backend(session_backend)

    # Best-effort description fetch (sync reader; build_agent runs in the
    # Chainlit event loop and cannot await).
    graph_description: str | None = None
    if active_graph:
        try:
            from falkordb_harness.graph_descriptions import get_description_sync

            graph_description = get_description_sync(active_graph) or None
        except Exception:  # noqa: BLE001 — never block agent build on desc fetch
            graph_description = None

    system_prompt = SYSTEM_PROMPT + _build_graph_context_prefix(
        active_graph, allowed_graphs, thread_id, graph_description
    )

    # Role-based tool gating: reset_graph is admin-only. CLI (no role)
    # defaults to admin so local dev isn't hobbled.
    role = configurable.get("role") or "admin"
    tools = all_tools_for_role(role)

    data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()

    # PythonRunnerSandbox: opt-in Docker-backed code execution with pandas.
    # When PYTHON_RUNNER_ENABLE is set, the agent's filesystem tools (ls,
    # read_file, write_file, edit_file, glob, grep) operate inside a
    # per-thread python-runner container via BaseSandbox's execute-based
    # implementations.  The host DATA_DIR is bind-mounted read-only at
    # /workspace so the agent can inspect originals/ and preprocessed/.
    # Custom tools (file_metadata, extract_and_write, etc.) continue to use
    # the host-side fs_backend() in _paths.py independently.
    if os.getenv("PYTHON_RUNNER_ENABLE", "").lower() in ("1", "true", "yes"):
        from falkordb_harness.python_runner import PythonRunnerSandbox

        global _SANDBOX
        _SANDBOX = PythonRunnerSandbox(
            thread_id=thread_id or "_unscoped",
            data_dir=data_dir,
        )
        backend = _SANDBOX
    else:
        backend = FilesystemBackend(root_dir=str(data_dir), virtual_mode=True)

    # Build the middleware stack. Order: LogAttachmentsMiddleware first so
    # the diagnostic log fires before any other before_agent hook; then
    # RepeatGuardMiddleware (loop-breaker); then TodoListMiddleware
    # (deepagents v0.7+ no longer auto-adds it — see AGENTS.md).
    middleware: list[AgentMiddleware] = [RepeatGuardMiddleware(), TodoListMiddleware()]
    if _LOG_ATTACHMENTS:
        # Prepend so the attachment log fires before other before_agent hooks.
        middleware.insert(0, LogAttachmentsMiddleware())

    # run_in_background dispatches by name over the COMPILED agent's tool
    # table, which also holds the deepagents built-ins (execute, task, ...)
    # that only exist after create_deep_agent; filled in below.
    tool_table: dict = {}
    tools = [*tools, make_run_in_background(lambda: tool_table)]

    agent = create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        backend=backend,
        middleware=middleware,
    )
    tool_table.update(agent.nodes["tools"].bound.tools_by_name)
    return agent


# Alias for LangGraph Studio / langgraph.json.
build_graph = build_agent
