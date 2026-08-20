#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

source "$PROJECT_DIR/.venv/bin/activate"

CUBLAS_LIB="$(
find "$VIRTUAL_ENV/lib" \
  -type f \
  -name 'libcublas.so.12' \
  -printf '%h\n' \
  | head -n 1
)"

CUDNN_LIB="$(
find "$VIRTUAL_ENV/lib" \
  -type f \
  -name 'libcudnn.so.9' \
  -printf '%h\n' \
  | head -n 1
)"

if [[ -z "$CUBLAS_LIB" ]]; then
  echo "ERROR: libcublas.so.12 was not found inside the virtual environment." >&2
  exit 1
fi

if [[ -z "$CUDNN_LIB" ]]; then
  echo "ERROR: libcudnn.so.9 was not found inside the virtual environment." >&2
  exit 1
fi

export LD_LIBRARY_PATH="$CUBLAS_LIB:$CUDNN_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

exec "$@"
