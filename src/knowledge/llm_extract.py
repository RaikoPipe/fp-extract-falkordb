"""LLM-based structured extraction: document chunks -> Pydantic models."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Type

from loguru import logger
from pydantic import BaseModel

from knowledge._clients import chat_client
from knowledge._textutils import strip_code_fence
from knowledge.graph_models.factory_graph_model import FactoryPlanningGraph

_DEFAULT_LLM_MODEL = "qwen3.5:122b-a10b"


def _validate_and_log(text_or_obj: str | object, schema_class: Type[BaseModel]) -> BaseModel:
    """Validate JSON text or a Python object and log any ambiguous durations.

    Dispatches on type: strings go through ``model_validate_json``, other
    objects through ``model_validate``.
    """
    if isinstance(text_or_obj, str):
        result = schema_class.model_validate_json(text_or_obj)
    else:
        result = schema_class.model_validate(text_or_obj)
    _log_ambiguous(result)
    return result


def _log_ambiguous(result: BaseModel) -> None:
    """Log a warning for each ambiguous duration recorded on the extraction."""
    ambiguous = getattr(result, "ambiguous_durations", None) or []
    for entry in ambiguous:
        logger.warning(
            "Ambiguous duration: entity={} field={} raw={!r}",
            f"{getattr(entry, 'entity_type', '?')}:"
            f"{getattr(entry, 'entity_name', '?')}",
            getattr(entry, "field_name", "?"),
            getattr(entry, "raw_value", ""),
        )

_SYSTEM_PROMPT = """\
You are a manufacturing domain expert. Extract all factory-planning entities \
from the given document text into the JSON schema provided.

Rules:
- Extract ONLY information explicitly stated in the text.
- Use consistent, exact entity names to enable deduplication.
- Never extract personal names, contact information, or employee identifiers.
- If a field's value is not mentioned in the text, omit it (do not guess).
- Return valid JSON matching the schema. No markdown fences, no commentary.

The schema's field descriptions carry the domain rules (duration grammar, \
units, Resource/Zone boundary, name_has_index). Follow them precisely.
"""


def build_extraction_prompt(
    chunk: str,
    schema_class: Type[BaseModel] = FactoryPlanningGraph,
) -> list[dict[str, str]]:
    """Build the message list for structured extraction."""
    schema_json = json.dumps(
        schema_class.model_json_schema(), indent=2, ensure_ascii=False
    )

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"## JSON Schema\n\n```json\n{schema_json}\n```\n\n"
                f"## Document Text\n\n{chunk}\n\n"
                "Extract all factory-planning entities from the text above. "
                "Return a single JSON object matching the schema."
            ),
        },
    ]


async def extract_from_chunk(
    chunk: str,
    schema_class: Type[BaseModel] = FactoryPlanningGraph,
    llm_model: str | None = None,
    api_base: str | None = None,
    max_retries: int = 3,
) -> BaseModel | None:
    """Extract entities from one chunk via LLM.

    Returns a validated Pydantic model instance, or None on failure.

    ``api_base`` is accepted for backward-compatibility but ignored — the
    chat client (:func:`knowledge._clients.chat_client`) carries the base
    URL and API key derived from ``OLLAMA_API_BASE`` / ``OLLAMA_API_KEY``.
    """
    model = llm_model or os.getenv("LLM_MODEL", _DEFAULT_LLM_MODEL)
    messages = build_extraction_prompt(chunk, schema_class)

    for attempt in range(max_retries):
        try:
            logger.debug(
                "LLM extraction request | model={} | messages={}",
                model,
                json.dumps(messages, indent=2, ensure_ascii=False),
            )
            t0 = time.time()
            response = await chat_client().chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                temperature=0.0,
            )
            raw = response.choices[0].message.content
            logger.debug(
                "LLM extraction response ({:.1f}s) | raw={}",
                time.time() - t0,
                raw,
            )

            # Strip markdown code fences if present, then validate.
            text = strip_code_fence(raw)
            try:
                return _validate_and_log(text, schema_class)
            except Exception:
                logger.debug("JSON validation failed, attempting repair")
                from json_repair import repair_json

                repaired = repair_json(text)
                return _validate_and_log(repaired, schema_class)

        except Exception as exc:
            if attempt < max_retries - 1:
                logger.debug(
                    "Extraction attempt {}/{} failed: {}, retrying...",
                    attempt + 1,
                    max_retries,
                    exc,
                )
                await asyncio.sleep(2 ** attempt)
                continue
            logger.warning(
                "Extraction failed after {} attempts: {}", max_retries, exc
            )
            return None

    return None


async def extract_from_chunks(
    chunks: list[dict],
    schema_class: Type[BaseModel] = FactoryPlanningGraph,
    llm_model: str | None = None,
    api_base: str | None = None,
    concurrency: int = 4,
    *,
    on_progress=None,
) -> list[tuple[BaseModel, str, int]]:
    """Extract from all chunks with bounded concurrency.

    Returns a list of ``(extraction, source, chunk_index)`` tuples — one per
    successfully parsed chunk — preserving the provenance needed by the
    conflict-detecting merge mode. ``source`` is the originating file name
    and ``chunk_index`` is the positional index within that file.

    Args:
        on_progress: Optional ``async (completed: int, total: int) -> None``
            callback invoked once per chunk as it completes (success or
            failure). The Chainlit UI uses this to drive a tqdm-style
            progress line with ETA. The callback is awaited from the
            ``as_completed`` consumer loop, so it must not block. ``None``
            (the default) keeps the previous behaviour exactly.
    """
    semaphore = asyncio.Semaphore(concurrency)
    results: list[tuple[BaseModel, str, int]] = []
    total = len(chunks)
    completed = 0

    async def _extract_one(chunk_info: dict) -> tuple[BaseModel, str, int] | None:
        async with semaphore:
            source = chunk_info["source"]
            idx = chunk_info["chunk_index"]
            logger.debug("Extracting {} chunk {}...", source, idx)
            result = await extract_from_chunk(
                chunk_info["text"],
                schema_class=schema_class,
                llm_model=llm_model,
                api_base=api_base,
            )
            if result:
                logger.debug("Done {} chunk {}", source, idx)
                return (result, source, idx)
            return None

    tasks = [_extract_one(c) for c in chunks]
    for coro in asyncio.as_completed(tasks):
        result = await coro
        completed += 1
        if on_progress is not None:
            try:
                await on_progress(completed, total)
            except Exception as exc:  # noqa: BLE001 — never break extraction
                logger.debug("extract on_progress callback raised: {}", exc)
        if result is not None:
            results.append(result)

    return results
