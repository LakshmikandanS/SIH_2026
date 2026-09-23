"""Where model-authored code executes -- after the receipt says it may.

`run_verified` is the executing boundary for `code.run`. It is called by the sandbox
service (services/sandbox, one container on an internal-only network with no route out)
and, where no container is available, by `LocalSandboxRunner` in-process. Either way it
does the same things in the same order:

1. Recompute the resource digest over the source it actually received and verify the
   receipt against it (signature, expiry, operation, digest, single use) with its own
   nonce store. Nothing runs on an invalid receipt.
2. Run the source in a fresh temporary directory, as a separate Python process with a
   minimal environment (no Citadel or database variables), CPU/memory/file-size limits
   where the OS supports them, a wall-clock timeout that kills the whole process group,
   and an audit hook that refuses sockets, subprocesses, foreign native libraries and
   file access outside the run directory and the interpreter's own library paths.
3. Return stdout, stderr, the exit code and any files the code wrote, then delete the
   directory.

The audit hook is defence in depth, not the boundary: in the container deployment the
network namespace has no route out at all (ops/compose). The local process sandbox is
labelled as exactly what it is -- a development fallback without container isolation --
in every result it returns.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from citadel_contracts.domain import Resource
from citadel_contracts.receipts import (
    InMemoryNonceStore,
    NonceStore,
    ReceiptExpired,
    ReceiptInvalid,
    verify_receipt,
)

OPERATION = "code.run"
MAX_SOURCE = 100_000
MAX_OUTPUT = 64_000
MAX_FILES = 20
MAX_FILE_BYTES = 10_000_000
MEMORY_BYTES = 2 * 1024**3
FILE_SIZE_BYTES = 64 * 1024**2

_GUARD = r'''
import os as _os, sys as _sys
_ROOT = _os.path.realpath(_os.getcwd())
_READ_OK = tuple({_os.path.realpath(p) for p in [_ROOT, _sys.prefix, _sys.base_prefix, _sys.exec_prefix,
    *[p for p in _sys.path if p], "/usr/share", "/usr/lib", "/usr/local/lib", "/etc/fonts", "/etc/ssl",
    "/dev/null", "/dev/urandom", "/proc/self", "/sys/devices/system/cpu"] if p})
_WRITE_OK = (_ROOT, "/dev/null")
_BLOCKED = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
    "os.forkpty", "pty.spawn", "os.startfile", "webbrowser.", "urllib.Request", "http.client.connect",
    "ftplib.", "smtplib.", "imaplib.", "poplib.", "nntplib.", "telnetlib.", "os.kill", "os.killpg", "winreg.")
def _inside(path, roots):
    try:
        real = _os.path.realpath(path if isinstance(path, str) else _os.fsdecode(path))
    except Exception:
        return False
    return any(real == r or real.startswith(r.rstrip(_os.sep) + _os.sep) for r in roots)
def _guard(event, args):
    if event.startswith(_BLOCKED):
        raise PermissionError("blocked in the Citadel sandbox: " + event)
    if event in ("os.listdir", "os.scandir") and args and isinstance(args[0], (str, bytes)) and not _inside(args[0], _READ_OK):
        raise PermissionError("blocked in the Citadel sandbox: listing outside the run directory")
    if event == "ctypes.dlopen" and args and args[0] is not None:
        raise PermissionError("blocked in the Citadel sandbox: loading native library " + str(args[0]))
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        path, mode, flags = (list(args) + [None, None])[:3]
        writing = bool(mode and any(c in str(mode) for c in "wax+")) or bool(flags and (flags & (_os.O_WRONLY | _os.O_RDWR | _os.O_CREAT)))
        if writing and not _inside(path, _WRITE_OK):
            raise PermissionError("blocked in the Citadel sandbox: writing outside the run directory")
        if not writing and not _inside(path, _READ_OK):
            raise PermissionError("blocked in the Citadel sandbox: reading outside the run directory")
_sys.addaudithook(_guard)
del _guard
_source = open("main.py", encoding="utf-8").read()
_globals = {"__name__": "__main__", "__file__": "main.py", "__builtins__": __builtins__}
exec(compile(_source, "main.py", "exec"), _globals)
'''


def code_resource(source: str, classification: str, acl: Any) -> Resource:
    """The resource a code.run receipt is bound to -- the exact source text. Built the
    same way by the chokepoint (from the model's call) and by this boundary (from the
    source it received), so a single changed byte fails verification."""
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return Resource.build(f"code:{digest}", "code", str(classification), tuple(str(a) for a in acl))


def _limits() -> None:  # pragma: no cover - runs in the child process
    import resource

    os.setsid()
    for name, value in (
        ("RLIMIT_AS", MEMORY_BYTES),
        ("RLIMIT_FSIZE", FILE_SIZE_BYTES),
        ("RLIMIT_NOFILE", 256),
        ("RLIMIT_CORE", 0),
    ):
        limit = getattr(resource, name, None)
        if limit is not None:
            try:
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError):
                pass


def _snapshot(root: Path) -> dict[str, str]:
    seen = {}
    for path in root.rglob("*"):
        if path.is_file() and ".mpl" not in path.parts:
            seen[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return seen


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} characters omitted] ...\n" + text[-half:]


def execute(source: str, *, timeout_s: int, files: Mapping[str, str], kind: str) -> dict[str, Any]:
    """Run `source` in a fresh directory. `files` maps relative names to base64 bytes
    to place beside it; the result carries every file the run created or changed."""
    workdir = Path(tempfile.mkdtemp(prefix="citadel-run-"))
    try:
        for name, encoded in list(files.items())[:MAX_FILES]:
            target = (workdir / name).resolve()
            if workdir.resolve() not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(encoded))
        (workdir / "main.py").write_text(source, encoding="utf-8")
        (workdir / "_citadel_guard.py").write_text(_GUARD, encoding="utf-8")
        before = _snapshot(workdir)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(workdir / ".mpl"),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        }
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        started = time.perf_counter()
        timed_out = False
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "_citadel_guard.py"],
            cwd=str(workdir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_limits if os.name == "posix" else None,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                try:
                    os.killpg(process.pid, 9)
                except OSError:
                    process.kill()
            else:
                process.kill()
            stdout, stderr = process.communicate()
        duration_ms = int((time.perf_counter() - started) * 1000)
        after = _snapshot(workdir)
        produced: dict[str, str] = {}
        total = 0
        for name, digest in sorted(after.items()):
            if name in ("main.py", "_citadel_guard.py") or before.get(name) == digest:
                continue
            data = (workdir / name).read_bytes()
            if len(produced) >= MAX_FILES or total + len(data) > MAX_FILE_BYTES:
                break
            produced[name] = base64.b64encode(data).decode("ascii")
            total += len(data)
        error_text = stderr.decode("utf-8", "replace").replace(str(workdir), "<run>")
        return {
            "exit_code": process.returncode if not timed_out else -9,
            "timed_out": timed_out,
            "stdout": _clip(stdout.decode("utf-8", "replace"), MAX_OUTPUT),
            "stderr": _clip(error_text, MAX_OUTPUT // 2),
            "duration_ms": duration_ms,
            "files": produced,
            "sandbox": {
                "kind": kind,
                "network": "no route out (container network)" if kind == "container" else "blocked in-process (audit hook)",
                "limits": {"timeout_s": timeout_s, "memory_bytes": MEMORY_BYTES if os.name == "posix" else None,
                           "file_size_bytes": FILE_SIZE_BYTES if os.name == "posix" else None},
                "isolation": "container" if kind == "container" else "process (development fallback; no container isolation)",
            },
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_verified(
    request: Mapping[str, Any],
    *,
    public_key: Ed25519PublicKey,
    nonces: NonceStore,
    kind: str,
) -> dict[str, Any]:
    """The executing boundary: verify, then run. Never runs on a refused receipt."""
    source = str(request.get("source") or "")
    if not source.strip() or len(source) > MAX_SOURCE:
        return {"receipt_verified": False, "receipt_error": "source missing or too large"}
    declared = request.get("resource") or {}
    resource = code_resource(source, str(declared.get("classification") or ""), declared.get("acl") or ())
    try:
        receipt = verify_receipt(
            str(request.get("receipt") or ""),
            public_key=public_key,
            operation=OPERATION,
            resource=resource,
            seen_nonces=nonces,
        )
    except (ReceiptInvalid, ReceiptExpired) as exc:
        return {"receipt_verified": False, "receipt_error": str(exc)}
    timeout = max(1, min(int(request.get("timeout_s") or 30), 120))
    result = execute(source, timeout_s=timeout, files=request.get("files") or {}, kind=kind)
    result.update({"receipt_verified": True, "decision_id": receipt.decision_id, "task_id": receipt.task_id})
    return result


class LocalSandboxRunner:
    """The in-process development sandbox: same receipt verification, same limits the
    OS allows, labelled as a process sandbox in every result."""

    kind = "process"

    def __init__(self, public_key: Ed25519PublicKey, nonces: Optional[NonceStore] = None) -> None:
        self._public_key = public_key
        self._nonces: NonceStore = nonces or InMemoryNonceStore()

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return run_verified(request, public_key=self._public_key, nonces=self._nonces, kind=self.kind)


__all__ = ["OPERATION", "code_resource", "execute", "run_verified", "LocalSandboxRunner"]
