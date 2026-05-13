#!/usr/bin/env bash
set -euo pipefail

# Compile Python package files as a low-cost smoke test.
.bootstrap/venv/bin/python -m compileall code

# Advisory: Run the project test suite.
set +e
.bootstrap/venv/bin/python -m pytest code/tests
advisory_code=$?
set -e
if [ "$advisory_code" -ne 0 ]; then echo "[rethink] advisory strongest_verify failed with exit_code=$advisory_code" >&2; fi
