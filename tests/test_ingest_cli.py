"""Unit tests for the ingest CLI flag plumbing (no FalkorDB / LLM required)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ingest as ingest_mod
from knowledge.cypher_mapper import MergeMode


def test_build_parser_merge_mode_default_none():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--ingest"])
    assert args.merge_mode is None


def test_build_parser_merge_mode_conflict_flag():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--ingest", "--merge-mode", "conflict"])
    assert args.merge_mode == "conflict"


def test_build_parser_merge_mode_overwrite_flag():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--ingest", "--merge-mode", "overwrite"])
    assert args.merge_mode == "overwrite"


def test_build_parser_recon_flag():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--ingest", "--recon"])
    assert args.recon_enabled is True


def test_build_parser_no_recon_flag():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--ingest", "--no-recon"])
    assert args.recon_enabled is False


def test_build_parser_recon_default_none():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--ingest"])
    assert args.recon_enabled is None


def test_build_parser_recon_posthoc_flag():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--recon-posthoc"])
    assert args.recon_posthoc is True


def test_build_parser_recon_cosine_cutoff():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--ingest", "--recon-cosine-cutoff", "0.65"])
    assert args.recon_cosine_cutoff == 0.65


def test_build_parser_recon_confidence_threshold():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--ingest", "--recon-confidence-threshold", "0.85"])
    assert args.recon_confidence_threshold == 0.85


def test_build_parser_recon_top_k():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--ingest", "--recon-top-k", "5"])
    assert args.recon_top_k == 5


def test_build_parser_reconciliations_log():
    parser = ingest_mod.build_parser()
    args = parser.parse_args(["--ingest", "--reconciliations-log", "/tmp/r.jsonl"])
    assert args.reconciliations_log == "/tmp/r.jsonl"


def test_main_passes_recon_flags_to_run(monkeypatch):
    """--recon + --recon-posthoc flow from argv through to run()."""
    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)

    def fake_asyncio_run(coro):
        import asyncio as _a
        loop = _a.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(ingest_mod, "run", fake_run)
    monkeypatch.setattr(ingest_mod.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(sys, "argv", [
        "ingest.py", "--ingest", "--recon", "--recon-posthoc",
        "--recon-cosine-cutoff", "0.65",
        "--recon-confidence-threshold", "0.85",
        "--recon-top-k", "15",
        "--reconciliations-log", "/tmp/recon.jsonl",
    ])

    ingest_mod.main()

    assert captured.get("recon_enabled") is True
    assert captured.get("recon_posthoc") is True
    assert captured.get("recon_cosine_cutoff") == 0.65
    assert captured.get("recon_confidence_threshold") == 0.85
    assert captured.get("recon_top_k") == 15
    assert captured.get("reconciliations_log") == "/tmp/recon.jsonl"