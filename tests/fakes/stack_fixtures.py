"""Shared wiring for tests that need the real registry, a fake runtime and a gateway.

The registry is the real `registry/` for the named profile with every model switched
on -- tests exercise the same routing data the product ships, only the runtime behind
it is scripted (fake_ollama.py).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from citadel_gateway import AdmissionGate, Gateway
from citadel_gateway.ollama import OllamaProvider
from citadel_platform.db import Database
from citadel_platform.registry import Registry, load_registry
from citadel_platform.storage import DataDir
from citadel_platform.tracing import Tracer
from fake_ollama import Brain, FakeOllama, minimal_instance

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "ops" / "demo" / "corpus"


def enabled_registry(profile: str = "demo-local") -> Registry:
    registry = load_registry(profile, REPO_ROOT / "registry")
    models = tuple(m.model_copy(update={"enabled": True}) for m in registry.models)
    return dataclasses.replace(registry, models=models)


def image_tags(registry: Registry) -> list[str]:
    return [m.tag for m in registry.models if "image" in m.modalities]


def start_fake(registry: Registry, brain: Optional[Brain] = None) -> FakeOllama:
    fake = FakeOllama(
        [m.tag for m in registry.models],
        vision_tags=image_tags(registry),
        brain=brain or vision_aware_brain,
    )
    return fake.start()


def gateway_for(registry: Registry, fake: FakeOllama, **kwargs: Any) -> Gateway:
    return Gateway(registry, OllamaProvider(fake.url), **kwargs)


def vision_aware_brain(model: str, messages: Sequence[Mapping[str, Any]], schema: Optional[Mapping[str, Any]]) -> str:
    """Page readings for image requests; minimal schema instances otherwise."""
    has_image = any(m.get("images") for m in messages)
    if has_image and schema is not None and "stamps" in (schema.get("properties") or {}):
        return json.dumps(
            {
                "document_type": "inspection report",
                "summary": "Scanned page with a circular rubber stamp and a handwritten signature.",
                "stamps": [{"text": "QA INSPECTED 14 AUG 2026 INSP. CELL"}],
                "signatures": [{"present_near": "Inspected by"}],
                "handwritten_fields": [],
                "key_values": [{"key": "Equipment tag", "value": "E-101"}],
            }
        )
    if schema is not None:
        return json.dumps(minimal_instance(schema))
    return "ok"


def ingest_corpus(env: Mapping[str, str], data_dir: DataDir, gateway: Gateway) -> Database:
    """Seed ops/demo/corpus through the real upload path and ingest every document
    through the real worker path (real Tesseract, real pgvector, the given gateway)."""
    from citadel_knowledge import IngestContext, claim_next, ingest
    from citadel_knowledge.seed import seed_corpus
    from citadel_platform.identity.store import get_user_by_external_identity

    db = Database(env=dict(env))
    summary = seed_corpus(
        db,
        data_dir,
        CORPUS / "manifest.yaml",
        profile_ceiling="CONFIDENTIAL",
        lookup_user=lambda external: get_user_by_external_identity(env, external),
    )
    assert summary["rejected"] == [], summary
    ctx = IngestContext(db=db, data_dir=data_dir, gateway=gateway, audit=None, tracer=Tracer(db), cpu=AdmissionGate("cpu", 2))
    while (document := claim_next(db, "test")) is not None:
        ingest(ctx, document)
    return db


__all__ = [
    "REPO_ROOT",
    "CORPUS",
    "enabled_registry",
    "image_tags",
    "start_fake",
    "gateway_for",
    "vision_aware_brain",
    "ingest_corpus",
]
