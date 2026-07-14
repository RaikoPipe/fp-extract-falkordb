"""LLM-based structured extraction: document chunks -> Pydantic models."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Type

import litellm
from loguru import logger
from pydantic import BaseModel

from knowledge.graph_models.factory_graph_model import FactoryPlanningGraph

_DEFAULT_LLM_MODEL = "ollama/qwen3.5:122b-a10b"

_SYSTEM_PROMPT = """\
You are a manufacturing domain expert. Extract all factory-planning entities \
from the given document text into the JSON schema provided.

Rules:
- Extract ONLY information explicitly stated in the text.
- Use consistent, exact entity names to enable deduplication.
- All time values in seconds, lengths in meters, weights in grams, speeds in m/s.
- Never extract personal names, contact information, or employee identifiers.
- If a field's value is not mentioned in the text, omit it (do not guess).
- Return valid JSON matching the schema. No markdown fences, no commentary.

Resource-specific rules:
- Every resource MUST have a semantically rich description that captures its \
function, location, role in the production flow, and distinguishing \
characteristics. Even for sparse mentions, synthesize a concise description \
from the available context. When re-encountering a known resource, extend \
the description with newly discovered context while preserving prior \
information.
- Set name_has_index to true when the name includes a clear index, ID, or \
code that distinguishes this resource (e.g. 'AKL-01', 'Workstation-3A', \
'AGV-02'). Set it to false when the name is a plain or generic word with no \
distinguishing index (e.g. 'Machine', 'Buffer', 'Conveyor').
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
    """
    model = llm_model or os.getenv("LLM_MODEL", _DEFAULT_LLM_MODEL)
    api_base = api_base or os.getenv("OLLAMA_BASE_URL")
    messages = build_extraction_prompt(chunk, schema_class)

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
    }
    if api_base:
        kwargs["api_base"] = api_base

    for attempt in range(max_retries):
        try:
            logger.debug(
                "LLM extraction request | model={} | messages={}",
                model,
                json.dumps(messages, indent=2, ensure_ascii=False),
            )
            t0 = time.time()
            response = await litellm.acompletion(**kwargs)
            raw = response.choices[0].message.content
            logger.debug(
                "LLM extraction response ({:.1f}s) | raw={}",
                time.time() - t0,
                raw,
            )

            # Strip markdown code fences if present
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = lines[1:]  # drop opening fence
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)

            try:
                return schema_class.model_validate_json(text)
            except Exception:
                logger.debug("JSON validation failed, attempting repair")
                from json_repair import repair_json

                repaired = repair_json(text)
                if isinstance(repaired, str):
                    return schema_class.model_validate_json(repaired)
                return schema_class.model_validate(repaired)

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
) -> list[tuple[BaseModel, str, int]]:
    """Extract from all chunks with bounded concurrency.

    Returns a list of ``(extraction, source, chunk_index)`` tuples — one per
    successfully parsed chunk — preserving the provenance needed by the
    conflict-detecting merge mode. ``source`` is the originating file name
    and ``chunk_index`` is the positional index within that file.
    """
    semaphore = asyncio.Semaphore(concurrency)
    results: list[tuple[BaseModel, str, int]] = []

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
        if result is not None:
            results.append(result)

    return results
