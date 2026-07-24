"""Tools for preprocessing raw source documents into Markdown via docprep.

``preprocess_document`` wraps the ``docprep`` submodule (a git submodule at
``src/document-to-markdown``). It converts a single raw source file — scanned
PDF, image, Excel chart, office format — to Markdown using the docprep
pipeline (Docling + EasyOCR + optional VLM fallback), and writes the result
into ``PREPROCESSED_DIR`` so the ingest tools (``chunk_documents`` /
``extract_and_write``, which default to ``PREPROCESSED_DIR``) pick it up.

The tool resolves input paths through the shared ``FilesystemBackend`` rooted
at ``DATA_DIR`` (see :mod:`falkordb_harness.tools._paths`), so both the
``originals/`` raw-source tree (where Chainlit uploads land and where
``file_metadata`` / ``read_excerpt`` inspect) and the ``preprocessed/``
output tree are visible under one root. The returned ``output_path`` is a
root-relative virtual path (e.g. ``preprocessed/foo.md``) so the agent can
pass it straight back to ``read_excerpt`` / ``file_metadata`` to verify the
conversion. This keeps raw sources separate from preprocessed Markdown, so
ingestion never double-counts a document by reading both the original and
its Markdown twin.
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.tools import tool

from falkordb_harness.tools._paths import (
    fs_backend as _fs_backend,  # noqa: F401  (imported to clear lru_cache in tests)
)
from falkordb_harness.tools._paths import preprocessed_dir, virtual_path
from falkordb_harness.tools._paths import resolve as _resolve
from falkordb_harness.tools._retry import with_retry


@tool
def preprocess_document(
    path: str,
    yaml_path: str = "",
    overwrite: bool = False,
) -> str:
    """Convert a single source document to Markdown via the docprep pipeline.

    Reads the input from the ``originals/`` tree (under DATA_DIR) and writes
    the result as ``<stem>.md`` into the ``preprocessed/`` tree, where
    ``chunk_documents`` / ``extract_and_write`` pick it up by default. Use
    this for scanned PDFs, images, Excel charts, and any office/binary format
    that needs OCR or VLM interpretation before extraction. Plain
    ``.txt``/``.md`` sources are already LLM-ready — do NOT preprocess them
    (it wastes a VLM call).

    Args:
        path: File path under DATA_DIR (relative or virtual absolute), e.g.
            ``originals/scan.pdf``. Path traversal (``..``) is rejected.
        yaml_path: Optional path to a docprep YAML config. When empty, falls
            back to ``./docprep.yaml`` if present, else docprep defaults.
            Fallback provider/model/base_url are read from ``DOCPREP_*`` env
            vars regardless (see .env.example).
        overwrite: If False (default) and the target ``.md`` already exists,
            the call is a no-op and reports ``already_exists``. Set True to
            re-run conversion and replace the existing ``.md``.

    Returns:
        JSON with ``output_path`` (a DATA_DIR-relative virtual path such as
        ``preprocessed/foo.md`` — pass it back to ``read_excerpt`` /
        ``file_metadata`` to verify the conversion), ``source``, ``pipeline_used``,
        ``format_detected``, ``page_count``, ``escalated``, ``warnings``,
        ``markdown_char_count``, and ``processing_time_seconds``. On error
        (missing file, unsupported format, VLM failure) returns
        ``{"error": ...}`` so the agent can recover rather than abort.
    """
    return with_retry(lambda: _preprocess_document_impl(path, yaml_path, overwrite))


class _ConfigNotFoundError(RuntimeError):
    """Raised when no docprep YAML config can be located.

    Refusing to fall back to ``PipelineConfig()`` defaults prevents the silent
    base64-embedding blowup that occurs when ``embed_images`` defaults to True
    and no VLM fallback endpoint is configured (the incident where a 6.9 MB
    PPTX became a 33.7 MB Markdown file of inline base64 images).
    """


def _build_config(yaml_path: str):
    """Build a ``PipelineConfig`` from a YAML config + env (docprep convention).

    Resolution order:
      1. ``yaml_path`` argument (if non-empty) — must exist, else error.
      2. ``./docprep.yaml`` next to the working directory — must exist, else error.

    Unlike the previous silent-fallback behaviour, this raises
    :class:`_ConfigNotFoundError` when no YAML can be found, so a misconfigured
    container (e.g. missing ``docprep.yaml`` in the image) fails loudly instead
    of emitting base64 images under the default ``embed_images=True``.
    """
    from docprep.config import PipelineConfig

    yp: Path | None = None
    if yaml_path:
        yp = Path(yaml_path)
        if not yp.exists():
            raise _ConfigNotFoundError(
                f"docprep config not found at explicit yaml_path={yaml_path!r}. "
                f"Pass a valid path or mount docprep.yaml at the working directory."
            )
    if yp is None:
        # Fall back to a ./docprep.yaml sitting next to the working dir.
        default_yaml = Path("docprep.yaml")
        if default_yaml.exists():
            yp = default_yaml
    if yp is None:
        raise _ConfigNotFoundError(
            "docprep config not found: no yaml_path argument was given and "
            "'./docprep.yaml' is absent from the working directory. Refusing to "
            "fall back to PipelineConfig() defaults (embed_images=True, no VLM "
            "fallback), which would emit base64 images inline instead of VLM "
            "text descriptions. Mount or COPY docprep.yaml into the container."
        )
    return PipelineConfig.from_sources(yaml_path=yp)


def _preprocess_document_impl(path: str, yaml_path: str, overwrite: bool) -> str:
    resolved = _resolve(path)
    if isinstance(resolved, str):
        return json.dumps({"error": resolved}, ensure_ascii=False)

    if not resolved.exists() or not resolved.is_file():
        return json.dumps({"error": f"File not found: {path}"}, ensure_ascii=False)

    out_dir = preprocessed_dir()
    out_path = out_dir / (resolved.stem + ".md")
    # Root-relative virtual path so the agent can feed output_path straight
    # back into read_excerpt / file_metadata (which resolve under DATA_DIR).
    out_virtual = virtual_path(out_path)

    if out_path.exists() and not overwrite:
        return json.dumps(
            {
                "already_exists": True,
                "output_path": out_virtual,
                "source": virtual_path(resolved),
                "markdown_char_count": out_path.stat().st_size,
            },
            ensure_ascii=False,
        )

    try:
        from docprep.entrypoint import convert
    except ImportError as exc:
        return json.dumps(
            {
                "error": (
                    "docprep is not installed. The harness depends on "
                    "'docprep[ollama] @ file:src/document-to-markdown' — "
                    f"re-run `pip install -e .`. Underlying error: {exc}"
                )
            },
            ensure_ascii=False,
        )

    try:
        config = _build_config(yaml_path)
    except Exception as exc:  # noqa: BLE001 — surface config errors to the agent
        return json.dumps(
            {"error": f"Failed to load docprep config: {exc}"},
            ensure_ascii=False,
        )

    try:
        result = convert(resolved, config)
    except Exception as exc:  # noqa: BLE001 — includes UnsupportedFormatError
        return json.dumps(
            {"error": f"docprep conversion failed: {exc}"}, ensure_ascii=False
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.markdown, encoding="utf-8")

    return json.dumps(
        {
            "already_exists": False,
            "output_path": out_virtual,
            "source": virtual_path(resolved),
            "pipeline_used": result.pipeline_used,
            "format_detected": result.format_detected,
            "page_count": result.page_count,
            "escalated": result.escalated,
            "warnings": result.warnings,
            "markdown_char_count": len(result.markdown),
            "processing_time_seconds": round(result.processing_time_seconds, 2),
        },
        indent=2,
        ensure_ascii=False,
    )
