"""Key material shared across processes: a keys directory, created once.

`citadel_platform.identity.keys` settled the session key's lifecycle for a single
process: an environment variable, or a loud per-process fallback. Two things change
once the system is more than one process:

* The **receipt** keypair (citadel_contracts.receipts) is split by design. The policy
  chokepoint -- in the API and the worker -- holds the private half; the executing
  boundary (the sandbox) holds only the public half and verifies every receipt it is
  handed. The two halves have to come from the same generation, so a per-process
  random fallback cannot work for receipts at all.
* The **session** key must survive an API restart, or every signed-in user is logged
  out whenever the container restarts.

So: a keys directory (`CITADEL_KEYS_DIR`), populated once by `python -m
citadel_platform.keyring init`, which generates only what is missing and never
overwrites. In Docker Compose a one-shot `keys` service runs it against a named volume;
the sandbox mounts that volume and reads nothing but `receipt.pub`. Explicit
environment variables still win over files, so an operator with a real secret store
never needs the directory at all.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from citadel_platform.identity.keys import load_session_signing_key

KEYS_DIR_VAR = "CITADEL_KEYS_DIR"
SESSION_KEY_VAR = "CITADEL_SESSION_SIGNING_KEY"
RECEIPT_KEY_VAR = "CITADEL_RECEIPT_SIGNING_KEY"
RECEIPT_PUBLIC_VAR = "CITADEL_RECEIPT_PUBLIC_KEY"

SESSION_KEY_FILE = "session.key"
RECEIPT_KEY_FILE = "receipt.key"
RECEIPT_PUBLIC_FILE = "receipt.pub"


class KeyMaterialError(RuntimeError):
    """Key material is required and absent or unusable. Never papered over with a
    generated key for receipts: a chokepoint and a sandbox holding different random
    keys would refuse every call, which is the right outcome but a baffling one."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _decode(value: str, what: str) -> bytes:
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise KeyMaterialError(f"{what} is not valid base64: {exc}") from exc
    if len(raw) != 32:
        raise KeyMaterialError(f"{what} must decode to 32 bytes, got {len(raw)}")
    return raw


def _keys_dir(env: Mapping[str, str]) -> Optional[Path]:
    value = env.get(KEYS_DIR_VAR)
    return Path(value) if value else None


def _read(env: Mapping[str, str], var: str, filename: str) -> Optional[str]:
    if env.get(var):
        return env[var]
    directory = _keys_dir(env)
    if directory is not None and (directory / filename).is_file():
        return (directory / filename).read_text(encoding="ascii")
    return None


def init_keys(directory: Path) -> list[str]:
    """Create whichever key files are missing; never overwrite. Returns the names
    created, so the caller can say what it did."""
    directory.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    session = directory / SESSION_KEY_FILE
    if not session.exists():
        session.write_text(_b64(Ed25519PrivateKey.generate().private_bytes_raw()), encoding="ascii")
        created.append(SESSION_KEY_FILE)

    receipt = directory / RECEIPT_KEY_FILE
    public = directory / RECEIPT_PUBLIC_FILE
    if not receipt.exists():
        private = Ed25519PrivateKey.generate()
        receipt.write_text(_b64(private.private_bytes_raw()), encoding="ascii")
        public.write_text(_b64(private.public_key().public_bytes_raw()), encoding="ascii")
        created += [RECEIPT_KEY_FILE, RECEIPT_PUBLIC_FILE]
    elif not public.exists():
        private = Ed25519PrivateKey.from_private_bytes(
            _decode(receipt.read_text(encoding="ascii"), str(receipt))
        )
        public.write_text(_b64(private.public_key().public_bytes_raw()), encoding="ascii")
        created.append(RECEIPT_PUBLIC_FILE)

    for name in (SESSION_KEY_FILE, RECEIPT_KEY_FILE):
        try:
            os.chmod(directory / name, 0o600)
        except OSError:
            pass  # a filesystem without POSIX modes (a Windows bind mount) -- nothing to tighten
    return created


def load_session_key(env: Optional[Mapping[str, str]] = None) -> Ed25519PrivateKey:
    """Environment variable, then keys directory, then identity.keys' loud
    per-process fallback -- in that order."""
    environ = env if env is not None else os.environ
    value = _read(environ, SESSION_KEY_VAR, SESSION_KEY_FILE)
    if value is not None:
        return Ed25519PrivateKey.from_private_bytes(_decode(value, SESSION_KEY_VAR))
    return load_session_signing_key(environ)


def load_receipt_signing_key(env: Optional[Mapping[str, str]] = None) -> Optional[Ed25519PrivateKey]:
    """The chokepoint's private receipt key, or None when this process has none --
    in which case it must refuse every tool that requires a receipt."""
    environ = env if env is not None else os.environ
    value = _read(environ, RECEIPT_KEY_VAR, RECEIPT_KEY_FILE)
    if value is None:
        return None
    return Ed25519PrivateKey.from_private_bytes(_decode(value, RECEIPT_KEY_VAR))


def load_receipt_public_key(env: Optional[Mapping[str, str]] = None) -> Ed25519PublicKey:
    """The executing boundary's verification key. Required: a boundary that cannot
    verify receipts must not run anything."""
    environ = env if env is not None else os.environ
    value = _read(environ, RECEIPT_PUBLIC_VAR, RECEIPT_PUBLIC_FILE)
    if value is None:
        signing = load_receipt_signing_key(environ)
        if signing is None:
            raise KeyMaterialError(
                f"no receipt verification key: set {RECEIPT_PUBLIC_VAR}, or {KEYS_DIR_VAR} "
                f"pointing at a directory initialised by 'python -m citadel_platform.keyring init'"
            )
        return signing.public_key()
    return Ed25519PublicKey.from_public_bytes(_decode(value, RECEIPT_PUBLIC_VAR))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m citadel_platform.keyring")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create missing key files in a keys directory")
    init.add_argument("--dir", default=os.environ.get(KEYS_DIR_VAR), required=False)
    args = parser.parse_args(argv)
    if args.command == "init":
        if not args.dir:
            print(f"--dir or {KEYS_DIR_VAR} is required", file=sys.stderr)
            return 2
        created = init_keys(Path(args.dir))
        print(f"[keyring] {args.dir}: created {created}" if created else f"[keyring] {args.dir}: all keys present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "KEYS_DIR_VAR",
    "KeyMaterialError",
    "init_keys",
    "load_session_key",
    "load_receipt_signing_key",
    "load_receipt_public_key",
]
