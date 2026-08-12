"""Tests for lazy connection + reconnect in FalkorDBBackend and the
non-poisoning backend cache in falkordb_harness.backend.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.falkordb_backend import FalkorDBBackend


class _FakeResult:
    def __init__(self, rows):
        self.result_set = rows


# ---------------------------------------------------------------------------
# Lazy connection
# ---------------------------------------------------------------------------
def test_backend_does_not_connect_on_construction():
    """__init__ must NOT call FalkorDB()/select_graph(); connection is lazy."""
    with patch(
        "knowledge.falkordb_backend.FalkorDB", autospec=True
    ) as fake_ctor:
        backend = FalkorDBBackend(host="h", port=6379, graph_name="g")
        fake_ctor.assert_not_called()
        assert backend._db is None
        assert backend._graph is None


def test_get_graph_connects_lazily_on_first_use():
    with patch(
        "knowledge.falkordb_backend.FalkorDB", autospec=True
    ) as fake_ctor:
        fake_db = MagicMock()
        fake_graph = MagicMock()
        fake_ctor.return_value = fake_db
        fake_db.select_graph.return_value = fake_graph

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g")
        assert backend._graph is None

        g = backend._get_graph()
        assert g is fake_graph
        fake_ctor.assert_called_once_with(host="h", port=6379)
        fake_db.select_graph.assert_called_once_with("g")

        g2 = backend._get_graph()
        assert g2 is fake_graph
        assert fake_ctor.call_count == 1


# ---------------------------------------------------------------------------
# Reconnect on transient error
# ---------------------------------------------------------------------------
def test_query_invalidates_handle_on_transient_then_reconnects():
    """On a transient query error, _query drops the handle so a retry reconnects.

    Simulates: first query raises ConnectionRefusedError (transient) -> handle
    invalidated. The next _get_graph() builds a fresh FalkorDB + graph handle,
    and the retried query succeeds.
    """
    with patch(
        "knowledge.falkordb_backend.FalkorDB", autospec=True
    ) as fake_ctor:
        graph1 = MagicMock()
        graph1.query.side_effect = ConnectionRefusedError("redis down")
        graph2 = MagicMock()
        graph2.query.return_value = _FakeResult([["ok"]])
        fake_db1 = MagicMock()
        fake_db1.select_graph.return_value = graph1
        fake_db2 = MagicMock()
        fake_db2.select_graph.return_value = graph2
        fake_ctor.side_effect = [fake_db1, fake_db2]

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g")

        with pytest.raises(ConnectionRefusedError):
            backend._query("MATCH (n) RETURN n")
        assert backend._graph is None

        out = backend._query("MATCH (n) RETURN n")
        assert out.result_set == [["ok"]]
        assert fake_ctor.call_count == 2
        assert backend._graph is graph2


def test_query_does_not_invalidate_on_non_transient_error():
    with patch(
        "knowledge.falkordb_backend.FalkorDB", autospec=True
    ) as fake_ctor:
        graph = MagicMock()
        graph.query.side_effect = ValueError("bad cypher syntax")
        fake_db = MagicMock()
        fake_db.select_graph.return_value = graph
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g")
        with pytest.raises(ValueError):
            backend._query("MATCH (n) RETURN n")
        assert backend._graph is graph
        assert fake_ctor.call_count == 1


def test_query_index_already_exists_not_treated_as_transient():
    """The 'index already exists' FalkorDB idempotency error must neither be
    retried nor invalidate the handle."""
    with patch(
        "knowledge.falkordb_backend.FalkorDB", autospec=True
    ) as fake_ctor:
        graph = MagicMock()
        graph.query.side_effect = Exception("Index already exists")
        fake_db = MagicMock()
        fake_db.select_graph.return_value = graph
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g")
        with pytest.raises(Exception, match="already exists"):
            backend._query("CALL db.idx.fulltext.createNodeIndex(...)")
        assert backend._graph is graph
        assert fake_ctor.call_count == 1


def test_execute_routes_through_query_and_reconnects():
    """The public execute() funnels through _query, so it benefits from the
    reconnect-on-transient behaviour too."""
    with patch(
        "knowledge.falkordb_backend.FalkorDB", autospec=True
    ) as fake_ctor:
        graph1 = MagicMock()
        graph1.query.side_effect = ConnectionResetError("reset by peer")
        graph2 = MagicMock()
        graph2.query.return_value = _FakeResult([[42]])
        fake_db1 = MagicMock()
        fake_db1.select_graph.return_value = graph1
        fake_db2 = MagicMock()
        fake_db2.select_graph.return_value = graph2
        fake_ctor.side_effect = [fake_db1, fake_db2]

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g")

        with pytest.raises(ConnectionResetError):
            backend.execute("MATCH (n) RETURN count(n)")
        result = backend.execute("MATCH (n) RETURN count(n)")
        assert result.result_set == [[42]]
        assert fake_ctor.call_count == 2


# ---------------------------------------------------------------------------
# End-to-end: retry_sync + backend reconnect
# ---------------------------------------------------------------------------
def test_retry_sync_recovers_when_backend_reconnects():
    """A tool-style wrapper around backend.execute that retries should see the
    backend reconnect between attempts and ultimately succeed."""
    with patch(
        "knowledge.falkordb_backend.FalkorDB", autospec=True
    ) as fake_ctor:
        graph1 = MagicMock()
        graph1.query.side_effect = ConnectionRefusedError("down")
        graph2 = MagicMock()
        graph2.query.return_value = _FakeResult([["recovered"]])
        fake_db1 = MagicMock()
        fake_db1.select_graph.return_value = graph1
        fake_db2 = MagicMock()
        fake_db2.select_graph.return_value = graph2
        fake_ctor.side_effect = [fake_db1, fake_db2]

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g")

        from knowledge.retry import retry_sync

        out = retry_sync(
            lambda: backend.execute("MATCH (n) RETURN n"),
            max_attempts=3,
            sleep=lambda d: None,
        )
        assert out.result_set == [["recovered"]]
        assert fake_ctor.call_count == 2


# ---------------------------------------------------------------------------
# Non-poisoning backend cache
# ---------------------------------------------------------------------------
def test_get_backend_does_not_cache_construction_failure(monkeypatch):
    """If FalkorDBBackend() raises (e.g. bad env), the cache stays empty so
    the next call retries construction instead of re-raising the cached error."""
    import falkordb_harness.backend as harness_backend

    harness_backend.reset_backend_cache()

    calls = {"n": 0}

    def fail_then_succeed(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("construction blew up")
        return MagicMock()

    monkeypatch.setattr(harness_backend, "FalkorDBBackend", fail_then_succeed)

    with pytest.raises(RuntimeError, match="construction blew up"):
        harness_backend.get_backend()

    backend = harness_backend.get_backend()
    assert backend is not None
    assert calls["n"] == 2

    harness_backend.reset_backend_cache()


def test_get_backend_caches_success(monkeypatch):
    import falkordb_harness.backend as harness_backend

    harness_backend.reset_backend_cache()

    calls = {"n": 0}

    def make(*args, **kwargs):
        calls["n"] += 1
        return MagicMock()

    monkeypatch.setattr(harness_backend, "FalkorDBBackend", make)

    b1 = harness_backend.get_backend()
    b2 = harness_backend.get_backend()
    assert b1 is b2
    assert calls["n"] == 1

    harness_backend.reset_backend_cache()


def test_reset_backend_cache_drops_instances(monkeypatch):
    import falkordb_harness.backend as harness_backend

    harness_backend.reset_backend_cache()
    monkeypatch.setattr(harness_backend, "FalkorDBBackend", lambda *a, **k: MagicMock())

    b1 = harness_backend.get_backend()
    assert b1 is not None
    harness_backend.reset_backend_cache()
    b2 = harness_backend.get_backend()
    assert b2 is not b1

    harness_backend.reset_backend_cache()