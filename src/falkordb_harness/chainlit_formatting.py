"""Formatting helpers for Chainlit tool step display.

Each tool's raw input/output is transformed into readable Markdown
for the Chainlit Step panel.  All formatters are fail-safe: if parsing
fails, the raw text is returned truncated.
"""
from __future__ import annotations

import json
from typing import Any, Callable

_MAX_ROWS = 50
_MAX_CELL = 120
_DEFAULT_MAX_CHARS = 6000


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + "..."


def _escape_pipe(s: str) -> str:
    return s.replace("|", "\\|")


def _truncate_cell(val: Any) -> str:
    s = str(val) if not isinstance(val, str) else val
    s = _escape_pipe(s)
    return _truncate(s, _MAX_CELL)


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _fmt_default(raw: str, max_chars: int) -> str:
    parsed = _try_parse_json(raw)
    if parsed is not None:
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        return f"```json\n{_truncate(pretty, max_chars - 20)}\n```"
    return _truncate(raw, max_chars)


# ---------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------

def _records_to_table(records: list[dict], max_chars: int) -> str:
    if not records:
        return "*No results.*"

    all_keys: list[str] = []
    seen: set[str] = set()
    for rec in records:
        if isinstance(rec, dict):
            for k in rec:
                if k not in seen:
                    seen.add(k)
                    all_keys.append(k)
    if not all_keys:
        return _fmt_default(json.dumps(records, ensure_ascii=False), max_chars)

    total = len(records)
    display = records[:_MAX_ROWS]

    header = "| " + " | ".join(_escape_pipe(k) for k in all_keys) + " |"
    sep = "| " + " | ".join("---" for _ in all_keys) + " |"
    rows = []
    for rec in display:
        if not isinstance(rec, dict):
            rec = {"value": rec}
        row = "| " + " | ".join(
            _truncate_cell(rec.get(k, "")) for k in all_keys
        ) + " |"
        rows.append(row)

    table = "\n".join([header, sep, *rows])
    if total > _MAX_ROWS:
        table += f"\n\n*... and {total - _MAX_ROWS} more rows (showing {_MAX_ROWS} of {total})*"
    return _truncate(table, max_chars)


# ---------------------------------------------------------------------------
# Per-tool output formatters
# ---------------------------------------------------------------------------

def _fmt_node_table(raw: str, max_chars: int) -> str:
    data = _try_parse_json(raw)
    if isinstance(data, list):
        return _records_to_table(data, max_chars)
    return _fmt_default(raw, max_chars)


def _fmt_edge_table(raw: str, max_chars: int) -> str:
    data = _try_parse_json(raw)
    if isinstance(data, list):
        return _records_to_table(data, max_chars)
    return _fmt_default(raw, max_chars)


def _fmt_cypher_result(raw: str, max_chars: int) -> str:
    data = _try_parse_json(raw)
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            return _records_to_table(data, max_chars)
        lines = [f"{i+1}. {_truncate_cell(r)}" for i, r in enumerate(data[:_MAX_ROWS])]
        if len(data) > _MAX_ROWS:
            lines.append(f"\n*... and {len(data) - _MAX_ROWS} more*")
        return "\n".join(lines) or "*No results.*"
    if isinstance(data, dict):
        return _records_to_table([data], max_chars)
    return _fmt_default(raw, max_chars)


def _fmt_schema(raw: str, max_chars: int) -> str:
    data = _try_parse_json(raw)
    if not isinstance(data, dict):
        return _fmt_default(raw, max_chars)

    sections: list[str] = []
    for heading, key in [
        ("Node Labels", "labels"),
        ("Node Labels", "node_labels"),
        ("Relationship Types", "relationship_types"),
        ("Relationship Types", "relationships"),
        ("Property Keys", "property_keys"),
        ("Property Keys", "properties"),
    ]:
        items = data.get(key)
        if items and isinstance(items, list):
            if any(s.startswith(f"**{heading}**") for s in sections):
                continue
            bullets = "\n".join(f"- `{_escape_pipe(str(i))}`" for i in items)
            sections.append(f"**{heading}**\n{bullets}")

    if not sections:
        return _fmt_default(raw, max_chars)
    return _truncate("\n\n".join(sections), max_chars)


def _fmt_node_count(raw: str, max_chars: int) -> str:
    data = _try_parse_json(raw)
    if isinstance(data, (int, float)):
        return f"**{int(data)}** nodes in the graph."
    if isinstance(data, dict) and "count" in data:
        return f"**{data['count']}** nodes in the graph."
    return _fmt_default(raw, max_chars)


def _fmt_search_results(raw: str, max_chars: int) -> str:
    data = _try_parse_json(raw)
    if not isinstance(data, list):
        return _fmt_default(raw, max_chars)
    if not data:
        return "*No results found.*"

    lines: list[str] = []
    for i, item in enumerate(data[:_MAX_ROWS], 1):
        if isinstance(item, dict):
            name = item.get("name") or item.get("id") or item.get("node_id", "")
            score = item.get("score", "")
            labels = item.get("labels") or item.get("label", "")
            parts = [f"**{_escape_pipe(str(name))}**"]
            if score != "":
                parts.append(f"(score: {score})")
            if labels:
                lbl = ", ".join(labels) if isinstance(labels, list) else str(labels)
                parts.append(f"labels: {_escape_pipe(lbl)}")
            lines.append(f"{i}. " + " - ".join(parts))
        else:
            lines.append(f"{i}. {_truncate_cell(item)}")

    if len(data) > _MAX_ROWS:
        lines.append(f"\n*... and {len(data) - _MAX_ROWS} more results*")
    return _truncate("\n".join(lines), max_chars)


def _fmt_summary_card(raw: str, max_chars: int) -> str:
    data = _try_parse_json(raw)
    if not isinstance(data, dict):
        return _fmt_default(raw, max_chars)

    lines: list[str] = []
    for k, v in data.items():
        if isinstance(v, list):
            lines.append(f"- **{_escape_pipe(str(k))}**: {len(v)} items")
        elif isinstance(v, dict):
            lines.append(f"- **{_escape_pipe(str(k))}**: ...")
        else:
            lines.append(f"- **{_escape_pipe(str(k))}**: {_truncate_cell(v)}")
    return _truncate("\n".join(lines), max_chars) if lines else _fmt_default(raw, max_chars)


def _fmt_kv(raw: str, max_chars: int) -> str:
    data = _try_parse_json(raw)
    if not isinstance(data, dict):
        return _fmt_default(raw, max_chars)
    lines = [f"- **{_escape_pipe(str(k))}**: {_truncate_cell(v)}" for k, v in data.items()]
    return _truncate("\n".join(lines), max_chars) if lines else _fmt_default(raw, max_chars)


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------

_OUTPUT_FORMATTERS: dict[str, Callable[[str, int], str]] = {
    "list_nodes": _fmt_node_table,
    "list_edges": _fmt_edge_table,
    "cypher_query": _fmt_cypher_result,
    "get_schema": _fmt_schema,
    "node_count": _fmt_node_count,
    "fulltext_search": _fmt_search_results,
    "vector_search": _fmt_search_results,
    "chunk_documents": _fmt_summary_card,
    "extract_and_write": _fmt_summary_card,
    "file_metadata": _fmt_kv,
    "preprocess_document": _fmt_kv,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def format_tool_input(tool_name: str, raw_input: Any) -> str:
    """Format a tool's input for display in a Chainlit Step."""
    try:
        if isinstance(raw_input, dict):
            if tool_name == "cypher_query" and "cypher" in raw_input:
                return f"```cypher\n{raw_input['cypher']}\n```"
            pretty = json.dumps(raw_input, indent=2, ensure_ascii=False)
            return f"```json\n{_truncate(pretty, _DEFAULT_MAX_CHARS - 20)}\n```"
        text = str(raw_input) if not isinstance(raw_input, str) else raw_input
        parsed = _try_parse_json(text)
        if isinstance(parsed, dict):
            if tool_name == "cypher_query" and "cypher" in parsed:
                return f"```cypher\n{parsed['cypher']}\n```"
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            return f"```json\n{_truncate(pretty, _DEFAULT_MAX_CHARS - 20)}\n```"
        return _truncate(text, _DEFAULT_MAX_CHARS)
    except Exception:
        return str(raw_input)[:2000]


def format_tool_output(tool_name: str, raw_output: Any, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """Format a tool's output for display in a Chainlit Step."""
    text = str(raw_output) if not isinstance(raw_output, str) else raw_output
    formatter = _OUTPUT_FORMATTERS.get(tool_name, _fmt_default)
    try:
        return formatter(text, max_chars)
    except Exception:
        return _fmt_default(text, max_chars)
