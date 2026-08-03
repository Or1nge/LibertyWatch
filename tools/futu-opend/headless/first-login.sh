#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
OPEN_D_DIR="${INSTALL_DIR}/Futu_OpenD_10.9.6918_Ubuntu18.04/Futu_OpenD_10.9.6918_Ubuntu18.04"
OPEN_D_BIN="${OPEN_D_DIR}/FutuOpenD"
TEMPLATE="${SCRIPT_DIR}/FutuOpenD.first-login.xml.in"
STATE_DIR="${SCRIPT_DIR}/state"
ACCOUNT_FILE="${STATE_DIR}/login_account"

if [[ ! -x "${OPEN_D_BIN}" ]]; then
    printf '找不到 OpenD：%s\n' "${OPEN_D_BIN}" >&2
    exit 1
fi

LOGIN_ACCOUNT="${1:-}"
if [[ -z "${LOGIN_ACCOUNT}" ]]; then
    read -r -p '请输入富途牛牛号（仅数字）: ' LOGIN_ACCOUNT
fi
if [[ ! "${LOGIN_ACCOUNT}" =~ ^[0-9]+$ ]]; then
    printf '牛牛号格式不正确：只允许数字。\n' >&2
    exit 1
fi

read -r -s -p '请输入富途登录密码（输入不会显示）: ' LOGIN_PASSWORD
printf '\n'
if [[ -z "${LOGIN_PASSWORD}" ]]; then
    printf '密码不能为空。\n' >&2
    exit 1
fi

mkdir -p -- "${STATE_DIR}"
chmod 700 -- "${STATE_DIR}"
printf '%s\n' "${LOGIN_ACCOUNT}" > "${ACCOUNT_FILE}"
chmod 600 -- "${ACCOUNT_FILE}"

LOGIN_PWD_MD5="$(printf '%s' "${LOGIN_PASSWORD}" | md5sum | awk '{print $1}')"
unset LOGIN_PASSWORD

TEMP_CONFIG="$(mktemp "${STATE_DIR}/FutuOpenD.first-login.XXXXXX.xml")"
chmod 600 -- "${TEMP_CONFIG}"
sed \
    -e "s/__LOGIN_ACCOUNT__/${LOGIN_ACCOUNT}/g" \
    -e "s/__LOGIN_PWD_MD5__/${LOGIN_PWD_MD5}/g" \
    "${TEMPLATE}" > "${TEMP_CONFIG}"
unset LOGIN_PWD_MD5

# OpenD 在启动阶段立即读取配置；稍后删除短期凭据文件。
( sleep 10; rm -f -- "${TEMP_CONFIG}" ) &

printf '正在启动命令行 OpenD。若提示需要设备锁验证，请按后续引导操作。\n'
cd -- "${OPEN_D_DIR}"
exec "${OPEN_D_BIN}" \
    "-cfg_file=${TEMP_CONFIG}" \
    -console=1 \
    -no_monitor=1 \
    -remember=1 \
    -simulate_trade=disable
