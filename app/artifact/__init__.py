"""Step 8 -- Artifact + Verifier (design doc section 6.10, BB-038/039/043/045).

    "An artifact that nothing ever mechanically verifies" -- closed here,
    completely. `generate_report` (the Tool Gateway backend, `app.artifact.backend`)
    renders the one fixed template (BB-045: "selection" is a no-op, exactly
    one template exists), and `app.artifact.pipeline.create_and_verify_artifact`
    runs the five-check Verifier (BB-043) synchronously, right after
    generation, in the same call that creates the `Artifact` row.

Import the pipeline entry point and the Verifier from here; `app.artifact.verifier`
and `app.artifact.template` are implementation modules, not a second public API.
"""

from __future__ import annotations

from app.artifact.pipeline import ArtifactPipelineOutcome, create_and_verify_artifact
from app.artifact.verifier import (
    REQUIRED_SECTIONS,
    VerifiableArtifact,
    VerificationReport,
    run_verification,
    verify,
)

__all__ = [
    "REQUIRED_SECTIONS",
    "ArtifactPipelineOutcome",
    "VerifiableArtifact",
    "VerificationReport",
    "create_and_verify_artifact",
    "run_verification",
    "verify",
]
