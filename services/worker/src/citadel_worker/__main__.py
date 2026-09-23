"""`python -m citadel_worker` -- run the task workers and the ingestion loop.

    python -m citadel_worker            # serve until stopped
    python -m citadel_worker seed       # register the demo corpus (idempotent), then exit
    python -m citadel_worker missing-models   # the approved models the runtime lacks, one tag per
                                              # line (exit 2: the runtime is not answering) -- for
                                              # the launchers, which must not guess

With CITADEL_SEED_CORPUS=1 the serving worker also registers the demo corpus -- but only
once every approved model is installed. Ingested before then, the scanned reports would
be read without vision and indexed without embeddings, and would stay that way, since
ingestion never re-runs on its own. A person's own uploads are not held back like this:
they are ingested at once and degrade honestly, saying which step was skipped.
CITADEL_SEED_CORPUS=now seeds immediately, models or not.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

from citadel_gateway import Gateway, build_provider
from citadel_knowledge.seed import seed_corpus
from citadel_platform.identity.store import get_user_by_external_identity
from citadel_runtime import Worker

from citadel_worker.wiring import (
    WorkerServices,
    build,
    ingestion_loop,
    registry_from_env,
    repo_root,
    wait_for_database,
    warm_models,
    worker_id,
)


def seed(services: WorkerServices) -> dict[str, object]:
    manifest = os.environ.get("CITADEL_SEED_MANIFEST") or str(repo_root() / "ops" / "demo" / "corpus" / "manifest.yaml")
    return seed_corpus(
        services.db,
        services.data_dir,
        Path(manifest),
        profile_ceiling=services.registry.profile.classification_ceiling,
        lookup_user=lambda external: get_user_by_external_identity(services.db.env, external),
    )


def seed_when_models_ready(services: WorkerServices, stop: threading.Event) -> None:
    """Register the demo corpus once every approved model is installed (module docstring)."""
    announced = False
    delay = 5.0
    while not stop.is_set():
        try:
            missing = [model.id for model in services.gateway.missing_models()]
            if not missing:
                print(f"[worker] demo corpus: {seed(services)}", flush=True)
                return
            if not announced:
                print(
                    f"[worker] the demo corpus waits for the approved models ({', '.join(missing)} not installed yet"
                    " or the runtime is not answering) -- run `citadel models`, or press 'Pull missing models'"
                    " under Models & routing",
                    flush=True,
                )
                announced = True
        except Exception as exc:
            print(f"[worker] could not seed the demo corpus yet: {exc}", file=sys.stderr, flush=True)
        stop.wait(delay)
        delay = min(delay * 2, 60.0)


def missing_models() -> int:
    """Answer from the registry and the runtime alone: no database, no telemetry, no
    audit -- this is a question a launcher asks, not work the worker does."""
    registry, _ = registry_from_env(os.environ)
    provider, notes = build_provider(registry, os.environ)
    gateway = Gateway(registry, provider, notes=notes)
    if not gateway.runtime_view(force=True).reachable:
        print(f"the inference runtime at {getattr(provider, 'endpoint', '?')} is not answering", file=sys.stderr)
        return 2
    for model in gateway.missing_models():
        print(model.tag)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["missing-models"]:
        return missing_models()
    services = build()
    wait_for_database(services.db)
    seed_mode = os.environ.get("CITADEL_SEED_CORPUS", "")
    if args[:1] == ["seed"] or seed_mode == "now":
        summary = seed(services)
        print(f"[worker] demo corpus: {summary}", flush=True)
        if args[:1] == ["seed"]:
            return 0
    stop = threading.Event()

    def _stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    for target, name in ((warm_models, "warm"), *((ingestion_loop, f"ingest-{i}") for i in range(services.registry.profile.cpu_admission))):
        threading.Thread(target=target, args=(services, stop), name=name, daemon=True).start()
    if seed_mode == "1":
        threading.Thread(target=seed_when_models_ready, args=(services, stop), name="seed", daemon=True).start()
    concurrency = int(os.environ.get("CITADEL_WORKERS") or 3)
    print(f"[worker] {worker_id()} serving with {concurrency} task thread(s); profile {services.registry.profile.name}", flush=True)
    Worker(services.runtime, worker_id(), concurrency=concurrency).serve(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
