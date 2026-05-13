#!/usr/bin/env bash
set -euo pipefail

# Confirm mounted workspace and visible repository files.
pwd && ls -la

# Runtime presence check.
.bootstrap/venv/bin/python --version
