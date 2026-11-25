#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 -m venv "${SCRIPT_DIR}/.venv"
"${SCRIPT_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${SCRIPT_DIR}/.venv/bin/python" -m pip install -r "${SCRIPT_DIR}/requirements.txt"
echo "安装完成：${SCRIPT_DIR}/.venv"
