#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
OPEN_D_DIR="${INSTALL_DIR}/Futu_OpenD_10.9.6918_Ubuntu18.04/Futu_OpenD_10.9.6918_Ubuntu18.04"
OPEN_D_BIN="${OPEN_D_DIR}/FutuOpenD"
CONFIG_FILE="${SCRIPT_DIR}/FutuOpenD.xml"
ACCOUNT_FILE="${SCRIPT_DIR}/state/login_account"

if [[ ! -x "${OPEN_D_BIN}" ]]; then
    printf '找不到 OpenD：%s\n' "${OPEN_D_BIN}" >&2
    exit 1
fi
if [[ ! -r "${ACCOUNT_FILE}" ]]; then
    printf '尚未完成首次登录，请先运行 first-login.sh。\n' >&2
    exit 1
fi

LOGIN_ACCOUNT="$(tr -d '[:space:]' < "${ACCOUNT_FILE}")"
if [[ ! "${LOGIN_ACCOUNT}" =~ ^[0-9]+$ ]]; then
    printf '保存的牛牛号格式不正确，请重新运行 first-login.sh。\n' >&2
    exit 1
fi

cd -- "${OPEN_D_DIR}"
exec "${OPEN_D_BIN}" \
    "-cfg_file=${CONFIG_FILE}" \
    "-login_account=${LOGIN_ACCOUNT}" \
    -login_by_remember=1 \
    -remember=1 \
    -console=1 \
    -no_monitor=1 \
    -simulate_trade=disable
