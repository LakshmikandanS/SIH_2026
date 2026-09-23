"""The in-process fence: a second, application-level enforcement layer.

The boundary that matters is the network one -- the sandbox and database sit on
internal-only networks with no route out, and the API and worker containers apply a
default-deny nftables ruleset in their own network namespace before the application
starts (ops/compose/egress-entrypoint.sh). This fence sits inside the Python process in
front of that: any `socket.connect` or name lookup that is not one of the deployment's
own destinations is refused before a packet is built, and the refusal is reported to the
recorder as `blocked` (or `dns_denied`).

Its code shares nothing with the monitor beyond the list of the deployment's own
destinations. A deliberate probe steps around it (`deliberate_probe()`), so the probe
tests the network boundary itself rather than this layer.
"""

from __future__ import annotations

import contextvars
import sys
from contextlib import contextmanager
from typing import Any, Iterator

from citadel_sovereignty.policy import EgressPolicy, is_ip
from citadel_sovereignty.telemetry import EgressRecorder

_probing: contextvars.ContextVar[bool] = contextvars.ContextVar("citadel_deliberate_probe", default=False)


class EgressBlocked(PermissionError):
    pass


@contextmanager
def deliberate_probe() -> Iterator[None]:
    token = _probing.set(True)
    try:
        yield
    finally:
        _probing.reset(token)


class EgressFence:
    def __init__(self, policy: EgressPolicy, recorder: EgressRecorder) -> None:
        self.policy = policy
        self.recorder = recorder
        self.enabled = True
        self.installed = False
        self.refusals = 0

    def install(self) -> None:
        if not self.installed:
            sys.addaudithook(self._hook)
            self.installed = True

    def _hook(self, event: str, args: tuple[Any, ...]) -> None:
        if not self.enabled or _probing.get():
            return
        if event == "socket.connect" and len(args) >= 2:
            address = args[1]
            if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[0], str):
                host, port = address[0], address[1] if isinstance(address[1], int) else None
                if not self.policy.address_allowed(host, port):
                    self.refusals += 1
                    self.recorder.record("blocked", "fence", host, port, {"event": event})
                    raise EgressBlocked(f"egress blocked by Citadel: {host}:{port} is not one of this deployment's destinations")
        elif event == "socket.getaddrinfo" and args and isinstance(args[0], (str, bytes)):
            name = args[0].decode("ascii", "replace") if isinstance(args[0], bytes) else args[0]
            if name and not is_ip(name) and not self.policy.name_allowed(name):
                self.refusals += 1
                port = args[1] if len(args) > 1 and isinstance(args[1], int) else None
                self.recorder.record("dns_denied", "fence", name, port, {"event": event})
                raise EgressBlocked(f"name lookup blocked by Citadel: {name} is not one of this deployment's names")


__all__ = ["EgressFence", "EgressBlocked", "deliberate_probe"]
