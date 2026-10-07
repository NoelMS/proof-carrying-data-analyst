#!/bin/sh
# Start Proof-Carrying Data Analyst (macOS / Linux). First start sets everything up.
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer is required: https://www.python.org/downloads/"
  exit 1
fi
exec "$PY" launch.pyw "$@"
