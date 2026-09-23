"""The one sanctioned hop to the sandbox service.

The worker hands `code.run` requests -- source, the receipt, the task's workspace files
-- to the sandbox container over the internal network, and the sandbox verifies the
receipt itself before running anything. This client lives here, not in citadel_tools,
because it is a network client and the no-egress detector allows those only in the
gateway and in this package; its destination is one of the deployment's own, named in
configuration (CITADEL_SANDBOX_URL) and on the egress allowlist.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

SANDBOX_URL_VAR = "CITADEL_SANDBOX_URL"


class SandboxUnavailable(RuntimeError):
    pass


class HttpSandboxRunner:
    kind = "container"

    def __init__(self, url: str, *, timeout_s: float = 150.0) -> None:
        self.url = url.rstrip("/")
        # trust_env=False: the sandbox is on an internal network; an ambient proxy variable
        # must never get a say in where a workspace and its receipt are sent.
        self._client = httpx.Client(timeout=timeout_s, trust_env=False)

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(f"{self.url}/run", json=dict(request))
        except httpx.HTTPError as exc:
            raise SandboxUnavailable(f"sandbox service unreachable at {self.url}: {exc}") from exc
        if response.status_code >= 500:
            raise SandboxUnavailable(f"sandbox service error {response.status_code}: {response.text[:300]}")
        data = response.json()
        if not isinstance(data, dict):
            raise SandboxUnavailable("sandbox service returned a malformed reply")
        return data

    def health(self) -> dict[str, Any]:
        try:
            response = self._client.get(f"{self.url}/health", timeout=5.0)
            data = response.json()
            return data if isinstance(data, dict) else {"ok": False}
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def probe(self, *, task_id: str | None = None) -> dict[str, Any]:
        try:
            response = self._client.post(f"{self.url}/probe", json={"task_id": task_id}, timeout=30.0)
            data = response.json()
            return data if isinstance(data, dict) else {"error": "malformed reply"}
        except (httpx.HTTPError, ValueError) as exc:
            return {"error": f"sandbox probe failed: {exc}"}


__all__ = ["HttpSandboxRunner", "SandboxUnavailable", "SANDBOX_URL_VAR"]
