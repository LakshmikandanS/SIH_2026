"""Which processes are alive, and how they are doing: each service writes one row every
few seconds (migration 0010, `service_heartbeats`). The container-health and
resource panels read them; nothing else depends on them, so a failed beat is printed and
forgotten, never raised.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from typing import Any, Callable, Mapping, Optional

from citadel_platform.db import Database, Json

INTERVAL_S = 10.0
#: A service that has not written for this long is shown as stale.
STALE_AFTER_S = 45


_STARTED = time.monotonic()


def _proc_status(field: str) -> Optional[int]:
    """A kB figure from /proc/self/status (Linux, which is where the services run)."""
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def process_stats() -> dict[str, Any]:
    """This process's own footprint -- what a container-health panel needs to tell a
    healthy service from one that is thrashing. Standard library only: /proc where it
    exists, and whatever is portable where it does not."""
    stats: dict[str, Any] = {"pid": os.getpid(), "threads": threading.active_count(), "python": sys.version.split()[0],
                             "uptime_s": int(time.monotonic() - _STARTED), "cpu_seconds": round(time.process_time(), 1)}
    rss_kb = _proc_status("VmRSS")
    if rss_kb is not None:
        stats["rss_mb"] = round(rss_kb / 1024, 1)
    try:
        stats["open_files"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    return stats


def host_stats() -> dict[str, Any]:
    """The machine this process runs on, as its container sees it: CPUs, load, memory."""
    stats: dict[str, Any] = {"cpus": os.cpu_count()}
    if hasattr(os, "getloadavg"):
        try:
            stats["load"] = [round(v, 2) for v in os.getloadavg()]
        except OSError:
            pass
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            info = {line.split(":")[0]: int(line.split()[1]) for line in handle if line.split()[1:2]}
        total, available = info.get("MemTotal"), info.get("MemAvailable")
        if total:
            stats["memory_total_mb"] = round(total / 1024)
            if available is not None:
                stats["memory_used_pct"] = round(100 * (total - available) / total, 1)
    except (OSError, ValueError, IndexError):
        pass
    return stats


def beat(db: Database, service: str, instance: str, detail: Optional[Mapping[str, Any]] = None) -> None:
    db.execute(
        "INSERT INTO service_heartbeats (service, instance, pid, detail) VALUES (%(s)s, %(i)s, %(p)s, %(d)s) "
        "ON CONFLICT (service, instance) DO UPDATE SET last_seen = now(), pid = EXCLUDED.pid, detail = EXCLUDED.detail",
        {"s": service, "i": instance, "p": os.getpid(), "d": Json({**process_stats(), **dict(detail or {})})},
    )


def start(
    db: Database,
    service: str,
    *,
    stop: Optional[threading.Event] = None,
    detail: Optional[Callable[[], Mapping[str, Any]]] = None,
    interval_s: float = INTERVAL_S,
) -> threading.Event:
    """Beat on a daemon thread until `stop` is set; returns the event that stops it."""
    stopper = stop or threading.Event()
    instance = f"{socket.gethostname()}:{os.getpid()}"

    def loop() -> None:
        while not stopper.is_set():
            try:
                beat(db, service, instance, detail() if detail is not None else None)
            except Exception as exc:
                print(f"[{service}] heartbeat not written: {str(exc)[:200]}", file=sys.stderr, flush=True)
            stopper.wait(interval_s)

    threading.Thread(target=loop, name=f"{service}-heartbeat", daemon=True).start()
    return stopper


def services(db: Database) -> list[dict[str, Any]]:
    return db.query(
        "SELECT service, instance, pid, started_at, last_seen, detail, "
        "extract(epoch FROM now() - last_seen)::int AS seconds_since, "
        "(now() - last_seen) > make_interval(secs => %(stale)s) AS stale "
        "FROM service_heartbeats WHERE last_seen > now() - interval '1 day' ORDER BY service, last_seen DESC",
        {"stale": STALE_AFTER_S},
    )


__all__ = ["beat", "start", "services", "process_stats", "host_stats", "INTERVAL_S", "STALE_AFTER_S"]
