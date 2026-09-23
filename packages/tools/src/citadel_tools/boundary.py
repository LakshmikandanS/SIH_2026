"""The data boundary: where a receipt is checked by the side that is about to act.

The chokepoint decides and signs; it never verifies its own receipts. The code that
reads the corpus, re-reads a page image or files a deliverable holds only the receipt
*public* key and its own nonce store, and immediately before acting it recomputes the
resource digest over what it is about to touch -- so an ALLOW for one document cannot
be spent on another, a replayed receipt is refused, and a document reclassified between
the decision and the read fails verification rather than leaking.
"""

from __future__ import annotations

from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from citadel_contracts.domain import Resource
from citadel_contracts.receipts import (
    InMemoryNonceStore,
    NonceStore,
    ReceiptExpired,
    ReceiptInvalid,
    verify_receipt,
)

from citadel_tools.context import Invocation, ReceiptRejected


class DataBoundary:
    name = "data-boundary"

    def __init__(self, public_key: Ed25519PublicKey, nonces: Optional[NonceStore] = None) -> None:
        self._public_key = public_key
        self._nonces: NonceStore = nonces or InMemoryNonceStore()

    def verify(self, invocation: Invocation, resource: Resource) -> None:
        """Verify the invocation's receipt against `resource` as recomputed by the
        caller *now*. Tools that do not require a receipt pass through untouched."""
        if not invocation.tool.requires_receipt:
            return
        if not invocation.receipt:
            raise ReceiptRejected("no receipt presented to the data boundary")
        try:
            verify_receipt(
                invocation.receipt,
                public_key=self._public_key,
                operation=invocation.tool.name,
                resource=resource,
                seen_nonces=self._nonces,
            )
        except (ReceiptInvalid, ReceiptExpired) as exc:
            raise ReceiptRejected(str(exc)) from None
        invocation.verified = True
        invocation.verified_by = self.name


__all__ = ["DataBoundary"]
