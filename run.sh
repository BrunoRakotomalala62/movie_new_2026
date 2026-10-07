#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install --quiet -r requirements.txt
exec python3 -m uvicorn app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8077}" --reload
