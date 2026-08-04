#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LIBERTY_SOURCE_ROOT="$(cd -- "${PROJECT_ROOT}/.." && pwd)"

if [[ "${1:-}" == "--smoke-only" ]]; then
  SMOKE_ROOT="${2:-}"
  if [[ -z "${SMOKE_ROOT}" || "${SMOKE_ROOT}" != /tmp/* || "${SMOKE_ROOT}" == *".."* ]]; then
    echo "--smoke-only requires an explicit safe /tmp root" >&2
    exit 2
  fi
  SMOKE_RELEASE_ID="incoming-$(date -u +%Y%m%dT%H%M%SZ)-$(sha256sum "${PROJECT_ROOT}/scripts/shareholder_v2.py" | cut -c1-10)"
  SMOKE_RELEASE="${SMOKE_ROOT}/releases/${SMOKE_RELEASE_ID}"
  install -d -m 0755 "${SMOKE_RELEASE}/config" "${SMOKE_RELEASE}/scripts/support"
  cp -a "${PROJECT_ROOT}/liberty_v2" "${PROJECT_ROOT}/analysis" "${SMOKE_RELEASE}/"
  install -m 0644 \
    "${PROJECT_ROOT}/config/metric_policy_v2.json" \
    "${PROJECT_ROOT}/config/metric_definitions_v2.json" \
    "${PROJECT_ROOT}/config/shareholder_v2_activation_reviews.json" \
    "${PROJECT_ROOT}/config/issuer_capital_structure_v1.json" \
    "${PROJECT_ROOT}/config/watchlist.json" \
    "${SMOKE_RELEASE}/config/"
  install -m 0644 "${LIBERTY_SOURCE_ROOT}/data/source/companies.json" "${SMOKE_RELEASE}/config/companies_v1.json"
  install -m 0755 "${PROJECT_ROOT}/scripts/shareholder_v2.py" "${SMOKE_RELEASE}/scripts/"
  install -m 0755 "${PROJECT_ROOT}"/scripts/support/*.py "${SMOKE_RELEASE}/scripts/support/"
  python3 -m venv --system-site-packages "${SMOKE_RELEASE}/.venv"
  SMOKE_RUNTIME="${SMOKE_ROOT}/runtime"
  env \
    SHAREHOLDER_SCREEN_ENABLED=false \
    CODEX_ANALYSIS_MODE=OFF \
    SHAREHOLDER_V2_LOCAL_ROOT="${SMOKE_RUNTIME}" \
    SHAREHOLDER_V2_STAGING_DIR="${SMOKE_RUNTIME}/staging" \
    ANALYSIS_JOB_DB="${SMOKE_RUNTIME}/analysis/jobs.sqlite3" \
    "${SMOKE_RELEASE}/.venv/bin/python" "${SMOKE_RELEASE}/scripts/shareholder_v2.py" health-check
  "${SMOKE_RELEASE}/.venv/bin/python" - "${SMOKE_RELEASE}/config/watchlist.json" "${SMOKE_RELEASE}/config/companies_v1.json" <<'PY'
import json
import sys
from pathlib import Path

watchlist = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
companies = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
watchlist_ids = {str(row.get("issuerId") or "") for row in watchlist.get("securities", [])}
company_rows = companies.get("companies", companies) if isinstance(companies, dict) else companies
company_ids = {str(row.get("issuerId") or row.get("companyId") or row.get("company_id") or "") for row in company_rows}
watchlist_ids.discard("")
company_ids.discard("")
if len(watchlist_ids) != 67 or watchlist_ids != company_ids:
    raise SystemExit(f"67-company configuration coverage failed: watchlist={len(watchlist_ids)} companies={len(company_ids)}")
print("incoming release smoke: 67-company configuration coverage passed")
PY
  ln -sfn "${SMOKE_RELEASE}" "${SMOKE_ROOT}/.current-${SMOKE_RELEASE_ID}"
  mv -Tf "${SMOKE_ROOT}/.current-${SMOKE_RELEASE_ID}" "${SMOKE_ROOT}/current"
  echo "Smoke-only installation passed: ${SMOKE_RELEASE}"
  echo "Smoke-only current: $(readlink -f "${SMOKE_ROOT}/current")"
  exit 0
fi

COLLECTOR_USER="$(stat -c '%U' "${PROJECT_ROOT}")"
SERVICE_USER="${LIBERTY_SERVICE_USER:-${COLLECTOR_USER}}"
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 2
fi

if ! getent passwd "${SERVICE_USER}" >/dev/null; then
  echo "LIBERTY_SERVICE_USER does not exist: ${SERVICE_USER}" >&2
  exit 2
fi
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
SERVICE_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
CODEX_HOME_DIR="${LIBERTY_CODEX_HOME:-${SERVICE_HOME}/.codex}"

RELEASE_ROOT=/opt/liberty/shareholder-v2
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(sha256sum "${PROJECT_ROOT}/scripts/shareholder_v2.py" | cut -c1-10)"
RELEASE_DIR="${RELEASE_ROOT}/releases/${RELEASE_ID}"
PREVIOUS_RELEASE=""
if [[ -L "${RELEASE_ROOT}/current" ]]; then
  PREVIOUS_RELEASE="$(readlink -f "${RELEASE_ROOT}/current")"
fi
install -d -m 0755 "${RELEASE_DIR}/config" "${RELEASE_DIR}/scripts/support"
cp -a "${PROJECT_ROOT}/liberty_v2" "${PROJECT_ROOT}/analysis" "${RELEASE_DIR}/"
install -m 0644 \
  "${PROJECT_ROOT}/config/metric_policy_v2.json" \
  "${PROJECT_ROOT}/config/metric_definitions_v2.json" \
  "${PROJECT_ROOT}/config/shareholder_v2_activation_reviews.json" \
  "${PROJECT_ROOT}/config/issuer_capital_structure_v1.json" \
  "${PROJECT_ROOT}/config/watchlist.json" \
  "${RELEASE_DIR}/config/"
install -m 0644 "${LIBERTY_SOURCE_ROOT}/data/source/companies.json" "${RELEASE_DIR}/config/companies_v1.json"
install -m 0755 "${PROJECT_ROOT}/scripts/shareholder_v2.py" "${RELEASE_DIR}/scripts/"
install -m 0755 "${PROJECT_ROOT}/scripts/publish_shareholder_v2.sh" "${RELEASE_DIR}/scripts/"
install -m 0755 "${PROJECT_ROOT}"/scripts/support/*.py "${RELEASE_DIR}/scripts/support/"
install -m 0644 "${PROJECT_ROOT}/requirements-worker.txt" "${RELEASE_DIR}/"
python3 -m venv "${RELEASE_DIR}/.venv"
"${RELEASE_DIR}/.venv/bin/pip" install --disable-pip-version-check -r "${RELEASE_DIR}/requirements-worker.txt"
chown -R root:root "${RELEASE_DIR}"
find "${RELEASE_DIR}" -type d -exec chmod 0755 {} +

install -d -m 0750 -o root -g root /etc/liberty
install -d -m 0710 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" /var/lib/liberty
install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${CODEX_HOME_DIR}"
install -d -m 2770 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" /var/lib/liberty/shareholder-v2
install -d -m 2770 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" \
  /var/lib/liberty/shareholder-v2/analysis \
  /var/lib/liberty/shareholder-v2/analysis/jobs \
  /var/lib/liberty/shareholder-v2/analysis/output \
  /var/lib/liberty/shareholder-v2/status
install -d -m 2770 -o "${COLLECTOR_USER}" -g "${SERVICE_GROUP}" /var/lib/liberty/shareholder-v2/inputs
JOB_DATABASE=/var/lib/liberty/shareholder-v2/analysis/jobs.sqlite3
if [[ ! -e "${JOB_DATABASE}" ]]; then
  install -m 0660 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" /dev/null "${JOB_DATABASE}"
else
  chown "${SERVICE_USER}:${SERVICE_GROUP}" "${JOB_DATABASE}"
  chmod 0660 "${JOB_DATABASE}"
fi

# The incoming release must prove that its own interpreter and copied configuration
# are healthy before it is allowed to replace the active symlink.
runuser -u "${SERVICE_USER}" -- env \
  HOME="${SERVICE_HOME}" \
  CODEX_HOME="${CODEX_HOME_DIR}" \
  SHAREHOLDER_SCREEN_ENABLED=false \
  CODEX_ANALYSIS_MODE=OFF \
  SHAREHOLDER_V2_LOCAL_ROOT=/var/lib/liberty/shareholder-v2 \
  SHAREHOLDER_V2_STAGING_DIR=/var/lib/liberty/shareholder-v2/staging \
  ANALYSIS_JOB_DB="${JOB_DATABASE}" \
  "${RELEASE_DIR}/.venv/bin/python" "${RELEASE_DIR}/scripts/shareholder_v2.py" health-check
"${RELEASE_DIR}/.venv/bin/python" - "${RELEASE_DIR}/config/watchlist.json" "${RELEASE_DIR}/config/companies_v1.json" <<'PY'
import json
import sys
from pathlib import Path

watchlist = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
companies = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
watchlist_ids = {str(row.get("issuerId") or "") for row in watchlist.get("securities", [])}
company_rows = companies.get("companies", companies) if isinstance(companies, dict) else companies
company_ids = {
    str(row.get("issuerId") or row.get("companyId") or row.get("company_id") or "")
    for row in company_rows
}
watchlist_ids.discard("")
company_ids.discard("")
if len(watchlist_ids) != 67 or len(company_ids) != 67 or watchlist_ids != company_ids:
    raise SystemExit(
        f"67-company configuration coverage failed: watchlist={len(watchlist_ids)} "
        f"companies={len(company_ids)} symmetric_diff={sorted(watchlist_ids ^ company_ids)}"
    )
print("incoming release smoke: 67-company configuration coverage passed")
PY

ln -sfn "${RELEASE_DIR}" "${RELEASE_ROOT}/.current-${RELEASE_ID}"
mv -Tf "${RELEASE_ROOT}/.current-${RELEASE_ID}" "${RELEASE_ROOT}/current"
echo "Incoming release smoke passed; atomically switched current from ${PREVIOUS_RELEASE:-none}."
if [[ ! -f /etc/liberty/shareholder-v2.env ]]; then
  install -m 0600 -o root -g root "${PROJECT_ROOT}/.env.example" /etc/liberty/shareholder-v2.env
  echo "Created /etc/liberty/shareholder-v2.env; fill non-secret host/path values and provision Codex auth before starting services."
fi
if command -v codex >/dev/null 2>&1; then
  CODEX_SOURCE="$(readlink -f "$(command -v codex)")"
  CODEX_SOURCE_DIR="$(dirname -- "${CODEX_SOURCE}")"
  install -d -m 0755 /opt/liberty/codex/bin
  install -m 0755 "${CODEX_SOURCE}" /opt/liberty/codex/bin/codex
  if [[ -x "${CODEX_SOURCE_DIR}/codex-code-mode-host" ]]; then
    install -m 0755 "${CODEX_SOURCE_DIR}/codex-code-mode-host" /opt/liberty/codex/bin/codex-code-mode-host
  fi
  sed -i 's#^CODEX_BINARY=.*#CODEX_BINARY=/opt/liberty/codex/bin/codex#' /etc/liberty/shareholder-v2.env
fi
for unit in "${PROJECT_ROOT}"/systemd/shareholder-*.service; do
  unit_name="$(basename "${unit}")"
  sed \
    -e "s/^User=.*/User=${SERVICE_USER}/" \
    -e "s/^Group=.*/Group=${SERVICE_GROUP}/" \
    -e "s#^Environment=HOME=.*#Environment=HOME=${SERVICE_HOME}#" \
    -e "s#^Environment=CODEX_HOME=.*#Environment=CODEX_HOME=${CODEX_HOME_DIR}#" \
    -e "s#/var/lib/liberty/.codex#${CODEX_HOME_DIR}#g" \
    "${unit}" > "/etc/systemd/system/${unit_name}"
  chmod 0644 "/etc/systemd/system/${unit_name}"
done
install -m 0644 "${PROJECT_ROOT}"/systemd/shareholder-*.timer /etc/systemd/system/
install -m 0644 "${PROJECT_ROOT}/deploy/logrotate/shareholder-v2" /etc/logrotate.d/shareholder-v2
systemctl daemon-reload
systemctl enable shareholder-data-pipeline.timer shareholder-codex-worker.service shareholder-publisher.timer
echo "Installed worker code release ${RELEASE_ID}."
echo "Add SHAREHOLDER_V2_QUOTE_SNAPSHOT=/var/lib/liberty/shareholder-v2/inputs/latest_snapshot.json to the existing collector env."
echo "Worker service user: ${SERVICE_USER}; no separate liberty-codex account is required."
echo "If this user is not already authenticated, run:"
echo "sudo -u ${SERVICE_USER} env HOME=${SERVICE_HOME} CODEX_HOME=${CODEX_HOME_DIR} /opt/liberty/codex/bin/codex login"
echo "Validate /etc/liberty/shareholder-v2.env, stage the one-time migration, then start services:"
echo "sudo -u ${SERVICE_USER} env SHAREHOLDER_V2_LOCAL_ROOT=/var/lib/liberty/shareholder-v2 SHAREHOLDER_V2_STAGING_DIR=/var/lib/liberty/shareholder-v2/staging /opt/liberty/shareholder-v2/current/.venv/bin/python /opt/liberty/shareholder-v2/current/scripts/shareholder_v2.py migrate --companies /opt/liberty/shareholder-v2/current/config/companies_v1.json --watchlist /opt/liberty/shareholder-v2/current/config/watchlist.json --apply"
echo "systemctl start shareholder-codex-worker.service shareholder-data-pipeline.timer shareholder-publisher.timer"
