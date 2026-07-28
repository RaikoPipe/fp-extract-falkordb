"""Bilingual (EN/DE) UI string catalog and lookup helpers.

The UI chrome is fully localized; agent replies stay English. The active
language is driven by the browser's ``Accept-Language`` header (exposed by
Chainlit as ``cl.context.session.language``), with German as the default
fallback when the browser locale is not ``de`` or ``en``. Outside a Chainlit
context (e.g. the CLI) the default language applies.

Strings are keyed by a dotted message-id and may contain ``{name}``
placeholders filled via ``str.format`` on lookup. Literal braces in a
message must be escaped as ``{{`` / ``}}``.
"""

from __future__ import annotations

from typing import Literal

Lang = Literal["en", "de"]

DEFAULT_LANG: Lang = "de"
_LANGS: tuple[Lang, ...] = ("en", "de")

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
#
# Every entry MUST define both "en" and "de". ``t()`` raises ``KeyError``
# on a missing message-id and a ``ValueError`` on a missing language (so a
# partially-translated key fails loudly rather than silently falling back).

STRINGS: dict[str, dict[str, str]] = {
    # --- Settings widgets (chainlit_app.py) -------------------------------
    "settings.tab.graph.label": {
        "en": "Graph",
        "de": "Graph",
    },
    "settings.tab.ingestion.label": {
        "en": "Ingestion (Expert Settings)",
        "de": "Ingestion (Experten-Einstellungen)",
    },
    "settings.active_graph.label": {
        "en": "Active knowledge graph",
        "de": "Aktiver Wissensgraph",
    },
    "settings.active_graph.desc": {
        "en": "The graph all queries and ingestion target. Use the checkboxes below to enable more graphs for switching.",
        "de": "Der Graph, auf den alle Anfragen und die Ingestion abzielen. Aktivieren Sie weitere Graphen über die Kontrollkästchen unten, um zwischen ihnen wechseln zu können.",
    },
    "settings.allowed_graphs.label": {
        "en": "Enabled knowledge graphs (in scope for the assistant)",
        "de": "Aktivierte Wissensgraphen (für den Assistenten verfügbar)",
    },
    "settings.allowed_graphs.desc": {
        "en": "Graphs the assistant may switch to via use_graph. The active graph is always included automatically.",
        "de": "Graphen, zu denen der Assistent über use_graph wechseln darf. Der aktive Graph wird automatisch immer einbezogen.",
    },
    "settings.new_graph_name.label": {
        "en": "Create a new knowledge graph",
        "de": "Neuen Wissensgraph erstellen",
    },
    "settings.new_graph_name.placeholder": {
        "en": "e.g. orders_v2",
        "de": "z.B. orders_v2",
    },
    "settings.new_graph_name.desc": {
        "en": "Type a name and hit Save to create a new empty graph on the FalkorDB instance. It will be added to the dropdowns and set as the active graph. Leave blank to skip.",
        "de": "Geben Sie einen Namen ein und klicken Sie auf Speichern, um einen neuen leeren Graphen auf der FalkorDB-Instanz anzulegen. Er wird zu den Dropdowns hinzugefügt und als aktiver Graph gesetzt. Leer lassen, um zu überspringen.",
    },
    "settings.label_filter.label": {
        "en": "Default node-label filter (for list_nodes / search)",
        "de": "Standard-Filter für Knoten-Labels (für list_nodes / search)",
    },
    "settings.label_filter.desc": {
        "en": "Optional comma-separated labels (e.g. Resource, Machine) that the agent prefers when browsing. Leave empty to not bias the agent.",
        "de": "Optionale kommagetrennte Labels (z.B. Resource, Machine), die der Assistent beim Durchsuchen bevorzugt. Leer lassen, um den Assistenten nicht zu beeinflussen.",
    },
    "settings.chunk_size.label": {
        "en": "Chunk size (characters)",
        "de": "Chunk-Größe (Zeichen)",
    },
    "settings.chunk_size.desc": {
        "en": "Size of each text chunk sent to the LLM for entity extraction. Smaller = more precise, larger = fewer calls.",
        "de": "Größe jedes Text-Chunks, der an das LLM zur Entitäten-Extraktion gesendet wird. Kleiner = präziser, größer = weniger Aufrufe.",
    },
    "settings.overlap.label": {
        "en": "Chunk overlap (characters)",
        "de": "Chunk-Überlappung (Zeichen)",
    },
    "settings.overlap.desc": {
        "en": "Overlap between consecutive chunks to preserve context across boundaries.",
        "de": "Überlappung zwischen aufeinanderfolgenden Chunks, um den Kontext an Grenzen zu erhalten.",
    },
    "settings.concurrency.label": {
        "en": "Extraction concurrency",
        "de": "Extraktions-Parallelität",
    },
    "settings.concurrency.desc": {
        "en": "Parallel LLM extraction calls. Higher is faster but uses more rate-limit / token budget.",
        "de": "Parallele LLM-Extraktionsaufrufe. Höher = schneller, verbraucht aber mehr Rate-Limit-/Token-Budget.",
    },
    "settings.overwrite_preprocessed.label": {
        "en": "Re-run preprocessing (overwrite cached .md)",
        "de": "Vorverarbeitung erneut ausführen (zwischengespeicherte .md überschreiben)",
    },
    "settings.overwrite_preprocessed.desc": {
        "en": "When on, docprep re-runs even if the preprocessed .md already exists. Off (default) reuses cached conversions.",
        "de": "Wenn aktiv, wird docprep erneut ausgeführt, selbst wenn die vorverarbeitete .md bereits existiert. Aus (Standard) verwendet zwischengespeicherte Konvertierungen.",
    },
    "settings.merge_mode.label": {
        "en": "Merge mode",
        "de": "Merge-Modus",
    },
    "settings.merge_mode.desc": {
        "en": "How to handle conflicting property values during ingestion: overwrite (last wins), conflict (record disagreements), skip (keep existing).",
        "de": "Umgang mit Konflikten bei Eigenschaftswerten während der Ingestion: overwrite (Letzter gewinnt), conflict (Diskrepanzen protokollieren), skip (Bestehende behalten).",
    },
    # --- Starter categories & prompts (chainlit_app.py) -------------------
    "starter.category.query.label": {"en": "Query", "de": "Abfragen"},
    "starter.category.inspect.label": {"en": "Inspect", "de": "Inspizieren"},
    "starter.category.ingest.label": {"en": "Ingest", "de": "Ingest"},
    "starter.query.machines.label": {
        "en": "Show all machines",
        "de": "Alle Maschinen anzeigen",
    },
    "starter.query.machines.message": {
        "en": "List all Resource nodes that represent machines, with their processing times and capacities.",
        "de": "Liste alle Resource-Knoten auf, die Maschinen darstellen, mit ihren Bearbeitungszeiten und Kapazitäten.",
    },
    "starter.query.transport.label": {
        "en": "Transport routes",
        "de": "Transportrouten",
    },
    "starter.query.transport.message": {
        "en": "What transport routes and vehicles are defined in the knowledge graph?",
        "de": "Welche Transportrouten und Fahrzeuge sind im Wissensgraph definiert?",
    },
    "starter.query.shifts.label": {
        "en": "Shift models",
        "de": "Schichtmodelle",
    },
    "starter.query.shifts.message": {
        "en": "Show me the shift models and worker pools currently in the graph.",
        "de": "Zeige mir die Schichtmodelle und Mitarbeiterpools, die aktuell im Graphen sind.",
    },
    "starter.query.search_resource.label": {
        "en": "Search for a resource",
        "de": "Nach einer Ressource suchen",
    },
    "starter.query.search_resource.message": {
        "en": "Search the knowledge graph for resources related to washing machines.",
        "de": "Durchsuche den Wissensgraph nach Ressourcen, die mit Waschmaschinen zusammenhängen.",
    },
    "starter.inspect.schema.label": {
        "en": "Graph schema",
        "de": "Graphen-Schema",
    },
    "starter.inspect.schema.message": {
        "en": "Show me the full schema of the current knowledge graph — labels, relationships, and properties.",
        "de": "Zeige mir das vollständige Schema des aktuellen Wissensgraphen — Labels, Beziehungen und Eigenschaften.",
    },
    "starter.inspect.reconciliations.label": {
        "en": "Show reconciliations",
        "de": "Rekonzilierungen anzeigen",
    },
    "starter.inspect.reconciliations.message": {
        "en": "List POSSIBLE_DUPLICATE_OF reconciliation links currently in the graph.",
        "de": "Liste aktuell im Graphen vorhandene POSSIBLE_DUPLICATE_OF-Rekonzilierungs-Links auf.",
    },
    "starter.ingest.how.label": {
        "en": "How to ingest documents",
        "de": "Wie Dokumente ingestiert werden",
    },
    "starter.ingest.how.message": {
        "en": "What document types can I upload, and how does the ingestion pipeline work?",
        "de": "Welche Dokumenttypen kann ich hochladen und wie funktioniert die Ingestion-Pipeline?",
    },

    # --- UI prompt callback (chainlit_app.py) -----------------------------
    "ui_prompt.confirm.default": {"en": "Confirm?", "de": "Bestätigen?"},
    "ui_prompt.confirm.label": {"en": "✅ Confirm", "de": "✅ Bestätigen"},
    "ui_prompt.cancel.label": {"en": "❌ Cancel", "de": "❌ Abbrechen"},
    "ui_prompt.question.default": {"en": "?", "de": "?"},
    "ui_prompt.no_response": {"en": "(no response)", "de": "(keine Antwort)"},
    "ui_prompt.unknown_kind": {
        "en": "error: unknown prompt kind {kind!r}",
        "de": "Fehler: unbekannter Prompt-Typ {kind!r}",
    },

    # --- Settings update messages (chainlit_app.py) ----------------------
    "settings.create.value_error": {
        "en": "Could not create knowledge graph `{name}`:\n{exc}",
        "de": "Wissensgraph `{name}` konnte nicht erstellt werden:\n{exc}",
    },
    "settings.create.unreachable": {
        "en": "Could not create knowledge graph `{name}` (FalkorDB unreachable): {exc}",
        "de": "Wissensgraph `{name}` konnte nicht erstellt werden (FalkorDB nicht erreichbar): {exc}",
    },
    "settings.create.success": {
        "en": (
            "Created new empty knowledge graph `{active}` and switched to it.\n"
            "- Active: `{active}`\n"
            "- Enabled: `{allowed}`\n\n"
            "Queries and ingestion now target `{active}`. I can switch to any of the enabled graphs via `use_graph`."
        ),
        "de": (
            "Neuer leerer Wissensgraph `{active}` erstellt und aktiviert.\n"
            "- Aktiv: `{active}`\n"
            "- Aktiviert: `{allowed}`\n\n"
            "Anfragen und Ingestion zielen jetzt auf `{active}`. Ich kann über `use_graph` zu jedem der aktivierten Graphen wechseln."
        ),
    },
    "settings.update.success": {
        "en": (
            "Knowledge graph selection updated.\n"
            "- Active: `{active}`\n"
            "- Enabled: `{allowed}`\n\n"
            "Queries and ingestion now target `{active}`. I can switch to any of the enabled graphs via `use_graph`."
        ),
        "de": (
            "Auswahl des Wissensgraphen aktualisiert.\n"
            "- Aktiv: `{active}`\n"
            "- Aktiviert: `{allowed}`\n\n"
            "Anfragen und Ingestion zielen jetzt auf `{active}`. Ich kann über `use_graph` zu jedem der aktivierten Graphen wechseln."
        ),
    },

    # --- Ingest action callback (chainlit_app.py) -------------------------
    "ingest.no_files": {
        "en": "No files uploaded yet. Upload one or more documents (use the paperclip / attachment button in the chat input) and then press **Ingest Documents** again.",
        "de": "Noch keine Dateien hochgeladen. Laden Sie ein oder mehrere Dokumente hoch (Büroklammer-/Anhang-Button in der Chat-Eingabe) und klicken Sie dann erneut auf **Dokumente ingestieren**.",
    },
    "ingest.starting": {
        "en": "Starting ingestion of {n} file(s) into knowledge graph `{graph}`…",
        "de": "Starte Ingestion von {n} Datei(en) in den Wissensgraph `{graph}`…",
    },
    "ingest.stage.stage": {"en": "Stage files", "de": "Dateien bereitstellen"},
    "ingest.stage.preprocess": {"en": "Convert documents", "de": "Dokumente konvertieren"},
    "ingest.stage.chunk": {"en": "Chunk text", "de": "Text chunken"},
    "ingest.stage.extract": {"en": "LLM entity extraction", "de": "LLM-Entitäten-Extraktion"},
    "ingest.stage.write": {"en": "Write to knowledge graph", "de": "In Wissensgraph schreiben"},
    "ingest.failed.file": {
        "en": "{stage}: {file} — failed: {err}",
        "de": "{stage}: {file} — fehlgeschlagen: {err}",
    },
    "ingest.failed.stage": {
        "en": "{stage} — failed: {err}",
        "de": "{stage} — fehlgeschlagen: {err}",
    },
    "ingest.failed.pipeline": {
        "en": "Ingestion failed: {exc}",
        "de": "Ingestion fehlgeschlagen: {exc}",
    },
    "ingest.summary.complete": {
        "en": "**Ingestion complete** into graph `{graph}`.",
        "de": "**Ingestion abgeschlossen** in Graph `{graph}`.",
    },
    "ingest.summary.files_staged": {"en": "- Files staged: {n}", "de": "- Dateien bereitgestellt: {n}"},
    "ingest.summary.files_preprocessed": {"en": "- Files preprocessed: {n}", "de": "- Dateien vorverarbeitet: {n}"},
    "ingest.summary.chunks": {"en": "- Chunks processed: {n}", "de": "- Chunks verarbeitet: {n}"},
    "ingest.summary.extractions": {"en": "- LLM extractions: {n}", "de": "- LLM-Extraktionen: {n}"},
    "ingest.summary.cypher": {"en": "- Cypher statements: {n}", "de": "- Cypher-Anweisungen: {n}"},
    "ingest.summary.nodes": {"en": "- Nodes in graph: {n}", "de": "- Knoten im Graphen: {n}"},
    "ingest.summary.conflicts": {"en": "- Conflicts detected: {n}", "de": "- Konflikte erkannt: {n}"},
    "ingest.summary.merge_mode": {"en": "- Merge mode: {mode}", "de": "- Merge-Modus: {mode}"},
    "ingest.summary.errors.header": {"en": "- Errors ({n}):", "de": "- Fehler ({n}):"},
    "ingest.summary.errors.more": {"en": "  - …and {n} more", "de": "  - …und {n} weitere"},

    # --- Upload receipt (chainlit_app.py on_message) ----------------------
    "upload.receipt": {
        "en": "Received **{n_new}** file(s). **{n_total}** total file(s) ready for ingestion into graph `{graph}`.",
        "de": "**{n_new}** Datei(en) empfangen. Insgesamt **{n_total}** Datei(en) bereit zur Ingestion in Graph `{graph}`.",
    },
    "upload.ingest_now.label": {
        "en": "Ingest Documents Now",
        "de": "Dokumente jetzt ingestieren",
    },
    "upload.ingest_now.tooltip": {
        "en": "Preprocess, chunk, LLM-extract, and write all uploaded files into the active knowledge graph.",
        "de": "Vorverarbeiten, chunken, LLM-extrahieren und alle hochgeladenen Dateien in den aktiven Wissensgraph schreiben.",
    },

    # --- Recursion / error messages (chainlit_app.py on_message) ---------
    "error.recursion": {
        "en": "I got stuck re-checking the same things and ran out of steps before finishing. Here's what I have so far — could you rephrase or tell me which file/part to focus on?",
        "de": "Ich habe mich beim erneuten Prüfen derselben Dinge verheddert und die Schritte sind vor Abschluss ausgelaufen. Hier ist, was ich bisher habe — könnten Sie umformulieren oder mir sagen, auf welche Datei/welchen Teil ich mich konzentrieren soll?",
    },
    "error.interrupted.partial": {
        "en": "\n\n---\n*(Processing was interrupted by an error. The partial response above may be incomplete.)*",
        "de": "\n\n---\n*(Die Verarbeitung wurde durch einen Fehler unterbrochen. Die obige Teil-Antwort möglicherweise unvollständig.)*",
    },
    "error.unexpected": {
        "en": "An unexpected error occurred while processing your request. Please try again or rephrase your question.",
        "de": "Bei der Verarbeitung Ihrer Anfrage ist ein unerwarteter Fehler aufgetreten. Bitte versuchen Sie es erneut oder formulieren Sie Ihre Frage um.",
    },
    "error.interrupted.step": {
        "en": "(interrupted by error)",
        "de": "(durch Fehler unterbrochen)",
    },

    # --- ElementSidebar schema title (chainlit_app.py) --------------------
    "sidebar.schema.title": {
        "en": "Schema — {active}",
        "de": "Schema — {active}",
    },

    # --- Formatting helpers (chainlit_formatting.py) ----------------------
    "fmt.no_results": {"en": "*No results.*", "de": "*Keine Ergebnisse.*"},
    "fmt.more_rows": {
        "en": "\n\n*... and {n} more rows (showing {shown} of {total})*",
        "de": "\n\n*... und {n} weitere Zeilen ({shown} von {total} angezeigt)*",
    },
    "fmt.more": {
        "en": "\n*... and {n} more*",
        "de": "\n*... und {n} weitere*",
    },
    "fmt.no_results_found": {"en": "*No results found.*", "de": "*Keine Ergebnisse gefunden.*"},
    "fmt.more_results": {
        "en": "\n*... and {n} more results*",
        "de": "\n*... und {n} weitere Ergebnisse*",
    },
    "fmt.schema.heading.node_labels": {"en": "Node Labels", "de": "Knoten-Labels"},
    "fmt.schema.heading.rel_types": {"en": "Relationship Types", "de": "Beziehungs-Typen"},
    "fmt.schema.heading.prop_keys": {"en": "Property Keys", "de": "Eigenschafts-Schlüssel"},
    "fmt.node_count.one": {
        "en": "**{n}** nodes in the graph.",
        "de": "**{n}** Knoten im Graphen.",
    },

    # --- Visual elements (chainlit_elements.py) ---------------------------
    "chart.nodes_by_label.title": {"en": "Nodes by label", "de": "Knoten nach Label"},
    "chart.nodes_by_label.x": {"en": "Label", "de": "Label"},
    "chart.nodes_by_label.y": {"en": "Node count", "de": "Knotenanzahl"},
    "chart.rel_by_type.title": {"en": "Relationships by type", "de": "Beziehungen nach Typ"},
    "chart.rel_by_type.x": {"en": "Relationship type", "de": "Beziehungstyp"},
    "chart.rel_by_type.y": {"en": "Count", "de": "Anzahl"},
    "chart.search_scores.title": {"en": "Search relevance scores", "de": "Suchrelevanz-Scores"},
    "chart.search_scores.x": {"en": "Node", "de": "Knoten"},
    "chart.search_scores.y": {"en": "Relevance", "de": "Relevanz"},
    "chart.ingestion_summary.title": {"en": "Ingestion pipeline summary", "de": "Ingestion-Pipeline Zusammenfassung"},
    "chart.ingestion_summary.x": {"en": "Stage", "de": "Stufe"},
    "chart.ingestion_summary.y": {"en": "Count", "de": "Anzahl"},
    "chart.ingestion_summary.stage.files_staged": {"en": "Files staged", "de": "Dateien bereitgestellt"},
    "chart.ingestion_summary.stage.preprocessed": {"en": "Preprocessed", "de": "Vorverarbeitet"},
    "chart.ingestion_summary.stage.chunks": {"en": "Chunks", "de": "Chunks"},
    "chart.ingestion_summary.stage.extractions": {"en": "Extractions", "de": "Extraktionen"},
    "chart.ingestion_summary.stage.cypher": {"en": "Cypher stmts", "de": "Cypher-Anweisungen"},
    "chart.ingestion_summary.stage.conflicts": {"en": "Conflicts", "de": "Konflikte"},
    "element.preprocessed.name": {
        "en": "Preprocessed: {name}",
        "de": "Vorverarbeitet: {name}",
    },

    # --- CLI fallback prompts (ui_prompts.py) ------------------------------
    "cli.confirm.prompt": {"en": "Proceed? [y/N] ", "de": "Fortfahren? [j/N] "},
    "cli.question.no_answer": {"en": "(no answer)", "de": "(keine Antwort)"},

    # --- Language switcher (chainlit_app.py) -------------------------------
    "lang.name.en": {"en": "English", "de": "Englisch"},
    "lang.name.de": {"en": "German", "de": "Deutsch"},
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def lang_from_accept_language(header: str | None) -> Lang:
    """Map a Chainlit ``session.language`` (or ``Accept-Language`` header) to a Lang.

    Accepts both full locales (``"de-DE"``, ``"en-US"``) and bare language
    codes (``"de"``, ``"en"``), case-insensitively. Anything that is not
    recognisably English falls back to German (the project default), so
    unknown locales get the German chrome and the German ``chainlit.md``
    fallback README.
    """
    if header:
        tag = header.strip().split("-")[0].split(";")[0].lower()
        if tag == "en":
            return "en"
    return "de"


def _session_lang() -> Lang | None:
    """Return the active language for the current Chainlit session, if any.

    Resolution order:
    1. An explicit per-session override (``cl.user_session["lang"]``), if set
       by application code. ``on_chat_start`` seeds this from the browser
       locale via :func:`lang_from_accept_language`.
    2. Otherwise ``None`` — :func:`_effective_lang` then applies the CLI
       override (if any) or :data:`DEFAULT_LANG`.
    """
    try:
        import chainlit as cl

        # cl.user_session is a thread-local proxy; ``get`` returns None outside
        # a request context, which is the CLI/pytest path.
        lang = cl.user_session.get("lang")
        if lang in _LANGS:
            return lang  # type: ignore[return-value]
    except Exception:  # noqa: BLE001, S110 — not in a Chainlit context
        pass
    return None


def get_lang() -> Lang:
    """Return the active language (session override or the default)."""
    return _effective_lang()


def set_lang(lang: Lang) -> None:
    """Persist the language choice into the current Chainlit session.

    Also updates the module-level ``_CLI_LANG`` fallback so ``set_lang`` is
    meaningful outside a Chainlit request context (CLI/pytest). When a real
    session is active, ``_session_lang`` takes precedence over ``_CLI_LANG``
    in :func:`_effective_lang`, so setting both is safe.
    """
    if lang not in _LANGS:
        raise ValueError(f"unsupported language: {lang!r}")
    global _CLI_LANG
    _CLI_LANG = lang
    try:
        import chainlit as cl

        cl.user_session.set("lang", lang)
    except Exception:  # noqa: BLE001, S110 — not in a Chainlit context
        pass


_CLI_LANG: Lang | None = None


def _effective_lang() -> Lang:
    """Resolve the language for lookups: session > CLI override > default."""
    lang = _session_lang()
    if lang is not None:
        return lang
    if _CLI_LANG is not None:
        return _CLI_LANG
    return DEFAULT_LANG


def t(key: str, **fmt) -> str:
    """Look up a localized string by message-id and format placeholders.

    Raises ``KeyError`` if the message-id is unknown and ``ValueError`` if a
    known message-id is missing a language entry (so a half-translated key
    fails loudly during development rather than silently degrading).
    """
    entry = STRINGS.get(key)
    if entry is None:
        raise KeyError(f"unknown i18n key: {key!r}")
    lang = _effective_lang()
    text = entry.get(lang)
    if text is None:
        raise ValueError(f"missing {lang!r} translation for key: {key!r}")
    if fmt:
        return text.format(**fmt)
    return text


def lang_name(lang: Lang) -> str:
    """Return the display name of ``lang`` in the currently active language."""
    return t(f"lang.name.{lang}")


__all__ = [
    "DEFAULT_LANG",
    "STRINGS",
    "Lang",
    "get_lang",
    "lang_from_accept_language",
    "lang_name",
    "set_lang",
    "t",
]