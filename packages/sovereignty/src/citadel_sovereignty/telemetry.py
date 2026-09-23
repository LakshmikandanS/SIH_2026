"""The telemetry half: observe and record, never decide.

Three detectors, each independent of the enforcement that should make them read zero:

* `EgressMonitor` -- a `sys.addaudithook` observer. Every outbound `socket.connect` and
  every name lookup is compared with the deployment's own destinations; anything else
  is recorded as an `attempt`, attributed to the task and agent active at that moment.
  It is registered before the fence, so it sees an attempt whether or not the fence
  then refuses it.
* `ConnectionScanner` -- a thread that asks the operating system (psutil), not Python,
  which connections this process and its children actually hold. An established
  connection to anywhere external is recorded as `observed`. This is the detector that
  would catch a native library opening its own socket, which no audit hook can see.
* the deliberate probe (probe.py), which records its own outcome.

`EgressRecorder` is the only writer: a queue drained by a background thread into
`egress_events` (the operational detail) and, for the governance-relevant kinds, the
audit chain (egress.attempt / egress.blocked / dns.denied). Recording never happens
inside the hook itself beyond a queue put, so a hook can never block or recurse on I/O.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database, Json
from citadel_platform.tracing import current_agent_id, current_task_id

from citadel_sovereignty.policy import EgressPolicy, is_ip

KINDS = ("attempt", "blocked", "dns_denied", "observed")
_AUDIT_EVENT = {"attempt": "egress.attempt", "blocked": "egress.blocked", "dns_denied": "dns.denied", "observed": "egress.attempt"}
_reentry = threading.local()


@dataclass
class EgressEvent:
    kind: str
    detector: str
    destination: str
    port: Optional[int]
    task_id: Optional[str]
    agent_id: Optional[str]
    detail: dict[str, Any]


class EgressRecorder:
    def __init__(self, db: Optional[Database], process: str, *, audit: Optional[AuditLog] = None, per_minute: int = 240) -> None:
        self.db = db
        self.process = process
        self.audit = audit
        self._queue: "queue.SimpleQueue[EgressEvent]" = queue.SimpleQueue()
        self._per_minute = per_minute
        self._window = (0.0, 0)
        self._counts = {k: 0 for k in KINDS}
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def record(
        self,
        kind: str,
        detector: str,
        destination: str,
        port: Optional[int] = None,
        detail: Optional[Mapping[str, Any]] = None,
        *,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            started, count = self._window
            if now - started > 60:
                self._window = (now, 1)
            elif count >= self._per_minute:
                return  # a flood is itself visible in the counts already recorded
            else:
                self._window = (started, count + 1)
            self._counts[kind] = self._counts.get(kind, 0) + 1
        self._queue.put(EgressEvent(
            kind, detector, str(destination)[:250], port,
            task_id or current_task_id(), agent_id or current_agent_id(), dict(detail or {}),
        ))
        self._ensure_writer()

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def _ensure_writer(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            with self._lock:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._drain, name="egress-recorder", daemon=True)
                    self._thread.start()

    def flush(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        self._write_batch()

    def _drain(self) -> None:
        _reentry.active = True
        while True:
            try:
                first = self._queue.get(timeout=30)
            except queue.Empty:
                return
            self._write([first])

    def _write_batch(self) -> None:
        batch: list[EgressEvent] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._write(batch)

    def _write(self, first: list[EgressEvent]) -> None:
        batch = list(first)
        while len(batch) < 100:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        previous = getattr(_reentry, "active", False)
        _reentry.active = True
        try:
            if self.db is not None:
                self.db.script([
                    (
                        "INSERT INTO egress_events (process, kind, detector, destination, port, task_id, agent_id, detail) "
                        "VALUES (%(p)s, %(k)s, %(d)s, %(dest)s, %(port)s, %(t)s::uuid, %(a)s, %(detail)s)",
                        {"p": self.process, "k": e.kind, "d": e.detector, "dest": e.destination, "port": e.port,
                         "t": e.task_id, "a": e.agent_id, "detail": Json(e.detail)},
                    )
                    for e in batch
                ])
            if self.audit is not None:
                for e in batch:
                    self.audit.record(
                        _AUDIT_EVENT[e.kind],
                        actor_id=None,
                        payload={"process": self.process, "kind": e.kind, "detector": e.detector,
                                 "destination": e.destination, "port": e.port, "task_id": e.task_id, "agent_id": e.agent_id},
                    )
        except Exception as exc:  # telemetry must never take the service down; say so loudly
            print(f"[sovereignty] failed to record {len(batch)} egress event(s): {exc}", file=sys.stderr)
        finally:
            _reentry.active = previous


def _destination(args: tuple[Any, ...]) -> Optional[tuple[str, Optional[int]]]:
    if len(args) < 2:
        return None
    address = args[1]
    if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[0], str):
        return address[0], int(address[1]) if isinstance(address[1], int) else None
    return None


class EgressMonitor:
    """Observation only. Records an attempt; never refuses one."""

    def __init__(self, policy: EgressPolicy, recorder: EgressRecorder) -> None:
        self.policy = policy
        self.recorder = recorder
        self.enabled = True
        self.installed = False

    def install(self) -> None:
        if not self.installed:
            sys.addaudithook(self._hook)
            self.installed = True

    def _hook(self, event: str, args: tuple[Any, ...]) -> None:
        if not self.enabled or getattr(_reentry, "active", False):
            return
        if event == "socket.connect":
            target = _destination(args)
            if target and not self.policy.address_allowed(target[0], target[1]):
                self.recorder.record("attempt", "monitor", target[0], target[1], {"event": event})
        elif event == "socket.getaddrinfo" and args and isinstance(args[0], (str, bytes)):
            name = args[0].decode("ascii", "replace") if isinstance(args[0], bytes) else args[0]
            if name and not is_ip(name) and not self.policy.name_allowed(name):
                port = args[1] if len(args) > 1 and isinstance(args[1], int) else None
                self.recorder.record("attempt", "monitor", name, port, {"event": event, "lookup": True})


class ConnectionScanner:
    """Asks the OS which connections exist. Independent of every hook in this process."""

    def __init__(self, policy: EgressPolicy, recorder: EgressRecorder, *, interval_s: float = 5.0) -> None:
        self.policy = policy
        self.recorder = recorder
        self.interval_s = interval_s
        self.scans = 0
        self.last_scan: Optional[float] = None
        self._seen: dict[tuple[str, int], float] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.available = True

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="egress-scanner", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def scan(self) -> list[dict[str, Any]]:
        try:
            import psutil
        except ImportError:
            self.available = False
            return []
        external: list[dict[str, Any]] = []
        me = psutil.Process()
        processes = [me]
        try:
            processes.extend(me.children(recursive=True))
        except psutil.Error:
            pass
        for process in processes:
            try:
                connections = process.net_connections(kind="inet") if hasattr(process, "net_connections") else process.connections(kind="inet")
            except (psutil.Error, OSError):
                continue
            # A connection accepted on one of this process's own listening ports was opened
            # by someone else -- the browser reaching the API, say. It is inbound, not
            # egress. It has to be told apart here because Docker delivers a published
            # port's connections from an address outside every internal network, so
            # without this the operator's own browser would read as data leaving.
            listening = {c.laddr.port for c in connections if c.status == psutil.CONN_LISTEN and c.laddr}
            for conn in connections:
                if not conn.raddr:
                    continue
                if conn.laddr and conn.laddr.port in listening:
                    continue
                ip, port = conn.raddr.ip, conn.raddr.port
                if self.policy.address_allowed(ip, port):
                    continue
                external.append({"ip": ip, "port": port, "status": conn.status, "pid": process.pid})
        now = time.monotonic()
        for item in external:
            key = (item["ip"], item["port"])
            if now - self._seen.get(key, 0.0) > 60:
                self._seen[key] = now
                self.recorder.record("observed", "scanner", item["ip"], item["port"],
                                     {"status": item["status"], "pid": item["pid"]}, task_id=None)
        self.scans += 1
        self.last_scan = time.time()
        return external

    def _loop(self) -> None:
        _reentry.active = True
        while not self._stop.wait(self.interval_s):
            try:
                self.scan()
            except Exception as exc:
                print(f"[sovereignty] connection scan failed: {exc}", file=sys.stderr)


__all__ = ["EgressRecorder", "EgressMonitor", "ConnectionScanner", "EgressEvent", "KINDS"]
