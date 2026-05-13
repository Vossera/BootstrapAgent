#!/usr/bin/env bash
set -euo pipefail

# Install Python runtime, venv support, pip, headers, and common native build tools before creating the bootstrap virtual environment.
apt-get update && apt-get install -y python3 python3-venv python3-pip python3-dev build-essential pkg-config

# Create isolated Python virtual environment for project dependencies.
python3 -m venv .bootstrap/venv

# Upgrade pip and wheel inside the bootstrap virtual environment.
.bootstrap/venv/bin/python -m pip install -U pip wheel

# Install Python project and test dependencies from the code package.
.bootstrap/venv/bin/python -m pip install -e "code[dev]"
