#!/usr/bin/env bash
# Local-dev wrapper around ``podman-compose up``.
#
# Single source of truth: ``config/config.yaml``. Copy
# ``config/config.yaml.example`` to ``config/config.yaml`` and
# populate every required field before running this script. If a
# required value is missing, the stack refuses to start --- compose
# ``:?required`` checks enforce this at startup time.
#
# Usage:
#   ./scripts/up.sh                    # all services, foreground
#   ./scripts/up.sh -d                 # detached
#   ./scripts/up.sh backend frontend   # specific services

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${REPO_ROOT}/config/config.yaml"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "error: ${CONFIG_FILE} is missing." >&2
    echo "       run: cp config/config.yaml.example config/config.yaml" >&2
    echo "       then edit the file in place; every required field is documented inline." >&2
    exit 1
fi

# Translate the structured YAML into the flat env-var vocabulary the
# compose file substitutes. The mapping lives in scripts/_config_export.py.
set -a
eval "$(python3 "$REPO_ROOT/scripts/_config_export.py")"
set +a

cd "$REPO_ROOT"
exec podman-compose up "$@"
