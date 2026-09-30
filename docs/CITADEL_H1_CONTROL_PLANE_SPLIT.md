# Superseded — see `CITADEL_TARGET_ARCHITECTURE.md` and `CITADEL_P1_AUTHORIZATION_SPINE.md`

This document planned a Control Plane process split as the first phase of horizontal growth. It was
withdrawn on 21 Sep 2026 because it was shaped by the existing repository rather than by the
architecture the project is aiming at: it planned module moves that minimised disruption, reused
seams because they happened to exist, and accepted a shared package import between two services
because the alternative was inconvenient.

It was also wrong about ordering. Splitting processes first produces services that still trust each
other's word and merely happen to run separately — the deployment diagram looks right while the
invariant stays false. The replacement establishes signed decision receipts first, after which the
process split is a deployment change rather than a security change.

Nothing in here should be built. Its one durable finding is recorded in the replacement: the current
HMAC token scheme is justified in `app/config.py` on the grounds that issuer and verifier share a
process, and that premise does not survive any boundary the project intends to build.
