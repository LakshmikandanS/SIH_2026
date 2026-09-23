"""Build the worker's process-lifetime objects from the environment."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from citadel_gateway import AdmissionGate, Gateway, ProviderError, build_provider
from citadel_knowledge import IngestContext, claim_next, fail, ingest
from citadel_platform.audit.log import AuditLog
from citadel_platform.db import Database
from citadel_platform.keyring import load_receipt_public_key, load_receipt_signing_key
from citadel_platform.registry import Registry, load_registry
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer
from citadel_runtime import BudgetLimits, Runtime
from citadel_sovereignty import HttpSandboxRunner, Sovereignty, install
from citadel_tools import Chokepoint, DataBoundary, LocalSandboxRunner, SandboxRunner


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def registry_from_env(env: Mapping[str, str]) -> tuple[Registry, Path]:
    registry_dir = Path(env.get("CITADEL_REGISTRY_DIR") or repo_root() / "registry")
    return load_registry(env.get("CITADEL_PROFILE") or "demo-local", registry_dir), registry_dir


@dataclass
class WorkerServices:
    env: Mapping[str, str]
    registry: Registry
    registry_dir: Path
    db: Database
    data_dir: DataDir
    audit: AuditLog
    tracer: Tracer
    gateway: Gateway
    sovereignty: Sovereignty
    runtime: Runtime
    cpu: AdmissionGate


def build(env: Optional[Mapping[str, str]] = None) -> WorkerServices:
    environ: Mapping[str, str] = env if env is not None else os.environ
    registry, registry_dir = registry_from_env(environ)
    db = Database(env=dict(environ))
    data_dir = DataDir.from_env(environ, default=repo_root() / ".citadel-data")
    audit = AuditLog(dict(environ), registry.event_registry())
    tracer = Tracer(db)
    provider, notes = build_provider(registry, environ)
    endpoint = getattr(provider, "endpoint", "") or ""
    sandbox_url = environ.get("CITADEL_SANDBOX_URL") or ""
    sovereignty = install("worker", db, audit=audit, env=environ, extra_urls=(endpoint, sandbox_url))
    gateway = Gateway(registry, provider, audit=audit, tracer=tracer, notes=notes)
    public = load_receipt_public_key(environ)
    sandbox: SandboxRunner = HttpSandboxRunner(sandbox_url) if sandbox_url else LocalSandboxRunner(public)
    runtime = Runtime(
        db=db,
        registry=registry,
        registry_dir=registry_dir,
        data_dir=data_dir,
        gateway=gateway,
        chokepoint=Chokepoint(registry, signing_key=load_receipt_signing_key(environ)),
        boundary=DataBoundary(public),
        sandbox=sandbox,
        audit=audit,
        tracer=tracer,
        limits=BudgetLimits(
            max_steps=int(environ.get("CITADEL_MAX_STEPS") or 14),
            max_seconds=int(environ.get("CITADEL_MAX_TASK_SECONDS") or 1500),
        ),
    )
    return WorkerServices(
        env=environ, registry=registry, registry_dir=registry_dir, db=db, data_dir=data_dir, audit=audit,
        tracer=tracer, gateway=gateway, sovereignty=sovereignty, runtime=runtime,
        cpu=AdmissionGate("cpu", registry.profile.cpu_admission),
    )


def worker_id() -> str:
    return f"worker-{socket.gethostname()}-{os.getpid()}"


def ingestion_loop(services: WorkerServices, stop: threading.Event) -> None:
    """Documents waiting for ingestion, one at a time per thread, behind the CPU gate."""
    ctx = IngestContext(
        db=services.db, data_dir=services.data_dir, gateway=services.gateway, audit=services.audit,
        tracer=services.tracer, cpu=services.cpu, worker_id=worker_id(),
    )
    while not stop.is_set():
        try:
            document = claim_next(services.db, worker_id())
        except Exception as exc:
            print(f"[worker] document claim failed: {exc}", file=sys.stderr)
            stop.wait(5.0)
            continue
        if document is None:
            stop.wait(1.5)
            continue
        try:
            report = ingest(ctx, document)
            print(f"[worker] ingested {document['title']}: {report.to_dict() if hasattr(report, 'to_dict') else ''}", flush=True)
        except Exception as exc:
            print(f"[worker] ingestion of {document['id']} failed: {exc}", file=sys.stderr)
            try:
                fail(ctx, document, str(exc)[:500])
            except Exception:
                pass


def warm_models(services: WorkerServices, stop: threading.Event) -> None:
    """Pin the resident set once the runtime answers; retry quietly until it does."""
    delay = 5.0
    while not stop.is_set():
        try:
            warmed = services.gateway.warm_resident_set()
            if warmed:
                print(f"[worker] resident set pinned: {', '.join(warmed)}", flush=True)
                return
            missing = [m.id for m in services.gateway.missing_models()]
            if missing:
                print(f"[worker] models not installed yet: {', '.join(missing)} (pull them from the Models panel)", flush=True)
        except ProviderError as exc:
            print(f"[worker] inference runtime not reachable yet: {exc}", flush=True)
        except Exception as exc:
            print(f"[worker] could not warm the resident set: {exc}", file=sys.stderr)
        stop.wait(delay)
        delay = min(delay * 2, 120.0)


def wait_for_database(db: Database, *, seconds: float = 90.0) -> None:
    deadline = time.monotonic() + seconds
    last: Any = None
    while time.monotonic() < deadline:
        try:
            if db.scalar("SELECT count(*) FROM tasks") is not None:
                return
        except Exception as exc:
            last = exc
        time.sleep(2.0)
    raise RuntimeError(f"the database (with migrations applied) did not become available: {last}")


__all__ = ["WorkerServices", "build", "worker_id", "ingestion_loop", "warm_models", "wait_for_database", "repo_root"]
