"""Unit tests for the reconciliation engine (knowledge.reconciliation).

These tests exercise the pure decision logic and LLM/embedding wrappers with
monkeypatched chat/embedding clients — no live FalkorDB or LLM connection
required.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.reconciliation import (
    Candidate,
    ReconciliationDecision,
    coalesce_description,
    llm_pairwise_confidence,
    reconcile_new_node,
)
from knowledge.graph_models.factory_graph_model import Resource


def _patch_chat(monkeypatch, response_factory):
    """Patch ``chat_client`` so ``chat.completions.create`` returns a fake.

    ``response_factory`` is a zero-arg callable returning a fake response whose
    ``choices[0].message.content`` holds the model output string. The fake
    ``create`` captures its ``messages`` kwarg into ``captured["messages"]``.
    """
    captured: dict = {}

    class FakeCreate:
        async def create(self, **kwargs):
            captured["messages"] = kwargs.get("messages")
            return response_factory()

    class FakeChat:
        completions = FakeCreate()

    class FakeClient:
        chat = FakeChat()

    import knowledge.reconciliation as _rec

    monkeypatch.setattr(_rec, "chat_client", lambda: FakeClient())
    return captured


# --------------------------------------------------------------------------
# coalesce_description
# --------------------------------------------------------------------------
def test_coalesce_description_returns_empty_for_both_empty():
    result = asyncio.run(coalesce_description("", ""))
    assert result == ""


def test_coalesce_description_returns_other_when_one_empty():
    assert asyncio.run(coalesce_description("", "incoming")) == "incoming"
    assert asyncio.run(coalesce_description("existing", "")) == "existing"


def test_coalesce_description_short_circuits_on_equal():
    """When both descriptions are identical, no LLM call is needed."""
    result = asyncio.run(coalesce_description("same", "same"))
    assert result == "same"


def test_coalesce_description_calls_llm_when_different(monkeypatch):
    """Different descriptions trigger an LLM coalesce call."""
    def factory():
        return type("r", (), {"choices": [type("c", (), {"message": type("m", (), {"content": "Coalesced description"})()})()]})()

    captured = _patch_chat(monkeypatch, factory)
    result = asyncio.run(coalesce_description("old desc", "new desc"))
    assert result == "Coalesced description"
    assert len(captured["messages"]) == 2


# --------------------------------------------------------------------------
# llm_pairwise_confidence
# --------------------------------------------------------------------------
def test_llm_pairwise_confidence_parses_json(monkeypatch):
    def factory():
        return type("r", (), {"choices": [type("c", (), {"message": type("m", (), {"content": '{"confidence": 0.95}'})()})()]})()

    _patch_chat(monkeypatch, factory)

    new_node = Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine")
    confidence = asyncio.run(llm_pairwise_confidence(new_node, {"name": "AKL-01", "resource_type": "machine"}))
    assert confidence == 0.95


def test_llm_pairwise_confidence_clamps_to_0_1(monkeypatch):
    def factory():
        return type("r", (), {"choices": [type("c", (), {"message": type("m", (), {"content": '{"confidence": 1.5}'})()})()]})()

    _patch_chat(monkeypatch, factory)

    new_node = Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine")
    confidence = asyncio.run(llm_pairwise_confidence(new_node, {"name": "AKL-01"}))
    assert confidence == 1.0


def test_llm_pairwise_confidence_defaults_to_zero_on_bad_json(monkeypatch):
    def factory():
        return type("r", (), {"choices": [type("c", (), {"message": type("m", (), {"content": "not json"})()})()]})()

    _patch_chat(monkeypatch, factory)

    new_node = Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine")
    confidence = asyncio.run(llm_pairwise_confidence(new_node, {"name": "AKL-01"}))
    assert confidence == 0.0


def test_llm_pairwise_confidence_strips_markdown_fences(monkeypatch):
    def factory():
        return type("r", (), {"choices": [type("c", (), {"message": type("m", (), {"content": '```json\n{"confidence": 0.88}\n```'})()})()]})()

    _patch_chat(monkeypatch, factory)

    new_node = Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine")
    confidence = asyncio.run(llm_pairwise_confidence(new_node, {"name": "AKL-01"}))
    assert confidence == 0.88


# --------------------------------------------------------------------------
# reconcile_new_node: decision logic
# --------------------------------------------------------------------------
def test_reconcile_new_node_no_candidates_returns_not_linked():
    """When vector_search returns no results, decision is not linked."""
    backend = MagicMock()
    backend.vector_search.return_value = []

    new_node = Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine")
    decision = asyncio.run(reconcile_new_node(
        backend, new_node, embedding=[0.1, 0.2],
        cosine_cutoff=0.70, confidence_threshold=0.90,
    ))
    assert decision.linked is False
    assert decision.matched_name is None
    assert decision.record is None


def test_reconcile_new_node_below_cosine_cutoff_returns_not_linked():
    """Candidates below the cosine cutoff are filtered out."""
    backend = MagicMock()
    backend.vector_search.return_value = [
        {"name": "AKL-01", "resource_type": "machine", "_score": 0.50, "_labels": ["Resource"]},
    ]

    new_node = Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine")
    decision = asyncio.run(reconcile_new_node(
        backend, new_node, embedding=[0.1, 0.2],
        cosine_cutoff=0.70, confidence_threshold=0.90,
    ))
    assert decision.linked is False
    assert len(decision.candidates) == 0


def test_reconcile_new_node_below_confidence_threshold_returns_not_linked(monkeypatch):
    """Candidate passes cosine but LLM confidence is below threshold."""
    backend = MagicMock()
    backend.vector_search.return_value = [
        {"name": "AKL-01", "resource_type": "machine", "description": "An AS/RS", "_score": 0.85, "_labels": ["Resource"]},
    ]

    def factory():
        return type("r", (), {"choices": [type("c", (), {"message": type("m", (), {"content": '{"confidence": 0.80}'})()})()]})()

    _patch_chat(monkeypatch, factory)

    new_node = Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine")
    decision = asyncio.run(reconcile_new_node(
        backend, new_node, embedding=[0.1, 0.2],
        cosine_cutoff=0.70, confidence_threshold=0.90,
    ))
    assert decision.linked is False
    assert decision.matched_name == "AKL-01"
    assert decision.llm_confidence == 0.80
    assert decision.cosine_similarity == 0.85
    assert decision.record is None


def test_reconcile_new_node_above_confidence_threshold_returns_linked(monkeypatch):
    """Candidate passes both cosine and confidence threshold -> linked."""
    backend = MagicMock()
    backend.vector_search.return_value = [
        {"name": "AKL-01", "resource_type": "machine", "description": "An AS/RS", "_score": 0.85, "_labels": ["Resource"]},
    ]

    def factory():
        return type("r", (), {"choices": [type("c", (), {"message": type("m", (), {"content": '{"confidence": 0.95}'})()})()]})()

    _patch_chat(monkeypatch, factory)

    new_node = Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine")
    decision = asyncio.run(reconcile_new_node(
        backend, new_node, embedding=[0.1, 0.2],
        cosine_cutoff=0.70, confidence_threshold=0.90,
        source="doc.md", chunk_index=3,
    ))
    assert decision.linked is True
    assert decision.matched_name == "AKL-01"
    assert decision.llm_confidence == 0.95
    assert decision.cosine_similarity == 0.85
    assert decision.record is not None
    assert decision.record["new_name"] == "Machine"
    assert decision.record["matched_name"] == "AKL-01"
    assert decision.record["source"] == "doc.md"
    assert decision.record["chunk_index"] == 3
    assert "detected_at" in decision.record


def test_reconcile_new_node_skips_self_match():
    """A candidate with the same name as the new node is skipped."""
    backend = MagicMock()
    backend.vector_search.return_value = [
        {"name": "Machine", "resource_type": "machine", "_score": 0.99, "_labels": ["Resource"]},
    ]

    new_node = Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine")
    decision = asyncio.run(reconcile_new_node(
        backend, new_node, embedding=[0.1, 0.2],
        cosine_cutoff=0.70, confidence_threshold=0.90,
    ))
    assert decision.linked is False
    assert len(decision.candidates) == 0


def test_reconcile_new_node_picks_highest_confidence(monkeypatch):
    """When multiple candidates pass cosine, the highest LLM confidence wins."""
    backend = MagicMock()
    backend.vector_search.return_value = [
        {"name": "AKL-01", "resource_type": "machine", "_score": 0.85, "_labels": ["Resource"]},
        {"name": "AKL-02", "resource_type": "machine", "_score": 0.80, "_labels": ["Resource"]},
    ]

    confidences = iter([0.75, 0.92])

    def factory():
        return type("r", (), {"choices": [type("c", (), {"message": type("m", (), {"content": f'{{"confidence": {next(confidences)}}}'})()})()]})()

    _patch_chat(monkeypatch, factory)

    new_node = Resource(name="Machine", name_has_index=False, description="A machine", resource_type="machine")
    decision = asyncio.run(reconcile_new_node(
        backend, new_node, embedding=[0.1, 0.2],
        cosine_cutoff=0.70, confidence_threshold=0.90,
    ))
    assert decision.linked is True
    assert decision.matched_name == "AKL-02"
    assert decision.llm_confidence == 0.92