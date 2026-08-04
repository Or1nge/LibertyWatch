#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WEBAPP_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

REMOTE_HOST="${LIBERTY_REMOTE_HOST:-}"
REMOTE_BASE="${LIBERTY_REMOTE_BASE:-}"
PUBLIC_PORT="${LIBERTY_PUBLIC_PORT:-}"
PUBLIC_HOST="${LIBERTY_PUBLIC_HOST:-}"
V2_ENABLED="${SHAREHOLDER_RETURN_V2_ENABLED:-false}"
V2_CANARY_INDEX="${SHAREHOLDER_V2_CANARY_INDEX:-${WEBAPP_DIR}/../data/shareholder-v2/published/structured/current/companies.json}"
DRY_RUN=0
SKIP_TESTS=0

usage() {
  echo "Usage: $0 [--dry-run] [--skip-tests]"
  echo
  echo "Requires LIBERTY_REMOTE_HOST, LIBERTY_REMOTE_BASE, LIBERTY_PUBLIC_HOST and LIBERTY_PUBLIC_PORT."
  echo "LIBERTY_REMOTE_HOST may be an existing SSH config/tunnel alias."
  echo "Research data, images, PDFs, Futu binaries, environments and credentials are excluded."
}

while (($#)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --skip-tests)
      SKIP_TESTS=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

for required_name in LIBERTY_REMOTE_HOST LIBERTY_REMOTE_BASE LIBERTY_PUBLIC_HOST LIBERTY_PUBLIC_PORT; do
  if [[ -z "${!required_name:-}" ]]; then
    echo "Required deployment environment variable is missing: ${required_name}" >&2
    exit 2
  fi
done

if [[ ! "${REMOTE_BASE}" =~ ^/[A-Za-z0-9._/-]+$ ]] ||
  [[ "${REMOTE_BASE}" =~ (^|/)\.\.(/|$) ]]; then
  echo "Unsafe LIBERTY_REMOTE_BASE: ${REMOTE_BASE}" >&2
  exit 2
fi
if [[ ! "${REMOTE_HOST}" =~ ^[A-Za-z0-9._@-]+$ ]] || [[ "${REMOTE_HOST}" == -* ]]; then
  echo "Unsafe LIBERTY_REMOTE_HOST: ${REMOTE_HOST}" >&2
  exit 2
fi
if [[ ! "${PUBLIC_PORT}" =~ ^[0-9]+$ ]] || ((PUBLIC_PORT < 1024 || PUBLIC_PORT > 65535)); then
  echo "Invalid LIBERTY_PUBLIC_PORT: ${PUBLIC_PORT}" >&2
  exit 2
fi
if [[ ! "${PUBLIC_HOST}" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "Invalid LIBERTY_PUBLIC_HOST: ${PUBLIC_HOST}" >&2
  exit 2
fi
if [[ "${V2_ENABLED}" != "true" && "${V2_ENABLED}" != "false" ]]; then
  echo "SHAREHOLDER_RETURN_V2_ENABLED must be true or false" >&2
  exit 2
fi

PYTHON_BIN="${WEBAPP_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

SSH_OPTIONS=(
  -o BatchMode=yes
  -o ConnectTimeout=15
)

if ((SKIP_TESTS == 0)); then
  "${PYTHON_BIN}" -m pytest -q "${WEBAPP_DIR}/tests"
  "${PYTHON_BIN}" -m compileall -q \
    "${WEBAPP_DIR}/app" \
    "${WEBAPP_DIR}/collector" \
    "${WEBAPP_DIR}/liberty_v2" \
    "${WEBAPP_DIR}/scripts/shareholder_v2.py"
  if command -v node >/dev/null 2>&1; then
    node --test "${WEBAPP_DIR}"/frontend-tests/*.test.mjs
    node --check "${WEBAPP_DIR}/public/app.js"
  fi
fi

if [[ "${V2_ENABLED}" == "true" ]]; then
  if [[ ! -f "${V2_CANARY_INDEX}" ]]; then
    echo "v2 activation canary index is missing: ${V2_CANARY_INDEX}" >&2
    exit 1
  fi
  "${PYTHON_BIN}" "${WEBAPP_DIR}/scripts/support/validate_v2_canary.py" \
    --companies "${V2_CANARY_INDEX}" \
    --reviews "${WEBAPP_DIR}/config/shareholder_v2_activation_reviews.json"
fi

TEMP_DIR="$(mktemp -d -t liberty-watch-deploy.XXXXXX)"
cleanup() {
  rm -rf -- "${TEMP_DIR}"
}
trap cleanup EXIT

ARCHIVE="${TEMP_DIR}/liberty-watch.tar.gz"
CHECKSUM="${ARCHIVE}.sha256"

INCLUDE_PATHS=(
  app
  liberty_v2
  collector
  config/watchlist.json
  config/demo-watchlist.json
  config/metric_definitions_v2.json
  config/metric_policy_v2.json
  config/shareholder_v2_activation_reviews.json
  vendor/wheels
  public
  Dockerfile
  compose.yaml
  .dockerignore
  requirements.txt
)

for path in "${INCLUDE_PATHS[@]}"; do
  if [[ ! -e "${WEBAPP_DIR}/${path}" ]]; then
    echo "Required deployment path is missing: ${WEBAPP_DIR}/${path}" >&2
    exit 1
  fi
done

tar \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  -C "${WEBAPP_DIR}" \
  -czf "${ARCHIVE}" \
  "${INCLUDE_PATHS[@]}"

(
  cd -- "${TEMP_DIR}"
  sha256sum "$(basename -- "${ARCHIVE}")" >"$(basename -- "${CHECKSUM}")"
)

RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(sha256sum "${ARCHIVE}" | cut -c1-10)"
REMOTE_RELEASE="${REMOTE_BASE}/releases/${RELEASE_ID}"

echo "Release: ${RELEASE_ID}"
echo "Target:  ${REMOTE_HOST}:${REMOTE_RELEASE}"
echo "Port:    ${PUBLIC_PORT}"
echo "Public:  ${PUBLIC_HOST}"
echo "V2:      ${V2_ENABLED}"
echo "Archive contents:"
tar -tzf "${ARCHIVE}"

if ((DRY_RUN == 1)); then
  echo "Dry run complete; no remote files or services were changed."
  exit 0
fi

PREVIOUS_RELEASE="$(
  ssh "${SSH_OPTIONS[@]}" -- "${REMOTE_HOST}" \
    "if [ -L '${REMOTE_BASE}/current' ]; then readlink -f '${REMOTE_BASE}/current'; fi"
)"
if [[ -n "${PREVIOUS_RELEASE}" ]] &&
  [[ "${PREVIOUS_RELEASE}" != "${REMOTE_BASE}"/releases/* ]]; then
  echo "Refusing unexpected previous release path: ${PREVIOUS_RELEASE}" >&2
  exit 1
fi

ssh "${SSH_OPTIONS[@]}" -- "${REMOTE_HOST}" \
  "install -d -m 0755 '${REMOTE_RELEASE}' '${REMOTE_BASE}/shared'"
scp -q "${SSH_OPTIONS[@]}" -- \
  "${ARCHIVE}" "${CHECKSUM}" "${REMOTE_HOST}:${REMOTE_RELEASE}/"

ssh "${SSH_OPTIONS[@]}" -- "${REMOTE_HOST}" \
  "REMOTE_BASE='${REMOTE_BASE}' REMOTE_RELEASE='${REMOTE_RELEASE}' PUBLIC_PORT='${PUBLIC_PORT}' SHAREHOLDER_RETURN_V2_ENABLED='${V2_ENABLED}' bash -s" <<'REMOTE'
set -Eeuo pipefail

archive="${REMOTE_RELEASE}/liberty-watch.tar.gz"
checksum="${archive}.sha256"
previous_release=""
if [[ -L "${REMOTE_BASE}/current" ]]; then
  previous_release="$(readlink -f "${REMOTE_BASE}/current" || true)"
fi

cd -- "${REMOTE_RELEASE}"
sha256sum -c "$(basename -- "${checksum}")"
tar -xzf "$(basename -- "${archive}")"
find "${REMOTE_RELEASE}" -type d -exec chmod 0755 {} +
find "${REMOTE_RELEASE}" -type f -exec chmod 0644 {} +
chmod 0755 "${REMOTE_RELEASE}/collector/push_quotes.py"

rollback() {
  status=$?
  trap - EXIT
  if ((status != 0)) && [[ -n "${previous_release}" ]] && [[ -f "${previous_release}/compose.yaml" ]]; then
    echo "Release failed; restoring previous Compose release ${previous_release}" >&2
    cd -- "${previous_release}"
    LIBERTY_SHARED_DIR="${REMOTE_BASE}/shared" \
      LIBERTY_PUBLIC_PORT="${PUBLIC_PORT}" \
      SHAREHOLDER_RETURN_V2_ENABLED="${SHAREHOLDER_RETURN_V2_ENABLED}" \
      docker compose -p liberty-watch up -d --build --wait --wait-timeout 120
  elif ((status != 0)) && [[ -f "${REMOTE_RELEASE}/compose.yaml" ]]; then
    echo "First release failed; stopping the incomplete Compose project." >&2
    cd -- "${REMOTE_RELEASE}"
  LIBERTY_SHARED_DIR="${REMOTE_BASE}/shared" \
    LIBERTY_PUBLIC_PORT="${PUBLIC_PORT}" \
    SHAREHOLDER_RETURN_V2_ENABLED="${SHAREHOLDER_RETURN_V2_ENABLED}" \
    docker compose -p liberty-watch down || true
  fi
  exit "${status}"
}
trap rollback EXIT

LIBERTY_SHARED_DIR="${REMOTE_BASE}/shared" \
  LIBERTY_PUBLIC_PORT="${PUBLIC_PORT}" \
  SHAREHOLDER_RETURN_V2_ENABLED="${SHAREHOLDER_RETURN_V2_ENABLED}" \
  docker compose -p liberty-watch up -d --build --wait --wait-timeout 120

python3 - "${PUBLIC_PORT}" "${SHAREHOLDER_RETURN_V2_ENABLED}" "${REMOTE_RELEASE}" <<'PY'
import json
import sys
import urllib.request

port = int(sys.argv[1])
v2_enabled = sys.argv[2] == "true"
release = sys.argv[3]
paths = ["/healthz", "/readyz", "/api/watchlist"]
if v2_enabled:
    paths.extend(("/api/v1/metric-definitions", "/api/v1/companies"))
for path in paths:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
        if response.status != 200:
            raise SystemExit(f"{path} returned {response.status}")
        if path == "/api/watchlist":
            payload = json.load(response)
            if "meta" not in payload or "securities" not in payload:
                raise SystemExit("watchlist payload is incomplete")
        elif path == "/api/v1/companies":
            payload = json.load(response)
            sys.path.insert(0, release)
            from app.v2_contract import validate_activation_canary
            with open(
                f"{release}/config/shareholder_v2_activation_reviews.json",
                encoding="utf-8",
            ) as handle:
                reviews = json.load(handle)
            validate_activation_canary(
                payload,
                approved_company_ids=reviews.get("approved_company_ids", []),
                expected_company_count=int(reviews.get("expected_company_count", 67)),
                minimum_scored_companies=int(reviews.get("minimum_scored_companies", 5)),
            )
PY

trap - EXIT
echo "Release ${REMOTE_RELEASE} is healthy on the server and awaits public verification."
REMOTE

public_check() {
  curl -fsS --max-time 15 "http://${PUBLIC_HOST}:${PUBLIC_PORT}/healthz" >/dev/null
  curl -fsS --max-time 15 "http://${PUBLIC_HOST}:${PUBLIC_PORT}/readyz" >/dev/null
  curl -fsS --max-time 15 "http://${PUBLIC_HOST}:${PUBLIC_PORT}/api/watchlist" |
    "${PYTHON_BIN}" -c \
      'import json,sys; p=json.load(sys.stdin); assert "meta" in p and "securities" in p'
  if [[ "${V2_ENABLED}" == "true" ]]; then
    curl -fsS --max-time 15 "http://${PUBLIC_HOST}:${PUBLIC_PORT}/api/v1/metric-definitions" |
      "${PYTHON_BIN}" -c \
        'import json,sys; p=json.load(sys.stdin); assert p["definition_version"].startswith("shareholder-return-v2")'
    curl -fsS --max-time 15 "http://${PUBLIC_HOST}:${PUBLIC_PORT}/api/v1/companies" |
      "${PYTHON_BIN}" -c \
        'import json,sys; sys.path.insert(0,sys.argv[1]); from app.v2_contract import validate_activation_canary; r=json.load(open(sys.argv[2],encoding="utf-8")); validate_activation_canary(json.load(sys.stdin), approved_company_ids=r.get("approved_company_ids",[]), expected_company_count=int(r.get("expected_company_count",67)), minimum_scored_companies=int(r.get("minimum_scored_companies",5)))' \
        "${WEBAPP_DIR}" "${WEBAPP_DIR}/config/shareholder_v2_activation_reviews.json"
  fi
  curl -fsS --max-time 15 \
    "http://${PUBLIC_HOST}:${PUBLIC_PORT}/" \
    -o "${TEMP_DIR}/public-index.html"
  grep -q "Liberty" "${TEMP_DIR}/public-index.html"
  curl -fsS --max-time 15 \
    "http://${PUBLIC_HOST}:${PUBLIC_PORT}/app.js" >/dev/null
}

if ! public_check; then
  echo "Public verification failed; restoring the previous release." >&2
  ssh "${SSH_OPTIONS[@]}" -- "${REMOTE_HOST}" \
    "REMOTE_BASE='${REMOTE_BASE}' REMOTE_RELEASE='${REMOTE_RELEASE}' PREVIOUS_RELEASE='${PREVIOUS_RELEASE}' PUBLIC_PORT='${PUBLIC_PORT}' SHAREHOLDER_RETURN_V2_ENABLED='${V2_ENABLED}' bash -s" <<'REMOTE'
set -Eeuo pipefail
if [[ -n "${PREVIOUS_RELEASE}" ]] && [[ -f "${PREVIOUS_RELEASE}/compose.yaml" ]]; then
  cd -- "${PREVIOUS_RELEASE}"
  LIBERTY_SHARED_DIR="${REMOTE_BASE}/shared" \
    LIBERTY_PUBLIC_PORT="${PUBLIC_PORT}" \
    SHAREHOLDER_RETURN_V2_ENABLED="${SHAREHOLDER_RETURN_V2_ENABLED}" \
    docker compose -p liberty-watch up -d --build --wait --wait-timeout 120
else
  cd -- "${REMOTE_RELEASE}"
  LIBERTY_SHARED_DIR="${REMOTE_BASE}/shared" \
    LIBERTY_PUBLIC_PORT="${PUBLIC_PORT}" \
    SHAREHOLDER_RETURN_V2_ENABLED="${SHAREHOLDER_RETURN_V2_ENABLED}" \
    docker compose -p liberty-watch down
fi
REMOTE
  exit 1
fi

ssh "${SSH_OPTIONS[@]}" -- "${REMOTE_HOST}" \
  "ln -sfn '${REMOTE_RELEASE}' '${REMOTE_BASE}/current'"
echo "Deployment verified: http://${PUBLIC_HOST}:${PUBLIC_PORT}"
