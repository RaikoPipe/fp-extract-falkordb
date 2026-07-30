"""Builders for Chainlit visual elements (Dataframe, Plotly, source files).

All builders are fail-safe: they return ``None`` when the optional
dependency (``pandas`` / ``plotly``) is missing or the tool output does not
contain the expected shape, so the caller can simply ``if el: await ...``
and fall back to the Markdown-in-Step rendering that
:mod:`chainlit_formatting` already provides.

The module is import-safe even when ``chainlit`` itself is not installed
(it only imports ``chainlit`` lazily inside each builder), so it can be
collected by tools that never touch the UI.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from falkordb_harness.i18n import t

logger = logging.getLogger("falkordb_harness.chainlit_elements")

# Row cap for rendered Dataframes. Larger result sets stay in the Step's
# Markdown table (which already caps at _MAX_ROWS); the Dataframe is a
# browse aid, not a full export.
_MAX_DF_ROWS = 200

# Tools whose JSON output is a list of record dicts (rows) suitable for a
# Dataframe. ``cypher_query`` is included because FalkorDB returns rows that
# are already dicts (see _cypher_query_impl str()-coerces — but records from
# list_nodes/list_edges/search are dicts).
_DF_TOOLS: frozenset[str] = frozenset(
    {
        "list_nodes",
        "list_edges",
        "fulltext_search",
        "vector_search",
    }
)

# Image extensions whose originals can be shown inline via cl.Image.
_IMAGE_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
)


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _flatten_record(rec: dict) -> dict:
    """Flatten nested dict/list values into scalar columns.

    Nested dicts/lists are JSON-encoded into a string so the DataFrame
    stays tabular. The top-level ``_labels`` list (from get_all_nodes)
    becomes a comma-joined string.
    """
    flat: dict[str, Any] = {}
    for k, v in rec.items():
        if isinstance(v, (dict, list)):
            if k == "_labels" and isinstance(v, list):
                flat[k] = ", ".join(str(x) for x in v)
            else:
                flat[k] = json.dumps(v, ensure_ascii=False, default=str)
        else:
            flat[k] = v
    return flat


def build_result_dataframe(tool_name: str, raw_output: Any):
    """Return a ``cl.Dataframe`` for tabular tool results, or ``None``.

    Handles ``list_nodes`` / ``list_edges`` / ``fulltext_search`` /
    ``vector_search`` (and best-effort ``cypher_query`` when rows are dicts).
    Returns ``None`` if pandas is unavailable or the output isn't a list
    of record dicts, so the caller falls back to the Markdown table.
    """
    if tool_name not in _DF_TOOLS and tool_name != "cypher_query":
        return None
    try:
        import pandas as pd
    except ImportError:
        return None

    text = raw_output if isinstance(raw_output, str) else str(raw_output)
    data = _try_parse_json(text)
    if not isinstance(data, list) or not data:
        return None
    # Only build a DataFrame when at least one row is a dict; cypher_query
    # returns a list of str() coercions (not dicts), which don't tabulate.
    records = [r for r in data if isinstance(r, dict)]
    if not records:
        return None

    flat = [_flatten_record(r) for r in records[:_MAX_DF_ROWS]]
    try:
        df = pd.DataFrame(flat)
    except Exception as exc:  # noqa: BLE001 — keep UI alive on odd shapes
        logger.debug("Dataframe build failed for %s: %s", tool_name, exc)
        return None

    try:
        import chainlit as cl
    except ImportError:
        return None
    return cl.Dataframe(
        name=f"{tool_name}_results",
        data=df,
        display="inline",
    )


def _label_counts_from_nodes(nodes: list[dict]) -> dict[str, int]:
    """Return ``{label: count}`` from get_all_nodes() output."""
    counts: dict[str, int] = {}
    for n in nodes:
        labels = n.get("_labels") or n.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        for lbl in labels or ["(unlabeled)"]:
            counts[str(lbl)] = counts.get(str(lbl), 0) + 1
    return counts


def _rel_type_counts_from_edges(edges: list) -> dict[str, int]:
    """Return ``{rel_type: count}`` from get_all_edges() tuples/lists."""
    counts: dict[str, int] = {}
    for e in edges:
        # get_all_edges returns tuples (src, tgt, rel, props); list_edges
        # tool wraps them into dicts with a "type" key.
        if isinstance(e, dict):
            rel = str(e.get("type", ""))
        elif len(e) >= 3:
            rel = str(e[2])
        else:
            continue
        counts[rel] = counts.get(rel, 0) + 1
    return counts


def build_label_distribution_plot(raw_output: str):
    """Bar chart of node counts by label (from ``list_nodes`` output)."""
    return _build_count_plot(
        raw_output,
        _label_counts_from_nodes,
        title=t("chart.nodes_by_label.title"),
        x_title=t("chart.nodes_by_label.x"),
        y_title=t("chart.nodes_by_label.y"),
        name="node_label_distribution",
    )


def build_rel_distribution_plot(raw_output: str):
    """Bar chart of relationship counts by type (from ``list_edges`` output)."""
    return _build_count_plot(
        raw_output,
        _rel_type_counts_from_edges,
        title=t("chart.rel_by_type.title"),
        x_title=t("chart.rel_by_type.x"),
        y_title=t("chart.rel_by_type.y"),
        name="rel_type_distribution",
    )


def _build_count_plot(raw_output: str, extractor, *, title, x_title, y_title, name):
    """Build a Plotly bar chart from a counts dict extracted via ``extractor``."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        return None
    data = _try_parse_json(raw_output if isinstance(raw_output, str) else str(raw_output))
    if not isinstance(data, list) or not data:
        return None
    counts = extractor(data)
    if not counts:
        return None
    labels = list(counts.keys())
    values = [counts[k] for k in labels]
    fig = go.Figure(
        data=[go.Bar(x=labels, y=values, marker_color="#6366f1")],
        layout={
            "title": title,
            "xaxis": {"title": x_title, "tickangle": -30},
            "yaxis": {"title": y_title, "dtick": 1},
            "margin": {"l": 40, "r": 20, "t": 40, "b": 60},
            "height": 320,
        },
    )
    try:
        import chainlit as cl
    except ImportError:
        return None
    return cl.Plotly(name=name, figure=fig, display="inline", size="medium")


def build_search_score_plot(raw_output: str, *, metric: str = "score"):
    """Bar chart of search-relevance scores per result (fulltext/vector)."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        return None
    data = _try_parse_json(raw_output if isinstance(raw_output, str) else str(raw_output))
    if not isinstance(data, list) or not data:
        return None
    names: list[str] = []
    scores: list[float] = []
    for i, item in enumerate(data[:_MAX_DF_ROWS]):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("id") or item.get("node_id") or f"#{i + 1}"
        score = item.get(metric)
        if score is None:
            continue
        try:
            scores.append(float(score))
            names.append(str(name))
        except (TypeError, ValueError):
            continue
    if not names:
        return None
    fig = go.Figure(
        data=[go.Bar(x=names, y=scores, marker_color="#10b981")],
        layout={
            "title": t("chart.search_scores.title"),
            "xaxis": {"title": t("chart.search_scores.x"), "tickangle": -30},
            "yaxis": {"title": t("chart.search_scores.y")},
            "margin": {"l": 40, "r": 20, "t": 40, "b": 60},
            "height": 320,
        },
    )
    try:
        import chainlit as cl
    except ImportError:
        return None
    return cl.Plotly(
        name=f"search_{metric}_plot", figure=fig, display="inline", size="medium"
    )


def build_ingestion_summary_plot(summary: dict):
    """Bar chart of an ingestion run's stage counts (files/chunks/extractions)."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        return None
    stages = [
        (t("chart.ingestion_summary.stage.files_staged"), summary.get("files_staged", 0)),
        (t("chart.ingestion_summary.stage.preprocessed"), summary.get("files_preprocessed", 0)),
        (t("chart.ingestion_summary.stage.chunks"), summary.get("chunks_processed", 0)),
        (t("chart.ingestion_summary.stage.extractions"), summary.get("extractions", 0)),
        (t("chart.ingestion_summary.stage.cypher"), summary.get("cypher_statements", 0)),
        (t("chart.ingestion_summary.stage.conflicts"), summary.get("conflicts_detected", 0)),
    ]
    labels = [s[0] for s in stages]
    values = [s[1] for s in stages]
    if not any(values):
        return None
    fig = go.Figure(
        data=[go.Bar(x=labels, y=values, marker_color="#f59e0b")],
        layout={
            "title": t("chart.ingestion_summary.title"),
            "xaxis": {"title": t("chart.ingestion_summary.x")},
            "yaxis": {"title": t("chart.ingestion_summary.y"), "dtick": 1},
            "margin": {"l": 40, "r": 20, "t": 40, "b": 40},
            "height": 300,
        },
    )
    try:
        import chainlit as cl
    except ImportError:
        return None
    return cl.Plotly(
        name="ingestion_summary", figure=fig, display="inline", size="medium"
    )


def build_schema_card_props(raw_output: str) -> dict | None:
    """Shape ``get_schema`` JSON output into props for the SchemaBrowser card.

    Returns ``{"labels", "relationship_types", "property_keys"}`` lists, or
    ``None`` if the output isn't a schema dict. Counts are not available from
    ``get_schema_info`` (which only lists names) so they are omitted.
    """
    data = _try_parse_json(raw_output if isinstance(raw_output, str) else str(raw_output))
    if not isinstance(data, dict):
        return None
    labels = data.get("labels") or data.get("node_labels") or []
    rels = data.get("relationship_types") or data.get("relationships") or []
    props = data.get("property_keys") or data.get("properties") or []
    return {
        "labels": [{"name": str(l)} for l in labels],
        "relationship_types": [{"name": str(r)} for r in rels],
        "property_keys": [str(p) for p in props],
    }


def build_source_elements(raw_output: str, data_dir: Path):
    """Return Pdf/Image/Text elements for ``preprocess_document`` / ``read_excerpt``.

    When the inspected file is a PDF or image, returns a ``cl.Pdf`` /
    ``cl.Image`` element pointing at the original, plus a ``cl.Text``
    element for the preprocessed Markdown when available. All paths are
    resolved under ``data_dir`` (the filesystem root) to stay contained.
    Returns ``[]`` when no relevant file can be located (the caller
    silently skips element attachment).
    """
    try:
        import chainlit as cl
    except ImportError:
        return []

    data = _try_parse_json(raw_output if isinstance(raw_output, str) else str(raw_output))
    # preprocess_document returns a dict with source/output_path virtual paths.
    source_virtual: str | None = None
    output_virtual: str | None = None
    if isinstance(data, dict):
        source_virtual = data.get("source") or data.get("path")
        output_virtual = data.get("output_path")
    else:
        return []

    if not source_virtual:
        return []

    elements: list = []
    src_abs = _safe_resolve(data_dir, source_virtual)
    if src_abs and src_abs.exists():
        ext = src_abs.suffix.lower()
        if ext == ".pdf":
            elements.append(
                cl.Pdf(name=src_abs.name, path=str(src_abs), display="side")
            )
        elif ext in _IMAGE_EXTS:
            elements.append(
                cl.Image(name=src_abs.name, path=str(src_abs), display="side")
            )

    if output_virtual:
        out_abs = _safe_resolve(data_dir, output_virtual)
        if out_abs and out_abs.exists():
            try:
                content = out_abs.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            if content:
                elements.append(
                    cl.Text(
                        name=t("element.preprocessed.name", name=out_abs.name),
                        content=content[:20000],
                        display="inline",
                        language="markdown",
                    )
                )
    return elements


def build_source_elements_from_row(row: dict, data_dir: Path):
    """Return Pdf/Image/Text elements for a registry document row.

    Sibling of :func:`build_source_elements` for the "Open" sidebar button.
    ``row`` is a :mod:`document_registry` row dict (absolute on-disk paths in
    ``preprocessedPath`` / ``originalPath``). Prefers the preprocessed
    Markdown (renders as a ``cl.Text``); falls back to the original (``cl.Pdf``
    for PDFs, ``cl.Image`` for images, ``cl.Text`` for plain text). The path
    must resolve under ``data_dir`` (containment guard via :func:`_safe_resolve`
    on the root-relative form of the path) — paths outside the data dir are
    skipped. Returns ``[]`` when ``chainlit`` is missing or no usable file is
    found (the caller sends a chat message instead).
    """
    try:
        import chainlit as cl
    except ImportError:
        return []

    elements: list = []
    pre_path = row.get("preprocessedPath")
    src_path = row.get("originalPath")

    # Prefer the preprocessed Markdown when it exists on disk.
    if pre_path:
        pre_abs = _abs_under_data_dir(pre_path, data_dir)
        if pre_abs and pre_abs.exists():
            try:
                content = pre_abs.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            if content:
                elements.append(
                    cl.Text(
                        name=t("element.preprocessed.name", name=pre_abs.name),
                        content=content[:20000],
                        display="inline",
                        language="markdown",
                    )
                )
                return elements  # preprocessed Markdown is the LLM-ready view

    # Fall back to the original file.
    if src_path:
        src_abs = _abs_under_data_dir(src_path, data_dir)
        if src_abs and src_abs.exists():
            ext = src_abs.suffix.lower()
            if ext == ".pdf":
                elements.append(
                    cl.Pdf(name=src_abs.name, path=str(src_abs), display="side")
                )
            elif ext in _IMAGE_EXTS:
                elements.append(
                    cl.Image(name=src_abs.name, path=str(src_abs), display="side")
                )
            else:
                try:
                    content = src_abs.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    content = ""
                if content:
                    elements.append(
                        cl.Text(
                            name=src_abs.name,
                            content=content[:20000],
                            display="inline",
                        )
                    )
    return elements


def _abs_under_data_dir(path_str: str, data_dir: Path) -> Path | None:
    """Return ``path_str`` as an absolute Path if it lies under ``data_dir``.

    Registry rows store absolute on-disk paths (e.g. ``/app/data/originals/x.pdf``).
    This guards against a path that escapes the data root (e.g. a row seeded
    from a test fixture pointing outside ``data_dir``) by refusing to resolve
    it. Returns ``None`` on traversal or unresolvable input.
    """
    try:
        root = data_dir.resolve()
    except OSError:
        return None
    try:
        candidate = Path(path_str).resolve()
    except OSError:
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _safe_resolve(data_dir: Path, virtual: str) -> Path | None:
    """Resolve a root-relative virtual path under ``data_dir`` safely.

    Returns ``None`` on traversal attempts or missing segments. This
    mirrors the containment in ``tools._paths.resolve`` but without
    importing the FilesystemBackend (which is chainlit-ui-independent).
    """
    try:
        root = data_dir.resolve()
    except OSError:
        return None
    # Reject any path that tries to escape the root.
    candidate = (root / virtual).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


__all__ = [
    "build_ingestion_summary_plot",
    "build_label_distribution_plot",
    "build_rel_distribution_plot",
    "build_result_dataframe",
    "build_schema_card_props",
    "build_search_score_plot",
    "build_source_elements",
    "build_source_elements_from_row",
]