"""Unit tests for the bilingual (EN/DE) i18n catalog and lookup helpers.

Outside a Chainlit request context, ``cl.user_session.get/set`` are no-ops
(they return None / silently drop), so the i18n module falls back to the
module-level ``_CLI_LANG`` override (set via ``set_lang``) and ultimately
to ``DEFAULT_LANG`` (German). These tests exercise that CLI/pytest path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness import i18n
from falkordb_harness.i18n import (
    DEFAULT_LANG,
    STRINGS,
    get_lang,
    lang_from_accept_language,
    lang_name,
    set_lang,
    t,
)


@pytest.fixture(autouse=True)
def _reset_cli_lang():
    """Reset the module-level CLI language override between tests."""
    i18n._CLI_LANG = None
    yield
    i18n._CLI_LANG = None


# ---------------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------------


def test_catalog_has_both_languages_for_every_key():
    """Every catalog entry MUST define both 'en' and 'de'."""
    incomplete = [
        key
        for key, entry in STRINGS.items()
        if "en" not in entry or "de" not in entry
    ]
    assert not incomplete, f"keys missing a language: {incomplete}"


def test_catalog_no_empty_values():
    """No translation value should be the empty string."""
    empty = [
        (key, lang)
        for key, entry in STRINGS.items()
        for lang, val in entry.items()
        if not str(val)
    ]
    assert not empty, f"empty translations: {empty}"


def test_every_key_is_unique():
    """Sanity: dict keys are inherently unique; this is a readability guard."""
    assert len(STRINGS) == len(set(STRINGS))


# ---------------------------------------------------------------------------
# Lookup behaviour
# ---------------------------------------------------------------------------


def test_t_unknown_key_raises_keyerror():
    with pytest.raises(KeyError):
        t("does.not.exist")


def test_t_returns_default_lang_when_no_override():
    """Without a session or CLI override, the default language (de) applies."""
    assert DEFAULT_LANG == "de"
    assert t("settings.tab.graph.label") == "Graph"


def test_t_format_placeholders_de():
    assert (
        t("ingest.starting", n=3, graph="factory_planning")
        == "Starte Ingestion von 3 Datei(en) in den Wissensgraph `factory_planning`…"
    )


def test_t_format_placeholders_en():
    set_lang("en")
    assert (
        t("ingest.starting", n=3, graph="factory_planning")
        == "Starting ingestion of 3 file(s) into knowledge graph `factory_planning`…"
    )


def test_t_no_format_args_returns_literal():
    set_lang("en")
    assert t("ui_prompt.confirm.default") == "Confirm?"


def test_t_repr_placeholder_in_error_message():
    """The unknown-kind error string uses {!r} formatting on `kind`."""
    set_lang("en")
    assert t("ui_prompt.unknown_kind", kind="bogus") == "error: unknown prompt kind 'bogus'"


# ---------------------------------------------------------------------------
# set_lang / get_lang
# ---------------------------------------------------------------------------


def test_get_lang_defaults_to_german_without_session():
    assert get_lang() == "de"


def test_set_lang_persists_for_subsequent_get():
    set_lang("en")
    assert get_lang() == "en"
    assert t("settings.tab.ingestion.label") == "Ingestion (Expert Settings)"


def test_set_lang_back_to_de_after_en():
    set_lang("en")
    set_lang("de")
    assert get_lang() == "de"
    assert t("settings.tab.ingestion.label") == "Ingestion (Experten-Einstellungen)"


def test_set_lang_rejects_unknown_language():
    with pytest.raises(ValueError):
        set_lang("fr")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# lang_name
# ---------------------------------------------------------------------------


def test_lang_name_in_german():
    # Default lang is de, so the display name is German
    assert lang_name("de") == "Deutsch"
    assert lang_name("en") == "Englisch"


def test_lang_name_in_english():
    set_lang("en")
    assert lang_name("de") == "German"
    assert lang_name("en") == "English"


# ---------------------------------------------------------------------------
# Real-world spot checks on strings used in the UI
# ---------------------------------------------------------------------------


def test_starter_message_roundtrip_both_langs():
    de = t("starter.query.machines.message")
    assert de.startswith("Liste alle Resource-Knoten")
    set_lang("en")
    en = t("starter.query.machines.message")
    assert en.startswith("List all Resource nodes")


def test_chart_titles_localize():
    assert t("chart.nodes_by_label.title") == "Knoten nach Label"
    set_lang("en")
    assert t("chart.nodes_by_label.title") == "Nodes by label"


def test_error_recursion_message_both_langs():
    assert "verheddert" in t("error.recursion")
    set_lang("en")
    assert "stuck" in t("error.recursion")


def test_tools_container_label_both_langs():
    assert t("tools.container.label") == "Tool-Aufrufe"
    set_lang("en")
    assert t("tools.container.label") == "Tool calls"


def test_tools_container_count_formats_n_both_langs():
    assert t("tools.container.count", n=3) == "Tool-Aufrufe (3)"
    set_lang("en")
    assert t("tools.container.count", n=3) == "Tool calls (3)"


def test_settings_create_success_formats_allowed_list():
    msg = t(
        "settings.create.success",
        active="orders_v2",
        allowed="orders_v2, factory_planning",
    )
    assert "`orders_v2`" in msg
    assert "factory_planning" in msg


# ---------------------------------------------------------------------------
# Removed Language-tab settings keys (browser-driven language, no switcher)
# ---------------------------------------------------------------------------


def test_language_tab_keys_removed():
    """The settings-pane Language tab was removed; its keys must be gone."""
    for key in (
        "settings.tab.language.label",
        "settings.ui_language.label",
        "settings.ui_language.desc",
    ):
        assert key not in STRINGS


def test_obsolete_language_bar_keys_removed():
    """The welcome-message language bar was removed; its keys must be gone."""
    assert "lang.bar.message" not in STRINGS
    assert "lang.switched.to" not in STRINGS


# ---------------------------------------------------------------------------
# lang_from_accept_language (browser-driven language routing)
# ---------------------------------------------------------------------------


def test_lang_from_accept_language_german_full_locale():
    assert lang_from_accept_language("de-DE") == "de"


def test_lang_from_accept_language_german_bare():
    assert lang_from_accept_language("de") == "de"


def test_lang_from_accept_language_english_full_locale():
    assert lang_from_accept_language("en-US") == "en"


def test_lang_from_accept_language_english_bare():
    assert lang_from_accept_language("en") == "en"


def test_lang_from_accept_language_unknown_falls_back_to_german():
    assert lang_from_accept_language("fr-FR") == "de"
    assert lang_from_accept_language("ja") == "de"


def test_lang_from_accept_language_none_falls_back_to_german():
    assert lang_from_accept_language(None) == "de"


def test_lang_from_accept_language_empty_falls_back_to_german():
    assert lang_from_accept_language("") == "de"


def test_lang_from_accept_language_case_insensitive():
    assert lang_from_accept_language("EN-us") == "en"
    assert lang_from_accept_language("De-DE") == "de"


def test_lang_from_accept_language_with_qvalue():
    assert lang_from_accept_language("en-US,en;q=0.9") == "en"
    assert lang_from_accept_language("de-DE,de;q=0.9,en;q=0.8") == "de"