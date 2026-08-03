#!/usr/bin/env bash
set -Eeuo pipefail

WEBAPP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec "${WEBAPP_ROOT}/.venv/bin/python" \
  "${WEBAPP_ROOT}/scripts/shareholder_v2.py" publisher
