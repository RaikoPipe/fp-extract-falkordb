"""Tests for PythonRunnerSandbox — Docker-backed code execution backend.

All tests mock ``docker.from_env()`` and ``subprocess.run`` — no live
Docker daemon is required.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness.python_runner import (
    PythonRunnerSandbox,
    _registry,
    _registry_lock,
)
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_container(cid: str = "abc123") -> MagicMock:
    c = MagicMock()
    c.id = cid
    c.status = "running"
    return c


def _make_fake_client(container: MagicMock | None = None) -> MagicMock:
    client = MagicMock()
    client.containers.get.return_value = container or _make_fake_container()
    client.containers.create.return_value = container or _make_fake_container()
    return client


def _make_fake_docker_module(client: MagicMock | None = None) -> MagicMock:
    """Return a MagicMock that quacks like the `docker` package."""
    mod = MagicMock(name="docker")
    mod.from_env.return_value = client or _make_fake_client()
    mod.errors.NotFound = type("NotFound", (Exception,), {})
    mod.errors.APIError = type("APIError", (Exception,), {})
    return mod


# ---------------------------------------------------------------------------
# Construction (lazy — no container created in __init__)
# ---------------------------------------------------------------------------


def test_construction_does_not_create_container():
    """__init__ must NOT call docker.from_env(); container creation is lazy."""
    fake_docker = _make_fake_docker_module()
    with patch.dict(sys.modules, {"docker": fake_docker}):
        sb = PythonRunnerSandbox(thread_id="test-lazy")
        fake_docker.from_env.assert_not_called()
        assert sb._container_id is None
        assert sb._container_name == "python-runner-test-lazy"


def test_construction_registers_in_registry():
    fake_docker = _make_fake_docker_module()
    with patch.dict(sys.modules, {"docker": fake_docker}):
        sb = PythonRunnerSandbox(thread_id="test-reg")
        with _registry_lock:
            assert _registry.get("python-runner-test-reg") is sb
        with _registry_lock:
            _registry.pop("python-runner-test-reg", None)


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def test_execute_creates_container_lazily():
    """First execute() call triggers _ensure_container -> docker.from_env()."""
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(
            stdout="hello\n", stderr="", returncode=0
        )

        sb = PythonRunnerSandbox(thread_id="test-exec")
        result = sb.execute("echo hello")

        fake_docker.from_env.assert_called_once()
        fake_client.containers.get.assert_called_once_with("python-runner-test-exec")
        assert isinstance(result, ExecuteResponse)
        assert result.exit_code == 0
        assert "hello" in result.output
        assert result.truncated is False

    with _registry_lock:
        _registry.pop("python-runner-test-exec", None)


def test_execute_reuses_existing_container():
    """Second execute() reuses the container, no second docker.from_env()."""
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(
            stdout="ok\n", stderr="", returncode=0
        )

        sb = PythonRunnerSandbox(thread_id="test-reuse")
        sb.execute("cmd1")
        sb.execute("cmd2")

        assert fake_docker.from_env.call_count == 1
        assert fake_client.containers.get.call_count == 1

    with _registry_lock:
        _registry.pop("python-runner-test-reuse", None)


def test_execute_creates_container_when_not_found():
    """When container doesn't exist, create it."""
    fake_container = _make_fake_container()
    fake_client = MagicMock()
    fake_docker = _make_fake_docker_module(fake_client)
    fake_client.containers.get.side_effect = fake_docker.errors.NotFound()
    fake_client.containers.create.return_value = fake_container

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(
            stdout="ok\n", stderr="", returncode=0
        )

        sb = PythonRunnerSandbox(thread_id="test-create")
        sb.execute("cmd")

        fake_client.containers.get.assert_called_once()
        fake_client.containers.create.assert_called_once()

    with _registry_lock:
        _registry.pop("python-runner-test-create", None)


def test_execute_returns_error_on_timeout():
    """subprocess.TimeoutExpired -> ExecuteResponse with error output."""
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
        sb = PythonRunnerSandbox(thread_id="test-timeout")
        result = sb.execute("sleep 999", timeout=5)

        assert result.exit_code == -1
        assert "timed out" in result.output.lower()

    with _registry_lock:
        _registry.pop("python-runner-test-timeout", None)


def test_execute_returns_error_when_docker_missing():
    """FileNotFoundError from subprocess -> ExecuteResponse with error."""
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run", side_effect=FileNotFoundError("docker")):
        sb = PythonRunnerSandbox(thread_id="test-nodocker")
        result = sb.execute("cmd")

        assert result.exit_code == -1
        assert "docker" in result.output.lower()

    with _registry_lock:
        _registry.pop("python-runner-test-nodocker", None)


def test_execute_truncates_large_output():
    """Output exceeding max_output_bytes is truncated."""
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(
            stdout="x" * 200, stderr="", returncode=0
        )

        sb = PythonRunnerSandbox(thread_id="test-trunc", max_output_bytes=100)
        result = sb.execute("cmd")

        assert result.truncated is True
        assert "[output truncated]" in result.output
        assert len(result.output.encode("utf-8")) <= 100 + len("\n... [output truncated]")

    with _registry_lock:
        _registry.pop("python-runner-test-trunc", None)


# ---------------------------------------------------------------------------
# upload_files / download_files
# ---------------------------------------------------------------------------


def test_upload_files_success():
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(stdout="", stderr="", returncode=0)

        sb = PythonRunnerSandbox(thread_id="test-upload")
        results = sb.upload_files([("/tmp/test.py", b"print(1)")])

        assert len(results) == 1
        assert results[0].path == "/tmp/test.py"
        assert results[0].error is None

    with _registry_lock:
        _registry.pop("python-runner-test-upload", None)


def test_upload_files_error():
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(
            stdout="", stderr="permission denied", returncode=1
        )

        sb = PythonRunnerSandbox(thread_id="test-upload-err")
        results = sb.upload_files([("/readonly/x.py", b"x")])

        assert len(results) == 1
        assert results[0].error is not None

    with _registry_lock:
        _registry.pop("python-runner-test-upload-err", None)


def test_download_files_success():
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    import base64
    content = b"hello world"
    b64 = base64.b64encode(content).decode("ascii")

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(
            stdout=b64 + "\n", stderr="", returncode=0
        )

        sb = PythonRunnerSandbox(thread_id="test-dl")
        results = sb.download_files(["/tmp/data.bin"])

        assert len(results) == 1
        assert results[0].path == "/tmp/data.bin"
        assert results[0].content == content
        assert results[0].error is None

    with _registry_lock:
        _registry.pop("python-runner-test-dl", None)


def test_download_files_not_found():
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(
            stdout="__NOT_FOUND__\n", stderr="", returncode=0
        )

        sb = PythonRunnerSandbox(thread_id="test-dl-nf")
        results = sb.download_files(["/tmp/missing.bin"])

        assert len(results) == 1
        assert results[0].content is None
        assert results[0].error == "file_not_found"

    with _registry_lock:
        _registry.pop("python-runner-test-dl-nf", None)


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def test_cleanup_stops_and_removes_container():
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(
            stdout="ok\n", stderr="", returncode=0
        )

        sb = PythonRunnerSandbox(thread_id="test-cleanup")
        sb.execute("cmd")
        sb.cleanup()

        fake_container.stop.assert_called_once()
        fake_container.remove.assert_called_once_with(force=True)
        assert sb._cleaned_up is True

    with _registry_lock:
        _registry.pop("python-runner-test-cleanup", None)


def test_cleanup_is_idempotent():
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(
            stdout="ok\n", stderr="", returncode=0
        )

        sb = PythonRunnerSandbox(thread_id="test-cleanup2")
        sb.execute("cmd")
        sb.cleanup()
        sb.cleanup()

        assert fake_container.stop.call_count == 1
        assert fake_container.remove.call_count == 1

    with _registry_lock:
        _registry.pop("python-runner-test-cleanup2", None)


def test_cleanup_handles_missing_container():
    """cleanup() is a no-op when the container is already gone."""
    fake_client = MagicMock()
    fake_docker = _make_fake_docker_module(fake_client)
    fake_client.containers.get.side_effect = fake_docker.errors.NotFound()

    with patch.dict(sys.modules, {"docker": fake_docker}):
        sb = PythonRunnerSandbox(thread_id="test-gone")
        sb._container_id = "abc123"
        sb.cleanup()

    with _registry_lock:
        _registry.pop("python-runner-test-gone", None)


def test_ensure_container_raises_after_cleanup():
    """_ensure_container raises RuntimeError after cleanup()."""
    fake_container = _make_fake_container()
    fake_client = _make_fake_client(fake_container)
    fake_docker = _make_fake_docker_module(fake_client)

    with patch.dict(sys.modules, {"docker": fake_docker}), \
         patch("subprocess.run") as fake_run:
        fake_run.return_value = MagicMock(
            stdout="ok\n", stderr="", returncode=0
        )

        sb = PythonRunnerSandbox(thread_id="test-post-cleanup")
        sb.execute("cmd")
        sb.cleanup()

        with pytest.raises(RuntimeError, match="cleaned up"):
            sb.execute("cmd2")

    with _registry_lock:
        _registry.pop("python-runner-test-post-cleanup", None)


# ---------------------------------------------------------------------------
# id property
# ---------------------------------------------------------------------------


def test_id_returns_container_name():
    fake_docker = _make_fake_docker_module()
    with patch.dict(sys.modules, {"docker": fake_docker}):
        sb = PythonRunnerSandbox(thread_id="test-id")
        assert sb.id == "python-runner-test-id"
    with _registry_lock:
        _registry.pop("python-runner-test-id", None)


# ---------------------------------------------------------------------------
# build_agent wiring toggle
# ---------------------------------------------------------------------------


def test_build_agent_uses_filesystem_backend_when_disabled():
    """When PYTHON_RUNNER_ENABLE is unset, build_agent uses FilesystemBackend."""
    from falkordb_harness import agent as agent_mod

    fake_agent = MagicMock(name="agent")
    fake_agent.checkpointer = None

    with patch.dict(os.environ, {}, clear=True), \
         patch.object(agent_mod, "resolve_model", return_value=MagicMock()), \
         patch.object(agent_mod, "create_deep_agent", return_value=fake_agent), \
         patch.object(agent_mod, "FilesystemBackend") as fake_fs:
        fake_fs.return_value = MagicMock(name="fs_backend")

        agent_mod.build_agent()

        fake_fs.assert_called_once()
        call_kwargs = agent_mod.create_deep_agent.call_args.kwargs
        assert call_kwargs["backend"] is fake_fs.return_value


def test_build_agent_uses_python_runner_when_enabled():
    """When PYTHON_RUNNER_ENABLE=1, build_agent uses PythonRunnerSandbox."""
    from falkordb_harness import agent as agent_mod

    fake_agent = MagicMock(name="agent")
    fake_agent.checkpointer = None
    fake_sandbox = MagicMock(name="sandbox")

    with patch.dict(os.environ, {"PYTHON_RUNNER_ENABLE": "1"}, clear=True), \
         patch.object(agent_mod, "resolve_model", return_value=MagicMock()), \
         patch.object(agent_mod, "create_deep_agent", return_value=fake_agent), \
         patch.object(agent_mod, "FilesystemBackend") as fake_fs, \
         patch("falkordb_harness.python_runner.PythonRunnerSandbox", return_value=fake_sandbox) as fake_sandbox_cls:
        agent_mod.build_agent()

        fake_fs.assert_not_called()
        fake_sandbox_cls.assert_called_once()
        call_kwargs = agent_mod.create_deep_agent.call_args.kwargs
        assert call_kwargs["backend"] is fake_sandbox


def test_build_agent_sets_module_sandbox():
    """build_agent sets the module-level _SANDBOX when enabled."""
    from falkordb_harness import agent as agent_mod

    fake_agent = MagicMock(name="agent")
    fake_agent.checkpointer = None
    fake_sandbox = MagicMock(name="sandbox")

    with patch.dict(os.environ, {"PYTHON_RUNNER_ENABLE": "1"}, clear=True), \
         patch.object(agent_mod, "resolve_model", return_value=MagicMock()), \
         patch.object(agent_mod, "create_deep_agent", return_value=fake_agent), \
         patch("falkordb_harness.python_runner.PythonRunnerSandbox", return_value=fake_sandbox):
        agent_mod.build_agent()

        assert agent_mod._SANDBOX is fake_sandbox
        assert agent_mod.get_sandbox() is fake_sandbox


def test_get_sandbox_returns_none_when_disabled():
    """get_sandbox() returns None when PYTHON_RUNNER_ENABLE is unset."""
    from falkordb_harness import agent as agent_mod

    fake_agent = MagicMock(name="agent")
    fake_agent.checkpointer = None

    with patch.dict(os.environ, {}, clear=True), \
         patch.object(agent_mod, "resolve_model", return_value=MagicMock()), \
         patch.object(agent_mod, "create_deep_agent", return_value=fake_agent), \
         patch.object(agent_mod, "FilesystemBackend", return_value=MagicMock()):
        agent_mod._SANDBOX = None
        agent_mod.build_agent()

        assert agent_mod.get_sandbox() is None
