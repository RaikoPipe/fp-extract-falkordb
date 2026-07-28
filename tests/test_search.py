"""Unit tests for GraphSearcher mode dispatch and backend search helpers."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.search import GraphSearcher


# --------------------------------------------------------------------------
# Fake FalkorDB node object, mimicking the `Node` properties/labels API.
# --------------------------------------------------------------------------
class _FakeNode:
    def __init__(self, props: dict, labels: list[str]) -> None:
        self.properties = props
        self.labels = labels


class _FakeResult:
    def __init__(self, rows):
        self.result_set = rows


# --------------------------------------------------------------------------
# Backend: fulltext / vector helpers build the right Cypher.
# --------------------------------------------------------------------------
def test_backend_fulltext_search_builds_query():
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend.__new__(FalkorDBBackend)
    backend._graph = MagicMock()

    node = _FakeNode({"name": "AKL-01"}, ["Resource"])
    backend._graph.query.return_value = _FakeResult([[node, 1.5]])

    rows = backend.fulltext_search("AGV", label="Resource", k=5)

    call_args = backend._graph.query.call_args
    cypher, params = call_args.args[0], call_args.args[1]
    assert "db.idx.fulltext.queryNodes" in cypher
    assert "LIMIT 5" in cypher
    assert params == {"label": "Resource", "query": "AGV"}
    assert rows == [
        {
            "name": "AKL-01",
            "_labels": ["Resource"],
            "_score": 1.5,
        }
    ]


def test_backend_vector_search_builds_query():
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend.__new__(FalkorDBBackend)
    backend._graph = MagicMock()

    node = _FakeNode({"name": "M-1"}, ["Resource"])
    backend._graph.query.return_value = _FakeResult([[node, 0.9]])

    rows = backend.vector_search([0.1, 0.2, 0.3], label="Resource", property="embedding", k=3)

    cypher = backend._graph.query.call_args.args[0]
    assert "db.idx.vector.queryNodes" in cypher
    assert "'Resource'" in cypher
    assert "'embedding'" in cypher
    assert "vecf32(" in cypher
    assert "0.1" in cypher and "0.2" in cypher and "0.3" in cypher
    assert rows[0]["name"] == "M-1"
    assert rows[0]["_score"] == 0.9


def test_backend_ensure_fulltext_index_idempotent_on_existing():
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend.__new__(FalkorDBBackend)
    backend._graph = MagicMock()
    backend._graph.query.side_effect = Exception("Index already exists")

    # Should not raise — "already exists" is treated as success.
    backend.ensure_fulltext_index("Resource", ("name",))


def test_backend_ensure_vector_index_idempotent_on_existing():
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend.__new__(FalkorDBBackend)
    backend._graph = MagicMock()
    backend._graph.query.side_effect = Exception("Index already exists")

    backend.ensure_vector_index("Resource", "embedding", dim=128)


def test_backend_ensure_fulltext_index_reraises_other_errors():
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend.__new__(FalkorDBBackend)
    backend._graph = MagicMock()
    backend._graph.query.side_effect = Exception("connection refused")

    try:
        backend.ensure_fulltext_index("Resource", ("name",))
    except Exception as exc:
        assert "connection refused" in str(exc)
    else:
        raise AssertionError("expected exception to propagate")


# --------------------------------------------------------------------------
# GraphSearcher: dispatch + embedding.
# --------------------------------------------------------------------------
def test_searcher_fulltext_delegates_to_backend():
    backend = MagicMock()
    backend.fulltext_search.return_value = [{"name": "AKL-01", "_score": 1.0}]

    searcher = GraphSearcher(backend, mode="fulltext")
    rows = searcher.fulltext_search("AGV")

    backend.ensure_fulltext_index.assert_called_once()
    backend.fulltext_search.assert_called_once()
    assert rows[0]["name"] == "AKL-01"


def test_searcher_vector_search_uses_embedding(monkeypatch):
    backend = MagicMock()
    backend.vector_search.return_value = [{"name": "M-1", "_score": 0.9}]

    searcher = GraphSearcher(backend, mode="vector")

    async def fake_embed(self, text):
        # The dim probe sends "dimension probe"; the real query sends the
        # user's text. Return a 3-dim vector for both so the index is created
        # with dim=3 and the search uses the same vector.
        return [0.5, 0.5, 0.5]

    # Patch the private embedding helper on the class.
    monkeypatch.setattr(GraphSearcher, "_embed", fake_embed)

    rows = asyncio.run(searcher.vector_search("AGV throughput"))

    backend.ensure_vector_index.assert_called_once()
    backend.vector_search.assert_called_once_with(
        [0.5, 0.5, 0.5],
        label=searcher._vector_label,
        property=searcher._vector_property,
        k=searcher._vector_k,
    )
    assert rows[0]["name"] == "M-1"


def test_searcher_run_query_fulltext_mode():
    backend = MagicMock()
    backend.fulltext_search.return_value = [{"name": "X"}]
    searcher = GraphSearcher(backend, mode="fulltext")

    out = asyncio.run(searcher._run_query("hello"))
    backend.fulltext_search.assert_called_once_with(
        "hello", label=searcher._fulltext_label, k=searcher._fulltext_k
    )
    assert out == [{"name": "X"}]


def test_searcher_run_query_vector_mode():
    backend = MagicMock()
    backend.vector_search.return_value = [{"name": "Y"}]
    searcher = GraphSearcher(backend, mode="vector")

    async def fake_embed(self, text):
        return [0.1]

    with patch.object(GraphSearcher, "_embed", fake_embed):
        out = asyncio.run(searcher._run_query("hello"))
    backend.vector_search.assert_called_once()
    assert out == [{"name": "Y"}]


def test_searcher_run_query_graph_mode_calls_natural_language():
    backend = MagicMock()
    searcher = GraphSearcher(backend, mode="graph")

    called = {"yes": False}

    async def fake_nlq(self, question):
        called["yes"] = True
        assert question == "what is an AGV?"
        return "answer"

    with patch.object(GraphSearcher, "natural_language_query", fake_nlq):
        out = asyncio.run(searcher._run_query("what is an AGV?"))

    assert called["yes"] is True
    assert out == "answer"


def test_searcher_default_mode_is_graph():
    searcher = GraphSearcher(MagicMock())
    assert searcher._mode == "graph"