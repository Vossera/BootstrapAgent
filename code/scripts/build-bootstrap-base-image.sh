#!/usr/bin/env bash
set -euo pipefail

IMAGE_TAG="${1:-rethink-bootstrap-base:latest}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

docker build \
  -t "${IMAGE_TAG}" \
  -f "${CODE_DIR}/docker/bootstrap-base/Dockerfile" \
  "${CODE_DIR}"

printf 'Built %s\n' "${IMAGE_TAG}"
