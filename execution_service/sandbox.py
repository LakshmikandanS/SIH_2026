"""The one real process boundary in this slice (design doc §2).

Every call creates exactly one throwaway container, runs the caller's code in
it, captures stdout/stderr/exit_code, and destroys the container -- never
reused, never pooled, never left running. Concretely, each container is
created with:

  * no Docker socket mounted (there is no mount at all, see below)
  * no host filesystem mount beyond an in-memory scratch tmpfs -- stronger
    than the doc's floor: not even that tmpfs touches the host disk
  * `network_disabled=True`: no network interface exists inside the
    container at all (not "no route to the internet" -- no network stack to
    route from), which trivially satisfies "no network route to Postgres,
    the vector store, or the internet"
  * a memory cap, a CPU cap, a pids cap, and a wall-clock timeout enforced by
    this module (Docker does not enforce wall-clock time on its own)
  * `remove(force=True)` in a `finally`, always, whether the run succeeded,
    crashed, or timed out

This module is the ONLY place in the entire system that imports the `docker`
package. `app/` (the trusted workflow zone) must never import it -- see
`tests/test_security.py::test_execution_zone_has_no_code_path_to_the_docker_socket`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import docker
import requests
from docker.errors import DockerException

from execution_service import settings


class SandboxError(Exception):
    """A real infrastructure failure: timeout, a resource-limit violation
    (e.g. OOM kill), or the container/daemon crashing outright.

    Deliberately NOT raised for a nonzero exit code from the caller's own
    code -- `python -c "raise ValueError()"` is a normal, successful sandbox
    run that happens to report exit_code=1. Conflating the two would turn
    every buggy script into an EXECUTION_ERROR instead of a result the agent
    can read and react to.
    """


@dataclass(frozen=True)
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int


_client: Optional["docker.DockerClient"] = None


def _get_client() -> "docker.DockerClient":
    global _client
    if _client is None:
        try:
            if settings.DOCKER_BASE_URL:
                _client = docker.DockerClient(base_url=settings.DOCKER_BASE_URL)
            else:
                _client = docker.from_env()
        except DockerException as exc:
            raise SandboxError(f"cannot reach the Docker daemon: {exc}") from exc
    return _client


def run_python(code: str, *, timeout_seconds: Optional[int] = None) -> ExecutionResult:
    """Run `code` as `python -c <code>` inside one one-shot container.

    Passing the source as the container's command (rather than writing it to
    a mounted file) means there is nothing to mount at all -- the strongest
    available reading of "no host filesystem mount beyond a scratch
    input/output directory".

    Raises `SandboxError` for timeout, an OOM/resource-limit kill, or any
    Docker-level failure. A plain nonzero exit code from the caller's own
    code is returned normally in `ExecutionResult.exit_code`.
    """
    timeout = _clamp_timeout(timeout_seconds)
    client = _get_client()

    container = None
    try:
        container = client.containers.run(
            settings.SANDBOX_IMAGE,
            ["python", "-c", code],
            detach=True,
            network_disabled=True,  # design doc §2: no network route, period.
            mem_limit=settings.SANDBOX_MEMORY_LIMIT,
            memswap_limit=settings.SANDBOX_MEMORY_LIMIT,  # no swap escape hatch
            nano_cpus=int(settings.SANDBOX_CPU_LIMIT * 1_000_000_000),
            pids_limit=settings.SANDBOX_PIDS_LIMIT,
            read_only=True,
            tmpfs={"/tmp": settings.SANDBOX_TMPFS_SIZE},
            working_dir="/tmp",
            security_opt=["no-new-privileges"],
            cap_drop=["ALL"],
            # No `volumes=`, no `ports=`, no `privileged`: nothing beyond the
            # in-memory /tmp above is ever attached to this container.
            labels=CONTAINER_LABELS,
        )
    except DockerException as exc:
        raise SandboxError(f"could not start the sandbox container: {exc}") from exc

    try:
        try:
            status = container.wait(timeout=timeout)
        except requests.exceptions.RequestException as exc:
            # The Docker API call itself timed out client-side; the
            # container is still running server-side, so it must be killed
            # explicitly -- this is what "kill on timeout" means in practice.
            _force_kill(container)
            raise SandboxError(
                f"execution exceeded the {timeout}s time limit and was killed"
            ) from exc

        exit_code = _status_code(status)
        container.reload()
        oom_killed = bool(container.attrs.get("State", {}).get("OOMKilled"))
        if oom_killed:
            raise SandboxError(
                f"container was killed after exceeding its "
                f"{settings.SANDBOX_MEMORY_LIMIT} memory limit"
            )

        stdout = _decode(container.logs(stdout=True, stderr=False))
        stderr = _decode(container.logs(stdout=False, stderr=True))
        return ExecutionResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
    except DockerException as exc:
        raise SandboxError(f"sandbox container failed: {exc}") from exc
    finally:
        # Destroyed immediately after the call returns -- never reused,
        # regardless of how the run ended.
        _force_remove(container)


#: Stamped on every sandbox container. Two reasons, both practical: `docker ps`
#: stays legible when a run leaves something behind, and the leak assertion in
#: `tests/test_execution.py` can scope itself to containers *we* created rather
#: than diffing every container on the daemon -- which otherwise fails whenever
#: anything unrelated starts or stops mid-test.
CONTAINER_LABELS = {"citadel.role": "python-execute"}


def _clamp_timeout(requested: Optional[int]) -> int:
    if requested is None:
        return settings.SANDBOX_TIMEOUT_SECONDS
    return max(1, min(int(requested), settings.SANDBOX_MAX_TIMEOUT_SECONDS))


def _status_code(status: Any) -> int:
    if isinstance(status, dict):
        return int(status.get("StatusCode", -1))
    return int(status)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _force_kill(container) -> None:
    try:
        container.kill()
    except DockerException:
        pass  # already exited -- nothing to kill


def _force_remove(container) -> None:
    if container is None:
        return
    try:
        container.remove(force=True)
    except DockerException:
        pass  # already gone -- destruction was still achieved either way
