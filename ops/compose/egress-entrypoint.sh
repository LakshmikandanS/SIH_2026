#!/usr/bin/env bash
# ops/compose/egress-entrypoint.sh <command...>
#
# Runs as root for exactly one purpose: to install a default-deny egress ruleset
# (ops/nftables/citadel-egress.nft) in THIS container's own network namespace, then
# drop every privilege and exec the application as the unprivileged `citadel` user.
# The container is granted NET_ADMIN (the ruleset), SETUID/SETGID (the user switch) and
# SETPCAP (emptying the capability bounding set) -- and nothing else; the application
# runs with no capabilities at all and no_new_privs set, so it can never undo the rules.
# Without SETPCAP, setpriv's --bounding-set fails with EPERM and the container never
# starts: found by running this script under exactly that capability set, before it
# ever reached Docker.
#
# What it writes to /run/citadel/enforcement.json is the enforcement half's own
# statement of what it did -- the sovereignty panel shows it beside the telemetry, and
# never infers one from the other. If the ruleset cannot be applied (a kernel without
# nf_tables, a missing NET_ADMIN capability) the statement says so, in words, and the
# application still starts with its in-process fence -- unless
# CITADEL_REQUIRE_ENFORCEMENT=1, in which case it refuses to start at all.

set -u

STATUS_DIR=/run/citadel
mkdir -p "$STATUS_DIR"
TEMPLATE=/app/ops/nftables/citadel-egress.nft
RULES="$STATUS_DIR/egress.nft"

endpoint="${CITADEL_INFERENCE_ENDPOINT:-http://host.docker.internal:11434}"
hostport="${endpoint#*://}"
hostport="${hostport%%/*}"
inference_host="${hostport%%:*}"
inference_port="${hostport##*:}"
[ "$inference_port" = "$hostport" ] && inference_port=11434
if [[ "$inference_host" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
    inference_ip="$inference_host"   # an address already; getaddrinfo is not needed (or always able) to say so
else
    inference_ip="$(getent ahostsv4 "$inference_host" 2>/dev/null | awk 'NR==1 {print $1}')"
fi

cidrs="${CITADEL_EGRESS_NETWORKS:-}"
cidrs="${cidrs//,/, }"
[ -z "$cidrs" ] && cidrs="127.0.0.0/8"
untrusted="${CITADEL_UNTRUSTED_NETWORKS:-}"
untrusted="${untrusted//,/, }"

sed -e "s|@INTERNAL_CIDRS@|$cidrs|g" \
    -e "s|@INFERENCE_PORT@|$inference_port|g" \
    "$TEMPLATE" > "$RULES"
if [ -n "$untrusted" ]; then
    sed -i "s|@UNTRUSTED_CIDRS@|$untrusted|g" "$RULES"
else
    sed -i "/@UNTRUSTED_CIDRS@/d" "$RULES"
fi
if [ -n "$inference_ip" ]; then
    sed -i "s|@INFERENCE_IP@|$inference_ip|g" "$RULES"
else
    # No address for the inference host: leave the rule out rather than guess.
    sed -i "/@INFERENCE_IP@/d" "$RULES"
fi

applied=false
error=""
if command -v nft >/dev/null 2>&1; then
    nft delete table inet citadel_egress >/dev/null 2>&1 || true
    if error="$(nft -f "$RULES" 2>&1)"; then
        applied=true
        error=""
    fi
else
    error="nft is not installed in this image"
fi

APPLIED="$applied" ERROR="$error" RULES_FILE="$RULES" CIDRS="$cidrs" UNTRUSTED="$untrusted" \
INFERENCE="${inference_ip:-unresolved}:${inference_port} (${inference_host})" \
python3 - <<'PY'
import json, os
from datetime import datetime, timezone
applied = os.environ["APPLIED"] == "true"
status = {
    "mechanism": "nftables",
    "applied": applied,
    "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "scope": "this container's network namespace: default-deny OUTPUT, and new inbound connections from "
             "the sandbox network refused",
    "allowed": ["loopback", "replies to inbound connections", *[c.strip() for c in os.environ["CIDRS"].split(",") if c.strip()],
                f"{os.environ['INFERENCE']} -- the inference runtime"],
    "refused_in": [c.strip() for c in os.environ["UNTRUSTED"].split(",") if c.strip()],
    "rules": open(os.environ["RULES_FILE"], encoding="utf-8").read(),
    # Said plainly, because the panel must not imply more than the ruleset does
    # (docs/adr/0006): these rules bind this container. The inference runtime runs on
    # the Docker host, and the host's own egress is governed by the host.
    "outside": "the Docker host itself, including the inference runtime it serves -- its own egress is governed "
               "by the host's firewall, not by this ruleset (docs/adr/0006)",
    "note": (
        "Default-deny egress is applied in this container. The sandbox and the database sit on internal-only "
        "networks with no route out at all."
        if applied else
        f"The egress ruleset could NOT be applied ({os.environ['ERROR'].strip()[:300]}). Only the in-process fence "
        "stands between this process and the network."
    ),
}
with open("/run/citadel/enforcement.json", "w", encoding="utf-8") as handle:
    json.dump(status, handle, indent=2)
PY
chmod 0755 "$STATUS_DIR"
chmod 0644 "$STATUS_DIR/enforcement.json" "$RULES"

if [ "$applied" = true ]; then
    echo "[egress] default-deny ruleset applied (internal: $cidrs; inference: ${inference_ip:-unresolved}:$inference_port; refused inbound: ${untrusted:-none})"
else
    echo "[egress] WARNING: ruleset NOT applied: $error"
    if [ "${CITADEL_REQUIRE_ENFORCEMENT:-0}" = 1 ]; then
        echo "[egress] CITADEL_REQUIRE_ENFORCEMENT=1 -- refusing to start without network-level enforcement"
        exit 1
    fi
fi

exec setpriv --reuid=10001 --regid=10001 --init-groups --inh-caps=-all --bounding-set=-all --no-new-privs "$@"
