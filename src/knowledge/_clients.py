"""Shared OpenAI-compatible clients for chat completions and embeddings.

The harness targets Ollama via its OpenAI-compatible endpoints rather than
LiteLLM, avoiding the fragile content-block conversion that broke streaming
with reasoning content (see the ``langchain_litellm._convert_message_to_dict``
bare-string pass-through bug).

Two endpoints are used:

- **Chat completions** — the LLM chat endpoint (Ollama Cloud by default),
  selected by ``OLLAMA_API_BASE`` + ``OLLAMA_API_KEY``. Drives entity
  extraction, NL-to-Cypher, summarization, pairwise reconciliation, and the
  deep-agent reasoning model.
- **Embeddings** — the embedding endpoint (local Ollama by default), selected
  by ``EMBEDDING_API_BASE`` (default ``http://localhost:11434``) +
  ``EMBEDDING_API_KEY``. Independent from chat because Ollama Cloud does not
  expose ``/v1/embeddings``; a local Ollama instance serves embeddings.

Both clients are lazily constructed and cached for the process lifetime.
``reset_clients()`` drops the cache (used by tests).
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI

_DEFAULT_EMBEDDING_API_BASE = "http://localhost:11434"

_CHAT_CLIENT: AsyncOpenAI | None = None
_EMBEDDING_CLIENT: AsyncOpenAI | None = None


def _ensure_trailing_v1(base: str) -> str:
    """Append ``/v1`` to ``base`` unless it already ends with ``/v1``.

    The OpenAI SDK posts to ``{base_url}/chat/completions`` and
    ``{base_url}/embeddings``, so the base URL must include the ``/v1``
    segment that Ollama's OpenAI-compatible surface expects.
    """
    stripped = base.rstrip("/")
    if stripped.endswith("/v1"):
        return stripped
    return f"{stripped}/v1"


def chat_client() -> AsyncOpenAI:
    """Return the cached chat-completions client (Ollama Cloud by default).

    Reads ``OLLAMA_API_BASE`` (default ``https://ollama.com``) and
    ``OLLAMA_API_KEY``. The ``/v1`` segment is appended if missing.
    """
    global _CHAT_CLIENT
    if _CHAT_CLIENT is not None:
        return _CHAT_CLIENT
    base = os.getenv("OLLAMA_API_BASE", "https://ollama.com")
    key = os.getenv("OLLAMA_API_KEY", "ollama")
    _CHAT_CLIENT = AsyncOpenAI(base_url=_ensure_trailing_v1(base), api_key=key)
    return _CHAT_CLIENT


def embedding_client() -> AsyncOpenAI:
    """Return the cached embeddings client (local Ollama by default).

    Reads ``EMBEDDING_API_BASE`` (default ``http://localhost:11434``) and
    ``EMBEDDING_API_KEY`` (optional). The ``/v1`` segment is appended if
    missing. Independent from the chat client because the embedding endpoint
    may run on a different host (e.g. local Ollama) than the chat endpoint.
    """
    global _EMBEDDING_CLIENT
    if _EMBEDDING_CLIENT is not None:
        return _EMBEDDING_CLIENT
    base = os.getenv("EMBEDDING_API_BASE", _DEFAULT_EMBEDDING_API_BASE)
    key = os.getenv("EMBEDDING_API_KEY", "ollama")
    _EMBEDDING_CLIENT = AsyncOpenAI(base_url=_ensure_trailing_v1(base), api_key=key)
    return _EMBEDDING_CLIENT


def reset_clients() -> None:
    """Drop the cached clients so the next call rebuilds them.

    Used by tests to force a fresh client between cases.
    """
    global _CHAT_CLIENT, _EMBEDDING_CLIENT
    _CHAT_CLIENT = None
    _EMBEDDING_CLIENT = None


__all__ = ["chat_client", "embedding_client", "reset_clients"]