"""Tests for per-session knowledge-graph selection.

Covers:
- ``FalkorDBBackend.list_graphs`` (DB-level GRAPH.LIST)
- ``FalkorDBBackend.set_active_graph`` (handle invalidation + allowlist)
- ``FalkorDBBackend._get_db`` lazy connect (without selecting a graph)
- the ``use_graph`` tool (allowlist enforcement, structured error JSON)
- the ``list_graphs`` tool (surfaces names)
- the per-session contextvar backend in ``falkordb_harness.backend``
- the Chainlit settings-dict -> configurable normalization helper
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, rows):
        self.result_set = rows


# ---------------------------------------------------------------------------
# FalkorDBBackend.list_graphs
# ---------------------------------------------------------------------------
def test_list_graphs_calls_db_list_graphs():
    """list_graphs() proxies to the FalkorDB client's list_graphs()."""
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        fake_db.list_graphs.return_value = ["factory_planning", "orders", "legacy"]
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="factory_planning")
        names = backend.list_graphs()

        assert names == ["factory_planning", "orders", "legacy"]
        fake_db.list_graphs.assert_called_once()
        # _get_db lazily connected without selecting a graph handle.
        fake_db.select_graph.assert_not_called()


def test_list_graphs_does_not_disturb_cached_graph_handle():
    """Calling list_graphs (a DB-level command) must not drop an already-cached
    graph handle — the two caches are independent."""
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        fake_db.list_graphs.return_value = ["g1"]
        fake_graph = MagicMock()
        fake_db.select_graph.return_value = fake_graph
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g1")
        # Prime the graph handle (simulates a prior query).
        assert backend._get_graph() is fake_graph

        # Now list graphs.
        assert backend.list_graphs() == ["g1"]
        # Graph handle still cached (not invalidated by the DB-level call).
        assert backend._graph is fake_graph


# ---------------------------------------------------------------------------
# FalkorDBBackend._get_db (lazy connect)
# ---------------------------------------------------------------------------
def test_get_db_lazily_connects_and_drops_stale_graph_handle():
    """_get_db connects on first call. If a graph handle was cached against a
    dead client, it is dropped so _get_graph reselects on the fresh client."""
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g")
        assert backend._db is None

        # Simulate a stale graph handle left over from an invalidated conn.
        stale_graph = MagicMock()
        backend._graph = stale_graph

        db = backend._get_db()
        assert db is fake_db
        fake_ctor.assert_called_once_with(host="h", port=6379)
        # Stale handle dropped.
        assert backend._graph is None


# ---------------------------------------------------------------------------
# FalkorDBBackend.set_active_graph
# ---------------------------------------------------------------------------
def test_set_active_graph_switches_name_and_invalidates_handle():
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        graph1 = MagicMock()
        graph2 = MagicMock()
        # select_graph returns graph1 first, graph2 on the second selection.
        fake_db.select_graph.side_effect = [graph1, graph2]
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(
            host="h", port=6379, graph_name="g1",
            allowed_graphs=["g1", "g2"],
        )

        # Prime handle for g1.
        assert backend._get_graph() is graph1
        fake_db.select_graph.assert_called_once_with("g1")

        # Switch to g2.
        backend.set_active_graph("g2")
        assert backend.graph_name == "g2"
        assert backend._graph is None  # invalidated

        # Next _get_graph reselects on the SAME client.
        assert backend._get_graph() is graph2
        assert fake_ctor.call_count == 1  # no reconnect
        assert fake_db.select_graph.call_args_list[-1].args == ("g2",)


def test_set_active_graph_rejects_out_of_allowlist():
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend(
        host="h", port=6379, graph_name="g1",
        allowed_graphs=["g1", "g2"],
    )
    with pytest.raises(ValueError, match="not in the allowed set"):
        backend.set_active_graph("secret_graph")
    # Active graph unchanged.
    assert backend.graph_name == "g1"


def test_set_active_graph_allows_anything_when_unrestricted():
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        fake_db.select_graph.return_value = MagicMock()
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g1")
        assert backend.allowed_graphs is None

        backend.set_active_graph("anything")
        assert backend.graph_name == "anything"


def test_set_active_graph_rejects_empty_name():
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend(host="h", port=6379, graph_name="g1")
    with pytest.raises(ValueError, match="non-empty"):
        backend.set_active_graph("")


def test_set_active_graph_noop_when_already_active_and_handle_live():
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        fake_graph = MagicMock()
        fake_db.select_graph.return_value = fake_graph
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g1")
        backend._get_graph()  # prime handle

        backend.set_active_graph("g1")
        # Handle NOT invalidated (no-op).
        assert backend._graph is fake_graph
        assert backend.graph_name == "g1"


def test_allowed_graphs_property_returns_copy():
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend(
        host="h", port=6379, graph_name="g1",
        allowed_graphs=["g1", "g2"],
    )
    snapshot = backend.allowed_graphs
    assert snapshot == ["g1", "g2"]
    snapshot.append("g3")
    # Mutating the returned list must not affect the backend's allowlist.
    assert backend.allowed_graphs == ["g1", "g2"]


# ---------------------------------------------------------------------------
# FalkorDBBackend.create_graph
# ---------------------------------------------------------------------------
def test_create_graph_materializes_new_graph():
    """create_graph() selects the new name on the DB client and runs a write
    so FalkorDB registers the graph in GRAPH.LIST, then lists it."""
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        fake_graph = MagicMock()
        # list_graphs() returns empty first, then the new graph after creation.
        fake_db.list_graphs.side_effect = [[], ["new_kg"]]
        fake_db.select_graph.return_value = fake_graph
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="factory_planning")
        backend.create_graph("new_kg")

        # Selected the new graph on the DB client and ran a write query.
        fake_db.select_graph.assert_called_with("new_kg")
        assert fake_graph.query.call_count == 1
        query, params = fake_graph.query.call_args.args
        assert "CREATE" in query and "DELETE" in query
        assert "created_at" in params
        # The new graph now appears in list_graphs.
        assert "new_kg" in backend.list_graphs()


def test_create_graph_does_not_disturb_cached_graph_handle():
    """create_graph() must not invalidate an already-cached active-graph
    handle — it uses the DB-level client, not the bound graph handle."""
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        active_graph = MagicMock()
        new_graph = MagicMock()
        fake_db.list_graphs.return_value = ["factory_planning"]
        fake_db.select_graph.side_effect = [active_graph, new_graph]
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="factory_planning")
        # Prime the active-graph handle.
        assert backend._get_graph() is active_graph

        backend.create_graph("new_kg")

        # Active-graph handle still cached (not invalidated by create_graph).
        assert backend._graph is active_graph
        # Active graph name unchanged.
        assert backend.graph_name == "factory_planning"


def test_create_graph_adds_to_allowed_graphs():
    """When an allowlist is configured, create_graph() appends the new name so
    use_graph can subsequently target it."""
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        fake_db.list_graphs.return_value = []
        fake_db.select_graph.return_value = MagicMock()
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(
            host="h", port=6379, graph_name="g1",
            allowed_graphs=["g1", "g2"],
        )
        backend.create_graph("g3")

        assert backend.allowed_graphs == ["g1", "g2", "g3"]


def test_create_graph_does_not_add_to_unrestricted_allowlist():
    """When no allowlist is configured (unrestricted), create_graph() leaves
    allowed_graphs as None."""
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        fake_db.list_graphs.return_value = []
        fake_db.select_graph.return_value = MagicMock()
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="g1")
        assert backend.allowed_graphs is None

        backend.create_graph("new_kg")
        assert backend.allowed_graphs is None


def test_create_graph_rejects_empty_name():
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend(host="h", port=6379, graph_name="g1")
    with pytest.raises(ValueError, match="non-empty"):
        backend.create_graph("")


def test_create_graph_rejects_non_string_name():
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend(host="h", port=6379, graph_name="g1")
    with pytest.raises(ValueError, match="non-empty"):
        backend.create_graph(None)  # type: ignore[arg-type]


def test_create_graph_rejects_existing_name():
    """create_graph() must not clobber an existing graph — a name already in
    GRAPH.LIST is rejected with a ValueError pointing to set_active_graph."""
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        fake_db.list_graphs.return_value = ["existing_kg"]
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="factory_planning")
        with pytest.raises(ValueError, match="already exists"):
            backend.create_graph("existing_kg")

        # No write was attempted on the existing graph.
        fake_db.select_graph.assert_not_called()


def test_create_graph_does_not_switch_active_graph():
    """create_graph() creates but does NOT switch the active graph — the
    caller is expected to follow with set_active_graph (or rely on the UI
    rebuild)."""
    from knowledge.falkordb_backend import FalkorDBBackend

    with patch("knowledge.falkordb_backend.FalkorDB", autospec=True) as fake_ctor:
        fake_db = MagicMock()
        fake_db.list_graphs.return_value = []
        fake_db.select_graph.return_value = MagicMock()
        fake_ctor.return_value = fake_db

        backend = FalkorDBBackend(host="h", port=6379, graph_name="factory_planning")
        backend.create_graph("new_kg")

        assert backend.graph_name == "factory_planning"


# ---------------------------------------------------------------------------
# use_graph tool
# ---------------------------------------------------------------------------
def test_use_graph_tool_allowed_switch():
    from falkordb_harness.tools.admin_tools import _use_graph_impl

    backend = MagicMock()
    backend.graph_name = "g2"
    backend.allowed_graphs = ["g1", "g2"]

    with patch("falkordb_harness.tools.admin_tools.get_backend", return_value=backend):
        out = _use_graph_impl("g2")
    payload = json.loads(out)
    assert payload == {"active_graph": "g2", "allowed_graphs": ["g1", "g2"]}
    backend.set_active_graph.assert_called_once_with("g2")


def test_use_graph_tool_disallowed_returns_error_and_keeps_active():
    from falkordb_harness.tools.admin_tools import _use_graph_impl

    backend = MagicMock()
    backend.graph_name = "g1"  # current active, must remain
    backend.allowed_graphs = ["g1", "g2"]
    backend.set_active_graph.side_effect = ValueError(
        "Graph 'secret' is not in the allowed set (['g1', 'g2'])."
    )

    with patch("falkordb_harness.tools.admin_tools.get_backend", return_value=backend):
        out = _use_graph_impl("secret")

    payload = json.loads(out)
    assert payload["error_type"] == "ValueError"
    assert "not in the allowed set" in payload["error"]
    assert payload["active_graph"] == "g1"
    # set_active_graph was attempted (and raised), but the backend's active
    # graph is unchanged because the validation rejected it.
    backend.set_active_graph.assert_called_once_with("secret")


# ---------------------------------------------------------------------------
# list_graphs tool
# ---------------------------------------------------------------------------
def test_list_graphs_tool_surfaces_names():
    from falkordb_harness.tools.inspect_tools import _list_graphs_impl

    backend = MagicMock()
    backend.list_graphs.return_value = ["a", "b", "c"]

    with patch("falkordb_harness.tools.inspect_tools.get_backend", return_value=backend):
        out = _list_graphs_impl()
    assert json.loads(out) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Per-session contextvar backend
# ---------------------------------------------------------------------------
def test_session_backend_takes_precedence_over_module_cache(monkeypatch):
    import falkordb_harness.backend as harness_backend

    harness_backend.reset_backend_cache()
    harness_backend.clear_session_backend()

    session_backend = MagicMock(name="session")
    harness_backend.set_session_backend(session_backend)

    # Even if the module-level cache were populated, the session one wins.
    monkeypatch.setattr(harness_backend, "FalkorDBBackend", lambda *a, **k: MagicMock(name="module"))

    assert harness_backend.get_backend() is session_backend

    harness_backend.clear_session_backend()


def test_clear_session_backend_falls_back_to_module_cache(monkeypatch):
    import falkordb_harness.backend as harness_backend

    harness_backend.reset_backend_cache()
    harness_backend.clear_session_backend()

    session_backend = MagicMock(name="session")
    module_backend = MagicMock(name="module")
    harness_backend.set_session_backend(session_backend)
    monkeypatch.setattr(harness_backend, "FalkorDBBackend", lambda *a, **k: module_backend)

    assert harness_backend.get_backend() is session_backend
    harness_backend.clear_session_backend()
    assert harness_backend.get_backend() is module_backend

    harness_backend.reset_backend_cache()


def test_session_searcher_built_from_session_backend():
    import falkordb_harness.backend as harness_backend

    harness_backend.clear_session_backend()
    session_backend = MagicMock(name="session_backend")
    harness_backend.set_session_backend(session_backend)

    with patch("falkordb_harness.backend.GraphSearcher") as fake_searcher_cls:
        fake_searcher = MagicMock()
        fake_searcher_cls.return_value = fake_searcher

        s1 = harness_backend.get_searcher()
        assert s1 is fake_searcher
        fake_searcher_cls.assert_called_once()
        # Second call reuses the cached session searcher.
        s2 = harness_backend.get_searcher()
        assert s2 is fake_searcher
        assert fake_searcher_cls.call_count == 1

    harness_backend.clear_session_backend()


def test_session_backend_isolated_across_contextvars():
    """Two contexts (simulating two Chainlit sessions) see different backends."""
    import contextvars

    import falkordb_harness.backend as harness_backend

    harness_backend.clear_session_backend()

    b_a = MagicMock(name="A")
    b_b = MagicMock(name="B")

    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()

    def setup_a():
        harness_backend.set_session_backend(b_a)
        assert harness_backend.get_backend() is b_a

    def setup_b():
        harness_backend.set_session_backend(b_b)
        assert harness_backend.get_backend() is b_b

    ctx_a.run(setup_a)
    ctx_b.run(setup_b)

    # Neither leaks into the other (contextvars are isolated).
    # In this outer context nothing was set.
    harness_backend.clear_session_backend()


# ---------------------------------------------------------------------------
# Chainlit settings-dict -> configurable normalization
# ---------------------------------------------------------------------------
def test_normalize_selection_defaults_when_empty():
    from falkordb_harness.chainlit_app import _normalize_selection

    active, allowed = _normalize_selection(None, None)
    assert active == "factory_planning"
    assert allowed == ["factory_planning"]


def test_normalize_selection_inserts_active_into_allowed():
    from falkordb_harness.chainlit_app import _normalize_selection

    active, allowed = _normalize_selection("g1", ["g2", "g3"])
    assert active == "g1"
    assert allowed == ["g1", "g2", "g3"]


def test_normalize_selection_keeps_active_when_already_allowed():
    from falkordb_harness.chainlit_app import _normalize_selection

    active, allowed = _normalize_selection("g2", ["g1", "g2", "g3"])
    assert active == "g2"
    assert allowed == ["g1", "g2", "g3"]


def test_normalize_selection_empty_allowed_becomes_active_only():
    from falkordb_harness.chainlit_app import _normalize_selection

    active, allowed = _normalize_selection("g1", [])
    assert active == "g1"
    assert allowed == ["g1"]


def test_graphs_unique_preserves_order():
    from falkordb_harness.chainlit_app import _graphs_unique

    assert _graphs_unique(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


# ---------------------------------------------------------------------------
# Agent builder: session backend installed from configurable
# ---------------------------------------------------------------------------
def test_build_agent_installs_session_backend_from_configurable():
    """build_agent with active_graph/allowed_graphs in the configurable must
    install a per-session backend bound to that graph."""
    from falkordb_harness import agent as agent_mod
    from falkordb_harness import backend as harness_backend

    harness_backend.clear_session_backend()

    captured: dict = {}
    fake_agent = MagicMock(name="agent")
    fake_agent.checkpointer = None  # avoid parent-graph compile validation

    def fake_build(model, tools, system_prompt, backend, **kwargs):  # noqa: ANN001
        captured["system_prompt"] = system_prompt
        return fake_agent

    with patch.object(agent_mod, "resolve_model", return_value=MagicMock()), \
         patch.object(agent_mod, "create_deep_agent", side_effect=fake_build), \
         patch.object(agent_mod, "FilesystemBackend", return_value=MagicMock()):
        agent_mod.build_agent(
            {"configurable": {"active_graph": "g1", "allowed_graphs": ["g1", "g2"]}}
        )

    # Session backend installed and bound to g1.
    session_backend = harness_backend.get_backend()
    assert session_backend.graph_name == "g1"
    assert session_backend.allowed_graphs == ["g1", "g2"]

    # System prompt got the graph-context preamble.
    assert "KNOWLEDGE GRAPH SELECTION" in captured["system_prompt"]
    assert "'g1'" in captured["system_prompt"]
    assert "use_graph" in captured["system_prompt"]

    harness_backend.clear_session_backend()


def test_build_agent_without_graph_config_uses_module_backend():
    """build_agent with no graph configurable must NOT install a session
    backend — the module-level env-driven cache is used instead (CLI path)."""
    from falkordb_harness import agent as agent_mod
    from falkordb_harness import backend as harness_backend

    harness_backend.clear_session_backend()
    harness_backend.reset_backend_cache()

    fake_agent = MagicMock(name="agent")
    fake_agent.checkpointer = None

    with patch.object(agent_mod, "resolve_model", return_value=MagicMock()), \
         patch.object(agent_mod, "create_deep_agent", return_value=fake_agent), \
         patch.object(agent_mod, "FilesystemBackend", return_value=MagicMock()):
        agent_mod.build_agent()

    # No session backend installed.
    assert harness_backend._SESSION_BACKEND.get() is None

    harness_backend.reset_backend_cache()


# ---------------------------------------------------------------------------
# Agent builder: per-session thread id surfaced in the prompt preamble
# ---------------------------------------------------------------------------
def test_build_agent_preamble_surfaces_thread_id():
    """build_agent with thread_id in the configurable must include the session
    id in the prompt preamble so the agent knows its on-disk subdirectory."""
    from falkordb_harness import agent as agent_mod
    from falkordb_harness import backend as harness_backend

    harness_backend.clear_session_backend()

    captured: dict = {}
    fake_agent = MagicMock(name="agent")
    fake_agent.checkpointer = None

    def fake_build(model, tools, system_prompt, backend, **kwargs):  # noqa: ANN001
        captured["system_prompt"] = system_prompt
        return fake_agent

    with patch.object(agent_mod, "resolve_model", return_value=MagicMock()), \
         patch.object(agent_mod, "create_deep_agent", side_effect=fake_build), \
         patch.object(agent_mod, "FilesystemBackend", return_value=MagicMock()):
        agent_mod.build_agent(
            {
                "configurable": {
                    "active_graph": "g1",
                    "allowed_graphs": ["g1"],
                    "thread_id": "thread-abc-123",
                }
            }
        )

    assert "SESSION FILE ISOLATION" in captured["system_prompt"]
    assert "Your current session id is 'thread-abc-123'" in captured["system_prompt"]
    assert "originals/thread-abc-123/" in captured["system_prompt"]

    harness_backend.clear_session_backend()


def test_build_agent_preamble_thread_id_none_refers_to_unscoped():
    """build_agent with thread_id=None (CLI) must tell the agent that
    _unscoped/ files belong to no session."""
    from falkordb_harness import agent as agent_mod
    from falkordb_harness import backend as harness_backend

    harness_backend.clear_session_backend()
    harness_backend.reset_backend_cache()

    captured: dict = {}
    fake_agent = MagicMock(name="agent")
    fake_agent.checkpointer = None

    def fake_build(model, tools, system_prompt, backend, **kwargs):  # noqa: ANN001
        captured["system_prompt"] = system_prompt
        return fake_agent

    with patch.object(agent_mod, "resolve_model", return_value=MagicMock()), \
         patch.object(agent_mod, "create_deep_agent", side_effect=fake_build), \
         patch.object(agent_mod, "FilesystemBackend", return_value=MagicMock()):
        agent_mod.build_agent(
            {"configurable": {"active_graph": "g1", "allowed_graphs": ["g1"], "thread_id": None}}
        )

    assert "No session id is set" in captured["system_prompt"]
    assert "originals/_unscoped/" in captured["system_prompt"]

    harness_backend.reset_backend_cache()


def test_build_graph_context_prefix_thread_id_only():
    """The preamble emits the session id even without a graph selection
    (e.g. CLI with a thread context but no graph pick)."""
    from falkordb_harness.agent import _build_graph_context_prefix

    prefix = _build_graph_context_prefix(None, None, thread_id="t-9")
    assert "Your current session id is 't-9'" in prefix
    assert "originals/t-9/" in prefix


def test_build_graph_context_prefix_no_context_empty():
    """No active graph, no allowed graphs, no thread id -> empty preamble
    (preserves the original CLI prompt unchanged)."""
    from falkordb_harness.agent import _build_graph_context_prefix

    assert _build_graph_context_prefix(None, None, thread_id=None) == ""