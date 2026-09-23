"""Egress telemetry, the in-process fence, the deliberate probe and the reports.

Acceptance target E. Enforcement at the network level lives in ops/ (internal-only
Compose networks and a default-deny nftables ruleset applied in each container's own
network namespace); this package is the independent evidence that it works.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database

from citadel_sovereignty.fence import EgressBlocked, EgressFence, deliberate_probe
from citadel_sovereignty.policy import EgressPolicy
from citadel_sovereignty.probe import run_probe
from citadel_sovereignty.report import enforcement_status, status, task_report
from citadel_sovereignty.sandbox_client import HttpSandboxRunner, SandboxUnavailable
from citadel_sovereignty.telemetry import ConnectionScanner, EgressMonitor, EgressRecorder


@dataclass
class Sovereignty:
    """Everything one process runs: the recorder, the monitor, the scanner and the fence."""

    process: str
    policy: EgressPolicy
    recorder: EgressRecorder
    monitor: EgressMonitor
    scanner: ConnectionScanner
    fence: EgressFence

    def describe(self) -> dict[str, object]:
        return {
            "process": self.process,
            "monitor": "installed" if self.monitor.installed else "not installed",
            "fence": "installed" if self.fence.installed else "not installed",
            "fence_refusals": self.fence.refusals,
            "scanner": {"scans": self.scanner.scans, "last_scan": self.scanner.last_scan, "available": self.scanner.available},
            "recorded": self.recorder.counts(),
            "allowlist": self.policy.describe(),
            "enforcement": enforcement_status(),
        }


def install(
    process: str,
    db: Optional[Database],
    *,
    audit: Optional[AuditLog] = None,
    env: Optional[Mapping[str, str]] = None,
    extra_urls: tuple[str, ...] = (),
    fence: bool = True,
    scan: bool = True,
) -> Sovereignty:
    """Install the monitor first (so it sees every attempt), then the fence, and start
    the scanner. Call once per process, at startup, before any work is accepted."""
    policy = EgressPolicy.from_env(env, extra_urls=extra_urls)
    recorder = EgressRecorder(db, process, audit=audit)
    monitor = EgressMonitor(policy, recorder)
    monitor.install()
    guard = EgressFence(policy, recorder)
    if fence:
        guard.install()
    scanner = ConnectionScanner(policy, recorder)
    if scan:
        scanner.start()
    return Sovereignty(process, policy, recorder, monitor, scanner, guard)


__all__ = [
    "Sovereignty",
    "install",
    "EgressPolicy",
    "EgressRecorder",
    "EgressMonitor",
    "ConnectionScanner",
    "EgressFence",
    "EgressBlocked",
    "deliberate_probe",
    "run_probe",
    "status",
    "task_report",
    "enforcement_status",
    "HttpSandboxRunner",
    "SandboxUnavailable",
]
