#!/usr/bin/env bash
# ops/compose/init.sh -- the one-shot setup container: keys, migrations, ownership.
# Idempotent; runs on every `citadel.cmd` start and does nothing the second time.
set -euo pipefail

python -m citadel_platform.keyring init --dir /keys
python -m citadel_platform.migrations up --create-database --wait 90

# The API and worker run as uid 10001 and may read the private keys; the sandbox runs
# as uid 10002 and can read only the receipt PUBLIC key, which is all a verifier needs.
chown -R 10001:10001 /keys /data
chmod 0755 /keys
chmod 0600 /keys/session.key /keys/receipt.key
chmod 0644 /keys/receipt.pub
echo "[init] keys ready, database migrated"
