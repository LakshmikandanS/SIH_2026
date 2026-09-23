"""Which destinations are the deployment's own.

One small, explicit list -- the database, the inference runtime, the sandbox, loopback
and the networks this container is directly attached to -- read from configuration,
never inferred from what happens to work. Everything else is egress.

The telemetry and the in-process fence both consult this list, and nothing else they
do is shared: the monitor only observes and records, the fence only refuses.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional
from urllib.parse import urlsplit

#: Comma-separated extra `host:port` (or `host`) entries a deployment adds.
ALLOW_VAR = "CITADEL_EGRESS_ALLOW"


def is_ip(value: str) -> bool:
    return _is_ip(value.strip().strip("[]"))


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


@dataclass
class EgressPolicy:
    endpoints: set[tuple[str, Optional[int]]] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    addresses: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *, extra_urls: Iterable[str] = ()) -> "EgressPolicy":
        env = env if env is not None else os.environ
        policy = cls()
        policy.names.update({"localhost", "localhost.localdomain", socket.gethostname()})
        policy.networks.extend(
            [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
        )
        for url in [*extra_urls, env.get("CITADEL_INFERENCE_ENDPOINT", ""), env.get("CITADEL_SANDBOX_URL", "")]:
            if url:
                parts = urlsplit(url if "://" in url else f"http://{url}")
                if parts.hostname:
                    policy.allow(parts.hostname, parts.port)
        if env.get("PGHOST") and not env["PGHOST"].startswith("/"):
            policy.allow(env["PGHOST"], int(env.get("PGPORT") or 5432))
        for entry in (env.get(ALLOW_VAR) or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            host, _, port = entry.rpartition(":") if entry.count(":") == 1 else (entry, "", "")
            policy.allow(host or entry, int(port) if port.isdigit() else None)
        for cidr in (env.get("CITADEL_EGRESS_NETWORKS") or "").split(","):
            if cidr.strip():
                policy.networks.append(ipaddress.ip_network(cidr.strip(), strict=False))
        if env.get("CITADEL_IN_CONTAINER") == "1":
            # Inside a container the directly attached subnets are the Compose networks;
            # on a bare host they would include the office LAN, which is not internal.
            policy.networks.extend(attached_networks())
        return policy

    def allow(self, host: str, port: Optional[int]) -> None:
        host = host.strip().strip("[]").lower()
        self.endpoints.add((host, port))
        if _is_ip(host):
            self.addresses.add(host)
        else:
            self.names.add(host)
            try:
                for info in socket.getaddrinfo(host, None):
                    self.addresses.add(str(info[4][0]))
            except OSError:
                pass  # resolved again at the moment of use

    def name_allowed(self, name: str) -> bool:
        name = name.strip().strip("[]").lower().rstrip(".")
        if _is_ip(name):
            return self.address_allowed(name)
        return name in self.names

    def address_allowed(self, address: str, port: Optional[int] = None) -> bool:
        address = address.strip().strip("[]").lower()
        if address in self.addresses:
            return True
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return self.name_allowed(address)
        return any(ip in network for network in self.networks)

    def describe(self) -> dict[str, object]:
        return {
            "endpoints": sorted(f"{h}:{p}" if p else h for h, p in self.endpoints),
            "networks": [str(n) for n in self.networks],
        }


def attached_networks() -> list[ipaddress.IPv4Network]:
    """The subnets of this host's (or container's) own interfaces, from the routing
    table -- the Compose networks a container sits on -- excluding the default route.
    Linux only; elsewhere the list is empty and only explicit endpoints are internal."""
    networks: list[ipaddress.IPv4Network] = []
    try:
        with open("/proc/net/route", encoding="ascii") as handle:
            next(handle)
            for line in handle:
                fields = line.split()
                destination, mask = fields[1], fields[7]
                if destination == "00000000":
                    continue
                network = ipaddress.IPv4Network(
                    (int.from_bytes(bytes.fromhex(destination), "little"), bin(int.from_bytes(bytes.fromhex(mask), "little")).count("1")),
                    strict=False,
                )
                networks.append(network)
    except (OSError, StopIteration, ValueError):
        pass
    return networks


__all__ = ["EgressPolicy", "ALLOW_VAR", "attached_networks", "is_ip"]
