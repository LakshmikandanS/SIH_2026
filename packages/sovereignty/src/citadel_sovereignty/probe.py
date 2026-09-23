"""The deliberate probe: try to leave, on purpose, and show what happened.

The probe steps around the in-process fence (so it tests the network boundary, not the
Python one), attempts a TCP connection to a public address and a lookup of a public
name, and records the outcome with attribution. The expected result is that both fail,
the monitor records the attempt, the counters move -- and whatever task was running
carries on, because nothing about the product depends on the network being there.

A probe that *connects* is recorded as `observed` and reported as a failure of the
sovereignty claim on this machine, in those words. Hiding it would make the panel
worthless on the one occasion it matters.
"""

from __future__ import annotations

import socket
import time
from typing import Any, Optional, Sequence

from citadel_sovereignty.fence import deliberate_probe
from citadel_sovereignty.telemetry import EgressRecorder

#: A public resolver's address (no lookup needed) and a public name (lookup needed).
DEFAULT_TARGETS: tuple[tuple[str, int], ...] = (("1.1.1.1", 443), ("example.com", 443))


def run_probe(
    recorder: EgressRecorder,
    *,
    targets: Sequence[tuple[str, int]] = DEFAULT_TARGETS,
    timeout_s: float = 3.0,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    with deliberate_probe():
        for host, port in targets:
            started = time.perf_counter()
            outcome, error = "connected", None
            try:
                connection = socket.create_connection((host, port), timeout=timeout_s)
                connection.close()
            except socket.gaierror as exc:
                outcome, error = "dns_denied", f"name lookup failed: {exc}"
            except socket.timeout:
                outcome, error = "blocked", f"no response within {timeout_s:g}s (dropped)"
            except OSError as exc:
                outcome, error = "blocked", f"{type(exc).__name__}: {exc}"
            elapsed = int((time.perf_counter() - started) * 1000)
            kind = {"connected": "observed"}.get(outcome, outcome)
            recorder.record(
                kind, "probe", host, port,
                {"outcome": outcome, "error": error, "elapsed_ms": elapsed, "deliberate": True},
                task_id=task_id, agent_id=agent_id,
            )
            results.append({"target": f"{host}:{port}", "outcome": outcome, "error": error, "elapsed_ms": elapsed})
    escaped = [r for r in results if r["outcome"] == "connected"]
    return {
        "process": recorder.process,
        "results": results,
        "egress_blocked": not escaped,
        "verdict": (
            "every outbound attempt was stopped at the network boundary"
            if not escaped
            else f"EGRESS IS NOT BLOCKED from {recorder.process}: {', '.join(r['target'] for r in escaped)} connected"
        ),
    }


__all__ = ["run_probe", "DEFAULT_TARGETS"]
