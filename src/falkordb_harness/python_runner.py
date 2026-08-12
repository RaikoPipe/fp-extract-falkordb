"""Docker-backed sandbox for Python code execution with pandas.

Provides a :class:`PythonRunnerSandbox` that implements
:class:`deepagents.backends.sandbox.BaseSandbox` by shelling out to
``docker exec`` against a per-thread ``python-runner-<thread_id>``
container.  The container image (``python-runner:latest``) ships pandas,
numpy, matplotlib, scipy, scikit-learn, and openpyxl preinstalled.

Thread-scoped lifecycle
-----------------------
One container per Chainlit thread, created lazily on the first
``execute`` / ``upload_files`` / ``download_files`` call and torn down
when the thread ends (``on_chat_end``) or after an idle TTL expires.
A module-level background reaper thread sweeps every 60 s and stops +
removes containers that have been idle longer than their TTL.

The host ``DATA_DIR`` is bind-mounted read-only at ``/workspace`` inside
the container so the agent's built-in ``ls`` / ``read_file`` / ``glob`` /
``grep`` tools (auto-built by ``BaseSandbox`` on top of ``execute``) see
the ``originals/`` and ``preprocessed/`` trees.  Writes go to
container-local paths (``/tmp/``, ``/large_tool_results/``, etc.).

Env vars
--------
``PYTHON_RUNNER_ENABLE``
    Set to ``"1"`` / ``"true"`` / ``"yes"`` to activate the sandbox
    backend in ``build_agent``.  Off by default.
``PYTHON_RUNNER_IMAGE``
    Docker image tag (default ``python-runner:latest``).
``PYTHON_RUNNER_TTL``
    Idle seconds before the reaper stops + removes the container
    (default 3600).
``PYTHON_RUNNER_NETWORK``
    Docker network mode for the container.  Default ``"none"`` (no
    network).  Set to ``"bridge"`` or a named network to allow
    ``pip install`` and outbound HTTP from the sandbox.
``PYTHON_RUNNER_DEFAULT_TIMEOUT``
    Default ``execute`` timeout in seconds (default 120).
``PYTHON_RUNNER_MAX_OUTPUT_BYTES``
    Max bytes of combined stdout+stderr returned inline from
    ``execute`` (default 100 000).  Output beyond this is truncated
    and ``ExecuteResponse.truncated`` is set to ``True``.
"""

from __future__ import annotations

import base64
import logging
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import ClassVar

from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

logger = logging.getLogger(__name__)

_DOCKER_IMPORT_ERROR = (
    "PythonRunnerSandbox requires the `docker` Python SDK. "
    "Install with: pip install -e \".[runner]\""
)

_DEFAULT_IMAGE = "python-runner:latest"
_DEFAULT_TTL = 3600
_DEFAULT_TIMEOUT = 120
_DEFAULT_MAX_OUTPUT_BYTES = 100_000
_REAPER_INTERVAL = 60

# ---------------------------------------------------------------------------
# Module-level container registry + background reaper
# ---------------------------------------------------------------------------

_registry: dict[str, PythonRunnerSandbox] = {}
_registry_lock = threading.Lock()
_reaper_started = False


def _ensure_reaper() -> None:
    """Start the background TTL reaper thread once per process."""
    global _reaper_started  # noqa: PLW0603
    if _reaper_started:
        return
    _reaper_started = True
    t = threading.Thread(target=_reap_loop, name="python-runner-reaper", daemon=True)
    t.start()


def _reap_loop() -> None:
    """Periodically stop+remove idle containers."""
    while True:
        time.sleep(_REAPER_INTERVAL)
        _reap_idle()


def _reap_idle() -> None:
    now = time.monotonic()
    with _registry_lock:
        idle = [
            (name, sb)
            for name, sb in _registry.items()
            if (now - sb._last_activity) > sb._ttl_seconds  # noqa: SLF001
        ]
    for name, sb in idle:
        logger.info("TTL expired for %s (idle %.0f s); cleaning up", name, now - sb._last_activity)  # noqa: SLF001
        try:
            sb.cleanup()
        except Exception:
            logger.exception("Reaper failed to clean up %s", name)


# ---------------------------------------------------------------------------
# PythonRunnerSandbox
# ---------------------------------------------------------------------------


class PythonRunnerSandbox(BaseSandbox):
    """A :class:`BaseSandbox` that runs commands inside a local Docker container.

    The container is created lazily on first use and torn down via
    :meth:`cleanup` (called on thread end) or by the background TTL reaper.
    """

    # Per-instance defaults (overridable via kwargs).
    _default_timeout: ClassVar[int] = _DEFAULT_TIMEOUT
    _max_output_bytes: ClassVar[int] = _DEFAULT_MAX_OUTPUT_BYTES

    def __init__(
        self,
        thread_id: str,
        *,
        image: str | None = None,
        data_dir: Path | str | None = None,
        ttl_seconds: int | None = None,
        network: str | None = None,
        default_timeout: int | None = None,
        max_output_bytes: int | None = None,
    ) -> None:
        self._thread_id = thread_id
        self._container_name = f"python-runner-{thread_id}"
        self._image = image or os.getenv("PYTHON_RUNNER_IMAGE", _DEFAULT_IMAGE)
        self._ttl_seconds = (
            ttl_seconds
            if ttl_seconds is not None
            else int(os.getenv("PYTHON_RUNNER_TTL", str(_DEFAULT_TTL)))
        )
        self._network = network if network is not None else os.getenv("PYTHON_RUNNER_NETWORK") or "none"
        self._default_timeout = (
            default_timeout
            if default_timeout is not None
            else int(os.getenv("PYTHON_RUNNER_DEFAULT_TIMEOUT", str(_DEFAULT_TIMEOUT)))
        )
        self._max_output_bytes = (
            max_output_bytes
            if max_output_bytes is not None
            else int(os.getenv("PYTHON_RUNNER_MAX_OUTPUT_BYTES", str(_DEFAULT_MAX_OUTPUT_BYTES)))
        )

        if data_dir is not None:
            self._data_dir = Path(data_dir).resolve()
        else:
            self._data_dir = None

        self._last_activity: float = time.monotonic()
        self._container_id: str | None = None
        self._cleaned_up = False

        with _registry_lock:
            _registry[self._container_name] = self
        _ensure_reaper()

    # ------------------------------------------------------------------
    # Abstract methods required by BaseSandbox
    # ------------------------------------------------------------------

    @property
    def id(self) -> str:  # noqa: A003
        return self._container_name

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        self._ensure_container()
        self._touch()
        effective_timeout = timeout if timeout is not None else self._default_timeout
        try:
            result = subprocess.run(
                [
                    "docker", "exec",
                    "-w", "/workspace",
                    self._container_name,
                    "sh", "-c", command,
                ],
                capture_output=True,
                timeout=effective_timeout,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Command timed out after {effective_timeout}s",
                exit_code=-1,
                truncated=False,
            )
        except FileNotFoundError:
            return ExecuteResponse(
                output="docker CLI not found — is Docker installed and on PATH?",
                exit_code=-1,
                truncated=False,
            )
        except OSError as exc:
            return ExecuteResponse(
                output=f"Failed to run docker exec: {exc}",
                exit_code=-1,
                truncated=False,
            )

        combined = result.stdout + result.stderr
        truncated = len(combined.encode("utf-8")) > self._max_output_bytes
        if truncated:
            combined = combined[:self._max_output_bytes] + "\n... [output truncated]"
        return ExecuteResponse(
            output=combined,
            exit_code=result.returncode,
            truncated=truncated,
        )

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        self._ensure_container()
        self._touch()
        responses: list[FileUploadResponse] = []
        for path, content in files:
            b64 = base64.b64encode(content).decode("ascii")
            parent = shlex.quote(str(Path(path).parent))
            target = shlex.quote(path)
            cmd = f"mkdir -p {parent} && echo {shlex.quote(b64)} | base64 -d > {target}"
            try:
                result = subprocess.run(
                    [
                        "docker", "exec",
                        "-w", "/workspace",
                        self._container_name,
                        "sh", "-c", cmd,
                    ],
                    capture_output=True,
                    timeout=30,
                    text=True,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                responses.append(FileUploadResponse(path=path, error=str(exc)))
                continue
            if result.returncode == 0:
                responses.append(FileUploadResponse(path=path, error=None))
            else:
                responses.append(
                    FileUploadResponse(
                        path=path,
                        error=result.stderr.strip() or "upload failed",
                    )
                )
        return responses

    def download_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        self._ensure_container()
        self._touch()
        responses: list[FileDownloadResponse] = []
        for path in paths:
            quoted = shlex.quote(path)
            cmd = f"test -f {quoted} && base64 -w0 {quoted} || echo __NOT_FOUND__"
            try:
                result = subprocess.run(
                    [
                        "docker", "exec",
                        "-w", "/workspace",
                        self._container_name,
                        "sh", "-c", cmd,
                    ],
                    capture_output=True,
                    timeout=30,
                    text=True,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                responses.append(
                    FileDownloadResponse(path=path, content=None, error=str(exc))
                )
                continue
            stdout = result.stdout.strip()
            if result.returncode != 0 or stdout == "__NOT_FOUND__":
                responses.append(
                    FileDownloadResponse(path=path, content=None, error=FILE_NOT_FOUND)
                )
                continue
            try:
                content = base64.b64decode(stdout)
            except Exception:
                responses.append(
                    FileDownloadResponse(path=path, content=None, error="base64 decode failed")
                )
                continue
            responses.append(FileDownloadResponse(path=path, content=content, error=None))
        return responses

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_container(self) -> None:
        """Create or resume the Docker container (lazy, idempotent)."""
        if self._cleaned_up:
            raise RuntimeError(
                f"Sandbox {self._container_name} has been cleaned up"
            )
        if self._container_id is not None:
            return

        try:
            import docker
        except ImportError:
            raise ImportError(_DOCKER_IMPORT_ERROR) from None

        client = docker.from_env()
        try:
            container = client.containers.get(self._container_name)
        except docker.errors.NotFound:
            logger.info("Creating container %s (image=%s)", self._container_name, self._image)
            volumes: dict[str, dict[str, str]] = {}
            if self._data_dir is not None:
                if not self._data_dir.exists():
                    self._data_dir.mkdir(parents=True, exist_ok=True)
                volumes[str(self._data_dir)] = {"bind": "/workspace", "mode": "ro"}
            network_mode: str | None = self._network if self._network != "none" else None
            container = client.containers.create(
                self._image,
                name=self._container_name,
                tty=True,
                stdin_open=True,
                volumes=volumes,
                working_dir="/workspace",
                network_mode=network_mode,
                network_disabled=(self._network == "none"),
            )
        else:
            if container.status != "running":
                logger.info("Starting existing container %s", self._container_name)
                container.start()

        self._container_id = container.id
        logger.info("Container %s ready (id=%s)", self._container_name, self._container_id[:12])

    def _touch(self) -> None:
        """Update the last-activity timestamp (called before each operation)."""
        self._last_activity = time.monotonic()

    def cleanup(self) -> None:
        """Stop and remove the Docker container.

        Idempotent — safe to call multiple times.  Deregisters from the
        module-level reaper registry.
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True

        with _registry_lock:
            _registry.pop(self._container_name, None)

        if self._container_id is None:
            return

        import docker

        client = docker.from_env()
        try:
            container = client.containers.get(self._container_name)
        except docker.errors.NotFound:
            logger.info("Container %s already gone", self._container_name)
            return

        try:
            container.stop(timeout=5)
            logger.info("Stopped container %s", self._container_name)
        except docker.errors.APIError:
            logger.exception("Failed to stop container %s", self._container_name)

        try:
            container.remove(force=True)
            logger.info("Removed container %s", self._container_name)
        except docker.errors.APIError:
            logger.exception("Failed to remove container %s", self._container_name)
